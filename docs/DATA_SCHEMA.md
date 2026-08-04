# Data Schema

## Legacy event row

```text
timestamp_utc_ns,instrument,event_type,price,volume,state
```

This is enough to inspect real-time Bid, Ask, and Last callbacks, but it cannot by
itself prove recorder continuity or stable identity across rotated files.

## V2 event row

```text
schema_version,recorder_run_id,file_part,record_seq,event_id,timestamp_utc_ns,receive_time_utc_ns,instrument,event_type,price,volume,state
```

`timestamp_utc_ns` remains the provider/source time for compatibility. The
recorder captures `receive_time_utc_ns` independently when the callback is
handled. Extended feed, connection, and session metadata belongs in the future
manifest rather than being inferred by the reader.

## Ordering rule

Caller-supplied manifest position, physical row order, and continuous `record_seq`
define causality. `file_part` must start at zero and increase by one at rotation.
Source time is retained for analysis and quality reporting but never reorders rows.

## Identity rule

The persisted identity is:

```text
event_id = recorder_run_id:record_seq
```

The sequence is not reset by file rotation. A recorder restart creates a new run
ID and begins again at sequence one and file part zero. The ID is assigned before
the row is written; any retry of that same record must retain the original ID.

A raw-row hash may be stored separately for integrity, but it cannot replace event
identity because two legitimate callbacks may have identical contents.

Legacy rows receive `legacy:<manifest-position>:<physical-row>` positional IDs.
These IDs make replay deterministic for an unchanged manifest. They do not prove
that two content-identical legacy rows are or are not duplicate writes, so the
reader preserves every one.

## Quote state

Bid and ask are updated independently. The normalized Python event describes state
after the current callback as `INCOMPLETE`, `VALID`, `LOCKED`, or `CROSSED`. A quote
is executable only when both sides are initialized and ask is greater than bid.
The state machine clears at every v2 recorder-run boundary because unobserved
callbacks may exist between runs. Legacy files cannot prove restart boundaries.

## File manifest

Every finalized part should include:

- Schema and recorder version.
- Recorder source-code SHA-256.
- Run ID and ordered part number.
- Instrument, contract, feed, and connection identity.
- Row count and first/last record sequence.
- First/last source and receive timestamps.
- Previous-part SHA-256 and current-file SHA-256.
- Clean-close status and writer-error count.

The v2 recorder now writes a separate control journal:

```text
schema_version,recorder_run_id,control_seq,receive_time_utc_ns,instrument,control_type,status,connection_name,details
```

It records run start/stop, state transitions, connection snapshots and changes,
and writer errors. `scripts/certify_session.py` combines that journal with the
ordered event parts, their SHA-256 hash chain and the recorder-source SHA-256 to
produce a deterministic machine-readable certificate. A session fails closed if
the expected connection is never observed, a writer/connection failure occurs,
the receive clock regresses, a configured receive gap is exceeded, RTH boundaries
are missing, or replay identity validation fails.
