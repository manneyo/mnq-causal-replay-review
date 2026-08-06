# MNQ Causal Replay Review

Minimal, sanitized review repository for deterministic replay of NinjaTrader
MNQ Level 1 event exports.

This repository asks one bounded engineering question:

> How should rotated bid, ask, and trade CSV files be streamed without loading
> the full dataset into memory while preserving callback order and producing an
> auditable top-of-book state for next-quote execution research?

The project does not claim profitability. It does not include live credentials,
broker accounts, proprietary raw market data, or an enabled trading system.

## Implemented contract

The original loader:

1. Loads every CSV into pandas memory.
2. Removes content-identical rows.
3. Sorts all rows by source timestamp.

Those operations were unsafe for causal replay. The replacement now:

- Streams CSV rows in caller-supplied manifest order and physical row order.
- Preserves content-identical callbacks.
- Uses persisted `recorder_run_id + record_seq` identity for v2 files.
- Rejects duplicate v2 IDs, sequence gaps, missing parts, and malformed rows.
- Gives legacy rows deterministic positional IDs without claiming idempotency.
- Reconstructs bid and ask independently after each observed callback.
- Reports source-time regressions without reordering events.
- Retains a pandas compatibility adapter over the streaming iterator.
- Validates run-wide identity and rotation while applying coverage, receive-gap,
  and connection-failure gates to the explicitly declared session window.

See [PROBLEM_STATEMENT.md](PROBLEM_STATEMENT.md) for the exact acceptance criteria.

## Quick start

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\inspect_sample.py samples\synthetic_mnq_events.csv
.\.venv\Scripts\python.exe scripts\benchmark_streaming.py --rows 100000
```

macOS or Linux:

```bash
python3.11 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/python -m pytest -q
./.venv/bin/python scripts/inspect_sample.py samples/synthetic_mnq_events.csv
./.venv/bin/python scripts/benchmark_streaming.py --rows 100000
```

All tests, including the two former expected failures, must pass normally.

After a v2 event run with v3 controls has closed cleanly, generate its
deterministic evidence certificate:

```powershell
.\.venv\Scripts\python.exe scripts\certify_session.py RUN_events.csv `
  --controls RUN_controls.csv `
  --instrument "MNQ 09-26" `
  --connection "EXACT NINJATRADER CONNECTION NAME" `
  --provider "Rithmic" `
  --feed-family "Rithmic" `
  --session-start 2026-08-04T13:30:00Z `
  --session-end 2026-08-04T20:00:00Z `
  --recorder-source ninjatrader\CodexResearchDataRecorder.cs `
  --output session_certificate.json
```

Use the exact connection name written by NinjaTrader. A failing certificate is
retained as audit evidence and is not eligible for strategy evaluation.
The full installation, collection, certification, bridge-comparison, and
60-session procedure is in
[docs/SESSION_COLLECTION_RUNBOOK.md](docs/SESSION_COLLECTION_RUNBOOK.md).

## Repository map

```text
src/video_trader/data/ninjatrader_events.py  Streaming reader, validator and adapter
tests/test_known_causality_gaps.py           Regressions for the original defects
tests/test_streaming_replay.py               Identity and rotation invariants
samples/synthetic_mnq_events.csv             Synthetic, non-provider test fixture
scripts/inspect_sample.py                    Constant-memory diagnostic scanner
scripts/benchmark_streaming.py               Synthetic streaming memory benchmark
scripts/certify_session.py                   Deterministic fail-closed certificate
scripts/check_bridge_provenance.py           Read-only recorder/bridge identity check
scripts/audit_certificate_inventory.py       Sixty-session consistency gate
scripts/locate_receive_gaps.py               Run-wide versus session gap diagnostic
ninjatrader/CodexResearchDataRecorder.cs     V2 source event-identity contract
ninjatrader/IntrabarPredictionBridge.cs      Disarmed bridge protocol context
docs/DATA_SCHEMA.md                          Legacy and proposed event contracts
docs/VIDEO_MECHANICS.md                      Observable mechanics of the reference
docs/COLLECTION_REQUIREMENTS.md              Data and evidence gates
docs/REVIEW_STATUS.md                        Solved defects and remaining evidence
docs/PYTHON_DISCORD_FOLLOWUP.md              Bounded external-review request
docs/EXTERNAL_AUDIT_RESPONSE_20260805.md      Verified response to the external audit
docs/EVENT_DIAGNOSTIC_REPORT_20260805.md      One-session development diagnostic
samples/august5_diagnostic_metrics.csv        Aggregate results; no provider events
audit_snapshot/20260805/                      Read-only snapshot of remediated evidence code
```

## Review requested

Reviewers are specifically asked to challenge:

- Whether `recorder_run_id + record_seq` is sufficient persisted identity.
- Whether rotation and restart failures are rejected tightly enough.
- Whether legacy positional identity is labeled conservatively enough.
- How to reconstruct bid and ask state without inventing simultaneous quotes.
- How to certify complete RTH sessions and detect recorder discontinuities.
- How to expose a streaming iterator while keeping the existing pandas adapter.

The current solved/missing matrix is maintained in
[docs/REVIEW_STATUS.md](docs/REVIEW_STATUS.md). The next bounded review target is
whether the schema-v3 decision journal and Sim101 promotion audit can incorrectly
accept a missing, post-intent, unlinked, or non-top-of-book decision. The August 5
audit response and aggregate metrics are included, but that session remains
development-only and all tested configurations were negative after costs.

The video referenced in the project is not committed. It can be shared separately
when redistribution is permitted. The code review must remain reproducible without
the video.

## Safety and scope

- Research and Sim101 paper-trading context only.
- One position at a time in the future baseline.
- Signal first; fill only on a later observable quote.
- Live trading remains locked and is not part of this repository.
- Do not submit API keys, account identifiers, or provider market data in issues.

## Data

The included CSV is synthetic and exists only to reproduce ordering behavior. Raw
Rithmic and Databento data are deliberately excluded. Contributors must not upload
licensed provider data without explicit redistribution permission.

## Contributing

Start with the help-wanted issue template or read
[CONTRIBUTING.md](CONTRIBUTING.md). Pull requests should include tests that prove
ordering and quote-state invariants.
