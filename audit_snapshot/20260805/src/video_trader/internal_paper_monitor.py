from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import AppConfig, live_execution_unlocked
from .decision_journal import HashChainedJsonlJournal
from .domain import EventType, ExecutionEvent, MarketSnapshot, Prediction, Side, TradeIntent
from .execution.ninjatrader import NinjaTraderReadOnlyFeed
from .execution.paper import PaperExecutor
from .session import trading_session_date
from .sim101_forward import FrozenExpectedMoveScorer, ScoredCompletedBar


class PaperScorer(Protocol):
    horizon_minutes: int
    fingerprint: str
    model_version: str

    def score(self, snapshot: MarketSnapshot) -> ScoredCompletedBar | None: ...


def run_internal_paper_monitor(
    config: AppConfig,
    scorer: PaperScorer,
    journal_path: Path,
    status_path: Path,
    *,
    feed: NinjaTraderReadOnlyFeed | None = None,
    poll_seconds: float = 0.5,
    bridge_retry_seconds: float = 5.0,
    handshake_interval_seconds: float = 60.0,
    adverse_fill_ticks: int = 3,
    emergency_stop_ticks: int = 40,
    emergency_target_ticks: int = 80,
    max_completed_bars: int = 0,
    max_runtime_seconds: int = 0,
) -> dict[str, object]:
    """Run a no-order, completed-bar paper monitor against a disarmed IPB feed."""

    if live_execution_unlocked():
        raise RuntimeError("internal paper monitor refuses to run with live trading unlocked")
    if poll_seconds < 0.1 or bridge_retry_seconds < 0.1:
        raise ValueError("monitor polling intervals are too small")
    if handshake_interval_seconds <= 0.0:
        raise ValueError("handshake interval must be positive")
    if adverse_fill_ticks < 3:
        raise ValueError("paper monitor requires at least three adverse ticks per fill")
    if emergency_stop_ticks <= 0 or emergency_target_ticks <= 0:
        raise ValueError("paper brackets must be positive")
    if max_completed_bars < 0 or max_runtime_seconds < 0:
        raise ValueError("monitor limits cannot be negative")

    paper_feed = feed or NinjaTraderReadOnlyFeed(
        config.execution.ninjatrader_host,
        config.execution.ninjatrader_port,
        expected_instrument=config.market.symbol,
        expected_account="Sim101",
    )
    journal = HashChainedJsonlJournal(journal_path)
    executor = PaperExecutor(
        point_value=config.market.point_value,
        slippage_ticks=adverse_fill_ticks,
        tick_size=config.market.tick_size,
        commission_per_side=config.execution.commission_per_side,
    )
    run_id = datetime.now(timezone.utc).strftime("paper-%Y%m%dT%H%M%S%fZ")
    started = time.monotonic()
    observed_bars = 0
    submitted = 0
    closed = 0
    session = None
    session_net = 0.0
    total_net = 0.0
    active_client_id: str | None = None
    scheduled_exit: datetime | None = None
    last_snapshot_open: datetime | None = None
    last_snapshot: MarketSnapshot | None = None
    last_handshake = 0.0

    journal.append(
        {
            "record_type": "RUN",
            "run_id": run_id,
            "execution_environment": "INTERNAL_PAPER_COMPLETED_BAR",
            "live_execution_unlocked": False,
            "bridge_host": paper_feed.host,
            "bridge_port": paper_feed.port,
            "candidate_fingerprint": scorer.fingerprint,
            "model_version": scorer.model_version,
            "one_position_at_a_time": True,
            "adverse_ticks_per_fill": adverse_fill_ticks,
            "round_trip_fee_usd": 2.0 * config.execution.commission_per_side,
            "limitation": (
                "Completed-bar snapshots are not quote-level fills and are not "
                "historical, Sim101, or live execution evidence."
            ),
        }
    )

    def write_status(state: str, detail: str = "") -> None:
        payload = {
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "state": state,
            "detail": detail,
            "bridge_port": paper_feed.port,
            "observed_completed_bars": observed_bars,
            "submitted_paper_intents": submitted,
            "closed_paper_trades": closed,
            "active_client_order_id": active_client_id,
            "session_net_usd": session_net,
            "total_net_usd": total_net,
            "live_execution_unlocked": False,
            "order_commands_exposed": False,
        }
        status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = status_path.with_suffix(status_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(status_path)

    def record_events(events: list[ExecutionEvent]) -> None:
        nonlocal active_client_id, scheduled_exit, closed, session_net, total_net
        for event in events:
            payload = asdict(event)
            payload["event_type"] = event.event_type.value
            payload["side"] = event.side.value
            payload["timestamp"] = event.timestamp.isoformat()
            journal.append({"record_type": "PAPER_EVENT", "run_id": run_id, "event": payload})
            if event.event_type is EventType.CLOSED:
                closed += 1
                session_net += event.realized_pnl
                total_net += event.realized_pnl
            if (
                event.client_order_id == active_client_id
                and event.event_type in {EventType.CLOSED, EventType.REJECTED}
            ):
                active_client_id = None
                scheduled_exit = None

    try:
        while True:
            elapsed = time.monotonic() - started
            if max_runtime_seconds and elapsed >= max_runtime_seconds:
                break
            if max_completed_bars and observed_bars >= max_completed_bars:
                break

            now_monotonic = time.monotonic()
            if now_monotonic - last_handshake >= handshake_interval_seconds:
                try:
                    bridge_status = paper_feed.assert_paper_ready()
                except (OSError, RuntimeError) as exc:
                    write_status("WAITING_FOR_SAFE_BRIDGE", str(exc))
                    time.sleep(bridge_retry_seconds)
                    continue
                journal.append(
                    {
                        "record_type": "BRIDGE_HANDSHAKE",
                        "run_id": run_id,
                        "status": bridge_status,
                    }
                )
                last_handshake = now_monotonic

            try:
                snapshot = paper_feed.latest_completed_bar(
                    config.market.display_candle_seconds
                )
            except (OSError, RuntimeError) as exc:
                write_status("WAITING_FOR_SNAPSHOT", str(exc))
                time.sleep(bridge_retry_seconds)
                continue
            if snapshot is None or snapshot.candle_open == last_snapshot_open:
                write_status("MONITORING")
                time.sleep(poll_seconds)
                continue

            last_snapshot = snapshot
            last_snapshot_open = snapshot.candle_open
            observed_bars += 1
            current_session = trading_session_date(snapshot.timestamp)
            if session != current_session:
                session = current_session
                session_net = 0.0
            journal.append(
                {
                    "record_type": "MARKET",
                    "run_id": run_id,
                    "market": {
                        **asdict(snapshot),
                        "timestamp": snapshot.timestamp.isoformat(),
                        "candle_open": snapshot.candle_open.isoformat(),
                    },
                }
            )

            record_events(executor.on_market(snapshot))
            if (
                active_client_id is not None
                and scheduled_exit is not None
                and snapshot.timestamp >= scheduled_exit
            ):
                record_events(
                    executor.flatten_all("frozen horizon exit", snapshot.timestamp)
                )

            scored = scorer.score(snapshot)
            if scored is not None:
                decision = "NO_SIGNAL"
                eligible = scored.side is not None
                intent = None
                if eligible and active_client_id is not None:
                    decision = "BLOCKED_NON_OVERLAP"
                elif eligible and session_net <= -abs(config.risk.max_daily_loss):
                    decision = "BLOCKED_DAILY_LOSS"
                elif eligible and session_net >= abs(config.risk.daily_profit_lock):
                    decision = "BLOCKED_DAILY_PROFIT_LOCK"
                elif eligible:
                    intent = _paper_intent(
                        config,
                        snapshot,
                        scored,
                        emergency_stop_ticks,
                        emergency_target_ticks,
                        scorer.horizon_minutes,
                    )
                    decision = "SIGNAL_READY_INTERNAL_PAPER"
                decision_fingerprint = "|".join(
                    (scored.model_version, scored.timestamp.isoformat(), "INTERNAL_PAPER_DECISION")
                )
                decision_id = "DEC_" + hashlib.sha256(
                    decision_fingerprint.encode("utf-8")
                ).hexdigest()[:24]
                journal.append(
                    {
                        "record_type": "PREDICTION",
                        "run_id": run_id,
                        "decision_id": decision_id,
                        "session_date": str(trading_session_date(scored.timestamp)),
                        "evidence_tier": "COMPLETED_BAR_ONLY",
                        "features": dict(scored.features),
                        "signal_observation": {
                            "timestamp": snapshot.timestamp.isoformat(),
                            "candle_open": snapshot.candle_open.isoformat(),
                            "open": snapshot.open,
                            "high": snapshot.high,
                            "low": snapshot.low,
                            "close": snapshot.close,
                            "volume": snapshot.volume,
                        },
                        "signal_bid": None,
                        "signal_ask": None,
                        "intended_next_quote_event_id": None,
                        "prediction": {
                            "timestamp": scored.timestamp.isoformat(),
                            "predicted_move_ticks": scored.predicted_move_ticks,
                            "threshold_ticks": scored.threshold_ticks,
                            "side": scored.side.value if scored.side else None,
                            "eligible": eligible,
                            "decision": decision,
                        },
                    }
                )
                if intent is not None:
                    journal.append(
                        {
                            "record_type": "PAPER_INTENT",
                            "run_id": run_id,
                            "decision_id": decision_id,
                            "session_date": str(trading_session_date(intent.created_at)),
                            "intent": _intent_payload(intent),
                        }
                    )
                    events = executor.submit(intent)
                    record_events(events)
                    if any(event.event_type is EventType.QUEUED for event in events):
                        active_client_id = intent.client_order_id
                        scheduled_exit = snapshot.timestamp + timedelta(
                            minutes=scorer.horizon_minutes
                        )
                        submitted += 1
                        decision = "QUEUED_INTERNAL_PAPER"
                    else:
                        decision = "PAPER_ADAPTER_REJECTED"
            write_status("MONITORING")
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        write_status("STOPPING", "keyboard interrupt")
    finally:
        if executor.pending_intents or executor.open_trades:
            record_events(
                executor.flatten_all(
                    "internal paper monitor stopped",
                    last_snapshot.timestamp if last_snapshot else datetime.now(timezone.utc),
                )
            )
        journal.append(
            {
                "record_type": "RUN_STOP",
                "run_id": run_id,
                "observed_completed_bars": observed_bars,
                "submitted_paper_intents": submitted,
                "closed_paper_trades": closed,
                "total_net_usd": total_net,
            }
        )
        write_status("STOPPED")

    return {
        "run_id": run_id,
        "journal": str(journal_path.resolve()),
        "status": str(status_path.resolve()),
        "observed_completed_bars": observed_bars,
        "submitted_paper_intents": submitted,
        "closed_paper_trades": closed,
        "total_net_usd": total_net,
        "live_execution_unlocked": False,
    }


def scorer_from_frozen_artifact(artifact_dir: Path) -> FrozenExpectedMoveScorer:
    metadata = json.loads(
        (artifact_dir / "frozen_candidate.json").read_text(encoding="utf-8")
    )
    manifest = dict(metadata["data_manifest"])
    proxy_path = Path(str(manifest["proxy_path"]))
    authoritative_path = Path(str(manifest["authoritative_path"]))
    _assert_sha256(proxy_path, str(manifest["proxy_sha256"]))
    _assert_sha256(authoritative_path, str(manifest["authoritative_sha256"]))
    return FrozenExpectedMoveScorer(proxy_path, authoritative_path, artifact_dir)


def _paper_intent(
    config: AppConfig,
    snapshot: MarketSnapshot,
    scored: ScoredCompletedBar,
    stop_ticks: int,
    target_ticks: int,
    horizon_minutes: int,
) -> TradeIntent:
    if scored.side is None:
        raise ValueError("cannot create a paper intent for a no-trade score")
    fingerprint = "|".join(
        (
            scored.model_version,
            snapshot.candle_open.isoformat(),
            scored.side.value,
            "INTERNAL_PAPER",
        )
    )
    client_id = "IP_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
    entry = snapshot.close
    stop = entry - scored.side.sign * stop_ticks * config.market.tick_size
    target = entry + scored.side.sign * target_ticks * config.market.tick_size
    scale = max(abs(scored.threshold_ticks), 1e-6)
    probability_up = float(
        np.clip(0.5 + 0.49 * np.tanh(scored.predicted_move_ticks / scale), 0.0, 1.0)
    )
    return TradeIntent(
        client_order_id=client_id,
        symbol=config.market.symbol,
        side=scored.side,
        quantity=1,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        created_at=snapshot.timestamp,
        candle_open=snapshot.candle_open,
        prediction=Prediction(
            probability_up=probability_up,
            model_version=scored.model_version,
            horizon_seconds=horizon_minutes * 60,
        ),
    )


def _intent_payload(intent: TradeIntent) -> dict[str, object]:
    return {
        "client_order_id": intent.client_order_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "quantity": intent.quantity,
        "entry_price": intent.entry_price,
        "stop_price": intent.stop_price,
        "target_price": intent.target_price,
        "created_at": intent.created_at.isoformat(),
        "candle_open": intent.candle_open.isoformat(),
        "prediction": asdict(intent.prediction),
    }


def _assert_sha256(path: Path, expected: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected.lower():
        raise ValueError(f"frozen candidate source hash mismatch: {path}")


__all__ = ["run_internal_paper_monitor", "scorer_from_frozen_artifact"]
