#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from dashboard.model_connectivity import (  # noqa: E402
    model_test_metadata,
    resolve_model_test_config,
    test_model_connection as run_model_connection_test,
)


class _Response:
    headers = {"Content-Type": "application/json"}

    def __init__(self, content: str = "连接成功") -> None:
        self.body = json.dumps(
            {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]},
            ensure_ascii=False,
        ).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class ModelConnectivityTests(unittest.TestCase):
    def test_one_shared_model_setting_section_publishes_test_metadata(self):
        metadata = model_test_metadata()

        self.assertEqual(
            [item["id"] for item in metadata],
            ["shared-model"],
        )
        self.assertEqual(
            {item["group_slug"] for item in metadata},
            {"model-config"},
        )
        self.assertTrue(all("API_KEY" in " ".join(item["field_names"]) for item in metadata))

    def test_successful_decision_test_sends_one_small_authenticated_request(self):
        calls = []

        def opener(request, timeout=0):
            calls.append((request, timeout))
            return _Response()

        ticks = iter((10.0, 10.125))
        result = run_model_connection_test(
            "shared-model",
            {
                "DASHBOARD_DECISION_MODEL": "decision-test-model",
                "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1/",
                "DASHBOARD_DECISION_API_KEY": "private-key",
                "DASHBOARD_DECISION_REASONING_EFFORT": "MAX",
                "DASHBOARD_DECISION_STREAM_MODE": "non_stream",
            },
            timeout=90,
            opener=opener,
            monotonic=lambda: next(ticks),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["api_mode"], "chat")
        self.assertEqual(result["elapsed_ms"], 125)
        self.assertNotIn("private-key", json.dumps(result, ensure_ascii=False))
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(request.full_url, "https://model.example/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-key")
        self.assertEqual(timeout, 30.0)
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "decision-test-model")
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertFalse(payload["stream"])
        self.assertIn("网关已接受当前配置", result["message"])
        self.assertIn("思考强度 max", result["message"])
        self.assertIn("强制非流式", result["message"])

    def test_connection_test_force_stream_uses_sse_and_reports_transport(self):
        requests = []

        def opener(request, timeout=0):
            requests.append(request)
            return _Response()

        result = run_model_connection_test(
            "shared-model",
            {
                "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
                "DASHBOARD_DECISION_API_KEY": "private-key",
                "DASHBOARD_DECISION_STREAM_MODE": "stream",
            },
            opener=opener,
        )

        self.assertTrue(result["ok"])
        payload = json.loads(requests[0].data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertIn("强制流式", result["message"])

    def test_complete_provider_fallback_is_not_mixed_with_partial_override(self):
        config = resolve_model_test_config(
            "shared-model",
            {
                "DASHBOARD_DECISION_MODEL": "decision-test-model",
                "DASHBOARD_DECISION_BASE_URL": "https://partial.example/v1",
            },
            provider_fallback={
                "base_url": "https://provider.example/v1",
                "api_key": "provider-key",
            },
        )

        self.assertEqual(config.base_url, "https://provider.example/v1")
        self.assertEqual(config.api_key, "provider-key")

    def test_shared_target_ignores_legacy_grok_values(self):
        values = {
            "DASHBOARD_GROK_MODEL": "shared-grok",
            "DASHBOARD_GROK_BASE_URL": "https://grok.example/v1",
            "DASHBOARD_GROK_API_KEY": "grok-key",
            "DASHBOARD_GROK_API_MODE": "responses",
        }

        summary = resolve_model_test_config("shared-model", values)

        self.assertEqual(
            (summary.model, summary.base_url, summary.api_key, summary.api_mode),
            ("deepseek-v4-pro", "", "", "auto"),
        )
        self.assertEqual(summary.reasoning_effort, "")
        self.assertEqual(summary.stream_mode, "auto")

    def test_known_model_typo_is_rejected_locally_without_network_request(self):
        calls = []

        result = run_model_connection_test(
            "shared-model",
            {
                "DASHBOARD_DECISION_MODEL": "deepseek-v4-pro",
                "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
                "DASHBOARD_DECISION_API_KEY": "private-key",
                "DASHBOARD_DECISION_REASONING_EFFORT": "highh",
            },
            opener=lambda *_args, **_kwargs: calls.append(True),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_reasoning_effort")
        self.assertIn("允许值", result["error"])
        self.assertEqual(calls, [])

    def test_documented_compatibility_mapping_is_visible_in_success_message(self):
        ticks = iter((3.0, 3.01))
        result = run_model_connection_test(
            "shared-model",
            {
                "DASHBOARD_DECISION_MODEL": "deepseek-v4-pro",
                "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
                "DASHBOARD_DECISION_API_KEY": "private-key",
                "DASHBOARD_DECISION_REASONING_EFFORT": "low",
            },
            opener=lambda *_args, **_kwargs: _Response(),
            monotonic=lambda: next(ticks),
        )

        self.assertTrue(result["ok"])
        self.assertIn("low（按官方规则映射为 high）", result["message"])

    def test_invalid_and_unsupported_reasoning_effort_errors_are_actionable(self):
        def invalid_value(request, timeout=0):
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                {},
                io.BytesIO(b'{"error":{"message":"reasoning_effort must be one of low, high"}}'),
            )

        invalid = run_model_connection_test(
            "shared-model",
            {
                "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
                "DASHBOARD_DECISION_API_KEY": "private-key",
                "DASHBOARD_DECISION_REASONING_EFFORT": "max",
            },
            opener=invalid_value,
        )

        self.assertEqual(invalid["error_code"], "invalid_reasoning_effort")
        self.assertIn("不接受思考强度“max”", invalid["error"])

        def unsupported_parameter(request, timeout=0):
            raise urllib.error.HTTPError(
                request.full_url,
                422,
                "Unprocessable Entity",
                {},
                io.BytesIO(b'{"error":{"message":"unknown parameter: reasoning_effort"}}'),
            )

        unsupported = run_model_connection_test(
            "shared-model",
            {
                "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
                "DASHBOARD_DECISION_API_KEY": "private-key",
                "DASHBOARD_DECISION_REASONING_EFFORT": "max",
            },
            opener=unsupported_parameter,
        )

        self.assertEqual(unsupported["error_code"], "unsupported_reasoning_effort")
        self.assertIn("请留空后重试", unsupported["error"])

    def test_failures_are_actionable_and_do_not_expose_provider_bodies(self):
        missing = run_model_connection_test("shared-model", {})
        self.assertFalse(missing["ok"])
        self.assertIn("API 地址", missing["error"])
        self.assertIn("API Key", missing["error"])

        def unauthorized(request, timeout=0):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized private-key-in-reason",
                {},
                io.BytesIO(b'{"error":"private-key-in-body"}'),
            )

        failed = run_model_connection_test(
            "shared-model",
            {
                "DASHBOARD_DECISION_BASE_URL": "https://model.example/v1",
                "DASHBOARD_DECISION_API_KEY": "private-key",
            },
            opener=unauthorized,
        )

        serialized = json.dumps(failed, ensure_ascii=False)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error_code"], "http_401")
        self.assertNotIn("private-key", serialized)


if __name__ == "__main__":
    unittest.main()
