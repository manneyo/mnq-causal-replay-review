from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from ..config import live_execution_unlocked, sim101_execution_unlocked
from ..decision_journal import HashChainedJsonlJournal
from ..domain import ExecutionEvent, MarketSnapshot, TradeIntent
from ..session import trading_session_date
from .base import Executor


JOURNAL_SCHEMA_VERSION = 3
COMPLETED_BAR_EVIDENCE = "COMPLETED_BAR_ONLY"
TOP_OF_BOOK_EVIDENCE = "TOP_OF_BOOK_NEXT_QUOTE"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint_candidate(artifact_dir: Path) -> tuple[str, dict[str, str]]:
    metadata_path = artifact_dir / "frozen_candidate.json"
    files: list[Path]
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        model_name = metadata.get("model_file")
        model_path = artifact_dir / str(model_name) if model_name else None
        if model_path is not None and model_path.is_file():
            files = [metadata_path, model_path]
        else:
            files = sorted(path for path in artifact_dir.iterdir() if path.is_file())
    else:
        files = sorted(path for path in artifact_dir.iterdir() if path.is_file())
    if not files:
        raise ValueError(f"candidate artifact has no files: {artifact_dir}")
    hashes = {path.name: _sha256(path) for path in files}
    digest = hashlib.sha256()
    for name, value in hashes.items():
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest(), hashes


class Sim101EvidenceExecutor:
    """Record a durable, append-only audit trail around a Sim101-only executor."""

    def __init__(
        self,
        executor: Executor,
        journal_path: Path,
        artifact_dir: Path,
        run_id: str,
    ):
        if live_execution_unlocked():
            raise RuntimeError("Sim101 evidence collection refuses to run with live trading unlocked")
        if not sim101_execution_unlocked():
            raise RuntimeError("Sim101 evidence collection requires the simulation-only unlock")
        self.executor = executor
        self.journal_path = journal_path
        self.run_id = run_id.strip()
        if not self.run_id:
            raise ValueError("run_id cannot be empty")
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal = HashChainedJsonlJournal(self.journal_path)
        self._submitted_ids: set[str] = set()
        self._decision_ids: set[str] = set()
        self._decisions: dict[str, dict[str, object]] = {}
        self._next_quotes: dict[str, dict[str, object]] = {}
        if self.journal_path.exists():
            for line in self.journal_path.read_text(encoding="utf-8").splitlines():
                prior = json.loads(line)
                if prior.get("record_type") == "INTENT":
                    client_id = str(dict(prior.get("intent", {})).get("client_order_id", ""))
                    if client_id:
                        self._submitted_ids.add(client_id)
                elif prior.get("record_type") == "PREDICTION":
                    decision_id = str(prior.get("decision_id", ""))
                    if decision_id:
                        self._decision_ids.add(decision_id)
                        self._decisions[decision_id] = prior
                elif prior.get("record_type") == "NEXT_QUOTE":
                    decision_id = str(prior.get("decision_id", ""))
                    if decision_id:
                        self._next_quotes[decision_id] = prior
        fingerprint, hashes = fingerprint_candidate(artifact_dir)
        metadata_path = artifact_dir / "frozen_candidate.json"
        candidate_metadata = (
            json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata_path.exists()
            else {}
        )
        self._last_market_minute: datetime | None = None
        self._append(
            {
                "record_type": "RUN",
                "execution_environment": "SIM101",
                "live_execution_unlocked": False,
                "candidate_artifact": str(artifact_dir.resolve()),
                "candidate_fingerprint": fingerprint,
                "candidate_file_sha256": hashes,
                "candidate_status": candidate_metadata.get("status"),
                "training_frame_end": candidate_metadata.get("training_frame_end"),
            }
        )

    def _append(self, payload: dict[str, object]) -> None:
        record = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "recorded_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            **payload,
        }
        self._journal.append(record)

    def _record_events(
        self,
        events: Iterable[ExecutionEvent],
        source: str,
    ) -> list[ExecutionEvent]:
        materialized = list(events)
        for event in materialized:
            payload = asdict(event)
            payload["event_type"] = event.event_type.value
            payload["side"] = event.side.value
            payload["timestamp"] = event.timestamp.isoformat()
            self._append(
                {
                    "record_type": "EVENT",
                    "event_source": source,
                    "session_date": str(trading_session_date(event.timestamp)),
                    "event": payload,
                }
            )
        return materialized

    def submit(
        self,
        intent: TradeIntent,
        *,
        decision_id: str | None = None,
    ) -> list[ExecutionEvent]:
        if intent.client_order_id in self._submitted_ids:
            raise RuntimeError(
                f"client order ID already exists in the durable Sim101 journal: "
                f"{intent.client_order_id}"
            )
        if decision_id is not None and decision_id not in self._decision_ids:
            raise RuntimeError(
                "Sim101 intent must reference a prediction journaled before submission"
            )
        if decision_id is not None:
            decision_record = self._decisions[decision_id]
            if (
                decision_record.get("evidence_tier") == TOP_OF_BOOK_EVIDENCE
                and decision_id not in self._next_quotes
            ):
                raise RuntimeError(
                    "top-of-book Sim101 intent requires its later next quote to be journaled"
                )
        prediction = asdict(intent.prediction)
        self._append(
            {
                "record_type": "INTENT",
                "decision_id": decision_id,
                "session_date": str(trading_session_date(intent.created_at)),
                "intent": {
                    "client_order_id": intent.client_order_id,
                    "symbol": intent.symbol,
                    "side": intent.side.value,
                    "quantity": intent.quantity,
                    "entry_price": intent.entry_price,
                    "stop_price": intent.stop_price,
                    "target_price": intent.target_price,
                    "created_at": intent.created_at.isoformat(),
                    "candle_open": intent.candle_open.isoformat(),
                    "prediction": prediction,
                },
            }
        )
        self._submitted_ids.add(intent.client_order_id)
        return self._record_events(self.executor.submit(intent), "adapter_response")

    def record_prediction(
        self,
        *,
        decision_id: str,
        timestamp: datetime,
        predicted_move_ticks: float,
        threshold_ticks: float,
        side: str | None,
        eligible: bool,
        decision: str,
        features: Mapping[str, float],
        signal_observation: Mapping[str, object],
        evidence_tier: str,
        signal_bid: float | None,
        signal_ask: float | None,
        intended_next_quote_event_id: str | None,
    ) -> None:
        decision_id = decision_id.strip()
        if not decision_id:
            raise ValueError("decision_id cannot be empty")
        if decision_id in self._decision_ids:
            raise RuntimeError(f"decision ID already exists in the journal: {decision_id}")
        if evidence_tier not in {COMPLETED_BAR_EVIDENCE, TOP_OF_BOOK_EVIDENCE}:
            raise ValueError(f"unsupported evidence tier: {evidence_tier}")
        feature_payload = {str(name): float(value) for name, value in features.items()}
        if not feature_payload:
            raise ValueError("prediction journal requires the complete model feature vector")
        required_observation = {
            "timestamp", "candle_open", "open", "high", "low", "close", "volume"
        }
        if not required_observation.issubset(signal_observation):
            missing = sorted(required_observation.difference(signal_observation))
            raise ValueError(f"signal observation is missing fields: {missing}")
        if evidence_tier == TOP_OF_BOOK_EVIDENCE:
            if signal_bid is None or signal_ask is None or signal_ask < signal_bid:
                raise ValueError("top-of-book evidence requires a valid signal bid and ask")
            if not intended_next_quote_event_id:
                raise ValueError(
                    "top-of-book evidence requires the intended next quote event ID"
                )
        self._append(
            {
                "record_type": "PREDICTION",
                "decision_id": decision_id,
                "session_date": str(trading_session_date(timestamp)),
                "evidence_tier": evidence_tier,
                "features": feature_payload,
                "signal_observation": dict(signal_observation),
                "signal_bid": signal_bid,
                "signal_ask": signal_ask,
                "intended_next_quote_event_id": intended_next_quote_event_id,
                "prediction": {
                    "timestamp": timestamp.isoformat(),
                    "predicted_move_ticks": predicted_move_ticks,
                    "threshold_ticks": threshold_ticks,
                    "side": side,
                    "eligible": eligible,
                    "decision": decision,
                },
            }
        )
        self._decision_ids.add(decision_id)
        self._decisions[decision_id] = {
            "evidence_tier": evidence_tier,
            "intended_next_quote_event_id": intended_next_quote_event_id,
            "prediction": {"timestamp": timestamp.isoformat()},
        }

    def record_next_quote(
        self,
        *,
        decision_id: str,
        event_id: str,
        timestamp: datetime,
        bid: float,
        ask: float,
    ) -> None:
        decision = self._decisions.get(decision_id)
        if decision is None:
            raise RuntimeError("next quote must reference a previously journaled decision")
        if decision.get("evidence_tier") != TOP_OF_BOOK_EVIDENCE:
            raise RuntimeError("completed-bar decisions cannot claim a next-quote event")
        if decision_id in self._next_quotes:
            raise RuntimeError(f"next quote already exists for decision: {decision_id}")
        expected = str(decision.get("intended_next_quote_event_id", ""))
        if not event_id or event_id != expected:
            raise ValueError("next quote event ID does not match the decision intent")
        if timestamp.tzinfo is None:
            raise ValueError("next quote timestamp must be timezone-aware")
        signal_timestamp = datetime.fromisoformat(
            str(dict(decision.get("prediction", {})).get("timestamp", ""))
        )
        if timestamp <= signal_timestamp:
            raise ValueError("next quote must be observed after the signal timestamp")
        if bid <= 0.0 or ask < bid:
            raise ValueError("next quote requires a valid bid and ask")
        payload = {
            "record_type": "NEXT_QUOTE",
            "decision_id": decision_id,
            "session_date": str(trading_session_date(timestamp)),
            "event_id": event_id,
            "timestamp": timestamp.isoformat(),
            "bid": float(bid),
            "ask": float(ask),
        }
        self._append(payload)
        self._next_quotes[decision_id] = payload

    def on_market(self, snapshot: MarketSnapshot) -> list[ExecutionEvent]:
        minute = snapshot.timestamp.replace(second=0, microsecond=0)
        if minute != self._last_market_minute:
            self._last_market_minute = minute
            self._append(
                {
                    "record_type": "MARKET",
                    "session_date": str(trading_session_date(snapshot.timestamp)),
                    "market": {
                        "timestamp": snapshot.timestamp.isoformat(),
                        "candle_open": snapshot.candle_open.isoformat(),
                        "open": snapshot.open,
                        "high": snapshot.high,
                        "low": snapshot.low,
                        "close": snapshot.close,
                        "volume": snapshot.volume,
                    },
                }
            )
        return self._record_events(self.executor.on_market(snapshot), "bridge")

    def poll_events(self) -> list[ExecutionEvent]:
        return self._record_events(self.executor.poll_events(), "bridge")

    def flatten_all(
        self,
        reason: str,
        timestamp: datetime | None = None,
    ) -> list[ExecutionEvent]:
        self._append(
            {
                "record_type": "CONTROL",
                "action": "FLATTEN_ALL",
                "reason": reason,
                "timestamp": timestamp.isoformat() if timestamp else None,
            }
        )
        return self._record_events(
            self.executor.flatten_all(reason, timestamp), "bridge"
        )

    def exit_order(self, client_order_id: str, reason: str) -> list[ExecutionEvent]:
        method = getattr(self.executor, "exit_order", None)
        if method is None:
            raise RuntimeError("wrapped executor does not support per-order exits")
        self._append(
            {
                "record_type": "CONTROL",
                "action": "EXIT_ORDER",
                "client_order_id": client_order_id,
                "reason": reason,
            }
        )
        return self._record_events(method(client_order_id), "bridge")


__all__ = [
    "COMPLETED_BAR_EVIDENCE",
    "JOURNAL_SCHEMA_VERSION",
    "TOP_OF_BOOK_EVIDENCE",
    "Sim101EvidenceExecutor",
    "fingerprint_candidate",
]
