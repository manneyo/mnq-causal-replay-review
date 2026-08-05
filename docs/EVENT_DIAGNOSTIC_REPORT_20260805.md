# MNQ Event Replay Diagnostic - 2026-08-05

## Status

`IN_SAMPLE_DIAGNOSTIC_ONLY`

This replay describes mechanics on one development session. It is not an
untouched holdout, does not establish expectancy, and cannot support live or
Sim101 promotion.

## Causal Contract

- Processing order: persisted recorder sequence and local receive timestamp.
- Market state: 250 ms top-of-book snapshots built without future events.
- Decision clocks: 250 ms, 1 second, 5 seconds, and 10 seconds.
- Entry: first later 250 ms quote snapshot after the signal.
- Exit: barrier/timeout trigger first, then the following quote snapshot.
- Position rule: one position at a time; later signals are ignored while active.
- Directions: sequential long and short trades are permitted.
- Costs: three adverse ticks per fill plus $1.90 round trip.
- Fixed modeled cost barrier: 9.8 MNQ ticks before the 2-tick safety margin,
  excluding the observed bid/ask spread already present in raw quote P&L.
- Stops/targets: 16/24 ticks; maximum holds of 15, 60, and 180 seconds.

## Data Audit

- Source events processed through the RTH end: 5,185,423.
- Real-time events in the context/RTH window: 4,270,593.
- Valid 250 ms quote buckets: 100,375.
- Maximum observed quote-bucket gap: 2.267 seconds.
- Source-clock regressions: 231,476; receive order was used instead.
- Locked/crossed quote events excluded: 176,738.
- Event identities and rotation boundaries were continuous through the replay.
- Every regenerated trade now records the signal bid/ask and source sequence,
  intended next-quote sequence, trigger sequence, modeled fill, session label,
  fee, fill stress, and the complete observable quote-bucket state.
- The recorder remained active after the replay; source hashes identify the
  exact byte prefixes captured by this artifact, not later appended bytes.

## Results

All 48 tested configurations were negative after the declared costs.

| Arm | Descriptively best clock/hold | Trades | Net after costs | Profit factor |
|---|---:|---:|---:|---:|
| Candidate 3 | 10 s / 180 s | 7 | -$37.30 | 0.389 |
| Candidate 2 | 10 s / 15 s | 203 | -$1,374.20 | 0.207 |
| Candidate 1 | 10 s / 180 s | 291 | -$1,702.90 | 0.343 |
| Matched random | 5 s / 180 s | 296 | -$1,379.40 | 0.440 |

The best raw-quote result before the 9.8-tick cost burden was matched random at
+$71.00. Candidate 3 was -$3.00 before costs; Candidates 2 and 1 were -$92.50
and -$277.00 respectively at their best raw-quote configurations. The tested
rules therefore did not merely lose because of fees and fill stress.

An independent recalculation from the regenerated ledger found that the
descriptively best after-cost configurations had positive raw movement on only
30.9% of Candidate 1 trades, 32.0% of Candidate 2 trades, and 28.6% of
Candidate 3 trades. The external claim that direction was often correct but
entry was merely late is therefore not supported by this ledger.

The median observed one-minute mid-price range was 67 ticks (interquartile
range 45-106 ticks). The fixed 9.8-tick burden was economically material, but
it was not large relative to the day's typical one-minute range. This further
supports attributing the failure primarily to the tested rules rather than to
cost modeling alone.

## Video Mechanics

The high-frequency variants can reproduce visible activity but not positive
expectancy:

| Arm at 250 ms / 180 s | Trades | Direction reversals | Maximum entries in one minute |
|---|---:|---:|---:|
| Candidate 1 | 765 | 343 | 12 |
| Candidate 2 | 620 | 341 | 13 |
| Candidate 3 | 42 | 0 | 6 |
| Matched random | 536 | 270 | 11 |

## Verdict

- Causal replay mechanics: **PASS for this diagnostic**.
- Bid/ask next-observation fills: **PASS at 250 ms sampling resolution**.
- Video-like marker frequency and reversals: **PASS for Candidates 1 and 2**.
- Positive expectancy after costs: **FAIL**.
- Candidate 1 versus matched random: **FAIL**.
- Historical durability: **NOT TESTED; one development session only**.
- Sim101 readiness: **FAIL**.
- Live readiness: **FAIL and locked**.

The forward evidence journal now seals each decision before its intent, records
the complete model input vector, and labels completed-bar observations
explicitly as lacking top-of-book evidence. The promotion audit now fails if
decisions are missing, unlinked, duplicated, ordered after an intent, or lack
a matching later top-of-book quote before the intent and fill. These changes
improve auditability; they do not improve or establish expectancy.

The next defensible action is to freeze these results, collect additional full
sessions, and test a new predeclared hypothesis on later sessions. Lowering the
threshold or choosing a setting because it looked best today would overfit this
development day.
