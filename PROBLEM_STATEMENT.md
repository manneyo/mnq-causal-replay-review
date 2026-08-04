# Problem Statement

## Objective

Build a deterministic, constant-memory event reader and session validator for
rotated NinjaTrader Level 1 CSV exports.

This is an execution-data integrity problem, not a request to invent a profitable
strategy.

## Input available today

The current recorder emits:

```text
timestamp_utc_ns,instrument,event_type,price,volume,state
```

`event_type` is one of `BID`, `ASK`, or `TRADE`. Each row is appended in the order
that the recorder receives its callback.

## Expected behavior

The replacement reader must:

1. Read one row at a time with memory use independent of total file size.
2. Preserve manifest file-part order and physical row order.
3. Never sort causal events by exchange/source timestamp.
4. Never remove a repeated callback merely because its values match another row.
5. Assign one stable, unique event ID to every physical callback row.
6. Maintain bid and ask independently and expose state only after both are known.
7. Mark locked, crossed, malformed, or incomplete quote states explicitly.
8. Report source-time regressions without changing callback order.
9. Fail closed on missing file parts, bad schemas, truncated rows, or sequence gaps.
10. Produce a machine-readable per-session certificate and an append-only audit log.

## Actual legacy behavior

`load_ninjatrader_event_exports()` currently concatenates pandas DataFrames,
deduplicates by event contents, and sorts by `timestamp_utc_ns`.

Consequences:

- A legitimate repeated quote update can disappear.
- A source-time regression can reorder callbacks.
- The returned index no longer proves physical recorder order.
- Full multi-gigabyte archives must fit into memory.

## Reproduction

```bash
python -m pytest -q tests/test_known_causality_gaps.py -rxX
python scripts/inspect_sample.py samples/synthetic_mnq_events.csv
```

The expected-failure tests cover:

- Physical callback order when source time regresses.
- Preservation of identical but physically separate quote callbacks.

## Requested review

Please focus feedback on these decisions:

1. Should event identity be `run_id:file_part:record_seq`, or should the reader also
   hash raw row bytes?
2. What manifest fields are sufficient to prove rotation continuity?
3. Should malformed rows stop the full session or be journaled and skipped?
4. How should the API expose control events such as reconnects and writer restarts?
5. What iterator and state types make accidental timestamp sorting difficult?
6. Which invariants belong in the recorder, the reader, and the session certifier?

## Definition of done

- All expected-failure tests become ordinary passing tests.
- A synthetic 10-million-row fixture can be streamed with bounded memory.
- Replaying the same manifest twice produces byte-identical normalized output and
  certificate hashes.
- Every physical input row maps to exactly one event ID or one explicit rejection.
- Quote reconstruction is deterministic and never uses a later row.
- Session validation fails on a deleted, repeated, reordered, or truncated file part.

