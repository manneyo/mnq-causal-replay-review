from __future__ import annotations

import argparse
import csv
import json
import tempfile
import tracemalloc
from pathlib import Path

from video_trader.data.ninjatrader_events import iter_ninjatrader_event_exports


def benchmark(rows: int) -> dict[str, int]:
    with tempfile.TemporaryDirectory(prefix="mnq-replay-") as directory:
        path = Path(directory) / "synthetic_events.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "timestamp_utc_ns",
                    "instrument",
                    "event_type",
                    "price",
                    "volume",
                    "state",
                ]
            )
            for sequence in range(rows):
                event_type = "BID" if sequence % 2 == 0 else "ASK"
                price = "100.00" if event_type == "BID" else "100.25"
                writer.writerow(
                    [sequence + 1, "MNQ TEST", event_type, price, 1, "Realtime"]
                )

        tracemalloc.start()
        observed = sum(1 for _ in iter_ninjatrader_event_exports([path]))
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    return {
        "expected_rows": rows,
        "observed_rows": observed,
        "current_bytes": current_bytes,
        "peak_bytes": peak_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure Python allocations while streaming a synthetic CSV."
    )
    parser.add_argument("--rows", type=int, default=100_000)
    args = parser.parse_args()
    if args.rows < 1:
        parser.error("--rows must be positive")
    result = benchmark(args.rows)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["observed_rows"] == result["expected_rows"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
