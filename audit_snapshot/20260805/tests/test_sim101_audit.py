from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from video_trader.sim101_audit import run_sim101_audit
from video_trader.decision_journal import HashChainedJsonlJournal
from video_trader.execution.evidence import (
    COMPLETED_BAR_EVIDENCE,
    JOURNAL_SCHEMA_VERSION,
    TOP_OF_BOOK_EVIDENCE,
)


def _record(record_type: str, **values):
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "recorded_utc": "2026-08-04T14:00:00+00:00",
        "run_id": "forward-001",
        "record_type": record_type,
        **values,
    }


def _write_passing_journal(
    path: Path,
    incomplete_last: bool = False,
    *,
    omit_predictions: bool = False,
    completed_bar_only: bool = False,
) -> None:
    records = [
        _record(
            "RUN",
            execution_environment="SIM101",
            live_execution_unlocked=False,
            candidate_fingerprint="fixed-candidate-sha",
            candidate_status="FROZEN_RESEARCH_ONLY",
            training_frame_end="2026-07-31T19:59:00+00:00",
        )
    ]
    sequence = 0
    sessions = pd.bdate_range("2026-08-04", periods=40)
    trade_number = 0
    for session in sessions:
        market_time = (
            session.tz_localize("America/New_York")
            + pd.Timedelta(hours=10)
        ).tz_convert("UTC")
        for offset in range(3):
            trade_number += 1
            client_id = f"fixed-{trade_number:04d}"
            entry_time = market_time + pd.Timedelta(minutes=offset * 2)
            exit_time = entry_time + pd.Timedelta(minutes=1)
            decision_id = f"decision-{trade_number:04d}"
            records.append(
                _record(
                    "MARKET",
                    session_date=str(session.date()),
                    market={
                        "timestamp": entry_time.isoformat(),
                        "candle_open": entry_time.isoformat(),
                    },
                )
            )
            if not omit_predictions:
                records.append(
                    _record(
                        "PREDICTION",
                        decision_id=decision_id,
                        session_date=str(session.date()),
                        evidence_tier=(
                            COMPLETED_BAR_EVIDENCE
                            if completed_bar_only
                            else TOP_OF_BOOK_EVIDENCE
                        ),
                        features={"momentum_ticks": 12.0, "spread_ticks": 1.0},
                        signal_observation={
                            "timestamp": entry_time.isoformat(),
                            "candle_open": entry_time.isoformat(),
                            "open": 20000.0,
                            "high": 20001.0,
                            "low": 19999.0,
                            "close": 20000.25,
                            "volume": 100,
                        },
                        signal_bid=None if completed_bar_only else 20000.0,
                        signal_ask=None if completed_bar_only else 20000.25,
                        intended_next_quote_event_id=(
                            None if completed_bar_only else f"run:{trade_number:020d}"
                        ),
                        prediction={
                            "timestamp": entry_time.isoformat(),
                            "predicted_move_ticks": 12.0,
                            "threshold_ticks": 10.0,
                            "side": "BUY",
                            "eligible": True,
                            "decision": "SIGNAL_READY",
                        },
                    )
                )
                if not completed_bar_only:
                    records.append(
                        _record(
                            "NEXT_QUOTE",
                            decision_id=decision_id,
                            session_date=str(session.date()),
                            event_id=f"run:{trade_number:020d}",
                            timestamp=(
                                entry_time + pd.Timedelta(milliseconds=250)
                            ).isoformat(),
                            bid=20000.25,
                            ask=20000.50,
                        )
                    )
            records.append(
                _record(
                    "INTENT",
                    decision_id=decision_id,
                    session_date=str(session.date()),
                    intent={
                        "client_order_id": client_id,
                        "symbol": "MNQ 09-26",
                        "side": "BUY",
                        "quantity": 1,
                    },
                )
            )
            sequence += 1
            records.append(
                _record(
                    "EVENT",
                    event_source="bridge",
                    event={
                        "sequence": sequence,
                        "event_type": "FILLED",
                        "client_order_id": client_id,
                        "timestamp": (
                            entry_time + pd.Timedelta(milliseconds=500)
                        ).isoformat(),
                        "side": "BUY",
                        "quantity": 1,
                        "price": 20000.0,
                        "realized_pnl": 0.0,
                        "reason": "entry",
                    },
                )
            )
            if incomplete_last and trade_number == 120:
                continue
            sequence += 1
            records.append(
                _record(
                    "EVENT",
                    event_source="bridge",
                    event={
                        "sequence": sequence,
                        "event_type": "CLOSED",
                        "client_order_id": client_id,
                        "timestamp": exit_time.isoformat(),
                        "side": "BUY",
                        "quantity": 1,
                        "price": 20005.0,
                        "realized_pnl": 10.0,
                        "reason": "target",
                    },
                )
            )
    journal = HashChainedJsonlJournal(path)
    for record in records:
        journal.append(record)


class Sim101AuditTests(unittest.TestCase):
    def test_complete_fixed_candidate_evidence_can_pass_internal_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "journal.jsonl"
            _write_passing_journal(journal)
            report = run_sim101_audit(
                journal,
                root / "report",
                simulations=1000,
                block_sessions=5,
            )
            result = json.loads((report.parent / "sim101_audit.json").read_text())
            self.assertTrue(result["passes_sim101_gate"])
            self.assertEqual(result["sessions"], 40)
            self.assertEqual(result["closed_trades"], 120)
            self.assertGreater(result["net_usd"], 0.0)
            self.assertTrue(result["journal_hash_chain_valid"])

    def test_open_fill_prevents_reconciliation_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "journal.jsonl"
            _write_passing_journal(journal, incomplete_last=True)
            report = run_sim101_audit(
                journal,
                root / "report",
                simulations=1000,
                block_sessions=5,
            )
            result = json.loads((report.parent / "sim101_audit.json").read_text())
            self.assertFalse(result["passes_sim101_gate"])
            self.assertFalse(result["checks"]["complete_lifecycle_reconciliation"])
            self.assertEqual(result["open_filled_intents"], 1)

    def test_tampered_hash_chain_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "journal.jsonl"
            _write_passing_journal(journal)
            records = [json.loads(line) for line in journal.read_text().splitlines()]
            records[1]["market"]["timestamp"] = "2026-08-04T15:00:00+00:00"
            journal.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            report = run_sim101_audit(
                journal,
                root / "report",
                simulations=100,
                block_sessions=5,
            )
            result = json.loads((report.parent / "sim101_audit.json").read_text())
            self.assertFalse(result["passes_sim101_gate"])
            self.assertFalse(result["checks"]["journal_integrity"])
            self.assertFalse(result["journal_hash_chain_valid"])

    def test_missing_decisions_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "journal.jsonl"
            _write_passing_journal(journal, omit_predictions=True)
            report = run_sim101_audit(
                journal, root / "report", simulations=100, block_sessions=5
            )
            result = json.loads((report.parent / "sim101_audit.json").read_text())
            self.assertFalse(result["passes_sim101_gate"])
            self.assertFalse(result["checks"]["complete_decision_journal"])

    def test_completed_bar_decisions_are_not_top_of_book_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "journal.jsonl"
            _write_passing_journal(journal, completed_bar_only=True)
            report = run_sim101_audit(
                journal, root / "report", simulations=100, block_sessions=5
            )
            result = json.loads((report.parent / "sim101_audit.json").read_text())
            self.assertTrue(result["checks"]["complete_decision_journal"])
            self.assertFalse(result["checks"]["top_of_book_decision_evidence"])
            self.assertFalse(result["passes_sim101_gate"])


if __name__ == "__main__":
    unittest.main()
