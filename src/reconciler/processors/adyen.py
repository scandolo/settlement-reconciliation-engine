"""Adyen -- Settlement Detail Report (CSV).

Modelled on Adyen's real settlement detail report, which is the awkward one:

* every field is quoted, and money is split across `Gross Debit`/`Gross Credit`
  columns rather than carrying a sign
* the fee is broken into four components -- commission, markup, scheme fees and
  interchange -- that must be recombined to get what was actually deducted
* the merchant's own reference lives in `Merchant Reference`, while
  `Psp Reference` is Adyen's ID; matching on the wrong one is a classic bug
* settlement can be in a different currency from capture (`Gross Currency` vs
  `Net Currency`), which is exactly the case we must flag rather than convert

Public reference: Adyen settlement detail report column set.
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
    name="Adyen",
    countries=("BR", "MX", "CO", "US"),
    currencies=("USD", "BRL", "MXN"),
    settlement_sla_days=2,
    fees={
        "credit_card": FeeRate(Decimal("0.0060"), {"USD": Decimal("0.11"), "BRL": Decimal("0.60"), "MXN": Decimal("2.00")}),
        "debit_card": FeeRate(Decimal("0.0045"), {"USD": Decimal("0.11"), "BRL": Decimal("0.60"), "MXN": Decimal("2.00")}),
        "pix": FeeRate(Decimal("0.0110"), {"BRL": Decimal("0.00")}),
    },
    file_format="csv",
    notes="Interchange++ pricing; fee arrives split across four columns.",
)

FEE_COLUMNS = {
    "commission": "Commission (NC)",
    "markup": "Markup (NC)",
    "scheme_fees": "Scheme Fees (NC)",
    "interchange": "Interchange (NC)",
}


class AdyenAdapter:
    profile = PROFILE
    format = "csv"

    def sniff(self, path: Path) -> bool:
        return path.suffix.lower() == ".csv" and "Psp Reference" in read_text(path)[:2000]

    def parse(self, path: Path) -> SettlementBatch:
        rows = list(csv.DictReader(read_text(path).splitlines()))
        if not rows:
            raise ValueError(f"{path.name}: no settlement rows")

        lines = [self._line(row) for row in rows]
        currency = lines[0].currency
        first = rows[0]

        return SettlementBatch(
            batch_id=first["Batch Number"].strip(),
            processor=self.profile.name,
            settlement_date=datetime.strptime(first["Booking Date"], "%Y-%m-%d").date(),
            currency=currency,
            lines=tuple(lines),
            # Adyen does not restate batch totals in the detail report, so we
            # derive gross/fees and leave `reported_net` for the bank statement.
            reported_gross=total((ln.gross for ln in lines), currency),
            reported_fees=total((ln.fee for ln in lines), currency),
            source_file=path.name,
            source_format="csv",
        )

    def _line(self, row: dict[str, str]) -> SettlementLine:
        gross_currency = row["Gross Currency"].strip().upper()
        net_currency = row["Net Currency"].strip().upper()

        # Credit means money to the merchant, debit money away from it.
        gross = Money.parse(row["Gross Credit (GC)"] or row["Gross Debit (GC)"] or 0, gross_currency)
        net = Money.parse(row["Net Credit (NC)"] or row["Net Debit (NC)"] or 0, net_currency)

        breakdown = {
            label: Money.parse(row.get(column) or 0, net_currency)
            for label, column in FEE_COLUMNS.items()
        }
        fee = total(breakdown.values(), net_currency)

        # A settlement-currency switch is a finding, not something to paper over:
        # surface the gross in the currency Adyen actually settled in so the
        # engine's currency check fires against the original transaction.
        if gross_currency != net_currency:
            gross = Money(gross.amount, net_currency)

        return SettlementLine(
            transaction_id=row["Merchant Reference"].strip(),
            gross=gross,
            fee=fee,
            net=net,
            payment_method=_method(row.get("Payment Method", "")),
            fee_breakdown={k: v for k, v in breakdown.items() if not v.is_zero},
        )


def _method(raw: str) -> str:
    value = raw.strip().lower()
    if value in {"visa", "mc", "mastercard", "amex", "visadebit"}:
        return "debit_card" if "debit" in value else "credit_card"
    return value or "credit_card"
