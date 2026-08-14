"""Reconciliation engine.

The engine only orchestrates: it indexes the ledger, walks the batches, and
hands each scope to the detectors that care about it. All the domain judgement
lives in `detectors.py`, and all the file handling lives in `processors/`, so
this module stays small enough to read in one sitting.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from .detectors import (
    BatchContext,
    LedgerContext,
    LineContext,
    run_batch_detectors,
    run_ledger_detectors,
    run_line_detectors,
)
from .models import (
    Discrepancy,
    DiscrepancyType,
    SettlementBatch,
    SettlementLine,
    Transaction,
    TransactionStatus,
)
from .money import Money
from .processors.base import ProcessorProfile
from .rules import DEFAULT_RULES, ReconciliationRules


@dataclass
class Totals:
    """Settled volume for one currency, optionally scoped to a processor."""

    currency: str
    gross: Money
    fees: Money
    net: Money
    lines: int = 0

    @classmethod
    def empty(cls, currency: str) -> "Totals":
        zero = Money.zero(currency)
        return cls(currency, zero, zero, zero)

    def add(self, line: SettlementLine) -> None:
        self.gross += line.gross
        self.fees += line.fee
        self.net += line.net
        self.lines += 1

    def to_json(self) -> dict:
        return {
            "currency": self.currency,
            "gross": str(self.gross),
            "fees": str(self.fees),
            "net": str(self.net),
            "lines": self.lines,
        }


@dataclass
class ReconciliationResult:
    discrepancies: list[Discrepancy] = field(default_factory=list)
    totals_by_currency: dict[str, Totals] = field(default_factory=dict)
    totals_by_processor: dict[str, Totals] = field(default_factory=dict)
    #: Expected transaction IDs that appeared in at least one settlement line.
    matched_ids: set[str] = field(default_factory=set)
    #: ID matches with no line, duplicate, or batch-total discrepancy.
    reconciled_ids: set[str] = field(default_factory=set)
    ledger: dict[str, Transaction] = field(default_factory=dict, repr=False)
    profiles: dict[str, ProcessorProfile] = field(default_factory=dict, repr=False)
    settlements: dict[str, list[tuple[SettlementBatch, SettlementLine]]] = field(
        default_factory=dict, repr=False
    )
    source_batches: tuple[SettlementBatch, ...] = field(default_factory=tuple, repr=False)
    batches: int = 0
    lines: int = 0
    ledger_size: int = 0
    awaiting_settlement: int = 0
    as_of: date | None = None
    rules: ReconciliationRules = DEFAULT_RULES

    # -- headline numbers -------------------------------------------------

    @property
    def clean(self) -> bool:
        return not self.discrepancies

    @property
    def unmatched_count(self) -> int:
        return self.awaiting_settlement - len(self.matched_ids)

    @property
    def match_rate(self) -> Decimal:
        if not self.awaiting_settlement:
            return Decimal("1")
        return Decimal(len(self.reconciled_ids)) / Decimal(self.awaiting_settlement)

    @property
    def id_match_rate(self) -> Decimal:
        if not self.awaiting_settlement:
            return Decimal("1")
        return Decimal(len(self.matched_ids)) / Decimal(self.awaiting_settlement)

    def counts(self, attribute: str) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for finding in self.discrepancies:
            counts[getattr(finding, attribute).value] += 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def exposure(self) -> dict[str, Money]:
        """Net money at risk per currency: owed to us, minus overpaid to us."""
        exposure: dict[str, Money] = {}
        duplicate_ids = {
            transaction_id
            for transaction_id, occurrences in self.settlements.items()
            if len(occurrences) > 1
        }

        for finding in self.discrepancies:
            if finding.impact is None:
                continue
            # Duplicate settlements are one economic event. Their line-level
            # fee and amount findings are still useful for investigation, but
            # summing those impacts again would inflate the headline.
            if duplicate_ids.intersection(finding.transaction_ids):
                continue
            code = finding.impact.currency
            exposure[code] = exposure.get(code, Money.zero(code)) + finding.impact

        for transaction_id in duplicate_ids:
            transaction = self.ledger.get(transaction_id)
            occurrences = self.settlements[transaction_id]
            if transaction is not None and transaction.expects_settlement:
                profile = self.profiles.get(transaction.processor)
                expected_fee = (
                    profile.expected_fee(transaction.amount, transaction.payment_method)
                    if profile is not None
                    else None
                )
                expected_net = (
                    transaction.amount - expected_fee
                    if expected_fee is not None
                    else transaction.amount
                )
                code = expected_net.currency
                exposure[code] = exposure.get(code, Money.zero(code)) + expected_net

            for _, line in occurrences:
                code = line.net.currency
                exposure[code] = exposure.get(code, Money.zero(code)) - line.net

        return dict(
            sorted((code, amount) for code, amount in exposure.items() if amount.amount)
        )

    def exposure_usd(self) -> Decimal:
        """Indicative single number for the CFO. Ranking only -- never a ledger figure."""
        return sum(
            (self.rules.usd_equivalent(abs(amount)) for amount in self.exposure().values()),
            Decimal(0),
        )

    def worklist(self) -> list[Discrepancy]:
        """Findings ordered the way an analyst should work them: worst money first."""
        return sorted(
            self.discrepancies,
            key=lambda d: (
                d.severity.rank,
                -(self.rules.usd_equivalent(abs(d.impact)) if d.impact else Decimal(0)),
                d.type.value,
            ),
        )


class ReconciliationEngine:
    """Matches settlement reports against the internal transaction ledger."""

    def __init__(
        self,
        transactions: Iterable[Transaction],
        profiles: dict[str, ProcessorProfile],
        rules: ReconciliationRules = DEFAULT_RULES,
    ) -> None:
        self.ledger = self._index_ledger(transactions)
        self.profiles = profiles
        self.rules = rules

    def reconcile(
        self, batches: Sequence[SettlementBatch], as_of: date | None = None
    ) -> ReconciliationResult:
        result = ReconciliationResult(rules=self.rules)
        result.ledger = dict(self.ledger)
        result.profiles = dict(self.profiles)
        result.source_batches = tuple(batches)
        result.ledger_size = len(self.ledger)
        result.awaiting_settlement = sum(1 for t in self.ledger.values() if t.expects_settlement)
        result.as_of = as_of or max((b.settlement_date for b in batches), default=date.today())

        # Every settlement of a transaction, so duplicates across *different*
        # payouts are visible, not just repeats within one file.
        settlements: dict[str, list[tuple[SettlementBatch, SettlementLine]]] = defaultdict(list)

        for batch in batches:
            self._validate_batch(batch)
            result.batches += 1
            profile = self.profiles.get(batch.processor)
            for line in batch.lines:
                result.lines += 1
                settlements[line.transaction_id].append((batch, line))
                self._accumulate(result, batch, line)
                result.discrepancies += run_line_detectors(
                    LineContext(
                        batch=batch,
                        line=line,
                        transaction=self.ledger.get(line.transaction_id),
                        profile=profile,
                        rules=self.rules,
                    )
                )
            result.discrepancies += run_batch_detectors(BatchContext(batch, self.rules))

        # The match-rate denominator contains only transactions that should
        # settle, so the numerator must use the same population. A refunded or
        # merely-authorized transaction appearing in a payout is a status
        # conflict, not a successful match.
        expected_ids = {
            tid for tid, transaction in self.ledger.items() if transaction.expects_settlement
        }
        result.matched_ids = expected_ids.intersection(settlements)
        result.settlements = dict(settlements)
        result.discrepancies += run_ledger_detectors(
            LedgerContext(
                ledger=self.ledger,
                settlements=dict(settlements),
                profiles=self.profiles,
                rules=self.rules,
                as_of=result.as_of,
            )
        )
        discrepant_ids = {
            transaction_id
            for finding in result.discrepancies
            for transaction_id in finding.transaction_ids
        }
        invalid_batches = {
            (finding.processor, finding.batch_id)
            for finding in result.discrepancies
            if finding.type in {
                DiscrepancyType.BATCH_CURRENCY_MISMATCH,
                DiscrepancyType.BATCH_TOTAL_MISMATCH,
            }
        }
        discrepant_ids.update(
            line.transaction_id
            for batch in batches
            if (batch.processor, batch.batch_id) in invalid_batches
            for line in batch.lines
        )
        result.reconciled_ids = result.matched_ids - discrepant_ids
        return result

    @staticmethod
    def _index_ledger(transactions: Iterable[Transaction]) -> dict[str, Transaction]:
        indexed: dict[str, Transaction] = {}
        for transaction in transactions:
            transaction_id = transaction.transaction_id
            if not transaction_id.strip():
                raise ValueError("ledger transaction ID cannot be empty")
            if transaction_id in indexed:
                raise ValueError(f"duplicate transaction ID in ledger: {transaction_id}")
            if (
                transaction.status is TransactionStatus.CAPTURED
                and transaction.captured_at is None
            ):
                raise ValueError(
                    f"captured transaction {transaction_id} requires captured_at"
                )
            indexed[transaction_id] = transaction
        return indexed

    @staticmethod
    def _validate_batch(batch: SettlementBatch) -> None:
        for line in batch.lines:
            currencies = {
                line.gross.currency,
                line.fee.currency,
                line.net.currency,
            }
            if len(currencies) != 1:
                joined = ", ".join(sorted(currencies))
                raise ValueError(
                    f"batch {batch.batch_id} line {line.transaction_id} mixes "
                    f"currencies: {joined}"
                )
        for label, reported in (
            ("reported gross", batch.reported_gross),
            ("reported fees", batch.reported_fees),
            ("reported net", batch.reported_net),
        ):
            if reported is not None and reported.currency != batch.currency:
                raise ValueError(
                    f"batch {batch.batch_id} {label} uses {reported.currency}, "
                    f"expected {batch.currency}"
                )

    def _accumulate(
        self, result: ReconciliationResult, batch: SettlementBatch, line: SettlementLine
    ) -> None:
        currency = line.currency
        result.totals_by_currency.setdefault(currency, Totals.empty(currency)).add(line)
        key = f"{batch.processor} · {currency}"
        result.totals_by_processor.setdefault(key, Totals.empty(currency)).add(line)
