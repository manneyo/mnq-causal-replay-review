# August 5 Evidence-Code Snapshot

This directory is a read-only review snapshot of the local evidence and replay
modules after the August 5 adversarial audit. It is not a second installable
package and is intentionally isolated from the repository's runnable synthetic
loader tests.

Review targets:

1. `src/video_trader/execution/evidence.py`: schema-v3 decision-before-intent
   and signal-before-next-quote-before-intent contract.
2. `src/video_trader/sim101_audit.py`: fail-closed promotion checks.
3. `src/video_trader/event_diagnostic_replay.py`: next-observation bid/ask replay
   and enriched trade ledger.
4. `src/video_trader/candle_state.py`: causal Candidate 1/2/3 rules.
5. The copied tests: regressions for ordering, evidence tier, duplicate identity,
   and later-observation fills.

Licensed raw market events, quote parquet, and the full price-bearing trade
ledger are not public. Aggregate metrics and a sanitized manifest are available
under `samples/` and `docs/` in the repository root.

Nothing in this snapshot authorizes live trading or establishes positive
expectancy.
