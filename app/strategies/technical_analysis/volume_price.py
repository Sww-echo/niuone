"""Volume/price, OBV, turnover and optional fund-flow analysis."""

from __future__ import annotations

from typing import Any

from ._indicators import ma_direction
from ._normalization import normalize_flows, normalize_quote, normalize_rows


def _classify(rows: list[dict[str, Any]]) -> tuple[str, str, int]:
    closes = [row["close"] for row in rows]
    gain8 = (closes[-1] - closes[-8]) / closes[-8] * 100.0 if closes[-8] else 0.0
    price_direction = "涨" if gain8 > 2.0 else ("跌" if gain8 < -2.0 else "平")
    volumes = [row["volume"] for row in rows]
    latest_average = sum(volumes[-3:]) / 3.0
    previous_average = sum(volumes[-8:-3]) / 5.0
    volume_change = (
        (latest_average - previous_average) / previous_average * 100.0
        if previous_average
        else 0.0
    )
    volume_direction = "增" if volume_change > 30.0 else ("缩" if volume_change < -30.0 else "平")
    pattern = f"价{price_direction}量{volume_direction}"
    direction = "看涨" if price_direction == "涨" else ("看跌" if price_direction == "跌" else "中性")
    confidence = {
        "价涨量增": 80,
        "价涨量平": 55,
        "价涨量缩": 60,
        "价平量增": 50,
        "价平量平": 20,
        "价平量缩": 35,
        "价跌量增": 75,
        "价跌量平": 50,
        "价跌量缩": 60,
    }[pattern]
    return pattern, direction, confidence


def _obv(rows: list[dict[str, Any]]) -> list[float]:
    result: list[float] = []
    running = 0.0
    previous: float | None = None
    for row in rows:
        close = row["close"]
        if previous is not None and close != previous:
            running += row["volume"] if close > previous else -row["volume"]
        result.append(running)
        previous = close
    return result


def _fund_signal(flows: list[dict[str, Any]]) -> tuple[str, int]:
    values = [flow["main_net"] for flow in flows[-3:]]
    if not values:
        return "", 0
    if len(values) >= 3 and all(value > 0 for value in values):
        return "连续3日主力净流入", 15
    if len(values) >= 3 and all(value < 0 for value in values):
        return "连续3日主力净流出", -15
    if len(values) < 3:
        return "主力资金样本不足", 0
    previous_average = (values[0] + values[1]) / 2.0
    if previous_average >= 0:
        threshold = previous_average * 0.5
        if values[-1] < -threshold:
            return "今日主力大幅流出", -10
        if values[-1] > threshold:
            return "今日主力大幅流入", 10
    return "主力资金温和", 0


def analyze_volume_price(rows: Any, quote: Any = None, flows: Any = None) -> dict[str, Any]:
    """Classify recent volume/price behavior and supporting participation."""
    klines, _ = normalize_rows(rows)
    normalized_quote = normalize_quote(quote)
    normalized_flows = normalize_flows(flows)
    pattern, direction, confidence = _classify(klines)
    prior_five = [row["volume"] for row in klines[-6:-1]]
    average_five = sum(prior_five) / len(prior_five) if prior_five else 0.0
    if normalized_quote and normalized_quote["volume"] > 0 and average_five:
        volume_ratio = normalized_quote["volume"] / average_five
    elif average_five:
        volume_ratio = klines[-1]["volume"] / average_five
    else:
        volume_ratio = 1.0
    volume_ratio = round(volume_ratio, 2)
    turnover = (
        normalized_quote["turnover"]
        if normalized_quote and normalized_quote["turnover"]
        else klines[-1]["turnover"]
    )

    obv_values = _obv(klines)
    obv_direction = ma_direction(obv_values, lookback=8)
    obv_trend = "上升" if obv_direction == "向上" else ("下降" if obv_direction == "向下" else "走平")
    fund_text, fund_delta = _fund_signal(normalized_flows)
    if volume_ratio < 0.5:
        confidence -= 3
    elif volume_ratio < 1.5:
        confidence += 2
    elif volume_ratio < 2.0:
        confidence += 7
    else:
        confidence += 12
    confidence = max(5, min(95, confidence + fund_delta))

    signals = [fund_text] if fund_text else []
    if obv_trend != "走平":
        signals.append(f"OBV{obv_trend}")
    latest = klines[-1]
    if latest["pct"] >= 9.5 and average_five and latest["volume"] > average_five * 1.5:
        signals.append(f"放量涨停(pct={latest['pct']:.1f}%)")
    previous_twenty = [row["volume"] for row in klines[-21:-1]]
    previous_average = sum(previous_twenty) / len(previous_twenty) if previous_twenty else 0.0
    if (
        previous_twenty
        and previous_average
        and latest["volume"] > previous_average * 1.5
        and latest["volume"] > max(previous_twenty)
    ):
        signals.append("量能突破，资金活跃")

    description = f"量价模式={pattern}，量比={volume_ratio}，换手={turnover:.1f}%"
    if fund_text:
        description += f"，{fund_text}"
    return {
        "pattern": pattern,
        "direction": direction,
        "confidence": confidence,
        "volume_ratio": volume_ratio,
        "turnover": round(turnover, 2),
        "obv_trend": obv_trend,
        "signals": signals,
        "description": description,
    }


__all__ = ["analyze_volume_price"]
