"""Pure technical-analysis composition and explainable trading decision."""

from __future__ import annotations

from typing import Any

from ._normalization import (
    MINIMUM_ROWS,
    normalize_flows,
    normalize_period,
    normalize_quote,
    normalize_rows,
)
from .breakout import analyze_breakout
from .canslim import analyze_canslim
from .chanlun import analyze_chanlun_daily
from .patterns import analyze_patterns
from .trend import analyze_trend
from .volume_price import analyze_volume_price


def _pattern_score(patterns: list[dict[str, Any]]) -> int:
    score = 50.0
    for pattern in patterns:
        direction = pattern.get("direction")
        sign = 1 if direction == "看涨" else -1 if direction == "看跌" else 0
        score += sign * int(pattern.get("confidence") or 0) * 0.2
    return max(20, min(100, int(score)))


def _volume_score(volume_price: dict[str, Any]) -> int:
    confidence = int(volume_price.get("confidence") or 0)
    if volume_price.get("direction") == "看涨":
        return confidence
    if volume_price.get("direction") == "看跌":
        return max(20, 100 - confidence)
    return 50


def _breakout_score(breakouts: list[dict[str, Any]]) -> int:
    long_hold = any(item.get("signal") == "持仓" for item in breakouts)
    short_cover = any(item.get("signal") == "空头平仓" for item in breakouts)
    short_hold = any(item.get("signal") == "持仓空头" for item in breakouts)
    long_stop = any(item.get("signal") == "卖出" for item in breakouts)
    if long_hold:
        return 65
    if short_cover:
        return 63
    if long_stop or short_hold:
        return 35
    return 50


def _chanlun_score(chanlun: dict[str, Any], trend_direction: str) -> int:
    signals = chanlun.get("signals") or []
    buy = [signal for signal in signals if str(signal.get("type") or "").startswith("buy")]
    sell = [signal for signal in signals if str(signal.get("type") or "").startswith("sell")]
    if signals:
        latest = signals[-1]
        confidence = int(latest.get("confidence") or 50)
        return confidence if str(latest.get("type") or "").startswith("buy") else 100 - confidence
    state = str(chanlun.get("current_state") or "")
    if "向上" in state:
        return 60
    if "向下" in state:
        return 40
    if buy and not sell:
        return 60
    if sell and not buy:
        return 40
    return 55 if trend_direction == "上升" else 45 if trend_direction == "下降" else 50


def _risk(
    trend: dict[str, Any],
    volume_price: dict[str, Any],
    canslim: dict[str, Any],
    breakouts: list[dict[str, Any]],
    chanlun: dict[str, Any],
    *,
    quote_available: bool,
    flow_available: bool,
    index_available: bool,
) -> dict[str, Any]:
    risk_score = 15
    warnings: list[str] = []
    if trend.get("direction") == "下降":
        risk_score += 30
        warnings.append("处于下降趋势")
    elif trend.get("direction") == "震荡":
        risk_score += 10
        warnings.append("趋势尚未明确")
    if volume_price.get("direction") == "看跌":
        risk_score += 20
        warnings.append("量价配合偏空")
    if int(canslim.get("m_score") or 50) < 30:
        risk_score += 15
        warnings.append("市场环境偏空")
    if int(canslim.get("i_score") or 50) < 30:
        risk_score += 10
        warnings.append("主力资金偏弱")
    if any(item.get("signal") in {"卖出", "持仓空头"} for item in breakouts):
        risk_score += 10
        warnings.append("海龟系统存在空头或止损状态")
    chanlun_signals = chanlun.get("signals") or []
    if chanlun_signals and str(chanlun_signals[-1].get("type") or "").startswith("sell"):
        risk_score += 10
        warnings.append("缠论最新信号为卖点")
    if not quote_available:
        warnings.append("未提供实时行情，入场价基于末根K线")
    if not flow_available:
        warnings.append("未提供足够资金流数据，机构维度使用中性分")
    if not index_available:
        warnings.append("未提供大盘指数，市场维度使用个股趋势近似")
    risk_score = max(0, min(100, risk_score))
    level = "高" if risk_score >= 70 else "中" if risk_score >= 40 else "低"
    return {"level": level, "score": risk_score, "warnings": warnings}


def _key_levels(
    rows: list[dict[str, Any]],
    trend: dict[str, Any],
    patterns: list[dict[str, Any]],
    breakouts: list[dict[str, Any]],
    canslim: dict[str, Any],
    chanlun: dict[str, Any],
) -> dict[str, float]:
    levels: dict[str, float] = {
        "近期支撑": round(min(row["low"] for row in rows[-20:]), 2),
        "近期阻力": round(max(row["high"] for row in rows[-20:]), 2),
    }
    priority = next(
        (
            pattern
            for prefix in ("头肩", "双底", "双顶", "箱体")
            for pattern in patterns
            if str(pattern.get("name") or "").startswith(prefix)
        ),
        None,
    )
    if priority:
        for label, value in (priority.get("key_levels") or {}).items():
            levels[f"{priority['name']}_{label}"] = round(float(value), 2)
        if priority.get("target_price") is not None:
            levels[f"{priority['name']}_目标"] = round(float(priority["target_price"]), 2)
    for breakout in breakouts:
        system = str(breakout.get("system") or "突破")
        if float(breakout.get("stop_loss") or 0) > 0:
            levels[f"{system}_止损"] = round(float(breakout["stop_loss"]), 2)
        if float(breakout.get("channel_high") or 0) > 0:
            levels[f"{system}_通道上沿"] = round(float(breakout["channel_high"]), 2)
    cup = canslim.get("cup_handle")
    if isinstance(cup, dict) and cup.get("buy_point") is not None:
        levels["杯柄买点"] = round(float(cup["buy_point"]), 2)
    trendline = trend.get("trendline")
    if isinstance(trendline, dict) and trendline.get("current_price") is not None:
        levels["趋势线"] = round(float(trendline["current_price"]), 2)
    centers = chanlun.get("zhongshus") or []
    if centers:
        latest = centers[-1]
        levels["最近中枢上沿"] = round(float(latest["zg"]), 2)
        levels["最近中枢下沿"] = round(float(latest["zd"]), 2)
    return levels


def _trade_plan(
    action: str,
    score: int,
    risk: dict[str, Any],
    rows: list[dict[str, Any]],
    patterns: list[dict[str, Any]],
    breakouts: list[dict[str, Any]],
    levels: dict[str, float],
) -> dict[str, Any]:
    entry = rows[-1]["close"]
    true_ranges = [
        max(
            row["high"] - row["low"],
            abs(row["high"] - previous["close"]),
            abs(row["low"] - previous["close"]),
        )
        for previous, row in zip(rows[-21:-1], rows[-20:])
    ]
    atr20 = sum(true_ranges) / len(true_ranges) if true_ranges else entry * 0.025
    stop_candidates = [entry * 0.95, entry - 2.0 * atr20]
    for breakout in breakouts:
        stop = float(breakout.get("stop_loss") or 0)
        if 0 < stop < entry:
            stop_candidates.append(stop)
    stop_loss = max(0.01, max(stop_candidates))
    ordered_patterns = sorted(
        (
            pattern
            for pattern in patterns
            if pattern.get("direction") == "看涨"
            and float(pattern.get("target_price") or 0) > entry
        ),
        key=lambda pattern: {"头肩底": 0, "双底": 1, "箱体震荡": 2}.get(
            str(pattern.get("name") or ""), 9
        ),
    )
    target = (
        float(ordered_patterns[0]["target_price"])
        if ordered_patterns
        else max(entry * 1.10, float(levels.get("近期阻力") or 0))
    )
    risk_amount = max(0.0, entry - stop_loss)
    reward_amount = max(0.0, target - entry)
    ratio = reward_amount / risk_amount if risk_amount else 0.0
    if action in {"观望", "卖出"}:
        position = "空仓等待" if action == "观望" else "不新增仓位"
    elif action == "强烈买入" and risk["level"] == "低":
        position = "正常仓位"
    elif action == "买入":
        position = "半仓(1/2)"
    else:
        position = "轻仓(1/3)"
    notes = []
    for breakout in breakouts:
        if breakout.get("signal") == "持仓" and breakout.get("entry_price"):
            notes.append(
                f"{breakout['system']}持仓，系统止损{float(breakout['stop_loss']):.2f}"
            )
            break
    if action == "卖出":
        notes.append("趋势、量价或环境维度共振偏空，优先控制风险")
    return {
        "action": action,
        "entry_price": round(entry, 2),
        "stop_loss": round(stop_loss, 2),
        "target_price": round(target, 2),
        "position_size": position,
        "holding_period": "中线(1-3月)",
        "risk_reward_ratio": round(ratio, 2),
        "max_loss_pct": round(max(0.0, (entry - stop_loss) / entry * 100.0), 2),
        "notes": "；".join(notes),
        "score": score,
    }


def _aggregate_signals(
    trend: dict[str, Any],
    volume_price: dict[str, Any],
    patterns: list[dict[str, Any]],
    breakouts: list[dict[str, Any]],
    canslim: dict[str, Any],
    chanlun: dict[str, Any],
) -> dict[str, list[str]]:
    buy: list[str] = []
    sell: list[str] = []
    if trend["direction"] == "上升":
        buy.append(f"趋势上升({trend['strength']}分)")
    elif trend["direction"] == "下降":
        sell.append(f"趋势下降({trend['strength']}分)")
    for pattern in patterns:
        text = f"{pattern['name']}({pattern['status']}，{pattern['confidence']}分)"
        if pattern["direction"] == "看涨":
            buy.append(text)
        elif pattern["direction"] == "看跌":
            sell.append(text)
    if volume_price["direction"] == "看涨":
        buy.append(f"量价{volume_price['pattern']}({volume_price['confidence']}分)")
    elif volume_price["direction"] == "看跌":
        sell.append(f"量价{volume_price['pattern']}({volume_price['confidence']}分)")
    for signal in volume_price.get("signals") or []:
        if "流出" in signal or "下降" in signal:
            sell.append(signal)
        elif "流入" in signal or "上升" in signal or "突破" in signal:
            buy.append(signal)
    for breakout in breakouts:
        state = breakout.get("signal")
        if state in {"持仓", "空头平仓"}:
            buy.append(f"{breakout['system']}{state}")
        elif state in {"卖出", "持仓空头"}:
            sell.append(f"{breakout['system']}{state}")
    for signal in canslim.get("signals") or []:
        if "⚠️" in signal:
            sell.append(signal)
        else:
            buy.append(signal)
    for signal in chanlun.get("signals") or []:
        text = f"缠论{signal['type_name']}@{float(signal['price']):.2f}"
        if str(signal.get("type") or "").startswith("buy"):
            buy.append(text)
        else:
            sell.append(text)
    buy = list(dict.fromkeys(buy))
    sell = list(dict.fromkeys(sell))
    return {
        "buy": buy,
        "sell": sell,
        "list": [*(f"买:{item}" for item in buy), *(f"卖:{item}" for item in sell)],
    }


def analyze_technical(
    rows: Any,
    quote: Any = None,
    flows: Any = None,
    index_rows: Any = None,
    period: str = "day",
) -> dict[str, Any]:
    """Analyze supplied OHLCV mappings and return a JSON-safe decision.

    The function performs no I/O, does not mutate caller-owned values, and does
    not fetch missing enrichment.  All degraded assumptions are explicitly
    reported through ``data_quality``.
    """
    resolved_period = normalize_period(period)
    klines, normalization = normalize_rows(rows, minimum=MINIMUM_ROWS)
    normalized_quote = normalize_quote(quote)
    normalized_flows = normalize_flows(flows)
    normalized_index: list[dict[str, Any]] | None = None
    if index_rows is not None:
        try:
            normalized_index, _ = normalize_rows(index_rows, minimum=MINIMUM_ROWS)
        except ValueError:
            normalized_index = None

    trend = analyze_trend(klines)
    volume_price = analyze_volume_price(klines, normalized_quote, normalized_flows)
    patterns = analyze_patterns(klines)
    breakouts = analyze_breakout(klines)
    canslim = analyze_canslim(klines, normalized_quote, normalized_flows, normalized_index)
    chanlun = analyze_chanlun_daily(klines)
    scores = {
        "趋势": int(trend["strength"]),
        "形态": _pattern_score(patterns),
        "量价": _volume_score(volume_price),
        "突破": _breakout_score(breakouts),
        "CAN_SLIM": int(canslim["total"]),
        "缠论": _chanlun_score(chanlun, str(trend["direction"])),
    }
    # The five migrated strategy weights remain primary.  Chanlun is an
    # additional confirmation dimension, incorporated at 10% by proportionally
    # reducing the earlier weights.
    score = int(
        scores["趋势"] * 0.23
        + scores["CAN_SLIM"] * 0.18
        + scores["突破"] * 0.18
        + scores["量价"] * 0.18
        + scores["形态"] * 0.13
        + scores["缠论"] * 0.10
    )
    score = max(0, min(100, score))

    downtrend = trend["direction"] == "下降"
    bearish_environment = int(canslim["m_score"]) < 30
    bearish_volume = volume_price["direction"] == "看跌"
    if downtrend and (bearish_volume or bearish_environment or score < 45):
        action = "卖出"
    elif score >= 75:
        action = "强烈买入"
    elif score >= 65:
        action = "买入"
    elif score >= 55:
        action = "谨慎买入"
    else:
        action = "观望"

    qualified = sum(module_score >= 60 for module_score in scores.values())
    agreement = abs(qualified - (len(scores) - qualified)) / len(scores)
    data_coverage = (
        0.70
        + (0.10 if normalized_quote else 0.0)
        + (0.10 if len(normalized_flows) >= 3 else 0.0)
        + (0.10 if normalized_index else 0.0)
    )
    confidence = int(score * 0.55 + agreement * 25.0 + data_coverage * 20.0)
    confidence = max(0, min(100, confidence))
    risk = _risk(
        trend,
        volume_price,
        canslim,
        breakouts,
        chanlun,
        quote_available=normalized_quote is not None,
        flow_available=len(normalized_flows) >= 3,
        index_available=normalized_index is not None,
    )
    levels = _key_levels(klines, trend, patterns, breakouts, canslim, chanlun)
    plan = _trade_plan(action, score, risk, klines, patterns, breakouts, levels)
    signals = _aggregate_signals(trend, volume_price, patterns, breakouts, canslim, chanlun)
    degraded = not normalized_quote or len(normalized_flows) < 3 or normalized_index is None
    quality_warnings = list(risk["warnings"])
    if normalization["rejected_row_count"]:
        quality_warnings.append(f"已忽略{normalization['rejected_row_count']}条无效K线")
    if normalization["missing_volume_count"]:
        quality_warnings.append(f"{normalization['missing_volume_count']}条K线缺少成交量")
    data_quality = {
        "status": "degraded" if degraded or normalization["rejected_row_count"] else "ok",
        "row_count": len(klines),
        "input_row_count": normalization["input_row_count"],
        "rejected_row_count": normalization["rejected_row_count"],
        "missing_volume_count": normalization["missing_volume_count"],
        "fallback_date_count": normalization["fallback_date_count"],
        "fallback_ohlc_count": normalization["fallback_ohlc_count"],
        "chronology_reordered": normalization["chronology_reordered"],
        "quote_available": normalized_quote is not None,
        "fund_flow_available": len(normalized_flows) >= 3,
        "index_available": normalized_index is not None,
        "coverage": round(data_coverage, 2),
        "degraded": degraded or bool(normalization["rejected_row_count"]),
        "warnings": list(dict.fromkeys(quality_warnings)),
    }
    modules = {
        "trend": trend,
        "volume_price": volume_price,
        "patterns": {"items": patterns, "count": len(patterns), "score": scores["形态"]},
        "breakout": {"systems": breakouts, "score": scores["突破"]},
        "canslim": canslim,
        "chanlun": chanlun,
    }
    description = (
        f"综合{score}分 | 趋势={scores['趋势']} | 量价={scores['量价']} | "
        f"形态={scores['形态']} | 突破={scores['突破']} | "
        f"CAN SLIM={scores['CAN_SLIM']} | 缠论={scores['缠论']}"
    )
    return {
        "period": resolved_period,
        "action": action,
        "score": score,
        "confidence": confidence,
        "risk_level": risk["level"],
        "risk": risk,
        "signal_strength": "强" if score >= 75 else "中" if score >= 60 else "弱",
        "module_scores": scores,
        "modules": modules,
        # Flat aliases retain compatibility with consumers of the migrated
        # signal-engine shape while ``modules`` is the canonical contract.
        "trend": trend,
        "volume_price": volume_price,
        "patterns": patterns,
        "breakouts": breakouts,
        "canslim": canslim,
        "chanlun": chanlun,
        "signals": signals,
        "buy_signals": signals["buy"],
        "sell_signals": signals["sell"],
        "risk_warnings": risk["warnings"],
        "key_levels": levels,
        "trade_plan": plan,
        "data_requirements": {
            "required": {
                "ohlcv_rows": MINIMUM_ROWS,
                "fields": ["date", "open", "high", "low", "close", "volume"],
            },
            "optional": ["quote", "flows(>=3)", "index_rows(>=60)"],
            "periods": ["day", "week"],
        },
        "data_quality": data_quality,
        "description": description,
    }


__all__ = ["analyze_technical"]
