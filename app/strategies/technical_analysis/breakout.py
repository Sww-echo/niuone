"""Turtle-style 20/55-bar Donchian breakout analysis."""

from __future__ import annotations

from typing import Any

from ._normalization import normalize_rows


def calc_true_range(high: float, low: float, previous_close: float) -> float:
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def calc_n(rows: Any, period: int = 20) -> float:
    klines, _ = normalize_rows(rows)
    if len(klines) < period + 1:
        return 0.0
    values = [
        calc_true_range(klines[index]["high"], klines[index]["low"], klines[index - 1]["close"])
        for index in range(len(klines) - period, len(klines))
    ]
    return round(sum(values) / len(values), 4)


def _channel(klines: list[dict[str, Any]], period: int) -> tuple[float, float]:
    window = klines[-period - 1:-1] if len(klines) > period else klines[:-1]
    if not window:
        return 0.0, 0.0
    return max(row["high"] for row in window), min(row["low"] for row in window)


def _last_entry(klines: list[dict[str, Any]], period: int) -> tuple[str, float, int] | None:
    for index in range(len(klines) - 1, period - 1, -1):
        window = klines[index - period:index]
        channel_high = max(row["high"] for row in window)
        channel_low = min(row["low"] for row in window)
        if klines[index]["high"] > channel_high:
            return "多", channel_high, index
        if klines[index]["low"] < channel_low:
            return "空", channel_low, index
    return None


def _system(klines: list[dict[str, Any]], period: int, name: str) -> dict[str, Any]:
    n_value = calc_n(klines, 20)
    channel_high, channel_low = _channel(klines, period)
    entry = _last_entry(klines, period)
    base = {
        "system": name,
        "signal": "无信号",
        "breakout_price": round(channel_high, 2),
        "current_n": n_value,
        "stop_loss": 0.0,
        "entry_price": None,
        "position_units": 0,
        "exit_price": None,
        "channel_high": round(channel_high, 2),
        "channel_low": round(channel_low, 2),
        "next_add_price": None,
        "signals": [],
        "description": f"{name}无突破信号",
    }
    if n_value <= 0 or channel_high <= 0 or entry is None:
        return base

    direction, entry_price, entry_index = entry
    holding_days = len(klines) - 1 - entry_index
    if direction == "多":
        later_high = max(
            (row["high"] for row in klines[entry_index + 1:]),
            default=entry_price,
        )
        extra_units = int(max(0.0, later_high - entry_price) // (0.5 * n_value))
        units = min(4, 1 + extra_units)
        latest_add = entry_price + (units - 1) * 0.5 * n_value
        stop = latest_add - 2.0 * n_value
        next_add = entry_price + units * 0.5 * n_value if units < 4 else None
        if klines[-1]["close"] <= stop:
            signal, exit_price = "卖出", stop
            signal_text = f"触及2N止损{stop:.2f}，卖出"
        else:
            signal, exit_price = "持仓", None
            signal_text = f"持有多头{holding_days}日，止损{stop:.2f}"
    else:
        units, next_add = 1, None
        stop = entry_price + 2.0 * n_value
        exit_period = 10 if period == 20 else 20
        exit_high, _ = _channel(klines, exit_period)
        if klines[-1]["high"] >= exit_high:
            signal, exit_price = "空头平仓", exit_high
            signal_text = f"突破{exit_period}日高点{exit_high:.2f}，空头平仓"
        elif klines[-1]["close"] >= stop:
            signal, exit_price = "空头平仓", stop
            signal_text = f"触及2N止损{stop:.2f}，空头平仓"
        else:
            signal, exit_price = "持仓空头", None
            signal_text = f"持有空头{holding_days}日，止损{stop:.2f}"

    base.update({
        "signal": signal,
        "stop_loss": round(stop, 2),
        "entry_price": round(entry_price, 2),
        "position_units": units,
        "exit_price": round(exit_price, 2) if exit_price is not None else None,
        "next_add_price": round(next_add, 2) if next_add is not None else None,
        "signals": [signal_text],
        "description": f"{name}入场={direction}@{entry_price:.2f}，N={n_value:.4f}，持有{holding_days}日",
    })
    return base


def analyze_breakout(rows: Any) -> list[dict[str, Any]]:
    """Return Turtle system-one (20-bar) and system-two (55-bar) states."""
    klines, _ = normalize_rows(rows)
    return [
        _system(klines, 20, "系统一(20日)"),
        _system(klines, 55, "系统二(55日)"),
    ]


__all__ = ["analyze_breakout", "calc_n", "calc_true_range"]
