"""Canonical domain model.

Every processor file format is normalised into these types before the engine
sees it, so the engine knows nothing about CSV, JSON or XML. Adding a processor
means writing one adapter -- never touching the engine or the reports.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

from .money import Money


class TransactionStatus(str, Enum):
    """Where a transaction sits in the authorise -> capture -> settle lifecycle."""

    AUTHORIZED = "authorized"  # funds reserved, not claimed -> not settleable yet
    CAPTURED = "captured"  # merchant claimed the funds -> settlement expected
    REFUNDED = "refunded"  # money returned -> must not settle
    CHARGEBACK = "chargeback"  # forcibly reversed -> must not settle
    FAILED = "failed"


class DiscrepancyType(str, Enum):
    MISSING_SETTLEMENT = "missing_settlement"
    UNKNOWN_SETTLEMENT = "unknown_settlement"
    DUPLICATE_SETTLEMENT = "duplicate_settlement"
    AMOUNT_MISMATCH = "amount_mismatch"
    NET_ARITHMETIC_ERROR = "net_arithmetic_error"
    FEE_ERROR = "fee_error"
    CURRENCY_MISMATCH = "currency_mismatch"
    STATUS_CONFLICT = "status_conflict"
    BATCH_CURRENCY_MISMATCH = "batch_currency_mismatch"
    BATCH_TOTAL_MISMATCH = "batch_total_mismatch"
    SETTLEMENT_AGING = "settlement_aging"
    PROCESSOR_MISMATCH = "processor_mismatch"


class Severity(str, Enum):
    CRITICAL = "critical"  # money lost or double-paid; act today
    HIGH = "high"  # material amount at risk
    MEDIUM = "medium"  # real but bounded impact
    LOW = "low"  # informational, watch only

    @property
    def rank(self) -> int:
        return ["critical", "high", "medium", "low"].index(self.value)


@dataclass(frozen=True)
class Transaction:
    """An internal transaction record -- the merchant's own source of truth."""

    transaction_id: str
    processor: str
    country: str
    payment_method: str
    amount: Money
    status: TransactionStatus
    created_at: date
    captured_at: date | None = None

    @property
    def currency(self) -> str:
        return self.amount.currency

    @property
    def expects_settlement(self) -> bool:
        return self.status is TransactionStatus.CAPTURED and self.captured_at is not None


@dataclass(frozen=True)
class SettlementLine:
    """One transaction's row inside a processor's settlement report."""

    transaction_id: str
    gross: Money
    fee: Money
    net: Money
    payment_method: str | None = None
    #: Fee broken out by component where the processor provides it (Adyen splits
    #: commission / markup / scheme fees / interchange). Kept for explainability.
    fee_breakdown: dict[str, Money] = field(default_factory=dict)

    @property
    def currency(self) -> str:
        return self.gross.currency


@dataclass(frozen=True)
class SettlementBatch:
    """A processor payout: many transactions, one bank deposit."""

    batch_id: str
    processor: str
    settlement_date: date
    currency: str
    lines: tuple[SettlementLine, ...]
    #: Header totals as claimed by the processor, used to validate the detail.
    reported_gross: Money | None = None
    reported_fees: Money | None = None
    reported_net: Money | None = None
    source_file: str | None = None
    source_format: str | None = None


@dataclass
class Discrepancy:
    """One actionable finding, with everything an analyst needs to chase it."""

    type: DiscrepancyType
    severity: Severity
    processor: str
    description: str
    recommended_action: str
    transaction_ids: list[str] = field(default_factory=list)
    batch_id: str | None = None
    metric: str | None = None
    expected: Any = None
    actual: Any = None
    #: Signed money impact. Positive = we are owed money. Negative = we were
    #: overpaid and should expect a clawback.
    impact: Money | None = None

    @property
    def id(self) -> str:
        """Stable identifier, so the same issue keeps the same ID across runs.

        This is what lets finance track "is this the same break as last month?"
        and lets the tool be run on a schedule without re-alerting on known items.
        """
        parts = [self.type.value, self.processor, str(self.batch_id)]
        if self.metric is not None:
            parts.append(self.metric)
        parts.append(",".join(sorted(self.transaction_ids)))
        seed = "|".join(parts)
        return f"{self.type.value[:4].upper()}-{hashlib.sha1(seed.encode()).hexdigest()[:8]}"

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "severity": self.severity.value,
            "processor": self.processor,
            "batch_id": self.batch_id,
            "metric": self.metric,
            "transaction_ids": self.transaction_ids,
            "description": self.description,
            "expected": _jsonable(self.expected),
            "actual": _jsonable(self.actual),
            "impact": self.impact.to_json() if self.impact is not None else None,
            "impact_display": str(self.impact) if self.impact is not None else None,
            "recommended_action": self.recommended_action,
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Money):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value
