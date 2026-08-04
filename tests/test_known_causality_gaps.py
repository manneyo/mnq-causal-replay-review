from __future__ import annotations

from pathlib import Path

import pandas as pd

from video_trader.data.ninjatrader_events import load_ninjatrader_event_exports


def _write_events(path: Path, rows: list[dict[str, object]]) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


def test_loader_preserves_physical_order_when_source_time_regresses(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.csv"
    _write_events(
        path,
        [
            {
                "timestamp_utc_ns": 200,
                "instrument": "MNQ TEST",
                "event_type": "BID",
                "price": 100.0,
                "volume": 1,
                "state": "Realtime",
            },
            {
                "timestamp_utc_ns": 100,
                "instrument": "MNQ TEST",
                "event_type": "ASK",
                "price": 100.25,
                "volume": 2,
                "state": "Realtime",
            },
        ],
    )

    events = load_ninjatrader_event_exports([path])

    assert events["event_type"].tolist() == ["BID", "ASK"]
    assert events["source_time_regression"].tolist() == [False, True]
    assert events["physical_row"].tolist() == [2, 3]


def test_loader_preserves_identical_physical_quote_callbacks(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    repeated = {
        "timestamp_utc_ns": 100,
        "instrument": "MNQ TEST",
        "event_type": "ASK",
        "price": 100.25,
        "volume": 2,
        "state": "Realtime",
    }
    _write_events(path, [repeated, repeated.copy()])

    events = load_ninjatrader_event_exports([path])

    assert len(events) == 2
    assert events["event_id"].is_unique
    assert events["event_id"].tolist() == [
        "legacy:000000:000000000002",
        "legacy:000000:000000000003",
    ]
