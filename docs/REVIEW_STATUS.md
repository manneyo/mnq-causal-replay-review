# Review Status

This page separates solved engineering defects from evidence that does not yet
exist. It is intentionally not a profitability claim.

## Resolved and tested

- **VERIFIED:** V2 files replay in caller-supplied part order and physical row
  order. Source timestamps never reorder callbacks.
- **VERIFIED:** `recorder_run_id + record_seq` is the persisted event identity.
  Content-identical callbacks are preserved; duplicate IDs and sequence gaps fail.
- **VERIFIED:** Bid and ask update independently. A quote becomes executable only
  after both sides exist and `ask > bid`.
- **VERIFIED:** The recorder writes event IDs before persistence and emits a
  separate control journal for run lifecycle, connection state, and writer errors.
- **VERIFIED:** The session certifier binds ordered file hashes, recorder-source
  hash, exact RTH boundaries, feed identity, receive-clock continuity, and a clean
  stop into one deterministic certificate.
- **VERIFIED:** The Sim101 bridge starts disarmed, listens only on loopback, limits
  entry size, rejects another active order, and exposes account-position,
  working-order, client-state, and event-range reconciliation endpoints.
- **VERIFIED:** `EXIT` and `FLAT` are allowed as Sim-account risk-reduction commands
  while new entries are disarmed. `FLAT` uses account-level instrument flattening.
- **VERIFIED:** All public synthetic contract tests pass.

## Still missing

- **MISSING:** A successful NinjaTrader compile of the current recorder and bridge
  in the operator's installed NinjaTrader version.
- **MISSING:** A fresh certified MNQ Rithmic RTH capture made by the current recorder.
- **MISSING:** At least 60 complete, correctly rolled, certified MNQ top-of-book RTH
  sessions from the same feed family intended for Sim101.
- **MISSING:** A frozen Candidate 1 versus matched-frequency random comparison on
  those certified sessions, followed by untouched Candidate 2/3 evaluation only if
  the predeclared gate permits it.
- **MISSING:** Positive after-cost historical durability under the predeclared fee,
  slippage, latency, bootstrap, concentration, and family-wise testing gates.
- **MISSING:** Forty untouched Sim101 RTH sessions and at least 100 fully reconciled
  closed trades from one frozen candidate.

## Current verdict

- Data-loader causality: **PASS on synthetic/adversarial tests**
- Recorder evidence contract: **PASS on source and synthetic tests; real capture pending**
- Historical durability after costs: **NOT ESTABLISHED**
- Sim101 readiness: **FAIL until real capture, compile, and historical gates pass**
- Live-trading readiness: **OUT OF SCOPE and locked**
- Similarity to the video's visible mechanics: **IMPLEMENTABLE, not yet validated as an edge**

The next useful external review is narrow: challenge the V2 recorder/control-journal
contract and `session_certification.py` for any way a missing, duplicated, reordered,
or disconnected callback could still receive a passing certificate.
