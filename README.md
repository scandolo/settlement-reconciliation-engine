# Settlement Reconciliation Engine

Automated settlement reconciliation for a merchant taking payments through
multiple processors, in multiple currencies, across multiple countries.

It ingests settlement reports in whatever format each processor sends, matches
them line by line against the internal transaction ledger, and produces a
ranked worklist of everything that does not add up — with the money at stake
and the next action attached to each finding.

**Live dashboard:** see the deployment link on the submission.
**Zero dependencies.** Python 3.10+ standard library only, so it runs anywhere
without an install step.

---

## Quick start

```bash
git clone <this-repo> && cd settlement-reconciliation-engine
make demo          # or: PYTHONPATH=src python3 -m reconciler demo
```

That one command generates the test data, reconciles it, and verifies the
result against known ground truth. It takes about a second and needs nothing
installed.

```bash
make test          # 26 tests, standard library unittest
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
  Matched                209  (82.3%)
  Discrepancies          26

  MONEY AT RISK   (positive = owed to us, negative = overpaid to us)
    BRL   R$-1,016.22 BRL
    COP   COL$-1,680,537 COP
    MXN   MX$5,555.89 MXN
    USD   US$2.04 USD
    indicative total  ~US$910.68

  BY TYPE  settlement_aging=8  fee_error=3  processor_mismatch=3  missing_settlement=3
           amount_mismatch=2  unknown_settlement=2  batch_total_mismatch=1
           net_arithmetic_error=1  status_conflict=1  currency_mismatch=1
           duplicate_settlement=1
```

And, from `verify`:

```
  Detected 15/15 injected defects (100% recall)
```

---

## Who this is for, and why it looks like this

The user is a **reconciliation analyst closing the month** — the person
currently spending 60+ hours doing this by hand — and secondarily the **CFO**
who wants one number. Three consequences run through the whole design:

1. **It is an exception tool, not a report.** 212 settlement lines matched
   cleanly; nobody needs to see those. The output is the 26 that did not,
   ordered by money at risk rather than by file order, so the worst thing is
   the first thing.
2. **Every finding carries its next action.** Not an error code — the sentence
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
| `engine.py` | Orchestration only — index the ledger, walk batches, run detectors |
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
subtract or compare across currencies — it raises `CurrencyMismatchError`. The
engine cannot silently mix BRL and USD because the type system will not let it.
Each amount is a `Decimal` quantised to that currency's real ISO exponent, and
that detail matters: **COP has no minor unit**, so any code assuming two
decimal places invents rounding breaks on every Colombian transaction. KWD is
registered too, at three decimal places, to prove the model is not quietly
built around cents.

**We never convert currencies to match.** Converting would manufacture
discrepancies out of FX movement. Amounts are compared only in their original
currency, and exposure is reported per currency. There is exactly one place a
rate appears — ranking severity across currencies, so a large Colombian break
and a large US one sort sensibly in one worklist — and it is labelled
indicative everywhere it surfaces.

**Configuration over code.** Currencies are a registry. Processors are a
registry. Rate cards, SLAs and tolerances are data on a `ProcessorProfile`.
Renegotiating a fee, onboarding a market, or changing a materiality threshold
is a data change, not a deploy of new logic.

**Detectors are small and independent.** Each is a function under fifteen lines
that guards its own preconditions and yields findings. They can run in any
order and none can corrupt another. Adding a rule does not mean touching a
600-line class — it means writing a function and decorating it.

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

Four real processors, modelled on their public report formats — because the
awkwardness is the point. Two of these are CSV and look nothing alike, which is
exactly the situation a real reconciliation team is in.

| Processor | Markets | Currencies | Format | SLA | The quirk that breaks naive parsers |
| --- | --- | --- | --- | --- | --- |
| **Adyen** | BR, MX, CO, US | USD, BRL, MXN | CSV | 2d | Fee split across commission / markup / scheme fees / interchange; separate `Gross Debit`/`Gross Credit` columns; settlement currency can differ from capture currency |
| **Stripe** | MX, US | USD, MXN | CSV | 3d | Lowercase currency codes; refund/payout/adjustment rows share the file and must be filtered out; payout row states the true deposit |
| **dLocal** | BR, MX, CO | BRL, MXN, COP, USD | JSON | 5d | Amounts as decimal strings; deduction split into commission plus local tax (IOF/IVA) |
| **PayU Latam** | CO | COP | XML | 7d | Money in element attributes; whole-peso COP; carries both `payuOrderId` and our `reference` — matching on the wrong one is the classic bug |

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
— the engine, detectors, reports and dashboard pick it up automatically. A new
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

- **Settlement aging** — per-processor SLAs (2–7 days) with a configurable
  grace period; aging and missing are separate findings at different severities.
- **Batch-level validation** — header gross/fees/net checked against the sum of
  the detail lines, for every format that restates them.
- **Configurable tolerance rules** — absolute tolerance expressed in *minor
  units* so it adapts to each currency automatically, plus a relative fee
  allowance and a materiality threshold. `--strict` switches to a zero-tolerance
  audit profile.
- **Dashboard** — the deployed page, plus `make processors` for a CLI view of
  the whole registry.

Beyond the brief: stable finding IDs for cross-run tracking, a
`--fail-on-discrepancy` exit code for scheduled CI runs, four output formats,
and the ground-truth verification harness.

---

## Testing

```bash
make test     # 26 tests
```

Three layers, matching the three things that can be wrong:

- **Money** — quantisation per currency, refusal to mix currencies, and parsing
  of the formats processors really send (`R$ 1.234,56`, `1,234.56`, `(45.10)`,
  `2.000.000`). Note `10.567` is 10.57 in BRL and 10,567 in COP; the parser
  reads separators against the currency rather than guessing.
- **Adapters** — every writer in `datagen.py` is the exact inverse of its
  adapter, so all four formats are asserted to round-trip byte-for-value.
- **Detectors** — each rule is tested firing when it should *and* staying quiet
  when it should not (a fee one centavo off the rate card is not a finding).

Plus end-to-end: determinism, 100% ground-truth recall, no silent currency
conversion in exposure, and all four renderers producing output.

---

## What I would do next

With more than two hours, in priority order:

1. **Bank statement as the third leg.** Right now this reconciles ledger against
   settlement reports. The full problem is three-way — ledger, settlement, and
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
340 across 9 files, 4 processors, 3 formats and 4 currencies. Perfect UI polish
was explicitly not expected, so the dashboard is deliberately plain — the
effort went into the reconciliation logic, the extensibility of the processor
and currency registries, and proving correctness against ground truth.
