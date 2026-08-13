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
import sqlite3
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
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


INDEX_REFERENCES = {
    "sh": {
        "code": "000001", "tencent": "sh000001", "secid": "1.000001",
        "market": "sh", "name": "上证指数",
    },
    "sz": {
        "code": "399001", "tencent": "sz399001", "secid": "0.399001",
        "market": "sz", "name": "深证成指",
    },
    "cyb": {
        "code": "399006", "tencent": "sz399006", "secid": "0.399006",
        "market": "sz", "name": "创业板指",
    },
    "kcb": {
        "code": "000688", "tencent": "sh000688", "secid": "1.000688",
        "market": "sh", "name": "科创50",
    },
    "bj": {
        "code": "899050", "tencent": "bj899050", "secid": "0.899050",
        "market": "bj", "name": "北证50",
    },
}


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


def _index_reference(symbol: object) -> dict[str, str]:
    """Select a same-board market benchmark for one A-share symbol."""
    normalized = normalize_symbol(symbol)
    code = normalized["code"]
    if normalized["market"] == "bj":
        key = "bj"
    elif code.startswith(("300", "301")):
        key = "cyb"
    elif code.startswith(("688", "689")):
        key = "kcb"
    else:
        key = normalized["market"]
    return dict(INDEX_REFERENCES[key])


def technical_index_key(symbol: object) -> str:
    """Return the stable Tencent-style key for a symbol's benchmark."""
    return _index_reference(symbol)["tencent"]


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
    """Fetch today's one-minute representative-price stream for 5m analysis.

    Eastmoney's ``trends2`` endpoint currently returns minute-by-minute points
    even when passed ``klt=5``.  The strategy layer therefore owns the explicit
    continuous-session five-minute aggregation.
    """
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
    source_invalid_row_count = 0
    source_invalid_price_count = 0
    source_missing_volume_count = 0
    for line in trends[:600]:
        fields = str(line or "").split(",")
        if len(fields) < 8:
            source_invalid_row_count += 1
            continue
        price = _finite(fields[2])
        if price is None or price <= 0:
            source_invalid_price_count += 1
            continue
        volume = _finite(fields[5])
        if volume is None or volume < 0:
            source_missing_volume_count += 1
            volume = 0.0
        times.append(fields[0].split(" ")[-1][:5])
        prices.append(price)
        averages.append(_finite(fields[7]) or price)
        volumes.append(volume)
    if not prices:
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
            "source": "eastmoney_one_minute_points",
            "source_interval": "1m",
            "requested_klt": 5,
            "aggregation_required": True,
            "point_count": len(prices),
            "source_row_count": min(600, len(trends)),
            "source_invalid_row_count": source_invalid_row_count,
            "source_invalid_price_count": source_invalid_price_count,
            "source_missing_volume_count": source_missing_volume_count,
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


def load_technical_index_data(
    symbol: object = "600000",
    *,
    period: str = "day",
    count: int = 120,
    series_loader: Callable[..., dict[str, list[dict[str, Any]]]] = load_kline_series_map,
    tencent_fetcher: Callable[..., list[dict[str, Any]]] = fetch_tencent_daily_klines,
    eastmoney_fetcher: Callable[..., list[dict[str, Any]]] = _fetch_eastmoney_daily_klines,
) -> dict[str, Any]:
    """Load a benchmark series for CAN SLIM's market-environment dimension.

    An unavailable benchmark is an explicit degraded result rather than a hard
    failure: stock K-lines remain sufficient for the rest of the analysis.
    """
    resolved_period = str(period or "day").lower()
    if resolved_period not in SUPPORTED_PERIODS:
        raise ValueError("unsupported_period")
    reference = _index_reference(symbol)
    daily_count = 500 if resolved_period == "week" else max(120, min(500, int(count or 120)))
    rows: list[dict[str, Any]] = []
    source = ""
    if reference["tencent"].startswith(("sh", "sz")):
        try:
            cached = series_loader(
                [reference["tencent"]], min_rows=60, count=daily_count,
            )
            rows = list(cached.get(reference["tencent"]) or [])
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            rows = []
        if rows:
            source = "niuone_sqlite_cache"
        if len(rows) < daily_count:
            try:
                extended = list(tencent_fetcher(reference["tencent"], daily_count) or [])
            except (OSError, RuntimeError, TypeError, ValueError):
                extended = []
            if len(extended) > len(rows):
                rows = extended
                source = "tencent_bounded_fallback"
    if len(rows) < daily_count:
        try:
            extended = list(eastmoney_fetcher(reference, daily_count) or [])
        except (OSError, RuntimeError, TypeError, ValueError):
            extended = []
        if len(extended) > len(rows):
            rows = extended
            source = "eastmoney_bounded_fallback"
    if resolved_period == "week" and rows:
        rows = _aggregate_weekly(rows)
    rows = _augment_rows(rows)
    if len(rows) < 60:
        rows = []
    return {
        "symbol": reference["code"],
        "tencent_symbol": reference["tencent"],
        "name": reference["name"],
        "period": resolved_period,
        "klines": rows,
        "data_quality": {
            "index_available": bool(rows),
            "index_source": source,
            "index_symbol": reference["code"],
            "index_name": reference["name"],
            "index_count": len(rows),
            "index_last_date": str(rows[-1].get("date") or "")[:10] if rows else "",
        },
    }


def load_technical_index_bundle(
    symbols: list[str] | tuple[str, ...],
    *,
    period: str = "day",
    count: int = 120,
    index_loader: Callable[..., dict[str, Any]] = load_technical_index_data,
) -> dict[str, dict[str, Any]]:
    """Load each required board benchmark once for a multi-stock scan."""
    representatives: dict[str, str] = {}
    for symbol in symbols:
        try:
            representatives.setdefault(technical_index_key(symbol), symbol)
        except ValueError:
            continue
    if not representatives:
        return {}
    results: dict[str, dict[str, Any]] = {}
    worker_count = min(5, len(representatives))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(index_loader, symbol, period=period, count=count): key
            for key, symbol in representatives.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = dict(future.result() or {})
            except (OSError, RuntimeError, TypeError, ValueError):
                results[key] = {}
    return results


def load_technical_market_data(
    symbol: object,
    *,
    period: str = "day",
    count: int = 250,
    include_fund_flow: bool = True,
    quote_fetcher: Callable[..., dict[str, dict[str, Any]]] = _fetch_tencent_quote,
    flow_fetcher: Callable[..., list[dict[str, Any]]] = fetch_fund_flows,
    index_loader: Callable[..., dict[str, Any]] = load_technical_index_data,
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

    try:
        cache = load_kline_series_map(
            [normalized["tencent"]], min_rows=60, count=daily_count,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        cache = {}
    rows = list(cache.get(normalized["tencent"]) or [])
    kline_source = "niuone_sqlite_cache" if rows else "tencent_bounded_fallback"
    # Deployments upgraded from the original 120-row cache need an upstream
    # extension before weekly aggregation and long-horizon CAN SLIM scoring.
    if len(rows) < daily_count:
        try:
            extended = fetch_tencent_daily_klines(normalized["tencent"], daily_count)
        except (OSError, RuntimeError, TypeError, ValueError):
            extended = []
        if len(extended) > len(rows):
            rows = extended
            kline_source = "tencent_bounded_fallback"
    if len(rows) < daily_count:
        try:
            extended = _fetch_eastmoney_daily_klines(normalized, daily_count)
        except (OSError, RuntimeError, TypeError, ValueError):
            extended = []
        if len(extended) > len(rows):
            rows = extended
            kline_source = "eastmoney_bounded_fallback"
    if not rows:
        raise TechnicalMarketDataError("kline_unavailable")
    rows = merge_live_quote(rows, quote, limit=daily_count)
    if resolved_period == "week":
        rows = _aggregate_weekly(rows)
    rows = _augment_rows(rows)
    if len(rows) < 60:
        raise TechnicalMarketDataError("kline_insufficient")

    if include_fund_flow:
        try:
            flows = list(flow_fetcher(normalized["code"], days=30) or [])
        except (OSError, RuntimeError, TypeError, ValueError):
            flows = []
    else:
        flows = []
    try:
        index_market = dict(index_loader(
            normalized["code"], period=resolved_period, count=daily_count,
        ) or {})
    except (OSError, RuntimeError, TypeError, ValueError):
        index_market = {}
    index_rows = list(index_market.get("klines") or [])
    index_quality = dict(index_market.get("data_quality") or {})
    degraded = not quote or len(flows) < 3 or len(index_rows) < 60
    return {
        "symbol": normalized["code"],
        "tencent_symbol": normalized["tencent"],
        "period": resolved_period,
        "quote": quote,
        "klines": rows,
        "flows": flows,
        "index_klines": index_rows,
        "data_quality": {
            "kline_source": kline_source,
            "kline_count": len(rows),
            "last_kline_date": str(rows[-1].get("date") or "")[:10],
            "latest_bar_status": str(rows[-1].get("bar_status") or "closed"),
            "quote_available": bool(quote.get("price")),
            "fund_flow_available": len(flows) >= 3,
            **index_quality,
            "index_available": len(index_rows) >= 60,
            "degraded": degraded,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


__all__ = [
    "TechnicalMarketDataError",
    "fetch_fund_flows",
    "fetch_minute_series",
    "load_technical_index_bundle",
    "load_technical_index_data",
    "load_technical_market_data",
    "normalize_symbol",
    "technical_index_key",
]
