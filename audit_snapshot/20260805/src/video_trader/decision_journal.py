from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd


class HashChainedJsonlJournal:
    """Append-only JSONL journal that verifies its complete prior hash chain."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        self.previous_hash = "0" * 64
        if self.path.exists():
            self._verify_existing()

    def _verify_existing(self) -> None:
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"journal line {line_number} is not valid JSON"
                    ) from exc
                expected_sequence = self.sequence + 1
                if record.get("journal_sequence") != expected_sequence:
                    raise ValueError(f"journal sequence gap at line {line_number}")
                if record.get("previous_record_hash") != self.previous_hash:
                    raise ValueError(f"journal hash-chain break at line {line_number}")
                actual = record.get("record_hash")
                payload = dict(record)
                payload.pop("record_hash", None)
                expected = _record_hash(payload)
                if actual != expected:
                    raise ValueError(f"journal record hash mismatch at line {line_number}")
                self.sequence = expected_sequence
                self.previous_hash = str(actual)

    def append(self, payload: Mapping[str, object]) -> dict[str, object]:
        record = {
            "journal_sequence": self.sequence + 1,
            "previous_record_hash": self.previous_hash,
            **dict(payload),
        }
        record_hash = _record_hash(record)
        record["record_hash"] = record_hash
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical_json(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.sequence += 1
        self.previous_hash = record_hash
        return record


def iter_independent_decisions(
    seconds: pd.DataFrame,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    predictions: Mapping[str, np.ndarray],
    thresholds: Mapping[str, float],
    tick_value: float,
) -> Iterable[dict[str, object]]:
    """Yield every signal and no-trade decision without suppressing overlaps."""

    if tick_value <= 0.0:
        raise ValueError("tick_value must be positive")
    if not seconds.index.equals(features.index) or not seconds.index.equals(labels.index):
        raise ValueError("seconds, features and labels must share an identical index")
    bid = seconds["first_bid"].to_numpy(dtype=np.float64)
    ask = seconds["first_ask"].to_numpy(dtype=np.float64)
    entry_rows = labels["entry_row"].to_numpy(dtype=np.int64)

    for candidate in sorted(predictions):
        if candidate not in thresholds:
            raise ValueError(f"missing threshold for candidate {candidate}")
        score = np.asarray(predictions[candidate], dtype=np.float64)
        if score.shape != (len(seconds),):
            raise ValueError(f"candidate {candidate} has the wrong prediction length")
        threshold = float(thresholds[candidate])
        if threshold < 0.0 or not np.isfinite(threshold):
            raise ValueError(f"candidate {candidate} has an invalid threshold")
        for row, value in enumerate(score):
            side = 1 if value >= threshold and value > 0.0 else -1 if value <= -threshold else 0
            prefix = "long" if side > 0 else "short"
            valid = bool(labels.iloc[row][f"{prefix}_valid"]) if side else False
            if side == 0:
                decision = "NO_TRADE_BAND"
            elif not valid:
                decision = "INVALID_FORWARD_PATH"
            else:
                decision = "INDEPENDENT_SIGNAL"
            entry = int(entry_rows[row]) if valid else -1
            exit_row = int(labels.iloc[row][f"{prefix}_exit_row"]) if valid else -1
            gross = float(labels.iloc[row][f"{prefix}_gross_ticks"]) if valid else 0.0
            net = float(labels.iloc[row][f"{prefix}_net_ticks"]) if valid else 0.0
            yield {
                "record_type": "DECISION",
                "decision_id": f"{candidate}:{row:012d}",
                "candidate": candidate,
                "signal_row": row,
                "signal_time": seconds.index[row].isoformat(),
                "score_ticks": float(value),
                "threshold_ticks": threshold,
                "side": "LONG" if side > 0 else "SHORT" if side < 0 else "FLAT",
                "decision": decision,
                "independent_path": True,
                "entry_row": entry if valid else None,
                "entry_time": seconds.index[entry].isoformat() if valid else None,
                "intended_entry_price": (
                    float(ask[entry]) if side > 0 else float(bid[entry])
                ) if valid else None,
                "exit_row": exit_row if valid else None,
                "exit_time": seconds.index[exit_row].isoformat() if valid else None,
                "exit_reason": str(labels.iloc[row][f"{prefix}_outcome"]) if valid else None,
                "gross_ticks": gross,
                "net_ticks": net,
                "net_usd": net * tick_value,
                "features": {
                    name: float(value)
                    for name, value in features.iloc[row].items()
                },
            }


def _record_hash(record: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _canonical_json(record: Mapping[str, object]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)


__all__ = ["HashChainedJsonlJournal", "iter_independent_decisions"]
