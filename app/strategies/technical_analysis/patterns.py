"""Classical price-pattern recognition with explainable levels and targets."""

from __future__ import annotations

from typing import Any

from ._indicators import find_peaks, find_troughs
from ._normalization import normalize_rows


def _result(
    name: str,
    direction: str,
    confidence: int,
    status: str,
    *,
    target_price: float | None = None,
    key_levels: dict[str, float] | None = None,
    description: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "direction": direction,
        "confidence": confidence,
        "status": status,
        "target_price": round(target_price, 2) if target_price is not None else None,
        "key_levels": {
            key: round(value, 2) for key, value in (key_levels or {}).items()
        },
        "description": description,
    }


def _box(rows: list[dict[str, Any]], price: float) -> dict[str, Any] | None:
    if len(rows) < 20:
        return None
    window = rows[-20:]
    upper = max(row["high"] for row in window)
    lower = min(row["low"] for row in window)
    if lower <= 0 or upper <= lower:
        return None
    amplitude = (upper - lower) / lower * 100.0
    if not 5.0 <= amplitude <= 25.0:
        return None
    near_upper = price >= upper * 0.98
    target = upper + (upper - lower)
    return _result(
        "箱体震荡",
        "看涨" if near_upper else "中性",
        55,
        "接近突破上沿" if near_upper else "形成中",
        target_price=target if near_upper else None,
        key_levels={"箱体上沿": upper, "箱体下沿": lower},
        description=(
            f"箱体振幅{amplitude:.1f}%，突破上沿目标{target:.2f}，"
            f"跌破下沿目标{lower - (upper - lower):.2f}"
        ),
    )


def _double_top_bottom(
    rows: list[dict[str, Any]], price: float
) -> dict[str, Any] | None:
    if len(rows) < 60:
        return None
    lows = [row["low"] for row in rows]
    highs = [row["high"] for row in rows]
    troughs = find_troughs(lows, window=5)
    for first, second in zip(troughs[-3:-1], troughs[-2:]):
        if second - first < 5:
            continue
        low_first, low_second = lows[first], lows[second]
        if min(low_first, low_second) <= 0:
            continue
        if abs(low_first - low_second) / min(low_first, low_second) > 0.03:
            continue
        neckline = max(row["high"] for row in rows[first:second + 1])
        bottom = min(low_first, low_second)
        if neckline <= bottom:
            continue
        target = neckline + (neckline - bottom)
        return _result(
            "双底",
            "看涨",
            65 if price > neckline else 55,
            "已突破" if price > neckline else "形成中",
            target_price=target,
            key_levels={"颈线": neckline, "底部": bottom},
            description="双底需以收盘突破颈线确认，目标按形态高度等幅测算",
        )

    peaks = find_peaks(highs, window=5)
    for first, second in zip(peaks[-3:-1], peaks[-2:]):
        if second - first < 5:
            continue
        high_first, high_second = highs[first], highs[second]
        if min(high_first, high_second) <= 0:
            continue
        if abs(high_first - high_second) / min(high_first, high_second) > 0.03:
            continue
        neckline = min(row["low"] for row in rows[first:second + 1])
        top = max(high_first, high_second)
        if price >= neckline or neckline >= top:
            continue
        target = neckline - (top - neckline)
        return _result(
            "双顶",
            "看跌",
            60,
            "已突破",
            target_price=target,
            key_levels={"颈线": neckline, "顶部": top},
            description=f"双顶跌破颈线{neckline:.2f}，等幅目标{target:.2f}",
        )
    return None


def _head_shoulders(
    rows: list[dict[str, Any]], price: float
) -> dict[str, Any] | None:
    if len(rows) < 20:
        return None
    lows = [row["low"] for row in rows]
    highs = [row["high"] for row in rows]
    troughs = find_troughs(lows, window=3)
    for left, head, right in zip(troughs[-4:-2], troughs[-3:-1], troughs[-2:]):
        if head - left < 2 or right - head < 2:
            continue
        if not (lows[head] < lows[left] and lows[head] < lows[right]):
            continue
        if min(lows[left], lows[right]) <= 0:
            continue
        if abs(lows[left] - lows[right]) / min(lows[left], lows[right]) > 0.08:
            continue
        neckline = max(highs[left], highs[right])
        if neckline <= lows[head]:
            continue
        target = neckline + (neckline - lows[head])
        broken = price > neckline
        return _result(
            "头肩底",
            "看涨",
            80 if broken else 60,
            "已突破" if broken else "形成中",
            target_price=target,
            key_levels={"颈线": neckline, "头部": lows[head]},
            description=f"突破颈线后按底部深度测算目标{target:.2f}",
        )

    peaks = find_peaks(highs, window=3)
    for left, head, right in zip(peaks[-4:-2], peaks[-3:-1], peaks[-2:]):
        if head - left < 2 or right - head < 2:
            continue
        if not (highs[head] > highs[left] and highs[head] > highs[right]):
            continue
        if min(highs[left], highs[right]) <= 0:
            continue
        if abs(highs[left] - highs[right]) / min(highs[left], highs[right]) > 0.08:
            continue
        neckline = min(lows[left], lows[right])
        if neckline >= highs[head]:
            continue
        target = neckline - (highs[head] - neckline)
        broken = price < neckline
        return _result(
            "头肩顶",
            "看跌",
            60 if broken else 45,
            "已突破" if broken else "形成中",
            target_price=target,
            key_levels={"颈线": neckline, "头部": highs[head]},
            description=f"跌破颈线后按顶部高度测算目标{target:.2f}",
        )
    return None


def _triangle(rows: list[dict[str, Any]], price: float) -> dict[str, Any] | None:
    if len(rows) < 20:
        return None
    window = rows[-30:]
    split = len(window) // 2
    first, second = window[:split], window[split:]
    first_high = max(row["high"] for row in first)
    second_high = max(row["high"] for row in second)
    first_low = min(row["low"] for row in first)
    second_low = min(row["low"] for row in second)
    upper_slope = second_high - first_high
    lower_slope = second_low - first_low
    if upper_slope < 0 and lower_slope > 0:
        return _result(
            "对称三角形",
            "中性",
            50,
            "形成中",
            key_levels={"上沿": second_high, "下沿": second_low},
            description="高点下移且低点抬高，等待收盘选择方向",
        )
    if first_high and abs(upper_slope) / first_high < 0.02 and lower_slope > 0:
        return _result(
            "上升三角形",
            "看涨",
            60,
            "已突破" if price > second_high else "接近突破",
            target_price=second_high + (second_high - first_low),
            key_levels={"阻力位": second_high},
            description=f"突破{second_high:.2f}确认上升三角形",
        )
    if first_low and abs(lower_slope) / first_low < 0.02 and upper_slope < 0:
        return _result(
            "下降三角形",
            "看跌",
            60,
            "已跌破" if price < second_low else "接近跌破",
            target_price=second_low - (first_high - second_low),
            key_levels={"支撑位": second_low},
            description=f"跌破{second_low:.2f}确认下降三角形",
        )
    return None


def _flag(rows: list[dict[str, Any]], price: float) -> dict[str, Any] | None:
    del price
    if len(rows) < 30:
        return None
    pole, flag = rows[-30:-15], rows[-15:]
    rise = pole[-1]["close"] - pole[0]["close"]
    flag_high = max(row["high"] for row in flag)
    flag_low = min(row["low"] for row in flag)
    if rise <= 0 or flag_low <= 0 or (flag_high - flag_low) / flag_low > 0.08:
        return None
    return _result(
        "上升旗形",
        "看涨",
        60,
        "整理中",
        target_price=flag_high + rise,
        key_levels={"旗形上沿": flag_high, "旗形下沿": flag_low},
        description=f"旗杆涨幅{rise:.2f}，突破后目标{flag_high + rise:.2f}",
    )


def _gap(rows: list[dict[str, Any]], price: float) -> dict[str, Any] | None:
    del price
    if len(rows) < 2:
        return None
    latest, previous = rows[-1], rows[-2]
    if latest["low"] > previous["high"]:
        return _result(
            "向上突破缺口",
            "看涨",
            65,
            "已形成",
            key_levels={"缺口上沿": latest["low"], "缺口下沿": previous["high"]},
            description=f"向上跳空{latest['low'] - previous['high']:.2f}，回补前视为支撑",
        )
    if latest["high"] < previous["low"]:
        return _result(
            "向下突破缺口",
            "看跌",
            65,
            "已形成",
            key_levels={"缺口上沿": previous["low"], "缺口下沿": latest["high"]},
            description=f"向下跳空{previous['low'] - latest['high']:.2f}，回补前视为压力",
        )
    return None


def _rounding_bottom(
    rows: list[dict[str, Any]], price: float
) -> dict[str, Any] | None:
    del price
    if len(rows) < 60:
        return None
    window = rows[-60:]
    lows = [row["low"] for row in window]
    bottom_index = lows.index(min(lows))
    if bottom_index < 15 or bottom_index > len(window) - 15:
        return None
    # Noise-tolerant slope comparison is more useful on market data than strict
    # monotonicity, while retaining the original price-and-volume premise.
    left_start = sum(lows[:5]) / 5.0
    left_end = sum(lows[bottom_index - 5:bottom_index]) / 5.0
    right_start = sum(lows[bottom_index + 1:bottom_index + 6]) / 5.0
    right_end = sum(lows[-5:]) / 5.0
    volumes = [row["volume"] for row in window]
    center_volume = sum(volumes[bottom_index - 3:bottom_index + 4]) / 7.0
    edge_volume = (sum(volumes[:7]) + sum(volumes[-7:])) / 14.0
    if not (left_end < left_start and right_end > right_start and center_volume <= edge_volume):
        return None
    return _result(
        "圆弧底",
        "看涨",
        70,
        "形成中",
        key_levels={"弧底低点": min(lows)},
        description="价格呈圆弧底且底部量能收缩，连续放量可确认起涨",
    )


def analyze_patterns(rows: Any) -> list[dict[str, Any]]:
    """Detect up to three material patterns in stable priority order."""
    klines, _ = normalize_rows(rows)
    price = klines[-1]["close"]
    window60 = klines[-60:]
    detectors = (
        (_head_shoulders, window60),
        (_double_top_bottom, window60),
        (_triangle, klines[-30:]),
        (_box, klines[-20:]),
        (_flag, window60),
        (_gap, window60),
        (_rounding_bottom, window60),
    )
    results: list[dict[str, Any]] = []
    for detector, window in detectors:
        result = detector(window, price)
        if result is not None:
            results.append(result)
        if len(results) == 3:
            break
    return results


__all__ = ["analyze_patterns"]
