"""Preregistered, read-only shadow comparison of the first-Probe price gate."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .fees import (
    A_SHARE_COMMISSION_RATE, A_SHARE_MINIMUM_COMMISSION,
    A_SHARE_SELL_STAMP_DUTY_RATE, A_SHARE_TRANSFER_FEE_RATE,
    calculate_a_share_trade_fees,
)

PROBE_CHASE_VERSION = "probe-chase-forward-v1"
PROBE_CHASE_COHORT_START = "2026-09-08"
PROBE_CHASE_MAX_CHANGE_PCT = 3.0
PROBE_CHASE_HORIZON = 5
PROBE_CHASE_SLIPPAGE_BPS = 5.0
PROBE_CHASE_MIN_GROUP_SAMPLES = 30
PROBE_CHASE_MIN_MONTHS = 3
PROBE_CHASE_BENCHMARK = "sh000001"


def probe_chase_protocol() -> dict[str, Any]:
    return {
        "version": PROBE_CHASE_VERSION, "cohort_start": PROBE_CHASE_COHORT_START,
        "threshold_pct_exclusive": PROBE_CHASE_MAX_CHANGE_PCT,
        "unit": "first_otherwise_executable_nonreplacement_probe_intent_per_stock_per_day",
        "arms": {"baseline": "shadow_buy_all", "filtered": "shadow_buy_only_change_lt_3_otherwise_cash"},
        "eligibility": "first_entry_only; actual quote; all other live activity/mechanics/risk/cash gates pass",
        "horizon_sessions": PROBE_CHASE_HORIZON, "shadow_shares": 100,
        "slippage_bps_each_side": PROBE_CHASE_SLIPPAGE_BPS,
        "fees": {"commission": A_SHARE_COMMISSION_RATE, "minimum": A_SHARE_MINIMUM_COMMISSION,
                 "stamp_duty_sell": A_SHARE_SELL_STAMP_DUTY_RATE, "transfer": A_SHARE_TRANSFER_FEE_RATE},
        "price_basis": "cached_adjusted_prices_normalized_to_frozen_quote_previous_close",
        "exit": "entire_shadow_position_at_fifth_following_market_session_close",
        "missing_data": "pending_or_unverifiable; never_shift_exit_to_next_available_stock_bar",
        "deduplication": "earliest_observation_per_protocol_stock_day; retries_do_not_reassign_group",
        "primary_metric": "paired_mean_net_return_per_observed_opportunity; filtered_skips_return_zero",
        "secondary_metric": "completed_shadow_trade_win_rate; breakeven_in_denominator; pending_excluded",
        "minimum_mature_samples_each_price_group": PROBE_CHASE_MIN_GROUP_SAMPLES,
        "minimum_calendar_months": PROBE_CHASE_MIN_MONTHS,
        "interpretation": "nonrandomized_shadow_event_comparison; not_live_PnL_or_full_strategy_backtest; manual_review_only",
        "limitations": "conditional_on_live_model_intents_and_portfolio; no_independent_portfolio_or_closing_order_book_simulation",
        "change_policy": "no_threshold_horizon_or_outcome_selection_after_start; new_version_requires_new_cohort",
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def make_probe_chase_observation(
    code: str, quote: Mapping[str, Any], candidate: Mapping[str, Any], *, observed_at: str,
) -> dict[str, Any] | None:
    """Called only after every non-price gate passes, before mutating the account."""
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError:
        return None
    if observed.date().isoformat() < PROBE_CHASE_COHORT_START:
        return None
    price, previous = _number(quote.get("price")), _number(quote.get("prev_close"))
    if price is None or previous is None or price <= 0 or previous <= 0:
        return None
    change = (price / previous - 1) * 100
    key = hashlib.sha256(f"{PROBE_CHASE_VERSION}|{code}|{observed.date()}".encode()).hexdigest()
    return {
        "observation_key": key, "protocol_version": PROBE_CHASE_VERSION,
        "code": code, "observed_at": observed_at,
        "price": price, "previous_close": previous, "change_pct": change,
        "filtered_accepts": change < PROBE_CHASE_MAX_CHANGE_PCT - 1e-9,
        "turnover_pct": quote.get("turnover"),
        "theme": str(candidate.get("signal_theme") or candidate.get("industry") or ""),
        "signal_generated_at": str(candidate.get("generated_at") or ""),
    }


def collect_probe_chase_observations(
    decisions: Iterable[Mapping[str, Any]], *, as_of: str,
) -> list[dict[str, Any]]:
    rows = []
    for row in decisions:
        if row.get("_forward_payload_available") is not True:
            continue
        decision = row.get("decision")
        if not isinstance(decision, Mapping) or decision.get("error"):
            continue
        for observation in decision.get("probe_chase_observations") or []:
            if not isinstance(observation, Mapping):
                continue
            stamp = str(observation.get("observed_at") or "")
            if observation.get("protocol_version") != PROBE_CHASE_VERSION:
                continue
            if not PROBE_CHASE_COHORT_START <= stamp[:10] <= as_of[:10]:
                continue
            rebuilt = make_probe_chase_observation(
                str(observation.get("code") or ""),
                {"price": observation.get("price"), "prev_close": observation.get("previous_close"),
                 "turnover": observation.get("turnover_pct")}, {}, observed_at=stamp,
            )
            if rebuilt is None or any(observation.get(key) != rebuilt[key] for key in (
                "observation_key", "filtered_accepts", "change_pct",
            )):
                continue
            rows.append(dict(observation))
    rows.sort(key=lambda row: (row["observed_at"], row["observation_key"]))
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        selected.setdefault(row["observation_key"], row)
    return list(selected.values())


def _symbol(code: str) -> str:
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def build_probe_chase_outcomes(
    observations: Iterable[Mapping[str, Any]], series: Mapping[str, list[dict[str, Any]]], *, as_of: str,
    expected_sessions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Mature at a fixed market-session horizon; no future or missing-day shortcuts."""
    market_dates = {str(row.get("date") or "")[:10] for row in series.get(PROBE_CHASE_BENCHMARK, [])
                    if str(row.get("date") or "")[:10] <= as_of[:10]}
    if expected_sessions is None:
        sessions = []
        if market_dates:
            current = date.fromisoformat(min(market_dates))
            while current.isoformat() <= as_of[:10]:
                if current.weekday() < 5:
                    sessions.append(current.isoformat())
                current += timedelta(days=1)
    else:
        sessions = sorted({day for day in expected_sessions if day <= as_of[:10]})
    outcomes = []
    for observation in observations:
        entry_day = str(observation["observed_at"])[:10]
        result = {**observation, "completed": False, "status": "pending", "as_of": as_of[:10]}
        if entry_day not in sessions or sessions.index(entry_day) == 0:
            result["status"] = "entry_price_basis_unavailable"
            outcomes.append(result)
            continue
        index = sessions.index(entry_day)
        future = sessions[index+1:index+1+PROBE_CHASE_HORIZON]
        if len(future) < PROBE_CHASE_HORIZON:
            outcomes.append(result)
            continue
        bars = {str(row.get("date") or "")[:10]: row for row in series.get(_symbol(str(observation["code"])), [])}
        needed = [sessions[index-1], entry_day, *future]
        if any(day not in market_dates for day in needed):
            result["status"] = "market_calendar_or_bar_gap"
            outcomes.append(result)
            continue
        if any(day not in bars or (_number(bars[day].get("close")) or 0) <= 0 for day in needed):
            result["status"] = "stock_bar_gap"
            outcomes.append(result)
            continue
        # A suspension/zero-volume terminal day cannot supply an executable exit.
        if (_number(bars[future[-1]].get("volume")) or 0) <= 0:
            result["status"] = "terminal_not_tradable"
            outcomes.append(result)
            continue
        factor = float(bars[sessions[index-1]]["close"]) / float(observation["previous_close"])
        terminal = float(bars[future[-1]]["close"]) / factor
        entry = float(observation["price"]) * (1 + PROBE_CHASE_SLIPPAGE_BPS / 10000)
        exit_price = terminal * (1 - PROBE_CHASE_SLIPPAGE_BPS / 10000)
        cost = entry * 100 + calculate_a_share_trade_fees(entry * 100, "BUY")["total_fee"]
        proceeds = exit_price * 100 - calculate_a_share_trade_fees(exit_price * 100, "SELL")["total_fee"]
        net_return = (proceeds / cost - 1) * 100
        result.update({"completed": True, "status": "completed", "exit_date": future[-1],
                       "net_return_pct": net_return, "buy_cost": cost, "sell_proceeds": proceeds,
                       "filtered_opportunity_return_pct": net_return if observation["filtered_accepts"] else 0.0})
        outcomes.append(result)
    return outcomes


def summarize_probe_chase(
    observations: list[dict[str, Any]], outcomes: list[dict[str, Any]], *, as_of: str,
) -> dict[str, Any]:
    valid_keys = {row["observation_key"] for row in observations}
    mature = [row for row in outcomes if row.get("completed") is True and row.get("observation_key") in valid_keys
              and str(row.get("exit_date") or "") <= as_of[:10]]
    def summary(rows):
        values = [float(row["net_return_pct"]) for row in rows]
        return {"completed_trade_count": len(values), "win_count": sum(value > 0 for value in values),
                "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 4) if values else None,
                "mean_net_return_pct": round(sum(values) / len(values), 4) if values else None}
    passed = [row for row in mature if row["filtered_accepts"]]
    chased = [row for row in mature if not row["filtered_accepts"]]
    start, cutoff = date.fromisoformat(PROBE_CHASE_COHORT_START), date.fromisoformat(as_of[:10])
    months = (cutoff.year-start.year)*12 + cutoff.month-start.month - (cutoff.day < start.day)
    counts_ready = min(len(passed), len(chased)) >= PROBE_CHASE_MIN_GROUP_SAMPLES
    return {
        "protocol": probe_chase_protocol(), "observed_opportunity_count": len(observations),
        "completed_opportunity_count": len(mature), "pending_or_unverifiable_count": len(observations)-len(mature),
        "baseline": summary(mature), "filtered": summary(passed),
        "price_groups": {"lt_3_pct": summary(passed), "ge_3_pct": summary(chased)},
        "filtered_skipped_opportunity_count": len(chased),
        "paired_mean_return_delta_pct": round(sum(-float(row["net_return_pct"]) for row in chased)/len(mature),4) if mature else None,
        "unique_stock_count": len({row["code"] for row in mature}),
        "unique_entry_day_theme_count": len({(row["observed_at"][:10],row.get("theme")) for row in mature}),
        "status": "pre_start" if cutoff < start else "manual_review_ready" if counts_ready and months >= PROBE_CHASE_MIN_MONTHS else "collecting",
        "causal_or_live_performance_claim_supported": False,
    }


def load_probe_chase_outcomes(path: str | Path) -> list[dict[str, Any]]:
    """Read the matured shadow ledger without creating or migrating a database."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='probe_chase_outcomes'").fetchone():
            return []
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT payload_json FROM probe_chase_outcomes WHERE protocol_version=? ORDER BY observation_key", (PROBE_CHASE_VERSION,)
        )]


def refresh_probe_chase_outcomes(*, now: datetime) -> dict[str, Any]:
    """The existing closing-snapshot job matures observations from local bars."""
    from app.market_data.tencent_kline_cache import load_kline_series_map
    from app.storage.practice_db import DB_PATH, record_probe_chase_outcomes
    from app.trading.niuone_forward import load_niuone_forward_decisions_from_db

    as_of = now.strftime("%Y-%m-%d")
    if now.strftime("%H:%M") < "15:00":
        return {"status": "awaiting_market_close"}
    decisions, _ = load_niuone_forward_decisions_from_db(DB_PATH)
    observations = collect_probe_chase_observations(decisions, as_of=as_of)
    frozen = load_probe_chase_outcomes(DB_PATH)
    completed_keys = {row["observation_key"] for row in frozen}
    pending = [row for row in observations if row["observation_key"] not in completed_keys]
    symbols = {_symbol(row["code"]) for row in pending} | {PROBE_CHASE_BENCHMARK}
    series = load_kline_series_map(symbols, min_rows=1, count=180) if pending else {}
    from app.reports.a_share.calendar import trading_day_status

    expected_sessions = []
    market_dates = [str(row.get("date") or "")[:10] for row in series.get(PROBE_CHASE_BENCHMARK, [])]
    if market_dates:
        current = date.fromisoformat(min(market_dates))
        while current.isoformat() <= as_of:
            status = trading_day_status(current, allow_refresh=False)
            if current.weekday() < 5 and (status.get("calendar_cached") is not True or status.get("is_trading_day") is True):
                expected_sessions.append(current.isoformat())
            current += timedelta(days=1)
    outcomes = build_probe_chase_outcomes(pending, series, as_of=as_of, expected_sessions=expected_sessions)
    record_probe_chase_outcomes([row for row in outcomes if row["completed"]])
    result = summarize_probe_chase(observations, frozen+[row for row in outcomes if row["completed"]], as_of=as_of)
    result["pending_status_counts"] = dict(Counter(row["status"] for row in outcomes if not row["completed"]))
    return result
