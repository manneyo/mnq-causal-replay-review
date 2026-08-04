from __future__ import annotations

import pandas as pd

from video_trader.data.ninjatrader_events import ninjatrader_events_to_tbbo_proxy


def test_trade_proxy_uses_only_previously_observed_quotes() -> None:
    index = pd.to_datetime(
        [
            "2026-08-02T22:00:00.000Z",
            "2026-08-02T22:00:00.100Z",
            "2026-08-02T22:00:00.200Z",
            "2026-08-02T22:00:00.300Z",
            "2026-08-02T22:00:00.400Z",
        ]
    )
    events = pd.DataFrame(
        {
            "instrument": ["MNQ TEST"] * 5,
            "event_type": ["TRADE", "BID", "ASK", "TRADE", "ASK"],
            "price": [100.5, 100.0, 101.0, 101.0, 101.25],
            "volume": [1, 3, 4, 2, 5],
            "state": ["Realtime"] * 5,
        },
        index=index,
    )

    proxy = ninjatrader_events_to_tbbo_proxy(events)

    assert len(proxy) == 1
    assert float(proxy.iloc[0]["bid_px_00"]) == 100.0
    assert float(proxy.iloc[0]["ask_px_00"]) == 101.0
    assert proxy.iloc[0]["side"] == "B"


def test_trade_proxy_does_not_sort_regressing_source_timestamps() -> None:
    index = pd.to_datetime(
        [
            "2026-08-02T22:00:00.300Z",
            "2026-08-02T22:00:00.200Z",
            "2026-08-02T22:00:00.100Z",
            "2026-08-02T22:00:00.400Z",
        ]
    )
    events = pd.DataFrame(
        {
            "event_id": ["one", "two", "three", "four"],
            "instrument": ["MNQ TEST"] * 4,
            "event_type": ["BID", "ASK", "TRADE", "TRADE"],
            "price": [100.0, 100.25, 100.25, 100.0],
            "volume": [3, 4, 1, 1],
            "state": ["Realtime"] * 4,
        },
        index=index,
    )

    proxy = ninjatrader_events_to_tbbo_proxy(events)

    assert proxy["source_event_id"].tolist() == ["three", "four"]
    assert proxy.index.tolist() == [index[2], index[3]]
