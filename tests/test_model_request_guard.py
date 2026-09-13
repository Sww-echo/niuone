import io
import multiprocessing
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from app.core import model_api as api
from app.core import model_request_guard as guard


def _competing_process(path, queue):
    try:
        guard.ModelRequestCoordinator(Path(path), clock=lambda: 1000).acquire("scope", duration=10)
        queue.put("admitted")
    except guard.ModelAdmissionError as exc:
        queue.put(exc.reason)


class ModelRequestGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="niuone-model-guard-")
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.path = Path(self.temp.name) / "guard.sqlite3"
        self.a = guard.ModelRequestCoordinator(self.path, clock=lambda: self.now)
        self.b = guard.ModelRequestCoordinator(self.path, clock=lambda: self.now)

    def test_processes_share_one_lease_and_crashes_expire(self):
        lease = self.a.acquire("scope", duration=10)
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=_competing_process, args=(str(self.path), queue))
        process.start()
        try:
            self.assertEqual(queue.get(timeout=10), "busy")
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            queue.close()
        self.now += 16
        replacement = self.b.acquire("scope", duration=10)
        self.a.release("scope", lease)  # A stale owner cannot release the new lease.
        with self.assertRaisesRegex(guard.ModelAdmissionError, "busy"):
            self.a.acquire("scope", duration=10)
        self.b.release("scope", replacement)

    def test_spacing_backoff_header_and_success_reset(self):
        lease = self.a.acquire("scope", duration=10)
        self.assertEqual(self.a.rate_limited("scope", lease, None), 60)
        self.a.release("scope", lease)
        with self.assertRaisesRegex(guard.ModelAdmissionError, "rate_limited"):
            self.b.acquire("scope", duration=10)
        self.now += 60
        lease = self.b.acquire("scope", duration=10)
        self.assertEqual(self.b.rate_limited("scope", lease, "600"), 600)
        self.b.release("scope", lease)
        self.now += 600
        lease = self.a.acquire("scope", duration=10)
        self.a.release("scope", lease, success=True)
        with self.assertRaisesRegex(guard.ModelAdmissionError, "spacing"):
            self.b.acquire("scope", duration=10)
        self.now += 2
        lease = self.b.acquire("scope", duration=10)
        self.assertEqual(self.b.rate_limited("scope", lease, None), 60)

    def test_unavailable_store_fails_closed(self):
        bad = guard.ModelRequestCoordinator(self.path / "missing.db")
        self.path.write_text("file", encoding="utf-8")
        with self.assertRaisesRegex(guard.ModelAdmissionError, "coordination_unavailable"):
            bad.acquire("scope", duration=10)

    def test_scopes_share_paths_and_models_but_not_credentials(self):
        one = guard.request_scope("https://api.example/v1/chat/completions", "secret")
        self.assertEqual(one, guard.request_scope("https://api.example/v1/responses", "secret"))
        self.assertNotEqual(one, guard.request_scope("https://api.example/v1/responses", "other"))
        self.assertNotIn("secret", one)
        self.assertEqual(guard.retry_after_seconds("Thu, 01 Jan 1970 00:17:00 GMT", 1000), 20)
        for value in ("bad", "nan", "inf"):
            self.assertIsNone(guard.retry_after_seconds(value, 1000))

    def test_positional_timeout_and_nested_retries_share_deadline(self):
        clock = [0.0]

        @guard.budgeted_model_call
        def nested(timeout=60):
            clock[0] += 2
            return "late"

        @guard.budgeted_model_call
        def outer(timeout=60):
            clock[0] += 2
            return nested(timeout=20)

        with patch.object(guard.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(guard.ModelRequestExpired):
                outer(3)
            self.assertEqual(guard.remaining_model_seconds(10), 10)  # Context reset.

    def test_http_429_stops_other_transport_before_network(self):
        calls = []
        def opener(*args, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError("https://api.example", 429, "limit", {"Retry-After": "120"}, io.BytesIO(b"private body"))
        request = api.build_model_request("https://api.example/v1", "test", [{"role": "user", "content": "x"}])
        with patch.object(api, "_STANDARD_OPENER", opener), patch.object(api, "shared_model_coordinator", return_value=self.a):
            with self.assertRaisesRegex(guard.ModelAdmissionError, "upstream_http_429"):
                api.request_model_complete(request, "key", timeout=10, opener=opener)
            with self.assertRaisesRegex(guard.ModelAdmissionError, "rate_limited"):
                api.request_model_complete(request, "key", timeout=10, opener=opener)
        self.assertEqual(len(calls), 1)

    def test_slow_stream_is_bounded_across_individually_fast_reads(self):
        clock = [0.0]
        class SlowBody:
            def read1(self, size):
                clock[0] += 1
                return b"data: x\n"
        response = api._DeadlineResponse(SlowBody(), 2.5)
        with patch.object(api.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(response.readline(), b"data: x\n")
            self.assertEqual(response.readline(), b"data: x\n")
            with self.assertRaises(guard.ModelRequestExpired):
                response.readline()


if __name__ == "__main__":
    unittest.main()
