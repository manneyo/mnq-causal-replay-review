from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from video_trader.domain import (
    EventType,
    ExecutionEvent,
    MarketSnapshot,
    Prediction,
    Side,
    TradeIntent,
)
from video_trader.execution.evidence import (
    JOURNAL_SCHEMA_VERSION,
    TOP_OF_BOOK_EVIDENCE,
    Sim101EvidenceExecutor,
)


class _FakeExecutor:
    def submit(self, intent: TradeIntent) -> list[ExecutionEvent]:
        return [
            ExecutionEvent(
                sequence=0,
                event_type=EventType.QUEUED,
                client_order_id=intent.client_order_id,
                timestamp=intent.created_at,
                side=intent.side,
                quantity=intent.quantity,
                price=intent.entry_price,
                reason="queued",
            )
        ]

    def on_market(self, _snapshot: MarketSnapshot) -> list[ExecutionEvent]:
        return []

    def poll_events(self) -> list[ExecutionEvent]:
        return []

    def flatten_all(self, _reason: str, _timestamp=None) -> list[ExecutionEvent]:
        return []


class Sim101EvidenceTests(unittest.TestCase):
    def test_records_candidate_market_intent_and_prevents_durable_duplicate(self):
        environment = {
            "TRADING_ENABLED": "false",
            "LIVE_TRADING_ACK": "",
            "SIM101_ENABLED": "true",
            "SIM101_ACK": "I_UNDERSTAND_SIMULATED_ORDERS",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", environment, clear=False
        ):
            root = Path(directory)
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "frozen_candidate.json").write_text(
                json.dumps(
                    {
                        "status": "FROZEN_RESEARCH_ONLY",
                        "training_frame_end": "2026-07-31T19:59:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            (artifact / "model.txt").write_text("fixed model", encoding="utf-8")
            journal = root / "sim101.jsonl"
            executor = Sim101EvidenceExecutor(
                _FakeExecutor(), journal, artifact, "test-run"
            )
            now = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
            snapshot = MarketSnapshot(now, now, 20000, 20001, 19999, 20000.25, 100)
            executor.on_market(snapshot)
            executor.on_market(snapshot)
            executor.record_prediction(
                decision_id="decision-001",
                timestamp=now,
                predicted_move_ticks=12.0,
                threshold_ticks=10.0,
                side="BUY",
                eligible=True,
                decision="SIGNAL_READY",
                features={"momentum_ticks": 12.0, "spread_ticks": 1.0},
                signal_observation={
                    "timestamp": now.isoformat(),
                    "candle_open": now.isoformat(),
                    "open": 20000.0,
                    "high": 20001.0,
                    "low": 19999.0,
                    "close": 20000.25,
                    "volume": 100,
                },
                evidence_tier=TOP_OF_BOOK_EVIDENCE,
                signal_bid=20000.0,
                signal_ask=20000.25,
                intended_next_quote_event_id="run:00000000000000000002",
            )
            next_quote_time = now.replace(microsecond=250_000)
            executor.record_next_quote(
                decision_id="decision-001",
                event_id="run:00000000000000000002",
                timestamp=next_quote_time,
                bid=20000.25,
                ask=20000.50,
            )
            intent = TradeIntent(
                client_order_id="fixed-001",
                symbol="MNQ 09-26",
                side=Side.BUY,
                quantity=1,
                entry_price=20000.25,
                stop_price=19996.25,
                target_price=20006.25,
                created_at=now,
                candle_open=now,
                prediction=Prediction(0.75, "frozen-10m", 600),
            )
            executor.submit(intent, decision_id="decision-001")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                executor.submit(intent, decision_id="decision-001")

            records = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertEqual(
                [record["record_type"] for record in records],
                ["RUN", "MARKET", "PREDICTION", "NEXT_QUOTE", "INTENT", "EVENT"],
            )
            self.assertEqual(records[0]["execution_environment"], "SIM101")
            self.assertFalse(records[0]["live_execution_unlocked"])
            self.assertEqual(records[0]["schema_version"], JOURNAL_SCHEMA_VERSION)
            self.assertEqual(records[2]["decision_id"], "decision-001")
            self.assertEqual(records[4]["intent"]["client_order_id"], "fixed-001")
            self.assertLess(records[2]["journal_sequence"], records[3]["journal_sequence"])
            self.assertLess(records[3]["journal_sequence"], records[4]["journal_sequence"])
            self.assertEqual(
                [record["journal_sequence"] for record in records],
                list(range(1, len(records) + 1)),
            )
            self.assertTrue(all(len(record["record_hash"]) == 64 for record in records))

            tampered = list(records)
            tampered[0]["execution_environment"] = "LIVE"
            journal.write_text(
                "\n".join(json.dumps(record) for record in tampered) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "record hash mismatch"):
                Sim101EvidenceExecutor(
                    _FakeExecutor(), journal, artifact, "tamper-check"
                )

    def test_intent_cannot_reference_an_unjournaled_decision(self):
        environment = {
            "TRADING_ENABLED": "false",
            "LIVE_TRADING_ACK": "",
            "SIM101_ENABLED": "true",
            "SIM101_ACK": "I_UNDERSTAND_SIMULATED_ORDERS",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", environment, clear=False
        ):
            root = Path(directory)
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "model.txt").write_text("fixed model", encoding="utf-8")
            executor = Sim101EvidenceExecutor(
                _FakeExecutor(), root / "sim101.jsonl", artifact, "test-run"
            )
            now = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
            intent = TradeIntent(
                "fixed-002", "MNQ", Side.BUY, 1, 20000.25, 19990.25, 20020.25,
                now, now, Prediction(0.75, "frozen-10m", 600),
            )
            with self.assertRaisesRegex(RuntimeError, "journaled before submission"):
                executor.submit(intent, decision_id="missing-decision")

    def test_top_of_book_intent_requires_the_declared_later_quote(self):
        environment = {
            "TRADING_ENABLED": "false",
            "LIVE_TRADING_ACK": "",
            "SIM101_ENABLED": "true",
            "SIM101_ACK": "I_UNDERSTAND_SIMULATED_ORDERS",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", environment, clear=False
        ):
            root = Path(directory)
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "model.txt").write_text("fixed model", encoding="utf-8")
            executor = Sim101EvidenceExecutor(
                _FakeExecutor(), root / "sim101.jsonl", artifact, "test-run"
            )
            now = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
            executor.record_prediction(
                decision_id="decision-002",
                timestamp=now,
                predicted_move_ticks=12.0,
                threshold_ticks=10.0,
                side="BUY",
                eligible=True,
                decision="SIGNAL_READY",
                features={"momentum_ticks": 12.0},
                signal_observation={
                    "timestamp": now.isoformat(), "candle_open": now.isoformat(),
                    "open": 20000.0, "high": 20001.0, "low": 19999.0,
                    "close": 20000.25, "volume": 100,
                },
                evidence_tier=TOP_OF_BOOK_EVIDENCE,
                signal_bid=20000.0,
                signal_ask=20000.25,
                intended_next_quote_event_id="run:00000000000000000003",
            )
            intent = TradeIntent(
                "fixed-003", "MNQ", Side.BUY, 1, 20000.25, 19990.25, 20020.25,
                now, now, Prediction(0.75, "frozen-10m", 600),
            )
            with self.assertRaisesRegex(RuntimeError, "later next quote"):
                executor.submit(intent, decision_id="decision-002")


if __name__ == "__main__":
    unittest.main()
