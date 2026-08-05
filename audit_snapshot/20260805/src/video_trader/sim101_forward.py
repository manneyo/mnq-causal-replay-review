from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from .config import AppConfig, live_execution_unlocked, sim101_execution_unlocked
from .cost_aware_tournament import build_cost_features, load_stitched_mnq
from .domain import EventType, MarketSnapshot, Prediction, Side, TradeIntent
from .execution.evidence import (
    COMPLETED_BAR_EVIDENCE,
    Sim101EvidenceExecutor,
    fingerprint_candidate,
)
from .execution.ninjatrader import NinjaTraderExecutor, probe_ninjatrader_bridge
from .session import NEW_YORK, trading_session_date


@dataclass(frozen=True)
class ScoredCompletedBar:
    timestamp: datetime
    predicted_move_ticks: float
    threshold_ticks: float
    side: Side | None
    model_version: str
    features: Mapping[str, float] = field(default_factory=dict)


class FrozenExpectedMoveScorer:
    def __init__(
        self,
        proxy_path: Path,
        authoritative_path: Path,
        artifact_dir: Path,
    ):
        import lightgbm as lgb

        self.metadata = json.loads(
            (artifact_dir / "frozen_candidate.json").read_text(encoding="utf-8")
        )
        if self.metadata.get("status") != "FROZEN_RESEARCH_ONLY":
            raise ValueError("Sim101 scorer requires a frozen research artifact")
        if self.metadata.get("market") != "MNQ":
            raise ValueError("Sim101 scorer currently supports only the frozen MNQ candidate")
        self.frame, self.context_manifest = load_stitched_mnq(
            proxy_path, authoritative_path
        )
        self.feature_columns = list(self.metadata["feature_columns"])
        self.threshold = float(self.metadata["threshold"])
        self.horizon_minutes = int(self.metadata["horizon_minutes"])
        self.tick_size = float(self.metadata["execution"]["tick_size"])
        self.fingerprint, _ = fingerprint_candidate(artifact_dir)
        self.model_version = f"frozen-{self.fingerprint[:16]}"
        self.model = lgb.Booster(
            model_file=str(artifact_dir / str(self.metadata["model_file"]))
        )

    def _is_executable_feature_bar(self, timestamp: datetime) -> bool:
        local = timestamp.astimezone(NEW_YORK)
        minute = local.hour * 60 + local.minute
        latest_feature_minute = 960 - self.horizon_minutes - 2
        return (
            local.weekday() < 5
            and minute >= 570
            and minute <= latest_feature_minute
        )

    def score(self, snapshot: MarketSnapshot) -> ScoredCompletedBar | None:
        timestamp = pd.Timestamp(snapshot.candle_open)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        if timestamp in self.frame.index or timestamp <= self.frame.index.max():
            return None
        if not self._is_executable_feature_bar(timestamp.to_pydatetime()):
            return None
        row = pd.DataFrame(
            {
                "open": [snapshot.open],
                "high": [snapshot.high],
                "low": [snapshot.low],
                "close": [snapshot.close],
                "volume": [snapshot.volume],
                "source_tier": ["sim101_forward"],
                "contract_id": ["MNQ-09-26"],
            },
            index=pd.DatetimeIndex([timestamp]),
        )
        self.frame = pd.concat([self.frame, row]).sort_index()
        features, _segments, positions = build_cost_features(self.frame, self.tick_size)
        if list(features.columns) != self.feature_columns:
            raise RuntimeError("live feature schema differs from the frozen artifact")
        feature_values = {
            str(name): float(value)
            for name, value in features.iloc[-1].items()
        }
        if not all(math.isfinite(value) for value in feature_values.values()):
            raise RuntimeError("live model feature vector contains a non-finite value")
        if int(positions[-1]) < 30:
            return ScoredCompletedBar(
                timestamp=timestamp.to_pydatetime(),
                predicted_move_ticks=0.0,
                threshold_ticks=self.threshold,
                side=None,
                model_version=self.model_version,
                features=feature_values,
            )
        score = float(
            np.asarray(
                self.model.predict(
                    features.iloc[[-1]].to_numpy(dtype=np.float32)
                ),
                dtype=np.float64,
            )[0]
        )
        side = None
        if math.isfinite(score) and abs(score) >= self.threshold and score != 0.0:
            side = Side.BUY if score > 0.0 else Side.SELL
        return ScoredCompletedBar(
            timestamp=timestamp.to_pydatetime(),
            predicted_move_ticks=score,
            threshold_ticks=self.threshold,
            side=side,
            model_version=self.model_version,
            features=feature_values,
        )


def _intent_for_score(
    config: AppConfig,
    snapshot: MarketSnapshot,
    scored: ScoredCompletedBar,
    emergency_stop_ticks: int,
    emergency_target_ticks: int,
) -> TradeIntent:
    if scored.side is None:
        raise ValueError("cannot create an intent for a no-trade score")
    if emergency_stop_ticks <= 0 or emergency_target_ticks <= 0:
        raise ValueError("emergency bracket ticks must be positive")
    fingerprint = "|".join(
        (
            scored.model_version,
            snapshot.candle_open.isoformat(),
            scored.side.value,
            "SIM101",
        )
    )
    client_id = "SF_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
    entry = snapshot.close
    tick_size = config.market.tick_size
    stop = entry - scored.side.sign * emergency_stop_ticks * tick_size
    target = entry + scored.side.sign * emergency_target_ticks * tick_size
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
            horizon_seconds=600,
        ),
    )


def run_sim101_forward(
    config: AppConfig,
    proxy_path: Path,
    authoritative_path: Path,
    artifact_dir: Path,
    journal_path: Path,
    run_id: str,
    *,
    seconds: int = 0,
    completed_bars: int = 0,
    poll_seconds: float = 1.0,
    emergency_stop_ticks: int = 40,
    emergency_target_ticks: int = 80,
) -> dict[str, object]:
    if live_execution_unlocked():
        raise RuntimeError("Sim101 forward runner refuses to run with live trading unlocked")
    if not sim101_execution_unlocked():
        raise RuntimeError("Sim101 forward runner requires the simulation-only unlock")
    if config.market.symbol != "MNQ":
        raise ValueError("the fixed forward runner must execute MNQ")
    if poll_seconds < 0.25:
        raise ValueError("poll_seconds cannot be lower than 0.25")
    if seconds < 0 or completed_bars < 0:
        raise ValueError("run limits cannot be negative")

    bridge = NinjaTraderExecutor(
        config.execution.ninjatrader_host,
        config.execution.ninjatrader_port,
        expected_instrument="MNQ",
    )
    evidence = Sim101EvidenceExecutor(
        bridge, journal_path, artifact_dir, run_id
    )
    scorer = FrozenExpectedMoveScorer(
        proxy_path, authoritative_path, artifact_dir
    )
    active_client_id: str | None = None
    scheduled_exit: datetime | None = None
    exit_requested = False
    session = None
    session_net = 0.0
    observed_bars = 0
    submitted = 0
    closed = 0
    last_snapshot_open: datetime | None = None
    started = time.monotonic()

    def apply_events(events) -> None:
        nonlocal active_client_id, scheduled_exit, exit_requested, session_net, closed
        for event in events:
            event_session = trading_session_date(event.timestamp)
            if event.event_type is EventType.CLOSED:
                session_net += event.realized_pnl - 1.90 * event.quantity
                closed += 1
            if (
                event.client_order_id == active_client_id
                and event.event_type in {EventType.CLOSED, EventType.REJECTED}
            ):
                active_client_id = None
                scheduled_exit = None
                exit_requested = False

    try:
        while True:
            snapshot = bridge.latest_completed_bar(
                config.market.display_candle_seconds
            )
            if snapshot is None or snapshot.candle_open == last_snapshot_open:
                apply_events(evidence.poll_events())
            else:
                last_snapshot_open = snapshot.candle_open
                observed_bars += 1
                current_session = trading_session_date(snapshot.timestamp)
                if session != current_session:
                    session = current_session
                    session_net = 0.0
                apply_events(evidence.on_market(snapshot))

                if (
                    active_client_id is not None
                    and scheduled_exit is not None
                    and snapshot.timestamp >= scheduled_exit
                    and not exit_requested
                ):
                    apply_events(
                        evidence.exit_order(active_client_id, "fixed 10-minute horizon")
                    )
                    exit_requested = True

                scored = scorer.score(snapshot)
                if scored is not None:
                    decision = "no signal"
                    eligible = scored.side is not None
                    intent = None
                    if eligible and active_client_id is not None:
                        decision = "blocked by non-overlap rule"
                    elif eligible and session_net <= -abs(config.risk.max_daily_loss):
                        decision = "blocked by Sim101 daily loss limit"
                    elif eligible and session_net >= abs(config.risk.daily_profit_lock):
                        decision = "blocked by Sim101 daily profit lock"
                    elif eligible:
                        intent = _intent_for_score(
                            config,
                            snapshot,
                            scored,
                            emergency_stop_ticks,
                            emergency_target_ticks,
                        )
                        decision = "signal ready for Sim101 submission"
                    decision_fingerprint = "|".join(
                        (scored.model_version, scored.timestamp.isoformat(), "SIM101_DECISION")
                    )
                    decision_id = "DEC_" + hashlib.sha256(
                        decision_fingerprint.encode("utf-8")
                    ).hexdigest()[:24]
                    evidence.record_prediction(
                        decision_id=decision_id,
                        timestamp=scored.timestamp,
                        predicted_move_ticks=scored.predicted_move_ticks,
                        threshold_ticks=scored.threshold_ticks,
                        side=scored.side.value if scored.side else None,
                        eligible=eligible,
                        decision=decision,
                        features=scored.features,
                        signal_observation={
                            "timestamp": snapshot.timestamp.isoformat(),
                            "candle_open": snapshot.candle_open.isoformat(),
                            "open": snapshot.open,
                            "high": snapshot.high,
                            "low": snapshot.low,
                            "close": snapshot.close,
                            "volume": snapshot.volume,
                        },
                        evidence_tier=COMPLETED_BAR_EVIDENCE,
                        signal_bid=None,
                        signal_ask=None,
                        intended_next_quote_event_id=None,
                    )
                    if intent is not None:
                        submit_events = evidence.submit(intent, decision_id=decision_id)
                        apply_events(submit_events)
                        if not any(
                            event.event_type is EventType.REJECTED
                            for event in submit_events
                        ):
                            active_client_id = intent.client_order_id
                            scheduled_exit = snapshot.timestamp + pd.Timedelta(
                                minutes=scorer.horizon_minutes
                            ).to_pytimedelta()
                            exit_requested = False
                            submitted += 1
                            decision = "submitted to Sim101"
                        else:
                            decision = "adapter rejected"
            elapsed = time.monotonic() - started
            if seconds and elapsed >= seconds:
                break
            if completed_bars and observed_bars >= completed_bars:
                break
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        if active_client_id is not None:
            apply_events(
                evidence.flatten_all("Sim101 forward runner stopped", datetime.now(timezone.utc))
            )
            flatten_deadline = time.monotonic() + 10.0
            while active_client_id is not None and time.monotonic() < flatten_deadline:
                time.sleep(0.25)
                apply_events(evidence.poll_events())

    return {
        "run_id": run_id,
        "journal": str(journal_path.resolve()),
        "observed_completed_bars": observed_bars,
        "submitted_sim101_orders": submitted,
        "closed_events": closed,
        "open_client_order_id": active_client_id,
        "candidate_fingerprint": scorer.fingerprint,
        "context_frame_end": scorer.frame.index.max().isoformat(),
        "live_execution_unlocked": False,
        "execution_environment": "SIM101",
    }


def write_sim101_readiness(
    config: AppConfig,
    artifact_dir: Path,
    journal_path: Path,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    bridge = probe_ninjatrader_bridge(
        config.execution.ninjatrader_host,
        config.execution.ninjatrader_port,
    )
    fingerprint, _hashes = fingerprint_candidate(artifact_dir)
    checks = {
        "live_execution_locked": not live_execution_unlocked(),
        "sim101_environment_acknowledged": sim101_execution_unlocked(),
        "bridge_reachable": bridge.get("reachable") is True,
        "bridge_protocol_ipb_1_1": str(bridge.get("version", "")).startswith(
            "VERSION IPB-1.1 "
        ),
        "bridge_sim101_and_manually_armed": bridge.get("safety")
        == "SAFETY SIM_ONLY ARMED",
        "bridge_instrument_is_mnq": bridge.get("instrument") == "INSTRUMENT MNQ",
        "completed_bar_available": str(bridge.get("snapshot", "")).startswith(
            "SNAPSHOT "
        )
        and bridge.get("snapshot") != "SNAPSHOT NONE",
    }
    ready = all(checks.values())
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "ready_to_collect_sim101": ready,
        "ready_for_live_trading": False,
        "candidate_fingerprint": fingerprint,
        "journal": str(journal_path.resolve()),
        "journal_exists": journal_path.exists(),
        "bridge": bridge,
        "checks": checks,
    }
    (output_dir / "readiness.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    lines = [
        "# Sim101 Collection Readiness",
        "",
        f"- Ready to collect: `{'YES' if ready else 'NO'}`",
        "- Live trading ready or authorized: `NO`",
        f"- Candidate fingerprint: `{fingerprint}`",
        f"- Bridge version response: `{bridge.get('version', bridge.get('error', 'none'))}`",
        f"- Bridge safety response: `{bridge.get('safety', 'none')}`",
        f"- Bridge instrument response: `{bridge.get('instrument', 'none')}`",
        f"- Completed-bar response: `{bridge.get('snapshot', 'none')}`",
        "",
        "## Checks",
        "",
    ]
    lines.extend(
        f"- `{name}`: `{'PASS' if value else 'FAIL'}`" for name, value in checks.items()
    )
    lines.extend(
        [
            "",
            "This status is read-only. It does not submit an order and cannot authorize live trading.",
        ]
    )
    report = output_dir / "READINESS.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


__all__ = [
    "FrozenExpectedMoveScorer",
    "ScoredCompletedBar",
    "run_sim101_forward",
    "write_sim101_readiness",
]
