"""Regression tests for input trust boundaries and reporting semantics."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconciler import reporting  # noqa: E402
from reconciler.engine import ReconciliationEngine  # noqa: E402
from reconciler.models import (  # noqa: E402
    DiscrepancyType,
    SettlementBatch,
    SettlementLine,
    Transaction,
    TransactionStatus,
)
from reconciler.money import Money  # noqa: E402
from reconciler.processors import PROFILES, load_batch  # noqa: E402
from reconciler.rules import DEFAULT_RULES  # noqa: E402

AS_OF = date(2026, 8, 14)


def transaction(
    transaction_id: str = "CT-1",
    *,
    amount: str = "100.00",
    captured_at: date | None = date(2026, 8, 1),
) -> Transaction:
    return Transaction(
        transaction_id=transaction_id,
        processor="dLocal",
        country="BR",
        payment_method="pix",
        amount=Money.parse(amount, "BRL"),
        status=TransactionStatus.CAPTURED,
        created_at=date(2026, 8, 1),
        captured_at=captured_at,
    )


def settlement_line(
    transaction_id: str = "CT-1",
    *,
    gross: str = "100.00",
    fee: str = "1.99",
    net: str | None = None,
    currency: str = "BRL",
    payment_method: str = "pix",
) -> SettlementLine:
    gross_money = Money.parse(gross, currency)
    fee_money = Money.parse(fee, currency)
    return SettlementLine(
        transaction_id=transaction_id,
        gross=gross_money,
        fee=fee_money,
        net=Money.parse(net, currency) if net else gross_money - fee_money,
        payment_method=payment_method,
    )


def settlement_batch(
    *lines: SettlementLine,
    batch_id: str = "B-1",
    currency: str = "BRL",
    reported_gross: Money | None = None,
    reported_fees: Money | None = None,
    reported_net: Money | None = None,
) -> SettlementBatch:
    return SettlementBatch(
        batch_id=batch_id,
        processor="dLocal",
        settlement_date=date(2026, 8, 6),
        currency=currency,
        lines=lines,
        reported_gross=reported_gross,
        reported_fees=reported_fees,
        reported_net=reported_net,
    )


class InputBoundaryTests(unittest.TestCase):
    def test_duplicate_ledger_ids_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate transaction ID"):
            ReconciliationEngine(
                [transaction("CT-DUP"), transaction("CT-DUP")],
                PROFILES,
                DEFAULT_RULES,
            )

    def test_captured_transaction_requires_capture_date(self):
        with self.assertRaisesRegex(ValueError, "captured_at"):
            ReconciliationEngine(
                [transaction("CT-NO-DATE", captured_at=None)],
                PROFILES,
                DEFAULT_RULES,
            )

    def test_valid_processor_content_does_not_depend_on_extension(self):
        source = Path(__file__).resolve().parents[1] / "data" / "settlements" / "DLO-BRL-20260813.json"
        with tempfile.TemporaryDirectory() as temporary:
            renamed = Path(temporary) / "processor-export.dat"
            renamed.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            parsed = load_batch(renamed)
        self.assertEqual(parsed.processor, "dLocal")

    def test_stripe_file_with_multiple_payouts_is_rejected(self):
        csv_text = "\n".join(
            [
                "balance_transaction_id,created_utc,gross,fee,net,currency,reporting_category,merchant_reference,payment_method,automatic_payout_id,automatic_payout_effective_at_utc",
                "txn_1,2026-08-12T00:00:00Z,100.00,3.90,96.10,usd,charge,CT-1,credit_card,po-1,2026-08-13T04:00:00Z",
                "txn_2,2026-08-13T00:00:00Z,80.00,3.18,76.82,usd,charge,CT-2,credit_card,po-2,2026-08-14T04:00:00Z",
                "payout_1,2026-08-13T04:00:00Z,-96.10,0,-96.10,usd,payout,,,,po-1,2026-08-13T04:00:00Z",
                "payout_2,2026-08-14T04:00:00Z,-76.82,0,-76.82,usd,payout,,,,po-2,2026-08-14T04:00:00Z",
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stripe.csv"
            path.write_text(csv_text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "multiple payouts"):
                load_batch(path)

    def test_mixed_currency_line_cannot_disable_batch_validation(self):
        invalid = settlement_batch(
            settlement_line("CT-BRL"),
            settlement_line("CT-USD", currency="USD", fee="1.00"),
            reported_net=Money.parse("999.00", "BRL"),
        )
        result = ReconciliationEngine([], PROFILES, DEFAULT_RULES).reconcile(
            [invalid], as_of=AS_OF
        )
        found = {finding.type for finding in result.discrepancies}
        self.assertIn(DiscrepancyType.BATCH_CURRENCY_MISMATCH, found)
        self.assertIn(DiscrepancyType.BATCH_TOTAL_MISMATCH, found)


class ReconciliationSemanticsTests(unittest.TestCase):
    def test_settlement_payment_method_cannot_bypass_fee_check(self):
        result = ReconciliationEngine(
            [transaction()], PROFILES, DEFAULT_RULES
        ).reconcile(
            [settlement_batch(settlement_line(fee="9.99", payment_method="unconfigured_method"))],
            as_of=AS_OF,
        )
        self.assertIn(
            DiscrepancyType.FEE_ERROR,
            {finding.type for finding in result.discrepancies},
        )

    def test_discrepant_id_match_is_not_cleanly_reconciled(self):
        result = ReconciliationEngine(
            [transaction()], PROFILES, DEFAULT_RULES
        ).reconcile(
            [settlement_batch(settlement_line(gross="80.00", fee="1.99"))],
            as_of=AS_OF,
        )
        payload = reporting.to_dict(result)
        self.assertEqual(result.matched_ids, {"CT-1"})
        self.assertEqual(result.reconciled_ids, set())
        self.assertEqual(payload["summary"]["id_matched"], 1)
        self.assertEqual(payload["summary"]["matched"], 0)
        self.assertEqual(payload["summary"]["unmatched"], 0)

    def test_batch_total_findings_have_distinct_stable_ids(self):
        invalid = settlement_batch(
            settlement_line(),
            reported_gross=Money.parse("90.00", "BRL"),
            reported_fees=Money.parse("1.00", "BRL"),
            reported_net=Money.parse("89.00", "BRL"),
        )
        result = ReconciliationEngine(
            [transaction()], PROFILES, DEFAULT_RULES
        ).reconcile([invalid], as_of=AS_OF)
        findings = [
            finding
            for finding in result.discrepancies
            if finding.type is DiscrepancyType.BATCH_TOTAL_MISMATCH
        ]
        self.assertEqual(len(findings), 3)
        self.assertEqual(len({finding.id for finding in findings}), 3)
        self.assertTrue(all(finding.impact is None for finding in findings))

    def test_duplicate_exposure_does_not_double_count_line_fee_variance(self):
        first = settlement_batch(settlement_line(), batch_id="B-1")
        duplicate = settlement_batch(
            settlement_line(fee="0.00", net="100.00"), batch_id="B-2"
        )
        result = ReconciliationEngine(
            [transaction()], PROFILES, DEFAULT_RULES
        ).reconcile([first, duplicate], as_of=AS_OF)
        self.assertEqual(result.exposure()["BRL"], Money.parse("-100.00", "BRL"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
