#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from pathlib import Path

from app.core.model_api import (
    ModelResponseParseError,
    build_model_request,
    normalize_model_stream_mode,
    parse_model_response,
    request_model,
    request_model_complete,
    stream_model_response,
    uses_responses_api,
)


class _Response:
    def __init__(self, body: str, content_type: str = "application/json") -> None:
        self._body = io.BytesIO(body.encode("utf-8"))
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body.read()

    def readline(self) -> bytes:
        return self._body.readline()


class ModelApiTests(unittest.TestCase):
    def test_stream_mode_normalization_is_strict_and_backward_friendly(self):
        self.assertEqual(normalize_model_stream_mode(""), "auto")
        self.assertEqual(normalize_model_stream_mode("streaming"), "stream")
        self.assertEqual(normalize_model_stream_mode("non-stream"), "non_stream")
        with self.assertRaisesRegex(ValueError, "流式模式"):
            normalize_model_stream_mode("sometimes")

    def test_model_endpoint_construction_is_centralized(self):
        app_dir = Path(__file__).resolve().parents[1] / "app"
        helper = (app_dir / "core" / "model_api.py").resolve()
        offenders = []
        for path in app_dir.rglob("*.py"):
            if path.resolve() == helper:
                continue
            source = path.read_text(encoding="utf-8")
            endpoint_literals = (
                '"/chat/completions"',
                "'/chat/completions'",
                '"/responses"',
                "'/responses'",
            )
            if any(literal in source for literal in endpoint_literals):
                offenders.append(str(path.relative_to(app_dir)))
        self.assertEqual(offenders, [])

    def test_auto_mode_preserves_legacy_chat_and_enables_known_search_models(self):
        self.assertFalse(uses_responses_api("auto", "legacy-search-model", web_search=True))
        self.assertTrue(uses_responses_api("auto", "grok-4.3", web_search=True))
        self.assertTrue(uses_responses_api("auto", "grok-latest", web_search=True))
        self.assertTrue(uses_responses_api("auto", "grok-4.5", web_search=True))
        self.assertTrue(uses_responses_api("auto", "mimo-v2.5-pro"))
        self.assertTrue(uses_responses_api("auto", "qwen3.8-max"))
        self.assertTrue(uses_responses_api("auto", "qwen3.7-plus"))
        self.assertFalse(uses_responses_api("auto", "qwen-plus-latest"))
        self.assertTrue(uses_responses_api("auto", "gpt-5.6-sol", web_search=True))
        self.assertFalse(uses_responses_api("chat-completions", "gpt-5.6-sol", web_search=True))
        self.assertTrue(uses_responses_api("responses", "legacy-model"))

    def test_gpt_x_search_does_not_enable_responses_in_auto_mode(self):
        request = build_model_request(
            "https://model.example/v1",
            "gpt-5.6-sol",
            [{"role": "user", "content": "search X"}],
            max_tokens=123,
            api_mode="auto",
            tools=[{"type": "x_search"}],
        )

        self.assertEqual(request.endpoint, "https://model.example/v1/chat/completions")
        self.assertEqual(request.payload["max_tokens"], 123)
        self.assertNotIn("tools", request.payload)

    def test_chat_request_uses_max_tokens(self):
        request = build_model_request(
            "https://model.example/v1/",
            "legacy-model",
            [{"role": "user", "content": "hello"}],
            max_tokens=123,
            api_mode="chat",
        )

        self.assertEqual(request.endpoint, "https://model.example/v1/chat/completions")
        self.assertEqual(request.payload["max_tokens"], 123)
        self.assertNotIn("max_output_tokens", request.payload)

    def test_reasoning_effort_maps_to_each_api_shape_and_normalizes(self):
        chat = build_model_request(
            "https://model.example/v1",
            "chat-model",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            reasoning_effort=" HIGH ",
        )
        responses = build_model_request(
            "https://model.example/v1",
            "responses-model",
            [{"role": "user", "content": "hello"}],
            api_mode="responses",
            reasoning_effort="XHIGH",
        )

        self.assertEqual(chat.payload["reasoning_effort"], "high")
        self.assertNotIn("reasoning", chat.payload)
        self.assertEqual(responses.payload["reasoning"], {"effort": "xhigh"})
        self.assertNotIn("reasoning_effort", responses.payload)

    def test_empty_reasoning_effort_omits_parameter(self):
        chat = build_model_request(
            "https://model.example/v1",
            "chat-model",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            reasoning_effort="",
        )
        responses = build_model_request(
            "https://model.example/v1",
            "responses-model",
            [{"role": "user", "content": "hello"}],
            api_mode="responses",
            reasoning_effort="",
        )

        self.assertNotIn("reasoning_effort", chat.payload)
        self.assertNotIn("reasoning", responses.payload)

    def test_unknown_model_reasoning_effort_is_free_form_but_bounded(self):
        request = build_model_request(
            "https://model.example/v1",
            "chat-model",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            reasoning_effort="provider.custom-high",
        )

        self.assertEqual(request.payload["reasoning_effort"], "provider.custom-high")
        with self.assertRaisesRegex(ValueError, "思考强度"):
            build_model_request(
                "https://model.example/v1",
                "chat-model",
                [{"role": "user", "content": "hello"}],
                api_mode="chat",
                reasoning_effort="not a token",
            )

    def test_known_model_reasoning_effort_is_checked_before_request(self):
        with self.assertRaisesRegex(ValueError, "允许值"):
            build_model_request(
                "https://model.example/v1",
                "deepseek-v4-pro",
                [{"role": "user", "content": "hello"}],
                api_mode="chat",
                reasoning_effort="highh",
            )

    def test_grok_43_none_uses_responses_reasoning_shape(self):
        request = build_model_request(
            "https://api.x.ai/v1",
            "grok-4.3",
            [{"role": "user", "content": "hello"}],
            api_mode="auto",
            reasoning_effort="none",
        )

        self.assertEqual(request.endpoint, "https://api.x.ai/v1/responses")
        self.assertEqual(request.payload["reasoning"], {"effort": "none"})

    def test_glm_52_chat_request_sends_thinking_and_reasoning_effort(self):
        request = build_model_request(
            "https://open.bigmodel.cn/api/paas/v4",
            "glm-5.2",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            reasoning_effort="low",
        )

        self.assertEqual(request.payload["thinking"], {"type": "enabled"})
        self.assertEqual(request.payload["reasoning_effort"], "low")

    def test_older_glm_chat_request_uses_thinking_switch(self):
        request = build_model_request(
            "https://open.bigmodel.cn/api/paas/v4",
            "glm-4.7",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            reasoning_effort="disabled",
        )

        self.assertEqual(request.payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", request.payload)

    def test_mimo_auto_responses_and_forced_chat_use_vendor_shapes(self):
        responses = build_model_request(
            "https://api.xiaomimimo.com/v1",
            "mimo-v2.5-pro",
            [{"role": "user", "content": "hello"}],
            api_mode="auto",
            reasoning_effort="medium",
        )
        chat = build_model_request(
            "https://api.xiaomimimo.com/v1",
            "mimo-v2.5",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            max_tokens=1024,
            reasoning_effort="none",
        )

        self.assertEqual(responses.payload["reasoning"], {"effort": "medium"})
        self.assertEqual(chat.payload["thinking"], {"type": "disabled"})
        self.assertEqual(chat.payload["max_completion_tokens"], 1024)
        self.assertNotIn("max_tokens", chat.payload)

    def test_qwen_auto_responses_preserves_levels_and_forced_chat_uses_vendor_shapes(self):
        responses = build_model_request(
            "https://dashscope.example/compatible-mode/v1",
            "qwen3.7-plus",
            [{"role": "user", "content": "hello"}],
            api_mode="auto",
            reasoning_effort="max",
        )
        forced_chat = build_model_request(
            "https://dashscope.example/compatible-mode/v1",
            "qwen3.7-plus",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            reasoning_effort="high",
        )
        qwen_38_off = build_model_request(
            "https://dashscope.example/compatible-mode/v1",
            "qwen3.8-max",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            reasoning_effort="none",
        )

        self.assertEqual(responses.api_mode, "responses")
        self.assertEqual(responses.payload["reasoning"], {"effort": "max"})
        self.assertEqual(forced_chat.payload["enable_thinking"], True)
        self.assertNotIn("reasoning_effort", forced_chat.payload)
        self.assertEqual(qwen_38_off.payload["enable_thinking"], False)

    def test_minimax_chat_and_responses_preserve_official_thinking_semantics(self):
        m3_chat = build_model_request(
            "https://api.minimax.io/v1",
            "MiniMax-M3",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            max_tokens=512,
            reasoning_effort="medium",
        )
        m3_responses = build_model_request(
            "https://api.minimax.io/v1",
            "MiniMax-M3",
            [{"role": "user", "content": "hello"}],
            api_mode="responses",
            reasoning_effort="none",
        )
        m2_chat = build_model_request(
            "https://api.minimax.io/v1",
            "MiniMax-M2.7-highspeed",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            max_tokens=1024,
            reasoning_effort="none",
        )

        self.assertEqual(m3_chat.payload["thinking"], {"type": "adaptive"})
        self.assertEqual(m3_chat.payload["reasoning_split"], True)
        self.assertEqual(m3_chat.payload["max_completion_tokens"], 512)
        self.assertEqual(m3_responses.payload["reasoning"], {"effort": "none"})
        self.assertNotIn("thinking", m2_chat.payload)
        self.assertNotIn("reasoning_effort", m2_chat.payload)
        self.assertEqual(m2_chat.payload["reasoning_split"], True)
        self.assertEqual(m2_chat.payload["max_completion_tokens"], 1024)

    def test_grok_responses_request_uses_max_output_tokens(self):
        request = build_model_request(
            "https://model.example/v1",
            "grok-4.5",
            [{"role": "user", "content": "hello"}],
            max_tokens=321,
            api_mode="auto",
            tools=[{"type": "web_search"}],
            reasoning={"effort": "low"},
        )

        self.assertEqual(request.endpoint, "https://model.example/v1/responses")
        self.assertEqual(request.payload["max_output_tokens"], 321)
        self.assertEqual(request.payload["tools"], [{"type": "web_search"}])
        self.assertNotIn("max_tokens", request.payload)

    def test_gpt_56_responses_request_omits_rejected_output_limit(self):
        request = build_model_request(
            "https://model.example/v1",
            "gpt-5.6-sol",
            [{"role": "user", "content": "hello"}],
            max_tokens=4096,
            api_mode="auto",
            tools=[{"type": "web_search"}],
        )

        self.assertEqual(request.endpoint, "https://model.example/v1/responses")
        self.assertNotIn("max_output_tokens", request.payload)
        self.assertNotIn("max_tokens", request.payload)

    def test_parse_chat_json_and_sse(self):
        parsed_json = parse_model_response(
            '{"choices":[{"message":{"content":"json ok"},"finish_reason":"stop"}]}'
        )
        parsed_sse = parse_model_response(
            "event: message\n"
            'data: {"choices":[{"delta":{"content":"sse "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n",
            "text/event-stream",
        )

        self.assertEqual(parsed_json.content, "json ok")
        self.assertIn("finish_reason=stop", parsed_json.detail)
        self.assertEqual(parsed_sse.content, "sse ok")
        self.assertIn("sse_chunks=2", parsed_sse.detail)

    def test_parse_responses_json_and_forced_sse(self):
        parsed_json = parse_model_response(
            '{"status":"completed","output":[{"content":[{"type":"output_text","text":"json result"}]}]}'
        )
        raw_sse = (
            "event: response.created\n"
            'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
            "event: response.web_search_call.searching\n"
            'data: {"type":"response.web_search_call.searching"}\n\n'
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"live "}\n\n'
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"result"}\n\n'
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"status":"completed","output":[{"content":[{"type":"output_text","text":"live result"}]}]}}\n\n'
        )
        parsed_sse = parse_model_response(raw_sse, "text/event-stream")

        self.assertEqual(parsed_json.content, "json result")
        self.assertEqual(parsed_sse.content, "live result")
        self.assertIn("search_events=1", parsed_sse.detail)

    def test_stream_chat_response_yields_deltas_and_requests_sse(self):
        request = build_model_request(
            "https://model.example/v1",
            "legacy-model",
            [{"role": "user", "content": "hello"}],
            max_tokens=100,
            api_mode="chat",
            stream=True,
        )
        captured = {}

        def opener(req, timeout=0):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            captured["accept"] = req.headers.get("Accept")
            captured["timeout"] = timeout
            return _Response(
                'data: {"choices":[{"delta":{"content":"live "}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"json"}}]}\n\n'
                "data: [DONE]\n\n",
                "text/event-stream",
            )

        chunks = list(
            stream_model_response(request, "secret", timeout=17, opener=opener)
        )

        self.assertEqual(chunks, ["live ", "json"])
        self.assertTrue(captured["payload"]["stream"])
        self.assertIn("text/event-stream", captured["accept"])
        self.assertEqual(captured["timeout"], 17)

    def test_stream_responses_api_avoids_completed_text_duplication(self):
        request = build_model_request(
            "https://model.example/v1",
            "gateway-model",
            [{"role": "user", "content": "hello"}],
            api_mode="responses",
            stream=True,
        )

        def opener(_req, timeout=0):
            return _Response(
                'data: {"type":"response.output_text.delta","delta":"part 1"}\n\n'
                'data: {"type":"response.output_text.delta","delta":" part 2"}\n\n'
                'data: {"type":"response.completed","response":{"output":'
                '[{"content":[{"type":"output_text","text":"part 1 part 2"}]}]}}\n\n',
                "text/event-stream",
            )

        self.assertEqual(
            list(stream_model_response(request, "secret", timeout=5, opener=opener)),
            ["part 1", " part 2"],
        )

    def test_stream_falls_back_to_complete_json_when_gateway_ignores_stream(self):
        request = build_model_request(
            "https://model.example/v1",
            "legacy-model",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            stream=True,
        )

        chunks = list(
            stream_model_response(
                request,
                "secret",
                timeout=5,
                opener=lambda *_args, **_kwargs: _Response(
                    '{"choices":[{"message":{"content":"complete json"}}]}'
                ),
            )
        )

        self.assertEqual(chunks, ["complete json"])

    def test_complete_request_force_stream_assembles_visible_text(self):
        request = build_model_request(
            "https://model.example/v1",
            "legacy-model",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
        )
        payloads = []

        def opener(req, timeout=0):
            payloads.append(json.loads(req.data.decode("utf-8")))
            return _Response(
                'data: {"choices":[{"delta":{"content":"full "}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
                "data: [DONE]\n\n",
                "text/event-stream",
            )

        parsed = request_model_complete(
            request,
            "secret",
            timeout=5,
            stream_mode="stream",
            opener=opener,
        )

        self.assertEqual(parsed.content, "full answer")
        self.assertEqual(parsed.detail, "transport=stream")
        self.assertEqual([payload["stream"] for payload in payloads], [True])

    def test_complete_request_auto_retries_only_explicit_stream_required_error(self):
        request = build_model_request(
            "https://model.example/v1",
            "legacy-model",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
        )
        payloads = []

        def opener(req, timeout=0):
            payload = json.loads(req.data.decode("utf-8"))
            payloads.append(payload)
            if not payload["stream"]:
                raise urllib.error.HTTPError(
                    req.full_url,
                    400,
                    "Bad Request",
                    {},
                    io.BytesIO(b'{"error":"stream must be true"}'),
                )
            return _Response(
                'data: {"choices":[{"delta":{"content":"fallback ok"}}]}\n\n'
                "data: [DONE]\n\n",
                "text/event-stream",
            )

        parsed = request_model_complete(
            request,
            "secret",
            timeout=5,
            stream_mode="auto",
            opener=opener,
        )

        self.assertEqual(parsed.content, "fallback ok")
        self.assertIn("auto_stream_fallback=1", parsed.detail)
        self.assertEqual([payload["stream"] for payload in payloads], [False, True])

    def test_complete_request_forced_non_stream_does_not_retry(self):
        request = build_model_request(
            "https://model.example/v1",
            "legacy-model",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
        )
        payloads = []

        def opener(req, timeout=0):
            payloads.append(json.loads(req.data.decode("utf-8")))
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                {},
                io.BytesIO(b'{"error":"stream=true is required"}'),
            )

        with self.assertRaises(urllib.error.HTTPError):
            request_model_complete(
                request,
                "secret",
                timeout=5,
                stream_mode="non_stream",
                opener=opener,
            )

        self.assertEqual([payload["stream"] for payload in payloads], [False])

    def test_parse_failures_use_dedicated_exception(self):
        for raw in ("", "<html>gateway error</html>", "[]"):
            with self.subTest(raw=raw):
                with self.assertRaises(ModelResponseParseError):
                    parse_model_response(raw)

    def test_unknown_responses_model_retries_without_unsupported_output_limit(self):
        request = build_model_request(
            "https://model.example/v1",
            "gateway-model",
            [{"role": "user", "content": "hello"}],
            max_tokens=500,
            api_mode="responses",
        )
        error_bodies = (
            b'{"detail":"Unsupported parameter: max_output_tokens"}',
            b'{"detail":"max_output_tokens is unsupported"}',
            b'{"detail":"gateway does not support max_output_tokens"}',
        )

        for error_body in error_bodies:
            with self.subTest(error_body=error_body):
                payloads: list[dict] = []

                def opener(req, timeout=0):
                    payloads.append(json.loads(req.data.decode("utf-8")))
                    if len(payloads) == 1:
                        body = io.BytesIO(error_body)
                        raise urllib.error.HTTPError(
                            req.full_url, 400, "Bad Request", {}, body
                        )
                    return _Response(
                        '{"output":[{"content":[{"type":"output_text","text":"ok"}]}]}'
                    )

                parsed = request_model(request, "secret", timeout=3, opener=opener)

                self.assertEqual(parsed.content, "ok")
                self.assertEqual(len(payloads), 2)
                self.assertIn("max_output_tokens", payloads[0])
                self.assertNotIn("max_output_tokens", payloads[1])

    def test_invalid_output_limit_value_does_not_retry_without_parameter(self):
        request = build_model_request(
            "https://model.example/v1",
            "gateway-model",
            [{"role": "user", "content": "hello"}],
            max_tokens=500,
            api_mode="responses",
        )
        payloads: list[dict] = []

        def opener(req, timeout=0):
            payloads.append(json.loads(req.data.decode("utf-8")))
            body = io.BytesIO(
                b'{"detail":"Invalid parameter: max_output_tokens must be at most 200"}'
            )
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, body)

        with self.assertRaises(urllib.error.HTTPError) as raised:
            request_model(request, "secret", timeout=3, opener=opener)

        self.assertEqual(len(payloads), 1)
        self.assertIn("max_output_tokens", payloads[0])
        self.assertIn(b"Invalid parameter", raised.exception.read())
        raised.exception.close()

    def test_rejected_reasoning_effort_is_not_silently_retried_without_it(self):
        request = build_model_request(
            "https://model.example/v1",
            "gateway-model",
            [{"role": "user", "content": "hello"}],
            api_mode="chat",
            reasoning_effort="max",
        )
        payloads: list[dict] = []

        def opener(req, timeout=0):
            payloads.append(json.loads(req.data.decode("utf-8")))
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                {},
                io.BytesIO(b'{"error":"unsupported parameter: reasoning_effort"}'),
            )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            request_model(request, "secret", timeout=3, opener=opener)

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["reasoning_effort"], "max")
        raised.exception.close()


if __name__ == "__main__":
    unittest.main()
