from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path


def inspect(path: Path) -> dict[str, object]:
    counts: Counter[str] = Counter()
    rows = 0
    malformed = 0
    timestamp_regressions: list[tuple[int, int, int]] = []
    repeated_payloads: list[tuple[int, int]] = []
    previous_timestamp: int | None = None
    previous_payload: tuple[str, ...] | None = None

    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {
            "timestamp_utc_ns",
            "instrument",
            "event_type",
            "price",
            "volume",
            "state",
        }
        missing = sorted(required.difference(reader.fieldnames or []))
        if missing:
            raise ValueError(f"missing columns: {missing}")

        for physical_row, row in enumerate(reader, start=2):
            rows += 1
            try:
                timestamp = int(row["timestamp_utc_ns"])
                float(row["price"])
                float(row["volume"])
            except (TypeError, ValueError):
                malformed += 1
                continue

            event_type = str(row["event_type"]).upper()
            counts[event_type] += 1
            payload = tuple(str(row[column]) for column in reader.fieldnames or [])

            if previous_timestamp is not None and timestamp < previous_timestamp:
                timestamp_regressions.append(
                    (physical_row, previous_timestamp, timestamp)
                )
            if previous_payload == payload:
                repeated_payloads.append((physical_row - 1, physical_row))

            previous_timestamp = timestamp
            previous_payload = payload

    return {
        "rows": rows,
        "malformed": malformed,
        "event_counts": dict(sorted(counts.items())),
        "timestamp_regressions": timestamp_regressions,
        "repeated_payloads": repeated_payloads,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect a NinjaTrader event CSV without reordering it."
    )
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return a failure code when quality observations are found",
    )
    args = parser.parse_args()
    result = inspect(args.path)

    for key, value in result.items():
        print(f"{key}={value}")

    has_findings = bool(
        result["malformed"]
        or result["timestamp_regressions"]
        or result["repeated_payloads"]
    )
    return 1 if args.strict and has_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

