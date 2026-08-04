from __future__ import annotations

import csv
from dataclasses import asdict
from pathlib import Path

import pytest

from video_trader.data.ninjatrader_events import (
    DuplicateEventIdError,
    EventReplayError,
    EventSchemaError,
    EventSequenceError,
    EventValidationError,
    iter_ninjatrader_event_exports,
    make_event_id,
)


LEGACY_FIELDS = [
    "timestamp_utc_ns",
    "instrument",
    "event_type",
    "price",
    "volume",
    "state",
]
V2_FIELDS = [
    "schema_version",
    "recorder_run_id",
    "file_part",
    "record_seq",
    "event_id",
    "timestamp_utc_ns",
    "receive_time_utc_ns",
    "instrument",
    "event_type",
    "price",
    "volume",
    "state",
]


def _write(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _v2_row(
    *,
    run_id: str = "run-a",
    part: int = 0,
    seq: int = 1,
    timestamp: int = 100,
    event_type: str = "BID",
    price: float = 100.0,
    volume: float = 1,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "recorder_run_id": run_id,
        "file_part": part,
        "record_seq": seq,
        "event_id": make_event_id(run_id, seq),
        "timestamp_utc_ns": timestamp,
        "receive_time_utc_ns": timestamp + 10,
        "instrument": "MNQ TEST",
        "event_type": event_type,
        "price": price,
        "volume": volume,
        "state": "Realtime",
    }


def test_v2_rotation_preserves_order_and_sequence(tmp_path: Path) -> None:
    part0 = tmp_path / "events.csv"
    part1 = tmp_path / "events_p0001.csv"
    _write(
        part0,
        V2_FIELDS,
        [
            _v2_row(seq=1, timestamp=200, event_type="BID"),
            _v2_row(seq=2, timestamp=100, event_type="ASK", price=100.25),
        ],
    )
    _write(
        part1,
        V2_FIELDS,
        [_v2_row(part=1, seq=3, timestamp=300, event_type="TRADE")],
    )

    events = list(iter_ninjatrader_event_exports([part0, part1]))

    assert [event.record_seq for event in events] == [1, 2, 3]
    assert [event.file_part for event in events] == [0, 0, 1]
    assert [event.event_type for event in events] == ["BID", "ASK", "TRADE"]
    assert [event.source_time_regression for event in events] == [False, True, False]


def test_v2_preserves_identical_payloads_with_distinct_ids(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    first = _v2_row(seq=1)
    second = _v2_row(seq=2)
    _write(path, V2_FIELDS, [first, second])

    events = list(iter_ninjatrader_event_exports([path]))

    assert len(events) == 2
    assert events[0].price == events[1].price
    assert events[0].event_id != events[1].event_id


def test_v2_rejects_duplicate_persisted_event_id(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    repeated = _v2_row(seq=1)
    _write(path, V2_FIELDS, [repeated, repeated.copy()])

    with pytest.raises(DuplicateEventIdError, match="duplicate persisted event_id"):
        list(iter_ninjatrader_event_exports([path]))


def test_v2_rejects_sequence_gap(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    _write(path, V2_FIELDS, [_v2_row(seq=1), _v2_row(seq=3)])

    with pytest.raises(EventSequenceError, match="expected record_seq 2"):
        list(iter_ninjatrader_event_exports([path]))


def test_v2_rejects_missing_rotation_part(tmp_path: Path) -> None:
    part0 = tmp_path / "events.csv"
    part2 = tmp_path / "events_p0002.csv"
    _write(part0, V2_FIELDS, [_v2_row(seq=1)])
    _write(part2, V2_FIELDS, [_v2_row(part=2, seq=2)])

    with pytest.raises(EventSequenceError, match="expected file_part 1"):
        list(iter_ninjatrader_event_exports([part0, part2]))


def test_v2_allows_new_run_to_restart_identity_sequence(tmp_path: Path) -> None:
    first_run = tmp_path / "run_a.csv"
    second_run = tmp_path / "run_b.csv"
    _write(
        first_run,
        V2_FIELDS,
        [
            _v2_row(run_id="run-a", seq=1, event_type="BID"),
            _v2_row(
                run_id="run-a",
                seq=2,
                event_type="ASK",
                price=100.25,
            ),
        ],
    )
    _write(
        second_run,
        V2_FIELDS,
        [
            _v2_row(
                run_id="run-b",
                seq=1,
                timestamp=200,
                event_type="ASK",
                price=100.5,
            )
        ],
    )

    events = list(iter_ninjatrader_event_exports([first_run, second_run]))

    assert [event.recorder_run_id for event in events] == [
        "run-a",
        "run-a",
        "run-b",
    ]
    assert [event.record_seq for event in events] == [1, 2, 1]
    assert [event.is_run_start for event in events] == [True, False, True]
    assert events[1].quote_status == "VALID"
    assert events[2].quote_status == "INCOMPLETE"
    assert events[2].best_bid_after is None


def test_v2_rejects_a_closed_run_reappearing(tmp_path: Path) -> None:
    paths = [tmp_path / f"part-{index}.csv" for index in range(3)]
    _write(paths[0], V2_FIELDS, [_v2_row(run_id="run-a")])
    _write(paths[1], V2_FIELDS, [_v2_row(run_id="run-b", timestamp=200)])
    _write(paths[2], V2_FIELDS, [_v2_row(run_id="run-a", timestamp=300)])

    with pytest.raises(DuplicateEventIdError, match="reappeared after it closed"):
        list(iter_ninjatrader_event_exports(paths))


def test_v2_rejects_recorder_restart_inside_one_file(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    _write(
        path,
        V2_FIELDS,
        [_v2_row(run_id="run-a"), _v2_row(run_id="run-b", timestamp=200)],
    )

    with pytest.raises(EventSequenceError, match="changed inside one CSV"):
        list(iter_ninjatrader_event_exports([path]))


def test_v2_rejects_event_id_that_disagrees_with_identity(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    row = _v2_row()
    row["event_id"] = "run-a:wrong"
    _write(path, V2_FIELDS, [row])

    with pytest.raises(EventValidationError, match="does not match"):
        list(iter_ninjatrader_event_exports([path]))


def test_partial_v2_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    fields = LEGACY_FIELDS + ["event_id"]
    row = _v2_row()
    _write(path, fields, [{field: row[field] for field in fields}])

    with pytest.raises(EventSchemaError, match="partial v2 identity schema"):
        list(iter_ninjatrader_event_exports([path]))


def test_malformed_numeric_row_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    row = {field: _v2_row()[field] for field in LEGACY_FIELDS}
    row["price"] = "not-a-price"
    _write(path, LEGACY_FIELDS, [row])

    with pytest.raises(EventValidationError, match="price must be numeric"):
        list(iter_ninjatrader_event_exports([path]))


def test_non_finite_numeric_row_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    row = {field: _v2_row()[field] for field in LEGACY_FIELDS}
    row["volume"] = "NaN"
    _write(path, LEGACY_FIELDS, [row])

    with pytest.raises(EventValidationError, match="volume must be finite"):
        list(iter_ninjatrader_event_exports([path]))


def test_empty_event_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    _write(path, LEGACY_FIELDS, [])

    with pytest.raises(EventValidationError, match="has no data rows"):
        list(iter_ninjatrader_event_exports([path]))


def test_mixed_legacy_and_v2_files_fail_closed(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.csv"
    v2 = tmp_path / "v2.csv"
    legacy_row = {field: _v2_row()[field] for field in LEGACY_FIELDS}
    _write(legacy, LEGACY_FIELDS, [legacy_row])
    _write(v2, V2_FIELDS, [_v2_row()])

    with pytest.raises(EventSchemaError, match="mixes legacy and v2"):
        list(iter_ninjatrader_event_exports([legacy, v2]))


def test_quote_state_is_applied_one_callback_at_a_time(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    rows = [
        _v2_row(seq=1, event_type="TRADE", price=100.25),
        _v2_row(seq=2, event_type="BID", price=100.0),
        _v2_row(seq=3, event_type="ASK", price=100.25),
        _v2_row(seq=4, event_type="BID", price=100.25),
        _v2_row(seq=5, event_type="BID", price=100.5),
    ]
    _write(path, V2_FIELDS, rows)

    events = list(iter_ninjatrader_event_exports([path]))

    assert [event.quote_status for event in events] == [
        "INCOMPLETE",
        "INCOMPLETE",
        "VALID",
        "LOCKED",
        "CROSSED",
    ]
    assert events[0].best_bid_after is None
    assert events[0].best_ask_after is None
    assert events[2].best_bid_after == 100.0
    assert events[2].best_ask_after == 100.25


def test_stream_opens_later_files_only_when_iteration_reaches_them(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.csv"
    missing = tmp_path / "missing.csv"
    row = {field: _v2_row()[field] for field in LEGACY_FIELDS}
    _write(first, LEGACY_FIELDS, [row])
    stream = iter_ninjatrader_event_exports([first, missing])

    assert next(stream).event_id == "legacy:000000:000000000002"
    with pytest.raises(EventReplayError, match="cannot open event export"):
        next(stream)


def test_replaying_same_manifest_is_byte_stable(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    _write(path, V2_FIELDS, [_v2_row(seq=1), _v2_row(seq=2)])

    first = [asdict(event) for event in iter_ninjatrader_event_exports([path])]
    second = [asdict(event) for event in iter_ninjatrader_event_exports([path])]

    assert first == second
