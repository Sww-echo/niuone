"""Market-data adapter for the technical-analysis dashboard feature.

The adapter keeps network access and durable caches outside the strategy
package.  A single-stock request prefers NiuOne's SQLite K-line cache, uses a
bounded Tencent fallback when needed, and enriches the last bar with a live
quote.  Optional Eastmoney fund-flow data fails closed without making the
technical calculation unavailable.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.market_data.tencent_kline_cache import (
    fetch_tencent_daily_klines,
    load_kline_series_map,
    merge_live_quote,
)
EASTMONEY_FLOW_URL = "https://push2delay.eastmoney.com/api/qt/stock/fflow/daykline/get"
EASTMONEY_MINUTE_URL = "https://push2delay.eastmoney.com/api/qt/stock/trends2/get"
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SUPPORTED_PERIODS = {"day", "week"}


class TechnicalMarketDataError(RuntimeError):
    """Stable public error raised when required market data is unavailable."""


def _fetch_tencent_quote(
    symbols: list[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Fetch a bounded quote batch without importing the screening layer."""
    normalized = [
        str(symbol or "").lower()
        for symbol in symbols[:20]
        if re.fullmatch(r"(?:sh|sz|bj)\d{6}", str(symbol or "").lower())
    ]
    if not normalized:
        return {}
    request = Request(
        TENCENT_QUOTE_URL + ",".join(normalized),
        headers={"User-Agent": "Mozilla/5.0 NiuOne/1.0", "Connection": "close"},
    )
    with urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("technical quote response too large")
    text = body.decode("gb18030", errors="ignore")
    result: dict[str, dict[str, Any]] = {}
    for match in re.finditer(r'v_([^=]+)="([^"]*)"', text):
        symbol_key = str(match.group(1) or "").lower()
        fields = match.group(2).split("~")
        if len(fields) < 39:
            continue
        price = _finite(fields[3])
        previous = _finite(fields[4])
        if price is None or price <= 0:
            continue
        amount_wan = _finite(fields[37]) or 0.0
        result[symbol_key] = {
            "name": str(fields[1] or ""),
            "price": price,
            "prev_close": previous or 0.0,
            "open": _finite(fields[5]) or price,
            "change_pct": _finite(fields[32]) or (
                (price / previous - 1) * 100 if previous else 0.0
            ),
            "amount": amount_wan * 10_000,
            "volume": _finite(fields[6]) or 0.0,
            "high": _finite(fields[33]) or price,
            "low": _finite(fields[34]) or price,
            "turnover": _finite(fields[38]) or 0.0,
            "quote_time": str(fields[30] or ""),
        }
    return result


def normalize_symbol(value: object) -> dict[str, str]:
    """Normalize an A-share code to plain, Tencent and Eastmoney forms."""
    text = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).lower()
    if re.fullmatch(r"(?:sh|sz|bj)\d{6}", text):
        market, code = text[:2], text[2:]
    elif re.fullmatch(r"\d{6}", text):
        code = text
        if code.startswith(("4", "8", "92")):
            market = "bj"
        elif code.startswith(("5", "6", "9")):
            market = "sh"
        else:
            market = "sz"
    else:
        raise ValueError("invalid_symbol")
    secid = f"1.{code}" if market == "sh" else f"0.{code}"
    return {"code": code, "tencent": f"{market}{code}", "secid": secid, "market": market}


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _download_json(url: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 NiuOne/1.0",
            "Referer": "https://quote.eastmoney.com/",
            "Connection": "close",
        },
    )
    with urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("technical market response too large")
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("technical market response invalid")
    return payload


def _fetch_eastmoney_daily_klines(
    normalized: dict[str, str],
    count: int,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Bounded daily fallback for Beijing-listed symbols and Tencent outages."""
    query = urlencode({
        "secid": normalized["secid"], "klt": "101", "fqt": "1",
        "beg": "0", "end": "20500101", "lmt": str(max(60, min(500, count))),
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    })
    try:
        payload = _download_json(f"{EASTMONEY_KLINE_URL}?{query}", timeout_seconds)
        lines = ((payload.get("data") or {}).get("klines") or [])
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        fields = str(line or "").split(",")
        if len(fields) < 6:
            continue
        values = [_finite(value) for value in fields[1:6]]
        if any(value is None for value in values):
            continue
        rows.append({
            "date": fields[0], "open": values[0], "close": values[1],
            "high": values[2], "low": values[3], "volume": values[4],
            "amount": _finite(fields[6]) if len(fields) > 6 else 0.0,
        })
    return rows[-max(1, min(500, int(count or 250))):]


def fetch_fund_flows(
    symbol: object,
    *,
    days: int = 30,
    downloader: Callable[[str, float], dict[str, Any]] = _download_json,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Fetch a bounded daily main-flow series; an unavailable source returns []."""
    normalized = normalize_symbol(symbol)
    query = urlencode({
        "lmt": str(max(3, min(120, int(days or 30)))),
        "klt": "101",
        "secid": normalized["secid"],
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    })
    try:
        payload = downloader(f"{EASTMONEY_FLOW_URL}?{query}", timeout_seconds)
        lines = ((payload.get("data") or {}).get("klines") or [])
    except Exception:
        return []
    result: list[dict[str, Any]] = []
    for line in lines:
        fields = str(line or "").split(",")
        if len(fields) < 7:
            continue
        values = [_finite(value) for value in fields[1:7]]
        if any(value is None for value in values[:5]):
            continue
        result.append({
            "date": fields[0],
            "main_net": values[0],
            "small_net": values[1],
            "medium_net": values[2],
            "large_net": values[3],
            "super_large_net": values[4],
            "main_pct": values[5] or 0.0,
        })
    return result


def fetch_minute_series(
    symbol: object,
    *,
    downloader: Callable[[str, float], dict[str, Any]] = _download_json,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch today's bounded five-minute series for minute Chanlun analysis."""
    normalized = normalize_symbol(symbol)
    query = urlencode({
        "secid": normalized["secid"],
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "isccr": "1",
        "ndays": "1",
        "iscca": "0",
        "klt": "5",
        "fqt": "1",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    })
    try:
        payload = downloader(f"{EASTMONEY_MINUTE_URL}?{query}", timeout_seconds)
        data = payload.get("data") or {}
        trends = data.get("trends") or []
    except Exception as exc:
        raise TechnicalMarketDataError("minute_unavailable") from exc
    times: list[str] = []
    prices: list[float] = []
    averages: list[float] = []
    volumes: list[float] = []
    for line in trends[:600]:
        fields = str(line or "").split(",")
        if len(fields) < 8:
            continue
        price = _finite(fields[2])
        if price is None or price <= 0:
            continue
        times.append(fields[0].split(" ")[-1][:5])
        prices.append(price)
        averages.append(_finite(fields[7]) or price)
        volumes.append(max(0.0, _finite(fields[5]) or 0.0))
    if len(prices) < 10:
        raise TechnicalMarketDataError("minute_insufficient")
    return {
        "symbol": normalized["code"],
        "name": str(data.get("name") or ""),
        "pre_close": _finite(data.get("preClose")) or 0.0,
        "times": times,
        "prices": prices,
        "avg_prices": averages,
        "volumes": volumes,
        "high": max(prices),
        "low": min(prices),
        "data_quality": {
            "source": "eastmoney_five_minute",
            "point_count": len(prices),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _aggregate_weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate ordered daily OHLCV rows into Monday-based weekly bars."""
    from datetime import date

    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for row in rows:
        try:
            iso = date.fromisoformat(str(row.get("date") or "")[:10]).isocalendar()
        except ValueError:
            continue
        groups.setdefault((iso.year, iso.week), []).append(row)
    result = []
    for key in sorted(groups):
        week = groups[key]
        first, last = week[0], week[-1]
        result.append({
            "date": str(last["date"])[:10],
            "open": float(first["open"]),
            "close": float(last["close"]),
            "high": max(float(item["high"]) for item in week),
            "low": min(float(item["low"]) for item in week),
            "volume": sum(float(item.get("volume") or 0) for item in week),
            "amount": sum(float(item.get("amount") or 0) for item in week),
            "turnover": float(last.get("turnover") or 0),
            "bar_status": last.get("bar_status", "closed"),
        })
    return result


def _augment_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    augmented: list[dict[str, Any]] = []
    previous = None
    for raw in rows:
        row = dict(raw)
        close = _finite(row.get("close")) or 0.0
        row.setdefault("amount", 0.0)
        row.setdefault("turnover", 0.0)
        row["pct"] = round((close / previous - 1) * 100, 2) if previous else 0.0
        previous = close or previous
        augmented.append(row)
    return augmented


def load_technical_market_data(
    symbol: object,
    *,
    period: str = "day",
    count: int = 250,
    include_fund_flow: bool = True,
    quote_fetcher: Callable[..., dict[str, dict[str, Any]]] = _fetch_tencent_quote,
    flow_fetcher: Callable[..., list[dict[str, Any]]] = fetch_fund_flows,
) -> dict[str, Any]:
    """Load one normalized technical-analysis input envelope."""
    normalized = normalize_symbol(symbol)
    resolved_period = str(period or "day").lower()
    if resolved_period not in SUPPORTED_PERIODS:
        raise ValueError("unsupported_period")
    daily_count = max(120, min(500, int(count or 250)))
    if resolved_period == "week":
        daily_count = 500

    quote: dict[str, Any] = {}
    try:
        quote = dict(quote_fetcher([normalized["tencent"]]).get(normalized["tencent"]) or {})
    except (RuntimeError, OSError):
        quote = {}

    cache = load_kline_series_map(
        [normalized["tencent"]], min_rows=60, count=daily_count,
    )
    rows = list(cache.get(normalized["tencent"]) or [])
    kline_source = "niuone_sqlite_cache" if rows else "tencent_bounded_fallback"
    if not rows:
        rows = fetch_tencent_daily_klines(normalized["tencent"], daily_count)
    if not rows:
        rows = _fetch_eastmoney_daily_klines(normalized, daily_count)
        if rows:
            kline_source = "eastmoney_bounded_fallback"
    if not rows:
        raise TechnicalMarketDataError("kline_unavailable")
    rows = merge_live_quote(rows, quote, limit=daily_count)
    if resolved_period == "week":
        rows = _aggregate_weekly(rows)
    rows = _augment_rows(rows)
    if len(rows) < 60:
        raise TechnicalMarketDataError("kline_insufficient")

    flows = flow_fetcher(normalized["code"], days=30) if include_fund_flow else []
    return {
        "symbol": normalized["code"],
        "tencent_symbol": normalized["tencent"],
        "period": resolved_period,
        "quote": quote,
        "klines": rows,
        "flows": flows,
        "data_quality": {
            "kline_source": kline_source,
            "kline_count": len(rows),
            "last_kline_date": str(rows[-1].get("date") or "")[:10],
            "latest_bar_status": str(rows[-1].get("bar_status") or "closed"),
            "quote_available": bool(quote.get("price")),
            "fund_flow_available": len(flows) >= 3,
            "degraded": not quote or len(flows) < 3,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


__all__ = [
    "TechnicalMarketDataError",
    "fetch_fund_flows",
    "fetch_minute_series",
    "load_technical_market_data",
    "normalize_symbol",
]
