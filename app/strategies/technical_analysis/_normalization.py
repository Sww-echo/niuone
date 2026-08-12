"""JSON-like input normalization for the pure technical-analysis package."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


MINIMUM_ROWS = 60
MINIMUM_ROWS_ERROR = "technical analysis requires at least 60 rows"
SUPPORTED_PERIODS = frozenset({"day", "week"})

_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "trade_date", "datetime", "time", "日期", "交易日期"),
    "open": ("open", "open_price", "开盘", "开盘价"),
    "high": ("high", "high_price", "最高", "最高价"),
    "low": ("low", "low_price", "最低", "最低价"),
    "close": ("close", "close_price", "price", "收盘", "收盘价", "最新价"),
    "volume": ("volume", "vol", "成交量"),
    "amount": ("amount", "成交额"),
    "turnover": ("turnover", "turnover_rate", "换手", "换手率"),
    "pct": ("pct", "pct_chg", "change_pct", "涨跌幅"),
}


def _pick(mapping: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def finite_number(value: Any, default: float | None = None) -> float | None:
    """Return a finite float, or ``default`` for unusable JSON values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _mapping_rows(rows: Any) -> list[Mapping[str, Any]]:
    if isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Iterable):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def normalize_rows(
    rows: Any,
    *,
    minimum: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize chronological OHLCV mappings without requiring model objects.

    Rows with no usable positive close are rejected.  Missing open/high/low/date
    values use conservative positional fallbacks and are disclosed in the
    returned quality metadata.  This keeps calculation inputs serializable while
    making degraded data visible to callers.
    """
    try:
        supplied = list(rows) if (
            isinstance(rows, Iterable) and not isinstance(rows, (str, bytes, Mapping))
        ) else []
    except TypeError:
        supplied = []
    mappings = [row for row in supplied if isinstance(row, Mapping)]
    normalized: list[dict[str, Any]] = []
    rejected = len(supplied) - len(mappings)
    fallback_dates = 0
    fallback_ohlc = 0
    missing_volume = 0

    for index, row in enumerate(mappings):
        close = finite_number(_pick(row, _ALIASES["close"]))
        if close is None or close <= 0:
            rejected += 1
            continue
        open_price = finite_number(_pick(row, _ALIASES["open"]), close)
        high = finite_number(_pick(row, _ALIASES["high"]), max(open_price or close, close))
        low = finite_number(_pick(row, _ALIASES["low"]), min(open_price or close, close))
        if open_price is None or high is None or low is None or high <= 0 or low <= 0:
            rejected += 1
            continue
        if any(_pick(row, _ALIASES[name]) is None for name in ("open", "high", "low")):
            fallback_ohlc += 1
        high = max(high, open_price, close)
        low = min(low, open_price, close)

        raw_volume = _pick(row, _ALIASES["volume"])
        volume = finite_number(raw_volume, 0.0) or 0.0
        if raw_volume is None:
            missing_volume += 1
        date_value = _pick(row, _ALIASES["date"])
        if date_value is None:
            fallback_dates += 1
            date_value = f"row-{index + 1:06d}"

        normalized.append({
            "date": str(date_value)[:32],
            "open": float(open_price),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": max(0.0, float(volume)),
            "amount": max(0.0, finite_number(_pick(row, _ALIASES["amount"]), 0.0) or 0.0),
            "turnover": max(0.0, finite_number(_pick(row, _ALIASES["turnover"]), 0.0) or 0.0),
            "pct": finite_number(_pick(row, _ALIASES["pct"])),
        })

    dates = [row["date"] for row in normalized]
    reordered = bool(dates and all(not date.startswith("row-") for date in dates) and dates != sorted(dates))
    if reordered:
        normalized.sort(key=lambda row: row["date"])

    previous_close: float | None = None
    for row in normalized:
        if row["pct"] is None:
            row["pct"] = (
                (row["close"] / previous_close - 1.0) * 100.0
                if previous_close
                else 0.0
            )
        row["pct"] = float(row["pct"])
        previous_close = row["close"]

    if len(normalized) < minimum:
        if minimum == MINIMUM_ROWS:
            raise ValueError(MINIMUM_ROWS_ERROR)
        raise ValueError("technical analysis requires at least one valid row")

    metadata = {
        "input_row_count": len(supplied),
        "valid_row_count": len(normalized),
        "rejected_row_count": rejected,
        "fallback_date_count": fallback_dates,
        "fallback_ohlc_count": fallback_ohlc,
        "missing_volume_count": missing_volume,
        "chronology_reordered": reordered,
    }
    return normalized, metadata


def normalize_quote(quote: Any) -> dict[str, Any] | None:
    """Normalize an optional live-quote mapping."""
    if not isinstance(quote, Mapping):
        return None
    price = finite_number(_pick(quote, _ALIASES["close"]))
    volume = finite_number(_pick(quote, _ALIASES["volume"]), 0.0) or 0.0
    turnover = finite_number(_pick(quote, _ALIASES["turnover"]), 0.0) or 0.0
    if price is None and not volume and not turnover:
        return None
    return {
        "price": price,
        "volume": max(0.0, volume),
        "turnover": max(0.0, turnover),
    }


def normalize_flows(flows: Any) -> list[dict[str, Any]]:
    """Normalize daily main-fund-flow mappings, preserving chronological order."""
    if isinstance(flows, (str, bytes, Mapping)) or not isinstance(flows, Iterable):
        return []
    result: list[dict[str, Any]] = []
    for index, flow in enumerate(flows):
        if not isinstance(flow, Mapping):
            continue
        main_net = finite_number(_pick(flow, ("main_net", "mainNet", "主力净流入", "主力净额")))
        if main_net is None:
            continue
        date = _pick(flow, _ALIASES["date"])
        result.append({
            "date": str(date)[:32] if date is not None else f"flow-{index + 1:06d}",
            "main_net": main_net,
        })
    if result and all(not item["date"].startswith("flow-") for item in result):
        result.sort(key=lambda item: item["date"])
    return result


def normalize_period(period: Any) -> str:
    resolved = str(period or "day").strip().lower()
    if resolved not in SUPPORTED_PERIODS:
        raise ValueError(f"unsupported technical period: {resolved}")
    return resolved


__all__ = [
    "MINIMUM_ROWS",
    "MINIMUM_ROWS_ERROR",
    "SUPPORTED_PERIODS",
    "finite_number",
    "normalize_flows",
    "normalize_period",
    "normalize_quote",
    "normalize_rows",
]
