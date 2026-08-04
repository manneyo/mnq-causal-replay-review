# Python Discord Follow-up

## Suggested title

Review request: can this streaming session certifier falsely pass an incomplete callback log?

## Suggested post

I previously asked about preserving callback order across rotated NinjaTrader CSV
files. I have now implemented and tested that bounded problem.

Repository: https://github.com/manneyo/mnq-causal-replay-review

Commit reviewed locally: `eb44ac1`

What changed:

- V2 events have persisted identity `(recorder_run_id, record_seq)`.
- Rotated files replay in explicit part and physical row order.
- Content-identical callbacks are preserved; duplicate IDs and sequence gaps fail.
- Bid and ask state update independently without source-time sorting.
- The NinjaTrader recorder now writes a separate control journal containing run
  start/stop, state changes, connection state, and writer errors.
- `session_certification.py` hashes the ordered parts and recorder source, then
  fails closed on gaps, wrong instrument/feed, clock regressions, missing RTH
  boundaries, connection/writer errors, or an unclean stop.
- The public test suite passes 30 tests, including adversarial rotation, restart,
  sequence, quote-state, certificate-tampering, and bridge-safety cases.

Expected behavior:

A session certificate must fail whenever the event stream cannot prove continuous,
ordered, correctly connected MNQ RTH observation. It should never repair or infer a
missing callback.

Actual behavior:

All included synthetic and adversarial fixtures now pass or fail as expected. A real
Rithmic V2 capture has not yet been certified, so I am not claiming production data
completeness or a trading edge.

My focused review question:

Can you identify a concrete event ordering, file rotation, restart, writer failure,
or connection transition where `src/video_trader/data/session_certification.py`
would issue a passing certificate even though the callback stream is incomplete?

The most relevant files are:

- `src/video_trader/data/ninjatrader_events.py`
- `src/video_trader/data/session_certification.py`
- `ninjatrader/CodexResearchDataRecorder.cs`
- `tests/test_streaming_replay.py`
- `tests/test_session_certification.py`

This is a data-integrity/code-review question only. Live trading is disabled, raw
provider data and credentials are not in the repository, and profitability is not
being presented as an expected result.
