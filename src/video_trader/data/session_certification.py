from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable

from .ninjatrader_events import EventReplayError, iter_ninjatrader_event_exports


CONTROL_SCHEMA_VERSION = 3
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
        "provider",
        "feed_family",
        "details",
    }
)
RUN_STOP_DETAIL_FIELDS = frozenset(
    {
        "final_record_seq",
        "event_rows",
        "final_event_part",
        "record_event_stream",
        "depth_events_dropped",
        "writer_error_count",
    }
)


@dataclass(frozen=True, slots=True)
class CertificationRequirements:
    expected_instrument: str
    expected_connection_name: str
    expected_provider: str
    expected_feed_family: str
    recorder_source_sha256: str
    session_start_utc_ns: int
    session_end_utc_ns: int
    boundary_tolerance_ns: int = 60_000_000_000
    max_receive_gap_ns: int = 5_000_000_000

    def __post_init__(self) -> None:
        required = {
            "expected_instrument": self.expected_instrument,
            "expected_connection_name": self.expected_connection_name,
            "expected_provider": self.expected_provider,
            "expected_feed_family": self.expected_feed_family,
        }
        for name, value in required.items():
            if not value.strip():
                raise ValueError(f"{name} cannot be empty")
            if value.strip().upper() in {"UNKNOWN", "UNDECLARED", "UNRESOLVED"}:
                raise ValueError(f"{name} must identify a resolved source")
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
class _ControlAudit:
    count: int = 0
    clean_stop: bool = False
    writer_errors: int = 0
    connection_failures: int = 0
    out_of_session_connection_failures: int = 0
    provenance_confirmed: bool = False
    observed_connection_name: str | None = None
    observed_provider: str | None = None
    observed_feed_family: str | None = None
    run_stop_final_record_seq: int | None = None
    run_stop_event_rows: int | None = None
    run_stop_final_event_part: int | None = None


@dataclass(frozen=True, slots=True)
class SessionCertificate:
    schema_version: int
    status: str
    reason_codes: tuple[str, ...]
    expected_instrument: str
    expected_connection_name: str
    expected_provider: str
    expected_feed_family: str
    observed_connection_name: str | None
    observed_provider: str | None
    observed_feed_family: str | None
    provenance_confirmed: bool
    recorder_source_sha256: str
    session_start_utc_ns: int
    session_end_utc_ns: int
    boundary_tolerance_ns: int
    maximum_allowed_receive_gap_ns: int
    recorder_run_id: str | None
    event_count: int
    session_event_count: int
    event_part_count: int
    first_record_seq: int | None
    last_record_seq: int | None
    highest_file_part: int | None
    first_source_time_utc_ns: int | None
    last_source_time_utc_ns: int | None
    first_receive_time_utc_ns: int | None
    last_receive_time_utc_ns: int | None
    first_session_receive_time_utc_ns: int | None
    last_session_receive_time_utc_ns: int | None
    maximum_receive_gap_ns: int
    maximum_run_receive_gap_ns: int
    source_time_regressions: int
    invalid_quote_states: int
    control_event_count: int
    clean_stop_observed: bool
    writer_error_count: int
    connection_failure_count: int
    out_of_session_connection_failure_count: int
    run_stop_final_record_seq: int | None
    run_stop_event_rows: int | None
    run_stop_final_event_part: int | None
    control_file_name: str
    control_file_byte_count: int
    control_file_sha256: str | None
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
    control_file_name, control_file_byte_count, control_file_sha256 = (
        _control_file_evidence(Path(control_path), reasons)
    )

    run_ids: set[str] = set()
    file_parts: set[int] = set()
    event_count = 0
    session_event_count = 0
    first_record_seq: int | None = None
    last_record_seq: int | None = None
    first_source: int | None = None
    last_source: int | None = None
    first_receive: int | None = None
    last_receive: int | None = None
    previous_receive: int | None = None
    previous_session_receive: int | None = None
    first_session_receive: int | None = None
    last_session_receive: int | None = None
    maximum_receive_gap = 0
    maximum_run_receive_gap = 0
    regressions = 0
    invalid_quotes = 0

    try:
        for event in iter_ninjatrader_event_exports(paths):
            event_count += 1
            if event.schema_version != 2 or event.recorder_run_id is None:
                reasons.add("LEGACY_OR_UNIDENTIFIED_EVENT")
            else:
                run_ids.add(event.recorder_run_id)
                file_parts.add(event.file_part)
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
                        maximum_run_receive_gap = max(
                            maximum_run_receive_gap, receive - previous_receive
                        )
                previous_receive = receive
                last_receive = receive
                if (
                    requirements.session_start_utc_ns
                    <= receive
                    <= requirements.session_end_utc_ns
                ):
                    session_event_count += 1
                    if first_session_receive is None:
                        first_session_receive = receive
                    if previous_session_receive is not None:
                        maximum_receive_gap = max(
                            maximum_receive_gap,
                            receive - previous_session_receive,
                        )
                    previous_session_receive = receive
                    last_session_receive = receive
            if first_record_seq is None:
                first_record_seq = event.record_seq
                first_source = event.timestamp_utc_ns
            last_record_seq = event.record_seq
            last_source = event.timestamp_utc_ns
            regressions += int(event.source_time_regression)
            invalid_quotes += int(event.quote_status in {"LOCKED", "CROSSED"})
    except (EventReplayError, ValueError, OSError, csv.Error):
        reasons.add("EVENT_REPLAY_ERROR")

    if event_count == 0:
        reasons.add("NO_EVENTS")
    if len(run_ids) != 1:
        reasons.add("RUN_ID_COUNT_NOT_ONE")
    if first_session_receive is None or first_session_receive > (
        requirements.session_start_utc_ns + requirements.boundary_tolerance_ns
    ):
        reasons.add("RTH_START_NOT_COVERED")
    if last_session_receive is None or last_session_receive < (
        requirements.session_end_utc_ns - requirements.boundary_tolerance_ns
    ):
        reasons.add("RTH_END_NOT_COVERED")
    if maximum_receive_gap > requirements.max_receive_gap_ns:
        reasons.add("RECEIVE_GAP_EXCEEDED")

    highest_file_part = max(file_parts) if file_parts else None
    control = _read_controls(
        control_path,
        requirements,
        run_ids,
        reasons,
        first_event_receive_ns=first_receive,
        last_event_receive_ns=last_receive,
        last_record_seq=last_record_seq,
        event_count=event_count,
        highest_file_part=highest_file_part,
        event_part_count=len(parts),
    )
    certificate = SessionCertificate(
        schema_version=3,
        status="FAIL" if reasons else "PASS",
        reason_codes=tuple(sorted(reasons)),
        expected_instrument=requirements.expected_instrument,
        expected_connection_name=requirements.expected_connection_name,
        expected_provider=requirements.expected_provider,
        expected_feed_family=requirements.expected_feed_family,
        observed_connection_name=control.observed_connection_name,
        observed_provider=control.observed_provider,
        observed_feed_family=control.observed_feed_family,
        provenance_confirmed=control.provenance_confirmed,
        recorder_source_sha256=requirements.recorder_source_sha256.lower(),
        session_start_utc_ns=requirements.session_start_utc_ns,
        session_end_utc_ns=requirements.session_end_utc_ns,
        boundary_tolerance_ns=requirements.boundary_tolerance_ns,
        maximum_allowed_receive_gap_ns=requirements.max_receive_gap_ns,
        recorder_run_id=next(iter(run_ids)) if len(run_ids) == 1 else None,
        event_count=event_count,
        session_event_count=session_event_count,
        event_part_count=len(parts),
        first_record_seq=first_record_seq,
        last_record_seq=last_record_seq,
        highest_file_part=highest_file_part,
        first_source_time_utc_ns=first_source,
        last_source_time_utc_ns=last_source,
        first_receive_time_utc_ns=first_receive,
        last_receive_time_utc_ns=last_receive,
        first_session_receive_time_utc_ns=first_session_receive,
        last_session_receive_time_utc_ns=last_session_receive,
        maximum_receive_gap_ns=maximum_receive_gap,
        maximum_run_receive_gap_ns=maximum_run_receive_gap,
        source_time_regressions=regressions,
        invalid_quote_states=invalid_quotes,
        control_event_count=control.count,
        clean_stop_observed=control.clean_stop,
        writer_error_count=control.writer_errors,
        connection_failure_count=control.connection_failures,
        out_of_session_connection_failure_count=(
            control.out_of_session_connection_failures
        ),
        run_stop_final_record_seq=control.run_stop_final_record_seq,
        run_stop_event_rows=control.run_stop_event_rows,
        run_stop_final_event_part=control.run_stop_final_event_part,
        control_file_name=control_file_name,
        control_file_byte_count=control_file_byte_count,
        control_file_sha256=control_file_sha256,
        parts=parts,
        certificate_sha256="",
    )
    digest = hashlib.sha256(_canonical_json(certificate).encode("utf-8")).hexdigest()
    return replace(certificate, certificate_sha256=digest)


def write_certificate(certificate: SessionCertificate, path: Path) -> None:
    payload = certificate.to_dict()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
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
    payload["parts"] = tuple(PartEvidence(**part) for part in payload.get("parts", ()))
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
    resolved_seen: set[Path] = set()
    for position, path in enumerate(paths):
        resolved = path.resolve()
        if resolved in resolved_seen:
            reasons.add("EVENT_PART_DUPLICATED_IN_MANIFEST")
        resolved_seen.add(resolved)
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
    if len(result) != len(paths):
        reasons.add("EVENT_MANIFEST_INCOMPLETE")
    return tuple(result)


def _control_file_evidence(
    path: Path, reasons: set[str]
) -> tuple[str, int, str | None]:
    try:
        payload = path.read_bytes()
    except OSError:
        reasons.add("CONTROL_FILE_UNREADABLE")
        return path.name, 0, None
    return path.name, len(payload), hashlib.sha256(payload).hexdigest()


def _read_controls(
    path: Path,
    requirements: CertificationRequirements,
    run_ids: set[str],
    reasons: set[str],
    *,
    first_event_receive_ns: int | None,
    last_event_receive_ns: int | None,
    last_record_seq: int | None,
    event_count: int,
    highest_file_part: int | None,
    event_part_count: int,
) -> _ControlAudit:
    count = 0
    writer_errors = 0
    connection_failures = 0
    out_of_session_connection_failures = 0
    expected_seq = 1
    previous_receive: int | None = None
    start_count = 0
    stop_count = 0
    clean_stop = False
    connected_identity_observed = False
    start_identity_matches = False
    stop_identity_matches = False
    observed_connection_name: str | None = None
    observed_provider: str | None = None
    observed_feed_family: str | None = None
    stop_details: dict[str, str] = {}
    stop_receive: int | None = None
    first_control_type: str | None = None
    last_control_type: str | None = None

    try:
        stream = Path(path).open("r", encoding="utf-8-sig", newline="")
    except OSError:
        reasons.add("CONTROL_FILE_UNREADABLE")
        return _ControlAudit()

    try:
        with stream:
            reader = csv.DictReader(stream)
            fields = frozenset(reader.fieldnames or ())
            if fields != CONTROL_COLUMNS:
                reasons.add("CONTROL_SCHEMA_INVALID")
                return _ControlAudit()
            for row in reader:
                count += 1
                if None in row or any(row.get(column) is None for column in CONTROL_COLUMNS):
                    reasons.add("CONTROL_ROW_MALFORMED")
                    continue
                try:
                    seq = int(row["control_seq"])
                    receive = int(row["receive_time_utc_ns"])
                    schema_version = int(row["schema_version"])
                except (TypeError, ValueError):
                    reasons.add("CONTROL_ROW_MALFORMED")
                    continue
                if seq != expected_seq:
                    reasons.add("CONTROL_SEQUENCE_GAP")
                expected_seq = seq + 1
                if receive < 0:
                    reasons.add("CONTROL_ROW_MALFORMED")
                if previous_receive is not None and receive < previous_receive:
                    reasons.add("CONTROL_TIME_REGRESSION")
                previous_receive = receive
                if schema_version != CONTROL_SCHEMA_VERSION:
                    reasons.add("CONTROL_SCHEMA_INVALID")
                if run_ids and row["recorder_run_id"] not in run_ids:
                    reasons.add("CONTROL_RUN_ID_MISMATCH")
                if row["instrument"] != requirements.expected_instrument:
                    reasons.add("CONTROL_INSTRUMENT_MISMATCH")

                control_type = row["control_type"].strip().upper()
                status = row["status"].strip().upper()
                connection_name = row["connection_name"].strip()
                provider = row["provider"].strip()
                feed_family = row["feed_family"].strip()
                if not all((control_type, status, connection_name, provider, feed_family)):
                    reasons.add("CONTROL_ROW_MALFORMED")
                if first_control_type is None:
                    first_control_type = control_type
                last_control_type = control_type

                connection_matches = connection_name == requirements.expected_connection_name
                provider_matches = provider == requirements.expected_provider
                feed_matches = feed_family == requirements.expected_feed_family
                full_identity_matches = connection_matches and provider_matches and feed_matches
                if connection_matches:
                    observed_connection_name = connection_name
                    observed_provider = provider
                    observed_feed_family = feed_family
                    if not provider_matches:
                        reasons.add("PROVIDER_MISMATCH_OBSERVED")
                    if not feed_matches:
                        reasons.add("FEED_FAMILY_MISMATCH_OBSERVED")

                if control_type == "RUN_START":
                    start_count += 1
                    start_identity_matches = full_identity_matches
                    if status != "STARTED":
                        reasons.add("RUN_START_STATUS_INVALID")
                    if first_event_receive_ns is not None and receive > first_event_receive_ns:
                        reasons.add("RUN_START_AFTER_FIRST_EVENT")
                elif control_type == "RUN_STOP":
                    stop_count += 1
                    stop_identity_matches = full_identity_matches
                    clean_stop = status == "CLEAN"
                    stop_receive = receive
                    stop_details = _parse_details(row["details"], reasons)
                elif control_type == "WRITER_ERROR":
                    writer_errors += 1
                elif control_type == "CONNECTION" and full_identity_matches:
                    if status == "CONNECTED":
                        connected_identity_observed = True
                    elif status != "CONNECTING":
                        if (
                            requirements.session_start_utc_ns
                            <= receive
                            <= requirements.session_end_utc_ns
                        ):
                            connection_failures += 1
                        else:
                            out_of_session_connection_failures += 1
    except (csv.Error, OSError, UnicodeError):
        reasons.add("CONTROL_REPLAY_ERROR")

    if count == 0:
        reasons.add("CONTROL_FILE_EMPTY")
    if first_control_type != "RUN_START":
        reasons.add("RUN_START_NOT_FIRST_CONTROL")
    if last_control_type != "RUN_STOP":
        reasons.add("RUN_STOP_NOT_FINAL_CONTROL")
    if start_count != 1:
        reasons.add("RUN_START_COUNT_NOT_ONE")
    if stop_count != 1:
        reasons.add("RUN_STOP_COUNT_NOT_ONE")
    if not start_identity_matches:
        reasons.add("RUN_START_PROVENANCE_MISMATCH")
    if not stop_identity_matches:
        reasons.add("RUN_STOP_PROVENANCE_MISMATCH")
    if not connected_identity_observed:
        reasons.add("CONNECTED_FEED_IDENTITY_NOT_OBSERVED")
    if not clean_stop:
        reasons.add("CLEAN_STOP_MISSING")
    if writer_errors:
        reasons.add("WRITER_ERROR_OBSERVED")
    if connection_failures:
        reasons.add("CONNECTION_FAILURE_OBSERVED")
    if stop_receive is not None and last_event_receive_ns is not None:
        if stop_receive < last_event_receive_ns:
            reasons.add("RUN_STOP_BEFORE_FINAL_EVENT")

    final_record_seq = _detail_int(stop_details, "final_record_seq", reasons)
    stop_event_rows = _detail_int(stop_details, "event_rows", reasons)
    final_event_part = _detail_int(stop_details, "final_event_part", reasons)
    dropped_depth = _detail_int(stop_details, "depth_events_dropped", reasons)
    reported_writer_errors = _detail_int(stop_details, "writer_error_count", reasons)
    if stop_details.get("record_event_stream", "").lower() != "true":
        reasons.add("EVENT_STREAM_NOT_ATTESTED")
    if final_record_seq != last_record_seq:
        reasons.add("RUN_STOP_RECORD_SEQUENCE_MISMATCH")
    if stop_event_rows != event_count:
        reasons.add("RUN_STOP_EVENT_COUNT_MISMATCH")
    if final_event_part != highest_file_part:
        reasons.add("RUN_STOP_FINAL_PART_MISMATCH")
    if final_event_part is not None and event_part_count != final_event_part + 1:
        reasons.add("EVENT_PART_COUNT_MISMATCH")
    if dropped_depth not in {None, 0}:
        reasons.add("DEPTH_EVENTS_DROPPED")
    if reported_writer_errors != writer_errors:
        reasons.add("RUN_STOP_WRITER_ERROR_COUNT_MISMATCH")

    provenance_confirmed = (
        start_count == 1
        and stop_count == 1
        and start_identity_matches
        and stop_identity_matches
        and connected_identity_observed
    )
    return _ControlAudit(
        count=count,
        clean_stop=clean_stop,
        writer_errors=writer_errors,
        connection_failures=connection_failures,
        out_of_session_connection_failures=out_of_session_connection_failures,
        provenance_confirmed=provenance_confirmed,
        observed_connection_name=observed_connection_name,
        observed_provider=observed_provider,
        observed_feed_family=observed_feed_family,
        run_stop_final_record_seq=final_record_seq,
        run_stop_event_rows=stop_event_rows,
        run_stop_final_event_part=final_event_part,
    )


def _parse_details(value: str, reasons: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in value.split(";"):
        if not item:
            continue
        key, separator, field_value = item.partition("=")
        if not separator or not key or key in result:
            reasons.add("RUN_STOP_DETAILS_MALFORMED")
            continue
        result[key] = field_value
    if not RUN_STOP_DETAIL_FIELDS.issubset(result):
        reasons.add("RUN_STOP_DETAILS_INCOMPLETE")
    return result


def _detail_int(
    details: dict[str, str], key: str, reasons: set[str]
) -> int | None:
    try:
        value = int(details[key])
    except (KeyError, TypeError, ValueError):
        reasons.add("RUN_STOP_DETAILS_MALFORMED")
        return None
    if value < 0:
        reasons.add("RUN_STOP_DETAILS_MALFORMED")
        return None
    return value


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
