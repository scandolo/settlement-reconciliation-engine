"""Test suite. Standard library only -- `python -m unittest discover tests`.

Three layers, matching the three things that can be wrong:

* money and currency behaviour (the foundation everything else assumes)
* adapters (does each processor's format survive a round trip?)
* detectors (does each rule fire when it should, and stay quiet when it should not?)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconciler import datagen  # noqa: E402
from reconciler.engine import ReconciliationEngine  # noqa: E402
from reconciler.models import (  # noqa: E402
    DiscrepancyType,
    SettlementBatch,
    SettlementLine,
    Transaction,
    TransactionStatus,
)
from reconciler.money import (  # noqa: E402
    REGISTRY,
    Currency,
    CurrencyMismatchError,
    Money,
    UnknownCurrencyError,
)
from reconciler.processors import PROFILES, load_batch  # noqa: E402
from reconciler.rules import DEFAULT_RULES  # noqa: E402

AS_OF = date(2026, 8, 14)


def txn(
    tid: str = "CT-1",
    amount: str = "100.00",
    currency: str = "BRL",
    processor: str = "dLocal",
    method: str = "pix",
    status: TransactionStatus = TransactionStatus.CAPTURED,
    captured: date | None = date(2026, 8, 1),
) -> Transaction:
    return Transaction(
        transaction_id=tid,
        processor=processor,
        country="BR",
        payment_method=method,
        amount=Money.parse(amount, currency),
        status=status,
        created_at=date(2026, 8, 1),
        captured_at=captured,
    )


def batch(*lines: SettlementLine, processor: str = "dLocal", currency: str = "BRL") -> SettlementBatch:
    return SettlementBatch(
        batch_id="B-1",
        processor=processor,
        settlement_date=date(2026, 8, 6),
        currency=currency,
        lines=lines,
    )


def line(
    tid: str = "CT-1",
    gross: str = "100.00",
    fee: str = "1.99",
    net: str | None = None,
    currency: str = "BRL",
    method: str = "pix",
) -> SettlementLine:
    g, f = Money.parse(gross, currency), Money.parse(fee, currency)
    return SettlementLine(tid, g, f, Money.parse(net, currency) if net else g - f, method)


def types_found(transactions, batches) -> set[str]:
    engine = ReconciliationEngine(transactions, PROFILES, DEFAULT_RULES)
    result = engine.reconcile(batches, as_of=AS_OF)
    return {d.type.value for d in result.discrepancies}


# --------------------------------------------------------------------------


class MoneyTests(unittest.TestCase):
    def test_quantises_to_currency_exponent(self):
        self.assertEqual(str(Money(Decimal("10.567"), "BRL").amount), "10.57")
        self.assertEqual(str(Money(Decimal("10.567"), "COP").amount), "11")  # no minor unit
        self.assertEqual(str(Money(Decimal("10.5674"), "KWD").amount), "10.567")  # three places

    def test_separators_are_read_against_the_currency_not_a_guess(self):
        """`10.567` is ten-point-five-seven in BRL and ten thousand in COP."""
        self.assertEqual(str(Money.parse("10.567", "BRL").amount), "10.57")
        self.assertEqual(str(Money.parse("10.567", "COP").amount), "10567")

    def test_refuses_to_mix_currencies(self):
        with self.assertRaises(CurrencyMismatchError):
            Money.parse("1", "BRL") + Money.parse("1", "USD")
        with self.assertRaises(CurrencyMismatchError):
            Money.parse("1", "BRL") < Money.parse("1", "MXN")

    def test_parses_supported_localized_money_formats(self):
        cases = [
            ("R$ 1.234,56", "BRL", "1234.56"),  # pt-BR decimal comma
            ("1,234.56", "USD", "1234.56"),  # en-US thousands separator
            ("(45.10)", "USD", "-45.10"),  # accounting negative
            ("2.000.000", "COP", "2000000"),  # no minor unit, dotted thousands
        ]
        for raw, currency, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(str(Money.parse(raw, currency).amount), expected)

    def test_minor_units_respect_the_exponent(self):
        self.assertEqual(str(Money.from_minor(12345, "MXN").amount), "123.45")
        self.assertEqual(str(Money.from_minor(12345, "COP").amount), "12345")

    def test_new_currency_is_one_line(self):
        REGISTRY.register(Currency("PEN", 2, "S/", "Peruvian sol"))
        self.assertEqual(str(Money.parse("10.5", "PEN")), "S/10.50 PEN")

    def test_unknown_currency_is_rejected_loudly(self):
        with self.assertRaises(UnknownCurrencyError):
            Money.parse("1", "XYZ")


class AdapterTests(unittest.TestCase):
    """Every writer is the inverse of its adapter, so files must round trip."""

    def test_all_four_formats_round_trip(self):
        dataset = datagen.generate(count=120, as_of=AS_OF)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            datagen.write_dataset(dataset, root)
            reparsed = {b.batch_id: b for b in map(load_batch, (root / "settlements").iterdir())}

        self.assertEqual(len(reparsed), len(dataset.batches))
        for original in dataset.batches:
            with self.subTest(batch=original.batch_id):
                found = reparsed[original.batch_id]
                self.assertEqual(found.processor, original.processor)
                self.assertEqual(found.settlement_date, original.settlement_date)
                self.assertEqual(
                    [(ln.transaction_id, ln.gross, ln.fee, ln.net) for ln in found.lines],
                    [(ln.transaction_id, ln.gross, ln.fee, ln.net) for ln in original.lines],
                )

    def test_every_format_is_represented(self):
        self.assertEqual(
            {p.file_format for p in PROFILES.values()}, {"csv", "json", "xml"}
        )
        self.assertEqual(len(PROFILES), 4)


class DetectorTests(unittest.TestCase):
    def test_clean_settlement_reports_nothing(self):
        t = txn()
        fee = PROFILES["dLocal"].expected_fee(t.amount, "pix")
        self.assertEqual(types_found([t], [batch(line(fee=str(fee.amount)))]), set())

    def test_missing_settlement(self):
        self.assertIn(
            DiscrepancyType.MISSING_SETTLEMENT.value,
            types_found([txn(captured=date(2026, 8, 1))], [batch()]),
        )

    def test_aging_is_low_priority_until_the_sla_lapses(self):
        recent = txn(captured=AS_OF - timedelta(days=6))  # dLocal SLA is 5 days
        found = types_found([recent], [batch()])
        self.assertIn(DiscrepancyType.SETTLEMENT_AGING.value, found)
        self.assertNotIn(DiscrepancyType.MISSING_SETTLEMENT.value, found)

    def test_unknown_settlement(self):
        self.assertIn(
            DiscrepancyType.UNKNOWN_SETTLEMENT.value,
            types_found([], [batch(line(tid="CT-GHOST"))]),
        )

    def test_amount_mismatch(self):
        found = types_found([txn(amount="100.00")], [batch(line(gross="80.00"))])
        self.assertIn(DiscrepancyType.AMOUNT_MISMATCH.value, found)

    def test_fee_error(self):
        found = types_found([txn()], [batch(line(fee="9.99"))])
        self.assertIn(DiscrepancyType.FEE_ERROR.value, found)

    def test_fee_within_tolerance_is_ignored(self):
        t = txn()
        contracted = PROFILES["dLocal"].expected_fee(t.amount, "pix")
        nudged = contracted + Money.parse("0.01", "BRL")
        self.assertNotIn(
            DiscrepancyType.FEE_ERROR.value,
            types_found([t], [batch(line(fee=str(nudged.amount)))]),
        )

    def test_currency_mismatch_blocks_meaningless_amount_comparison(self):
        found = types_found(
            [txn(currency="BRL")],
            [batch(line(currency="USD"), currency="USD")],
        )
        self.assertIn(DiscrepancyType.CURRENCY_MISMATCH.value, found)
        self.assertNotIn(DiscrepancyType.AMOUNT_MISMATCH.value, found)

    def test_net_arithmetic_error(self):
        self.assertIn(
            DiscrepancyType.NET_ARITHMETIC_ERROR.value,
            types_found([txn()], [batch(line(net="50.00"))]),
        )

    def test_status_conflict_on_a_refunded_transaction(self):
        self.assertIn(
            DiscrepancyType.STATUS_CONFLICT.value,
            types_found([txn(status=TransactionStatus.REFUNDED)], [batch(line())]),
        )

    def test_non_settleable_transactions_do_not_inflate_the_match_rate(self):
        captured = txn(tid="CT-CAPTURED")
        refunded = txn(tid="CT-REFUNDED", status=TransactionStatus.REFUNDED)
        settlement = batch(line(tid="CT-CAPTURED"), line(tid="CT-REFUNDED"))

        result = ReconciliationEngine(
            [captured, refunded], PROFILES, DEFAULT_RULES
        ).reconcile([settlement], as_of=AS_OF)

        self.assertEqual(result.awaiting_settlement, 1)
        self.assertEqual(result.matched_ids, {"CT-CAPTURED"})
        self.assertEqual(result.unmatched_count, 0)
        self.assertEqual(str(result.match_rate), "1")

    def test_duplicate_settlement_across_two_payouts(self):
        first = batch(line())
        second = SettlementBatch("B-2", "dLocal", date(2026, 8, 7), "BRL", (line(),))
        self.assertIn(
            DiscrepancyType.DUPLICATE_SETTLEMENT.value,
            types_found([txn()], [first, second]),
        )

    def test_processor_mismatch(self):
        self.assertIn(
            DiscrepancyType.PROCESSOR_MISMATCH.value,
            types_found([txn(processor="Adyen")], [batch(line(), processor="dLocal")]),
        )

    def test_batch_total_mismatch(self):
        stated = SettlementBatch(
            "B-1", "dLocal", date(2026, 8, 6), "BRL", (line(),),
            reported_net=Money.parse("999.00", "BRL"),
        )
        self.assertIn(DiscrepancyType.BATCH_TOTAL_MISMATCH.value, types_found([txn()], [stated]))


class EndToEndTests(unittest.TestCase):
    def test_generator_is_deterministic(self):
        first, second = datagen.generate(count=80), datagen.generate(count=80)
        self.assertEqual(
            [t.transaction_id for t in first.transactions],
            [t.transaction_id for t in second.transactions],
        )

    def test_every_injected_defect_is_detected(self):
        """The headline claim: 100% recall against known ground truth."""
        dataset = datagen.generate(as_of=AS_OF)
        engine = ReconciliationEngine(dataset.transactions, PROFILES, DEFAULT_RULES)
        result = engine.reconcile(dataset.batches, as_of=AS_OF)
        found = {
            (d.type.value, tid) for d in result.discrepancies for tid in d.transaction_ids
        } | {(d.type.value, d.batch_id) for d in result.discrepancies}

        for defect in dataset.defects:
            key = (defect.type.value, defect.transaction_id or defect.batch_id)
            with self.subTest(defect=key):
                self.assertIn(key, found)

    def test_exposure_never_silently_converts_currencies(self):
        dataset = datagen.generate(as_of=AS_OF)
        engine = ReconciliationEngine(dataset.transactions, PROFILES, DEFAULT_RULES)
        exposure = engine.reconcile(dataset.batches, as_of=AS_OF).exposure()
        for currency, amount in exposure.items():
            self.assertEqual(amount.currency, currency)

    def test_report_renderers_all_produce_output(self):
        from reconciler import reporting

        dataset = datagen.generate(count=120, as_of=AS_OF)
        engine = ReconciliationEngine(dataset.transactions, PROFILES, DEFAULT_RULES)
        result = engine.reconcile(dataset.batches, as_of=AS_OF)
        for fmt in reporting.RENDERERS:
            with self.subTest(fmt=fmt):
                self.assertTrue(len(reporting.render(result, fmt)) > 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
