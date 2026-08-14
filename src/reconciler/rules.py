"""Tolerances and materiality.

Reconciliation drowns in noise unless you can say "don't page me for two
centavos". These are the knobs a finance team actually turns.

Everything is derived from the currency registry rather than hard-coded per
currency, so a new market inherits sensible defaults automatically.

One deliberate distinction: we **never** convert currencies to match or compare
amounts -- that would invent breaks. We do use an indicative FX table for one
narrow purpose, ranking severity across currencies, so that a large Colombian
break and a large US one sort sensibly in the same worklist. Ranking is the
only place a rate is allowed to appear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from .money import REGISTRY, Money


@dataclass(frozen=True)
class ReconciliationRules:
    #: Amounts within this many minor units are treated as rounding noise.
    #: 5 minor units is 0.05 in USD/BRL/MXN and 5 pesos in COP.
    tolerance_minor_units: int = 5
    #: A fee may deviate by this fraction of the contracted amount before it is
    #: reported, on top of the absolute tolerance above.
    fee_relative_tolerance: Decimal = Decimal("0.01")
    #: Days of grace on top of a processor's SLA before a captured-but-unsettled
    #: transaction is escalated from "aging" to "missing".
    aging_grace_days: int = 1
    #: A finding worth more than this, in USD-equivalent, is CRITICAL.
    materiality_usd: Decimal = Decimal("250")
    #: Indicative units per USD, used *only* to rank severity across currencies.
    indicative_fx: Mapping[str, Decimal] = field(
        default_factory=lambda: {
            "USD": Decimal("1"),
            "EUR": Decimal("0.92"),
            "BRL": Decimal("5.4"),
            "MXN": Decimal("18.5"),
            "COP": Decimal("4000"),
            "CLP": Decimal("950"),
            "JPY": Decimal("150"),
            "KWD": Decimal("0.31"),
        }
    )
    #: Explicit per-currency overrides, if a controller wants a specific number.
    tolerance_overrides: Mapping[str, Decimal] = field(default_factory=dict)

    # -- absolute tolerance ----------------------------------------------

    def tolerance(self, currency: str) -> Decimal:
        if currency in self.tolerance_overrides:
            return self.tolerance_overrides[currency]
        return REGISTRY.get(currency).quantum * self.tolerance_minor_units

    def exceeds(self, delta: Money) -> bool:
        """True when a difference is big enough to be worth a human's time."""
        return abs(delta.amount) > self.tolerance(delta.currency)

    def fee_exceeds(self, delta: Money, expected: Money) -> bool:
        """Fees also get a relative allowance, since rate cards round differently."""
        relative = abs(expected.amount) * self.fee_relative_tolerance
        return abs(delta.amount) > max(self.tolerance(delta.currency), relative)

    # -- materiality -----------------------------------------------------

    def critical_threshold(self, currency: str) -> Decimal:
        rate = self.indicative_fx.get(currency, Decimal("1"))
        return self.materiality_usd * rate

    def usd_equivalent(self, amount: Money) -> Decimal:
        """Indicative USD value, for ranking and headline totals only."""
        rate = self.indicative_fx.get(amount.currency, Decimal("1"))
        return (amount.amount / rate).quantize(Decimal("0.01"))


DEFAULT_RULES = ReconciliationRules()

#: Audit profile: report every last centavo, escalate sooner.
STRICT_RULES = ReconciliationRules(
    tolerance_minor_units=0,
    fee_relative_tolerance=Decimal("0"),
    aging_grace_days=0,
    materiality_usd=Decimal("50"),
)
