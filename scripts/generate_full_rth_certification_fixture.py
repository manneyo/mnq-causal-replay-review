from __future__ import annotations

import csv
import hashlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_trader.data.ninjatrader_events import make_event_id  # noqa: E402
from video_trader.data.session_certification import (  # noqa: E402
    CertificationRequirements,
    certify_session,
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


def main() -> int:
    output = ROOT / "samples" / "synthetic_full_rth_passing_certificate.json"
    source = ROOT / "ninjatrader" / "CodexResearchDataRecorder.cs"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    instrument = "MNQ 09-26"
    connection = "Rithmic research"
    provider = "Rithmic"
    run_id = "synthetic-rth-20260806"
    start = datetime(2026, 8, 6, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int(end.timestamp() * 1_000_000_000)

    with tempfile.TemporaryDirectory(prefix="mnq-certification-fixture-") as directory:
        work = Path(directory)
        rows: list[dict[str, object]] = []
        cursor = start
        sequence = 0
        while cursor <= end:
            timestamp = int(cursor.timestamp() * 1_000_000_000)
            for event_type, price in (("BID", 29889.50), ("ASK", 29889.75)):
                sequence += 1
                rows.append(
                    {
                        "schema_version": 2,
                        "recorder_run_id": run_id,
                        "file_part": min((sequence - 1) // 4000, 2),
                        "record_seq": sequence,
                        "event_id": make_event_id(run_id, sequence),
                        "timestamp_utc_ns": timestamp,
                        "receive_time_utc_ns": timestamp,
                        "instrument": instrument,
                        "event_type": event_type,
                        "price": price,
                        "volume": 1,
                        "state": "Realtime",
                    }
                )

            cursor += timedelta(seconds=5)

        event_paths: list[Path] = []
        for part in range(3):
            name = "synthetic_events.csv" if part == 0 else f"synthetic_events_p{part:04d}.csv"
            path = work / name
            _write(path, EVENT_FIELDS, [row for row in rows if row["file_part"] == part])
            event_paths.append(path)

        controls = work / "synthetic_controls.csv"
        control_rows = [
            {
                "schema_version": 3,
                "recorder_run_id": run_id,
                "control_seq": 1,
                "receive_time_utc_ns": start_ns - 1,
                "instrument": instrument,
                "control_type": "RUN_START",
                "status": "STARTED",
                "connection_name": connection,
                "provider": provider,
                "feed_family": provider,
                "details": "account=Sim101;account_connection=Simulation;price_status=Connected",
            },
            {
                "schema_version": 3,
                "recorder_run_id": run_id,
                "control_seq": 2,
                "receive_time_utc_ns": start_ns,
                "instrument": instrument,
                "control_type": "CONNECTION",
                "status": "CONNECTED",
                "connection_name": connection,
                "provider": provider,
                "feed_family": provider,
                "details": "startup_snapshot",
            },
            {
                "schema_version": 3,
                "recorder_run_id": run_id,
                "control_seq": 3,
                "receive_time_utc_ns": end_ns + 1,
                "instrument": instrument,
                "control_type": "RUN_STOP",
                "status": "CLEAN",
                "connection_name": connection,
                "provider": provider,
                "feed_family": provider,
                "details": (
                    f"final_record_seq={sequence};event_rows={sequence};"
                    "final_event_part=2;record_event_stream=true;"
                    "depth_events_dropped=0;writer_error_count=0"
                ),
            },
        ]
        _write(controls, CONTROL_FIELDS, control_rows)
        certificate = certify_session(
            event_paths,
            controls,
            CertificationRequirements(
                expected_instrument=instrument,
                expected_connection_name=connection,
                expected_provider=provider,
                expected_feed_family=provider,
                recorder_source_sha256=source_hash,
                session_start_utc_ns=start_ns,
                session_end_utc_ns=end_ns,
                boundary_tolerance_ns=0,
                max_receive_gap_ns=5_000_000_000,
            ),
        )
        if certificate.status != "PASS":
            raise RuntimeError(
                "synthetic full-RTH fixture failed: "
                + ",".join(certificate.reason_codes)
            )
        write_certificate(certificate, output)

    print(f"PASS {certificate.certificate_sha256} {output}")
    print("PIPELINE TEST ONLY: this is not recorded market evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
