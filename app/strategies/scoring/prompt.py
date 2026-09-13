"""Neutral market-data scorer for prompt-driven strategy candidate pools."""
from __future__ import annotations

import math
import statistics
from typing import Any

from .common import safe_float, safe_round, with_strategy_profile


def _return_pct(current: float, previous: float | None) -> float | None:
    if previous is None or previous <= 0:
        return None
    return (current / previous - 1.0) * 100.0


def score_preset_text(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Expose broad, strategy-neutral facts without borrowing another entry rule."""
    if len(rows) < 30:
        return None
    recent = rows[-1]
    close = safe_float(recent.get("close"))
    if close is None or close <= 0:
        return None

    closes = [safe_float(row.get("close")) for row in rows]
    volumes = [safe_float(row.get("volume")) for row in rows]
    valid_closes = [value for value in closes if value is not None and value > 0]
    if len(valid_closes) < 30:
        return None

    amount = safe_float(recent.get("quote_amount")) or 0.0
    amount_yi = amount / 1e8 if amount > 0 else 0.0
    liquidity_score = max(0.0, min(10.0, 3.5 + math.log10(amount_yi + 1.0) * 3.0))
    return_5d = _return_pct(close, closes[-6] if len(closes) >= 6 else None)
    return_20d = _return_pct(close, closes[-21] if len(closes) >= 21 else None)
    ema20 = safe_float(recent.get("ema20"))
    bbi = safe_float(recent.get("bbi"))
    distance_ema20 = _return_pct(close, ema20)
    distance_bbi = _return_pct(close, bbi)
    high_20d = max(
        safe_float(row.get("high")) or close
        for row in rows[-20:]
    )
    distance_high_20d = _return_pct(close, high_20d)

    recent_volumes = [value for value in volumes[-5:] if value is not None and value >= 0]
    prior_volumes = [value for value in volumes[-25:-5] if value is not None and value >= 0]
    volume_ratio = (
        statistics.mean(recent_volumes) / statistics.mean(prior_volumes)
        if recent_volumes and prior_volumes and statistics.mean(prior_volumes) > 0
        else None
    )
    daily_returns = [
        (right / left - 1.0) * 100.0
        for left, right in zip(valid_closes[-21:-1], valid_closes[-20:])
        if left > 0
    ]
    volatility = statistics.pstdev(daily_returns) if len(daily_returns) >= 2 else None
    turnover = safe_float(recent.get("quote_turnover"))
    change_pct = safe_float(recent.get("quote_change_pct"))
    current_j = safe_float(recent.get("j"))

    risk_flags: list[str] = []
    if volatility is not None and volatility > 5.0:
        risk_flags.append("20日波动偏高")
    if turnover is not None and turnover > 15.0:
        risk_flags.append("换手率偏高")
    if change_pct is not None and change_pct >= 9.5:
        risk_flags.append("接近涨停，不宜按静态价格追单")
    if amount <= 0:
        risk_flags.append("成交额缺失")

    return with_strategy_profile("preset_text", {
        "score": liquidity_score,
        "score_total": 10,
        "verdict": "预设文字策略中性候选",
        "distance_pct": None,
        "bbi": safe_round(bbi, 3),
        "ema20": safe_round(ema20, 3),
        "distance_bbi_pct": safe_round(distance_bbi, 3),
        "distance_ema20_pct": safe_round(distance_ema20, 3),
        "distance_high_20d_pct": safe_round(distance_high_20d, 3),
        "return_5d_pct": safe_round(return_5d, 3),
        "return_20d_pct": safe_round(return_20d, 3),
        "volume_ratio_5d": safe_round(volume_ratio, 3),
        "volatility_20d_pct": safe_round(volatility, 3),
        "current_j": safe_round(current_j, 3),
        "above_ema20": bool(ema20 is not None and close >= ema20),
        "above_bbi": bool(bbi is not None and close >= bbi),
        "recent_close": safe_round(close, 3),
        "change_pct": safe_round(change_pct, 3),
        "amount_yi": safe_round(amount_yi, 3),
        "turnover": safe_round(turnover, 3),
        "risk_flags": risk_flags,
    })
