"""Pure daily/weekly Chanlun pipeline for JSON-like OHLCV rows.

The implementation keeps the useful structural stages from the migrated tool:
K-line inclusion merging, fractals, strokes, centres (中枢), MACD-area
divergence, and first/second/third-class buy and sell points.  It accepts and
returns ordinary dictionaries so callers do not need the legacy data models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._normalization import normalize_rows


@dataclass
class _Merged:
    date_start: str
    date_end: str
    high: float
    low: float
    direction: int
    raw_count: int


@dataclass
class _Fractal:
    index: int
    type: str
    price: float
    date: str


@dataclass
class _Stroke:
    direction: str
    start_price: float
    end_price: float
    start_date: str
    end_date: str
    start_idx: int
    end_idx: int
    macd_area: float = 0.0
    has_divergence: bool = False


@dataclass
class _Zhongshu:
    start_date: str
    end_date: str
    zg: float
    zd: float
    zz: float
    stroke_start_idx: int
    stroke_end_idx: int
    is_broken: bool = False
    break_direction: str = ""


@dataclass
class _Signal:
    type: str
    price: float
    date: str
    description: str
    confidence: int


def calc_daily_macd(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    """Return DIF/DEA/BAR using SMA-seeded 12/26/9 exponential averages."""
    if not closes:
        return [], [], []
    count = len(closes)
    seed12 = sum(closes[:12]) / min(12, count)
    seed26 = sum(closes[:26]) / min(26, count)
    ema12 = [seed12] * count
    ema26 = [seed26] * count
    for index in range(12, count):
        ema12[index] = ema12[index - 1] + (closes[index] - ema12[index - 1]) * (2.0 / 13.0)
    for index in range(26, count):
        ema26[index] = ema26[index - 1] + (closes[index] - ema26[index - 1]) * (2.0 / 27.0)
    dif = [fast - slow for fast, slow in zip(ema12, ema26)]
    seed_dea = sum(dif[:9]) / min(9, count)
    dea = [seed_dea] * count
    for index in range(9, count):
        dea[index] = dea[index - 1] + (dif[index] - dea[index - 1]) * (2.0 / 10.0)
    bar = [(dif_value - dea_value) * 2.0 for dif_value, dea_value in zip(dif, dea)]
    return dif, dea, bar


def _merge(rows: list[dict[str, Any]]) -> list[_Merged]:
    if not rows:
        return []
    current = _Merged(rows[0]["date"], rows[0]["date"], rows[0]["high"], rows[0]["low"], 0, 1)
    merged: list[_Merged] = []
    for row in rows[1:]:
        high, low = row["high"], row["low"]
        contained = (
            (high <= current.high and low >= current.low)
            or (high >= current.high and low <= current.low)
        )
        if not contained:
            merged.append(current)
            direction = 1 if high > current.high else -1
            current = _Merged(row["date"], row["date"], high, low, direction, 1)
            continue
        if current.direction > 0:
            next_high, next_low = max(high, current.high), max(low, current.low)
        elif current.direction < 0:
            next_high, next_low = min(high, current.high), min(low, current.low)
        else:
            next_high, next_low = max(high, current.high), min(low, current.low)
        current = _Merged(
            current.date_start,
            row["date"],
            next_high,
            next_low,
            current.direction,
            current.raw_count + 1,
        )
    merged.append(current)
    return merged


def _fractals(merged: list[_Merged]) -> list[_Fractal]:
    result: list[_Fractal] = []
    for index in range(1, len(merged) - 1):
        left, current, right = merged[index - 1], merged[index], merged[index + 1]
        if current.high > left.high and current.high > right.high:
            result.append(_Fractal(index, "top", current.high, current.date_end))
        elif current.low < left.low and current.low < right.low:
            result.append(_Fractal(index, "bottom", current.low, current.date_end))
    return result


def _strokes(fractals: list[_Fractal]) -> list[_Stroke]:
    if not fractals:
        return []
    result: list[_Stroke] = []
    start = fractals[0]
    start_position = 0
    direction = "down" if start.type == "top" else "up"
    end: _Fractal | None = None
    end_position = 0
    index = 1
    while index < len(fractals):
        fractal = fractals[index]
        if fractal.type == start.type:
            if end is not None:
                result.append(_Stroke(
                    direction,
                    start.price,
                    end.price,
                    start.date,
                    end.date,
                    start_position,
                    end_position,
                ))
                start, start_position = end, end_position
                direction = "up" if direction == "down" else "down"
                end = None
            else:
                more_extreme = (
                    direction == "down" and fractal.price > start.price
                ) or (
                    direction == "up" and fractal.price < start.price
                )
                if more_extreme:
                    start, start_position = fractal, index
                index += 1
        else:
            if fractal.index - start.index >= 4:
                end, end_position = fractal, index
            index += 1
    if end is not None:
        result.append(_Stroke(
            direction,
            start.price,
            end.price,
            start.date,
            end.date,
            start_position,
            end_position,
        ))
    return result


def _zhongshus(strokes: list[_Stroke]) -> list[_Zhongshu]:
    result: list[_Zhongshu] = []
    index = 0
    while index + 2 < len(strokes):
        first, second = strokes[index], strokes[index + 1]
        zd = max(
            min(first.start_price, first.end_price),
            min(second.start_price, second.end_price),
        )
        zg = min(
            max(first.start_price, first.end_price),
            max(second.start_price, second.end_price),
        )
        if zd < zg:
            result.append(_Zhongshu(
                first.start_date,
                second.end_date,
                zg,
                zd,
                (zg + zd) / 2.0,
                index,
                index + 1,
            ))
            index += 2
        else:
            index += 1
    for center in result:
        for stroke in strokes[center.stroke_end_idx + 1:]:
            if stroke.direction == "up" and stroke.end_price > center.zg:
                center.is_broken = True
                center.break_direction = "up"
                center.end_date = stroke.start_date
                break
            if stroke.direction == "down" and stroke.end_price < center.zd:
                center.is_broken = True
                center.break_direction = "down"
                center.end_date = stroke.start_date
                break
    return result


def _detect_divergence(
    strokes: list[_Stroke], macd_bar: list[float], dates: list[str]
) -> None:
    positions = {date: index for index, date in enumerate(dates)}
    for stroke in strokes:
        start = positions.get(stroke.start_date)
        end = positions.get(stroke.end_date)
        if start is None or end is None:
            continue
        stroke.macd_area = sum(abs(value) for value in macd_bar[start:end + 1])
    for index, stroke in enumerate(strokes):
        previous = next(
            (
                strokes[earlier]
                for earlier in range(index - 1, -1, -1)
                if strokes[earlier].direction == stroke.direction
            ),
            None,
        )
        if previous is None:
            continue
        new_extreme = (
            stroke.end_price < previous.end_price
            if stroke.direction == "down"
            else stroke.end_price > previous.end_price
        )
        stroke.has_divergence = stroke.macd_area < previous.macd_area and new_extreme


def _divergence_confidence(strokes: list[_Stroke], index: int) -> int:
    stroke = strokes[index]
    previous = next(
        (
            strokes[earlier]
            for earlier in range(index - 1, -1, -1)
            if strokes[earlier].direction == stroke.direction
        ),
        None,
    )
    if previous is None or previous.macd_area <= 0:
        return 55
    raw = 94.5 - 35.0 * (stroke.macd_area / previous.macd_area)
    return max(55, min(92, int(round(raw))))


def _signals(strokes: list[_Stroke], centers: list[_Zhongshu]) -> list[_Signal]:
    first_class: list[_Signal] = []
    buy2: list[_Signal] = []
    sell2: list[_Signal] = []
    buy3: list[_Signal] = []
    sell3: list[_Signal] = []
    for index, stroke in enumerate(strokes):
        if not stroke.has_divergence:
            continue
        signal_type = "buy1" if stroke.direction == "down" else "sell1"
        direction_text = "底背驰，空头力度衰竭" if signal_type == "buy1" else "顶背驰，多头力度衰竭"
        first_class.append(_Signal(
            signal_type,
            stroke.end_price,
            stroke.end_date,
            f"{'一类买点' if signal_type == 'buy1' else '一类卖点'}：日线{direction_text}",
            _divergence_confidence(strokes, index),
        ))
        follow_index = index + 2
        if follow_index >= len(strokes):
            continue
        follow = strokes[follow_index]
        if stroke.direction == "down" and follow.direction == "down" and follow.end_price > stroke.end_price:
            buy2.append(_Signal(
                "buy2", follow.end_price, follow.end_date,
                f"二类买点：一类买点后回落未破前低{stroke.end_price:.2f}", 70,
            ))
        elif stroke.direction == "up" and follow.direction == "up" and follow.end_price < stroke.end_price:
            sell2.append(_Signal(
                "sell2", follow.end_price, follow.end_date,
                f"二类卖点：一类卖点后反弹未破前高{stroke.end_price:.2f}", 70,
            ))
    for center in centers:
        if not center.is_broken:
            continue
        retrace_index = center.stroke_end_idx + 2
        if retrace_index >= len(strokes):
            continue
        retrace = strokes[retrace_index]
        if center.break_direction == "up" and retrace.direction == "down" and retrace.end_price > center.zg:
            buy3.append(_Signal(
                "buy3", retrace.end_price, retrace.end_date,
                f"三类买点：中枢[{center.zd:.2f}-{center.zg:.2f}]突破后回踩不入中枢", 75,
            ))
        elif center.break_direction == "down" and retrace.direction == "up" and retrace.end_price < center.zd:
            sell3.append(_Signal(
                "sell3", retrace.end_price, retrace.end_date,
                f"三类卖点：中枢[{center.zd:.2f}-{center.zg:.2f}]跌破后反弹不入中枢", 75,
            ))
    first_class.sort(key=lambda signal: signal.date)
    buy2.sort(key=lambda signal: signal.date)
    sell2.sort(key=lambda signal: signal.date)
    sell3.sort(key=lambda signal: signal.date)
    buy3.sort(key=lambda signal: signal.date)
    return first_class + buy2 + sell2 + sell3 + buy3


def _type_name(signal_type: str) -> str:
    return {
        "buy1": "一类买点",
        "buy2": "二类买点",
        "buy3": "三类买点",
        "sell1": "一类卖点",
        "sell2": "二类卖点",
        "sell3": "三类卖点",
    }.get(signal_type, signal_type)


def _state(
    strokes: list[_Stroke], centers: list[_Zhongshu], signals: list[_Signal]
) -> tuple[str, str]:
    if not strokes:
        return "笔形成中，无明确信号", "无信号"
    latest_stroke = strokes[-1]
    direction = "向上" if latest_stroke.direction == "up" else "向下"
    force = "多头" if latest_stroke.direction == "up" else "空头"
    if centers:
        center = centers[-1]
        if center.is_broken:
            center_text = "已向上突破" if center.break_direction == "up" else "已向下突破"
        else:
            center_text = f"[{center.zd:.2f}-{center.zg:.2f}]震荡中"
    else:
        center_text = "无中枢"
    state = f"处于{direction}笔中，{force}延续，最近中枢{center_text}"
    if not signals:
        return f"{state}，无信号", "无信号"
    latest = signals[-1]
    return (
        f"{state}，最新信号：{_type_name(latest.type)}@{latest.price:.2f}",
        f"最新信号：{_type_name(latest.type)}@{latest.price:.2f}({latest.date})",
    )


def analyze_chanlun_daily(rows: Any) -> dict[str, Any]:
    """Run the complete daily/weekly Chanlun structural analysis."""
    klines, _ = normalize_rows(rows)
    dates = [row["date"] for row in klines]
    merged = _merge(klines)
    fractals = _fractals(merged)
    strokes = _strokes(fractals)
    centers = _zhongshus(strokes)
    dif, dea, bar = calc_daily_macd([row["close"] for row in klines])
    _detect_divergence(strokes, bar, dates)
    signals = _signals(strokes, centers)
    current_state, summary = _state(strokes, centers, signals)
    description = (
        f"共{len(fractals)}个分型、{len(strokes)}笔、{len(centers)}个中枢、"
        f"{len(signals)}个信号。{current_state}"
    )
    return {
        "kline_count": len(klines),
        "merged_count": len(merged),
        "fractal_count": len(fractals),
        "stroke_count": len(strokes),
        "zhongshu_count": len(centers),
        "counts": {
            "klines": len(klines),
            "merged": len(merged),
            "fractals": len(fractals),
            "strokes": len(strokes),
            "zhongshus": len(centers),
            "signals": len(signals),
        },
        "fractals": [
            {
                "type": fractal.type,
                "type_name": "顶分型" if fractal.type == "top" else "底分型",
                "price": round(fractal.price, 2),
                "date": fractal.date,
            }
            for fractal in fractals
        ],
        "strokes": [
            {
                "direction": stroke.direction,
                "start_price": round(stroke.start_price, 2),
                "end_price": round(stroke.end_price, 2),
                "start_date": stroke.start_date,
                "end_date": stroke.end_date,
                "macd_area": round(stroke.macd_area, 4),
                "has_divergence": stroke.has_divergence,
            }
            for stroke in strokes
        ],
        "zhongshus": [
            {
                "start_date": center.start_date,
                "end_date": center.end_date,
                "zg": round(center.zg, 2),
                "zd": round(center.zd, 2),
                "zz": round(center.zz, 2),
                "is_broken": center.is_broken,
                "break_direction": center.break_direction,
            }
            for center in centers
        ],
        "signals": [
            {
                "type": signal.type,
                "type_name": _type_name(signal.type),
                "price": round(signal.price, 2),
                "date": signal.date,
                "description": signal.description,
                "confidence": signal.confidence,
            }
            for signal in signals
        ],
        # Full arrays are retained for charting and algorithm audit.  Rounding
        # prevents floating point tail noise while preserving signal precision.
        "macd_dif": [round(value, 6) for value in dif],
        "macd_dea": [round(value, 6) for value in dea],
        "macd_bar": [round(value, 6) for value in bar],
        "current_state": current_state,
        "summary": summary,
        "description": description,
    }


__all__ = ["analyze_chanlun_daily", "calc_daily_macd"]
