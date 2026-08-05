from __future__ import annotations

import numpy as np
import pandas as pd


CANDLE_STATE_MODELS = (
    "previous_close",
    "previous_close_ladder",
    "ohlc_breakout",
)
PREDECLARED_CAUSAL_MODELS = (
    "candidate_1",
    "candidate_2",
    "candidate_3a",
    "candidate_3b",
)


def _persistent_transition(
    raw_side: np.ndarray,
    score: np.ndarray,
    minute_codes: np.ndarray,
    segments: np.ndarray,
) -> np.ndarray:
    """Emit once after two consecutive observations agree on a new state."""
    previous_side = np.r_[0.0, raw_side[:-1]]
    previous_previous_side = np.r_[0.0, 0.0, raw_side[:-2]]
    same_context = np.r_[
        False,
        (minute_codes[1:] == minute_codes[:-1])
        & (segments[1:] == segments[:-1]),
    ]
    persisted = same_context & (raw_side != 0.0) & (raw_side == previous_side)
    transitioned = persisted & (previous_previous_side != raw_side)
    return np.where(transitioned, np.sign(raw_side) * np.abs(score), 0.0)


def _persistent_state(
    raw_side: np.ndarray,
    score: np.ndarray,
    minute_codes: np.ndarray,
    segments: np.ndarray,
) -> np.ndarray:
    """Keep emitting while two consecutive observations agree on the state."""
    previous_side = np.r_[0.0, raw_side[:-1]]
    same_context = np.r_[
        False,
        (minute_codes[1:] == minute_codes[:-1])
        & (segments[1:] == segments[:-1]),
    ]
    persisted = same_context & (raw_side != 0.0) & (raw_side == previous_side)
    return np.where(persisted, np.sign(raw_side) * np.abs(score), 0.0)


def _neutral_rearmed_transition(
    raw_side: np.ndarray,
    score: np.ndarray,
    minute_codes: np.ndarray,
    segments: np.ndarray,
) -> np.ndarray:
    """Require two confirmations and an observed neutral state before re-entry."""

    output = np.zeros(len(raw_side), dtype=np.float64)
    armed = True
    pending_side = 0.0
    confirmation_count = 0
    previous_minute = -1
    previous_segment = -1
    for row, side in enumerate(raw_side):
        minute = int(minute_codes[row])
        segment = int(segments[row])
        if minute != previous_minute or segment != previous_segment:
            armed = True
            pending_side = 0.0
            confirmation_count = 0
        previous_minute = minute
        previous_segment = segment
        if side == 0.0:
            armed = True
            pending_side = 0.0
            confirmation_count = 0
            continue
        if side == pending_side:
            confirmation_count += 1
        else:
            pending_side = side
            confirmation_count = 1
        if armed and confirmation_count >= 2:
            output[row] = np.sign(side) * abs(float(score[row]))
            armed = False
    return output


def build_candle_state_predictions(
    seconds: pd.DataFrame,
    tick_size: float,
    minimum_displacement_ticks: float = 2.0,
    minimum_completed_bars: int = 20,
) -> dict[str, np.ndarray]:
    """Build causal, sub-minute signals from the last completed one-minute bar.

    The candle uses quote mids available in the second cache. Every OHLC field is
    shifted by one complete minute before it is mapped back to an observation.
    """
    if tick_size <= 0.0:
        raise ValueError("tick_size must be positive")
    if minimum_displacement_ticks < 0.0:
        raise ValueError("minimum_displacement_ticks cannot be negative")
    if minimum_completed_bars < 1:
        raise ValueError("minimum_completed_bars must be positive")
    required = {"segment_id", "first_mid", "last_mid", "last_spread_ticks"}
    missing = sorted(required.difference(seconds.columns))
    if missing:
        raise ValueError(f"missing candle-state columns: {missing}")
    if not seconds.index.is_monotonic_increasing:
        raise ValueError("seconds must be sorted by timestamp")

    segments = seconds["segment_id"].to_numpy(dtype=np.int64)
    minute = seconds.index.floor("min")
    minute_codes, minute_uniques = pd.factorize(
        pd.MultiIndex.from_arrays([segments, minute]), sort=False
    )
    mid = seconds["last_mid"].to_numpy(dtype=np.float64)
    first_mid = seconds["first_mid"].to_numpy(dtype=np.float64)

    observations = pd.DataFrame(
        {
            "segment_id": segments,
            "minute_code": minute_codes,
            "first_mid": first_mid,
            "last_mid": mid,
        }
    )
    bars = observations.groupby("minute_code", sort=False).agg(
        segment_id=("segment_id", "first"),
        open=("first_mid", "first"),
        first_high=("first_mid", "max"),
        first_low=("first_mid", "min"),
        last_high=("last_mid", "max"),
        last_low=("last_mid", "min"),
        close=("last_mid", "last"),
    )
    bars["minute"] = pd.DatetimeIndex(
        [minute_uniques[code][1] for code in bars.index]
    )
    bars["high"] = bars[["first_high", "last_high"]].max(axis=1)
    bars["low"] = bars[["first_low", "last_low"]].min(axis=1)
    span = (bars["high"] - bars["low"]).clip(lower=tick_size)
    bars["body_ratio"] = (bars["close"] - bars["open"]).abs() / span
    bars["close_location"] = (bars["close"] - bars["low"]) / span
    bars["upper_wick_ratio"] = (
        bars["high"] - bars[["open", "close"]].max(axis=1)
    ) / span
    bars["lower_wick_ratio"] = (
        bars[["open", "close"]].min(axis=1) - bars["low"]
    ) / span
    bars["range_ticks"] = span / tick_size
    prior_bar_close = bars.groupby("segment_id", sort=False)["close"].shift(1)
    high_gap = ((bars["high"] - prior_bar_close).abs() / tick_size).fillna(
        bars["range_ticks"]
    )
    low_gap = ((bars["low"] - prior_bar_close).abs() / tick_size).fillna(
        bars["range_ticks"]
    )
    bars["true_range_ticks"] = np.maximum.reduce(
        [
            bars["range_ticks"].to_numpy(dtype=np.float64),
            high_gap.to_numpy(dtype=np.float64),
            low_gap.to_numpy(dtype=np.float64),
        ]
    )
    bars["atr20_ticks"] = bars.groupby("segment_id", sort=False)[
        "true_range_ticks"
    ].transform(
        lambda values: values.rolling(
            20, min_periods=minimum_completed_bars
        ).mean()
    )

    candle_columns = [
        "minute",
        "open",
        "high",
        "low",
        "close",
        "body_ratio",
        "close_location",
        "upper_wick_ratio",
        "lower_wick_ratio",
        "range_ticks",
        "atr20_ticks",
    ]
    shifted = bars.groupby("segment_id", sort=False)[candle_columns].shift(1)
    shifted.columns = [f"prior_{name}" for name in shifted.columns]
    bars = pd.concat([bars, shifted], axis=1)

    prior = bars.loc[minute_codes]
    adjacent = (
        prior["minute"].to_numpy(dtype="datetime64[ns]")
        - prior["prior_minute"].to_numpy(dtype="datetime64[ns]")
        == np.timedelta64(1, "m")
    )
    prior_close = prior["prior_close"].to_numpy(dtype=np.float64)
    displacement = (mid - prior_close) / tick_size
    prior_atr = prior["prior_atr20_ticks"].to_numpy(dtype=np.float64)
    spread = seconds["last_spread_ticks"].to_numpy(dtype=np.float64)
    spread_median = pd.Series(spread).groupby(segments, sort=False).transform(
        lambda values: values.rolling(60, min_periods=20).median()
    ).to_numpy(dtype=np.float64)
    finite_prior = adjacent & np.isfinite(prior_close) & np.isfinite(prior_atr)
    liquid = (
        finite_prior
        & np.isfinite(spread)
        & np.isfinite(spread_median)
        & (spread > 0.0)
        & (spread <= 2.0 * spread_median)
        & (spread <= 0.15 * prior_atr)
    )
    dynamic_buffer = np.maximum.reduce(
        [
            np.full(len(seconds), minimum_displacement_ticks),
            1.5 * np.nan_to_num(spread, nan=np.inf),
            0.10 * np.nan_to_num(prior_atr, nan=np.inf),
        ]
    )

    displacement_side = np.where(
        liquid & (np.abs(displacement) >= dynamic_buffer),
        np.sign(displacement),
        0.0,
    )
    previous_close = _persistent_transition(
        displacement_side,
        np.nan_to_num(displacement),
        minute_codes,
        segments,
    )
    previous_close_ladder = _persistent_state(
        displacement_side,
        np.nan_to_num(displacement),
        minute_codes,
        segments,
    )

    prior_open = prior["prior_open"].to_numpy(dtype=np.float64)
    prior_high = prior["prior_high"].to_numpy(dtype=np.float64)
    prior_low = prior["prior_low"].to_numpy(dtype=np.float64)
    body_ratio = prior["prior_body_ratio"].to_numpy(dtype=np.float64)
    close_location = prior["prior_close_location"].to_numpy(dtype=np.float64)
    upper_wick = prior["prior_upper_wick_ratio"].to_numpy(dtype=np.float64)
    lower_wick = prior["prior_lower_wick_ratio"].to_numpy(dtype=np.float64)
    range_ticks = prior["prior_range_ticks"].to_numpy(dtype=np.float64)
    bullish_context = (
        finite_prior
        & (prior_close > prior_open)
        & (body_ratio >= 0.50)
        & (close_location >= 0.60)
        & (upper_wick <= 0.30)
    )
    bearish_context = (
        finite_prior
        & (prior_close < prior_open)
        & (body_ratio >= 0.50)
        & (close_location <= 0.40)
        & (lower_wick <= 0.30)
    )
    breakout_side = np.where(
        bullish_context & liquid & (mid >= prior_high + dynamic_buffer * tick_size),
        1.0,
        np.where(
            bearish_context
            & liquid
            & (mid <= prior_low - dynamic_buffer * tick_size),
            -1.0,
            0.0,
        ),
    )
    breakout_distance = np.where(
        breakout_side > 0.0,
        (mid - prior_high) / tick_size,
        np.where(breakout_side < 0.0, (prior_low - mid) / tick_size, 0.0),
    )
    breakout_score = np.nan_to_num(range_ticks) + np.maximum(
        np.nan_to_num(breakout_distance), 0.0
    )
    ohlc_breakout = _persistent_transition(
        breakout_side,
        breakout_score,
        minute_codes,
        segments,
    )

    candidate_1 = _neutral_rearmed_transition(
        displacement_side,
        np.nan_to_num(displacement),
        minute_codes,
        segments,
    )
    running_high = pd.Series(mid).groupby(minute_codes, sort=False).cummax().to_numpy()
    running_low = pd.Series(mid).groupby(minute_codes, sort=False).cummin().to_numpy()
    high_boundary = prior_high + dynamic_buffer * tick_size
    low_boundary = prior_low - dynamic_buffer * tick_size
    broke_high = running_high >= high_boundary
    broke_low = running_low <= low_boundary
    candidate_2_side = np.select(
        [
            liquid & (mid >= high_boundary),
            liquid & (mid <= low_boundary),
            liquid & broke_low & (mid >= prior_low),
            liquid & broke_high & (mid <= prior_high),
        ],
        [1.0, -1.0, 1.0, -1.0],
        default=0.0,
    )
    boundary_distance = np.where(
        candidate_2_side > 0.0,
        np.minimum(
            np.abs(mid - prior_high),
            np.abs(mid - prior_low),
        ) / tick_size,
        np.where(
            candidate_2_side < 0.0,
            np.minimum(
                np.abs(prior_high - mid),
                np.abs(prior_low - mid),
            ) / tick_size,
            0.0,
        ),
    )
    candidate_2_score = np.nan_to_num(range_ticks) + np.maximum(
        np.nan_to_num(boundary_distance), 0.0
    )
    # A reclaim is genuinely new information after a break, so Candidate 2 may
    # reverse directly after two confirming observations.
    candidate_2 = _persistent_transition(
        candidate_2_side,
        candidate_2_score,
        minute_codes,
        segments,
    )

    def prior_completed_value(column: str, aggregation: str) -> np.ndarray:
        if column not in seconds.columns:
            return np.full(len(seconds), np.nan, dtype=np.float64)
        values = seconds[column].to_numpy(dtype=np.float64)
        minute_frame = pd.DataFrame(
            {
                "minute_code": minute_codes,
                "segment_id": segments,
                "value": values,
            }
        )
        grouped = minute_frame.groupby("minute_code", sort=False)
        if aggregation == "sum":
            complete = grouped["value"].sum()
        elif aggregation == "last":
            complete = grouped["value"].last()
        else:
            raise ValueError(f"unsupported completed-minute aggregation {aggregation}")
        minute_segments = grouped["segment_id"].first()
        shifted_values = complete.groupby(minute_segments, sort=False).shift(1)
        return shifted_values.reindex(minute_codes).to_numpy(dtype=np.float64)

    prior_vwap_distance = prior_completed_value(
        "session_vwap_distance_ticks", "last"
    )
    prior_trend = prior_completed_value("ema_trend_ticks", "last")
    prior_efficiency = prior_completed_value("trend_efficiency_60s", "last")
    prior_order_flow = prior_completed_value("signed_volume", "sum")
    ordinary_volatility = (
        np.isfinite(prior_atr)
        & (range_ticks >= 0.50 * prior_atr)
        & (range_ticks <= 2.00 * prior_atr)
    )
    long_regime = (
        ordinary_volatility
        & (prior_vwap_distance >= 0.0)
        & (prior_trend >= 0.25)
        & (prior_efficiency >= 0.10)
    )
    short_regime = (
        ordinary_volatility
        & (prior_vwap_distance <= 0.0)
        & (prior_trend <= -0.25)
        & (prior_efficiency >= 0.10)
    )
    candidate_3a = np.where(
        ((candidate_2 > 0.0) & long_regime)
        | ((candidate_2 < 0.0) & short_regime),
        candidate_2,
        0.0,
    )
    flow_confirms = ((candidate_3a > 0.0) & (prior_order_flow > 0.0)) | (
        (candidate_3a < 0.0) & (prior_order_flow < 0.0)
    )
    candidate_3b = np.where(flow_confirms, candidate_3a, 0.0)
    return {
        "previous_close": previous_close,
        "previous_close_ladder": previous_close_ladder,
        "ohlc_breakout": ohlc_breakout,
        "candidate_1": candidate_1,
        "candidate_2": candidate_2,
        "candidate_3a": candidate_3a,
        "candidate_3b": candidate_3b,
    }


__all__ = [
    "CANDLE_STATE_MODELS",
    "PREDECLARED_CAUSAL_MODELS",
    "build_candle_state_predictions",
]
