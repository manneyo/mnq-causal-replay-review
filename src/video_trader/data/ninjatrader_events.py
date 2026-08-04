from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


REQUIRED_EVENT_COLUMNS = {
    "timestamp_utc_ns",
    "instrument",
    "event_type",
    "price",
    "volume",
    "state",
}


def load_ninjatrader_event_exports(paths: Iterable[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in paths:
        frame = pd.read_csv(path)
        missing = sorted(REQUIRED_EVENT_COLUMNS.difference(frame.columns))
        if missing:
            raise ValueError(f"{path} is missing NinjaTrader event columns: {missing}")
        frame["source_file"] = path.name
        frames.append(frame)
    if not frames:
        raise ValueError("at least one NinjaTrader event export is required")
    events = pd.concat(frames, ignore_index=True)
    events["timestamp_utc_ns"] = pd.to_numeric(
        events["timestamp_utc_ns"], errors="raise"
    ).astype(np.int64)
    events["price"] = pd.to_numeric(events["price"], errors="coerce")
    events["volume"] = pd.to_numeric(events["volume"], errors="coerce")
    events = events.dropna(subset=["price", "volume"])
    events["event_type"] = events["event_type"].astype(str).str.upper()
    events = events[events["event_type"].isin({"TRADE", "BID", "ASK"})]
    events = events.drop_duplicates(
        ["timestamp_utc_ns", "instrument", "event_type", "price", "volume"],
        keep="first",
    ).sort_values("timestamp_utc_ns", kind="stable")
    events.index = pd.to_datetime(events.pop("timestamp_utc_ns"), unit="ns", utc=True)
    events.index.name = "ts_event"
    return events


def ninjatrader_events_to_tbbo_proxy(events: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(events.index, pd.DatetimeIndex):
        raise ValueError("NinjaTrader events must use a DatetimeIndex")
    work = events.sort_index(kind="stable").copy()
    instruments = work["instrument"].astype(str)
    work["bid"] = work["price"].where(work["event_type"].eq("BID"))
    work["ask"] = work["price"].where(work["event_type"].eq("ASK"))
    work["bid_size"] = work["volume"].where(work["event_type"].eq("BID"))
    work["ask_size"] = work["volume"].where(work["event_type"].eq("ASK"))
    for column in ("bid", "ask", "bid_size", "ask_size"):
        work[column] = work[column].groupby(instruments, sort=False).ffill()
    trades = work[
        work["event_type"].eq("TRADE")
        & work["bid"].notna()
        & work["ask"].notna()
        & (work["ask"] > work["bid"])
        & (work["volume"] > 0.0)
    ].copy()
    midpoint = (trades["bid"] + trades["ask"]) / 2.0
    side = np.where(
        trades["price"] >= trades["ask"],
        "B",
        np.where(trades["price"] <= trades["bid"], "A", np.where(trades["price"] >= midpoint, "B", "A")),
    )
    proxy = pd.DataFrame(
        {
            "price": trades["price"].to_numpy(dtype=np.float64),
            "size": trades["volume"].to_numpy(dtype=np.float64),
            "side": side,
            "bid_px_00": trades["bid"].to_numpy(dtype=np.float64),
            "ask_px_00": trades["ask"].to_numpy(dtype=np.float64),
            "bid_sz_00": trades["bid_size"].to_numpy(dtype=np.float64),
            "ask_sz_00": trades["ask_size"].to_numpy(dtype=np.float64),
            "bid_ct_00": np.ones(len(trades), dtype=np.int64),
            "ask_ct_00": np.ones(len(trades), dtype=np.int64),
            "contract_id": trades["instrument"].astype(str).to_numpy(),
            "market": "MNQ",
        },
        index=trades.index,
    )
    proxy.index.name = "ts_event"
    return proxy


__all__ = ["load_ninjatrader_event_exports", "ninjatrader_events_to_tbbo_proxy"]
