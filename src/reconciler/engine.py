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
from .models import Discrepancy, SettlementBatch, SettlementLine, Transaction
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
    matched_ids: set[str] = field(default_factory=set)
    ledger: dict[str, Transaction] = field(default_factory=dict, repr=False)
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
        return Decimal(len(self.matched_ids)) / Decimal(self.awaiting_settlement)

    def counts(self, attribute: str) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for finding in self.discrepancies:
            counts[getattr(finding, attribute).value] += 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def exposure(self) -> dict[str, Money]:
        """Net money at risk per currency: owed to us, minus overpaid to us."""
        exposure: dict[str, Money] = {}
        for finding in self.discrepancies:
            if finding.impact is None:
                continue
            code = finding.impact.currency
            exposure[code] = exposure.get(code, Money.zero(code)) + finding.impact
        return dict(sorted(exposure.items()))

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
        self.ledger = {t.transaction_id: t for t in transactions}
        self.profiles = profiles
        self.rules = rules

    def reconcile(
        self, batches: Sequence[SettlementBatch], as_of: date | None = None
    ) -> ReconciliationResult:
        result = ReconciliationResult(rules=self.rules)
        result.ledger = dict(self.ledger)
        result.source_batches = tuple(batches)
        result.ledger_size = len(self.ledger)
        result.awaiting_settlement = sum(1 for t in self.ledger.values() if t.expects_settlement)
        result.as_of = as_of or max((b.settlement_date for b in batches), default=date.today())

        # Every settlement of a transaction, so duplicates across *different*
        # payouts are visible, not just repeats within one file.
        settlements: dict[str, list[tuple[SettlementBatch, SettlementLine]]] = defaultdict(list)

        for batch in batches:
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
        return result

    def _accumulate(
        self, result: ReconciliationResult, batch: SettlementBatch, line: SettlementLine
    ) -> None:
        currency = line.currency
        result.totals_by_currency.setdefault(currency, Totals.empty(currency)).add(line)
        key = f"{batch.processor} · {currency}"
        result.totals_by_processor.setdefault(key, Totals.empty(currency)).add(line)
