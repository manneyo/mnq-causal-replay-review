from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_trader.data.session_certification import read_certificate  # noqa: E402


RTH_DURATION_NS = int(6.5 * 60 * 60 * 1_000_000_000)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed inventory audit for locked MNQ RTH certificates."
    )
    parser.add_argument("certificates", nargs="+", type=Path)
    parser.add_argument("--minimum", type=int, default=60)
    args = parser.parse_args()

    errors: list[str] = []
    sessions: set[tuple[int, int]] = set()
    run_ids: set[str] = set()
    identity: tuple[str, str, str, str, str] | None = None
    for path in args.certificates:
        try:
            certificate = read_certificate(path)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{path}: unreadable or invalid certificate: {exc}")
            continue
        if certificate.status != "PASS" or certificate.reason_codes:
            errors.append(f"{path}: certificate is not PASS")
        if not certificate.clean_stop_observed or not certificate.provenance_confirmed:
            errors.append(f"{path}: lifecycle or provenance is not confirmed")
        session = (certificate.session_start_utc_ns, certificate.session_end_utc_ns)
        if session in sessions:
            errors.append(f"{path}: duplicate session boundary")
        sessions.add(session)
        if certificate.session_end_utc_ns - certificate.session_start_utc_ns != RTH_DURATION_NS:
            errors.append(f"{path}: declared session is not a 6.5-hour RTH window")
        if not certificate.recorder_run_id or certificate.recorder_run_id in run_ids:
            errors.append(f"{path}: missing or duplicate recorder_run_id")
        if certificate.recorder_run_id:
            run_ids.add(certificate.recorder_run_id)
        current_identity = (
            certificate.expected_instrument,
            certificate.expected_connection_name,
            certificate.expected_provider,
            certificate.expected_feed_family,
            certificate.recorder_source_sha256,
        )
        if identity is None:
            identity = current_identity
        elif current_identity != identity:
            errors.append(f"{path}: feed, instrument, or recorder source differs")

    if len(sessions) < args.minimum:
        errors.append(f"only {len(sessions)} unique sessions; need at least {args.minimum}")
    if errors:
        for error in errors:
            print("FAIL " + error)
        return 1
    print(f"PASS {len(sessions)} unique certified MNQ RTH sessions")
    print("Inventory integrity passes; non-use for tuning remains a process attestation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
