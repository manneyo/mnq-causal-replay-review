# Data Schema

## Legacy event row

```text
timestamp_utc_ns,instrument,event_type,price,volume,state
```

This is enough to inspect real-time Bid, Ask, and Last callbacks, but it cannot by
itself prove recorder continuity or stable identity across rotated files.

## Proposed event row

```text
schema_version
run_id
file_part
record_seq
event_id
source_time_utc_ns
receive_time_utc_ns
receive_monotonic_ticks
feed_family
connection_name
instrument_full_name
contract_month
trading_session_date
event_type
event_price
event_size
best_bid_after
best_ask_after
best_bid_size_after
best_ask_size_after
market_state
connection_state
is_reset
```

## Ordering rule

`record_seq` and manifest file-part order define causality. Source time is retained
for analysis and quality reporting but must never reorder callbacks.

## Identity rule

The initial proposal is:

```text
event_id = run_id:file_part:record_seq
```

A raw-row hash can be stored separately for integrity. It cannot replace the event
ID because two legitimate callbacks may have identical contents.

## Quote state

Bid and ask are updated independently. The normalized row describes state after the
current callback. A quote is executable only when both sides are initialized and
`best_ask_after > best_bid_after`.

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

