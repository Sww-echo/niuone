"""Composition service for single-stock analysis and bounded cache scans."""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.market_data.technical_analysis import (
    TechnicalMarketDataError,
    fetch_minute_series,
    load_technical_market_data,
)
from app.market_data.tencent_kline_cache import (
    load_cached_kline_symbols,
    load_kline_series_map,
)


MAX_SCAN_LIMIT = 5_000
DEFAULT_SCAN_LIMIT = 1_000
MAX_SCAN_WORKERS = 6


def _engine() -> Callable[..., dict[str, Any]]:
    # Deferred import keeps FastAPI startup independent from analysis internals.
    from app.strategies.technical_analysis import analyze_technical

    return analyze_technical


def analyze_symbol(
    symbol: str,
    period: str = "day",
    *,
    market_loader: Callable[..., dict[str, Any]] = load_technical_market_data,
    analyzer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the public single-stock analysis envelope."""
    market = market_loader(symbol, period=period, count=250, include_fund_flow=True)
    active_analyzer = analyzer or _engine()
    signal = active_analyzer(
        market["klines"],
        quote=market.get("quote"),
        flows=market.get("flows"),
        index_rows=None,
        period=market["period"],
    )
    signal_quality = signal.get("data_quality") if isinstance(signal, dict) else None
    if isinstance(signal_quality, dict):
        signal_quality.update(market.get("data_quality") or {})
    else:
        signal["data_quality"] = dict(market.get("data_quality") or {})
    quote = dict(market.get("quote") or {})
    return {
        "symbol": market["symbol"],
        "name": str(quote.get("name") or ""),
        "period": market["period"],
        "quote": quote,
        "klines": market["klines"][-160:],
        "signal": signal,
        "data_quality": market.get("data_quality") or {},
    }


def analyze_minute_symbol(
    symbol: str,
    *,
    market_loader: Callable[..., dict[str, Any]] = fetch_minute_series,
    analyzer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a five-minute Chanlun envelope without coupling it to HTTP."""
    market = market_loader(symbol)
    if analyzer is None:
        try:
            from app.strategies.technical_analysis import analyze_minute_technical
        except ImportError:
            analyze_minute_technical = None
        analyzer = analyze_minute_technical
    if analyzer is None:
        analysis = {
            "available": False,
            "reason": "minute_analysis_not_available",
            "summary": "分时缠论模块暂不可用",
        }
    else:
        analysis = analyzer(
            market["times"], market["prices"], market["volumes"]
        )
    return {**market, "analysis": analysis}


class TechnicalScanManager:
    """One-process, bounded technical scan manager using only cached K-lines."""

    def __init__(
        self,
        *,
        symbol_loader: Callable[..., tuple[str, ...]] = load_cached_kline_symbols,
        series_loader: Callable[..., dict[str, list[dict[str, Any]]]] = load_kline_series_map,
        analyzer: Callable[..., dict[str, Any]] | None = None,
        max_workers: int = MAX_SCAN_WORKERS,
    ) -> None:
        self._symbol_loader = symbol_loader
        self._series_loader = series_loader
        self._analyzer = analyzer
        self._max_workers = max(1, min(MAX_SCAN_WORKERS, int(max_workers or 1)))
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job_id = ""

    def start(self, *, period: str = "day", limit: int = DEFAULT_SCAN_LIMIT) -> dict[str, Any]:
        resolved_period = str(period or "day").lower()
        if resolved_period not in {"day", "week"}:
            raise ValueError("unsupported_period")
        resolved_limit = max(1, min(MAX_SCAN_LIMIT, int(limit or DEFAULT_SCAN_LIMIT)))
        with self._lock:
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id) or {}
                if active.get("status") in {"queued", "running"}:
                    return self._public(active)
            job_id = uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "status": "queued",
                "stage": "等待扫描",
                "progress": 0.0,
                "period": resolved_period,
                "limit": resolved_limit,
                "total": 0,
                "scanned": 0,
                "matched": 0,
                "results": [],
                "error": "",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "started_ts": 0.0,
                "elapsed": 0.0,
            }
            self._jobs[job_id] = job
            self._active_job_id = job_id
        threading.Thread(target=self._run, args=(job_id,), daemon=True).start()
        return self._public(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            return self._public(job) if job else None

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in job.items()
            if key not in {"started_ts"}
        }

    @staticmethod
    def _weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from app.market_data.technical_analysis import _aggregate_weekly, _augment_rows

        return _augment_rows(_aggregate_weekly(rows))

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(status="running", stage="读取 NiuOne K线缓存", started_ts=time.time())
        try:
            symbols = list(self._symbol_loader(min_rows=60))[: int(job["limit"])]
            if not symbols:
                raise TechnicalMarketDataError("kline_cache_empty")
            with self._lock:
                job["total"] = len(symbols)
                job["stage"] = "批量加载缓存"
            count = 500 if job["period"] == "week" else 250
            series = self._series_loader(symbols, min_rows=60, count=count)
            analyzer = self._analyzer or _engine()
            results: list[dict[str, Any]] = []

            def calculate(symbol: str) -> dict[str, Any] | None:
                rows = list(series.get(symbol) or [])
                if job["period"] == "week":
                    rows = self._weekly(rows)
                if len(rows) < 60:
                    return None
                signal = analyzer(rows, period=job["period"])
                action = str(signal.get("action") or "观望")
                score = int(signal.get("score") or 0)
                if action not in {"强烈买入", "买入", "谨慎买入"} and score < 60:
                    return None
                plan = signal.get("trade_plan") or {}
                return {
                    "symbol": symbol[-6:],
                    "name": "",
                    "price": float(rows[-1].get("close") or 0),
                    "period": job["period"],
                    "action": action,
                    "score": score,
                    "confidence": int(signal.get("confidence") or 0),
                    "risk_level": str(signal.get("risk_level") or ""),
                    "module_scores": signal.get("module_scores") or {},
                    "trade_plan": plan,
                }

            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                futures = {pool.submit(calculate, symbol): symbol for symbol in symbols}
                for index, future in enumerate(as_completed(futures), 1):
                    try:
                        result = future.result()
                    except (ValueError, ArithmeticError, TypeError):
                        result = None
                    if result:
                        results.append(result)
                    with self._lock:
                        job["scanned"] = index
                        job["matched"] = len(results)
                        job["progress"] = round(index / max(1, len(symbols)) * 100, 1)
                        job["stage"] = "缓存技术扫描"
            results.sort(
                key=lambda item: (int(item.get("score") or 0), int(item.get("confidence") or 0)),
                reverse=True,
            )
            with self._lock:
                job.update(
                    status="done",
                    stage="扫描完成",
                    progress=100.0,
                    results=results[:50],
                    matched=len(results),
                    elapsed=round(time.time() - float(job["started_ts"]), 2),
                )
        except Exception as exc:
            with self._lock:
                job.update(
                    status="error",
                    stage="扫描失败",
                    error=str(exc)[:200],
                    elapsed=round(time.time() - float(job.get("started_ts") or time.time()), 2),
                )
        finally:
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = ""


_SHARED_SCAN_MANAGER: TechnicalScanManager | None = None
_SHARED_SCAN_LOCK = threading.Lock()


def get_technical_scan_manager() -> TechnicalScanManager:
    global _SHARED_SCAN_MANAGER
    with _SHARED_SCAN_LOCK:
        if _SHARED_SCAN_MANAGER is None:
            _SHARED_SCAN_MANAGER = TechnicalScanManager()
        return _SHARED_SCAN_MANAGER


__all__ = [
    "TechnicalScanManager",
    "analyze_minute_symbol",
    "analyze_symbol",
    "get_technical_scan_manager",
]
