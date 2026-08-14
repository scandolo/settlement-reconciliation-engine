"""Deterministic test-data generation.

Two jobs, kept together because they are two halves of one idea:

1. build a realistic ledger and a set of settlement files, and
2. record every defect deliberately injected into them, as ground truth.

That second half is what makes the engine checkable rather than merely
plausible: `reconcile verify` re-runs the engine against this ground truth and
proves each planted break was found. Test data you cannot grade is just data.

The writers here are the exact inverse of the adapters in `processors/`, so the
files are round-trip proof that each format parses back to what we wrote.
"""

from __future__ import annotations

import csv
import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

from .models import (
    DiscrepancyType,
    SettlementBatch,
    SettlementLine,
    Transaction,
    TransactionStatus,
)
from .money import REGISTRY, Money, total
from .processors import PROFILES

COUNTRY_CURRENCY = {"BR": "BRL", "MX": "MXN", "CO": "COP", "US": "USD"}

#: Which markets and methods each processor actually serves for us.
ROUTING: dict[str, dict[str, tuple[str, ...]]] = {
    "Adyen": {
        "BR": ("pix", "credit_card", "debit_card"),
        "MX": ("credit_card", "debit_card"),
        "US": ("credit_card", "debit_card"),
    },
    "Stripe": {
        "MX": ("credit_card", "oxxo"),
        "US": ("credit_card", "debit_card"),
    },
    "dLocal": {
        "BR": ("pix", "credit_card"),
        "MX": ("spei", "credit_card"),
        "CO": ("pse", "credit_card"),
    },
    "PayU Latam": {"CO": ("pse", "credit_card", "cash")},
}

#: Realistic per-currency ticket sizes for a coffee subscription business.
TICKET_RANGE: dict[str, tuple[int, int]] = {
    "BRL": (25, 2_500),
    "MXN": (90, 9_000),
    "COP": (20_000, 2_000_000),
    "USD": (5, 500),
}

STATUS_WEIGHTS = [
    (TransactionStatus.CAPTURED, 78),
    (TransactionStatus.AUTHORIZED, 11),
    (TransactionStatus.REFUNDED, 6),
    (TransactionStatus.FAILED, 5),
]


@dataclass(frozen=True)
class InjectedDefect:
    """One deliberately planted break, and where to find it."""

    type: DiscrepancyType
    note: str
    transaction_id: str | None = None
    batch_id: str | None = None

    def to_json(self) -> dict:
        return {
            "type": self.type.value,
            "transaction_id": self.transaction_id,
            "batch_id": self.batch_id,
            "note": self.note,
        }


@dataclass
class Dataset:
    transactions: list[Transaction]
    batches: list[SettlementBatch]
    defects: list[InjectedDefect] = field(default_factory=list)
    as_of: date = date.today()


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------


def generate_ledger(rng: random.Random, count: int, as_of: date) -> list[Transaction]:
    transactions: list[Transaction] = []
    routes = [
        (processor, country, methods)
        for processor, markets in ROUTING.items()
        for country, methods in markets.items()
    ]
    for index in range(count):
        processor, country, methods = rng.choice(routes)
        currency = COUNTRY_CURRENCY[country]
        created = as_of - timedelta(days=rng.randint(0, 29))
        status = _weighted_status(rng)
        captured = (
            created + timedelta(days=rng.choice([0, 0, 0, 1]))
            if status in (TransactionStatus.CAPTURED, TransactionStatus.REFUNDED)
            else None
        )
        transactions.append(
            Transaction(
                transaction_id=f"CT-{as_of:%Y%m}-{index:05d}",
                processor=processor,
                country=country,
                payment_method=rng.choice(methods),
                amount=_ticket(rng, currency),
                status=status,
                created_at=created,
                captured_at=min(captured, as_of) if captured else None,
            )
        )
    return transactions


def _weighted_status(rng: random.Random) -> TransactionStatus:
    population, weights = zip(*STATUS_WEIGHTS)
    return rng.choices(population, weights=weights, k=1)[0]


def _ticket(rng: random.Random, currency: str) -> Money:
    low, high = TICKET_RANGE[currency]
    spec = REGISTRY.get(currency)
    raw = Decimal(rng.randint(low * 100, high * 100)) / 100
    return Money(raw.quantize(spec.quantum), currency)


# --------------------------------------------------------------------------
# Settlement batches (canonical, before defects)
# --------------------------------------------------------------------------


def build_batches(transactions: Sequence[Transaction], as_of: date) -> list[SettlementBatch]:
    """Settle everything captured long enough ago to be past its SLA.

    Transactions captured inside the SLA window are deliberately left unsettled:
    that is normal, and it gives the aging detector something true to say.
    """
    grouped: dict[tuple[str, str], list[Transaction]] = {}
    for txn in transactions:
        if not txn.expects_settlement:
            continue
        profile = PROFILES[txn.processor]
        cutoff = as_of - timedelta(days=profile.settlement_sla_days + 1)
        if txn.captured_at > cutoff:
            continue
        grouped.setdefault((txn.processor, txn.currency), []).append(txn)

    batches: list[SettlementBatch] = []
    for (processor, currency), members in sorted(grouped.items()):
        profile = PROFILES[processor]
        members.sort(key=lambda t: (t.captured_at, t.transaction_id))
        settlement_date = max(
            t.captured_at + timedelta(days=profile.settlement_sla_days) for t in members
        )
        lines = tuple(_line_for(txn, processor) for txn in members)
        batches.append(
            SettlementBatch(
                batch_id=f"{_prefix(processor)}-{currency}-{settlement_date:%Y%m%d}",
                processor=processor,
                settlement_date=settlement_date,
                currency=currency,
                lines=lines,
                **_totals(lines, currency),
            )
        )
    return batches


def _line_for(txn: Transaction, processor: str) -> SettlementLine:
    fee = PROFILES[processor].expected_fee(txn.amount, txn.payment_method) or Money.zero(
        txn.currency
    )
    return SettlementLine(
        transaction_id=txn.transaction_id,
        gross=txn.amount,
        fee=fee,
        net=txn.amount - fee,
        payment_method=txn.payment_method,
    )


def _totals(lines: Sequence[SettlementLine], currency: str) -> dict[str, Money]:
    return {
        "reported_gross": total((ln.gross for ln in lines), currency),
        "reported_fees": total((ln.fee for ln in lines), currency),
        "reported_net": total((ln.net for ln in lines), currency),
    }


def _prefix(processor: str) -> str:
    return {"Adyen": "ADY", "Stripe": "po", "dLocal": "DLO", "PayU Latam": "PAYU"}[processor]


def _replace_lines(batch: SettlementBatch, lines: Sequence[SettlementLine]) -> SettlementBatch:
    """Rebuild a batch around new lines, keeping its header totals honest."""
    from dataclasses import replace

    return replace(batch, lines=tuple(lines), **_totals(lines, batch.currency))


# --------------------------------------------------------------------------
# Defect injection
# --------------------------------------------------------------------------


def inject_defects(dataset: Dataset, rng: random.Random) -> Dataset:
    """Plant one of every discrepancy type the engine claims to detect."""
    from dataclasses import replace

    batches = list(dataset.batches)
    ledger = {t.transaction_id: t for t in dataset.transactions}
    defects: list[InjectedDefect] = []

    def pick_batch(predicate: Callable[[SettlementBatch], bool]) -> int:
        candidates = [i for i, b in enumerate(batches) if predicate(b) and len(b.lines) >= 3]
        return rng.choice(candidates)

    # 1. Missing settlements: drop three settled lines entirely.
    for _ in range(3):
        index = pick_batch(lambda b: True)
        lines = list(batches[index].lines)
        dropped = lines.pop(rng.randrange(len(lines)))
        batches[index] = _replace_lines(batches[index], lines)
        defects.append(
            InjectedDefect(
                DiscrepancyType.MISSING_SETTLEMENT,
                "captured transaction omitted from the payout",
                dropped.transaction_id,
                batches[index].batch_id,
            )
        )

    # 2. Amount mismatches: settle less than was charged, consistently priced,
    #    so only the amount check fires and the file still adds up.
    for shrink in (Decimal("0.5"), Decimal("0.82")):
        index = pick_batch(lambda b: True)
        lines = list(batches[index].lines)
        position = rng.randrange(len(lines))
        original = lines[position]
        gross = original.gross.scaled(shrink)
        fee = PROFILES[batches[index].processor].expected_fee(
            gross, original.payment_method
        ) or Money.zero(gross.currency)
        lines[position] = replace(original, gross=gross, fee=fee, net=gross - fee)
        batches[index] = _replace_lines(batches[index], lines)
        defects.append(
            InjectedDefect(
                DiscrepancyType.AMOUNT_MISMATCH,
                f"settled at {shrink:.0%} of the captured amount",
                original.transaction_id,
                batches[index].batch_id,
            )
        )

    # 3. Unknown settlements: money for transactions we have never heard of.
    for suffix in ("ADJ-9001", "RSV-9002"):
        index = pick_batch(lambda b: True)
        batch = batches[index]
        template = batch.lines[0]
        phantom = replace(template, transaction_id=f"CT-{suffix}")
        batches[index] = _replace_lines(batch, list(batch.lines) + [phantom])
        defects.append(
            InjectedDefect(
                DiscrepancyType.UNKNOWN_SETTLEMENT,
                "settlement line with no ledger counterpart",
                phantom.transaction_id,
                batch.batch_id,
            )
        )

    # 4. Fee errors: overcharge, but keep gross - fee = net so only fees look wrong.
    for uplift in (Decimal("1.9"), Decimal("2.6")):
        index = pick_batch(lambda b: True)
        lines = list(batches[index].lines)
        position = rng.randrange(len(lines))
        original = lines[position]
        fee = original.fee.scaled(uplift)
        lines[position] = replace(original, fee=fee, net=original.gross - fee)
        batches[index] = _replace_lines(batches[index], lines)
        defects.append(
            InjectedDefect(
                DiscrepancyType.FEE_ERROR,
                f"processor charged {uplift}x the contracted rate",
                original.transaction_id,
                batches[index].batch_id,
            )
        )

    # 5. Currency mismatch: a payout in the wrong currency entirely.
    index = pick_batch(lambda b: b.currency == "MXN")
    lines = list(batches[index].lines)
    position = rng.randrange(len(lines))
    original = lines[position]
    lines[position] = replace(
        original,
        gross=Money(original.gross.amount, "USD"),
        fee=Money(original.fee.amount, "USD"),
        net=Money(original.net.amount, "USD"),
    )
    batches[index] = replace(batches[index], lines=tuple(lines))
    defects.append(
        InjectedDefect(
            DiscrepancyType.CURRENCY_MISMATCH,
            "MXN transaction settled in USD",
            original.transaction_id,
            batches[index].batch_id,
        )
    )

    # 6. Duplicate settlement: the same transaction paid twice, in two batches.
    source = pick_batch(lambda b: True)
    donor = batches[source].lines[0]
    target = next(
        i
        for i, b in enumerate(batches)
        if i != source and b.currency == batches[source].currency
    )
    batches[target] = _replace_lines(batches[target], list(batches[target].lines) + [donor])
    defects.append(
        InjectedDefect(
            DiscrepancyType.DUPLICATE_SETTLEMENT,
            "same transaction present in two payouts",
            donor.transaction_id,
            batches[target].batch_id,
        )
    )

    # 7. Status conflict: settle a transaction we refunded.
    refunded = next(
        t
        for t in dataset.transactions
        if t.status is TransactionStatus.REFUNDED and t.captured_at
    )
    index = pick_batch(lambda b: b.currency == refunded.currency)
    fee = PROFILES[batches[index].processor].expected_fee(
        refunded.amount, refunded.payment_method
    ) or Money.zero(refunded.currency)
    ghost = SettlementLine(
        transaction_id=refunded.transaction_id,
        gross=refunded.amount,
        fee=fee,
        net=refunded.amount - fee,
        payment_method=refunded.payment_method,
    )
    batches[index] = _replace_lines(batches[index], list(batches[index].lines) + [ghost])
    defects.append(
        InjectedDefect(
            DiscrepancyType.STATUS_CONFLICT,
            "refunded transaction included in a payout",
            refunded.transaction_id,
            batches[index].batch_id,
        )
    )

    # 8. Net arithmetic error: gross - fee no longer equals net.
    index = pick_batch(lambda b: True)
    lines = list(batches[index].lines)
    position = rng.randrange(len(lines))
    original = lines[position]
    lines[position] = replace(original, net=original.net.scaled(Decimal("0.93")))
    batches[index] = _replace_lines(batches[index], lines)
    defects.append(
        InjectedDefect(
            DiscrepancyType.NET_ARITHMETIC_ERROR,
            "net does not equal gross minus fee",
            original.transaction_id,
            batches[index].batch_id,
        )
    )

    # 9. Batch total mismatch: header disagrees with its own detail lines.
    #    Only formats that restate a net total can carry this defect -- Adyen's
    #    detail report does not, which is itself worth knowing.
    index = pick_batch(lambda b: b.processor in {"dLocal", "PayU Latam", "Stripe"})
    batch = batches[index]
    batches[index] = replace(batch, reported_net=batch.reported_net.scaled(Decimal("1.04")))
    defects.append(
        InjectedDefect(
            DiscrepancyType.BATCH_TOTAL_MISMATCH,
            "header net inflated by 4% against the detail",
            None,
            batch.batch_id,
        )
    )

    # 10. Processor mismatch: a transaction routed to one processor, paid by another.
    index = pick_batch(lambda b: b.processor == "dLocal" and b.currency == "COP")
    foreign = next(
        t
        for t in dataset.transactions
        if t.processor == "PayU Latam"
        and t.status is TransactionStatus.CAPTURED
        and t.currency == "COP"
    )
    fee = PROFILES["dLocal"].expected_fee(foreign.amount, "pse") or Money.zero("COP")
    batches[index] = _replace_lines(
        batches[index],
        list(batches[index].lines)
        + [
            SettlementLine(
                transaction_id=foreign.transaction_id,
                gross=foreign.amount,
                fee=fee,
                net=foreign.amount - fee,
                payment_method="pse",
            )
        ],
    )
    defects.append(
        InjectedDefect(
            DiscrepancyType.PROCESSOR_MISMATCH,
            "PayU transaction settled by dLocal",
            foreign.transaction_id,
            batches[index].batch_id,
        )
    )

    dataset.batches = batches
    dataset.defects = defects
    return dataset


def generate(seed: int = 20260814, count: int = 340, as_of: date | None = None) -> Dataset:
    """Build a complete, defect-seeded dataset. Same seed, same bytes, every time."""
    as_of = as_of or date(2026, 8, 14)
    rng = random.Random(seed)
    transactions = generate_ledger(rng, count, as_of)
    dataset = Dataset(transactions, build_batches(transactions, as_of), as_of=as_of)
    return inject_defects(dataset, rng)


# --------------------------------------------------------------------------
# Writers -- the inverse of the adapters in processors/
# --------------------------------------------------------------------------


def _plain(amount: Money) -> str:
    return f"{amount.amount}"


def write_adyen(batch: SettlementBatch, path: Path) -> None:
    """Adyen settlement detail report: quoted CSV, fee split four ways."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(
        [
            "Company Account", "Merchant Account", "Psp Reference", "Merchant Reference",
            "Payment Method", "Booking Date", "Batch Number", "Gross Currency",
            "Gross Debit (GC)", "Gross Credit (GC)", "Net Currency", "Net Debit (NC)",
            "Net Credit (NC)", "Commission (NC)", "Markup (NC)", "Scheme Fees (NC)",
            "Interchange (NC)",
        ]
    )
    batch_number = batch.batch_id
    for index, line in enumerate(batch.lines):
        markup = line.fee.scaled("0.20")
        scheme = line.fee.scaled("0.12")
        interchange = line.fee.scaled("0.08")
        commission = line.fee - markup - scheme - interchange
        writer.writerow(
            [
                "CafeTerraCo", f"CafeTerra{batch.currency}", f"psp_{index:07d}",
                line.transaction_id, _adyen_method(line.payment_method),
                batch.settlement_date.isoformat(), batch_number, line.currency,
                "", _plain(line.gross), line.currency, "", _plain(line.net),
                _plain(commission), _plain(markup), _plain(scheme), _plain(interchange),
            ]
        )
    path.write_text(buffer.getvalue(), encoding="utf-8")


def _adyen_method(method: str | None) -> str:
    return {"credit_card": "visa", "debit_card": "visadebit"}.get(method or "", method or "")


def write_stripe(batch: SettlementBatch, path: Path) -> None:
    """Stripe payout reconciliation: lowercase currencies, plus non-charge rows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "balance_transaction_id", "created_utc", "gross", "fee", "net", "currency",
            "reporting_category", "merchant_reference", "payment_method",
            "automatic_payout_id", "automatic_payout_effective_at_utc",
        ]
    )
    effective = f"{batch.settlement_date.isoformat()}T04:00:00Z"
    for index, line in enumerate(batch.lines):
        writer.writerow(
            [
                f"txn_{index:08d}", f"{batch.settlement_date.isoformat()}T00:00:00Z",
                _plain(line.gross), _plain(line.fee), _plain(line.net),
                line.currency.lower(), "charge", line.transaction_id,
                line.payment_method or "credit_card", batch.batch_id, effective,
            ]
        )
    # The payout row itself: the deposit Stripe says it actually sent.
    net = batch.reported_net or total((ln.net for ln in batch.lines), batch.currency)
    writer.writerow(
        [
            "txn_payout", f"{batch.settlement_date.isoformat()}T04:00:00Z",
            _plain(-net), "0", _plain(-net), batch.currency.lower(), "payout", "", "",
            batch.batch_id, effective,
        ]
    )
    path.write_text(buffer.getvalue(), encoding="utf-8")


def write_dlocal(batch: SettlementBatch, path: Path) -> None:
    """Representative dLocal settlement fixture based on public payment fields."""
    payments = []
    for index, line in enumerate(batch.lines):
        tax = line.fee.scaled("0.15")
        payments.append(
            {
                "payment_id": f"D-{index:08d}",
                "order_id": line.transaction_id,
                "payment_method": line.payment_method,
                "currency": line.currency,
                "amount": _plain(line.gross),
                "commission": _plain(line.fee - tax),
                "tax": _plain(tax),
                "net_amount": _plain(line.net),
            }
        )
    document = {
        "settlement": {
            "settlement_id": batch.batch_id,
            "settlement_date": f"{batch.settlement_date.isoformat()}T00:00:00Z",
            "settlement_currency": batch.currency,
            "merchant": "CafeTerra",
            "payments": payments,
            "totals": {
                "gross_amount": _plain(batch.reported_gross),
                "total_fees": _plain(batch.reported_fees),
                "net_amount": _plain(batch.reported_net),
            },
        }
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def write_payu(batch: SettlementBatch, path: Path) -> None:
    """Representative PayU XML fixture with whole-peso COP amounts."""
    rows = "\n".join(
        f'    <Transaction payuOrderId="PU{index:09d}" reference="{line.transaction_id}" '
        f'method="{line.payment_method}" currency="{line.currency}" '
        f'gross="{_plain(line.gross)}" fee="{_plain(line.fee)}" net="{_plain(line.net)}"/>'
        for index, line in enumerate(batch.lines)
    )
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ReconciliationReport batchId="{batch.batch_id}" merchant="CafeTerra" '
        f'settlementDate="{batch.settlement_date.isoformat()}" currency="{batch.currency}">\n'
        f"  <Transactions>\n{rows}\n  </Transactions>\n"
        f'  <Totals gross="{_plain(batch.reported_gross)}" '
        f'fees="{_plain(batch.reported_fees)}" net="{_plain(batch.reported_net)}"/>\n'
        f"</ReconciliationReport>\n",
        encoding="utf-8",
    )


WRITERS: dict[str, Callable[[SettlementBatch, Path], None]] = {
    "Adyen": write_adyen,
    "Stripe": write_stripe,
    "dLocal": write_dlocal,
    "PayU Latam": write_payu,
}

EXTENSIONS = {"Adyen": "csv", "Stripe": "csv", "dLocal": "json", "PayU Latam": "xml"}


def write_dataset(dataset: Dataset, root: Path) -> dict[str, Path]:
    """Write the ledger, every settlement file, and the ground truth."""
    from .ledger import save_transactions

    settlements = root / "settlements"
    settlements.mkdir(parents=True, exist_ok=True)
    for stale in settlements.iterdir():
        stale.unlink()

    ledger_path = root / "transactions.csv"
    save_transactions(dataset.transactions, ledger_path)

    written = {"ledger": ledger_path}
    for batch in dataset.batches:
        slug = batch.batch_id.replace("_", "").replace(" ", "")
        path = settlements / f"{slug}.{EXTENSIONS[batch.processor]}"
        WRITERS[batch.processor](batch, path)
        written[batch.batch_id] = path

    truth_path = root / "ground_truth.json"
    truth_path.write_text(
        json.dumps(
            {
                "as_of": dataset.as_of.isoformat(),
                "transactions": len(dataset.transactions),
                "settlement_files": len(dataset.batches),
                "injected_defects": [d.to_json() for d in dataset.defects],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written["ground_truth"] = truth_path
    return written
