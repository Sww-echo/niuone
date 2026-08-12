"""Small, dependency-free adapter for five-minute Chanlun analysis.

The legacy project works on an intraday price stream rather than daily OHLCV
rows.  The structural Chanlun implementation in :mod:`chanlun` is deliberately
shared here by constructing conservative five-minute bars from the stream.
This keeps the minute API deterministic and avoids importing the legacy app at
runtime.
"""

from __future__ import annotations

from typing import Any

from .chanlun import analyze_chanlun_daily


def analyze_minute_technical(
    times: Any,
    prices: Any,
    volumes: Any,
) -> dict[str, Any]:
    """Analyze an intraday stream with the same JSON-safe Chanlun contract."""

    price_values = list(prices or [])
    time_values = list(times or [])
    volume_values = list(volumes or [])
    rows: list[dict[str, Any]] = []
    for index, raw_price in enumerate(price_values):
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        previous = rows[-1]["close"] if rows else price
        try:
            volume = max(0.0, float(volume_values[index]))
        except (IndexError, TypeError, ValueError):
            volume = 0.0
        timestamp = str(time_values[index]) if index < len(time_values) else f"bar-{index + 1:04d}"
        rows.append({
            "date": timestamp,
            "open": previous,
            "high": max(previous, price),
            "low": min(previous, price),
            "close": price,
            "volume": volume,
        })
    if len(rows) < 10:
        return {
            "available": False,
            "reason": "minute_insufficient",
            "summary": "分时样本不足，至少需要 10 个有效点",
            "kline_count": len(rows),
        }
    result = analyze_chanlun_daily(rows)
    result.update({
        "available": True,
        "period": "minute",
        "time_count": len(rows),
    })
    return result


__all__ = ["analyze_minute_technical"]
