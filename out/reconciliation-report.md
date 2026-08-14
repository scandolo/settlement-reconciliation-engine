# Settlement reconciliation — as of 2026-08-14

_Generated 2026-08-14T14:10:20+00:00_

## Summary

| Metric | Value |
| --- | --- |
| Batches processed | 9 |
| Settlement lines | 212 |
| Ledger transactions | 340 |
| Expecting settlement | 254 |
| Matched | 208 |
| Unmatched | 46 |
| Match rate | 81.89% |
| Discrepancies | 26 |

## Settled volume

| Processor · currency | Lines | Gross | Fees | Net |
| --- | ---: | ---: | ---: | ---: |
| Adyen · BRL | 20 | R$24,735.32 BRL | R$192.11 BRL | R$24,521.01 BRL |
| Adyen · MXN | 21 | MX$88,870.09 MXN | MX$516.24 MXN | MX$88,353.85 MXN |
| Adyen · USD | 22 | US$5,604.92 USD | US$31.51 USD | US$5,573.41 USD |
| PayU Latam · COP | 21 | COL$19,264,387 COP | COL$597,521 COP | COL$18,666,866 COP |
| Stripe · MXN | 20 | MX$106,786.09 MXN | MX$3,827.57 MXN | MX$102,958.52 MXN |
| Stripe · USD | 30 | US$7,246.30 USD | US$240.20 USD | US$7,006.10 USD |
| dLocal · BRL | 38 | R$53,161.67 BRL | R$1,787.28 BRL | R$51,374.39 BRL |
| dLocal · COP | 26 | COL$29,197,199 COP | COL$1,180,424 COP | COL$28,016,775 COP |
| dLocal · MXN | 13 | MX$67,878.75 MXN | MX$2,298.61 MXN | MX$65,580.14 MXN |
| dLocal · USD | 1 | US$5,065.47 USD | US$130.64 USD | US$4,934.83 USD |

## Money at risk

Positive means CafeTerra is owed money; negative means CafeTerra was overpaid and should expect a clawback. Amounts are never converted for matching — the USD figure below is indicative, for ranking only.

| Currency | Net exposure |
| --- | ---: |
| BRL | R$-1,016.22 BRL |
| COP | COL$-1,680,537 COP |
| MXN | MX$5,555.89 MXN |
| USD | US$2.04 USD |
| **Indicative total** | **~US$910.68** |

## Discrepancies by type

| Type | Count |
| --- | ---: |
| `settlement_aging` | 8 |
| `fee_error` | 3 |
| `processor_mismatch` | 3 |
| `missing_settlement` | 3 |
| `amount_mismatch` | 2 |
| `unknown_settlement` | 2 |
| `batch_total_mismatch` | 1 |
| `net_arithmetic_error` | 1 |
| `status_conflict` | 1 |
| `currency_mismatch` | 1 |
| `duplicate_settlement` | 1 |

## Worklist

Ordered by severity, then by money at risk.

| # | ID | Severity | Type | Processor | Transaction | Impact | Issue |
| ---: | --- | --- | --- | --- | --- | ---: | --- |
| 1 | `DUPL-8f5f090a` | critical | `duplicate_settlement` | Adyen | CT-202608-00302 | MX$-7,793.97 MXN | CT-202608-00302 was settled 2 times, across batches ADY-MXN-20260813, po-MXN-20260813. |
| 2 | `UNKN-141cd736` | critical | `unknown_settlement` | Adyen | CT-RSV-9002 | R$-2,130.26 BRL | Adyen settled R$2,130.26 BRL for CT-RSV-9002, which does not exist in the internal ledger. |
| 3 | `MISS-6f40b584` | critical | `missing_settlement` | dLocal | CT-202608-00216 | MX$6,646.73 MXN | CT-202608-00216 was captured on 2026-08-05 for MX$6,646.73 MXN and has never been settled. Due 2026-08-10 under the 5-day dLocal SLA -- 4 days overdue. |
| 4 | `CURR-e96b7e1e` | critical | `currency_mismatch` | dLocal | CT-202608-00047 | MX$5,065.47 MXN | CT-202608-00047 was charged in MXN (MX$5,065.47 MXN) but settled in USD (US$5,065.47 USD). |
| 5 | `UNKN-1216b83c` | critical | `unknown_settlement` | dLocal | CT-ADJ-9001 | COL$-1,040,417 COP | dLocal settled COL$1,040,417 COP for CT-ADJ-9001, which does not exist in the internal ledger. |
| 6 | `STAT-1717a39b` | critical | `status_conflict` | dLocal | CT-202608-00008 | R$-829.90 BRL | CT-202608-00008 is 'refunded' internally but was settled for R$829.90 BRL. |
| 7 | `MISS-667d6dc1` | high | `missing_settlement` | dLocal | CT-202608-00096 | R$1,206.73 BRL | CT-202608-00096 was captured on 2026-07-21 for R$1,206.73 BRL and has never been settled. Due 2026-07-26 under the 5-day dLocal SLA -- 19 days overdue. |
| 8 | `BATC-c5ffaa3f` | high | `batch_total_mismatch` | PayU Latam | — | COL$-746,675 COP | Batch PAYU-COP-20260811 claims net of COL$19,413,541 COP, but its 21 lines sum to COL$18,666,866 COP. |
| 9 | `MISS-86040d33` | high | `missing_settlement` | dLocal | CT-202608-00270 | MX$1,830.53 MXN | CT-202608-00270 was captured on 2026-07-24 for MX$1,830.53 MXN and has never been settled. Due 2026-07-29 under the 5-day dLocal SLA -- 16 days overdue. |
| 10 | `NET_-86c9bfe2` | high | `net_arithmetic_error` | Adyen | CT-202608-00226 | R$22.20 BRL | Settlement line does not add up: gross R$319.73 BRL minus fee R$2.52 BRL is R$317.21 BRL, but the report states net R$295.01 BRL. |
| 11 | `PROC-e94bedd1` | high | `processor_mismatch` | dLocal | CT-202608-00008 | — | dLocal settled CT-202608-00008, but the ledger routed it through Adyen. |
| 12 | `PROC-3b6e640f` | high | `processor_mismatch` | dLocal | CT-202608-00004 | — | dLocal settled CT-202608-00004, but the ledger routed it through PayU Latam. |
| 13 | `PROC-a53354ff` | high | `processor_mismatch` | Stripe | CT-202608-00302 | — | Stripe settled CT-202608-00302, but the ledger routed it through Adyen. |
| 14 | `AMOU-884e40fc` | medium | `amount_mismatch` | Adyen | CT-202608-00264 | R$700.63 BRL | CT-202608-00264 was charged R$1,401.27 BRL but settled with gross R$700.64 BRL -- short by R$700.63 BRL. |
| 15 | `FEE_-a1cdf1cb` | medium | `fee_error` | dLocal | CT-202608-00243 | COL$106,555 COP | Fee on CT-202608-00243 (credit_card) was COL$173,152 COP, but the contracted rate implies COL$66,597 COP -- overcharged by COL$106,555 COP. |
| 16 | `FEE_-e7d2de32` | medium | `fee_error` | Stripe | CT-202608-00302 | MX$-192.87 MXN | Fee on CT-202608-00302 (debit_card) was MX$37.24 MXN, but the contracted rate implies MX$230.11 MXN -- undercharged by MX$192.87 MXN. |
| 17 | `FEE_-9f2f0aa8` | medium | `fee_error` | Adyen | CT-202608-00046 | R$14.38 BRL | Fee on CT-202608-00046 (pix) was R$30.36 BRL, but the contracted rate implies R$15.98 BRL -- overcharged by R$14.38 BRL. |
| 18 | `AMOU-c8253abf` | medium | `amount_mismatch` | Stripe | CT-202608-00090 | US$2.04 USD | CT-202608-00090 was charged US$11.32 USD but settled with gross US$9.28 USD -- short by US$2.04 USD. |
| 19 | `SETT-586eb6d6` | low | `settlement_aging` | Adyen | CT-202608-00006 | — | CT-202608-00006 (US$41.42 USD) has reached the edge of Adyen's 2-day SLA, due 2026-08-14. |
| 20 | `SETT-5e5d58bb` | low | `settlement_aging` | Adyen | CT-202608-00084 | — | CT-202608-00084 (US$385.92 USD) has reached the edge of Adyen's 2-day SLA, due 2026-08-14. |
| 21 | `SETT-07017cd1` | low | `settlement_aging` | dLocal | CT-202608-00097 | — | CT-202608-00097 (COL$997,848 COP) has reached the edge of dLocal's 5-day SLA, due 2026-08-14. |
| 22 | `SETT-63766b15` | low | `settlement_aging` | Adyen | CT-202608-00109 | — | CT-202608-00109 (US$368.82 USD) has reached the edge of Adyen's 2-day SLA, due 2026-08-14. |
| 23 | `SETT-638a8c5d` | low | `settlement_aging` | Adyen | CT-202608-00136 | — | CT-202608-00136 (R$48.44 BRL) has reached the edge of Adyen's 2-day SLA, due 2026-08-14. |
| 24 | `SETT-0d7adbad` | low | `settlement_aging` | Adyen | CT-202608-00142 | — | CT-202608-00142 (R$979.74 BRL) has reached the edge of Adyen's 2-day SLA, due 2026-08-14. |
| 25 | `SETT-b7fcc7ee` | low | `settlement_aging` | Adyen | CT-202608-00165 | — | CT-202608-00165 (US$96.51 USD) has reached the edge of Adyen's 2-day SLA, due 2026-08-14. |
| 26 | `SETT-9ddc112b` | low | `settlement_aging` | Stripe | CT-202608-00333 | — | CT-202608-00333 (US$351.45 USD) has reached the edge of Stripe's 3-day SLA, due 2026-08-14. |

## Recommended actions

- **`DUPL-8f5f090a`** (critical) — Freeze the duplicated amount before month-end close. Duplicates are reversed later and will otherwise open a hole in the next payout.
- **`UNKN-141cd736`** (critical) — Do not recognise as revenue yet. Confirm whether this is a reserve release, refund reversal or adjustment, and book it accordingly.
- **`MISS-6f40b584`** (critical) — Raise a missing-settlement enquiry with dLocal quoting the capture date. If it was refunded processor-side, correct the internal status instead.
- **`CURR-e96b7e1e`** (critical) — Escalate today. The deposit will not clear against a MXN receivable; request reversal and re-settlement in the original currency.
- **`UNKN-1216b83c`** (critical) — Do not recognise as revenue yet. Confirm whether this is a reserve release, refund reversal or adjustment, and book it accordingly.
- **`STAT-1717a39b`** (critical) — Quarantine the funds. Settled reversals are normally clawed back, so recognising this now creates a shortfall in a later payout.
- **`MISS-667d6dc1`** (high) — Raise a missing-settlement enquiry with dLocal quoting the capture date. If it was refunded processor-side, correct the internal status instead.
- **`BATC-c5ffaa3f`** (high) — Do not post this payout until the processor reissues the file -- header and detail disagree, so one of them is wrong.
- **`MISS-86040d33`** (high) — Raise a missing-settlement enquiry with dLocal quoting the capture date. If it was refunded processor-side, correct the internal status instead.
- **`NET_-86c9bfe2`** (high) — Hold the line and request a corrected settlement advice -- the file is internally inconsistent, so neither figure can be trusted.
- **`PROC-e94bedd1`** (high) — Check for a routing failover or a mis-keyed merchant reference before accepting the payout, and confirm the other processor is not also settling it.
- **`PROC-3b6e640f`** (high) — Check for a routing failover or a mis-keyed merchant reference before accepting the payout, and confirm the other processor is not also settling it.
- **`PROC-a53354ff`** (high) — Check for a routing failover or a mis-keyed merchant reference before accepting the payout, and confirm the other processor is not also settling it.
- **`AMOU-884e40fc`** (medium) — Check for a partial refund or partial capture recorded only on the processor side; if there is none, open a settlement dispute.
- **`FEE_-a1cdf1cb`** (medium) — Reclaim the difference and re-check the credit_card rate configured for dLocal. A systematic error here repeats on every transaction.
- **`FEE_-e7d2de32`** (medium) — Reclaim the difference and re-check the debit_card rate configured for Stripe. A systematic error here repeats on every transaction.
- **`FEE_-9f2f0aa8`** (medium) — Reclaim the difference and re-check the pix rate configured for Adyen. A systematic error here repeats on every transaction.
- **`AMOU-c8253abf`** (medium) — Check for a partial refund or partial capture recorded only on the processor side; if there is none, open a settlement dispute.
- **`SETT-586eb6d6`** (low) — Monitor; escalate if the next payout omits it.
- **`SETT-5e5d58bb`** (low) — Monitor; escalate if the next payout omits it.
- **`SETT-07017cd1`** (low) — Monitor; escalate if the next payout omits it.
- **`SETT-63766b15`** (low) — Monitor; escalate if the next payout omits it.
- **`SETT-638a8c5d`** (low) — Monitor; escalate if the next payout omits it.
- **`SETT-0d7adbad`** (low) — Monitor; escalate if the next payout omits it.
- **`SETT-b7fcc7ee`** (low) — Monitor; escalate if the next payout omits it.
- **`SETT-9ddc112b`** (low) — Monitor; escalate if the next payout omits it.

