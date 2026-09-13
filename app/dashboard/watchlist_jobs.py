"""Single-worker watchlist jobs with durable progress and explicit retry.

The Dashboard runs one process. Network work stays off the request thread;
each worker opens its own SQLite connections and never shares them with routes.
"""
from __future__ import annotations

import threading
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from app.storage import watchlist_tracker_store as store
from app.trading import watchlist_tracker_service as tracker

ACTIVE_STATUSES = {"queued", "running"}


def _retry_codes(job: dict[str, Any]) -> list[str]:
    failed = {item["code"] for item in job["failed"]}
    processed = set(job["processed_codes"])
    return [code for code in job["codes"] if code in failed or code not in processed]


def public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    fields = (
        "id", "kind", "status", "total", "processed", "succeeded", "skipped",
        "failed", "current_code", "quotes", "created_at", "updated_at", "finished_at", "error",
    )
    started = datetime.fromisoformat(job["created_at"])
    ended = datetime.fromisoformat(job["finished_at"]) if job["finished_at"] else tracker.now_shanghai()
    return {
        **{field: job[field] for field in fields},
        "failed": [dict(item) for item in job["failed"]],
        "elapsed_seconds": max(0, int((ended - started).total_seconds())),
        "retry_count": len(_retry_codes(job)) if job["status"] not in ACTIVE_STATUSES else 0,
    }


class WatchlistJobManager:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recovered = False

    def _ensure_recovered(self) -> None:
        if self._recovered:
            return
        # A second router in the same process must not interrupt a live worker.
        with tracker._JOB_LOCK:
            if tracker._JOB_RUNNING is not None:
                return
            with closing(store.connect(self.db_path)) as con:
                for job in store.unfinished_jobs(con):
                    job.update(status="interrupted", current_code="",
                               error="服务已重启，失败及未完成股票可重试",
                               updated_at=tracker.now_shanghai().isoformat(timespec="seconds"))
                    job["finished_at"] = job["updated_at"]
                    store.save_job(con, job)
            self._recovered = True

    def get(self, job_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_recovered()
            with closing(store.connect(self.db_path)) as con:
                job = store.get_job(con, job_id)
            if job_id is not None and job is None:
                raise KeyError("任务不存在或已清理")
            return public_job(job)

    def start(self, kind: str, *, codes: list[str] | None = None, **params: Any) -> dict[str, Any]:
        if kind not in {"backfill", "update-today"}:
            raise ValueError("不支持的自选股任务")
        if codes is not None and (not isinstance(codes, list) or not codes or any(
            not isinstance(code, str) or not tracker.CODE_RE.fullmatch(code) for code in codes
        )):
            raise ValueError("股票代码须为非空的六位代码列表")
        if kind == "backfill":
            params = {"days": tracker.validate_backfill_days(params.get("days", 60)),
                      "missing_only": params.get("missing_only", True)}
            if not isinstance(params["missing_only"], bool):
                raise ValueError("只补缺失日须为布尔值")
        else:
            params = {"force": params.get("force", False)}
            if not isinstance(params["force"], bool):
                raise ValueError("强制更新须为布尔值")
        with self._lock:
            self._ensure_recovered()
            if self._stop.is_set():
                raise tracker.JobConflictError("服务正在停止，请稍后重试")
            tracker._acquire_job(kind)
            try:
                with closing(store.connect(self.db_path)) as con:
                    active = {row["code"] for row in store.list_stocks(con)}
                    selected = sorted(active) if codes is None else list(dict.fromkeys(codes))
                    selected = [code for code in selected if code in active]
                    if not selected:
                        raise ValueError("没有可处理的自选股")
                    timestamp = tracker.now_shanghai().isoformat(timespec="seconds")
                    job = {
                        "id": uuid.uuid4().hex, "kind": kind, "params": params,
                        "codes": selected, "processed_codes": [], "status": "queued",
                        "total": len(selected), "processed": 0, "succeeded": 0,
                        "skipped": 0, "failed": [], "quotes": 0, "current_code": "",
                        "created_at": timestamp, "updated_at": timestamp,
                        "finished_at": "", "error": "",
                    }
                    store.save_job(con, job)
                self._thread = threading.Thread(
                    target=self._run, args=(job,), name="watchlist-quotes", daemon=True,
                )
                self._thread.start()
                return public_job(job)
            except Exception:
                try:
                    if "job" in locals():
                        self._save(job, status="failed", error="任务启动失败，可重试",
                                   finished_at=tracker.now_shanghai().isoformat(timespec="seconds"))
                finally:
                    tracker._release_job()
                raise

    def retry(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_recovered()
            with closing(store.connect(self.db_path)) as con:
                job = store.get_job(con, job_id)
            if job is None:
                raise KeyError("任务不存在或已清理")
            if job["status"] in ACTIVE_STATUSES:
                raise tracker.JobConflictError("任务仍在进行中")
            codes = _retry_codes(job)
            if not codes:
                raise ValueError("没有需要重试的股票")
            return self.start(job["kind"], codes=codes, **job["params"])

    def _save(self, job: dict[str, Any], **fields: Any) -> None:
        job.update(fields, updated_at=tracker.now_shanghai().isoformat(timespec="seconds"))
        with closing(store.connect(self.db_path)) as con:
            store.save_job(con, job)

    def _progress(self, job: dict[str, Any], event: dict[str, Any]) -> None:
        with self._lock:
            code, status = event["code"], event["status"]
            if status == "running":
                self._save(job, current_code=code)
                return
            if code in job["processed_codes"]:
                return
            job["processed_codes"].append(code)
            job["processed"] += 1
            if status == "failed":
                job["failed"].append({"code": code, "error": event["error"]})
            else:
                job[status] += 1
            job["quotes"] += int(event.get("quotes") or 0)
            self._save(job, current_code="")

    def _run(self, job: dict[str, Any]) -> None:
        try:
            with self._lock:
                self._save(job, status="running")
            runner = tracker.backfill_quotes if job["kind"] == "backfill" else tracker.update_today_quotes
            runner(
                codes=job["codes"], db_path=self.db_path, **job["params"],
                progress=lambda event: self._progress(job, event),
                should_stop=self._stop.is_set, include_board=False, _job_reserved=True,
            )
            with self._lock:
                # Stocks removed or deactivated after submission need no network work.
                for code in job["codes"]:
                    if code not in job["processed_codes"]:
                        self._progress(job, {"code": code, "status": "skipped"})
                status = "completed"
                if job["failed"]:
                    status = "partial" if job["succeeded"] or job["skipped"] else "failed"
                self._save(job, status=status, current_code="",
                           finished_at=tracker.now_shanghai().isoformat(timespec="seconds"))
        except Exception as exc:
            with self._lock:
                self._save(job, status="interrupted" if isinstance(exc, tracker.JobInterruptedError) else "failed",
                           error=str(exc) if isinstance(exc, tracker.JobInterruptedError) else tracker.quote_error(exc),
                           current_code="", finished_at=tracker.now_shanghai().isoformat(timespec="seconds"))
        finally:
            tracker._release_job()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1)
