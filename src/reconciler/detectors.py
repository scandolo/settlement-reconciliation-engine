"""Discrepancy detectors.

Each detector is a small, independent function that answers one question and
yields findings. They are registered into three families by the scope of data
they need:

* `@line_detector`   -- one settlement line against its transaction
* `@batch_detector`  -- one payout against its own header totals
* `@ledger_detector` -- the whole run against the internal ledger

Adding a rule means writing one function under fifteen lines and decorating it.
Every detector guards its own preconditions, so they can run in any order and
none can corrupt another.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable, Iterator, Sequence

from .models import (
    Discrepancy,
    DiscrepancyType,
    SettlementBatch,
    SettlementLine,
    Severity,
    Transaction,
    TransactionStatus,
)
from .money import Money, total
from .processors.base import ProcessorProfile
from .rules import ReconciliationRules


# --------------------------------------------------------------------------
# Contexts: exactly the data each family of detector needs, and nothing more.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LineContext:
    batch: SettlementBatch
    line: SettlementLine
    transaction: Transaction | None
    profile: ProcessorProfile | None
    rules: ReconciliationRules

    def finding(self, type_: DiscrepancyType, **kwargs) -> Discrepancy:
        kwargs.setdefault("transaction_ids", [self.line.transaction_id])
        return Discrepancy(
            type=type_, processor=self.batch.processor, batch_id=self.batch.batch_id, **kwargs
        )

    @property
    def comparable(self) -> bool:
        """True when the line and transaction can be compared amount-for-amount."""
        return (
            self.transaction is not None
            and self.transaction.currency == self.line.currency
        )


@dataclass(frozen=True)
class BatchContext:
    batch: SettlementBatch
    rules: ReconciliationRules


@dataclass(frozen=True)
class LedgerContext:
    ledger: dict[str, Transaction]
    #: transaction_id -> every (batch, line) that settled it.
    settlements: dict[str, list[tuple[SettlementBatch, SettlementLine]]]
    profiles: dict[str, ProcessorProfile]
    rules: ReconciliationRules
    as_of: date


Detector = Callable[..., Iterable[Discrepancy]]
LINE_DETECTORS: list[Detector] = []
BATCH_DETECTORS: list[Detector] = []
LEDGER_DETECTORS: list[Detector] = []


def line_detector(fn: Detector) -> Detector:
    LINE_DETECTORS.append(fn)
    return fn


def batch_detector(fn: Detector) -> Detector:
    BATCH_DETECTORS.append(fn)
    return fn


def ledger_detector(fn: Detector) -> Detector:
    LEDGER_DETECTORS.append(fn)
    return fn


def severity_for(impact: Money, rules: ReconciliationRules, floor: Severity) -> Severity:
    """Escalate to CRITICAL once the money at stake crosses the currency threshold."""
    if abs(impact.amount) >= rules.critical_threshold(impact.currency):
        return Severity.CRITICAL
    return floor


# --------------------------------------------------------------------------
# Line-level detectors
# --------------------------------------------------------------------------


@line_detector
def net_arithmetic(ctx: LineContext) -> Iterator[Discrepancy]:
    """gross - fee must equal net, whether or not we can match the transaction."""
    expected = ctx.line.gross - ctx.line.fee
    delta = expected - ctx.line.net
    if not ctx.rules.exceeds(delta):
        return
    yield ctx.finding(
        DiscrepancyType.NET_ARITHMETIC_ERROR,
        severity=severity_for(delta, ctx.rules, Severity.HIGH),
        description=(
            f"Settlement line does not add up: gross {ctx.line.gross} minus fee "
            f"{ctx.line.fee} is {expected}, but the report states net {ctx.line.net}."
        ),
        expected=expected,
        actual=ctx.line.net,
        impact=delta,
        recommended_action=(
            "Hold the line and request a corrected settlement advice -- the file is "
            "internally inconsistent, so neither figure can be trusted."
        ),
    )


@line_detector
def unknown_settlement(ctx: LineContext) -> Iterator[Discrepancy]:
    """Money arrived for something that is not in our ledger at all."""
    if ctx.transaction is not None:
        return
    yield ctx.finding(
        DiscrepancyType.UNKNOWN_SETTLEMENT,
        severity=severity_for(ctx.line.net, ctx.rules, Severity.HIGH),
        description=(
            f"{ctx.batch.processor} settled {ctx.line.net} for "
            f"{ctx.line.transaction_id}, which does not exist in the internal ledger."
        ),
        expected="a matching internal transaction",
        actual="no ledger record",
        impact=-ctx.line.net,
        recommended_action=(
            "Do not recognise as revenue yet. Confirm whether this is a reserve "
            "release, refund reversal or adjustment, and book it accordingly."
        ),
    )


@line_detector
def currency_mismatch(ctx: LineContext) -> Iterator[Discrepancy]:
    """Settled in a different currency from the one the customer was charged in."""
    txn = ctx.transaction
    if txn is None or txn.currency == ctx.line.currency:
        return
    yield ctx.finding(
        DiscrepancyType.CURRENCY_MISMATCH,
        severity=Severity.CRITICAL,
        description=(
            f"{txn.transaction_id} was charged in {txn.currency} ({txn.amount}) but "
            f"settled in {ctx.line.currency} ({ctx.line.gross})."
        ),
        expected=txn.currency,
        actual=ctx.line.currency,
        impact=txn.amount,  # the original receivable stays open
        recommended_action=(
            f"Escalate today. The deposit will not clear against a {txn.currency} "
            "receivable; request reversal and re-settlement in the original currency."
        ),
    )


@line_detector
def status_conflict(ctx: LineContext) -> Iterator[Discrepancy]:
    """We were paid for something we refunded, reversed or never captured."""
    txn = ctx.transaction
    if txn is None or txn.status is TransactionStatus.CAPTURED:
        return
    reversed_ = txn.status in (TransactionStatus.REFUNDED, TransactionStatus.CHARGEBACK)
    yield ctx.finding(
        DiscrepancyType.STATUS_CONFLICT,
        severity=Severity.CRITICAL if reversed_ else Severity.HIGH,
        description=(
            f"{txn.transaction_id} is '{txn.status.value}' internally but was "
            f"settled for {ctx.line.net}."
        ),
        expected=TransactionStatus.CAPTURED.value,
        actual=txn.status.value,
        impact=-ctx.line.net,
        recommended_action=(
            "Quarantine the funds. Settled reversals are normally clawed back, so "
            "recognising this now creates a shortfall in a later payout."
        ),
    )


@line_detector
def amount_mismatch(ctx: LineContext) -> Iterator[Discrepancy]:
    """The gross settled does not match what the customer was charged."""
    if not ctx.comparable:
        return
    txn = ctx.transaction
    delta = txn.amount - ctx.line.gross
    if not ctx.rules.exceeds(delta):
        return
    direction = "short by" if delta.amount > 0 else "over by"
    yield ctx.finding(
        DiscrepancyType.AMOUNT_MISMATCH,
        severity=severity_for(delta, ctx.rules, Severity.MEDIUM),
        description=(
            f"{txn.transaction_id} was charged {txn.amount} but settled with gross "
            f"{ctx.line.gross} -- {direction} {abs(delta)}."
        ),
        expected=txn.amount,
        actual=ctx.line.gross,
        impact=delta,
        recommended_action=(
            "Check for a partial refund or partial capture recorded only on the "
            "processor side; if there is none, open a settlement dispute."
        ),
    )


@line_detector
def fee_error(ctx: LineContext) -> Iterator[Discrepancy]:
    """The fee charged does not match the contracted rate card."""
    if not ctx.comparable or ctx.profile is None:
        return
    method = ctx.line.payment_method or ctx.transaction.payment_method
    expected = ctx.profile.expected_fee(ctx.line.gross, method)
    if expected is None:
        return
    delta = ctx.line.fee - expected  # positive means we were overcharged
    if not ctx.rules.fee_exceeds(delta, expected):
        return
    yield ctx.finding(
        DiscrepancyType.FEE_ERROR,
        severity=severity_for(delta, ctx.rules, Severity.MEDIUM),
        description=(
            f"Fee on {ctx.transaction.transaction_id} ({method}) was {ctx.line.fee}, "
            f"but the contracted rate implies {expected} -- "
            f"{'overcharged' if delta.amount > 0 else 'undercharged'} by {abs(delta)}."
        ),
        expected=expected,
        actual=ctx.line.fee,
        impact=delta,
        recommended_action=(
            f"Reclaim the difference and re-check the {method} rate configured for "
            f"{ctx.batch.processor}. A systematic error here repeats on every "
            "transaction."
        ),
    )


@line_detector
def processor_mismatch(ctx: LineContext) -> Iterator[Discrepancy]:
    """A processor paid us for a transaction we routed elsewhere."""
    txn = ctx.transaction
    if txn is None or txn.processor == ctx.batch.processor:
        return
    yield ctx.finding(
        DiscrepancyType.PROCESSOR_MISMATCH,
        severity=Severity.HIGH,
        description=(
            f"{ctx.batch.processor} settled {txn.transaction_id}, but the ledger "
            f"routed it through {txn.processor}."
        ),
        expected=txn.processor,
        actual=ctx.batch.processor,
        recommended_action=(
            "Check for a routing failover or a mis-keyed merchant reference before "
            "accepting the payout, and confirm the other processor is not also "
            "settling it."
        ),
    )


# --------------------------------------------------------------------------
# Batch-level detectors
# --------------------------------------------------------------------------


@batch_detector
def batch_totals(ctx: BatchContext) -> Iterator[Discrepancy]:
    """The processor's header totals must equal the sum of its own detail lines."""
    batch = ctx.batch
    if {ln.currency for ln in batch.lines} - {batch.currency}:
        return  # mixed-currency payout; per-line currency findings already cover it

    computed = {
        "gross": total((ln.gross for ln in batch.lines), batch.currency),
        "fees": total((ln.fee for ln in batch.lines), batch.currency),
        "net": total((ln.net for ln in batch.lines), batch.currency),
    }
    claimed = {
        "gross": batch.reported_gross,
        "fees": batch.reported_fees,
        "net": batch.reported_net,
    }
    for name, stated in claimed.items():
        if stated is None:
            continue
        delta = stated - computed[name]
        if not ctx.rules.exceeds(delta):
            continue
        yield Discrepancy(
            type=DiscrepancyType.BATCH_TOTAL_MISMATCH,
            severity=severity_for(delta, ctx.rules, Severity.HIGH),
            processor=batch.processor,
            batch_id=batch.batch_id,
            description=(
                f"Batch {batch.batch_id} claims {name} of {stated}, but its "
                f"{len(batch.lines)} lines sum to {computed[name]}."
            ),
            expected=computed[name],
            actual=stated,
            impact=-delta if name == "net" else None,
            recommended_action=(
                "Do not post this payout until the processor reissues the file -- "
                "header and detail disagree, so one of them is wrong."
            ),
        )


# --------------------------------------------------------------------------
# Ledger-level detectors
# --------------------------------------------------------------------------


@ledger_detector
def duplicate_settlement(ctx: LedgerContext) -> Iterator[Discrepancy]:
    """The same transaction was paid out more than once."""
    for txn_id, occurrences in ctx.settlements.items():
        if len(occurrences) < 2:
            continue
        extra = [line for _, line in occurrences[1:]]
        currency = extra[0].currency
        overpaid = (
            total((ln.net for ln in extra), currency)
            if all(ln.currency == currency for ln in extra)
            else None
        )
        batches = ", ".join(sorted({b.batch_id for b, _ in occurrences}))
        yield Discrepancy(
            type=DiscrepancyType.DUPLICATE_SETTLEMENT,
            severity=Severity.CRITICAL,
            processor=occurrences[0][0].processor,
            batch_id=occurrences[0][0].batch_id,
            transaction_ids=[txn_id],
            description=(
                f"{txn_id} was settled {len(occurrences)} times, across batches {batches}."
            ),
            expected="1 settlement",
            actual=f"{len(occurrences)} settlements",
            impact=-overpaid if overpaid is not None else None,
            recommended_action=(
                "Freeze the duplicated amount before month-end close. Duplicates are "
                "reversed later and will otherwise open a hole in the next payout."
            ),
        )


@ledger_detector
def missing_and_aging(ctx: LedgerContext) -> Iterator[Discrepancy]:
    """Captured transactions nobody has paid us for, split by SLA breach."""
    for txn in ctx.ledger.values():
        if txn.transaction_id in ctx.settlements or not txn.expects_settlement:
            continue
        profile = ctx.profiles.get(txn.processor)
        sla = profile.settlement_sla_days if profile else 7
        due = txn.captured_at + timedelta(days=sla)
        overdue_days = (ctx.as_of - due).days

        if overdue_days > ctx.rules.aging_grace_days:
            yield Discrepancy(
                type=DiscrepancyType.MISSING_SETTLEMENT,
                severity=severity_for(txn.amount, ctx.rules, Severity.HIGH),
                processor=txn.processor,
                transaction_ids=[txn.transaction_id],
                description=(
                    f"{txn.transaction_id} was captured on {txn.captured_at} for "
                    f"{txn.amount} and has never been settled. Due {due} under the "
                    f"{sla}-day {txn.processor} SLA -- {overdue_days} days overdue."
                ),
                expected=f"settlement by {due.isoformat()}",
                actual="never settled",
                impact=txn.amount,
                recommended_action=(
                    f"Raise a missing-settlement enquiry with {txn.processor} quoting "
                    "the capture date. If it was refunded processor-side, correct the "
                    "internal status instead."
                ),
            )
        elif overdue_days >= 0:
            yield Discrepancy(
                type=DiscrepancyType.SETTLEMENT_AGING,
                severity=Severity.LOW,
                processor=txn.processor,
                transaction_ids=[txn.transaction_id],
                description=(
                    f"{txn.transaction_id} ({txn.amount}) has reached the edge of "
                    f"{txn.processor}'s {sla}-day SLA, due {due}."
                ),
                expected=f"settlement by {due.isoformat()}",
                actual="pending",
                recommended_action="Monitor; escalate if the next payout omits it.",
            )


def run_line_detectors(ctx: LineContext) -> list[Discrepancy]:
    return [d for detector in LINE_DETECTORS for d in detector(ctx)]


def run_batch_detectors(ctx: BatchContext) -> list[Discrepancy]:
    return [d for detector in BATCH_DETECTORS for d in detector(ctx)]


def run_ledger_detectors(ctx: LedgerContext) -> list[Discrepancy]:
    return [d for detector in LEDGER_DETECTORS for d in detector(ctx)]


def registered() -> dict[str, Sequence[str]]:
    """Names of every registered detector -- surfaced in the CLI and the docs."""
    return {
        "line": [d.__name__ for d in LINE_DETECTORS],
        "batch": [d.__name__ for d in BATCH_DETECTORS],
        "ledger": [d.__name__ for d in LEDGER_DETECTORS],
    }
