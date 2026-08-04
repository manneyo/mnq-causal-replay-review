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

## Original legacy behavior

`load_ninjatrader_event_exports()` originally concatenated pandas DataFrames,
deduplicated by event contents, and sorted by `timestamp_utc_ns`.

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

The regression tests cover:

- Physical callback order when source time regresses.
- Preservation of identical but physically separate quote callbacks.

## Implemented reader decision

V2 identity is `recorder_run_id:record_seq`. File rotation does not reset the
sequence, while a recorder restart creates a new run ID. The reader rejects
duplicate persisted IDs, gaps, non-contiguous file parts, mid-file run changes,
and malformed schemas. Legacy payloads are never deduplicated.

## Remaining review

Please focus feedback on these decisions:

1. What signed or hashed manifest fields are sufficient to certify raw files?
2. How should reconnect and writer-error control events be represented?
3. What session-level completeness rules require feed-specific metadata?
4. Should a separate audit sink journal rejected rows before fail-closed exit?

## Definition of done

- All original expected-failure tests pass normally.
- A synthetic 10-million-row fixture can be streamed with bounded memory.
- Replaying the same manifest twice produces byte-identical normalized output and
  certificate hashes.
- Every physical input row maps to exactly one event ID or one explicit rejection.
- Quote reconstruction is deterministic and never uses a later row.
- Session validation fails on a deleted, repeated, reordered, or truncated file part.
