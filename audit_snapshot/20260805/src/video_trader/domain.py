from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class EventType(str, Enum):
    QUEUED = "QUEUED"
    ACCEPTED = "ACCEPTED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class MarketSnapshot:
    timestamp: datetime
    candle_open: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.candle_open.tzinfo is None:
            raise ValueError("market timestamps must be timezone-aware")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC ordering")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")


@dataclass(frozen=True)
class Prediction:
    probability_up: float
    model_version: str
    horizon_seconds: int

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability_up <= 1.0:
            raise ValueError("probability_up must be between 0 and 1")

    @property
    def confidence(self) -> float:
        return abs(self.probability_up - 0.5) * 2.0


@dataclass(frozen=True)
class TradeIntent:
    client_order_id: str
    symbol: str
    side: Side
    quantity: int
    entry_price: float
    stop_price: float
    target_price: float
    created_at: datetime
    candle_open: datetime
    prediction: Prediction


@dataclass(frozen=True)
class ExecutionEvent:
    sequence: int
    event_type: EventType
    client_order_id: str
    timestamp: datetime
    side: Side
    quantity: int
    price: float
    realized_pnl: float = 0.0
    commission: float = 0.0
    reason: str = ""


@dataclass
class OpenTrade:
    intent: TradeIntent
    fill_price: float
    filled_at: datetime
