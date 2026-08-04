from __future__ import annotations

import csv
from dataclasses import replace
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


def _controls(*, clean: bool = True, writer_error: bool = False):
    rows = [
        {
            "schema_version": 2,
            "recorder_run_id": "run-a",
            "control_seq": 1,
            "receive_time_utc_ns": 90,
            "instrument": "MNQ TEST",
            "control_type": "RUN_START",
            "status": "STARTED",
            "connection_name": "Rithmic",
            "details": "",
        }
    ]
    if writer_error:
        rows.append(
            {
                "schema_version": 2,
                "recorder_run_id": "run-a",
                "control_seq": len(rows) + 1,
                "receive_time_utc_ns": 180,
                "instrument": "MNQ TEST",
                "control_type": "WRITER_ERROR",
                "status": "ERROR",
                "connection_name": "Rithmic",
                "details": "disk full",
            }
        )
    if clean:
        rows.append(
            {
                "schema_version": 2,
                "recorder_run_id": "run-a",
                "control_seq": len(rows) + 1,
                "receive_time_utc_ns": 210,
                "instrument": "MNQ TEST",
                "control_type": "RUN_STOP",
                "status": "CLEAN",
                "connection_name": "Rithmic",
                "details": "",
            }
        )
    return rows


def _requirements() -> CertificationRequirements:
    return CertificationRequirements(
        expected_instrument="MNQ TEST",
        expected_connection_name="Rithmic",
        recorder_source_sha256="a" * 64,
        session_start_utc_ns=100,
        session_end_utc_ns=200,
        boundary_tolerance_ns=0,
        max_receive_gap_ns=100,
    )


def test_complete_session_passes_and_hash_is_deterministic(tmp_path: Path) -> None:
    events = tmp_path / "events.csv"
    controls = tmp_path / "controls.csv"
    _write(events, EVENT_FIELDS, _events())
    _write(controls, CONTROL_FIELDS, _controls())

    first = certify_session([events], controls, _requirements())
    second = certify_session([events], controls, _requirements())

    assert first.status == "PASS"
    assert first.reason_codes == ()
    assert first.certificate_sha256 == second.certificate_sha256
    assert first.parts[0].sha256 == second.parts[0].sha256
    verify_certificate(first)
    output = tmp_path / "certificate.json"
    write_certificate(first, output)
    assert read_certificate(output) == first
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_certificate(replace(first, session_end_utc_ns=201))


def test_missing_clean_stop_fails(tmp_path: Path) -> None:
    events = tmp_path / "events.csv"
    controls = tmp_path / "controls.csv"
    _write(events, EVENT_FIELDS, _events())
    _write(controls, CONTROL_FIELDS, _controls(clean=False))

    certificate = certify_session([events], controls, _requirements())

    assert certificate.status == "FAIL"
    assert "CLEAN_STOP_MISSING" in certificate.reason_codes


def test_writer_error_fails_even_after_clean_stop(tmp_path: Path) -> None:
    events = tmp_path / "events.csv"
    controls = tmp_path / "controls.csv"
    _write(events, EVENT_FIELDS, _events())
    _write(controls, CONTROL_FIELDS, _controls(writer_error=True))

    certificate = certify_session([events], controls, _requirements())

    assert certificate.status == "FAIL"
    assert certificate.writer_error_count == 1
    assert "WRITER_ERROR_OBSERVED" in certificate.reason_codes


def test_sequence_gap_becomes_explicit_certificate_failure(tmp_path: Path) -> None:
    events = tmp_path / "events.csv"
    controls = tmp_path / "controls.csv"
    rows = _events()
    rows[2]["record_seq"] = 4
    rows[2]["event_id"] = make_event_id("run-a", 4)
    _write(events, EVENT_FIELDS, rows)
    _write(controls, CONTROL_FIELDS, _controls())

    certificate = certify_session([events], controls, _requirements())

    assert certificate.status == "FAIL"
    assert "EVENT_REPLAY_ERROR" in certificate.reason_codes


def test_receive_gap_and_wrong_connection_fail(tmp_path: Path) -> None:
    events = tmp_path / "events.csv"
    controls = tmp_path / "controls.csv"
    rows = _events()
    rows[1]["receive_time_utc_ns"] = 500
    rows[2]["receive_time_utc_ns"] = 600
    _write(events, EVENT_FIELDS, rows)
    _write(controls, CONTROL_FIELDS, _controls())
    requirements = CertificationRequirements(
        expected_instrument="MNQ TEST",
        expected_connection_name="Different feed",
        recorder_source_sha256="a" * 64,
        session_start_utc_ns=100,
        session_end_utc_ns=600,
        boundary_tolerance_ns=0,
        max_receive_gap_ns=100,
    )

    certificate = certify_session([events], controls, requirements)

    assert certificate.status == "FAIL"
    assert "RECEIVE_GAP_EXCEEDED" in certificate.reason_codes
    assert "CONNECTION_IDENTITY_NOT_OBSERVED" in certificate.reason_codes
