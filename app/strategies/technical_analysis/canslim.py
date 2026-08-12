"""Price/volume proxy implementation of the seven CAN SLIM dimensions."""

from __future__ import annotations

from typing import Any

from ._indicators import pct_change, sma_series
from ._normalization import normalize_flows, normalize_quote, normalize_rows


def _c_score(rows: list[dict[str, Any]]) -> int:
    closes = [row["close"] for row in rows]
    gain20 = pct_change(closes[-21], closes[-1])
    gain5 = pct_change(closes[-6], closes[-1])
    score20 = 90 if gain20 > 20 else 80 if gain20 > 15 else 70 if gain20 > 10 else 60 if gain20 > 5 else 50 if gain20 > 0 else 35 if gain20 > -5 else 20
    score5 = 80 if gain5 > 5 else 70 if gain5 > 2 else 60 if gain5 > 0 else 50 if gain5 > -5 else 35
    return max(score20, score5)


def _a_score(rows: list[dict[str, Any]]) -> int:
    if len(rows) < 125:
        return 50
    gain = pct_change(rows[-121]["close"], rows[-1]["close"])
    return 90 if gain > 150 else 75 if gain > 30 else 70 if gain > 15 else 60 if gain > 12 else 50 if gain > 5 else 35 if gain > -15 else 20


def _cup_handle(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rows) < 80:
        return None
    window = rows[-120:]
    lows = [row["low"] for row in window]
    bottom_index = lows.index(min(lows))
    if bottom_index < 20 or bottom_index > len(window) - 20:
        return None
    cup_low = lows[bottom_index]
    cup_high = max(row["high"] for row in window[max(0, bottom_index - 20):bottom_index])
    right = window[bottom_index:]
    handle_high_index = max(range(len(right)), key=lambda index: right[index]["high"])
    handle_high = right[handle_high_index]["high"]
    handle_window = rows[-18:] if len(rows) >= 18 else rows
    handle_low = min(row["low"] for row in handle_window)
    if cup_high <= cup_low or handle_high <= cup_low or handle_high <= 0:
        return None
    cup_depth = (cup_high - cup_low) / cup_high * 100.0
    handle_depth = (handle_high - handle_low) / handle_high * 100.0
    if not 5.0 <= cup_depth <= 35.0 or handle_depth > 30.0:
        return None
    target = handle_high + (cup_high - cup_low)
    return {
        "pattern": "杯柄形态",
        "cup_high": round(cup_high, 2),
        "cup_low": round(cup_low, 2),
        "handle_high": round(handle_high, 2),
        "handle_low": round(handle_low, 2),
        "cup_depth": round(cup_depth, 1),
        "handle_depth": round(handle_depth, 1),
        "breakout": max(row["high"] for row in rows[-30:]) >= handle_high,
        "buy_point": round(handle_high, 2),
        "target": round(target, 2),
    }


def _n_score(rows: list[dict[str, Any]], cup: dict[str, Any] | None) -> int:
    price = rows[-1]["close"]
    high_long = max(row["high"] for row in rows[-250:])
    distance = (high_long - price) / high_long * 100.0 if high_long else 100.0
    previous = rows[-121:-1] if len(rows) >= 121 else rows[:-1]
    if previous and price >= max(row["high"] for row in previous):
        return 100
    if distance < 3.0:
        return 70
    if cup and cup["breakout"]:
        return 90 if distance < 8.0 else 85 if distance < 12.0 else 75
    return 40


def _s_score(rows: list[dict[str, Any]]) -> int:
    average = sum(row["volume"] for row in rows[-6:-1]) / 5.0
    ratio = rows[-1]["volume"] / average if average else 1.0
    return 38 if ratio < 0.5 else 55 if ratio >= 2.0 else 45 if ratio >= 1.5 else 40


def _l_score(rows: list[dict[str, Any]]) -> int:
    if len(rows) < 95:
        return 50
    closes = [row["close"] for row in rows]
    gain60 = pct_change(closes[-61], closes[-1])
    gain_long = pct_change(closes[-251], closes[-1]) if len(closes) >= 251 else pct_change(closes[0], closes[-1])
    base = 70 if gain60 >= 30 else 60 if gain60 >= 15 else 50 if gain60 >= 5 else 43 if gain60 >= 1 else 30 if gain60 >= -9 else 20
    adjustment = 18 if gain_long > 2.5 else 13 if gain_long > 0 else -5 if gain_long < -30 else 0
    return max(0, min(100, base + adjustment))


def _i_score(flows: list[dict[str, Any]]) -> int:
    values = [flow["main_net"] for flow in flows]
    if not values:
        return 50
    if len(values) >= 3 and all(value > 0 for value in values[-3:]):
        return 85
    total = sum(values[-5:])
    if total < 0:
        return 10
    if values[-1] < -5e8:
        return 45
    return 75 if total > 0 else 55


def _m_score(index_rows: list[dict[str, Any]] | None, rows: list[dict[str, Any]]) -> tuple[int, str]:
    source = index_rows if index_rows and len(index_rows) >= 60 else rows
    source_name = "大盘指数" if source is index_rows else "个股均线(近似)"
    closes = [row["close"] for row in source]
    ma20 = sma_series(closes, 20)[-1]
    ma60 = sma_series(closes, 60)[-1]
    if ma20 is None or ma60 is None:
        return 50, source_name
    up_days = sum(closes[index] > closes[index - 1] for index in range(len(closes) - 20, len(closes)))
    if ma20 > ma60 and up_days >= 13:
        return 80, source_name
    if ma20 > ma60 and up_days >= 7:
        return 70, source_name
    if ma20 > ma60:
        return 60, source_name
    return (15 if source_name == "大盘指数" else 35), source_name


def _grade(score: int) -> str:
    return "A+" if score >= 85 else "A" if score >= 70 else "B+" if score >= 60 else "B" if score >= 50 else "C+" if score >= 40 else "C" if score >= 30 else "D"


def analyze_canslim(
    rows: Any,
    quote: Any = None,
    flows: Any = None,
    index_rows: Any = None,
) -> dict[str, Any]:
    """Score C/A/N/S/L/I/M using only supplied price, quote and flow data."""
    klines, _ = normalize_rows(rows)
    normalize_quote(quote)  # Validate/accept quote shape; S intentionally uses closed bars.
    normalized_flows = normalize_flows(flows)
    normalized_index = None
    if index_rows is not None:
        try:
            normalized_index, _ = normalize_rows(index_rows)
        except ValueError:
            normalized_index = None
    cup = _cup_handle(klines)
    c, a, n, s, l, i = _c_score(klines), _a_score(klines), _n_score(klines, cup), _s_score(klines), _l_score(klines), _i_score(normalized_flows)
    m, market_source = _m_score(normalized_index, klines)
    scores = {"C": c, "A": a, "N": n, "S": s, "L": l, "I": i, "M": m}
    total = int(0.15 * c + 0.10 * a + 0.25 * n + 0.05 * s + 0.20 * l + 0.15 * i + 0.10 * m)
    signals: list[str] = []
    labels = {"C": "近期动量", "A": "中期趋势", "N": "新高/形态", "S": "供需关系", "L": "相对强度", "I": "机构资金", "M": "市场环境"}
    for key in ("C", "A", "N", "L", "I"):
        threshold = 70 if key == "L" else 65
        if scores[key] >= threshold:
            signals.append(f"{key}({labels[key]}){scores[key]}分")
    if m >= 70:
        signals.append(f"M(市场环境){m}分")
    if m < 40:
        signals.append("⚠️ 市场环境偏空，谨慎操作")
    if i < 30:
        signals.append("⚠️ 机构资金流出，注意风险")
    if l < 30:
        signals.append("⚠️ 相对强度弱势，非领涨股")
    return {
        "scores": scores,
        "c_score": c,
        "a_score": a,
        "n_score": n,
        "s_score": s,
        "l_score": l,
        "i_score": i,
        "m_score": m,
        "total": total,
        "grade": _grade(total),
        "signals": signals,
        "cup_handle": cup,
        "market_source": market_source,
        "description": f"综合{total}分({_grade(total)}) | C={c} A={a} N={n} S={s} L={l} I={i} M={m}",
    }


__all__ = ["analyze_canslim"]
