from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from video_trader.event_diagnostic_replay import (
    EventReplaySpec,
    ReplaySignal,
    aggregate_quote_buckets,
    replay_signals,
)


HEADER = [
    "schema_version",
    "recorder_run_id",
    "file_part",
    "record_seq",
    "event_id",
    "timestamp_utc_ns",
    "receive_time_utc_ns",
    "instrument",
    "event_type",
    "price",
    "volume",
    "state",
]


class EventDiagnosticReplayTests(unittest.TestCase):
    def test_compaction_uses_receive_order_and_preserves_later_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            rows = [
                (1, 200, 100, "BID", 100.0, 2),
                (2, 100, 110, "ASK", 100.25, 3),
                (3, 300, 400_000_000, "BID", 100.25, 4),
                (4, 400, 410_000_000, "ASK", 100.50, 5),
            ]
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(HEADER)
                for seq, source, receive, kind, price, volume in rows:
                    writer.writerow(
                        [2, "run", 0, seq, f"run:{seq:020d}", source, receive,
                         "MNQ 09-26", kind, price, volume, "Realtime"]
                    )
            frame, audit = aggregate_quote_buckets(
                [path],
                pd.Timestamp(0, unit="ns", tz="UTC"),
                pd.Timestamp(1_000_000_000, unit="ns", tz="UTC"),
            )
            self.assertEqual(audit.last_record_seq, 4)
            self.assertEqual(len(frame), 2)
            self.assertEqual(frame["last_bid"].tolist(), [100.0, 100.25])
            self.assertEqual(frame["last_ask"].tolist(), [100.25, 100.5])

    def test_replay_fills_entry_and_exit_on_later_snapshots(self) -> None:
        index = pd.to_datetime(
            [
                "2026-08-05T13:30:00.000Z",
                "2026-08-05T13:30:00.250Z",
                "2026-08-05T13:30:00.500Z",
                "2026-08-05T13:30:00.750Z",
            ]
        )
        quotes = pd.DataFrame(
            {
                "last_bid": [100.0, 100.0, 107.0, 107.0],
                "last_ask": [100.25, 100.25, 107.25, 107.25],
                "last_record_seq": [1, 2, 3, 4],
            },
            index=index,
        )
        trades = replay_signals(
            quotes,
            [ReplaySignal(index[0], 1, 20.0)],
            "candidate_1",
            250,
            60,
            pd.Timestamp("2026-08-05T14:00:00Z"),
            EventReplaySpec(),
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades.iloc[0]["entry_record_seq"], 2)
        self.assertEqual(trades.iloc[0]["signal_record_seq"], 1)
        self.assertEqual(
            trades.iloc[0]["intended_entry_next_quote_record_seq"], 2
        )
        self.assertEqual(trades.iloc[0]["exit_record_seq"], 4)
        self.assertEqual(
            trades.iloc[0]["intended_exit_next_quote_record_seq"], 4
        )
        self.assertEqual(trades.iloc[0]["session_date"], "2026-08-05")
        self.assertLess(
            pd.Timestamp(trades.iloc[0]["signal_time"]),
            pd.Timestamp(trades.iloc[0]["entry_time"]),
        )
        self.assertEqual(trades.iloc[0]["exit_reason"], "TARGET")
        self.assertGreater(float(trades.iloc[0]["net_usd"]), 0.0)

    def test_cost_floor_cannot_be_weakened(self) -> None:
        with self.assertRaises(ValueError):
            EventReplaySpec(adverse_ticks_per_fill=2.0)
        with self.assertRaises(ValueError):
            EventReplaySpec(round_trip_fee_usd=1.0)


if __name__ == "__main__":
    unittest.main()
