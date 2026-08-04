from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable

from .ninjatrader_events import EventReplayError, iter_ninjatrader_event_exports


CONTROL_COLUMNS = frozenset(
    {
        "schema_version",
        "recorder_run_id",
        "control_seq",
        "receive_time_utc_ns",
        "instrument",
        "control_type",
        "status",
        "connection_name",
        "details",
    }
)


@dataclass(frozen=True, slots=True)
class CertificationRequirements:
    expected_instrument: str
    expected_connection_name: str
    recorder_source_sha256: str
    session_start_utc_ns: int
    session_end_utc_ns: int
    boundary_tolerance_ns: int = 60_000_000_000
    max_receive_gap_ns: int = 5_000_000_000

    def __post_init__(self) -> None:
        if not self.expected_instrument.strip():
            raise ValueError("expected_instrument cannot be empty")
        if not self.expected_connection_name.strip():
            raise ValueError("expected_connection_name cannot be empty")
        source_hash = self.recorder_source_sha256.lower()
        if len(source_hash) != 64 or any(
            character not in "0123456789abcdef" for character in source_hash
        ):
            raise ValueError("recorder_source_sha256 must be a 64-character hex digest")
        if self.session_start_utc_ns >= self.session_end_utc_ns:
            raise ValueError("session start must precede session end")
        if self.boundary_tolerance_ns < 0 or self.max_receive_gap_ns <= 0:
            raise ValueError("certificate timing tolerances are invalid")


@dataclass(frozen=True, slots=True)
class PartEvidence:
    manifest_position: int
    file_name: str
    byte_count: int
    sha256: str
    previous_part_sha256: str | None


@dataclass(frozen=True, slots=True)
class SessionCertificate:
    schema_version: int
    status: str
    reason_codes: tuple[str, ...]
    expected_instrument: str
    expected_connection_name: str
    recorder_source_sha256: str
    session_start_utc_ns: int
    session_end_utc_ns: int
    boundary_tolerance_ns: int
    maximum_allowed_receive_gap_ns: int
    recorder_run_id: str | None
    event_count: int
    first_record_seq: int | None
    last_record_seq: int | None
    first_source_time_utc_ns: int | None
    last_source_time_utc_ns: int | None
    first_receive_time_utc_ns: int | None
    last_receive_time_utc_ns: int | None
    maximum_receive_gap_ns: int
    source_time_regressions: int
    invalid_quote_states: int
    control_event_count: int
    clean_stop_observed: bool
    writer_error_count: int
    connection_failure_count: int
    parts: tuple[PartEvidence, ...]
    certificate_sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def certify_session(
    event_paths: Iterable[Path],
    control_path: Path,
    requirements: CertificationRequirements,
) -> SessionCertificate:
    paths = tuple(Path(path) for path in event_paths)
    reasons: set[str] = set()
    parts = _part_evidence(paths, reasons)

    run_ids: set[str] = set()
    event_count = 0
    first_record_seq: int | None = None
    last_record_seq: int | None = None
    first_source: int | None = None
    last_source: int | None = None
    first_receive: int | None = None
    last_receive: int | None = None
    previous_receive: int | None = None
    maximum_receive_gap = 0
    regressions = 0
    invalid_quotes = 0

    try:
        for event in iter_ninjatrader_event_exports(paths):
            event_count += 1
            if event.schema_version != 2 or event.recorder_run_id is None:
                reasons.add("LEGACY_OR_UNIDENTIFIED_EVENT")
            else:
                run_ids.add(event.recorder_run_id)
            if event.instrument != requirements.expected_instrument:
                reasons.add("INSTRUMENT_MISMATCH")
            if event.state.upper() != "REALTIME":
                reasons.add("NON_REALTIME_EVENT")
            if event.receive_time_utc_ns is None:
                reasons.add("RECEIVE_TIME_MISSING")
            else:
                receive = event.receive_time_utc_ns
                if first_receive is None:
                    first_receive = receive
                if previous_receive is not None:
                    if receive < previous_receive:
                        reasons.add("RECEIVE_TIME_REGRESSION")
                    else:
                        maximum_receive_gap = max(
                            maximum_receive_gap, receive - previous_receive
                        )
                previous_receive = receive
                last_receive = receive
            if first_record_seq is None:
                first_record_seq = event.record_seq
                first_source = event.timestamp_utc_ns
            last_record_seq = event.record_seq
            last_source = event.timestamp_utc_ns
            regressions += int(event.source_time_regression)
            invalid_quotes += int(event.quote_status in {"LOCKED", "CROSSED"})
    except EventReplayError:
        reasons.add("EVENT_REPLAY_ERROR")

    if event_count == 0:
        reasons.add("NO_EVENTS")
    if len(run_ids) != 1:
        reasons.add("RUN_ID_COUNT_NOT_ONE")
    if first_receive is not None:
        if first_receive > (
            requirements.session_start_utc_ns + requirements.boundary_tolerance_ns
        ):
            reasons.add("RTH_START_NOT_COVERED")
    if last_receive is not None:
        if last_receive < (
            requirements.session_end_utc_ns - requirements.boundary_tolerance_ns
        ):
            reasons.add("RTH_END_NOT_COVERED")
    if maximum_receive_gap > requirements.max_receive_gap_ns:
        reasons.add("RECEIVE_GAP_EXCEEDED")

    control = _read_controls(control_path, requirements, run_ids, reasons)
    certificate = SessionCertificate(
        schema_version=1,
        status="FAIL" if reasons else "PASS",
        reason_codes=tuple(sorted(reasons)),
        expected_instrument=requirements.expected_instrument,
        expected_connection_name=requirements.expected_connection_name,
        recorder_source_sha256=requirements.recorder_source_sha256.lower(),
        session_start_utc_ns=requirements.session_start_utc_ns,
        session_end_utc_ns=requirements.session_end_utc_ns,
        boundary_tolerance_ns=requirements.boundary_tolerance_ns,
        maximum_allowed_receive_gap_ns=requirements.max_receive_gap_ns,
        recorder_run_id=next(iter(run_ids)) if len(run_ids) == 1 else None,
        event_count=event_count,
        first_record_seq=first_record_seq,
        last_record_seq=last_record_seq,
        first_source_time_utc_ns=first_source,
        last_source_time_utc_ns=last_source,
        first_receive_time_utc_ns=first_receive,
        last_receive_time_utc_ns=last_receive,
        maximum_receive_gap_ns=maximum_receive_gap,
        source_time_regressions=regressions,
        invalid_quote_states=invalid_quotes,
        control_event_count=control["count"],
        clean_stop_observed=control["clean_stop"],
        writer_error_count=control["writer_errors"],
        connection_failure_count=control["connection_failures"],
        parts=parts,
        certificate_sha256="",
    )
    digest = hashlib.sha256(_canonical_json(certificate).encode("utf-8")).hexdigest()
    return replace(certificate, certificate_sha256=digest)


def write_certificate(certificate: SessionCertificate, path: Path) -> None:
    payload = certificate.to_dict()
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def verify_certificate(certificate: SessionCertificate) -> None:
    expected = hashlib.sha256(_canonical_json(certificate).encode("utf-8")).hexdigest()
    if certificate.certificate_sha256 != expected:
        raise ValueError("session certificate hash mismatch")


def read_certificate(path: Path) -> SessionCertificate:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["reason_codes"] = tuple(payload.get("reason_codes", ()))
    payload["parts"] = tuple(
        PartEvidence(**part) for part in payload.get("parts", ())
    )
    certificate = SessionCertificate(**payload)
    verify_certificate(certificate)
    return certificate


def _part_evidence(
    paths: tuple[Path, ...], reasons: set[str]
) -> tuple[PartEvidence, ...]:
    if not paths:
        reasons.add("EVENT_MANIFEST_EMPTY")
        return ()
    result: list[PartEvidence] = []
    previous: str | None = None
    for position, path in enumerate(paths):
        try:
            payload = path.read_bytes()
        except OSError:
            reasons.add("EVENT_PART_UNREADABLE")
            continue
        digest = hashlib.sha256(payload).hexdigest()
        result.append(
            PartEvidence(
                manifest_position=position,
                file_name=path.name,
                byte_count=len(payload),
                sha256=digest,
                previous_part_sha256=previous,
            )
        )
        previous = digest
    return tuple(result)


def _read_controls(
    path: Path,
    requirements: CertificationRequirements,
    run_ids: set[str],
    reasons: set[str],
) -> dict[str, int | bool]:
    count = 0
    clean_stop = False
    writer_errors = 0
    connection_failures = 0
    expected_seq = 1
    try:
        stream = Path(path).open("r", encoding="utf-8-sig", newline="")
    except OSError:
        reasons.add("CONTROL_FILE_UNREADABLE")
        return {
            "count": count,
            "clean_stop": clean_stop,
            "writer_errors": writer_errors,
            "connection_failures": connection_failures,
        }

    with stream:
        reader = csv.DictReader(stream)
        fields = frozenset(reader.fieldnames or ())
        if not CONTROL_COLUMNS.issubset(fields):
            reasons.add("CONTROL_SCHEMA_INVALID")
            return {
                "count": count,
                "clean_stop": clean_stop,
                "writer_errors": writer_errors,
                "connection_failures": connection_failures,
            }
        start_count = 0
        expected_connection_observed = False
        for row in reader:
            count += 1
            try:
                seq = int(row["control_seq"])
            except (TypeError, ValueError):
                reasons.add("CONTROL_SEQUENCE_INVALID")
                continue
            if seq != expected_seq:
                reasons.add("CONTROL_SEQUENCE_GAP")
            expected_seq = seq + 1
            if row["schema_version"] != "2":
                reasons.add("CONTROL_SCHEMA_INVALID")
            if run_ids and row["recorder_run_id"] not in run_ids:
                reasons.add("CONTROL_RUN_ID_MISMATCH")
            if row["instrument"] != requirements.expected_instrument:
                reasons.add("CONTROL_INSTRUMENT_MISMATCH")
            control_type = row["control_type"].upper()
            status = row["status"].upper()
            connection_name = row["connection_name"]
            connection_matches = (
                connection_name == requirements.expected_connection_name
            )
            expected_connection_observed = (
                expected_connection_observed or connection_matches
            )
            if control_type == "RUN_START":
                start_count += 1
            elif control_type == "RUN_STOP" and status == "CLEAN":
                clean_stop = True
            elif control_type == "WRITER_ERROR":
                writer_errors += 1
            elif (
                control_type == "CONNECTION"
                and connection_matches
                and status not in {"CONNECTED", "CONNECTING"}
            ):
                connection_failures += 1

    if count == 0:
        reasons.add("CONTROL_FILE_EMPTY")
    if start_count != 1:
        reasons.add("RUN_START_COUNT_NOT_ONE")
    if not expected_connection_observed:
        reasons.add("CONNECTION_IDENTITY_NOT_OBSERVED")
    if not clean_stop:
        reasons.add("CLEAN_STOP_MISSING")
    if writer_errors:
        reasons.add("WRITER_ERROR_OBSERVED")
    if connection_failures:
        reasons.add("CONNECTION_FAILURE_OBSERVED")
    return {
        "count": count,
        "clean_stop": clean_stop,
        "writer_errors": writer_errors,
        "connection_failures": connection_failures,
    }


def _canonical_json(certificate: SessionCertificate) -> str:
    payload = certificate.to_dict()
    payload["certificate_sha256"] = ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CertificationRequirements",
    "PartEvidence",
    "SessionCertificate",
    "certify_session",
    "read_certificate",
    "verify_certificate",
    "write_certificate",
]
