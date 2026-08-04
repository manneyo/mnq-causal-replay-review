from __future__ import annotations

import csv
from pathlib import Path


def test_synthetic_fixture_documents_both_known_ordering_cases() -> None:
    path = Path(__file__).parents[1] / "samples" / "synthetic_mnq_events.csv"
    with path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))

    timestamps = [int(row["timestamp_utc_ns"]) for row in rows]
    assert any(right < left for left, right in zip(timestamps, timestamps[1:]))
    assert any(left == right for left, right in zip(rows, rows[1:]))
    assert {row["event_type"] for row in rows} == {"BID", "ASK", "TRADE"}

