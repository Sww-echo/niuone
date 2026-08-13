"""Pure five-minute Chanlun analysis derived from one-minute price points.

Eastmoney's ``trends2`` response currently contains consecutive one-minute
time/price/volume points even when its query carries ``klt=5``.  This module
ports the legacy tool's useful contract: points are grouped inside each
continuous trading segment, five consecutive minutes per bar; a time gap such
as the lunch break or auction/open transition starts a new group, and the final
short group is retained.

The resulting OHLC is derived from minute representative prices (first, max,
min and last), not upstream-native five-minute OHLC.  Results disclose that
limitation instead of presenting the derived range as exchange-native K-lines.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .chanlun import analyze_chanlun_daily


MINIMUM_FIVE_MINUTE_BARS = 10
INPUT_KIND = "one_minute_representative_price_points"
BAR_KIND = "derived_five_minute_ohlcv"
OHLC_METHOD = "first_max_min_last_of_continuous_one_minute_representative_prices"
OHLC_WARNING = (
    "5分钟 OHLC 由连续1分钟代表价聚合（首价/最高/最低/末价），"
    "并非上游原生5分钟 OHLC K线。"
)

StructureAnalyzer = Callable[[Any], dict[str, Any]]


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _minute_label(value: Any) -> tuple[str, int] | None:
    text = str(value or "").strip()
    match = re.search(r"(?:^|\s)([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$", text)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return f"{hour:02d}:{minute:02d}", hour * 60 + minute


def _make_bar(points: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [float(point["price"]) for point in points]
    return {
        "date": points[-1]["time"],
        "time": points[-1]["time"],
        "start_time": points[0]["time"],
        "end_time": points[-1]["time"],
        "open": prices[0],
        "high": max(prices),
        "low": min(prices),
        "close": prices[-1],
        "volume": sum(float(point["volume"]) for point in points),
        "source_point_count": len(points),
        "bar_kind": BAR_KIND,
        "ohlc_is_derived": True,
    }


def build_five_minute_bars(
    times: Any,
    prices: Any,
    volumes: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate consecutive one-minute price points into five-minute bars."""
    time_values = _sequence(times)
    price_values = _sequence(prices)
    volume_values = _sequence(volumes)
    points: list[dict[str, Any]] = []
    invalid_price_count = 0
    invalid_time_count = 0
    missing_volume_count = 0

    for index, raw_price in enumerate(price_values):
        price = _finite(raw_price)
        if price is None or price <= 0:
            invalid_price_count += 1
            continue
        parsed_time = _minute_label(time_values[index] if index < len(time_values) else None)
        if parsed_time is None:
            invalid_time_count += 1
            continue
        label, minute_index = parsed_time
        raw_volume = volume_values[index] if index < len(volume_values) else None
        volume = _finite(raw_volume)
        if volume is None or volume < 0:
            missing_volume_count += 1
            volume = 0.0
        points.append({
            "time": label,
            "minute_index": minute_index,
            "source_index": index,
            "price": price,
            "volume": volume,
        })

    chronological = [point["minute_index"] for point in points]
    chronology_reordered = chronological != sorted(chronological)
    points.sort(key=lambda point: (point["minute_index"], point["source_index"]))
    unique_points: list[dict[str, Any]] = []
    seen_times: set[str] = set()
    duplicate_time_count = 0
    for point in points:
        if point["time"] in seen_times:
            duplicate_time_count += 1
            continue
        seen_times.add(point["time"])
        unique_points.append(point)

    bars: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []
    discontinuity_count = 0
    previous_minute_index: int | None = None
    for point in unique_points:
        is_discontinuous = (
            previous_minute_index is not None
            and point["minute_index"] - previous_minute_index != 1
        )
        if is_discontinuous:
            discontinuity_count += 1
            if group:
                bars.append(_make_bar(group))
                group = []
        group.append(point)
        previous_minute_index = point["minute_index"]
        if len(group) == 5:
            bars.append(_make_bar(group))
            group = []
    trailing_partial_count = len(group)
    if group:
        bars.append(_make_bar(group))

    quality = {
        "status": "derived",
        "degraded": True,
        "source_interval": "1m",
        "output_interval": "5m",
        "source_interval_minutes": 1,
        "output_interval_minutes": 5,
        "input_kind": INPUT_KIND,
        "input_point_count": len(price_values),
        "valid_point_count": len(unique_points),
        "derived_bar_count": len(bars),
        "invalid_price_count": invalid_price_count,
        "invalid_time_count": invalid_time_count,
        "duplicate_time_count": duplicate_time_count,
        "missing_volume_count": missing_volume_count,
        "discontinuity_count": discontinuity_count,
        "trailing_partial_point_count": trailing_partial_count,
        "chronology_reordered": chronology_reordered,
        "aggregated_from_one_minute": True,
        "real_ohlc_available": False,
        "ohlc_is_derived": True,
        "ohlc_method": OHLC_METHOD,
        "volume_semantics": "sum_of_source_one_minute_interval_values",
        "warnings": [OHLC_WARNING],
    }
    return bars, quality


def _minute_wording(result: dict[str, Any]) -> dict[str, Any]:
    converted = dict(result)
    signals: list[dict[str, Any]] = []
    for raw_signal in result.get("signals") or []:
        signal = dict(raw_signal) if isinstance(raw_signal, Mapping) else {}
        signal["description"] = str(signal.get("description") or "").replace(
            "日线", "5分钟派生结构"
        )
        signals.append(signal)
    converted["signals"] = signals
    return converted


def analyze_minute_technical(
    times: Any,
    prices: Any,
    volumes: Any,
    *,
    structure_analyzer: StructureAnalyzer = analyze_chanlun_daily,
) -> dict[str, Any]:
    """Run the complete pure Chanlun pipeline on derived five-minute bars."""
    bars, quality = build_five_minute_bars(times, prices, volumes)
    base = {
        "available": False,
        "period": "5m",
        "source_interval": "1m",
        "output_interval": "5m",
        "input_kind": INPUT_KIND,
        "bar_kind": BAR_KIND,
        "time_count": quality["valid_point_count"],
        "point_count": quality["valid_point_count"],
        "derived_bar_count": len(bars),
        "kline_count": len(bars),
        "derived_bars": bars,
        "data_quality": quality,
        "ohlc_note": OHLC_WARNING,
    }
    if len(bars) < MINIMUM_FIVE_MINUTE_BARS:
        return {
            **base,
            "reason": "minute_insufficient",
            "current_state": "样本不足",
            "summary": (
                f"5分钟派生 bar 不足（{len(bars)}根），"
                f"至少需要{MINIMUM_FIVE_MINUTE_BARS}根"
            ),
            "description": OHLC_WARNING,
            "counts": {
                "klines": len(bars), "fractals": 0, "strokes": 0,
                "zhongshus": 0, "signals": 0,
            },
            "fractals": [], "strokes": [], "zhongshus": [], "signals": [],
        }

    result = _minute_wording(structure_analyzer(bars))
    structure_description = str(result.get("description") or "").strip()
    result.update({
        **base,
        "available": True,
        "current_state": str(result.get("current_state") or "结构形成中"),
        "summary": f"5分钟派生结构：{str(result.get('summary') or '暂无明确信号')}",
        "description": f"{structure_description} {OHLC_WARNING}".strip(),
    })
    counts = dict(result.get("counts") or {})
    counts["source_points"] = quality["valid_point_count"]
    counts["derived_bars"] = len(bars)
    result["counts"] = counts
    return result


__all__ = [
    "BAR_KIND", "INPUT_KIND", "MINIMUM_FIVE_MINUTE_BARS", "OHLC_METHOD",
    "OHLC_WARNING", "analyze_minute_technical", "build_five_minute_bars",
]
