from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_trader.data.session_certification import read_certificate  # noqa: E402


def _request(host: str, port: int, command: str, timeout: float) -> str:
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        with connection.makefile("r", encoding="utf-8", newline="\n") as reader:
            connection.sendall((command + "\n").encode("utf-8"))
            response = reader.readline().strip()
    if not response:
        raise RuntimeError(f"bridge returned no response to {command}")
    return response


def _parse_provenance(response: str) -> dict[str, str]:
    prefix = "PROVENANCE "
    if not response.startswith(prefix):
        raise RuntimeError(f"unexpected provenance response: {response}")
    result: dict[str, str] = {}
    for item in response[len(prefix) :].split(";"):
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise RuntimeError(f"malformed provenance response: {response}")
        result[key] = unquote(value)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare a disarmed Sim bridge's read-only provenance with a certificate."
    )
    parser.add_argument("certificate", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5570)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()

    certificate = read_certificate(args.certificate)
    version = _request(args.host, args.port, "VERSION", args.timeout)
    safety = _request(args.host, args.port, "SAFETY", args.timeout)
    if not version.endswith(" DISARMED"):
        raise RuntimeError(f"bridge is not disarmed: {version}")
    if safety != "SAFETY SIM_ONLY DISARMED":
        raise RuntimeError(f"bridge is not locked to disarmed simulation: {safety}")
    provenance = _parse_provenance(
        _request(args.host, args.port, "PROVENANCE", args.timeout)
    )
    expected = {
        "connection": certificate.expected_connection_name,
        "provider": certificate.expected_provider,
        "feed_family": certificate.expected_feed_family,
        "instrument": certificate.expected_instrument,
        "price_status": "Connected",
    }
    mismatches = {
        key: (expected_value, provenance.get(key))
        for key, expected_value in expected.items()
        if provenance.get(key) != expected_value
    }
    if mismatches:
        for key, values in sorted(mismatches.items()):
            print(f"MISMATCH {key}: certificate={values[0]!r} bridge={values[1]!r}")
        return 1
    print(f"PASS {version}")
    print(f"PASS {safety}")
    print("PASS recorder and Sim101 bridge use the same declared feed identity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
