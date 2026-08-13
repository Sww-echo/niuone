from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import Future
from typing import Any
from unittest import mock

from app.dashboard.technical_analysis_service import TechnicalScanManager

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # Manager tests intentionally run without FastAPI.
    FastAPI = None
    Request = Any
    JSONResponse = None
    Response = Any
    TestClient = None


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "date": f"2026-01-{index + 1:02d}",
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0,
            "volume": 1000.0,
        }
        for index in range(60)
    ]


def _signal(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "action": "买入",
        "score": 70,
        "confidence": 65,
        "risk_level": "中",
        "module_scores": {},
        "trade_plan": {},
    }


def _wait_for_status(
    manager: TechnicalScanManager,
    job_id: str,
    statuses: set[str],
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job is not None and job.get("status") in statuses:
            return job
        time.sleep(0.01)
    raise AssertionError(f"scan {job_id} did not reach {sorted(statuses)}")


class MutableClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class TechnicalScanLifecycleTests(unittest.TestCase):
    def _manager(self, **overrides: Any) -> TechnicalScanManager:
        rows = _rows()
        options: dict[str, Any] = {
            "symbol_loader": lambda **_kwargs: ("sh600000",),
            "series_loader": lambda symbols, **_kwargs: {
                symbol: rows for symbol in symbols
            },
            "index_bundle_loader": lambda *_args, **_kwargs: {},
            "analyzer": _signal,
            "max_workers": 1,
        }
        options.update(overrides)
        return TechnicalScanManager(**options)

    def test_cancelled_scan_stays_active_until_running_calculation_exits(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        calls = 0

        def analyzer(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            with call_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                first_started.set()
                self.assertTrue(release_first.wait(timeout=2.0))
            return _signal()

        manager = self._manager(analyzer=analyzer)
        first = manager.start(limit=1)
        self.assertTrue(first_started.wait(timeout=2.0))

        cancelled = manager.cancel(first["job_id"])
        self.assertIsNotNone(cancelled)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["stage"], "扫描已取消")

        blocked = manager.start(limit=1)
        self.assertEqual(blocked["job_id"], first["job_id"])
        self.assertEqual(blocked["status"], "cancelled")

        release_first.set()
        deadline = time.monotonic() + 2.0
        second = manager.start(limit=1)
        while second["job_id"] == first["job_id"] and time.monotonic() < deadline:
            time.sleep(0.01)
            second = manager.start(limit=1)
        self.assertNotEqual(second["job_id"], first["job_id"])
        _wait_for_status(manager, second["job_id"], {"done"})
        self.assertEqual(manager.get(first["job_id"])["status"], "cancelled")

    def test_concurrent_starts_and_cancels_share_one_active_job(self) -> None:
        analyzer_started = threading.Event()
        release_analyzer = threading.Event()

        def analyzer(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            analyzer_started.set()
            self.assertTrue(release_analyzer.wait(timeout=2.0))
            return _signal()

        manager = self._manager(analyzer=analyzer)
        first = manager.start(limit=1)
        self.assertTrue(analyzer_started.wait(timeout=2.0))

        start_results: list[dict[str, Any]] = []
        start_threads = [
            threading.Thread(target=lambda: start_results.append(manager.start(limit=1)))
            for _ in range(20)
        ]
        for thread in start_threads:
            thread.start()
        for thread in start_threads:
            thread.join(timeout=2.0)
        self.assertEqual({item["job_id"] for item in start_results}, {first["job_id"]})

        cancel_results: list[dict[str, Any] | None] = []
        cancel_threads = [
            threading.Thread(
                target=lambda: cancel_results.append(manager.cancel(first["job_id"])),
            )
            for _ in range(20)
        ]
        for thread in cancel_threads:
            thread.start()
        for thread in cancel_threads:
            thread.join(timeout=2.0)
        self.assertTrue(cancel_results)
        self.assertEqual(
            {item["status"] for item in cancel_results if item is not None},
            {"cancelled"},
        )

        release_analyzer.set()
        deadline = time.monotonic() + 2.0
        while manager.start(limit=1)["job_id"] == first["job_id"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_thread_start_failure_is_terminal_and_releases_active_slot(self) -> None:
        class FailingThread:
            def __init__(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
                del target, args, daemon

            def start(self) -> None:
                raise RuntimeError("cannot start worker")

        manager = self._manager()
        with mock.patch(
            "app.dashboard.technical_analysis_service.threading.Thread",
            FailingThread,
        ):
            failed = manager.start(limit=1)

        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["error"], "cannot start worker")
        replacement = manager.start(limit=1)
        self.assertNotEqual(replacement["job_id"], failed["job_id"])
        _wait_for_status(manager, replacement["job_id"], {"done"})

    def test_partial_executor_submit_failure_always_shuts_down_pool(self) -> None:
        shutdown_called = threading.Event()

        class FailingExecutor:
            def __init__(self, *, max_workers: int) -> None:
                del max_workers
                self.submissions = 0

            def submit(self, function: Any, symbol: str) -> Future[Any]:
                self.submissions += 1
                if self.submissions == 2:
                    raise RuntimeError("submit failed")
                future: Future[Any] = Future()
                future.set_result(function(symbol))
                return future

            def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
                self.assert_shutdown_contract(wait, cancel_futures)
                shutdown_called.set()

            @staticmethod
            def assert_shutdown_contract(wait: bool, cancel_futures: bool) -> None:
                if wait is not True or cancel_futures is not True:
                    raise AssertionError("executor must be fully drained")

        manager = self._manager(
            symbol_loader=lambda **_kwargs: ("sh600000", "sz000001"),
        )
        with mock.patch(
            "app.dashboard.technical_analysis_service.ThreadPoolExecutor",
            FailingExecutor,
        ):
            failed = manager.start(limit=2)
            status = _wait_for_status(manager, failed["job_id"], {"error"})

        self.assertTrue(shutdown_called.is_set())
        self.assertEqual(status["error"], "submit failed")
        replacement = manager.start(limit=1)
        self.assertNotEqual(replacement["job_id"], failed["job_id"])

    def test_queued_scan_can_be_cancelled_before_worker_starts(self) -> None:
        pending_target: list[Any] = []

        class DeferredThread:
            def __init__(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
                self.target = target
                self.args = args
                self.daemon = daemon
                pending_target.append(self)

            def start(self) -> None:
                return None

        manager = self._manager()
        with mock.patch(
            "app.dashboard.technical_analysis_service.threading.Thread",
            DeferredThread,
        ):
            created = manager.start(limit=1)

        cancelled = manager.cancel(created["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        replacement = manager.start(limit=1)
        self.assertNotEqual(replacement["job_id"], created["job_id"])
        pending_target[0].target(*pending_target[0].args)
        self.assertEqual(manager.get(created["job_id"])["status"], "cancelled")
        _wait_for_status(manager, replacement["job_id"], {"done"})

    def test_terminal_jobs_expire_after_ttl(self) -> None:
        clock = MutableClock()
        manager = self._manager(completed_job_ttl_seconds=10, clock=clock)
        created = manager.start(limit=1)
        _wait_for_status(manager, created["job_id"], {"done"})

        clock.value += 9
        self.assertIsNotNone(manager.get(created["job_id"]))
        clock.value += 1
        self.assertIsNone(manager.get(created["job_id"]))

    def test_ttl_works_when_job_finishes_at_zero_timestamp(self) -> None:
        clock = MutableClock(0.0)
        manager = self._manager(completed_job_ttl_seconds=5, clock=clock)
        created = manager.start(limit=1)
        _wait_for_status(manager, created["job_id"], {"done"})

        clock.value = 5.0
        self.assertIsNone(manager.get(created["job_id"]))

    def test_capacity_cleanup_evicts_oldest_terminal_job(self) -> None:
        clock = MutableClock()
        manager = self._manager(
            max_retained_jobs=2,
            completed_job_ttl_seconds=1000,
            clock=clock,
        )
        job_ids: list[str] = []
        for _ in range(3):
            created = manager.start(limit=1)
            job_ids.append(created["job_id"])
            _wait_for_status(manager, created["job_id"], {"done"})
            clock.value += 1

        self.assertIsNone(manager.get(job_ids[0]))
        self.assertIsNotNone(manager.get(job_ids[1]))
        self.assertIsNotNone(manager.get(job_ids[2]))

    def test_repeated_start_during_active_job_does_not_over_evict_history(self) -> None:
        analyzer_started = threading.Event()
        release_analyzer = threading.Event()

        def analyzer(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            analyzer_started.set()
            self.assertTrue(release_analyzer.wait(timeout=2.0))
            return _signal()

        manager = self._manager(max_retained_jobs=2)
        historical = manager.start(limit=1)
        _wait_for_status(manager, historical["job_id"], {"done"})
        manager._analyzer = analyzer
        active = manager.start(limit=1)
        self.assertTrue(analyzer_started.wait(timeout=2.0))

        repeated = manager.start(limit=1)
        self.assertEqual(repeated["job_id"], active["job_id"])
        self.assertIsNotNone(manager.get(historical["job_id"]))

        manager.cancel(active["job_id"])
        release_analyzer.set()

    def test_stale_completed_active_marker_does_not_block_new_job(self) -> None:
        manager = self._manager()
        historical = manager.start(limit=1)
        _wait_for_status(manager, historical["job_id"], {"done"})
        with manager._lock:
            manager._active_job_id = historical["job_id"]

        replacement = manager.start(limit=1)

        self.assertNotEqual(replacement["job_id"], historical["job_id"])
        _wait_for_status(manager, replacement["job_id"], {"done"})

    def test_public_result_mutation_does_not_change_retained_job(self) -> None:
        manager = self._manager()
        created = manager.start(limit=1)
        completed = _wait_for_status(manager, created["job_id"], {"done"})
        self.assertEqual(len(completed["results"]), 1)

        completed["results"][0]["score"] = 0
        completed["results"].clear()
        retained = manager.get(created["job_id"])

        self.assertIsNotNone(retained)
        self.assertEqual(len(retained["results"]), 1)
        self.assertEqual(retained["results"][0]["score"], 70)

    @unittest.skipIf(FastAPI is None, "FastAPI is not installed")
    def test_delete_route_cancels_job_with_limit_and_response_conventions(self) -> None:
        from app.dashboard.routers.technical_analysis import (
            create_technical_analysis_router,
        )

        job_id = "a" * 32

        class FakeScanManager:
            def __init__(self) -> None:
                self.cancel_calls: list[str] = []

            def cancel(self, requested_job_id: str) -> dict[str, Any] | None:
                self.cancel_calls.append(requested_job_id)
                if requested_job_id != job_id:
                    return None
                return {
                    "job_id": job_id,
                    "status": "cancelled",
                    "stage": "扫描已取消",
                }

        scan_manager = FakeScanManager()
        limit_calls: list[str] = []

        async def allow_request(request: Request) -> Response | None:
            limit_calls.append(request.method)
            return None

        def json_response(
            _request: Request,
            payload: dict[str, Any],
            *,
            cache_control: str,
            status_code: int = 200,
        ) -> Response:
            return JSONResponse(
                payload,
                status_code=status_code,
                headers={"Cache-Control": cache_control},
            )

        app = FastAPI()
        app.include_router(create_technical_analysis_router(
            enforce_api_limits=allow_request,
            json_response=json_response,
            scan_manager=scan_manager,
        ))

        with TestClient(app) as client:
            cancelled = client.delete(
                f"/api/technical-analysis/scans/{job_id}",
            )
            malformed = client.delete(
                "/api/technical-analysis/scans/not-a-job",
            )
            unknown = client.delete(
                f"/api/technical-analysis/scans/{'b' * 32}",
            )

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(cancelled.headers["Cache-Control"], "no-store")
        self.assertEqual(malformed.status_code, 404)
        self.assertEqual(malformed.json()["error"], "scan_not_found")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"], "scan_not_found")
        self.assertEqual(limit_calls, ["DELETE", "DELETE", "DELETE"])
        self.assertEqual(scan_manager.cancel_calls, [job_id, "b" * 32])


if __name__ == "__main__":
    unittest.main()
