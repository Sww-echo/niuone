"""Composition service for single-stock analysis and bounded cache scans."""
from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from app.market_data.technical_analysis import (
    TechnicalMarketDataError,
    fetch_minute_series,
    load_technical_index_bundle,
    load_technical_market_data,
    technical_index_key,
)
from app.market_data.tencent_kline_cache import (
    load_cached_kline_symbols,
    load_kline_series_map,
)


MAX_SCAN_LIMIT = 5_000
DEFAULT_SCAN_LIMIT = 1_000
MAX_SCAN_WORKERS = 6
DEFAULT_SCAN_JOB_TTL_SECONDS = 60 * 60
DEFAULT_MAX_RETAINED_SCAN_JOBS = 100
STRATEGY_VERSION = "niuone-technical-v2"
SCORE_MODEL = "six-factor-enhanced"

_ACTIVE_SCAN_STATUSES = frozenset({"queued", "running"})
_TERMINAL_SCAN_STATUSES = frozenset({"done", "error", "cancelled"})


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
        index_rows=market.get("index_klines") or None,
        period=market["period"],
    )
    signal.update({
        "strategy_version": STRATEGY_VERSION,
        "score_model": SCORE_MODEL,
        "analysis_mode": "realtime",
    })
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
        "strategy_version": STRATEGY_VERSION,
        "score_model": SCORE_MODEL,
        "analysis_mode": "realtime",
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
        index_bundle_loader: Callable[..., dict[str, dict[str, Any]]] = load_technical_index_bundle,
        analyzer: Callable[..., dict[str, Any]] | None = None,
        max_workers: int = MAX_SCAN_WORKERS,
        completed_job_ttl_seconds: float = DEFAULT_SCAN_JOB_TTL_SECONDS,
        max_retained_jobs: int = DEFAULT_MAX_RETAINED_SCAN_JOBS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._symbol_loader = symbol_loader
        self._series_loader = series_loader
        self._index_bundle_loader = index_bundle_loader
        self._analyzer = analyzer
        self._max_workers = max(1, min(MAX_SCAN_WORKERS, int(max_workers or 1)))
        self._completed_job_ttl_seconds = max(0.0, float(completed_job_ttl_seconds))
        self._max_retained_jobs = max(1, int(max_retained_jobs))
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job_id = ""

    def start(self, *, period: str = "day", limit: int = DEFAULT_SCAN_LIMIT) -> dict[str, Any]:
        resolved_period = str(period or "day").lower()
        if resolved_period not in {"day", "week"}:
            raise ValueError("unsupported_period")
        resolved_limit = max(1, min(MAX_SCAN_LIMIT, int(limit or DEFAULT_SCAN_LIMIT)))
        with self._lock:
            self._cleanup_locked()
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id) or {}
                if active.get("status") in {*_ACTIVE_SCAN_STATUSES, "cancelled"}:
                    return self._public(active)
                self._active_job_id = ""
            self._cleanup_locked(reserve=1)
            job_id = uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "status": "queued",
                "stage": "等待扫描",
                "progress": 0.0,
                "period": resolved_period,
                "strategy_version": STRATEGY_VERSION,
                "score_model": SCORE_MODEL,
                "analysis_mode": "eod_scan",
                "data_source": "niuone_sqlite_cache",
                "price_status": "closed",
                "limit": resolved_limit,
                "total": 0,
                "scanned": 0,
                "matched": 0,
                "results": [],
                "error": "",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "started_ts": None,
                "finished_ts": None,
                "cancel_event": threading.Event(),
                "elapsed": 0.0,
            }
            self._jobs[job_id] = job
            self._active_job_id = job_id
            initial = self._public(job)
        try:
            threading.Thread(target=self._run, args=(job_id,), daemon=True).start()
        except RuntimeError as exc:
            with self._lock:
                if self._cancel_requested(job):
                    self._mark_cancelled_locked(job)
                else:
                    now = self._clock()
                    job.update(
                        status="error",
                        stage="扫描失败",
                        error=str(exc)[:200],
                        finished_ts=now,
                        elapsed=0.0,
                    )
                if self._active_job_id == job_id:
                    self._active_job_id = ""
                return self._public(job)
        return initial

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_locked()
            job = self._jobs.get(str(job_id or ""))
            return self._public(job) if job else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """Request cancellation and publish a terminal state immediately."""
        with self._lock:
            self._cleanup_locked()
            resolved_job_id = str(job_id or "")
            job = self._jobs.get(resolved_job_id)
            if job is None:
                return None
            if job.get("status") in _ACTIVE_SCAN_STATUSES:
                was_queued = job.get("status") == "queued"
                cancel_event = job.get("cancel_event")
                if isinstance(cancel_event, threading.Event):
                    cancel_event.set()
                self._mark_cancelled_locked(job)
                if was_queued and self._active_job_id == resolved_job_id:
                    self._active_job_id = ""
            return self._public(job)

    def _cleanup_locked(self, *, reserve: int = 0) -> None:
        now = self._clock()
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job_id != self._active_job_id
            and job.get("status") in _TERMINAL_SCAN_STATUSES
            and now - self._timestamp(job, "finished_ts", now)
            >= self._completed_job_ttl_seconds
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)

        retained_limit = max(0, self._max_retained_jobs - max(0, reserve))
        overflow = len(self._jobs) - retained_limit
        if overflow <= 0:
            return
        completed = sorted(
            (
                (self._timestamp(job, "finished_ts", 0.0), job_id)
                for job_id, job in self._jobs.items()
                if job_id != self._active_job_id
                and job.get("status") in _TERMINAL_SCAN_STATUSES
            ),
            key=lambda item: (item[0], item[1]),
        )
        for _, job_id in completed[:overflow]:
            self._jobs.pop(job_id, None)

    def _mark_cancelled_locked(self, job: dict[str, Any]) -> None:
        now = self._clock()
        started_ts = self._timestamp(job, "started_ts", now)
        job.update(
            status="cancelled",
            stage="扫描已取消",
            finished_ts=now,
            elapsed=round(max(0.0, now - started_ts), 2),
        )

    @staticmethod
    def _cancel_requested(job: dict[str, Any]) -> bool:
        cancel_event = job.get("cancel_event")
        return isinstance(cancel_event, threading.Event) and cancel_event.is_set()

    @staticmethod
    def _timestamp(job: dict[str, Any], key: str, fallback: float) -> float:
        value = job.get(key)
        return fallback if value is None else float(value)

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy({
            key: value
            for key, value in job.items()
            if key not in {"started_ts", "finished_ts", "cancel_event"}
        })

    @staticmethod
    def _weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from app.market_data.technical_analysis import _aggregate_weekly, _augment_rows

        return _augment_rows(_aggregate_weekly(rows))

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if self._cancel_requested(job):
                if self._active_job_id == job_id:
                    self._active_job_id = ""
                return
            job.update(
                status="running",
                stage="读取 NiuOne K线缓存",
                started_ts=self._clock(),
            )
        try:
            symbols = list(self._symbol_loader(min_rows=60))[: int(job["limit"])]
            if not symbols:
                raise TechnicalMarketDataError("kline_cache_empty")
            with self._lock:
                if self._cancel_requested(job):
                    self._mark_cancelled_locked(job)
                    return
                job["total"] = len(symbols)
                job["stage"] = "批量加载缓存"
            count = 500 if job["period"] == "week" else 250
            series = self._series_loader(symbols, min_rows=60, count=count)
            if self._cancel_requested(job):
                with self._lock:
                    self._mark_cancelled_locked(job)
                return
            try:
                index_bundle = self._index_bundle_loader(
                    symbols, period=job["period"], count=count,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                index_bundle = {}
            if self._cancel_requested(job):
                with self._lock:
                    self._mark_cancelled_locked(job)
                return
            analyzer = self._analyzer or _engine()
            results: list[dict[str, Any]] = []

            def calculate(symbol: str) -> dict[str, Any] | None:
                if self._cancel_requested(job):
                    return None
                rows = list(series.get(symbol) or [])
                if job["period"] == "week":
                    rows = self._weekly(rows)
                if len(rows) < 60:
                    return None
                try:
                    index_market = index_bundle.get(technical_index_key(symbol)) or {}
                except ValueError:
                    index_market = {}
                index_rows = list(index_market.get("klines") or [])
                signal = analyzer(
                    rows,
                    index_rows=index_rows or None,
                    period=job["period"],
                )
                if self._cancel_requested(job):
                    return None
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
                    "strategy_version": STRATEGY_VERSION,
                    "score_model": SCORE_MODEL,
                    "analysis_mode": "eod_scan",
                    "data_source": "niuone_sqlite_cache",
                    "price_status": "closed",
                    "as_of": str(rows[-1].get("date") or "")[:10],
                    "index_available": len(index_rows) >= 60,
                    "action": action,
                    "score": score,
                    "confidence": int(signal.get("confidence") or 0),
                    "risk_level": str(signal.get("risk_level") or ""),
                    "module_scores": signal.get("module_scores") or {},
                    "trade_plan": plan,
                }

            pool = ThreadPoolExecutor(max_workers=self._max_workers)
            pending = set()
            try:
                for symbol in symbols:
                    pending.add(pool.submit(calculate, symbol))
                scanned = 0
                while pending:
                    if self._cancel_requested(job):
                        for future in pending:
                            future.cancel()
                        with self._lock:
                            self._mark_cancelled_locked(job)
                        return
                    completed, pending = wait(
                        pending,
                        timeout=0.1,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in completed:
                        scanned += 1
                        try:
                            result = future.result()
                        except (ValueError, ArithmeticError, TypeError):
                            result = None
                        if result:
                            results.append(result)
                    if completed:
                        with self._lock:
                            if self._cancel_requested(job):
                                self._mark_cancelled_locked(job)
                                return
                            job["scanned"] = scanned
                            job["matched"] = len(results)
                            job["progress"] = round(
                                scanned / max(1, len(symbols)) * 100, 1,
                            )
                            job["stage"] = "缓存技术扫描"
            finally:
                # A cancelled scan remains the active job until already-running
                # calculations exit, preventing overlapping CPU-heavy scans.
                pool.shutdown(wait=True, cancel_futures=True)
            results.sort(
                key=lambda item: (int(item.get("score") or 0), int(item.get("confidence") or 0)),
                reverse=True,
            )
            with self._lock:
                if self._cancel_requested(job):
                    self._mark_cancelled_locked(job)
                    return
                now = self._clock()
                job.update(
                    status="done",
                    stage="扫描完成",
                    progress=100.0,
                    results=results[:50],
                    matched=len(results),
                    finished_ts=now,
                    elapsed=round(
                        now - self._timestamp(job, "started_ts", now), 2,
                    ),
                )
        except Exception as exc:
            with self._lock:
                if self._cancel_requested(job):
                    self._mark_cancelled_locked(job)
                else:
                    now = self._clock()
                    job.update(
                        status="error",
                        stage="扫描失败",
                        error=str(exc)[:200],
                        finished_ts=now,
                        elapsed=round(
                            now - self._timestamp(job, "started_ts", now), 2,
                        ),
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
