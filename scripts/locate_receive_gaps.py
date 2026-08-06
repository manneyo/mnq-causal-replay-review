from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path


def _stamp(value: int | None) -> str:
    if value is None:
        return "NONE"
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).astimezone().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Locate maximum receive-time gaps without loading events into memory.")
    parser.add_argument("events", nargs="+", type=Path)
    parser.add_argument("--session-start-ns", required=True, type=int)
    parser.add_argument("--session-end-ns", required=True, type=int)
    args = parser.parse_args()

    previous: int | None = None
    maximum = (-1, None, None)
    session_maximum = (-1, None, None)
    for path in args.events:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                current = int(row["receive_time_utc_ns"])
                if previous is not None:
                    gap = current - previous
                    if gap > maximum[0]:
                        maximum = (gap, previous, current)
                    if (
                        previous >= args.session_start_ns
                        and current <= args.session_end_ns
                        and gap > session_maximum[0]
                    ):
                        session_maximum = (gap, previous, current)
                previous = current

    print("GLOBAL", maximum[0], _stamp(maximum[1]), _stamp(maximum[2]))
    print("SESSION", session_maximum[0], _stamp(session_maximum[1]), _stamp(session_maximum[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
