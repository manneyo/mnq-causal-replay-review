from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_trader.data.session_certification import (  # noqa: E402
    CertificationRequirements,
    certify_session,
    write_certificate,
)


def _utc_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a UTC offset")
    return int(parsed.timestamp() * 1_000_000_000)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a deterministic fail-closed NinjaTrader session certificate."
    )
    parser.add_argument("events", nargs="+", type=Path, help="ordered event CSV parts")
    parser.add_argument("--controls", required=True, type=Path)
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--connection", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--feed-family", required=True)
    parser.add_argument("--session-start", required=True, type=_utc_ns)
    parser.add_argument("--session-end", required=True, type=_utc_ns)
    parser.add_argument("--recorder-source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--boundary-tolerance-seconds", type=float, default=60.0)
    parser.add_argument("--max-receive-gap-seconds", type=float, default=5.0)
    args = parser.parse_args()

    source_hash = hashlib.sha256(args.recorder_source.read_bytes()).hexdigest()
    requirements = CertificationRequirements(
        expected_instrument=args.instrument,
        expected_connection_name=args.connection,
        expected_provider=args.provider,
        expected_feed_family=args.feed_family,
        recorder_source_sha256=source_hash,
        session_start_utc_ns=args.session_start,
        session_end_utc_ns=args.session_end,
        boundary_tolerance_ns=int(args.boundary_tolerance_seconds * 1_000_000_000),
        max_receive_gap_ns=int(args.max_receive_gap_seconds * 1_000_000_000),
    )
    certificate = certify_session(args.events, args.controls, requirements)
    write_certificate(certificate, args.output)
    print(f"{certificate.status} {certificate.certificate_sha256} {args.output}")
    if certificate.reason_codes:
        print("reason_codes=" + ",".join(certificate.reason_codes))
    return 0 if certificate.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
