# MNQ Causal Replay Review

Minimal, sanitized review repository for deterministic replay of NinjaTrader
MNQ Level 1 event exports.

This repository asks one bounded engineering question:

> How should rotated bid, ask, and trade CSV files be streamed without loading
> the full dataset into memory while preserving callback order and producing an
> auditable top-of-book state for next-quote execution research?

The project does not claim profitability. It does not include live credentials,
broker accounts, proprietary raw market data, or an enabled trading system.

## Current problem

The legacy loader in
`src/video_trader/data/ninjatrader_events.py` currently:

1. Loads every CSV into pandas memory.
2. Removes content-identical rows.
3. Sorts all rows by source timestamp.

Those operations are convenient for analysis but unsafe for causal replay.
Repeated quote callbacks may be legitimate, and source timestamps can regress at
file rotations even though callback/file order remains causal.

Two expected-failure tests document these known gaps. The review objective is to
replace the legacy behavior with a streaming, deterministic validator and replay
contract without hiding or weakening those tests.

See [PROBLEM_STATEMENT.md](PROBLEM_STATEMENT.md) for the exact acceptance criteria.

## Quick start

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\inspect_sample.py samples\synthetic_mnq_events.csv
```

macOS or Linux:

```bash
python3.11 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/python -m pytest -q
./.venv/bin/python scripts/inspect_sample.py samples/synthetic_mnq_events.csv
```

The suite should pass while reporting two intentional `XFAIL` results. Those
tests become normal passing tests when the causal loader is repaired.

## Repository map

```text
src/video_trader/data/ninjatrader_events.py  Current legacy loader and quote proxy
tests/test_known_causality_gaps.py           Reproductions of the known problems
samples/synthetic_mnq_events.csv             Synthetic, non-provider test fixture
scripts/inspect_sample.py                    Constant-memory diagnostic scanner
ninjatrader/CodexResearchDataRecorder.cs     Current recorder schema context
ninjatrader/IntrabarPredictionBridge.cs      Disarmed bridge protocol context
docs/DATA_SCHEMA.md                          Legacy and proposed event contracts
docs/VIDEO_MECHANICS.md                      Observable mechanics of the reference
docs/COLLECTION_REQUIREMENTS.md              Data and evidence gates
```

## Review requested

Reviewers are specifically asked to examine:

- How to assign stable event IDs across rotated files.
- How to preserve callback order when source timestamps regress.
- How to distinguish legitimate repeated callbacks from duplicate writes.
- How to reconstruct bid and ask state without inventing simultaneous quotes.
- How to certify complete RTH sessions and detect recorder discontinuities.
- How to expose a streaming iterator while keeping the existing pandas adapter.

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

