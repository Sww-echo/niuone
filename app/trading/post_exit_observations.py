"""Durable, forward-only evaluation of simulated SELL outcomes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

try:
    from app.market_data.tencent_kline_cache import load_kline_series_map
    from app.storage.practice_db import (
        query_active_exit_feedback_policy,
        query_exit_feedback_reentry_observations,
        query_exit_feedback_tuning_observations,
        query_latest_exit_feedback_evaluation,
        query_post_exit_observation_summary,
        query_post_exit_reentry_audits,
        query_post_exit_sell_trades,
        record_exit_feedback_evaluation,
        record_exit_feedback_policy,
        upsert_post_exit_observations,
        upsert_post_exit_reentry_observations,
    )
    from app.strategies.exit_feedback import (
        EXIT_FEEDBACK_ALGORITHM_VERSION,
        EXIT_FEEDBACK_DEFAULT_COOLDOWN_SAMPLES,
        EXIT_FEEDBACK_DEFAULT_MIN_MONTHS,
        EXIT_FEEDBACK_DEFAULT_MIN_SAMPLES,
        EXIT_FEEDBACK_DEFAULT_PARAMETERS,
        normalize_exit_feedback_parameters,
        propose_exit_feedback_policy,
    )
except ImportError:  # pragma: no cover - legacy top-level import path
    from market_data.tencent_kline_cache import load_kline_series_map
    from storage.practice_db import (
        query_active_exit_feedback_policy,
        query_exit_feedback_reentry_observations,
        query_exit_feedback_tuning_observations,
        query_latest_exit_feedback_evaluation,
        query_post_exit_observation_summary,
        query_post_exit_reentry_audits,
        query_post_exit_sell_trades,
        record_exit_feedback_evaluation,
        record_exit_feedback_policy,
        upsert_post_exit_observations,
        upsert_post_exit_reentry_observations,
    )
    from strategies.exit_feedback import (
        EXIT_FEEDBACK_ALGORITHM_VERSION,
        EXIT_FEEDBACK_DEFAULT_COOLDOWN_SAMPLES,
        EXIT_FEEDBACK_DEFAULT_MIN_MONTHS,
        EXIT_FEEDBACK_DEFAULT_MIN_SAMPLES,
        EXIT_FEEDBACK_DEFAULT_PARAMETERS,
        normalize_exit_feedback_parameters,
        propose_exit_feedback_policy,
    )


POST_EXIT_HORIZONS = (1, 3, 5, 10)
POST_EXIT_BENCHMARK_SYMBOL = "sh000001"
SELL_FLY_MIN_MFE_PCT = 5.0
SELL_FLY_MIN_CLOSE_RETURN_PCT = 2.0
AVOIDED_LOSS_MAE_PCT = -5.0
REPLACEMENT_REGRET_MIN_PCT = 3.0


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _symbol(code: Any) -> str:
    normalized = "".join(character for character in str(code or "") if character.isdigit())[-6:]
    if len(normalized) != 6:
        return ""
    return ("sh" if normalized.startswith(("6", "9")) else "sz") + normalized


def _trade_key(trade: dict[str, Any]) -> str:
    identity = {
        key: trade.get(key, "")
        for key in ("time", "action", "code", "shares", "price", "reason")
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ordered_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [row for row in rows if str(row.get("date") or "")],
        key=lambda row: str(row.get("date") or ""),
    )


def _window(
    rows: list[dict[str, Any]],
    sell_date: str,
    horizon: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    ordered = _ordered_rows(rows)
    for index, row in enumerate(ordered):
        if str(row.get("date") or "")[:10] == sell_date:
            return row, ordered[index + 1:index + 1 + horizon]
    return None, []


def _returns(
    baseline: dict[str, Any] | None,
    future: list[dict[str, Any]],
    *,
    execution_price: float | None = None,
) -> tuple[float | None, float | None, float | None]:
    base_close = _number((baseline or {}).get("close"), 0.0)
    if base_close <= 0 or not future:
        return None, None, None
    candidate_price = _number(execution_price, 0.0)
    basis = (
        candidate_price
        if candidate_price > 0 and 0.8 <= candidate_price / base_close <= 1.2
        else base_close
    )
    close_return = (_number(future[-1].get("close")) / basis - 1.0) * 100.0
    highs = [_number(row.get("high") or row.get("close")) for row in future]
    lows = [_number(row.get("low") or row.get("close")) for row in future]
    highs = [value for value in highs if value > 0]
    lows = [value for value in lows if value > 0]
    mfe = (max(highs) / basis - 1.0) * 100.0 if highs else None
    mae = (min(lows) / basis - 1.0) * 100.0 if lows else None
    return close_return, mfe, mae


def _price_basis(
    baseline: dict[str, Any] | None,
    execution_price: Any,
) -> str:
    base_close = _number((baseline or {}).get("close"), 0.0)
    price = _number(execution_price, 0.0)
    if base_close > 0 and price > 0 and 0.8 <= price / base_close <= 1.2:
        return "actual_execution"
    return "daily_close_fallback"


def build_post_exit_observations(
    trades: list[dict[str, Any]],
    series_map: dict[str, list[dict[str, Any]]],
    *,
    updated_at: str,
    benchmark_symbol: str = POST_EXIT_BENCHMARK_SYMBOL,
) -> list[dict[str, Any]]:
    """Build idempotent horizon rows from immutable trades and cached bars."""
    observations: list[dict[str, Any]] = []
    benchmark_rows = series_map.get(benchmark_symbol) or []
    for trade in trades:
        if str(trade.get("action") or "").upper() != "SELL":
            continue
        code = "".join(character for character in str(trade.get("code") or "") if character.isdigit())[-6:]
        sell_time = str(trade.get("time") or "")
        sell_date = sell_time[:10]
        symbol = _symbol(code)
        if not symbol or len(sell_date) != 10:
            continue
        rows = series_map.get(symbol) or []
        replacement_code = "".join(
            character
            for character in str(trade.get("replacement_target_code") or "")
            if character.isdigit()
        )[-6:]
        replacement_rows = series_map.get(_symbol(replacement_code)) or []
        entry_context = trade.get("niuone_entry_context")
        entry_atr = _number(
            trade.get("entry_atr20")
            or ((entry_context or {}).get("entry_atr20") if isinstance(entry_context, dict) else 0),
            0.0,
        )
        key = _trade_key(trade)
        sell_price = _number(trade.get("price"), 0.0)
        shares = int(_number(trade.get("shares"), 0.0))
        replacement_executed = bool(
            replacement_code
            and _number(trade.get("replacement_execution_price"), 0.0) > 0
            and int(_number(trade.get("replacement_execution_shares"), 0.0)) > 0
        )
        replacement_buy_price = _number(
            trade.get("replacement_execution_price"),
            0.0,
        )
        for horizon in POST_EXIT_HORIZONS:
            baseline, future = _window(rows, sell_date, horizon)
            close_return, mfe, mae = _returns(
                baseline,
                future,
                execution_price=sell_price,
            )
            benchmark_base, benchmark_future = _window(
                benchmark_rows,
                sell_date,
                horizon,
            )
            benchmark_return, _, _ = _returns(benchmark_base, benchmark_future)
            if len(benchmark_future) < horizon:
                benchmark_return = None
            replacement_base, replacement_future = _window(
                replacement_rows,
                sell_date,
                horizon,
            )
            replacement_counterfactual_return, _, _ = _returns(
                replacement_base,
                replacement_future,
            )
            replacement_return, _, _ = _returns(
                replacement_base,
                replacement_future,
                execution_price=(
                    replacement_buy_price
                    if replacement_executed
                    else None
                ),
            )
            if len(replacement_future) < horizon:
                replacement_counterfactual_return = None
                replacement_return = None
            if replacement_executed and replacement_return is not None:
                replacement_amount = replacement_buy_price * int(
                    _number(trade.get("replacement_execution_shares"), 0.0)
                )
                if replacement_amount > 0:
                    replacement_return -= (
                        _number(trade.get("replacement_execution_fee"), 0.0)
                        / replacement_amount
                        * 100.0
                    )
            elif not replacement_executed:
                replacement_return = None
            completed = len(future) >= horizon and baseline is not None
            basis_status = _price_basis(baseline, sell_price)
            base_close = (
                sell_price
                if basis_status == "actual_execution"
                else _number((baseline or {}).get("close"), 0.0)
            )
            atr_threshold_pct = (
                max(SELL_FLY_MIN_MFE_PCT, entry_atr / base_close * 100.0)
                if entry_atr > 0 and base_close > 0
                else SELL_FLY_MIN_MFE_PCT
            )
            sell_fly: int | None = None
            avoided_loss: int | None = None
            replacement_regret: int | None = None
            if horizon == 5 and completed and mfe is not None and close_return is not None:
                sell_fly = int(
                    mfe >= atr_threshold_pct
                    and close_return >= SELL_FLY_MIN_CLOSE_RETURN_PCT
                )
                avoided_loss = int(mae is not None and mae <= AVOIDED_LOSS_MAE_PCT)
                if replacement_executed and replacement_return is not None:
                    replacement_regret = int(
                        close_return - replacement_return
                        >= REPLACEMENT_REGRET_MIN_PCT
                    )
            quality_status = (
                "complete"
                if completed
                else "missing_sell_date_bar"
                if baseline is None
                else "awaiting_future_bars"
            )
            observations.append({
                "trade_key": key,
                "horizon": horizon,
                "sell_time": sell_time,
                "code": code,
                "sell_price": sell_price,
                "sell_notional": round(max(0.0, sell_price * shares), 2),
                "price_basis": basis_status,
                "shares": shares,
                "full_exit": int(bool(
                    trade.get("position_fully_closed")
                    or (
                        trade.get("position_after_qty") is not None
                        and int(_number(trade.get("position_after_qty"), 0.0)) <= 0
                    )
                )),
                "exit_rule": str(trade.get("exit_rule") or ""),
                "exit_signal": str(trade.get("exit_signal") or ""),
                "buy_strategy": str(trade.get("buy_strategy") or trade.get("entry_strategy") or ""),
                "replacement_target_code": replacement_code,
                "sessions_observed": len(future),
                "observation_date": str(future[-1].get("date") or "") if future else "",
                "close_return_pct": round(close_return, 4) if close_return is not None else None,
                "mfe_pct": round(mfe, 4) if mfe is not None else None,
                "mae_pct": round(mae, 4) if mae is not None else None,
                "benchmark_return_pct": round(benchmark_return, 4) if benchmark_return is not None else None,
                "excess_return_pct": (
                    round(close_return - benchmark_return, 4)
                    if close_return is not None and benchmark_return is not None
                    else None
                ),
                "replacement_return_pct": (
                    round(replacement_return, 4)
                    if replacement_return is not None
                    else None
                ),
                "replacement_counterfactual_return_pct": (
                    round(replacement_counterfactual_return, 4)
                    if replacement_counterfactual_return is not None
                    else None
                ),
                "replacement_regret_pct": (
                    round(close_return - replacement_return, 4)
                    if close_return is not None and replacement_return is not None
                    else None
                ),
                "replacement_regret": replacement_regret,
                "replacement_executed": int(replacement_executed),
                "replacement_buy_time": str(
                    trade.get("replacement_execution_time") or ""
                ),
                "replacement_buy_price": (
                    round(replacement_buy_price, 4)
                    if replacement_executed
                    else None
                ),
                "replacement_buy_shares": int(
                    _number(trade.get("replacement_execution_shares"), 0.0)
                ),
                "replacement_buy_fee": round(
                    _number(trade.get("replacement_execution_fee"), 0.0),
                    4,
                ),
                "feedback_policy_version": int(
                    _number(trade.get("exit_feedback_policy_version"), 0)
                ),
                "sell_fly_threshold_pct": round(atr_threshold_pct, 4),
                "sell_fly": sell_fly,
                "avoided_loss": avoided_loss,
                "completed": int(completed),
                "quality_status": quality_status,
                "updated_at": updated_at,
            })
    return observations


def build_post_exit_reentry_observations(
    audits: list[dict[str, Any]],
    series_map: dict[str, list[dict[str, Any]]],
    *,
    updated_at: str,
) -> list[dict[str, Any]]:
    """Build five-session outcomes for direct allowed and blocked re-entry audits."""
    observations: list[dict[str, Any]] = []
    for audit in audits:
        code = "".join(
            character for character in str(audit.get("code") or "")
            if character.isdigit()
        )[-6:]
        observed_at = str(audit.get("observed_at") or "")
        observed_date = observed_at[:10]
        candidate_price = _number(audit.get("candidate_price"), 0.0)
        if not code or len(observed_date) != 10 or candidate_price <= 0:
            continue
        baseline, future = _window(
            series_map.get(_symbol(code)) or [],
            observed_date,
            5,
        )
        future_return, _, _ = _returns(
            baseline,
            future,
            execution_price=candidate_price,
        )
        completed = baseline is not None and len(future) >= 5
        if not completed:
            future_return = None
        quality_status = (
            "complete"
            if completed
            else "missing_observation_date_bar"
            if baseline is None
            else "awaiting_future_bars"
        )
        observations.append({
            **audit,
            "price_basis": _price_basis(baseline, candidate_price),
            "sessions_observed": len(future),
            "observation_date": str(future[-1].get("date") or "") if future else "",
            "future_return_pct": (
                round(future_return, 4)
                if future_return is not None
                else None
            ),
            "completed": int(completed),
            "quality_status": quality_status,
            "updated_at": updated_at,
        })
    return observations


def refresh_exit_feedback_policy(
    *,
    now: datetime,
    enabled: bool,
    min_samples: int = EXIT_FEEDBACK_DEFAULT_MIN_SAMPLES,
    min_months: int = EXIT_FEEDBACK_DEFAULT_MIN_MONTHS,
    cooldown_samples: int = EXIT_FEEDBACK_DEFAULT_COOLDOWN_SAMPLES,
) -> dict[str, Any]:
    """Evaluate and, when eligible, atomically activate one bounded version."""
    current = query_active_exit_feedback_policy()
    if not enabled:
        return {
            "enabled": False,
            "status": "disabled",
            "algorithm_version": EXIT_FEEDBACK_ALGORITHM_VERSION,
            "version": int((current or {}).get("version") or 0),
            "parameters": dict(EXIT_FEEDBACK_DEFAULT_PARAMETERS),
            "reason": "自动调参未启用，交易链路继续使用兼容默认值",
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    rows = query_exit_feedback_tuning_observations()
    reentry_rows = query_exit_feedback_reentry_observations()
    latest_evaluation = query_latest_exit_feedback_evaluation()
    proposal = propose_exit_feedback_policy(
        rows,
        current,
        reentry_rows=reentry_rows,
        last_evaluation_count=(
            int((latest_evaluation or {}).get("observation_count") or 0)
            if str((latest_evaluation or {}).get("algorithm_version") or "")
            == EXIT_FEEDBACK_ALGORITHM_VERSION
            and str((latest_evaluation or {}).get("status") or "") == "hold"
            else 0
        ),
        min_samples=min_samples,
        min_months=min_months,
        cooldown_samples=cooldown_samples,
    )
    if bool(proposal.get("persist")):
        activated = record_exit_feedback_policy({
            **proposal,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "effective_date": now.strftime("%Y-%m-%d"),
            "previous_version": int((current or {}).get("version") or 0) or None,
        })
        result = {
            **activated,
            "enabled": True,
            "parameters": normalize_exit_feedback_parameters(
                activated.get("parameters")
            ),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    else:
        parameters = normalize_exit_feedback_parameters(
            (current or {}).get("parameters")
        )
        result = {
            **proposal,
            "enabled": True,
            "version": int((current or {}).get("version") or 0),
            "algorithm_version": str(
                (current or {}).get("algorithm_version")
                or proposal.get("algorithm_version")
                or EXIT_FEEDBACK_ALGORITHM_VERSION
            ),
            "evaluation_algorithm_version": str(
                proposal.get("algorithm_version")
                or EXIT_FEEDBACK_ALGORITHM_VERSION
            ),
            "parameters": parameters,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    if bool(proposal.get("record_evaluation")):
        record_exit_feedback_evaluation({
            **proposal,
            "evaluated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "policy_version": int(result.get("version") or 0),
        })
    return result


def refresh_post_exit_observations(
    *,
    now: datetime | None = None,
    trade_limit: int = 2000,
    auto_tune_enabled: bool = True,
    auto_tune_min_samples: int = EXIT_FEEDBACK_DEFAULT_MIN_SAMPLES,
    auto_tune_min_months: int = EXIT_FEEDBACK_DEFAULT_MIN_MONTHS,
    auto_tune_cooldown_samples: int = EXIT_FEEDBACK_DEFAULT_COOLDOWN_SAMPLES,
) -> dict[str, Any]:
    """Refresh all maturing horizons from the local daily-bar cache."""
    now = now or datetime.now()
    trades = query_post_exit_sell_trades(limit=trade_limit)
    reentry_audits = query_post_exit_reentry_audits(limit=max(5000, trade_limit))
    symbols = {
        _symbol(trade.get("code"))
        for trade in trades
        if _symbol(trade.get("code"))
    }
    symbols.update(
        _symbol(trade.get("replacement_target_code"))
        for trade in trades
        if _symbol(trade.get("replacement_target_code"))
    )
    symbols.update(
        _symbol(audit.get("code"))
        for audit in reentry_audits
        if _symbol(audit.get("code"))
    )
    symbols.add(POST_EXIT_BENCHMARK_SYMBOL)
    series_map = load_kline_series_map(symbols, min_rows=1, count=180)
    rows = build_post_exit_observations(
        trades,
        series_map,
        updated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
    )
    upsert_post_exit_observations(rows)
    reentry_rows = build_post_exit_reentry_observations(
        reentry_audits,
        series_map,
        updated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
    )
    upsert_post_exit_reentry_observations(reentry_rows)
    feedback_policy = refresh_exit_feedback_policy(
        now=now,
        enabled=auto_tune_enabled,
        min_samples=auto_tune_min_samples,
        min_months=auto_tune_min_months,
        cooldown_samples=auto_tune_cooldown_samples,
    )
    return {
        **query_post_exit_observation_summary(),
        "tracked_sell_count": len(trades),
        "observation_row_count": len(rows),
        "reentry_observation_row_count": len(reentry_rows),
        "feedback_policy": feedback_policy,
    }


__all__ = [
    "POST_EXIT_HORIZONS",
    "build_post_exit_observations",
    "build_post_exit_reentry_observations",
    "refresh_exit_feedback_policy",
    "refresh_post_exit_observations",
]
