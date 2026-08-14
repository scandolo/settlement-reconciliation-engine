# Payment Reconciliation

Automated settlement reconciliation for a merchant taking payments through
multiple processors, in multiple currencies, across multiple countries.

It ingests settlement reports in whatever format each processor sends, matches
them line by line against the internal transaction ledger, and produces a
ranked worklist of everything that does not add up - with the money at stake
and the next action attached to each finding.

**Live dashboard:** [settlement-reconciliation-engine.vercel.app](https://settlement-reconciliation-engine.vercel.app)
**Zero dependencies.** Python 3.10+ standard library only, so it runs anywhere
without an install step.

---

## Quick start

```bash
git clone https://github.com/scandolo/settlement-reconciliation-engine.git
cd settlement-reconciliation-engine
make demo          # or: PYTHONPATH=src python3 -m reconciler demo
```

That one command generates the test data, reconciles it, and verifies the
result against known ground truth. It takes about a second and needs nothing
installed.

```bash
make test          # 38 tests, standard library unittest
make processors    # registered processors, currencies, rate cards, detectors
make report        # write the Markdown report to out/
```

Individual commands:

```bash
PYTHONPATH=src python3 -m reconciler generate                      # rebuild test data
PYTHONPATH=src python3 -m reconciler reconcile --format markdown   # console|markdown|json|csv
PYTHONPATH=src python3 -m reconciler reconcile --strict            # zero-tolerance audit run
PYTHONPATH=src python3 -m reconciler reconcile --fail-on-discrepancy   # exit 1, for CI
PYTHONPATH=src python3 -m reconciler verify                        # prove detection recall
```

Committed sample outputs live in [`out/`](out/):
[`reconciliation-report.md`](out/reconciliation-report.md),
[`reconciliation-report.json`](out/reconciliation-report.json),
[`discrepancies.csv`](out/discrepancies.csv).

---

## What it found in the sample data

From 340 ledger transactions and 9 settlement files across 4 processors:

```
  Batches processed      9
  Settlement lines       212
  Ledger transactions    340 (254 expecting settlement)
  ID matched             208  (81.9%)
  Cleanly reconciled     166  (65.4%)
  Unmatched              46
  Discrepancies          30

  MONEY AT RISK   (positive = owed to us, negative = overpaid to us)
    BRL   R$-1,016.22 BRL
    COP   COL$-933,862 COP
    MXN   MX$5,748.76 MXN
    USD   US$2.04 USD
    indicative total  ~US$734.44

  BY TYPE  settlement_aging=8  batch_total_mismatch=4  fee_error=3
           processor_mismatch=3  missing_settlement=3  amount_mismatch=2
           unknown_settlement=2  net_arithmetic_error=1  status_conflict=1
           currency_mismatch=1  batch_currency_mismatch=1
           duplicate_settlement=1
```

And, from `verify`:

```
  Detected 15/15 injected defects (100% recall)
```

---

## Who this is for, and why it looks like this

The user is a **reconciliation analyst closing the month** - the person
currently spending 60+ hours doing this by hand - and secondarily the **CFO**
who wants one number. Three consequences run through the whole design:

1. **It is an exception tool, not a ledger dump.** Most of the 212 settlement
   lines require no action; nobody needs to inspect those one by one. The
   output is the 30 actionable findings, ordered by money at risk rather than
   by file order, so the worst thing is the first thing.
2. **Every finding carries its next action.** Not an error code - the sentence
   the analyst would otherwise have to compose: which processor to contact,
   what to quote, and what happens if they do nothing.
3. **The numbers arrive in the format they already work in.** Terminal for a
   quick check, Markdown to paste into the month-end ticket, **CSV because
   finance teams live in spreadsheets**, JSON for the dashboard and for
   downstream automation. One report model, four renderers, so the figures can
   never disagree with each other.

Findings also carry a **stable ID** (`FEE_-3a91c204`), derived from the finding
rather than its position. The same break keeps the same ID across runs, which
is what makes it possible to run this nightly and only alert on what is new.

---

## Architecture

```
settlement files ──▶ adapters ──▶ canonical model ──▶ detectors ──▶ report model ──▶ renderers
  (csv/json/xml)     processors/     models.py       detectors.py    engine.py       reporting.py
                                          ▲
internal ledger ────────────────────────┘
  (transactions.csv)   ledger.py
```

| Module | Responsibility |
| --- | --- |
| `money.py` | Currency registry and currency-safe `Money` arithmetic |
| `models.py` | Canonical domain types: transactions, settlement lines, batches, findings |
| `processors/` | One adapter per processor; the only code that knows about file formats |
| `detectors.py` | One small function per discrepancy rule |
| `engine.py` | Orchestration only - index the ledger, walk batches, run detectors |
| `rules.py` | Tolerances, materiality, severity banding |
| `reporting.py` | One report model, four renderers |
| `datagen.py` | Deterministic test data, plus the ground truth that grades it |

Each layer only depends on the one to its left. The engine never opens a file;
the adapters never know what a discrepancy is; the renderers never recompute a
number.

### The reconciliation runs in three passes

Each maps to a question the finance team actually asks:

| Pass | Question | Detectors |
| --- | --- | --- |
| **Line** | Is every line in this payout correct? | net arithmetic, unknown settlement, currency mismatch, status conflict, amount mismatch, fee error, processor mismatch |
| **Batch** | Does the payout add up to what they said they paid? | batch total mismatch |
| **Ledger** | What did we capture that nobody has paid us for? | duplicate settlement, missing settlement, aging |

---

## Design decisions

**Currency safety is structural, not defensive.** `Money` refuses to add,
subtract or compare across currencies - it raises `CurrencyMismatchError`. The
engine cannot silently mix BRL and USD because the type system will not let it.
Each amount is a `Decimal` quantised to that currency's real ISO exponent, and
that detail matters: **COP has no minor unit**, so any code assuming two
decimal places invents rounding breaks on every Colombian transaction. KWD is
registered too, at three decimal places, to prove the model is not quietly
built around cents.

**We never convert currencies to match.** Converting would manufacture
discrepancies out of FX movement. Amounts are compared only in their original
currency, and exposure is reported per currency. There is exactly one place a
rate appears - ranking severity across currencies, so a large Colombian break
and a large US one sort sensibly in one worklist - and it is labelled
indicative everywhere it surfaces.

**An ID match is not automatically a reconciliation.** The report keeps the
208 expected IDs that appeared in processor files separate from the 166 that
have no line, duplicate, or batch-control exception. This prevents a wrong
amount from being counted as successfully reconciled merely because its ID was
present.

**Economic exposure is counted once per root event.** Duplicate settlements
are valued from the expected net receipt against all actual receipts. Their
line-level fee and amount findings remain visible for investigation but are
not summed again. Batch header breaks are control failures and carry no
independent cash impact until the processor confirms which figure is real.

**Input boundaries fail loudly.** Duplicate ledger IDs, captured rows without
capture dates, internally mixed line currencies, and multi-payout Stripe files
are rejected instead of being collapsed or ignored. Settlement adapters sniff
content, so a valid export does not stop working because its filename changes.

**Configuration over code.** Currencies are a registry. Processors are a
registry. Rate cards, SLAs and tolerances are data on a `ProcessorProfile`.
Renegotiating a fee, onboarding a market, or changing a materiality threshold
is a data change, not a deploy of new logic.

**Detectors are focused and independent.** Each guards its own preconditions
and yields findings. They can run in any
order and none can corrupt another. Adding a rule does not mean touching a
600-line class - it means writing a function and decorating it.

**Content sniffing, not filename conventions.** Adapters identify their own
files by looking inside them, because processors change export filenames far
more often than they change schemas.

**Test data that grades itself.** The generator records every defect it plants
into `ground_truth.json`, and `verify` re-runs the engine to prove each one was
caught. This is the difference between "the code runs" and "the logic is
right", and it is why the recall number above is a measurement rather than a
claim.

---

## Processors

Four real processors, represented by adapters informed by their public
reconciliation and payment documentation - because the awkwardness is the
point. Two of these are CSV and look nothing alike, which is exactly the
situation a real reconciliation team is in.

This is a challenge implementation, not a certified parser for production
exports. Adyen and Stripe closely follow their documented reconciliation
columns. dLocal and PayU publish relevant payment and reconciliation concepts,
but exact merchant settlement exports can be account-specific; their JSON and
XML fixtures are therefore representative schemas built from those public
concepts. A production rollout would validate each adapter against redacted
files from the merchant account before enabling it.

The fee cards and settlement SLAs are illustrative CafeTerra contract
configuration, not claims about the processors' public pricing or payout terms.

| Processor | Markets | Currencies | Format | SLA | The quirk that breaks naive parsers |
| --- | --- | --- | --- | --- | --- |
| **Adyen** | BR, MX, CO, US | USD, BRL, MXN | CSV | 2d | Fee split across commission / markup / scheme fees / interchange; separate `Gross Debit`/`Gross Credit` columns; settlement currency can differ from capture currency |
| **Stripe** | MX, US | USD, MXN | CSV | 3d | Lowercase currency codes; refund/payout/adjustment rows share the file and must be filtered out; payout row states the true deposit |
| **dLocal** | BR, MX, CO | BRL, MXN, COP, USD | JSON | 5d | Amounts as decimal strings; deduction split into commission plus local tax (IOF/IVA) |
| **PayU Latam** | CO | COP | XML | 7d | Money in element attributes; whole-peso COP; carries both `payuOrderId` and our `reference` - matching on the wrong one is the classic bug |

Public references used for the models:

- [Adyen Settlement details report](https://docs.adyen.com/reporting/settlement-reconciliation/transaction-level/settlement-details-report)
- [Stripe Payout reconciliation report](https://docs.stripe.com/reports/payout-reconciliation)
- [dLocal Order object](https://docs.dlocal.com/reference/the-order-object)
- [PayU Latam Financial Statement](https://developers.payulatam.com/latam/en/payu-module-documentation/reports/financial-statement.html)

Run `make processors` to print the live registry, including every rate card.

### Adding a fifth

```python
# src/reconciler/processors/newbank.py
PROFILE = ProcessorProfile(
    name="NewBank", countries=("PE",), currencies=("PEN",),
    settlement_sla_days=3, file_format="csv",
    fees={"credit_card": FeeRate(Decimal("0.029"), {"PEN": Decimal("0.50")})},
)

class NewBankAdapter:
    profile = PROFILE
    def sniff(self, path): ...   # recognise the file
    def parse(self, path): ...   # return a SettlementBatch
```

Then add it to `ADAPTERS` in `processors/__init__.py`. That is the whole change
- the engine, detectors, reports and dashboard pick it up automatically. A new
currency is one line: `REGISTRY.register(Currency("PEN", 2, "S/"))`.

---

## What it detects

Eleven discrepancy types. The brief asked for five; the other six are breaks
that cost real money and fall out of the same matching pass.

| Type | What it means | Severity |
| --- | --- | --- |
| `missing_settlement` | Captured, past SLA, never paid | High → Critical on materiality |
| `unknown_settlement` | Money arrived for a transaction not in the ledger | High → Critical |
| `duplicate_settlement` | Same transaction paid twice, including across payouts | Critical |
| `amount_mismatch` | Settled gross ≠ amount charged | Medium → Critical |
| `net_arithmetic_error` | gross − fee ≠ net; the file contradicts itself | High → Critical |
| `fee_error` | Fee outside the contracted rate card | Medium → Critical |
| `currency_mismatch` | Settled in a different currency from capture | Critical |
| `status_conflict` | A refunded, charged-back or uncaptured transaction was settled | Critical |
| `batch_total_mismatch` | Header totals disagree with the detail lines | High → Critical |
| `settlement_aging` | Captured, at the edge of SLA, not yet paid | Low |
| `processor_mismatch` | One processor settled another's transaction | High |

Severity escalates to Critical once the money at stake passes a materiality
threshold (default US$250-equivalent), so a 4-cent fee error and a $6,000 one
never sit at the same priority.

---

## Stretch goals

All four in the brief are implemented:

- **Settlement aging** - per-processor SLAs (2–7 days) with a configurable
  grace period; aging and missing are separate findings at different severities.
- **Batch-level validation** - header gross/fees/net checked against the sum of
  the detail lines, for every format that restates them.
- **Configurable tolerance rules** - absolute tolerance expressed in *minor
  units* so it adapts to each currency automatically, plus a relative fee
  allowance and a materiality threshold. `--strict` switches to a zero-tolerance
  audit profile.
- **Interactive workflow** - the deployed page walks through file selection,
  preflight, reconciliation, exceptions, ID-matched transactions, and batches.
  Every exception expands to its expected/actual values, recommended action,
  and exact source row or JSON/XML path. CSV, Markdown, and JSON exports are
  generated in the browser from the same report payload.

Beyond the brief: stable finding IDs for cross-run tracking, a
`--fail-on-discrepancy` exit code for scheduled CI runs, four output formats,
and the ground-truth verification harness.

---

## Testing

```bash
make test     # 38 tests
```

Four layers, matching the four things that can be wrong:

- **Money** - quantisation per currency, refusal to mix currencies, and parsing
  of the localized formats represented in the fixtures (`R$ 1.234,56`, `1,234.56`, `(45.10)`,
  `2.000.000`). Note `10.567` is 10.57 in BRL and 10,567 in COP; the parser
  reads separators against the currency rather than guessing.
- **Adapters** - every writer in `datagen.py` is the exact inverse of its
  adapter, so all four formats are asserted to round-trip byte-for-value.
- **Detectors** - each rule is tested firing when it should *and* staying quiet
  when it should not (a fee one centavo off the rate card is not a finding).
- **Trust boundaries** - adversarial cases cover duplicate and incomplete
  ledgers, fee-label manipulation, multi-payout files, filename changes,
  mixed-currency batches, stable IDs, reconciliation counts, and exposure
  deduplication.

Plus end-to-end: determinism, 100% ground-truth recall, no silent currency
conversion in exposure, and all four renderers producing output.

---

## How I used AI

I used AI as an implementation partner: to turn the brief into a scored design,
scaffold the adapters and fixtures, diagnose round-trip parsing failures, and
draft the reporting layer and dashboard. I kept the product and architecture
decisions explicit: CLI-first for reviewer speed, a canonical model between
files and rules, no implicit currency conversion, and an exception worklist for
finance rather than a generic engineering dashboard.

Generated code was not treated as evidence that the engine worked. I verified
it with 38 unit and integration tests, deterministic ground truth that measures
15/15 injected defects, a fresh-clone run of the documented command, and checks
against the deployed report. I also changed the initial approach after review:
fictional processors became public-doc-informed adapters, three currencies
became a registry with USD and non-two-decimal examples, and the report became
a finance workflow rather than raw output.

---

## What I would do next

With more than two hours, in priority order:

1. **Bank statement as the third leg.** Right now this reconciles ledger against
   settlement reports. The full problem is three-way - ledger, settlement, and
   what actually landed in the bank. A processor can report a payout it never
   sent, and only the bank statement catches that.
2. **Persistence and run-over-run state.** Findings have stable IDs precisely so
   they can be stored, assigned, resolved and suppressed. A schedule, a
   `resolved` flag and a diff between runs turns this from a report into a
   workflow.
3. **FX-aware cross-border settlement.** dLocal genuinely can settle a BRL
   payment in USD by agreement. Today that is flagged as a currency mismatch,
   which is the safe default; the real model is a per-processor contract saying
   which conversions are expected and at what rate tolerance.
4. **Partial refunds and chargebacks as first-class events.** Several
   `amount_mismatch` findings in real data are partial refunds recorded only on
   the processor side. Ingesting refund events would reclassify them
   automatically instead of leaving an analyst to work it out.
5. **Alerting.** `--fail-on-discrepancy` already gives a CI exit code; the next
   step is routing Critical findings to Slack with the recommended action, so
   nobody has to remember to look.

---

## Notes on scope

The brief asked for 300+ transactions and 5 settlement files; this generates
340 across 9 files, 4 processors, 3 formats and 4 currencies. The web workflow
uses the committed deterministic run so a reviewer can inspect the same result
the CLI verifies, while keeping the core engine independent of any web runtime.
