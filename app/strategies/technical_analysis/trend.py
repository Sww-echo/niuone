"""Moving-average trend and rising-trendline analysis."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ._indicators import ma_direction, sma_series
from ._normalization import normalize_rows


def _trendline(rows: Sequence[Mapping[str, Any]], direction: str) -> dict[str, Any] | None:
    """Find the most recent valid rising support line in a 20-bar window."""
    if direction != "上升" or len(rows) < 21:
        return None
    lows = [float(row["low"]) for row in rows]
    window_start = len(rows) - 20
    window_end = len(rows) - 1
    troughs: list[int] = []
    for index in range(window_start, window_end + 1):
        lower = max(window_start, index - 5)
        upper = min(window_end, index + 5)
        neighbors = lows[lower:index] + lows[index + 1:upper + 1]
        if neighbors and lows[index] <= min(neighbors):
            troughs.append(index)
    first = troughs[0] if troughs else window_start
    later = lows[first + 1:window_end + 1]
    if not later:
        return None
    second = first + 1 + later.index(min(later))
    if lows[second] <= lows[first]:
        return None
    if first > window_start and lows[second] >= min(lows[window_start:first]):
        return None
    slope = (lows[second] - lows[first]) / (second - first)
    if slope <= 0:
        return None
    return {
        "type": "上升趋势线",
        "slope": round(slope, 4),
        "current_price": round(lows[first] + slope * (len(rows) - 1 - first), 2),
        "points": [first - window_start, second - window_start],
        "dates": [rows[first]["date"], rows[second]["date"]],
    }


def _stage(direction: str, strength: int) -> str:
    if direction == "上升":
        if strength >= 70:
            return "强势上升趋势"
        if strength >= 45:
            return "上升趋势形成中"
        return "弱势上升"
    if direction == "下降":
        return "强势下降趋势" if strength <= 30 else "下降趋势"
    return "震荡整理"


def analyze_trend(rows: Any) -> dict[str, Any]:
    """Analyze trend using MA20/MA60 direction, position and resonance."""
    klines, _ = normalize_rows(rows)
    closes = [row["close"] for row in klines]
    price = closes[-1]
    ma20 = sma_series(closes, 20)
    ma60 = sma_series(closes, 60)
    ma20_value = ma20[-1] if ma20 else None
    ma60_value = ma60[-1] if ma60 else None
    ma20_direction = ma_direction(ma20, lookback=5)
    ma60_direction = ma_direction(ma60, lookback=5)

    scores = {
        "ma20_dir": 30 if ma20_direction == "向上" else 0,
        "ma60_dir": 25 if ma60_direction == "向上" else 0,
        "price_vs_ma20": 15 if ma20_value is not None and price > ma20_value else 0,
        "price_vs_ma60": 10 if ma60_value is not None and price > ma60_value else 0,
        "resonance": (
            20
            if len(closes) >= 21 and closes[-21] and price > closes[-21]
            else 0
        ),
    }
    strength = sum(scores.values())
    if ma20_value is not None and price > ma20_value and scores["ma20_dir"]:
        direction = "上升"
    elif ma20_value is not None and price < ma20_value and not scores["ma20_dir"]:
        direction = "下降"
    else:
        direction = "震荡"

    signals: list[str] = []
    if ma20_direction == "向上":
        signals.append("MA20向上")
    elif ma20_direction == "向下":
        signals.append("MA20向下")
    if ma60_value is not None and price > ma60_value:
        signals.append("站稳60日决策线")

    if ma20_value is None or ma60_value is None:
        arrangement = "纠缠"
    elif ma20_value > ma60_value:
        arrangement = "多头排列"
    elif ma20_value < ma60_value:
        arrangement = "空头排列"
    else:
        arrangement = "纠缠"

    return {
        "direction": direction,
        "strength": strength,
        "stage": _stage(direction, strength),
        "ma_arrangement": arrangement,
        "ma_scores": scores,
        "moving_averages": {
            "ma20": round(ma20_value, 4) if ma20_value is not None else None,
            "ma60": round(ma60_value, 4) if ma60_value is not None else None,
            "ma20_direction": ma20_direction,
            "ma60_direction": ma60_direction,
        },
        "trendline": _trendline(klines, direction),
        "signals": signals,
    }


__all__ = ["analyze_trend"]
