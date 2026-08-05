# Response to Adversarial Quantitative Audit

Date: 2026-08-05

## Scope

This response checks the external audit against the local source code and the
raw August 5 diagnostic artifacts. August 5 remains a development session. No
result below is holdout evidence, a profitability claim, or authorization for
live trading.

## Accepted Findings

- **VERIFIED:** Event-level clocks can reproduce multiple signals and direction
  changes inside a one-minute display candle.
- **VERIFIED:** All 48 August 5 configurations were negative after the declared
  fill stress and fees.
- **VERIFIED:** Candidate 1 failed to beat the matched-frequency random arm.
- **VERIFIED:** At least 60 certified MNQ RTH top-of-book sessions from the
  intended feed family do not yet exist.
- **VERIFIED:** The faster clocks created severe churn.
- **VERIFIED:** The forward journal did not previously contain a complete model
  feature vector or explicit top-of-book evidence fields.
- **VERIFIED:** Positive after-cost expectancy and durability are not
  established.

## Corrections to the External Audit

- **CORRECTED:** The complete local `trade_ledger.csv`,
  `quote_buckets_250ms.parquet`, `diagnostic_summary.json`, and `metrics.csv`
  exist. They were missing from the external review package, not from the local
  research workspace.
- **CORRECTED:** Historical and Sim101 session grouping both use the 18:00 ET
  futures rollover. One historical code path duplicated the same `+6 hour`
  calculation; it now calls the shared `trading_session_date` helper.
- **NOT SUPPORTED:** "Direction was often correct but entry was late." On the
  descriptively best after-cost settings, positive raw movement occurred on
  30.9% of Candidate 1 trades, 32.0% of Candidate 2 trades, and 28.6% of
  Candidate 3 trades. The tested rules were directionally weak before fixed
  cost deductions.
- **CORRECTED:** The fixed 9.8-tick burden excludes the observed bid/ask spread,
  which is already embedded in raw quote P&L. It is material, but the session's
  median one-minute mid-price range was 67 ticks, with a 45-106 tick
  interquartile range. Costs alone do not explain the failure.
- **AMBIGUOUS:** Exact source provenance remains labeled `Simulation` by the
  recorder. Top-of-book events are real and sequence-audited, but the evidence
  does not yet prove the same Rithmic feed family required for eventual Sim101
  comparison.

## Implemented Remediation

1. Journal schema advanced to version 3.
2. Every forward prediction is now hash-sealed before any linked order intent.
3. Every prediction records a deterministic decision ID, futures session date,
   complete model feature vector, signal observation, evidence tier, signal
   bid/ask fields, and intended next-quote event ID field.
4. Completed-bar observations are explicitly labeled `COMPLETED_BAR_ONLY` and
   carry null quote evidence. They cannot masquerade as executable top-of-book
   evidence.
5. Sim101 intents must reference a decision already present in the durable
   journal.
6. A top-of-book decision must be followed by a journaled `NEXT_QUOTE` whose
   event ID matches the decision, whose timestamp is later than the signal, and
   whose bid/ask is valid. The linked intent and fill must occur after it.
7. The Sim101 promotion audit now fails on missing, malformed, duplicate, or
   post-intent decisions; unlinked or repeated intents; missing RTH decisions;
   mismatched next quotes; and fills preceding the declared next quote.
8. The event trade ledger now records signal and trigger source sequences,
   signal bid/ask, intended next-quote sequences, quote delays, observable state,
   session date, fill stress, fees, and safety margin.
9. Historical daily grouping now uses the same shared futures session helper as
   the Sim101 audit.

These are evidence-quality improvements. They do not change the strategy rules
or improve the August 5 P&L.

## Independent Regeneration

- 100,375 valid 250 ms quote buckets.
- 16,088 replayed trades across 48 configurations.
- 48 of 48 configurations negative after modeled costs.
- Regenerated net P&L matches `metrics.csv` within `4.55e-13` dollars.
- Every entry timestamp is later than its signal timestamp.
- Every exit timestamp is later than its trigger timestamp.
- Every intended entry/exit next-quote sequence matches the sequence actually
  used by the replay.
- Every captured source-prefix hash verifies.

## Remaining Blocking Work

1. Disable the August 5 recorder cleanly so its control journal receives
   `RUN_STOP`; the current run is not a certified complete session.
2. Confirm and record exact feed provenance. `Simulation` is insufficient as a
   same-feed-family assertion.
3. Collect at least 60 complete, certified RTH MNQ top-of-book sessions.
4. Predeclare the next candidate before seeing its evaluation sessions. Do not
   retune Candidate 1, thresholds, holds, or costs on August 5.
5. Build the event-level Sim101 runner. The current forward runner is
   completed-bar only and therefore cannot pass the new top-of-book evidence
   gate.
6. Run anchored chronological folds with matched random, +1/+2 adverse-tick
   stress, session-block bootstrap, concentration limits, and family-wise
   multiple-testing correction.
7. Only after a historical pass, freeze one candidate and collect at least 40
   untouched Sim101 RTH sessions and 100 fully reconciled closed trades.

## Verdict

| Axis | Verdict |
|---|---|
| Code-level causal ordering | PASS after remediation |
| August 5 replay integrity | PASS for development diagnostics |
| Data sufficiency | FAIL |
| Historical durability after costs | FAIL / not established |
| Event-level execution readiness | FAIL |
| Sim101 promotion readiness | FAIL |
| Video-mechanics similarity | PASS for Candidate 1/2 activity only |
| Positive expectancy | FAIL on tested rules; unknown for untested hypotheses |
| Live readiness | FAIL and locked |

The defensible conclusion is narrow: the system can reproduce the video's
visible intrabar activity causally at 250 ms resolution, but the tested rules do
not possess positive expectancy. The next source of progress is better
predeclared signal hypotheses tested on new certified sessions, not more trades
or looser cost assumptions.
