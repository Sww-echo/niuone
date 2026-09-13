"""Research-only entry and cost-line comparisons; never imported by execution.

Thresholds are preregistered diagnostic hypotheses, not production eligibility.
Daily bars can test entry timing, but cannot validate minute cost confirmation.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping, Sequence


def entry_risk_groups(*, stage: str, strength: float | None, change_pct: float | None,
                      stop_distance_pct: float | None) -> list[str]:
    groups = []
    def finite(value: float | None) -> bool:
        return value is not None and math.isfinite(value)
    if stage == "divergence" and finite(strength) and finite(change_pct) and strength < 55 and change_pct >= 5:
        groups.append("diverging_weak_chase")
    if stage == "climax" and finite(stop_distance_pct) and stop_distance_pct >= 8:
        groups.append("climax_wide_stop")
    return groups


def compare_entry_variants(*, entry_price: float, structural_stop: float,
                           bars: Sequence[Mapping[str, Any]], capital: float = 10000) -> dict[str, Any]:
    """Paired five-session stress test with one frozen structural exit.

    baseline/half enter at the observed original fill. Waiting requires one of
    the first two later daily closes above both original fill and that day's
    open, with no structural breach; it buys only at the following open. All
    variants respect T+1, lots and fees. No entry-day stop is synthesized.
    This price confirmation does not establish that the theme has recovered.
    """
    if len(bars) != 5 or not 0 < structural_stop < entry_price or capital <= 0:
        raise ValueError("five valid future sessions and a genuine original stop are required")
    dates = [str(bar["date"]) for bar in bars]
    if dates != sorted(set(dates)):
        raise ValueError("sessions must be unique and strictly ordered")
    for bar in bars:
        values = [float(bar[k]) for k in ("open", "high", "low", "close")]
        if not all(math.isfinite(v) and v > 0 for v in values) or not values[2] <= min(values[0], values[3]) <= max(values[0], values[3]) <= values[1]:
            raise ValueError("invalid OHLC")

    def fee(gross: float, *, sell: bool) -> float:
        # Fixed research assumptions: 3bp commission (minimum CNY5),
        # 0.1bp transfer fee both ways, 5bp stamp duty on sales.
        return max(5.0, gross * 0.0003) + gross * (0.00051 if sell else 0.00001)

    def replay(price: float, size: float, entry_index: int) -> dict[str, Any]:
        qty = int(capital * size / price / 100) * 100
        while qty > 0 and qty * price + fee(qty * price, sell=False) > capital * size:
            qty -= 100
        if qty == 0:
            return {"entered": False, "net_pnl": 0.0, "return_on_budget_pct": 0.0, "reason": "lot_or_budget"}
        exit_price, exit_index, reason = float(bars[-1]["close"]), 4, "five_session_close"
        for index in range(max(0, entry_index + 1), 5):
            bar = bars[index]
            if float(bar["low"]) < structural_stop:
                exit_price, exit_index = min(float(bar["open"]), structural_stop), index
                reason = "original_structure_stop"
                break
        gross_in, gross_out = qty * price, qty * exit_price
        pnl = gross_out - gross_in - fee(gross_in, sell=False) - fee(gross_out, sell=True)
        return {"entered": True, "shares": qty, "entry_index": entry_index, "exit_index": exit_index,
                "net_pnl": round(pnl, 2), "return_on_budget_pct": round(pnl / capital * 100, 4), "reason": reason}

    waiting = {"entered": False, "net_pnl": 0.0, "return_on_budget_pct": 0.0, "reason": "no_confirmation"}
    for index, bar in enumerate(bars[:2]):
        if float(bar["low"]) < structural_stop:
            break  # A broken original thesis cannot be revived by a later bounce.
        if float(bar["close"]) > max(entry_price, float(bar["open"])):
            next_open = float(bars[index + 1]["open"])
            if structural_stop < next_open <= entry_price:
                # Cheaper fills after confirmation are allowed, with the same stop.
                waiting = replay(next_open, 1.0, index + 1)
            elif next_open > entry_price:
                # Do not increase the original cash-at-risk after a gap up.
                size = min(1.0, (1 - structural_stop / entry_price) / (1 - structural_stop / next_open))
                waiting = replay(next_open, size, index + 1)
            break
    return {"baseline": replay(entry_price, 1.0, -1), "half_initial": replay(entry_price, 0.5, -1), "wait_confirmation": waiting}


def bounded_cost_confirmation(*, price: float, cost_line: float, structural_line: float | None,
                              observed_at: datetime, now: datetime, pending: Mapping[str, Any] | None = None,
                              hard_failure: bool = False, max_breach_pct: float = 0.25,
                              max_seconds: float = 60, min_spacing_seconds: float = 15) -> dict[str, Any]:
    """Research state transition: two distinct quotes, at most one minute.

    Unknown structure, hard failure, deep breach or stale quotes never receive
    confirmation grace. A timeout requires a fresh executable quote; this
    function reports an exit requirement, never a fabricated fill.
    """
    if not (0 < max_breach_pct <= 0.5 and 0 < min_spacing_seconds <= max_seconds <= 120):
        raise ValueError("confirmation bounds exceeded")
    if hard_failure:
        return {"action": "exit", "reason": "hard_failure"}
    if not all(math.isfinite(v) and v > 0 for v in (price, cost_line)):
        return {"action": "exit_required", "reason": "invalid_quote"}
    if structural_line is None or not math.isfinite(structural_line) or structural_line <= 0:
        return {"action": "exit" if price < cost_line else "clear", "reason": "unknown_structure"}
    if price < structural_line:
        return {"action": "exit", "reason": "original_structure_stop"}
    age = (now - observed_at).total_seconds()
    if not 0 <= age <= min_spacing_seconds:
        return {"action": "exit_required", "reason": "stale_quote"}
    if price >= cost_line:
        return {"action": "clear", "reason": "recovered"}
    if (cost_line - price) / cost_line * 100 > max_breach_pct:
        return {"action": "exit", "reason": "deep_cost_breach"}
    if pending:
        first = datetime.fromisoformat(str(pending["first_breach_at"]))
        last = datetime.fromisoformat(str(pending["last_observed_at"]))
        if (now - first).total_seconds() >= max_seconds:
            return {"action": "exit", "reason": "confirmation_timeout"}
        if (observed_at - last).total_seconds() >= min_spacing_seconds:
            return {"action": "exit", "reason": "two_distinct_breaches"}
        return {"action": "wait", "reason": "duplicate_or_too_soon", "pending": dict(pending)}
    return {"action": "wait", "reason": "small_cost_breach", "pending": {
        "first_breach_at": now.isoformat(), "last_observed_at": observed_at.isoformat(),
    }}
