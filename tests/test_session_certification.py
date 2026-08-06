from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from video_trader.data.ninjatrader_events import make_event_id
from video_trader.data.session_certification import (
    CertificationRequirements,
    certify_session,
    read_certificate,
    verify_certificate,
    write_certificate,
)


EVENT_FIELDS = [
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
CONTROL_FIELDS = [
    "schema_version",
    "recorder_run_id",
    "control_seq",
    "receive_time_utc_ns",
    "instrument",
    "control_type",
    "status",
    "connection_name",
    "provider",
    "feed_family",
    "details",
]


def _write(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _events() -> list[dict[str, object]]:
    times = [100, 150, 200]
    types = ["BID", "ASK", "TRADE"]
    prices = [100.0, 100.25, 100.25]
    rows = []
    for seq, (timestamp, event_type, price) in enumerate(
        zip(times, types, prices), start=1
    ):
        rows.append(
            {
                "schema_version": 2,
                "recorder_run_id": "run-a",
                "file_part": 0,
                "record_seq": seq,
                "event_id": make_event_id("run-a", seq),
                "timestamp_utc_ns": timestamp,
                "receive_time_utc_ns": timestamp,
                "instrument": "MNQ TEST",
                "event_type": event_type,
                "price": price,
                "volume": 1,
                "state": "Realtime",
            }
        )
    return rows


def _controls(
    *,
    include_stop: bool = True,
    clean: bool = True,
    writer_error: bool = False,
    final_record_seq: int = 3,
    event_rows: int = 3,
    final_event_part: int = 0,
    provider: str = "Rithmic",
    feed_family: str = "Rithmic",
) -> list[dict[str, object]]:
    rows = [
        {
            "schema_version": 3,
            "recorder_run_id": "run-a",
            "control_seq": 1,
            "receive_time_utc_ns": 90,
            "instrument": "MNQ TEST",
            "control_type": "RUN_START",
            "status": "STARTED",
            "connection_name": "Rithmic research",
            "provider": provider,
            "feed_family": feed_family,
            "details": "account=Sim101;account_connection=Simulation;price_status=Connected",
        },
        {
            "schema_version": 3,
            "recorder_run_id": "run-a",
            "control_seq": 2,
            "receive_time_utc_ns": 91,
            "instrument": "MNQ TEST",
            "control_type": "CONNECTION",
            "status": "CONNECTED",
            "connection_name": "Rithmic research",
            "provider": provider,
            "feed_family": feed_family,
            "details": "startup_snapshot",
        },
    ]
    if writer_error:
        rows.append(
            {
                "schema_version": 3,
                "recorder_run_id": "run-a",
                "control_seq": len(rows) + 1,
                "receive_time_utc_ns": 180,
                "instrument": "MNQ TEST",
                "control_type": "WRITER_ERROR",
                "status": "ERROR",
                "connection_name": "Rithmic research",
                "provider": provider,
                "feed_family": feed_family,
                "details": "disk full",
            }
        )
    if include_stop:
        rows.append(
            {
                "schema_version": 3,
                "recorder_run_id": "run-a",
                "control_seq": len(rows) + 1,
                "receive_time_utc_ns": 210,
                "instrument": "MNQ TEST",
                "control_type": "RUN_STOP",
                "status": "CLEAN" if clean else "ERROR",
                "connection_name": "Rithmic research",
                "provider": provider,
                "feed_family": feed_family,
                "details": (
                    f"final_record_seq={final_record_seq};event_rows={event_rows};"
                    f"final_event_part={final_event_part};record_event_stream=true;"
                    "depth_events_dropped=0;"
                    f"writer_error_count={int(writer_error)}"
                ),
            }
        )
    return rows


def _requirements() -> CertificationRequirements:
    return CertificationRequirements(
        expected_instrument="MNQ TEST",
        expected_connection_name="Rithmic research",
        expected_provider="Rithmic",
        expected_feed_family="Rithmic",
        recorder_source_sha256="a" * 64,
        session_start_utc_ns=100,
        session_end_utc_ns=200,
        boundary_tolerance_ns=0,
        max_receive_gap_ns=100,
    )


def _certify_rows(
    tmp_path: Path,
    event_rows: list[dict[str, object]],
    control_rows: list[dict[str, object]],
):
    events = tmp_path / "events.csv"
    controls = tmp_path / "controls.csv"
    _write(events, EVENT_FIELDS, event_rows)
    _write(controls, CONTROL_FIELDS, control_rows)
    return certify_session([events], controls, _requirements())


def test_complete_session_passes_and_hash_is_deterministic(tmp_path: Path) -> None:
    first = _certify_rows(tmp_path, _events(), _controls())
    second = _certify_rows(tmp_path, _events(), _controls())

    assert first.status == "PASS"
    assert first.reason_codes == ()
    assert first.provenance_confirmed
    assert first.certificate_sha256 == second.certificate_sha256
    assert first.parts[0].sha256 == second.parts[0].sha256
    verify_certificate(first)
    output = tmp_path / "certificate.json"
    write_certificate(first, output)
    assert read_certificate(output) == first
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_certificate(replace(first, session_end_utc_ns=201))


def test_missing_clean_stop_fails(tmp_path: Path) -> None:
    certificate = _certify_rows(tmp_path, _events(), _controls(include_stop=False))

    assert certificate.status == "FAIL"
    assert "CLEAN_STOP_MISSING" in certificate.reason_codes
    assert "RUN_STOP_COUNT_NOT_ONE" in certificate.reason_codes


def test_writer_error_fails_even_after_clean_stop(tmp_path: Path) -> None:
    certificate = _certify_rows(tmp_path, _events(), _controls(writer_error=True))

    assert certificate.status == "FAIL"
    assert certificate.writer_error_count == 1
    assert "WRITER_ERROR_OBSERVED" in certificate.reason_codes


def test_sequence_gap_becomes_explicit_certificate_failure(tmp_path: Path) -> None:
    rows = _events()
    rows[2]["record_seq"] = 4
    rows[2]["event_id"] = make_event_id("run-a", 4)
    certificate = _certify_rows(tmp_path, rows, _controls(final_record_seq=4))

    assert certificate.status == "FAIL"
    assert "EVENT_REPLAY_ERROR" in certificate.reason_codes


def test_duplicate_record_sequence_fails(tmp_path: Path) -> None:
    rows = _events()
    rows[2]["record_seq"] = 2
    rows[2]["event_id"] = make_event_id("run-a", 2)
    certificate = _certify_rows(tmp_path, rows, _controls(final_record_seq=2))

    assert certificate.status == "FAIL"
    assert "EVENT_REPLAY_ERROR" in certificate.reason_codes


def test_malformed_event_row_fails(tmp_path: Path) -> None:
    rows = _events()
    rows[1]["price"] = "not-a-price"
    certificate = _certify_rows(tmp_path, rows, _controls())

    assert certificate.status == "FAIL"
    assert "EVENT_REPLAY_ERROR" in certificate.reason_codes


def test_missing_rotated_part_fails_against_run_stop_attestation(
    tmp_path: Path,
) -> None:
    rows = _events()
    rows[2]["file_part"] = 1
    part_zero = tmp_path / "events.csv"
    controls = tmp_path / "controls.csv"
    _write(part_zero, EVENT_FIELDS, rows[:2])
    _write(
        controls,
        CONTROL_FIELDS,
        _controls(final_record_seq=3, event_rows=3, final_event_part=1),
    )

    certificate = certify_session([part_zero], controls, _requirements())

    assert certificate.status == "FAIL"
    assert "RUN_STOP_RECORD_SEQUENCE_MISMATCH" in certificate.reason_codes
    assert "RUN_STOP_EVENT_COUNT_MISMATCH" in certificate.reason_codes
    assert "RUN_STOP_FINAL_PART_MISMATCH" in certificate.reason_codes


def test_provider_and_feed_family_must_match(tmp_path: Path) -> None:
    certificate = _certify_rows(
        tmp_path,
        _events(),
        _controls(provider="Different", feed_family="Different"),
    )

    assert certificate.status == "FAIL"
    assert "PROVIDER_MISMATCH_OBSERVED" in certificate.reason_codes
    assert "FEED_FAMILY_MISMATCH_OBSERVED" in certificate.reason_codes
    assert "RUN_START_PROVENANCE_MISMATCH" in certificate.reason_codes


def test_control_after_run_stop_is_rejected(tmp_path: Path) -> None:
    controls = _controls()
    trailing = dict(controls[1])
    trailing["control_seq"] = len(controls) + 1
    trailing["receive_time_utc_ns"] = 220
    controls.append(trailing)
    certificate = _certify_rows(tmp_path, _events(), controls)

    assert certificate.status == "FAIL"
    assert "RUN_STOP_NOT_FINAL_CONTROL" in certificate.reason_codes


def test_receive_gap_and_wrong_connection_fail(tmp_path: Path) -> None:
    events = tmp_path / "events.csv"
    controls = tmp_path / "controls.csv"
    rows = _events()
    rows[1]["receive_time_utc_ns"] = 500
    rows[2]["receive_time_utc_ns"] = 600
    _write(events, EVENT_FIELDS, rows)
    _write(controls, CONTROL_FIELDS, _controls())
    requirements = replace(
        _requirements(),
        expected_connection_name="Different feed",
        session_end_utc_ns=600,
    )

    certificate = certify_session([events], controls, requirements)

    assert certificate.status == "FAIL"
    assert "RECEIVE_GAP_EXCEEDED" in certificate.reason_codes
    assert "CONNECTED_FEED_IDENTITY_NOT_OBSERVED" in certificate.reason_codes


def test_pre_session_gap_and_recovered_connection_do_not_invalidate_rth(
    tmp_path: Path,
) -> None:
    rows = _events()
    rows[0]["timestamp_utc_ns"] = 10
    rows[0]["receive_time_utc_ns"] = 10
    rows[1]["timestamp_utc_ns"] = 150
    rows[1]["receive_time_utc_ns"] = 150
    controls = _controls()
    controls[0]["receive_time_utc_ns"] = 0
    controls[1]["receive_time_utc_ns"] = 1
    lost = dict(controls[1])
    lost["control_seq"] = 3
    lost["receive_time_utc_ns"] = 50
    lost["status"] = "CONNECTIONLOST"
    recovered = dict(controls[1])
    recovered["control_seq"] = 4
    recovered["receive_time_utc_ns"] = 60
    controls[-1]["control_seq"] = 5
    controls[-1]["receive_time_utc_ns"] = 210
    controls = [controls[0], controls[1], lost, recovered, controls[-1]]
    events = tmp_path / "events.csv"
    control_path = tmp_path / "controls.csv"
    _write(events, EVENT_FIELDS, rows)
    _write(control_path, CONTROL_FIELDS, controls)
    requirements = replace(
        _requirements(),
        session_start_utc_ns=150,
        session_end_utc_ns=200,
        max_receive_gap_ns=100,
    )

    certificate = certify_session([events], control_path, requirements)

    assert certificate.status == "PASS"
    assert certificate.maximum_run_receive_gap_ns == 140
    assert certificate.maximum_receive_gap_ns == 50
    assert certificate.connection_failure_count == 0
    assert certificate.out_of_session_connection_failure_count == 1


def test_full_rth_start_to_clean_stop_passes_end_to_end(tmp_path: Path) -> None:
    start = datetime(2026, 8, 6, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int(end.timestamp() * 1_000_000_000)
    rows: list[dict[str, object]] = []
    cursor = start
    seq = 0
    while cursor <= end:
        timestamp = int(cursor.timestamp() * 1_000_000_000)
        for event_type, price in (("BID", 100.0), ("ASK", 100.25)):
            seq += 1
            rows.append(
                {
                    "schema_version": 2,
                    "recorder_run_id": "run-a",
                    "file_part": min((seq - 1) // 4000, 2),
                    "record_seq": seq,
                    "event_id": make_event_id("run-a", seq),
                    "timestamp_utc_ns": timestamp,
                    "receive_time_utc_ns": timestamp,
                    "instrument": "MNQ TEST",
                    "event_type": event_type,
                    "price": price,
                    "volume": 1,
                    "state": "Realtime",
                }
            )
        cursor += timedelta(seconds=5)

    event_paths: list[Path] = []
    for part in range(3):
        path = tmp_path / ("events.csv" if part == 0 else f"events_p{part:04d}.csv")
        _write(path, EVENT_FIELDS, [row for row in rows if row["file_part"] == part])
        event_paths.append(path)
    control_rows = _controls(
        final_record_seq=seq,
        event_rows=seq,
        final_event_part=2,
    )
    control_rows[0]["receive_time_utc_ns"] = start_ns - 1
    control_rows[1]["receive_time_utc_ns"] = start_ns
    control_rows[-1]["receive_time_utc_ns"] = end_ns + 1
    controls = tmp_path / "controls.csv"
    _write(controls, CONTROL_FIELDS, control_rows)
    requirements = replace(
        _requirements(),
        session_start_utc_ns=start_ns,
        session_end_utc_ns=end_ns,
        max_receive_gap_ns=5_000_000_000,
    )

    certificate = certify_session(event_paths, controls, requirements)

    assert certificate.status == "PASS"
    assert certificate.reason_codes == ()
    assert certificate.event_count == seq
    assert certificate.event_part_count == 3
    assert certificate.clean_stop_observed
    assert certificate.provenance_confirmed
