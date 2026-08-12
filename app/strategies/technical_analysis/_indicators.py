"""Dependency-free technical indicators shared by analysis modules."""

from __future__ import annotations

from collections.abc import Sequence


def sma_series(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0 or not values:
        return []
    result: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            result[index] = running / period
    return result


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0 or not values:
        return []
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    alpha = 2.0 / (period + 1)
    previous = sum(values[:period]) / period
    result[period - 1] = previous
    for index in range(period, len(values)):
        previous = alpha * values[index] + (1.0 - alpha) * previous
        result[index] = previous
    return result


def last_sma(values: Sequence[float], period: int) -> float:
    if period <= 0 or len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def ma_direction(values: Sequence[float | None], lookback: int = 5) -> str:
    valid = [value for value in values if value is not None]
    if len(valid) < lookback + 1:
        return "未知"
    recent = valid[-(lookback + 1):]
    slope = recent[-1] - recent[0]
    threshold = abs(recent[0]) * 0.002 if recent[0] else 1e-9
    if slope > threshold:
        return "向上"
    if slope < -threshold:
        return "向下"
    return "走平"


def find_peaks(values: Sequence[float], window: int = 3) -> list[int]:
    peaks: list[int] = []
    for index in range(window, len(values) - window):
        lower, upper = index - window, index + window
        neighbors = [values[pos] for pos in range(lower, upper + 1) if pos != index]
        if all(values[index] >= value for value in neighbors) and any(
            value != values[index] for value in neighbors
        ):
            peaks.append(index)
    return peaks


def find_troughs(values: Sequence[float], window: int = 3) -> list[int]:
    troughs: list[int] = []
    for index in range(window, len(values) - window):
        lower, upper = index - window, index + window
        neighbors = [values[pos] for pos in range(lower, upper + 1) if pos != index]
        if all(values[index] <= value for value in neighbors) and any(
            value != values[index] for value in neighbors
        ):
            troughs.append(index)
    return troughs


def pct_change(start: float, end: float) -> float:
    return (end - start) / start * 100.0 if start else 0.0


__all__ = [
    "ema_series",
    "find_peaks",
    "find_troughs",
    "last_sma",
    "ma_direction",
    "pct_change",
    "sma_series",
]
