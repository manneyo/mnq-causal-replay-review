from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from video_trader.config import load_config
from video_trader.decision_journal import HashChainedJsonlJournal
from video_trader.domain import MarketSnapshot, Side
from video_trader.execution.ninjatrader import NinjaTraderReadOnlyFeed
from video_trader.internal_paper_monitor import run_internal_paper_monitor
from video_trader.sim101_forward import ScoredCompletedBar


class _FakeFeed:
    host = "127.0.0.1"
    port = 5570

    def __init__(self, snapshots: list[MarketSnapshot]):
        self.snapshots = snapshots
        self.position = 0
        self.handshakes = 0

    def assert_paper_ready(self):
        self.handshakes += 1
        return {
            "reachable": True,
            "version": "VERSION IPB-1.2 DISARMED",
            "safety": "SAFETY SIM_ONLY DISARMED",
            "instrument": "INSTRUMENT MNQ",
            "account": "ACCOUNT Sim101",
            "status": "STATUS Flat 0",
            "orders": "ORDERS 0",
        }

    def latest_completed_bar(self, _seconds: int):
        if self.position >= len(self.snapshots):
            return self.snapshots[-1]
        value = self.snapshots[self.position]
        self.position += 1
        return value


class _OneSignalScorer:
    horizon_minutes = 10
    fingerprint = "a" * 64
    model_version = "frozen-test"

    def __init__(self):
        self.calls = 0

    def score(self, snapshot: MarketSnapshot):
        self.calls += 1
        if self.calls != 1:
            return None
        return ScoredCompletedBar(
            timestamp=snapshot.candle_open,
            predicted_move_ticks=2.0,
            threshold_ticks=0.5,
            side=Side.BUY,
            model_version=self.model_version,
        )


def _snapshots() -> list[MarketSnapshot]:
    opened = datetime(2026, 8, 4, 14, 30, tzinfo=timezone.utc)
    return [
        MarketSnapshot(opened + timedelta(minutes=1), opened, 100, 100.25, 99.75, 100, 10),
        MarketSnapshot(
            opened + timedelta(minutes=2), opened + timedelta(minutes=1),
            100, 100.25, 99.75, 100, 10,
        ),
        MarketSnapshot(
            opened + timedelta(minutes=3), opened + timedelta(minutes=2),
            100, 120, 99.75, 120, 10,
        ),
    ]


class ReadOnlyFeedTests(unittest.TestCase):
    def test_handshake_requires_disarmed_exact_sim101(self):
        feed = NinjaTraderReadOnlyFeed("127.0.0.1", 5570)
        responses = [
            "VERSION IPB-1.2 DISARMED",
            "SAFETY SIM_ONLY DISARMED",
            "INSTRUMENT MNQ",
            "ACCOUNT Sim101",
            "STATUS Flat 0",
            "ORDERS 0",
            "EVENTRANGE 0 0",
            "SNAPSHOT NONE",
        ]
        with patch(
            "video_trader.execution.ninjatrader._send_readonly",
            side_effect=responses,
        ):
            status = feed.assert_paper_ready()
        self.assertTrue(status["reachable"])
        self.assertFalse(hasattr(feed, "submit"))
        self.assertFalse(hasattr(feed, "flatten_all"))

    def test_handshake_rejects_a_non_sim_account(self):
        feed = NinjaTraderReadOnlyFeed("127.0.0.1", 5570)
        with patch.object(
            feed,
            "probe",
            return_value={
                "reachable": True,
                "version": "VERSION IPB-1.2 DISARMED",
                "safety": "SAFETY NON_SIM DISARMED",
                "instrument": "INSTRUMENT MNQ",
                "account": "ACCOUNT evaluation",
                "status": "STATUS Flat 0",
                "orders": "ORDERS 0",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "paper-safety handshake failed"):
                feed.assert_paper_ready()


class InternalPaperMonitorTests(unittest.TestCase):
    def test_delayed_fill_costed_close_and_hash_chained_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "paper.jsonl"
            status = root / "status.json"
            result = run_internal_paper_monitor(
                load_config(),
                _OneSignalScorer(),
                journal,
                status,
                feed=_FakeFeed(_snapshots()),
                poll_seconds=0.1,
                bridge_retry_seconds=0.1,
                handshake_interval_seconds=3600,
                max_completed_bars=3,
            )
            self.assertEqual(result["submitted_paper_intents"], 1)
            self.assertEqual(result["closed_paper_trades"], 1)
            self.assertGreater(result["total_net_usd"], 0.0)
            records = [json.loads(line) for line in journal.read_text().splitlines()]
            event_types = [
                record["event"]["event_type"]
                for record in records
                if record["record_type"] == "PAPER_EVENT"
            ]
            self.assertEqual(event_types, ["QUEUED", "FILLED", "CLOSED"])
            filled = next(
                record["event"] for record in records
                if record.get("record_type") == "PAPER_EVENT"
                and record["event"]["event_type"] == "FILLED"
            )
            self.assertEqual(filled["price"], 100.75)
            HashChainedJsonlJournal(journal)
            self.assertEqual(json.loads(status.read_text())["state"], "STOPPED")


if __name__ == "__main__":
    unittest.main()
