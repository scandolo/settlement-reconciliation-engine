"""dLocal -- representative cross-border settlement report (JSON).

The public dLocal payment object informs identifiers, currencies, amounts and
JSON conventions. The settlement envelope is a challenge fixture because exact
merchant settlement exports are account-specific:

* amounts arrive as decimal *strings*, not numbers, to avoid float drift
* the merchant's reference is `order_id`; dLocal's own is `payment_id`
* the deduction is split into a commission and a local tax (IOF in Brazil,
  IVA in Mexico) that must be recombined into a single fee
* cross-border volume may settle in USD while the payment was taken in local
  currency -- the report keeps both, and we must not convert between them

Public reference: https://docs.dlocal.com/reference/the-order-object
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..models import SettlementBatch, SettlementLine
from ..money import Money, total
from .base import FeeRate, ProcessorProfile, read_text

PROFILE = ProcessorProfile(
    name="dLocal",
    countries=("BR", "MX", "CO"),
    currencies=("BRL", "MXN", "COP", "USD"),
    settlement_sla_days=5,
    fees={
        "pix": FeeRate(Decimal("0.0199"), {"BRL": Decimal("0.00")}),
        "spei": FeeRate(Decimal("0.0250"), {"MXN": Decimal("4.00")}),
        "pse": FeeRate(Decimal("0.0299"), {"COP": Decimal("900")}),
        "credit_card": FeeRate(
            Decimal("0.0399"),
            {"BRL": Decimal("0.50"), "MXN": Decimal("4.00"), "COP": Decimal("1200"), "USD": Decimal("0.30")},
        ),
    },
    file_format="json",
    notes="Cross-border; deductions split into commission plus local tax.",
)


class DLocalAdapter:
    profile = PROFILE
    format = "json"

    def sniff(self, path: Path) -> bool:
        return '"settlement"' in read_text(path)[:2000]

    def parse(self, path: Path) -> SettlementBatch:
        settlement = json.loads(read_text(path))["settlement"]
        currency = settlement["settlement_currency"].strip().upper()
        lines = tuple(self._line(item) for item in settlement["payments"])
        totals = settlement.get("totals", {})

        return SettlementBatch(
            batch_id=settlement["settlement_id"],
            processor=self.profile.name,
            settlement_date=datetime.fromisoformat(
                settlement["settlement_date"].replace("Z", "+00:00")
            ).date(),
            currency=currency,
            lines=lines,
            reported_gross=_money(totals.get("gross_amount"), currency)
            or total((ln.gross for ln in lines), currency),
            reported_fees=_money(totals.get("total_fees"), currency),
            reported_net=_money(totals.get("net_amount"), currency),
            source_file=path.name,
            source_format="json",
        )

    def _line(self, item: dict) -> SettlementLine:
        currency = item["currency"].strip().upper()
        commission = Money.parse(item["commission"], currency)
        tax = Money.parse(item.get("tax", "0"), currency)
        fee = commission + tax
        breakdown = {"commission": commission}
        if not tax.is_zero:
            breakdown["local_tax"] = tax

        return SettlementLine(
            transaction_id=item["order_id"].strip(),
            gross=Money.parse(item["amount"], currency),
            fee=fee,
            net=Money.parse(item["net_amount"], currency),
            payment_method=(item.get("payment_method") or "").strip().lower() or None,
            fee_breakdown=breakdown,
        )


def _money(raw: object, currency: str) -> Money | None:
    return None if raw is None else Money.parse(raw, currency)
