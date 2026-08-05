from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import live_execution_unlocked
from .decision_journal import HashChainedJsonlJournal
from .execution.evidence import JOURNAL_SCHEMA_VERSION, TOP_OF_BOOK_EVIDENCE
from .session import NEW_YORK, trading_session_date


def _parse_timestamp(value: object) -> datetime:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC").to_pydatetime()


def _rth_session(value: object) -> str | None:
    timestamp = _parse_timestamp(value)
    local = timestamp.astimezone(NEW_YORK)
    minute = local.hour * 60 + local.minute
    if local.weekday() >= 5 or minute < 570 or minute >= 960:
        return None
    return str(trading_session_date(timestamp))


def _valid_top_of_book(bid: object, ask: object, event_id: object) -> bool:
    try:
        bid_value = float(bid)
        ask_value = float(ask)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(bid_value)
        and math.isfinite(ask_value)
        and ask_value >= bid_value > 0.0
        and bool(str(event_id or ""))
    )


def _load_journal(path: Path) -> tuple[list[dict[str, object]], int, bool]:
    records: list[dict[str, object]] = []
    malformed = 0
    hash_chain_valid = True
    try:
        HashChainedJsonlJournal(path)
    except ValueError:
        hash_chain_valid = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if (
            not isinstance(value, dict)
            or int(value.get("schema_version", -1)) != JOURNAL_SCHEMA_VERSION
        ):
            malformed += 1
            continue
        records.append(value)
    return records, malformed, hash_chain_valid


def _bootstrap_daily_totals(
    values: np.ndarray,
    simulations: int,
    block_sessions: int,
    seed: int = 731,
) -> np.ndarray:
    if len(values) == 0:
        return np.empty(0, dtype=np.float64)
    block = max(1, min(int(block_sessions), len(values)))
    blocks_needed = math.ceil(len(values) / block)
    rng = np.random.default_rng(seed)
    totals = np.empty(simulations, dtype=np.float64)
    offsets = np.arange(block, dtype=np.int64)
    batch_size = 2000
    for start in range(0, simulations, batch_size):
        count = min(batch_size, simulations - start)
        origins = rng.integers(0, len(values), size=(count, blocks_needed))
        indices = (origins[:, :, None] + offsets) % len(values)
        sampled = values[indices.reshape(count, -1)[:, : len(values)]]
        totals[start : start + count] = sampled.sum(axis=1)
    return totals


def _maximum_drawdown(values: np.ndarray) -> float:
    curve = np.r_[0.0, np.cumsum(values, dtype=np.float64)]
    return float((np.maximum.accumulate(curve) - curve).max(initial=0.0))


def run_sim101_audit(
    journal_path: Path,
    output_dir: Path,
    *,
    point_value: float = 2.0,
    round_trip_fee: float = 1.90,
    minimum_sessions: int = 40,
    minimum_trades: int = 100,
    simulations: int = 20_000,
    block_sessions: int = 5,
) -> Path:
    if live_execution_unlocked():
        raise RuntimeError("Sim101 audit refuses to run while live execution is unlocked")
    if minimum_sessions < 20:
        raise ValueError("minimum_sessions cannot be lower than 20")
    if minimum_trades < 100:
        raise ValueError("minimum_trades cannot be lower than 100")
    records, malformed, hash_chain_valid = _load_journal(journal_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    runs = [record for record in records if record.get("record_type") == "RUN"]
    markets = [record for record in records if record.get("record_type") == "MARKET"]
    prediction_records = [
        record for record in records if record.get("record_type") == "PREDICTION"
    ]
    next_quote_records = [
        record for record in records if record.get("record_type") == "NEXT_QUOTE"
    ]
    intent_records = [record for record in records if record.get("record_type") == "INTENT"]
    event_records = [
        record
        for record in records
        if record.get("record_type") == "EVENT"
        and record.get("event_source") == "bridge"
        and int(dict(record.get("event", {})).get("sequence", 0)) > 0
    ]

    fingerprints = {
        str(record.get("candidate_fingerprint", "")) for record in runs
        if record.get("candidate_fingerprint")
    }
    frozen_statuses = {str(record.get("candidate_status", "")) for record in runs}
    training_cutoffs = [
        _parse_timestamp(record["training_frame_end"])
        for record in runs
        if record.get("training_frame_end")
    ]
    market_timestamps = [
        _parse_timestamp(dict(record.get("market", {})).get("timestamp"))
        for record in markets
        if dict(record.get("market", {})).get("timestamp")
    ]
    sessions = sorted(
        {
            session
            for record in markets
            if (session := _rth_session(dict(record.get("market", {})).get("timestamp")))
            is not None
        }
    )

    decisions: dict[str, dict[str, object]] = {}
    duplicate_decisions = 0
    malformed_decisions = 0
    top_of_book_decisions = 0
    prediction_market_keys: set[tuple[str, str]] = set()
    required_observation_fields = {
        "timestamp", "candle_open", "open", "high", "low", "close", "volume"
    }
    required_prediction_fields = {
        "timestamp", "predicted_move_ticks", "threshold_ticks", "side",
        "eligible", "decision",
    }
    for record in prediction_records:
        decision_id = str(record.get("decision_id", ""))
        if not decision_id:
            malformed_decisions += 1
            continue
        if decision_id in decisions:
            duplicate_decisions += 1
            continue
        prediction = dict(record.get("prediction", {}))
        observation = dict(record.get("signal_observation", {}))
        features = dict(record.get("features", {}))
        timestamp_value = prediction.get("timestamp")
        if (
            not timestamp_value
            or not features
            or not required_observation_fields.issubset(observation)
            or not required_prediction_fields.issubset(prediction)
        ):
            malformed_decisions += 1
            continue
        try:
            finite_features = all(
                math.isfinite(float(value)) for value in features.values()
            )
        except (TypeError, ValueError):
            finite_features = False
        if not finite_features:
            malformed_decisions += 1
            continue
        timestamp = _parse_timestamp(timestamp_value)
        if str(record.get("session_date", "")) != str(trading_session_date(timestamp)):
            malformed_decisions += 1
            continue
        quote_valid = (
            record.get("evidence_tier") == TOP_OF_BOOK_EVIDENCE
            and _valid_top_of_book(
                record.get("signal_bid"),
                record.get("signal_ask"),
                record.get("intended_next_quote_event_id"),
            )
        )
        if quote_valid:
            top_of_book_decisions += 1
        decisions[decision_id] = record
        candle_open = str(observation.get("candle_open", ""))
        if candle_open:
            prediction_market_keys.add((str(record.get("run_id", "")), candle_open))

    next_quotes: dict[str, dict[str, object]] = {}
    duplicate_next_quotes = 0
    malformed_next_quotes = 0
    for record in next_quote_records:
        decision_id = str(record.get("decision_id", ""))
        decision = decisions.get(decision_id)
        if decision is None:
            malformed_next_quotes += 1
            continue
        if decision_id in next_quotes:
            duplicate_next_quotes += 1
            continue
        expected_event_id = str(decision.get("intended_next_quote_event_id", ""))
        event_id = str(record.get("event_id", ""))
        try:
            quote_timestamp = _parse_timestamp(record.get("timestamp"))
            signal_timestamp = _parse_timestamp(
                dict(decision.get("prediction", {})).get("timestamp")
            )
        except (TypeError, ValueError):
            malformed_next_quotes += 1
            continue
        valid = (
            decision.get("evidence_tier") == TOP_OF_BOOK_EVIDENCE
            and event_id == expected_event_id
            and quote_timestamp > signal_timestamp
            and _valid_top_of_book(record.get("bid"), record.get("ask"), event_id)
            and str(record.get("session_date", ""))
            == str(trading_session_date(quote_timestamp))
        )
        if not valid:
            malformed_next_quotes += 1
            continue
        next_quotes[decision_id] = record

    rth_market_keys = {
        (
            str(record.get("run_id", "")),
            str(dict(record.get("market", {})).get("candle_open", "")),
        )
        for record in markets
        if _rth_session(dict(record.get("market", {})).get("timestamp")) is not None
    }
    missing_market_decisions = len(rth_market_keys.difference(prediction_market_keys))

    intents: dict[str, dict[str, object]] = {}
    conflicting_intents = 0
    duplicate_intents = 0
    unlinked_intents = 0
    decision_order_violations = 0
    next_quote_order_violations = 0
    intents_by_decision: defaultdict[str, int] = defaultdict(int)
    intent_decision_ids: dict[str, str] = {}
    for record in intent_records:
        intent = dict(record.get("intent", {}))
        client_id = str(intent.get("client_order_id", ""))
        if not client_id:
            conflicting_intents += 1
            continue
        if client_id in intents:
            duplicate_intents += 1
            if intents[client_id] != intent:
                conflicting_intents += 1
            continue
        decision_id = str(record.get("decision_id", ""))
        decision_record = decisions.get(decision_id)
        if decision_record is None:
            unlinked_intents += 1
        else:
            intents_by_decision[decision_id] += 1
            if int(decision_record.get("journal_sequence", 0)) >= int(
                record.get("journal_sequence", 0)
            ):
                decision_order_violations += 1
            next_quote_record = next_quotes.get(decision_id)
            if next_quote_record is not None and int(
                next_quote_record.get("journal_sequence", 0)
            ) >= int(record.get("journal_sequence", 0)):
                next_quote_order_violations += 1
        intent_decision_ids[client_id] = decision_id
        intents[client_id] = intent
    repeated_intent_decisions = sum(
        count - 1 for count in intents_by_decision.values() if count > 1
    )

    sequence_violations = 0
    sequence_gaps = 0
    sequences_by_run: dict[str, list[int]] = defaultdict(list)
    for record in event_records:
        sequences_by_run[str(record.get("run_id", ""))].append(
            int(dict(record["event"])["sequence"])
        )
    for values in sequences_by_run.values():
        for previous, current in zip(values, values[1:]):
            if current <= previous:
                sequence_violations += 1
            elif current > previous + 1:
                sequence_gaps += current - previous - 1

    unique_events: list[dict[str, object]] = []
    seen_events: set[str] = set()
    duplicate_events = 0
    for record in event_records:
        event = dict(record["event"])
        signature = json.dumps(event, sort_keys=True, separators=(",", ":"))
        if signature in seen_events:
            duplicate_events += 1
            continue
        seen_events.add(signature)
        unique_events.append(event)

    events_by_client: dict[str, list[dict[str, object]]] = defaultdict(list)
    orphan_events = 0
    for event in unique_events:
        client_id = str(event.get("client_order_id", ""))
        if client_id not in intents:
            orphan_events += 1
            continue
        events_by_client[client_id].append(event)

    lifecycle_errors = 0
    rejected_intents = 0
    open_filled_intents = 0
    pnl_mismatches = 0
    fills_before_next_quote = 0
    trades: list[dict[str, object]] = []
    for client_id, intent in intents.items():
        events = sorted(
            events_by_client.get(client_id, []),
            key=lambda event: (_parse_timestamp(event["timestamp"]), int(event["sequence"])),
        )
        fills = [event for event in events if str(event.get("event_type")) == "FILLED"]
        closes = [event for event in events if str(event.get("event_type")) == "CLOSED"]
        rejections = [
            event for event in events if str(event.get("event_type")) == "REJECTED"
        ]
        decision_id = intent_decision_ids.get(client_id, "")
        next_quote = next_quotes.get(decision_id)
        if next_quote is not None:
            quote_time = _parse_timestamp(next_quote.get("timestamp"))
            fills_before_next_quote += sum(
                _parse_timestamp(event.get("timestamp")) < quote_time for event in fills
            )
        if rejections and not fills:
            rejected_intents += 1
            continue
        filled_qty = sum(int(event.get("quantity", 0)) for event in fills)
        closed_qty = sum(int(event.get("quantity", 0)) for event in closes)
        expected_qty = int(intent.get("quantity", 0))
        intent_side = str(intent.get("side", ""))
        if any(str(event.get("side", "")) != intent_side for event in fills + closes):
            lifecycle_errors += 1
        if filled_qty > expected_qty or closed_qty > filled_qty or (closes and not fills):
            lifecycle_errors += 1
        if filled_qty == 0 or closed_qty < filled_qty:
            if filled_qty > closed_qty:
                open_filled_intents += 1
            continue
        entry_notional = sum(
            float(event.get("price", 0.0)) * int(event.get("quantity", 0))
            for event in fills
        )
        entry_price = entry_notional / filled_qty
        sign = 1.0 if intent_side == "BUY" else -1.0
        reconstructed_gross = sum(
            (float(event.get("price", 0.0)) - entry_price)
            * sign
            * int(event.get("quantity", 0))
            * point_value
            for event in closes
        )
        bridge_gross = sum(float(event.get("realized_pnl", 0.0)) for event in closes)
        if abs(reconstructed_gross - bridge_gross) > 0.011:
            pnl_mismatches += 1
        commission = round_trip_fee * closed_qty
        net = bridge_gross - commission
        exit_time = max(_parse_timestamp(event["timestamp"]) for event in closes)
        trades.append(
            {
                "client_order_id": client_id,
                "session": str(trading_session_date(exit_time)),
                "side": intent_side,
                "quantity": closed_qty,
                "entry_time_utc": min(_parse_timestamp(event["timestamp"]) for event in fills).isoformat(),
                "exit_time_utc": exit_time.isoformat(),
                "entry_price": entry_price,
                "exit_price": sum(
                    float(event.get("price", 0.0)) * int(event.get("quantity", 0))
                    for event in closes
                ) / closed_qty,
                "bridge_gross_usd": bridge_gross,
                "modeled_commission_usd": commission,
                "net_usd": net,
            }
        )

    trade_frame = pd.DataFrame(trades)
    if trade_frame.empty:
        trade_frame = pd.DataFrame(
            columns=[
                "client_order_id", "session", "side", "quantity", "entry_time_utc",
                "exit_time_utc", "entry_price", "exit_price", "bridge_gross_usd",
                "modeled_commission_usd", "net_usd",
            ]
        )
    trade_frame.to_csv(output_dir / "reconciled_trades.csv", index=False)

    daily = pd.DataFrame({"session": sessions})
    if not trade_frame.empty:
        grouped = trade_frame.groupby("session", sort=True).agg(
            trades=("client_order_id", "count"),
            contracts=("quantity", "sum"),
            gross_usd=("bridge_gross_usd", "sum"),
            commission_usd=("modeled_commission_usd", "sum"),
            net_usd=("net_usd", "sum"),
        )
        daily = daily.join(grouped, on="session")
    for column in ("trades", "contracts", "gross_usd", "commission_usd", "net_usd"):
        if column not in daily:
            daily[column] = 0
        daily[column] = daily[column].fillna(0)
    daily[["trades", "contracts"]] = daily[["trades", "contracts"]].astype(int)
    daily.to_csv(output_dir / "daily_sim101.csv", index=False)

    pnl = trade_frame["net_usd"].to_numpy(dtype=np.float64)
    daily_pnl = daily["net_usd"].to_numpy(dtype=np.float64)
    gross_wins = float(pnl[pnl > 0.0].sum()) if len(pnl) else 0.0
    gross_losses = float(-pnl[pnl < 0.0].sum()) if len(pnl) else 0.0
    profit_factor = gross_wins / gross_losses if gross_losses > 0.0 else (
        float("inf") if gross_wins > 0.0 else 0.0
    )
    total_net = float(pnl.sum()) if len(pnl) else 0.0
    top_five = float(np.sort(pnl)[-5:].sum()) if len(pnl) else 0.0
    concentration = top_five / total_net if total_net > 0.0 else 1.0
    bootstrap = _bootstrap_daily_totals(
        daily_pnl, simulations=simulations, block_sessions=block_sessions
    )
    bootstrap_p05 = float(np.quantile(bootstrap, 0.05)) if len(bootstrap) else float("-inf")
    positive_session_share = float(np.mean(daily_pnl > 0.0)) if len(daily_pnl) else 0.0
    post_freeze = bool(
        training_cutoffs
        and market_timestamps
        and min(market_timestamps) > max(training_cutoffs)
    )

    checks = {
        "simulation_only_runs": bool(runs)
        and all(
            record.get("execution_environment") == "SIM101"
            and record.get("live_execution_unlocked") is False
            for record in runs
        ),
        "single_frozen_candidate": len(fingerprints) == 1
        and frozen_statuses == {"FROZEN_RESEARCH_ONLY"},
        "post_freeze_market_evidence": post_freeze,
        "complete_decision_journal": bool(decisions)
        and duplicate_decisions == 0
        and malformed_decisions == 0
        and missing_market_decisions == 0
        and unlinked_intents == 0
        and decision_order_violations == 0
        and next_quote_order_violations == 0
        and repeated_intent_decisions == 0,
        "top_of_book_decision_evidence": bool(decisions)
        and top_of_book_decisions == len(decisions)
        and len(next_quotes) == len(decisions)
        and duplicate_next_quotes == 0
        and malformed_next_quotes == 0
        and fills_before_next_quote == 0,
        "journal_integrity": hash_chain_valid
        and malformed == 0
        and conflicting_intents == 0
        and sequence_violations == 0
        and sequence_gaps == 0,
        "complete_lifecycle_reconciliation": orphan_events == 0
        and lifecycle_errors == 0
        and open_filled_intents == 0
        and pnl_mismatches == 0,
        "minimum_sessions": len(sessions) >= minimum_sessions,
        "minimum_closed_trades": len(trade_frame) >= minimum_trades,
        "positive_after_cost_net": total_net > 0.0,
        "profit_factor": profit_factor >= 1.20,
        "positive_session_share": positive_session_share >= 0.55,
        "bootstrap_lower_tail": bootstrap_p05 > 0.0,
        "profit_concentration": concentration <= 0.50,
    }
    passed = all(checks.values())
    mean_daily = float(daily_pnl.mean()) if len(daily_pnl) else 0.0
    metrics = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "journal": str(journal_path.resolve()),
        "candidate_fingerprints": sorted(fingerprints),
        "run_count": len(runs),
        "sessions": len(sessions),
        "closed_trades": len(trade_frame),
        "intents": len(intents),
        "decisions": len(decisions),
        "duplicate_decisions": duplicate_decisions,
        "malformed_decisions": malformed_decisions,
        "top_of_book_decisions": top_of_book_decisions,
        "next_quote_records": len(next_quotes),
        "duplicate_next_quotes": duplicate_next_quotes,
        "malformed_next_quotes": malformed_next_quotes,
        "missing_market_decisions": missing_market_decisions,
        "unlinked_intents": unlinked_intents,
        "decision_order_violations": decision_order_violations,
        "next_quote_order_violations": next_quote_order_violations,
        "repeated_intent_decisions": repeated_intent_decisions,
        "fills_before_next_quote": fills_before_next_quote,
        "rejected_intents": rejected_intents,
        "open_filled_intents": open_filled_intents,
        "duplicate_intents": duplicate_intents,
        "conflicting_intents": conflicting_intents,
        "orphan_events": orphan_events,
        "duplicate_bridge_events": duplicate_events,
        "sequence_violations": sequence_violations,
        "sequence_gaps": sequence_gaps,
        "malformed_records": malformed,
        "journal_hash_chain_valid": hash_chain_valid,
        "pnl_mismatches": pnl_mismatches,
        "net_usd": total_net,
        "mean_daily_usd": mean_daily,
        "median_daily_usd": float(np.median(daily_pnl)) if len(daily_pnl) else 0.0,
        "positive_session_share": positive_session_share,
        "profit_factor": None if not np.isfinite(profit_factor) else profit_factor,
        "maximum_drawdown_usd": _maximum_drawdown(pnl),
        "bootstrap_total_p05_usd": bootstrap_p05,
        "top_five_trade_profit_share": concentration,
        "checks": checks,
        "passes_sim101_gate": passed,
        "live_execution_unlocked": False,
        "live_execution_authorized": False,
    }
    (output_dir / "sim101_audit.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )

    lines = [
        "# Sim101 Forward-Evidence Audit",
        "",
        "## Verdict",
        "",
        (
            "The fixed candidate passed the internal Sim101 evidence gate. This does not authorize live trading."
            if passed
            else "The fixed candidate has not passed the Sim101 evidence gate. It remains research-only."
        ),
        "",
        "## Evidence",
        "",
        f"- Untouched RTH sessions including no-trade days: `{len(sessions)}` / required `{minimum_sessions}`",
        f"- Reconciled closed intents: `{len(trade_frame)}` / required `{minimum_trades}`",
        f"- After-fee net: `${total_net:.2f}`",
        f"- Mean / median per observed session: `${metrics['mean_daily_usd']:.2f}` / `${metrics['median_daily_usd']:.2f}`",
        f"- Profit factor: `{'infinite' if not np.isfinite(profit_factor) else f'{profit_factor:.4f}'}`",
        f"- Positive session share: `{positive_session_share:.2%}`",
        f"- Session-block bootstrap P05 total: `${bootstrap_p05:.2f}`",
        f"- Five-best-trade profit share: `{concentration:.2%}`",
        f"- Maximum closed-trade drawdown: `${metrics['maximum_drawdown_usd']:.2f}`",
        "",
        "## Gates",
        "",
    ]
    lines.extend(
        f"- `{name}`: `{'PASS' if value else 'FAIL'}`" for name, value in checks.items()
    )
    lines.extend(
        [
            "",
            "## Accounting",
            "",
            "Gross P&L is reconstructed from NinjaTrader execution fills and checked against the bridge value. Configured round-trip fees are then subtracted. This is Sim101 fill evidence, not a live-account statement and not proof that real fills will match.",
            "",
            "Live execution remains locked regardless of this report's verdict.",
        ]
    )
    report = output_dir / "SIM101_AUDIT.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


__all__ = ["run_sim101_audit"]
