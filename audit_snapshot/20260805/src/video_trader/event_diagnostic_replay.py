from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from .candle_state import build_candle_state_predictions
from .data.ninjatrader_events import iter_ninjatrader_event_exports
from .session import trading_session_date


DIAGNOSTIC_STATUS = "IN_SAMPLE_DIAGNOSTIC_ONLY"
DIAGNOSTIC_ARMS = ("candidate_1", "candidate_2", "candidate_3", "matched_random")


@dataclass(frozen=True, slots=True)
class EventReplaySpec:
    tick_size: float = 0.25
    point_value: float = 2.0
    bucket_milliseconds: int = 250
    adverse_ticks_per_fill: float = 3.0
    round_trip_fee_usd: float = 1.90
    safety_margin_ticks: float = 2.0
    target_ticks: float = 24.0
    stop_ticks: float = 16.0
    maximum_fill_delay_milliseconds: int = 1_000
    minimum_signal_score_ticks: float = 12.0

    def __post_init__(self) -> None:
        if self.tick_size <= 0.0 or self.point_value <= 0.0:
            raise ValueError("market values must be positive")
        if self.bucket_milliseconds < 1:
            raise ValueError("bucket milliseconds must be positive")
        if self.adverse_ticks_per_fill < 3.0:
            raise ValueError("diagnostic replay requires three adverse ticks per fill")
        if self.round_trip_fee_usd < 1.90:
            raise ValueError("diagnostic replay requires at least $1.90 round trip")
        if self.target_ticks <= 0.0 or self.stop_ticks <= 0.0:
            raise ValueError("barriers must be positive")
        if self.maximum_fill_delay_milliseconds < self.bucket_milliseconds:
            raise ValueError("fill delay must allow at least one later quote bucket")
        if self.minimum_signal_score_ticks < 0.0:
            raise ValueError("minimum signal score cannot be negative")
        if self.target_ticks <= self.full_cost_ticks + self.safety_margin_ticks:
            raise ValueError("target must clear full costs and the safety margin")

    @property
    def tick_value(self) -> float:
        return self.tick_size * self.point_value

    @property
    def full_cost_ticks(self) -> float:
        return (
            2.0 * self.adverse_ticks_per_fill
            + self.round_trip_fee_usd / self.tick_value
        )


@dataclass(frozen=True, slots=True)
class ReplaySignal:
    timestamp: pd.Timestamp
    side: int
    score_ticks: float


@dataclass(frozen=True, slots=True)
class QuoteAggregationAudit:
    first_record_seq: int | None
    last_record_seq: int | None
    events_seen: int
    realtime_events_in_window: int
    source_time_regressions: int
    invalid_quote_events: int
    first_receive_time_utc_ns: int | None
    last_receive_time_utc_ns: int | None


def aggregate_quote_buckets(
    event_paths: Iterable[Path],
    start: pd.Timestamp,
    end: pd.Timestamp,
    spec: EventReplaySpec = EventReplaySpec(),
) -> tuple[pd.DataFrame, QuoteAggregationAudit]:
    """Compact persisted event order into observable quote states.

    Receive time is the causal clock. Each output row is the final valid quote
    observed in one bucket; no empty bucket is forward-filled into a new event.
    """

    start = _utc_timestamp(start)
    end = _utc_timestamp(end)
    if start >= end:
        raise ValueError("aggregation start must precede end")
    start_ns = int(start.value)
    end_ns = int(end.value)
    bucket_ns = spec.bucket_milliseconds * 1_000_000

    rows: list[dict[str, object]] = []
    current_bucket: int | None = None
    accumulator: dict[str, object] | None = None
    first_record_seq: int | None = None
    last_record_seq: int | None = None
    events_seen = 0
    events_in_window = 0
    source_regressions = 0
    invalid_quotes = 0
    first_receive: int | None = None
    last_receive: int | None = None

    def flush() -> None:
        nonlocal accumulator
        if accumulator is not None and accumulator.get("first_mid") is not None:
            rows.append(accumulator)
        accumulator = None

    for event in iter_ninjatrader_event_exports(event_paths):
        events_seen += 1
        if first_record_seq is None:
            first_record_seq = event.record_seq
        last_record_seq = event.record_seq
        source_regressions += int(event.source_time_regression)
        receive = event.receive_time_utc_ns
        if receive is None or receive < start_ns:
            continue
        if receive >= end_ns:
            break
        if event.state.upper() != "REALTIME":
            continue
        events_in_window += 1
        if first_receive is None:
            first_receive = receive
        last_receive = receive
        valid_quote = (
            event.quote_status == "VALID"
            and event.best_bid_after is not None
            and event.best_ask_after is not None
            and event.best_ask_after > event.best_bid_after
        )
        if event.quote_status in {"LOCKED", "CROSSED"}:
            invalid_quotes += 1
        bucket = receive // bucket_ns
        if current_bucket != bucket:
            flush()
            current_bucket = bucket
            accumulator = {
                "receive_time_utc_ns": receive,
                "first_record_seq": event.record_seq,
                "last_record_seq": event.record_seq,
                "event_count": 0,
                "trade_count": 0,
                "trade_volume": 0.0,
                "trade_notional": 0.0,
                "signed_volume": 0.0,
                "first_mid": None,
                "last_mid": None,
                "last_bid": None,
                "last_ask": None,
                "last_bid_size": None,
                "last_ask_size": None,
                "last_spread_ticks": None,
            }
        assert accumulator is not None
        accumulator["receive_time_utc_ns"] = receive
        accumulator["last_record_seq"] = event.record_seq
        accumulator["event_count"] = int(accumulator["event_count"]) + 1
        if valid_quote:
            bid = float(event.best_bid_after)
            ask = float(event.best_ask_after)
            mid = 0.5 * (bid + ask)
            if accumulator["first_mid"] is None:
                accumulator["first_mid"] = mid
            accumulator["last_mid"] = mid
            accumulator["last_bid"] = bid
            accumulator["last_ask"] = ask
            accumulator["last_bid_size"] = float(event.best_bid_size_after or 0.0)
            accumulator["last_ask_size"] = float(event.best_ask_size_after or 0.0)
            accumulator["last_spread_ticks"] = (ask - bid) / spec.tick_size
            if event.event_type == "TRADE":
                volume = float(event.volume)
                accumulator["trade_count"] = int(accumulator["trade_count"]) + 1
                accumulator["trade_volume"] = float(accumulator["trade_volume"]) + volume
                accumulator["trade_notional"] = (
                    float(accumulator["trade_notional"]) + event.price * volume
                )
                midpoint = 0.5 * (bid + ask)
                sign = 1.0 if event.price >= ask else -1.0 if event.price <= bid else np.sign(event.price - midpoint)
                accumulator["signed_volume"] = (
                    float(accumulator["signed_volume"]) + sign * volume
                )
    flush()

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("no valid real-time quotes were observed in the requested window")
    index = pd.to_datetime(
        frame.pop("receive_time_utc_ns").to_numpy(dtype=np.int64), unit="ns", utc=True
    )
    frame.index = pd.DatetimeIndex(index, name="observed_at")
    numeric = [
        "first_mid",
        "last_mid",
        "last_bid",
        "last_ask",
        "last_bid_size",
        "last_ask_size",
        "last_spread_ticks",
    ]
    if frame[numeric].isna().any().any():
        raise ValueError("quote compaction produced an incomplete quote row")
    frame["segment_id"] = _segment_ids(frame.index, 2_000)
    frame = _add_causal_context(frame, start, spec.tick_size)
    audit = QuoteAggregationAudit(
        first_record_seq=first_record_seq,
        last_record_seq=last_record_seq,
        events_seen=events_seen,
        realtime_events_in_window=events_in_window,
        source_time_regressions=source_regressions,
        invalid_quote_events=invalid_quotes,
        first_receive_time_utc_ns=first_receive,
        last_receive_time_utc_ns=last_receive,
    )
    return frame, audit


def build_diagnostic_signals(
    quote_buckets: pd.DataFrame,
    decision_interval_milliseconds: int,
    decision_start: pd.Timestamp,
    decision_end: pd.Timestamp,
    spec: EventReplaySpec = EventReplaySpec(),
    *,
    random_seed: int = 42,
) -> dict[str, list[ReplaySignal]]:
    if decision_interval_milliseconds < spec.bucket_milliseconds:
        raise ValueError("decision interval cannot be shorter than quote buckets")
    decisions = _downsample_decisions(quote_buckets, decision_interval_milliseconds)
    predictions = build_candle_state_predictions(
        decisions,
        spec.tick_size,
        minimum_displacement_ticks=2.0,
        minimum_completed_bars=20,
    )
    start = _utc_timestamp(decision_start)
    end = _utc_timestamp(decision_end)
    mask = (decisions.index >= start) & (decisions.index < end)
    result: dict[str, list[ReplaySignal]] = {}
    source_names = {
        "candidate_1": "candidate_1",
        "candidate_2": "candidate_2",
        "candidate_3": "candidate_3b",
    }
    for output_name, prediction_name in source_names.items():
        values = np.asarray(predictions[prediction_name], dtype=np.float64)
        active = mask & (values != 0.0) & (
            np.abs(values) >= spec.minimum_signal_score_ticks
        )
        result[output_name] = [
            ReplaySignal(decisions.index[row], 1 if values[row] > 0.0 else -1, float(abs(values[row])))
            for row in np.flatnonzero(active)
        ]
    rng = np.random.default_rng(random_seed + decision_interval_milliseconds)
    reference = result["candidate_2"]
    random_directions = rng.choice(np.array([-1, 1]), size=len(reference))
    result["matched_random"] = [
        ReplaySignal(signal.timestamp, int(random_directions[row]), signal.score_ticks)
        for row, signal in enumerate(reference)
    ]
    return result


def replay_signals(
    quote_buckets: pd.DataFrame,
    signals: Iterable[ReplaySignal],
    arm: str,
    decision_interval_milliseconds: int,
    maximum_holding_seconds: int,
    decision_end: pd.Timestamp,
    spec: EventReplaySpec = EventReplaySpec(),
) -> pd.DataFrame:
    """Replay sequential trades with signal-first, next-snapshot fills."""

    if maximum_holding_seconds < 1:
        raise ValueError("maximum holding seconds must be positive")
    if arm not in DIAGNOSTIC_ARMS:
        raise ValueError(f"unknown diagnostic arm {arm}")
    index_ns = quote_buckets.index.as_unit("ns").asi8
    bid = quote_buckets["last_bid"].to_numpy(dtype=np.float64)
    ask = quote_buckets["last_ask"].to_numpy(dtype=np.float64)
    end_ns = int(_utc_timestamp(decision_end).value)
    max_fill_delay_ns = spec.maximum_fill_delay_milliseconds * 1_000_000
    hold_ns = maximum_holding_seconds * 1_000_000_000
    adverse = spec.adverse_ticks_per_fill * spec.tick_size
    active_exit = -1
    rows: list[dict[str, object]] = []
    skipped_overlap = 0

    for signal in signals:
        signal_ns = int(_utc_timestamp(signal.timestamp).value)
        if signal_ns + hold_ns >= end_ns:
            continue
        signal_row = int(np.searchsorted(index_ns, signal_ns, side="left"))
        if signal_row >= len(index_ns) or index_ns[signal_row] != signal_ns:
            continue
        entry = int(np.searchsorted(index_ns, signal_ns, side="right"))
        if entry >= len(index_ns) or index_ns[entry] >= end_ns:
            continue
        if index_ns[entry] - signal_ns > max_fill_delay_ns:
            continue
        if entry <= active_exit:
            skipped_overlap += 1
            continue
        side = int(signal.side)
        raw_entry = ask[entry] if side > 0 else bid[entry]
        entry_fill = raw_entry + side * adverse
        target = entry_fill + side * spec.target_ticks * spec.tick_size
        stop = entry_fill - side * spec.stop_ticks * spec.tick_size
        deadline = index_ns[entry] + hold_ns
        trigger = -1
        reason = "TIMEOUT"
        cursor = entry + 1
        while cursor < len(index_ns) and index_ns[cursor] <= deadline:
            executable = bid[cursor] if side > 0 else ask[cursor]
            if side > 0 and executable >= target or side < 0 and executable <= target:
                trigger = cursor
                reason = "TARGET"
                break
            if side > 0 and executable <= stop or side < 0 and executable >= stop:
                trigger = cursor
                reason = "STOP"
                break
            cursor += 1
        if trigger < 0:
            trigger = int(np.searchsorted(index_ns, deadline, side="left"))
        exit_row = trigger + 1
        if (
            trigger >= len(index_ns)
            or exit_row >= len(index_ns)
            or index_ns[exit_row] >= end_ns
            or index_ns[exit_row] - index_ns[trigger] > max_fill_delay_ns
        ):
            continue
        raw_exit = bid[exit_row] if side > 0 else ask[exit_row]
        exit_fill = raw_exit - side * adverse
        gross_ticks = side * (exit_fill - entry_fill) / spec.tick_size
        gross_usd = side * (exit_fill - entry_fill) * spec.point_value
        net_usd = gross_usd - spec.round_trip_fee_usd
        rows.append(
            {
                "decision_id": (
                    f"{arm}:{decision_interval_milliseconds}:"
                    f"{maximum_holding_seconds}:{signal_ns}"
                ),
                "arm": arm,
                "decision_interval_ms": decision_interval_milliseconds,
                "maximum_holding_seconds": maximum_holding_seconds,
                "signal_time": pd.Timestamp(signal_ns, unit="ns", tz="UTC"),
                "session_date": str(
                    trading_session_date(
                        pd.Timestamp(signal_ns, unit="ns", tz="UTC").to_pydatetime(
                            warn=False
                        )
                    )
                ),
                "signal_row": signal_row,
                "signal_record_seq": int(
                    quote_buckets.iloc[signal_row]["last_record_seq"]
                ),
                "signal_bid": float(bid[signal_row]),
                "signal_ask": float(ask[signal_row]),
                "signal_mid": float((bid[signal_row] + ask[signal_row]) / 2.0),
                "signal_observation_json": _observation_json(
                    quote_buckets.iloc[signal_row]
                ),
                "signal_score_ticks": signal.score_ticks,
                "side": "LONG" if side > 0 else "SHORT",
                "entry_time": quote_buckets.index[entry],
                "entry_record_seq": int(quote_buckets.iloc[entry]["last_record_seq"]),
                "intended_entry_next_quote_record_seq": int(
                    quote_buckets.iloc[entry]["last_record_seq"]
                ),
                "entry_quote_delay_ms": (index_ns[entry] - signal_ns) / 1e6,
                "raw_entry_quote": raw_entry,
                "entry_fill": entry_fill,
                "exit_trigger_time": quote_buckets.index[trigger],
                "exit_trigger_record_seq": int(
                    quote_buckets.iloc[trigger]["last_record_seq"]
                ),
                "exit_time": quote_buckets.index[exit_row],
                "exit_record_seq": int(quote_buckets.iloc[exit_row]["last_record_seq"]),
                "intended_exit_next_quote_record_seq": int(
                    quote_buckets.iloc[exit_row]["last_record_seq"]
                ),
                "exit_quote_delay_ms": (
                    index_ns[exit_row] - index_ns[trigger]
                ) / 1e6,
                "raw_exit_quote": raw_exit,
                "exit_fill": exit_fill,
                "exit_reason": reason,
                "gross_ticks_after_adverse_fills": gross_ticks,
                "adverse_ticks_per_fill": spec.adverse_ticks_per_fill,
                "fee_usd": spec.round_trip_fee_usd,
                "fixed_cost_ticks_excluding_observed_spread": spec.full_cost_ticks,
                "safety_margin_ticks": spec.safety_margin_ticks,
                "net_usd": net_usd,
                "holding_seconds": (index_ns[exit_row] - index_ns[entry]) / 1e9,
                "skipped_overlap_before_trade": skipped_overlap,
            }
        )
        skipped_overlap = 0
        active_exit = exit_row
    return pd.DataFrame(rows)


def run_event_diagnostic_matrix(
    quote_buckets: pd.DataFrame,
    decision_start: pd.Timestamp,
    decision_end: pd.Timestamp,
    spec: EventReplaySpec = EventReplaySpec(),
    *,
    decision_intervals_milliseconds: tuple[int, ...] = (250, 1_000, 5_000, 10_000),
    maximum_holds_seconds: tuple[int, ...] = (15, 60, 180),
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    ledgers: list[pd.DataFrame] = []
    signal_counts: dict[tuple[int, str], int] = {}
    for interval in decision_intervals_milliseconds:
        signal_sets = build_diagnostic_signals(
            quote_buckets, interval, decision_start, decision_end, spec
        )
        for arm, signals in signal_sets.items():
            signal_counts[(interval, arm)] = len(signals)
            for hold in maximum_holds_seconds:
                ledger = replay_signals(
                    quote_buckets,
                    signals,
                    arm,
                    interval,
                    hold,
                    decision_end,
                    spec,
                )
                if not ledger.empty:
                    ledgers.append(ledger)
    all_trades = pd.concat(ledgers, ignore_index=True) if ledgers else pd.DataFrame()
    metric_rows: list[dict[str, object]] = []
    for interval in decision_intervals_milliseconds:
        for hold in maximum_holds_seconds:
            for arm in DIAGNOSTIC_ARMS:
                trades = (
                    all_trades[
                        (all_trades["decision_interval_ms"] == interval)
                        & (all_trades["maximum_holding_seconds"] == hold)
                        & (all_trades["arm"] == arm)
                    ]
                    if not all_trades.empty
                    else pd.DataFrame()
                )
                metric_rows.append(
                    _diagnostic_metrics(
                        trades, arm, interval, hold, signal_counts.get((interval, arm), 0)
                    )
                )
    metrics = pd.DataFrame(metric_rows)
    summary = {
        "status": DIAGNOSTIC_STATUS,
        "claim": "Mechanics and in-sample diagnostics only; not holdout evidence.",
        "arms": list(DIAGNOSTIC_ARMS),
        "decision_intervals_milliseconds": list(decision_intervals_milliseconds),
        "maximum_holds_seconds": list(maximum_holds_seconds),
        "tested_configurations": int(len(metrics)),
        "full_cost_ticks": spec.full_cost_ticks,
        "cost_model": asdict(spec),
    }
    return all_trades, metrics, summary


def write_diagnostic_artifact(
    output_dir: Path,
    quote_buckets: pd.DataFrame,
    trades: pd.DataFrame,
    metrics: pd.DataFrame,
    audit: QuoteAggregationAudit,
    summary: Mapping[str, object],
    source_paths: Iterable[Path],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    quote_buckets.to_parquet(output_dir / "quote_buckets_250ms.parquet")
    trades.to_csv(output_dir / "trade_ledger.csv", index=False)
    metrics.to_csv(output_dir / "metrics.csv", index=False)
    payload = dict(summary)
    payload["aggregation_audit"] = asdict(audit)
    payload["quote_bucket_count"] = len(quote_buckets)
    payload["source_manifest"] = [
        {
            "path": str(Path(path).resolve()),
            "captured_prefix_bytes": Path(path).stat().st_size,
            "captured_prefix_sha256": _sha256_prefix(
                Path(path), Path(path).stat().st_size
            ),
        }
        for path in source_paths
    ]
    (output_dir / "diagnostic_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _downsample_decisions(frame: pd.DataFrame, interval_ms: int) -> pd.DataFrame:
    if interval_ms == 250:
        return frame.copy()
    bucket = frame.index.as_unit("ns").asi8 // (interval_ms * 1_000_000)
    work = frame.copy()
    work["_bucket"] = bucket
    grouped = work.groupby("_bucket", sort=False)
    result = grouped.agg(
        segment_id=("segment_id", "last"),
        first_mid=("first_mid", "first"),
        last_mid=("last_mid", "last"),
        last_bid=("last_bid", "last"),
        last_ask=("last_ask", "last"),
        last_bid_size=("last_bid_size", "last"),
        last_ask_size=("last_ask_size", "last"),
        last_spread_ticks=("last_spread_ticks", "last"),
        event_count=("event_count", "sum"),
        trade_count=("trade_count", "sum"),
        trade_volume=("trade_volume", "sum"),
        trade_notional=("trade_notional", "sum"),
        signed_volume=("signed_volume", "sum"),
        session_vwap_distance_ticks=("session_vwap_distance_ticks", "last"),
        ema_trend_ticks=("ema_trend_ticks", "last"),
        trend_efficiency_60s=("trend_efficiency_60s", "last"),
        first_record_seq=("first_record_seq", "first"),
        last_record_seq=("last_record_seq", "last"),
    )
    timestamps = grouped.apply(lambda values: values.index[-1], include_groups=False)
    result.index = pd.DatetimeIndex(timestamps.to_numpy(), name="observed_at")
    return result


def _add_causal_context(
    frame: pd.DataFrame, session_start: pd.Timestamp, tick_size: float
) -> pd.DataFrame:
    result = frame.copy()
    timestamps = result.index.as_unit("ns").asi8
    mid = result["last_mid"].to_numpy(dtype=np.float64)
    volume = result["trade_volume"].to_numpy(dtype=np.float64)
    notional = result["trade_notional"].to_numpy(dtype=np.float64)
    session_mask = timestamps >= int(session_start.value)
    cumulative_volume = np.cumsum(np.where(session_mask, volume, 0.0))
    cumulative_notional = np.cumsum(np.where(session_mask, notional, 0.0))
    vwap = np.divide(
        cumulative_notional,
        cumulative_volume,
        out=mid.copy(),
        where=cumulative_volume > 0.0,
    )
    result["session_vwap_distance_ticks"] = (mid - vwap) / tick_size
    fast = _time_ema(mid, timestamps, 30.0)
    slow = _time_ema(mid, timestamps, 180.0)
    result["ema_trend_ticks"] = (fast - slow) / tick_size
    starts = np.searchsorted(timestamps, timestamps - 60_000_000_000, side="left")
    changes = np.r_[0.0, np.abs(np.diff(mid)) / tick_size]
    cumulative_path = np.cumsum(changes)
    before = np.maximum(starts - 1, 0)
    path = cumulative_path - np.where(starts > 0, cumulative_path[before], 0.0)
    net = (mid - mid[starts]) / tick_size
    result["trend_efficiency_60s"] = np.divide(
        np.abs(net), path, out=np.zeros(len(mid)), where=path > 0.0
    )
    return result


def _time_ema(values: np.ndarray, timestamps: np.ndarray, halflife_seconds: float) -> np.ndarray:
    result = np.empty(len(values), dtype=np.float64)
    result[0] = values[0]
    decay = math.log(2.0) / halflife_seconds
    for row in range(1, len(values)):
        elapsed = max((timestamps[row] - timestamps[row - 1]) / 1e9, 0.0)
        alpha = 1.0 - math.exp(-decay * elapsed)
        result[row] = result[row - 1] + alpha * (values[row] - result[row - 1])
    return result


def _segment_ids(index: pd.DatetimeIndex, maximum_gap_ms: int) -> np.ndarray:
    timestamps = index.as_unit("ns").asi8
    boundary = np.r_[True, np.diff(timestamps) > maximum_gap_ms * 1_000_000]
    return np.cumsum(boundary, dtype=np.int64) - 1


def _diagnostic_metrics(
    trades: pd.DataFrame,
    arm: str,
    interval: int,
    hold: int,
    signals: int,
) -> dict[str, object]:
    if trades.empty:
        return {
            "arm": arm,
            "decision_interval_ms": interval,
            "maximum_holding_seconds": hold,
            "signals": signals,
            "trades": 0,
            "longs": 0,
            "shorts": 0,
            "net_usd": 0.0,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "maximum_drawdown_usd": 0.0,
            "mean_holding_seconds": 0.0,
            "direction_reversals": 0,
            "max_entries_one_minute": 0,
        }
    pnl = trades["net_usd"].to_numpy(dtype=np.float64)
    wins = float(pnl[pnl > 0.0].sum())
    losses = float(-pnl[pnl < 0.0].sum())
    cumulative = np.cumsum(pnl)
    drawdown = np.maximum.accumulate(np.r_[0.0, cumulative]) - np.r_[0.0, cumulative]
    sides = trades["side"].astype(str).to_numpy()
    reversals = int(np.sum(sides[1:] != sides[:-1]))
    per_minute = trades.groupby(pd.to_datetime(trades["signal_time"]).dt.floor("min")).size()
    return {
        "arm": arm,
        "decision_interval_ms": interval,
        "maximum_holding_seconds": hold,
        "signals": signals,
        "trades": len(trades),
        "longs": int((sides == "LONG").sum()),
        "shorts": int((sides == "SHORT").sum()),
        "net_usd": float(pnl.sum()),
        "profit_factor": wins / losses if losses > 0.0 else math.inf if wins > 0.0 else 0.0,
        "win_rate": float(np.mean(pnl > 0.0)),
        "maximum_drawdown_usd": float(drawdown.max(initial=0.0)),
        "mean_holding_seconds": float(trades["holding_seconds"].mean()),
        "direction_reversals": reversals,
        "max_entries_one_minute": int(per_minute.max()) if len(per_minute) else 0,
    }


def _utc_timestamp(value: pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def _observation_json(row: pd.Series) -> str:
    payload: dict[str, object] = {}
    for name, value in row.items():
        if pd.isna(value):
            payload[str(name)] = None
        elif isinstance(value, (np.integer, int)):
            payload[str(name)] = int(value)
        elif isinstance(value, (np.floating, float)):
            payload[str(name)] = float(value)
        else:
            payload[str(name)] = str(value)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sha256_prefix(path: Path, byte_count: int) -> str:
    if byte_count < 0:
        raise ValueError("byte count cannot be negative")
    digest = hashlib.sha256()
    remaining = byte_count
    with path.open("rb") as stream:
        while remaining:
            block = stream.read(min(4 * 1024 * 1024, remaining))
            if not block:
                raise ValueError(f"source file shortened while hashing: {path}")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


__all__ = [
    "DIAGNOSTIC_ARMS",
    "DIAGNOSTIC_STATUS",
    "EventReplaySpec",
    "QuoteAggregationAudit",
    "ReplaySignal",
    "aggregate_quote_buckets",
    "build_diagnostic_signals",
    "replay_signals",
    "run_event_diagnostic_matrix",
    "write_diagnostic_artifact",
]
