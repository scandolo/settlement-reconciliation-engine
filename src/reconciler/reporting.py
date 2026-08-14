"""Reconciliation reporting.

The customer is a reconciliation analyst closing the month, not an engineer.
Everything here is shaped by three things they actually need:

* an **exception worklist**, ordered by money at risk, not by file order
* an **action per line**, phrased as the next thing to do rather than a code
* the **same numbers in the format they already work in** -- terminal for ops,
  Markdown to paste into a ticket, CSV for the spreadsheet they live in, JSON
  for the dashboard and for any downstream automation.

One report model, four renderers, so the numbers can never disagree with
themselves.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from .engine import ReconciliationResult
from .models import (
    Discrepancy,
    DiscrepancyType,
    SettlementBatch,
    SettlementLine,
    Severity,
)
from .money import total

SEVERITY_ICON = {
    Severity.CRITICAL: "!!",
    Severity.HIGH: "! ",
    Severity.MEDIUM: "· ",
    Severity.LOW: "  ",
}


def to_dict(result: ReconciliationResult) -> dict:
    """The single source of truth every renderer reads from."""
    matched = [
        _matched_to_json(result, transaction_id)
        for transaction_id in sorted(result.matched_ids)
    ]
    batches = [_batch_to_json(result, batch) for batch in result.source_batches]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": result.as_of.isoformat() if result.as_of else None,
        "summary": {
            "batches_processed": result.batches,
            "settlement_lines": result.lines,
            "ledger_transactions": result.ledger_size,
            "awaiting_settlement": result.awaiting_settlement,
            "id_matched": len(result.matched_ids),
            "cleanly_reconciled": len(result.reconciled_ids),
            # Kept as a compatibility alias for existing dashboard consumers.
            "matched": len(result.reconciled_ids),
            "unmatched": result.unmatched_count,
            "id_match_rate": f"{result.id_match_rate:.2%}",
            "match_rate": f"{result.match_rate:.2%}",
            "discrepancies": len(result.discrepancies),
            "indicative_exposure_usd": str(result.exposure_usd()),
        },
        "settled_totals": {k: v.to_json() for k, v in sorted(result.totals_by_currency.items())},
        "indicative_totals_usd": {
            "gross": str(sum(
                (result.rules.usd_equivalent(value.gross) for value in result.totals_by_currency.values()),
                start=0,
            )),
            "fees": str(sum(
                (result.rules.usd_equivalent(value.fees) for value in result.totals_by_currency.values()),
                start=0,
            )),
            "net": str(sum(
                (result.rules.usd_equivalent(value.net) for value in result.totals_by_currency.values()),
                start=0,
            )),
        },
        "settled_by_processor": {
            k: v.to_json() for k, v in sorted(result.totals_by_processor.items())
        },
        "exposure_by_currency": {k: str(v) for k, v in result.exposure().items()},
        "by_severity": result.counts("severity"),
        "by_type": result.counts("type"),
        "ledger_file": {
            "name": "transactions.csv",
            "format": "csv",
            "transactions": result.ledger_size,
            "currencies": sorted(
                {transaction.currency for transaction in result.ledger.values()}
            ),
            "status": "ready",
            "warnings": [],
        },
        "files": [
            {
                "name": batch.source_file,
                "processor": batch.processor,
                "format": batch.source_format,
                "batch_id": batch.batch_id,
                "settlement_date": batch.settlement_date.isoformat(),
                "currencies": [batch.currency],
                "lines": len(batch.lines),
                "status": "ready",
                "warnings": [],
            }
            for batch in result.source_batches
        ],
        "batches": batches,
        "matched_transactions": matched,
        "discrepancies": [
            _discrepancy_to_json(result, finding) for finding in result.worklist()
        ],
    }


def _matched_to_json(result: ReconciliationResult, transaction_id: str) -> dict:
    """One inspectable matched transaction, including its exact source location."""
    transaction = result.ledger[transaction_id]
    batch, line = result.settlements[transaction_id][0]
    source = _line_source(batch, line)
    finding_ids = [
        finding.id
        for finding in result.discrepancies
        if transaction_id in finding.transaction_ids
        or (
            finding.processor == batch.processor
            and finding.batch_id == batch.batch_id
            and finding.type
            in {
                DiscrepancyType.BATCH_CURRENCY_MISMATCH,
                DiscrepancyType.BATCH_TOTAL_MISMATCH,
            }
        )
    ]
    return {
        "transaction_id": transaction_id,
        "processor": batch.processor,
        "batch_id": batch.batch_id,
        "currency": line.currency,
        "internal_amount": str(transaction.amount),
        "gross": str(line.gross),
        "fees": str(line.fee),
        "net": str(line.net),
        "settlement_date": batch.settlement_date.isoformat(),
        "source_file": source["source_file"],
        "source_format": source["source_format"],
        "source_locator": source["source_locator"],
        "settlement_count": len(result.settlements[transaction_id]),
        "status": "review" if finding_ids else "matched",
        "finding_ids": finding_ids,
    }


def _batch_to_json(result: ReconciliationResult, batch: SettlementBatch) -> dict:
    currencies = sorted({line.currency for line in batch.lines})
    finding_ids = [
        finding.id
        for finding in result.discrepancies
        if finding.batch_id == batch.batch_id and finding.processor == batch.processor
    ]
    return {
        "batch_id": batch.batch_id,
        "processor": batch.processor,
        "settlement_date": batch.settlement_date.isoformat(),
        "currency": " + ".join(currencies),
        "currencies": currencies,
        "lines": len(batch.lines),
        "gross": _multi_currency_total(batch.lines, "gross"),
        "fees": _multi_currency_total(batch.lines, "fee"),
        "net": _multi_currency_total(batch.lines, "net"),
        "reported_gross": str(batch.reported_gross) if batch.reported_gross else None,
        "reported_fees": str(batch.reported_fees) if batch.reported_fees else None,
        "reported_net": str(batch.reported_net) if batch.reported_net else None,
        "source_file": batch.source_file,
        "source_format": batch.source_format,
        "status": "review" if finding_ids else "ready",
        "finding_ids": finding_ids,
    }


def _discrepancy_to_json(result: ReconciliationResult, finding: Discrepancy) -> dict:
    payload = finding.to_json()
    payload.update(_finding_source(result, finding))
    return payload


def _finding_source(result: ReconciliationResult, finding: Discrepancy) -> dict:
    if finding.batch_id:
        batch = next(
            (
                candidate for candidate in result.source_batches
                if candidate.batch_id == finding.batch_id
                and candidate.processor == finding.processor
            ),
            None,
        )
        if batch is not None:
            transaction_id = finding.transaction_ids[0] if finding.transaction_ids else None
            line = next(
                (candidate for candidate in batch.lines if candidate.transaction_id == transaction_id),
                None,
            )
            if line is not None:
                return _line_source(batch, line)
            return {
                "source_file": batch.source_file,
                "source_format": batch.source_format,
                "source_locator": "Batch header",
            }

    transaction_id = finding.transaction_ids[0] if finding.transaction_ids else None
    if transaction_id in result.ledger:
        ledger_row = list(result.ledger).index(transaction_id) + 2
        return {
            "source_file": "transactions.csv",
            "source_format": "csv",
            "source_locator": f"Row {ledger_row}",
        }
    return {"source_file": None, "source_format": None, "source_locator": None}


def _line_source(batch: SettlementBatch, line: SettlementLine) -> dict:
    index = next(
        position
        for position, candidate in enumerate(batch.lines)
        if candidate is line or candidate.transaction_id == line.transaction_id
    )
    if batch.source_format == "json":
        locator = f"$.settlement.payments[{index}]"
    elif batch.source_format == "xml":
        locator = f"/ReconciliationReport/Transactions/Transaction[{index + 1}]"
    else:
        locator = f"Row {index + 2}"
    return {
        "source_file": batch.source_file,
        "source_format": batch.source_format,
        "source_locator": locator,
    }


def _multi_currency_total(lines: tuple[SettlementLine, ...], attribute: str) -> str:
    currencies = sorted({getattr(line, attribute).currency for line in lines})
    amounts = [
        str(
            total(
                (
                    getattr(line, attribute) for line in lines
                    if getattr(line, attribute).currency == code
                ),
                code,
            )
        )
        for code in currencies
    ]
    return " + ".join(amounts)


def to_json(result: ReconciliationResult) -> str:
    return json.dumps(to_dict(result), indent=2) + "\n"


def to_csv(result: ReconciliationResult) -> str:
    """Flat exception list -- the format finance teams actually work in."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["id", "severity", "type", "processor", "batch_id", "transaction_ids",
         "expected", "actual", "impact", "currency", "description", "recommended_action"]
    )
    for finding in result.worklist():
        writer.writerow(
            [
                finding.id, finding.severity.value, finding.type.value, finding.processor,
                finding.batch_id or "", " ".join(finding.transaction_ids),
                finding.expected if finding.expected is not None else "",
                finding.actual if finding.actual is not None else "",
                finding.impact.amount if finding.impact else "",
                finding.impact.currency if finding.impact else "",
                finding.description, finding.recommended_action,
            ]
        )
    return buffer.getvalue()


def to_console(result: ReconciliationResult, limit: int = 15) -> str:
    """A one-screen answer to "should I worry, and what do I do first?"."""
    out: list[str] = []
    add = out.append

    add("=" * 78)
    add(f"  SETTLEMENT RECONCILIATION -- as of {result.as_of}")
    add("=" * 78)
    add("")
    add(f"  Batches processed      {result.batches}")
    add(f"  Settlement lines       {result.lines}")
    add(f"  Ledger transactions    {result.ledger_size} ({result.awaiting_settlement} expecting settlement)")
    add(f"  ID matched             {len(result.matched_ids)}  ({result.id_match_rate:.1%})")
    add(f"  Cleanly reconciled     {len(result.reconciled_ids)}  ({result.match_rate:.1%})")
    add(f"  Unmatched              {result.unmatched_count}")
    add(f"  Discrepancies          {len(result.discrepancies)}")
    add("")

    add("  SETTLED VOLUME")
    for currency, totals in sorted(result.totals_by_currency.items()):
        add(f"    {currency}   gross {totals.gross!s:>26}   fees {totals.fees!s:>22}   net {totals.net!s:>26}")
    add("")

    exposure = result.exposure()
    if exposure:
        add("  MONEY AT RISK   (positive = owed to us, negative = overpaid to us)")
        for currency, amount in exposure.items():
            add(f"    {currency}   {amount}")
        add(f"    indicative total  ~US${result.exposure_usd():,.2f}")
        add("")

    if result.clean:
        add("  No discrepancies found. Every expected settlement matched.")
        return "\n".join(out)

    severities = result.counts("severity")
    add("  BY SEVERITY   " + "   ".join(f"{k}={v}" for k, v in severities.items()))
    add("  BY TYPE       " + "   ".join(f"{k}={v}" for k, v in result.counts("type").items()))
    add("")
    add("-" * 78)
    add(f"  WORKLIST -- highest money at risk first (showing {min(limit, len(result.discrepancies))} of {len(result.discrepancies)})")
    add("-" * 78)

    for finding in result.worklist()[:limit]:
        add("")
        add(f"  {SEVERITY_ICON[finding.severity]} [{finding.severity.value.upper()}] {finding.id}  {finding.type.value}")
        add(f"     {finding.description}")
        if finding.impact is not None:
            add(f"     impact: {finding.impact}")
        add(f"     -> {finding.recommended_action}")

    if len(result.discrepancies) > limit:
        add("")
        add(f"  ... {len(result.discrepancies) - limit} more. Use --format markdown|csv|json for the full list.")
    return "\n".join(out)


def to_markdown(result: ReconciliationResult) -> str:
    """The shareable artefact: what gets pasted into the month-end ticket."""
    data = to_dict(result)
    out: list[str] = []
    add = out.append

    add(f"# Settlement reconciliation - as of {result.as_of}")
    add("")
    add(f"_Generated {data['generated_at']}_")
    add("")
    add("## Summary")
    add("")
    add("| Metric | Value |")
    add("| --- | --- |")
    for label, key in [
        ("Batches processed", "batches_processed"),
        ("Settlement lines", "settlement_lines"),
        ("Ledger transactions", "ledger_transactions"),
        ("Expecting settlement", "awaiting_settlement"),
        ("ID matched", "id_matched"),
        ("Cleanly reconciled", "cleanly_reconciled"),
        ("Unmatched", "unmatched"),
        ("ID match rate", "id_match_rate"),
        ("Clean reconciliation rate", "match_rate"),
        ("Discrepancies", "discrepancies"),
    ]:
        add(f"| {label} | {data['summary'][key]} |")
    add("")

    add("## Settled volume")
    add("")
    add("| Processor · currency | Lines | Gross | Fees | Net |")
    add("| --- | ---: | ---: | ---: | ---: |")
    for key, totals in sorted(result.totals_by_processor.items()):
        add(f"| {key} | {totals.lines} | {totals.gross} | {totals.fees} | {totals.net} |")
    add("")

    exposure = result.exposure()
    if exposure:
        add("## Money at risk")
        add("")
        add("Positive means CafeTerra is owed money; negative means CafeTerra was overpaid "
            "and should expect a clawback. Amounts are never converted for matching - the "
            "USD figure below is indicative, for ranking only.")
        add("")
        add("| Currency | Net exposure |")
        add("| --- | ---: |")
        for currency, amount in exposure.items():
            add(f"| {currency} | {amount} |")
        add(f"| **Indicative total** | **~US${result.exposure_usd():,.2f}** |")
        add("")

    if result.clean:
        add("## Discrepancies")
        add("")
        add("None. Every expected settlement matched.")
        return "\n".join(out) + "\n"

    add("## Discrepancies by type")
    add("")
    add("| Type | Count |")
    add("| --- | ---: |")
    for name, count in data["by_type"].items():
        add(f"| `{name}` | {count} |")
    add("")

    add("## Worklist")
    add("")
    add("Ordered by severity, then by money at risk.")
    add("")
    add("| # | ID | Severity | Type | Processor | Transaction | Impact | Issue |")
    add("| ---: | --- | --- | --- | --- | --- | ---: | --- |")
    for index, finding in enumerate(result.worklist(), start=1):
        txns = ", ".join(finding.transaction_ids) or "-"
        impact = str(finding.impact) if finding.impact else "-"
        add(
            f"| {index} | `{finding.id}` | {finding.severity.value} | `{finding.type.value}` | "
            f"{finding.processor} | {txns} | {impact} | {finding.description} |"
        )
    add("")

    add("## Recommended actions")
    add("")
    for finding in result.worklist():
        add(f"- **`{finding.id}`** ({finding.severity.value}) - {finding.recommended_action}")
    add("")
    return "\n".join(out) + "\n"


RENDERERS = {
    "console": to_console,
    "markdown": to_markdown,
    "json": to_json,
    "csv": to_csv,
}


def render(result: ReconciliationResult, fmt: str) -> str:
    try:
        return RENDERERS[fmt](result)
    except KeyError:
        raise ValueError(
            f"unknown format {fmt!r}; choose from {', '.join(RENDERERS)}"
        ) from None
