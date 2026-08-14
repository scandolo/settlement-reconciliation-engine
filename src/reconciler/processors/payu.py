"""PayU Latam -- representative reconciliation report (XML).

PayU publicly documents its financial-statement reconciliation concepts. This
challenge fixture puts those concepts in a small XML envelope so the ingestion
layer must handle a third format; it is not claimed to be a drop-in merchant
export schema:

* money lives in element *attributes* rather than child elements
* Colombian pesos have no minor unit, so amounts are whole numbers and any
  code that assumes two decimals will invent rounding breaks
* every entry carries both PayU's own `payuOrderId` and our `reference`;
  matching on the wrong one is the classic reconciliation bug, so the adapter
  is explicit about which is the merchant key
* the batch header restates its own totals, which is what makes header-versus-
  detail validation possible

Public reference:
https://developers.payulatam.com/latam/en/payu-module-documentation/reports/financial-statement.html
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

from ..models import SettlementBatch, SettlementLine
from ..money import Money
from .base import FeeRate, ProcessorProfile, read_text

PROFILE = ProcessorProfile(
    name="PayU Latam",
    countries=("CO",),
    currencies=("COP",),
    settlement_sla_days=7,
    fees={
        "pse": FeeRate(Decimal("0.0299"), {"COP": Decimal("900")}),
        "credit_card": FeeRate(Decimal("0.0349"), {"COP": Decimal("900")}),
        "cash": FeeRate(Decimal("0.0250"), {"COP": Decimal("1500")}),
    },
    file_format="xml",
    notes="Colombia only; COP has no minor unit.",
)


class PayUAdapter:
    profile = PROFILE
    format = "xml"

    def sniff(self, path: Path) -> bool:
        return "ReconciliationReport" in read_text(path)[:2000]

    def parse(self, path: Path) -> SettlementBatch:
        root = ElementTree.fromstring(read_text(path))
        currency = (root.get("currency") or self.profile.currencies[0]).upper()
        lines = tuple(self._line(entry, currency) for entry in root.iterfind(".//Transaction"))
        totals = root.find("Totals")

        return SettlementBatch(
            batch_id=root.get("batchId"),
            processor=self.profile.name,
            settlement_date=datetime.strptime(root.get("settlementDate"), "%Y-%m-%d").date(),
            currency=currency,
            lines=lines,
            reported_gross=_attr(totals, "gross", currency),
            reported_fees=_attr(totals, "fees", currency),
            reported_net=_attr(totals, "net", currency),
            source_file=path.name,
            source_format="xml",
        )

    def _line(self, entry: ElementTree.Element, batch_currency: str) -> SettlementLine:
        currency = (entry.get("currency") or batch_currency).upper()
        # `reference` is our id; `payuOrderId` is PayU's. Always match on ours.
        reference = (entry.get("reference") or "").strip()
        return SettlementLine(
            transaction_id=reference,
            gross=Money.parse(entry.get("gross"), currency),
            fee=Money.parse(entry.get("fee"), currency),
            net=Money.parse(entry.get("net"), currency),
            payment_method=(entry.get("method") or "").strip().lower() or None,
        )


def _attr(element: ElementTree.Element | None, name: str, currency: str) -> Money | None:
    if element is None or element.get(name) is None:
        return None
    return Money.parse(element.get(name), currency)
