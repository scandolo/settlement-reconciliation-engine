"""Stripe -- Payout Reconciliation Report (CSV).

Modelled on Stripe's real payout reconciliation export:

* lowercase ISO currency codes (`usd`, `mxn`) -- a favourite source of failed
  joins against systems that store them uppercase
* one row per balance transaction, including non-charge rows (`refund`,
  `payout`, `adjustment`) that must be filtered out before matching
* fees are already netted, and `net = gross - fee` should hold exactly
* `automatic_payout_id` groups rows into the actual bank deposit

Public reference: Stripe balance/payout reconciliation report schema.
"""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..models import SettlementBatch, SettlementLine
from ..money import Money, total
from .base import FeeRate, ProcessorProfile, read_text

PROFILE = ProcessorProfile(
    name="Stripe",
    countries=("MX", "US"),
    currencies=("USD", "MXN"),
    settlement_sla_days=3,
    fees={
        "credit_card": FeeRate(Decimal("0.0360"), {"MXN": Decimal("3.00"), "USD": Decimal("0.30")}),
        "debit_card": FeeRate(Decimal("0.0290"), {"MXN": Decimal("3.00"), "USD": Decimal("0.30")}),
        "oxxo": FeeRate(Decimal("0.0390"), {"MXN": Decimal("8.00")}),
    },
    file_format="csv",
    notes="Blended pricing; rows for refunds and adjustments share the file.",
)

#: Only these reporting categories represent merchant sales we can match.
CHARGE_CATEGORIES = {"charge", "payment"}


class StripeAdapter:
    profile = PROFILE
    format = "csv"

    def sniff(self, path: Path) -> bool:
        return (
            path.suffix.lower() == ".csv"
            and "balance_transaction_id" in read_text(path)[:2000]
        )

    def parse(self, path: Path) -> SettlementBatch:
        rows = list(csv.DictReader(read_text(path).splitlines()))
        charges = [r for r in rows if r["reporting_category"].strip() in CHARGE_CATEGORIES]
        if not charges:
            raise ValueError(f"{path.name}: no charge rows to settle")

        currency = charges[0]["currency"].strip().upper()
        lines = tuple(self._line(row) for row in charges)
        payout_id = charges[0]["automatic_payout_id"].strip()

        return SettlementBatch(
            batch_id=payout_id,
            processor=self.profile.name,
            settlement_date=datetime.fromisoformat(
                charges[0]["automatic_payout_effective_at_utc"].replace("Z", "+00:00")
            ).date(),
            currency=currency,
            lines=lines,
            reported_gross=total((ln.gross for ln in lines), currency),
            reported_fees=total((ln.fee for ln in lines), currency),
            # Stripe's payout row states the deposit actually sent, which is the
            # number worth validating the detail against.
            reported_net=_payout_net(rows, currency),
            source_file=path.name,
            source_format="csv",
        )

    def _line(self, row: dict[str, str]) -> SettlementLine:
        currency = row["currency"].strip().upper()
        return SettlementLine(
            transaction_id=row["merchant_reference"].strip(),
            gross=Money.parse(row["gross"], currency),
            fee=Money.parse(row["fee"], currency),
            net=Money.parse(row["net"], currency),
            payment_method=(row.get("payment_method") or "credit_card").strip().lower(),
        )


def _payout_net(rows: list[dict[str, str]], currency: str) -> Money | None:
    payouts = [r for r in rows if r["reporting_category"].strip() == "payout"]
    if not payouts:
        return None
    # Stripe books the payout as a negative balance movement; flip the sign so
    # it reads as "amount deposited".
    return total(
        (abs(Money.parse(r["gross"], r["currency"].strip().upper())) for r in payouts),
        currency,
    )
