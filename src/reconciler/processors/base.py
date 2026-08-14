"""Processor profiles and the adapter contract.

Two things are deliberately separated:

* a **profile** is the commercial agreement -- which currencies a processor
  settles in, what it may charge, how fast it must pay. Renegotiating a rate is
  a data change.
* an **adapter** is the technical translation -- whatever shape that
  processor's file happens to be, turn it into a `SettlementBatch`.

Adapters are registered by decorating them, so a new processor is one
self-contained module and an import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from ..models import SettlementBatch
from ..money import Money


@dataclass(frozen=True)
class FeeRate:
    """A contracted rate: a percentage of gross plus a per-currency fixed part."""

    percentage: Decimal
    fixed: Mapping[str, Decimal] = field(default_factory=dict)

    def expected_fee(self, gross: Money) -> Money:
        fixed = self.fixed.get(gross.currency, Decimal(0))
        return Money(gross.amount * self.percentage + fixed, gross.currency)

    def describe(self, currency: str) -> str:
        pct = f"{self.percentage * 100:.2f}%".rstrip("0").rstrip(".")
        fixed = self.fixed.get(currency)
        return f"{pct} + {fixed} {currency}" if fixed else pct


@dataclass(frozen=True)
class ProcessorProfile:
    """What we know, contractually, about how a processor should behave."""

    name: str
    #: Markets this processor serves for us, as ISO country codes.
    countries: tuple[str, ...]
    #: Currencies it is allowed to settle in.
    currencies: tuple[str, ...]
    #: Captured transactions must settle within this many days.
    settlement_sla_days: int
    #: Contracted rate card, keyed by payment method.
    fees: Mapping[str, FeeRate]
    file_format: str
    notes: str = ""

    def expected_fee(self, gross: Money, payment_method: str | None) -> Money | None:
        """Contracted fee for this line, or None if the method is not rate-carded."""
        rate = self.fees.get((payment_method or "").lower())
        return rate.expected_fee(gross) if rate else None

    def rate_card(self) -> list[tuple[str, str]]:
        primary = self.currencies[0]
        return [(method, rate.describe(primary)) for method, rate in self.fees.items()]


@runtime_checkable
class SettlementAdapter(Protocol):
    """Parses one processor's report format into the canonical batch model."""

    profile: ProcessorProfile

    def sniff(self, path: Path) -> bool:
        """True if this adapter recognises the file as its own."""

    def parse(self, path: Path) -> SettlementBatch:
        """Parse the file into a canonical `SettlementBatch`."""


def read_text(path: Path) -> str:
    """Read a report, tolerating the BOM that Windows-exported reports carry."""
    return path.read_text(encoding="utf-8-sig")
