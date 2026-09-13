"""Strategy-specific exit rules without market-data or execution side effects."""
from __future__ import annotations

from collections.abc import Collection, Mapping
import math
from typing import Any


SHAOFU_MIN_HOLD_TRADING_DAYS = 3
SHAOFU_SOFT_EXIT_CONFIRMATIONS = 2
SOFT_EXIT_CONFIRMATIONS = 2
SOFT_EXIT_REDUCE_RATIO = 0.50
SOFT_EXIT_SCORE_VETO_THRESHOLD = 4
NIUONE_MAX_HOLD_CALENDAR_DAYS = 25
NIUONE_LEADER_LOSS_CONFIRMATIONS = 2
NIUONE_MAINLINE_WEAK_CONFIRMATIONS = 2
NIUONE_CLIMAX_RUNNER_ENABLED = True
NIUONE_CLIMAX_RUNNER_LEADER_LOSS_CONFIRMATIONS = 3
NIUONE_CLIMAX_RUNNER_TRAILING_ATR = 3.0
# Frozen on disjoint development windows on 2026-08-01.  NiuOne realizes a
# smaller first leg once the initial structural risk has been earned, then
# protects the runner at cost while the mainline remains healthy.
NIUONE_PARTIAL_TAKE_PROFIT_R = 1.0
NIUONE_PARTIAL_TAKE_PROFIT_RATIO = 0.45
# Reversal probes entered while the broad tape is advancing, repairing, or
# defensive use earlier protection than rotation-regime probes. Realize a
# slightly larger first leg sooner, while leaving mature NiuOne paths and
# rotation-regime reversals on the common 1R/45% policy.
NIUONE_REVERSAL_EARLY_PROTECTION_REGIMES = frozenset({
    "offensive",
    "recovery",
    "defensive",
})
NIUONE_REVERSAL_EARLY_PARTIAL_TAKE_PROFIT_R = 0.75
NIUONE_REVERSAL_EARLY_PARTIAL_TAKE_PROFIT_RATIO = 0.50


def niuone_climax_runner_active(
    *,
    enabled: bool,
    climax_partial_done: bool,
    partial_tp_done: bool,
    stock_strong: bool,
    theme_score: float,
    theme_state: str,
) -> bool:
    """Keep only a de-risked winner's remainder on a healthy mainline."""
    return bool(
        enabled
        and climax_partial_done
        and partial_tp_done
        and stock_strong
        and float(theme_score) >= 55.0
        and str(theme_state or "")
        in {"candidate", "emerging", "mainline", "diverging"}
    )
NIUONE_BREAK_EVEN_AFTER_PARTIAL = True
NIUONE_LIFECYCLE_CLIMAX_PARTIAL_RATIO = 1.0 / 3.0
NIUONE_LIFECYCLE_CLIMAX_MIN_PNL_PCT = 0.0
NIUONE_INTRADAY_PROFIT_TARGET = True
NIUONE_REVERSAL_MAINLINE_WEAK_CONFIRMATIONS = 1


def _sell_signal(reason: str, signal: str, sell_ratio: float = 1.0) -> dict[str, Any]:
    return {"reason": reason, "signal": signal, "sell_ratio": sell_ratio}


def niuone_stop_levels(position: Mapping[str, Any], *, cost: float, break_even: bool) -> dict[str, Any]:
    """Separate immutable structure from cost protection without inventing history.

    entry_stop_price remains the legacy effective-stop projection. Old positions
    already overwritten with niu_breakeven have unknown original structure unless
    a separately sourced shaofu stop survives.
    """
    def positive(value: Any) -> float:
        try:
            number = float(value or 0)
        except (ValueError, TypeError):
            return 0.0
        return number if math.isfinite(number) and number > 0 else 0.0

    source = str(position.get("entry_stop_source") or "")
    shaofu_source = str(position.get("shaofu_stop_source") or "")
    legacy = 0.0 if shaofu_source == "fallback_pct" else positive(
        position.get("shaofu_stop_price") or position.get("entry_stop_price")
    )
    original = positive(position.get("original_structural_stop_price"))
    if not original:
        if positive(position.get("shaofu_stop_price")) and shaofu_source not in {"fallback_pct", "niu_breakeven"}:
            original = positive(position.get("shaofu_stop_price"))
        elif source not in {"niu_breakeven", "fallback_pct"} and shaofu_source != "fallback_pct":
            original = positive(position.get("entry_stop_price"))
    protection = positive(cost) if break_even and position.get("partial_tp_done") else 0.0
    # An already applied legacy cost stop remains protected if a setting changes.
    if source == "niu_breakeven":
        protection = max(protection, positive(position.get("entry_stop_price")))
    return {
        "original_structural_stop_price": original or None,
        "cost_protection_stop_price": protection,
        "effective_stop_price": max(legacy, original, protection),
    }


def niuone_hard_exit_evidence(
    *,
    strategy_id: str,
    current_price: float,
    structural_stop: float,
    market_hard_stop: bool,
    theme_score: float,
    theme_state: str,
    cost_protection_stop: float = 0.0,
    original_structural_stop: float | None = None,
) -> dict[str, Any]:
    """Verify hard risk conditions from observations, never model prose."""
    price = float(current_price)
    stop = float(structural_stop)
    score = float(theme_score)
    price = price if math.isfinite(price) and price > 0 else 0.0
    stop = stop if math.isfinite(stop) and stop > 0 else 0.0
    score = score if math.isfinite(score) else 100.0
    signal, reason = "", ""
    stop_kind = ""
    if price > 0 and stop > 0 and price < stop:
        signal = "niu_structure_stop"
        if original_structural_stop and price < original_structural_stop:
            stop_kind = "structure"
        elif cost_protection_stop > 0 and price < cost_protection_stop:
            stop_kind = "cost_protection"
        else:
            stop_kind = "structure"
        label = "成本保护线" if stop_kind == "cost_protection" else "结构止损线"
        reason = f"现价跌破牛牛{label} (现价{price:.2f} < 止损{stop:.2f})"
    elif market_hard_stop and (score < 55 or theme_state in {"fading", "inactive"}):
        signal = "niu_market_hard_stop"
        reason = f"市场硬停止且主线转弱 (分数{score:.1f}，状态{theme_state or '-'})"
    elif theme_state == "inactive":
        signal = "niu_reversal_theme_failed" if strategy_id == "niu_reversal_probe" else "niu_mainline_faded"
        reason = f"主线失活 (分数{score:.1f})"
    return {
        "schema_version": 1,
        "confirmed": bool(signal),
        "signal": signal,
        "reason": reason,
        "current_price": price,
        "structural_stop": stop,
        "stop_kind": stop_kind,
        "original_structural_stop": original_structural_stop,
        "cost_protection_stop": cost_protection_stop,
        "market_hard_stop": bool(market_hard_stop),
        "theme_score": score,
        "theme_state": theme_state,
    }


def resolve_niuone_partial_take_profit(
    *,
    strategy_id: str,
    entry_market_regime: str,
    default_r: float = NIUONE_PARTIAL_TAKE_PROFIT_R,
    default_ratio: float = NIUONE_PARTIAL_TAKE_PROFIT_RATIO,
    reversal_early_regimes: Collection[str] = NIUONE_REVERSAL_EARLY_PROTECTION_REGIMES,
    reversal_early_r: float = NIUONE_REVERSAL_EARLY_PARTIAL_TAKE_PROFIT_R,
    reversal_early_ratio: float = NIUONE_REVERSAL_EARLY_PARTIAL_TAKE_PROFIT_RATIO,
) -> tuple[float, float]:
    """Resolve one NiuOne first-profit policy from immutable entry context."""
    normalized_regimes = {
        str(value or "").strip().lower() for value in reversal_early_regimes
    }
    if (
        str(strategy_id or "").strip() == "niu_reversal_probe"
        and str(entry_market_regime or "").strip().lower() in normalized_regimes
    ):
        return float(reversal_early_r), float(reversal_early_ratio)
    return float(default_r), float(default_ratio)


def evaluate_shaofu_soft_exit(
    *,
    hold_trading_days: int,
    soft_exit_allowed: bool,
    confirmation_key: str,
    previous_key: str,
    previous_count: int,
    sector_flow_direction: str,
    volume_price_signal: str,
    already_reduced: bool,
    min_hold_trading_days: int = SHAOFU_MIN_HOLD_TRADING_DAYS,
    confirmations_required: int = SHAOFU_SOFT_EXIT_CONFIRMATIONS,
) -> dict[str, Any]:
    """Arbitrate one non-structural Shaofu exit without mutating position state.

    Strong industry inflow or a constructive price/volume relationship vetoes a
    soft exit.  Industry outflow plus bearish projected volume counts as two
    independent confirmations, but can only release half of the position; the
    remaining runner still waits for a structural hard exit.
    """
    if already_reduced:
        return {"status": "runner_hold", "allow_reduce": False, "count": 0, "required": confirmations_required}
    if hold_trading_days < min_hold_trading_days:
        return {"status": "min_hold", "allow_reduce": False, "count": 0, "required": confirmations_required}
    if not soft_exit_allowed:
        return {"status": "morning_hold", "allow_reduce": False, "count": 0, "required": confirmations_required}

    direction = str(sector_flow_direction or "neutral")
    volume_signal = str(volume_price_signal or "neutral")
    if direction == "inflow" or volume_signal == "supportive":
        return {"status": "context_hold", "allow_reduce": False, "count": 0, "required": confirmations_required}

    required = max(1, int(confirmations_required))
    if direction == "outflow" and volume_signal == "bearish":
        required = 1
    count = max(0, int(previous_count))
    if confirmation_key and confirmation_key != previous_key:
        count += 1
    if count < required:
        return {"status": "pending", "allow_reduce": False, "count": count, "required": required}
    return {"status": "confirmed", "allow_reduce": True, "count": count, "required": required}


def arbitrate_staged_soft_exit(
    *,
    signal_family: str,
    session_key: str,
    previous_family: str,
    previous_session: str,
    previous_count: int,
    already_reduced: bool,
    sell_score: float | None,
    evidence_count: int = 1,
    confirmations_required: int = SOFT_EXIT_CONFIRMATIONS,
    reduce_ratio: float = SOFT_EXIT_REDUCE_RATIO,
) -> dict[str, Any]:
    """Resolve a non-structural exit without allowing a one-shot full sale.

    A distinct trading session is one confirmation.  The first actionable
    confirmation releases risk while preserving a runner; a full exit is only
    available after the runner has already been reduced and the same soft
    family persists on another session.  A strong 4-5 sell-fly score vetoes
    the first session, but does not erase the evidence or block later risk
    reduction.  Structural and market hard stops never call this arbiter.
    """
    family = str(signal_family or "soft_exit").strip() or "soft_exit"
    session = str(session_key or "").strip()
    prior_family = str(previous_family or "").strip()
    prior_session = str(previous_session or "").strip()
    count = max(0, int(previous_count or 0)) if family == prior_family else 0
    if session and (family != prior_family or session != prior_session):
        count += 1
    count = max(count, max(1, int(evidence_count or 1)))

    required = max(2, int(confirmations_required or 2))
    score = float(sell_score) if isinstance(sell_score, (int, float)) else None
    if score is not None and score >= SOFT_EXIT_SCORE_VETO_THRESHOLD and count < required:
        return {
            "status": "score_veto",
            "count": count,
            "required": required,
            "sell_ratio": 0.0,
            "signal_family": family,
            "session_key": session,
        }
    if not already_reduced:
        return {
            "status": "reduce",
            "count": count,
            "required": required,
            "sell_ratio": max(0.0, min(0.75, float(reduce_ratio))),
            "signal_family": family,
            "session_key": session,
        }
    if count >= required:
        return {
            "status": "exit",
            "count": count,
            "required": required,
            "sell_ratio": 1.0,
            "signal_family": family,
            "session_key": session,
        }
    return {
        "status": "runner_hold",
        "count": count,
        "required": required,
        "sell_ratio": 0.0,
        "signal_family": family,
        "session_key": session,
    }


def evaluate_strategy_time_exit(
    *,
    entry_strategy: str,
    hold_days: int,
    max_pnl_pct: float,
    pnl_pct: float,
    time_exit_allowed: bool,
    b3_exit_allowed: bool,
    b3_exit_hhmm: str,
    time_exit_hhmm: str,
    no_progress_hold_days: int,
    no_progress_max_pnl_pct: float,
    strategy_confirmation_met: bool = False,
    strategy_variant: str = "",
) -> dict[str, Any] | None:
    """Evaluate strategy-specific time-boxed exits."""
    if b3_exit_allowed and entry_strategy == "b3_accelerate" and hold_days >= 1 and max_pnl_pct < 1.0 and pnl_pct <= 0:
        return _sell_signal(
            f"B3次日不涨离场 ({hold_days}d {b3_exit_hhmm}开盘检查，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
            "b3_next_day_no_progress",
        )
    if time_exit_allowed:
        if entry_strategy == "tide_leader" and hold_days >= 5 and max_pnl_pct < 3.0:
            return _sell_signal(
                f"主线领航5日未创新高 ({hold_days}d，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "tide_leader_no_progress",
            )
        if entry_strategy == "tide_rotation" and hold_days >= 3 and max_pnl_pct < 2.0:
            return _sell_signal(
                f"轮动初升3日未延续 ({hold_days}d，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "tide_rotation_no_follow_through",
            )
        if entry_strategy == "tide_recovery" and hold_days >= 2 and max_pnl_pct < 1.5 and pnl_pct <= 0.5:
            return _sell_signal(
                f"冰点修复T+2未确认 ({hold_days}d，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "tide_recovery_unconfirmed",
            )
        if entry_strategy == "niu_leader" and hold_days >= 5 and max_pnl_pct < 3.0:
            return _sell_signal(
                f"牛牛领涨5日未创新高 ({hold_days}d，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "niu_leader_no_progress",
            )
        if entry_strategy == "niu_pullback" and hold_days >= 3 and max_pnl_pct < 2.0:
            return _sell_signal(
                f"牛牛转强3日未恢复强势 ({hold_days}d，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "niu_pullback_no_follow_through",
            )
        if (
            entry_strategy == "niu_reversal_probe"
            and strategy_variant == "daily_v"
            and hold_days >= 3
            and max_pnl_pct < 2.0
        ):
            return _sell_signal(
                f"牛牛试仓3日未延续 ({hold_days}d，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "niu_reversal_no_progress",
            )
        if (
            entry_strategy == "niu_reversal_probe"
            and strategy_variant != "daily_v"
            and hold_days >= 2
            and not strategy_confirmation_met
        ):
            return _sell_signal(
                f"牛牛试仓T+2仍未升级 ({hold_days}d，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "niu_reversal_not_upgraded",
            )
        if entry_strategy == "niu_emerging" and hold_days >= 2 and max_pnl_pct < 1.5 and pnl_pct <= 0.5:
            return _sell_signal(
                f"牛牛启动T+2未升级为确认主线 ({hold_days}d，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "niu_emerging_unconfirmed",
            )
        if entry_strategy == "b2_confirm" and hold_days >= 2 and max_pnl_pct < 2.0 and pnl_pct <= 0.5:
            return _sell_signal(
                f"B2确认未延续离场 ({hold_days}d {time_exit_hhmm}尾盘检查，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "b2_no_follow_through",
            )
        if entry_strategy == "super_b1" and hold_days >= no_progress_hold_days and max_pnl_pct < no_progress_max_pnl_pct:
            return _sell_signal(
                f"超级B1只赌一次未兑现离场 ({hold_days}d {time_exit_hhmm}尾盘检查，最高盈利{max_pnl_pct:.1f}%，现盈亏{pnl_pct:.1f}%)",
                "super_b1_no_progress",
            )
    return None
