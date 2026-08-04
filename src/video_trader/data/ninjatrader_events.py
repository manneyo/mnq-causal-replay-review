from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping

import numpy as np
import pandas as pd


SCHEMA_VERSION = 2
SUPPORTED_EVENT_TYPES = frozenset({"TRADE", "BID", "ASK"})
LEGACY_EVENT_COLUMNS = frozenset(
    {
        "timestamp_utc_ns",
        "instrument",
        "event_type",
        "price",
        "volume",
        "state",
    }
)
V2_IDENTITY_COLUMNS = frozenset(
    {
        "schema_version",
        "recorder_run_id",
        "file_part",
        "record_seq",
        "event_id",
        "receive_time_utc_ns",
    }
)
REQUIRED_EVENT_COLUMNS = LEGACY_EVENT_COLUMNS


class EventReplayError(ValueError):
    """Base class for deterministic replay validation failures."""


class EventSchemaError(EventReplayError):
    """The CSV schema cannot support the selected replay contract."""


class EventValidationError(EventReplayError):
    """An event row is malformed or contradicts its persisted identity."""


class EventSequenceError(EventReplayError):
    """A v2 run or file rotation is incomplete or out of order."""


class DuplicateEventIdError(EventSequenceError):
    """A persisted event identity was observed more than once."""


@dataclass(frozen=True, slots=True)
class NormalizedNinjaTraderEvent:
    schema_version: int
    event_id: str
    recorder_run_id: str | None
    record_seq: int | None
    file_part: int
    manifest_position: int
    physical_row: int
    source_file: str
    timestamp_utc_ns: int
    receive_time_utc_ns: int | None
    instrument: str
    event_type: str
    price: float
    volume: float
    state: str
    is_legacy: bool
    is_run_start: bool
    source_time_regression: bool
    best_bid_after: float | None
    best_ask_after: float | None
    best_bid_size_after: float | None
    best_ask_size_after: float | None
    quote_status: str


@dataclass(slots=True)
class _QuoteState:
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    def apply(self, event_type: str, price: float, volume: float) -> None:
        if event_type == "BID":
            self.bid = price
            self.bid_size = volume
        elif event_type == "ASK":
            self.ask = price
            self.ask_size = volume

    @property
    def status(self) -> str:
        if self.bid is None or self.ask is None:
            return "INCOMPLETE"
        if self.ask > self.bid:
            return "VALID"
        if self.ask == self.bid:
            return "LOCKED"
        return "CROSSED"


@dataclass(slots=True)
class _V2SequenceTracker:
    run_id: str | None = None
    record_seq: int = 0
    file_part: int = -1
    file_manifest_position: int = -1
    closed_run_ids: set[str] | None = None

    def __post_init__(self) -> None:
        if self.closed_run_ids is None:
            self.closed_run_ids = set()

    def accept(
        self,
        *,
        run_id: str,
        record_seq: int,
        file_part: int,
        event_id: str,
        manifest_position: int,
        path: Path,
        physical_row: int,
    ) -> bool:
        location = _location(path, physical_row)
        first_row_in_file = manifest_position != self.file_manifest_position
        is_run_start = self.run_id is None or run_id != self.run_id

        if is_run_start:
            if self.run_id is not None and not first_row_in_file:
                raise EventSequenceError(
                    f"{location}: recorder_run_id changed inside one CSV"
                )
            if run_id in (self.closed_run_ids or set()):
                raise DuplicateEventIdError(
                    f"{location}: recorder run {run_id!r} reappeared after it closed"
                )
            if self.run_id is not None:
                assert self.closed_run_ids is not None
                self.closed_run_ids.add(self.run_id)
            if record_seq != 1:
                raise EventSequenceError(
                    f"{location}: new recorder run must start at record_seq 1; "
                    f"found {record_seq}"
                )
            if file_part != 0:
                raise EventSequenceError(
                    f"{location}: new recorder run must start at file_part 0; "
                    f"found {file_part}"
                )
            self.run_id = run_id
            self.record_seq = 0
            self.file_part = -1
        elif first_row_in_file:
            expected_part = self.file_part + 1
            if file_part != expected_part:
                raise EventSequenceError(
                    f"{location}: expected file_part {expected_part}; found {file_part}"
                )
        elif file_part != self.file_part:
            raise EventSequenceError(
                f"{location}: file_part changed inside one CSV from "
                f"{self.file_part} to {file_part}"
            )

        expected_seq = self.record_seq + 1
        if record_seq == self.record_seq:
            raise DuplicateEventIdError(
                f"{location}: duplicate persisted event_id {event_id!r}"
            )
        if record_seq != expected_seq:
            raise EventSequenceError(
                f"{location}: expected record_seq {expected_seq}; found {record_seq}"
            )

        self.record_seq = record_seq
        self.file_part = file_part
        self.file_manifest_position = manifest_position
        return is_run_start


def make_event_id(recorder_run_id: str, record_seq: int) -> str:
    run_id = recorder_run_id.strip()
    if not run_id:
        raise ValueError("recorder_run_id cannot be empty")
    if record_seq < 1:
        raise ValueError("record_seq must be positive")
    return f"{run_id}:{record_seq:020d}"


def iter_ninjatrader_event_exports(
    paths: Iterable[Path],
) -> Iterator[NormalizedNinjaTraderEvent]:
    """Stream event exports in caller-supplied manifest and physical row order.

    V2 files fail closed when persisted identity or rotation continuity is invalid.
    Legacy files preserve every physical row and receive deterministic positional
    IDs; those IDs do not claim that content-identical writes are duplicates.
    """

    ordered_paths = tuple(Path(path) for path in paths)
    if not ordered_paths:
        raise ValueError("at least one NinjaTrader event export is required")

    schema_mode: int | None = None
    sequence = _V2SequenceTracker()
    quotes: dict[str, _QuoteState] = {}
    previous_source_time: int | None = None

    for manifest_position, path in enumerate(ordered_paths):
        try:
            stream = path.open("r", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise EventReplayError(f"cannot open event export {path}: {exc}") from exc

        with stream:
            reader = csv.DictReader(stream)
            fieldnames = tuple(reader.fieldnames or ())
            missing = sorted(LEGACY_EVENT_COLUMNS.difference(fieldnames))
            if missing:
                raise EventSchemaError(
                    f"{path} is missing NinjaTrader event columns: {missing}"
                )

            identity_present = V2_IDENTITY_COLUMNS.intersection(fieldnames)
            if identity_present and identity_present != V2_IDENTITY_COLUMNS:
                missing_identity = sorted(V2_IDENTITY_COLUMNS.difference(fieldnames))
                raise EventSchemaError(
                    f"{path} has a partial v2 identity schema; missing: "
                    f"{missing_identity}"
                )
            file_schema_mode = SCHEMA_VERSION if identity_present else 1
            if schema_mode is None:
                schema_mode = file_schema_mode
            elif schema_mode != file_schema_mode:
                raise EventSchemaError(
                    f"{path} mixes legacy and v2 event files in one replay"
                )

            rows_seen = 0
            for physical_row, row in enumerate(reader, start=2):
                rows_seen += 1
                if None in row:
                    raise EventValidationError(
                        f"{_location(path, physical_row)}: row has extra CSV fields"
                    )
                parsed = _parse_event_row(row, path, physical_row, file_schema_mode)

                if file_schema_mode == SCHEMA_VERSION:
                    run_id = parsed["recorder_run_id"]
                    record_seq = parsed["record_seq"]
                    file_part = parsed["file_part"]
                    event_id = parsed["event_id"]
                    assert isinstance(run_id, str)
                    assert isinstance(record_seq, int)
                    assert isinstance(file_part, int)
                    assert isinstance(event_id, str)
                    is_run_start = sequence.accept(
                        run_id=run_id,
                        record_seq=record_seq,
                        file_part=file_part,
                        event_id=event_id,
                        manifest_position=manifest_position,
                        path=path,
                        physical_row=physical_row,
                    )
                    if is_run_start:
                        # A restart can hide arbitrary callbacks. Carrying a prior
                        # book through that gap would manufacture quote state.
                        quotes.clear()
                    is_legacy = False
                else:
                    run_id = None
                    record_seq = None
                    file_part = manifest_position
                    event_id = (
                        f"legacy:{manifest_position:06d}:{physical_row:012d}"
                    )
                    is_legacy = True
                    is_run_start = manifest_position == 0 and physical_row == 2

                source_time = parsed["timestamp_utc_ns"]
                assert isinstance(source_time, int)
                regression = (
                    previous_source_time is not None
                    and source_time < previous_source_time
                )
                previous_source_time = source_time

                instrument = parsed["instrument"]
                event_type = parsed["event_type"]
                price = parsed["price"]
                volume = parsed["volume"]
                assert isinstance(instrument, str)
                assert isinstance(event_type, str)
                assert isinstance(price, float)
                assert isinstance(volume, float)
                quote = quotes.setdefault(instrument, _QuoteState())
                quote.apply(event_type, price, volume)

                yield NormalizedNinjaTraderEvent(
                    schema_version=file_schema_mode,
                    event_id=event_id,
                    recorder_run_id=run_id,
                    record_seq=record_seq,
                    file_part=file_part,
                    manifest_position=manifest_position,
                    physical_row=physical_row,
                    source_file=path.name,
                    timestamp_utc_ns=source_time,
                    receive_time_utc_ns=parsed["receive_time_utc_ns"],
                    instrument=instrument,
                    event_type=event_type,
                    price=price,
                    volume=volume,
                    state=parsed["state"],
                    is_legacy=is_legacy,
                    is_run_start=is_run_start,
                    source_time_regression=regression,
                    best_bid_after=quote.bid,
                    best_ask_after=quote.ask,
                    best_bid_size_after=quote.bid_size,
                    best_ask_size_after=quote.ask_size,
                    quote_status=quote.status,
                )

            if rows_seen == 0:
                raise EventValidationError(f"{path}: event export has no data rows")


def load_ninjatrader_event_exports(paths: Iterable[Path]) -> pd.DataFrame:
    """Compatibility adapter that materializes the causal stream as a DataFrame."""

    events = pd.DataFrame(
        asdict(event) for event in iter_ninjatrader_event_exports(paths)
    )
    events.index = pd.to_datetime(
        events.pop("timestamp_utc_ns"), unit="ns", utc=True
    )
    events.index.name = "ts_event"
    return events


def ninjatrader_events_to_tbbo_proxy(events: pd.DataFrame) -> pd.DataFrame:
    """Build a trade-linked BBO proxy without changing physical event order."""

    if not isinstance(events.index, pd.DatetimeIndex):
        raise ValueError("NinjaTrader events must use a DatetimeIndex")
    work = events.copy()
    work["event_type"] = work["event_type"].astype(str).str.upper()
    instruments = work["instrument"].astype(str)

    normalized_columns = {
        "best_bid_after",
        "best_ask_after",
        "best_bid_size_after",
        "best_ask_size_after",
    }
    if normalized_columns.issubset(work.columns):
        work["bid"] = pd.to_numeric(work["best_bid_after"], errors="coerce")
        work["ask"] = pd.to_numeric(work["best_ask_after"], errors="coerce")
        work["bid_size"] = pd.to_numeric(
            work["best_bid_size_after"], errors="coerce"
        )
        work["ask_size"] = pd.to_numeric(
            work["best_ask_size_after"], errors="coerce"
        )
    else:
        work["bid"] = work["price"].where(work["event_type"].eq("BID"))
        work["ask"] = work["price"].where(work["event_type"].eq("ASK"))
        work["bid_size"] = work["volume"].where(work["event_type"].eq("BID"))
        work["ask_size"] = work["volume"].where(work["event_type"].eq("ASK"))
        for column in ("bid", "ask", "bid_size", "ask_size"):
            work[column] = work[column].groupby(instruments, sort=False).ffill()

    trades = work[
        work["event_type"].eq("TRADE")
        & work["bid"].notna()
        & work["ask"].notna()
        & (work["ask"] > work["bid"])
        & (work["volume"] > 0.0)
    ].copy()
    midpoint = (trades["bid"] + trades["ask"]) / 2.0
    side = np.where(
        trades["price"] >= trades["ask"],
        "B",
        np.where(
            trades["price"] <= trades["bid"],
            "A",
            np.where(trades["price"] >= midpoint, "B", "A"),
        ),
    )
    proxy_data: dict[str, object] = {
        "price": trades["price"].to_numpy(dtype=np.float64),
        "size": trades["volume"].to_numpy(dtype=np.float64),
        "side": side,
        "bid_px_00": trades["bid"].to_numpy(dtype=np.float64),
        "ask_px_00": trades["ask"].to_numpy(dtype=np.float64),
        "bid_sz_00": trades["bid_size"].to_numpy(dtype=np.float64),
        "ask_sz_00": trades["ask_size"].to_numpy(dtype=np.float64),
        "bid_ct_00": np.ones(len(trades), dtype=np.int64),
        "ask_ct_00": np.ones(len(trades), dtype=np.int64),
        "contract_id": trades["instrument"].astype(str).to_numpy(),
        "market": "MNQ",
    }
    if "event_id" in trades.columns:
        proxy_data["source_event_id"] = trades["event_id"].astype(str).to_numpy()
    proxy = pd.DataFrame(proxy_data, index=trades.index)
    proxy.index.name = "ts_event"
    return proxy


def _parse_event_row(
    row: Mapping[str, str | None],
    path: Path,
    physical_row: int,
    schema_mode: int,
) -> dict[str, str | int | float | None]:
    location = _location(path, physical_row)
    timestamp = _parse_int(row, "timestamp_utc_ns", location, minimum=0)
    price = _parse_float(row, "price", location, minimum_exclusive=0.0)
    volume = _parse_float(row, "volume", location, minimum=0.0)
    instrument = _required_text(row, "instrument", location)
    state = _required_text(row, "state", location)
    event_type = _required_text(row, "event_type", location).upper()
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise EventValidationError(
            f"{location}: unsupported event_type {event_type!r}"
        )

    result: dict[str, str | int | float | None] = {
        "timestamp_utc_ns": timestamp,
        "instrument": instrument,
        "event_type": event_type,
        "price": price,
        "volume": volume,
        "state": state,
        "receive_time_utc_ns": None,
        "recorder_run_id": None,
        "record_seq": None,
        "file_part": None,
        "event_id": None,
    }
    if schema_mode == SCHEMA_VERSION:
        version = _parse_int(row, "schema_version", location, minimum=1)
        if version != SCHEMA_VERSION:
            raise EventSchemaError(
                f"{location}: expected schema_version {SCHEMA_VERSION}; found {version}"
            )
        run_id = _required_text(row, "recorder_run_id", location)
        file_part = _parse_int(row, "file_part", location, minimum=0)
        record_seq = _parse_int(row, "record_seq", location, minimum=1)
        event_id = _required_text(row, "event_id", location)
        expected_event_id = make_event_id(run_id, record_seq)
        if event_id != expected_event_id:
            raise EventValidationError(
                f"{location}: event_id {event_id!r} does not match "
                f"{expected_event_id!r}"
            )
        result.update(
            {
                "receive_time_utc_ns": _parse_int(
                    row, "receive_time_utc_ns", location, minimum=0
                ),
                "recorder_run_id": run_id,
                "record_seq": record_seq,
                "file_part": file_part,
                "event_id": event_id,
            }
        )
    return result


def _required_text(
    row: Mapping[str, str | None], column: str, location: str
) -> str:
    value = row.get(column)
    text = "" if value is None else str(value).strip()
    if not text:
        raise EventValidationError(f"{location}: {column} cannot be empty")
    return text


def _parse_int(
    row: Mapping[str, str | None],
    column: str,
    location: str,
    *,
    minimum: int,
) -> int:
    text = _required_text(row, column, location)
    try:
        value = int(text)
    except ValueError as exc:
        raise EventValidationError(
            f"{location}: {column} must be an integer; found {text!r}"
        ) from exc
    if value < minimum:
        raise EventValidationError(
            f"{location}: {column} must be >= {minimum}; found {value}"
        )
    return value


def _parse_float(
    row: Mapping[str, str | None],
    column: str,
    location: str,
    *,
    minimum: float | None = None,
    minimum_exclusive: float | None = None,
) -> float:
    text = _required_text(row, column, location)
    try:
        value = float(text)
    except ValueError as exc:
        raise EventValidationError(
            f"{location}: {column} must be numeric; found {text!r}"
        ) from exc
    if not math.isfinite(value):
        raise EventValidationError(f"{location}: {column} must be finite")
    if minimum is not None and value < minimum:
        raise EventValidationError(
            f"{location}: {column} must be >= {minimum}; found {value}"
        )
    if minimum_exclusive is not None and value <= minimum_exclusive:
        raise EventValidationError(
            f"{location}: {column} must be > {minimum_exclusive}; found {value}"
        )
    return value


def _location(path: Path, physical_row: int) -> str:
    return f"{path}:{physical_row}"


__all__ = [
    "DuplicateEventIdError",
    "EventReplayError",
    "EventSchemaError",
    "EventSequenceError",
    "EventValidationError",
    "NormalizedNinjaTraderEvent",
    "iter_ninjatrader_event_exports",
    "load_ninjatrader_event_exports",
    "make_event_id",
    "ninjatrader_events_to_tbbo_proxy",
]
