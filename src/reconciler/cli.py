"""Command line interface.

    python -m reconciler demo          # generate data, reconcile, verify -- one command
    python -m reconciler generate      # rebuild the test dataset
    python -m reconciler reconcile     # reconcile whatever is in data/
    python -m reconciler verify        # prove every injected defect was detected
    python -m reconciler processors    # show the registered processors and rate cards
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from . import datagen, detectors, ledger, reporting
from .engine import ReconciliationEngine, ReconciliationResult
from .money import REGISTRY
from .processors import PROFILES, UnknownSettlementFormatError, load_batches
from .rules import DEFAULT_RULES, STRICT_RULES

DATA = Path("data")
OUT = Path("out")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    dataset = datagen.generate(seed=args.seed, count=args.count, as_of=args.as_of)
    written = datagen.write_dataset(dataset, args.data)
    print(f"Ledger        {written['ledger']}  ({len(dataset.transactions)} transactions)")
    for status, count in ledger.summarise(dataset.transactions).items():
        print(f"                {status:<12} {count}")
    print(f"Settlements   {args.data / 'settlements'}  ({len(dataset.batches)} files)")
    for batch in dataset.batches:
        print(f"                {batch.batch_id:<24} {len(batch.lines):>4} lines  {batch.currency}")
    print(f"Ground truth  {written['ground_truth']}  ({len(dataset.defects)} injected defects)")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    result = _run(args)
    output = reporting.render(result, args.format)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(output)
    return 1 if args.fail_on_discrepancy and not result.clean else 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Prove the engine found every defect the generator planted.

    This is the difference between "the code runs" and "the logic is right".
    """
    truth_path = args.data / "ground_truth.json"
    if not truth_path.exists():
        print(f"No ground truth at {truth_path}. Run `generate` first.", file=sys.stderr)
        return 2

    expected = json.loads(truth_path.read_text(encoding="utf-8"))["injected_defects"]
    result = _run(args)
    found = {
        (d.type.value, txn) for d in result.discrepancies for txn in (d.transaction_ids or [None])
    }
    found_batch = {(d.type.value, d.batch_id) for d in result.discrepancies}

    print(f"Verifying {len(expected)} injected defects against {len(result.discrepancies)} findings")
    print("-" * 78)
    misses = 0
    for defect in expected:
        key = (defect["type"], defect["transaction_id"])
        hit = key in found or (
            defect["transaction_id"] is None and (defect["type"], defect["batch_id"]) in found_batch
        )
        misses += not hit
        marker = "PASS" if hit else "MISS"
        target = defect["transaction_id"] or defect["batch_id"]
        print(f"  [{marker}] {defect['type']:<24} {target:<22} {defect['note']}")

    print("-" * 78)
    detected = len(expected) - misses
    print(f"  Detected {detected}/{len(expected)} injected defects ({detected / len(expected):.0%} recall)")
    extra = len(result.discrepancies) - detected
    if extra > 0:
        print(f"  Plus {extra} further findings from naturally occurring data "
              "(aging transactions, knock-on effects).")
    return 1 if misses else 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Everything a reviewer needs, in one command."""
    print("== 1. Generating test data " + "=" * 51)
    cmd_generate(args)
    print()
    print("== 2. Reconciling " + "=" * 60)
    result = _run(args)
    print(reporting.render(result, "console"))
    print()
    print("== 3. Verifying against ground truth " + "=" * 41)
    status = cmd_verify(args)
    _write_artifacts(result, args)
    return status


def cmd_processors(args: argparse.Namespace) -> int:
    print(f"{len(PROFILES)} registered processors\n")
    for profile in PROFILES.values():
        print(f"  {profile.name}")
        print(f"    format      {profile.file_format}")
        print(f"    markets     {', '.join(profile.countries)}")
        print(f"    currencies  {', '.join(profile.currencies)}")
        print(f"    settles in  {profile.settlement_sla_days} days")
        for method, rate in profile.rate_card():
            print(f"    rate        {method:<14} {rate}")
        if profile.notes:
            print(f"    notes       {profile.notes}")
        print()
    print(f"{len(REGISTRY)} registered currencies")
    print("  " + ", ".join(f"{c.code}({c.exponent}dp)" for c in REGISTRY))
    print()
    print("Detectors")
    for scope, names in detectors.registered().items():
        print(f"  {scope:<8} {', '.join(names)}")
    return 0


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> ReconciliationResult:
    transactions = ledger.load_transactions(args.data / "transactions.csv")
    try:
        batches = load_batches(args.data / "settlements")
    except UnknownSettlementFormatError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    rules = STRICT_RULES if getattr(args, "strict", False) else DEFAULT_RULES
    engine = ReconciliationEngine(transactions, PROFILES, rules)
    return engine.reconcile(batches, as_of=args.as_of)


def _write_artifacts(result: ReconciliationResult, args: argparse.Namespace) -> None:
    """Persist the committed sample outputs and the dashboard payload."""
    OUT.mkdir(exist_ok=True)
    (OUT / "reconciliation-report.md").write_text(reporting.to_markdown(result), encoding="utf-8")
    (OUT / "reconciliation-report.json").write_text(reporting.to_json(result), encoding="utf-8")
    (OUT / "discrepancies.csv").write_text(reporting.to_csv(result), encoding="utf-8")
    dashboard = Path("public/data/report.json")
    if dashboard.parent.exists():
        dashboard.write_text(reporting.to_json(result), encoding="utf-8")
    print(f"\nWrote {OUT}/reconciliation-report.md, .json and discrepancies.csv")


def _as_of(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconciler",
        description="Settlement reconciliation for multi-processor, multi-currency merchants.",
    )
    parser.add_argument("--data", type=Path, default=DATA, help="data directory (default: data)")
    parser.add_argument("--as-of", type=_as_of, default=date(2026, 8, 14),
                        help="reconciliation date, YYYY-MM-DD")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="build a deterministic test dataset")
    generate.add_argument("--seed", type=int, default=20260814)
    generate.add_argument("--count", type=int, default=340, help="number of transactions")
    generate.set_defaults(func=cmd_generate)

    reconcile = subparsers.add_parser("reconcile", help="reconcile settlements against the ledger")
    reconcile.add_argument("--format", choices=list(reporting.RENDERERS), default="console")
    reconcile.add_argument("--out", type=Path, help="write to a file instead of stdout")
    reconcile.add_argument("--strict", action="store_true", help="zero tolerance, lower materiality")
    reconcile.add_argument("--fail-on-discrepancy", action="store_true",
                           help="exit non-zero if anything is flagged (for CI)")
    reconcile.set_defaults(func=cmd_reconcile)

    verify = subparsers.add_parser("verify", help="prove every injected defect is detected")
    verify.add_argument("--strict", action="store_true")
    verify.set_defaults(func=cmd_verify)

    demo = subparsers.add_parser("demo", help="generate, reconcile and verify in one go")
    demo.add_argument("--seed", type=int, default=20260814)
    demo.add_argument("--count", type=int, default=340)
    demo.add_argument("--strict", action="store_true")
    demo.set_defaults(func=cmd_demo)

    show = subparsers.add_parser("processors", help="list processors, currencies and detectors")
    show.set_defaults(func=cmd_processors)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
