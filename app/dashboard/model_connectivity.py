"""Bounded connectivity checks for model settings managed by the Dashboard."""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from core.model_api import (
    ModelResponseParseError,
    build_model_request,
    normalize_model_stream_mode,
    request_model_complete,
)
from core.model_reasoning import (
    UnsupportedReasoningEffortError,
    effective_reasoning_effort,
    resolve_model_reasoning_effort,
)
from core.shared_model_config import (
    LEGACY_SUMMARY_MODEL_ENV_NAMES,
    SHARED_MODEL_ENV_NAMES,
    resolve_shared_model_config,
)


MODEL_TEST_PROMPT = "这是一次模型连通性测试。无需解释，请只回复：连接成功"


@dataclass(frozen=True)
class ModelTestTarget:
    id: str
    group_slug: str
    label: str
    description: str
    model_names: tuple[str, ...]
    base_url_names: tuple[str, ...]
    api_key_names: tuple[str, ...]
    api_mode_names: tuple[str, ...]
    stream_mode_names: tuple[str, ...]
    reasoning_effort_names: tuple[str, ...]
    override_names: tuple[str, ...]
    default_model: str = ""
    default_api_mode: str = "chat"
    tool_type: str = ""


MODEL_TEST_TARGETS: tuple[ModelTestTarget, ...] = (
    ModelTestTarget(
        id="shared-model",
        group_slug="model-config",
        label="共享模型",
        description="验证买卖决策、文字策略细化和盘面总结共用的 OpenAI 兼容接口。",
        model_names=(
            "DASHBOARD_DECISION_MODEL",
            "A_SHARE_MODEL_SUMMARY_MODEL",
        ),
        base_url_names=(
            "DASHBOARD_DECISION_BASE_URL",
            "A_SHARE_MODEL_SUMMARY_BASE_URL",
            "CROSSDESK_BASE_URL",
        ),
        api_key_names=(
            "DASHBOARD_DECISION_API_KEY",
            "A_SHARE_MODEL_SUMMARY_API_KEY",
            "CROSSDESK_API_KEY",
        ),
        api_mode_names=(),
        stream_mode_names=("DASHBOARD_DECISION_STREAM_MODE",),
        reasoning_effort_names=("DASHBOARD_DECISION_REASONING_EFFORT",),
        override_names=(
            "DASHBOARD_DECISION_MODEL",
            "DASHBOARD_DECISION_BASE_URL",
            "DASHBOARD_DECISION_API_KEY",
            "DASHBOARD_DECISION_STREAM_MODE",
            "DASHBOARD_DECISION_REASONING_EFFORT",
        ),
        default_model="deepseek-v4-pro",
        default_api_mode="auto",
    ),
)

MODEL_TEST_TARGET_BY_ID = {target.id: target for target in MODEL_TEST_TARGETS}
# Keep old API target ids callable during upgrades, while metadata exposes only
# the single shared model test in the settings UI.
MODEL_TEST_TARGET_BY_ID.update(
    {
        "decision-model": MODEL_TEST_TARGETS[0],
        "a-share-summary-model": MODEL_TEST_TARGETS[0],
    }
)


@dataclass(frozen=True)
class ResolvedModelTestConfig:
    target: ModelTestTarget
    model: str
    base_url: str
    api_key: str
    api_mode: str
    stream_mode: str
    reasoning_effort: str
    effective_reasoning_effort: str


def model_test_metadata() -> list[dict[str, Any]]:
    """Return safe UI metadata without resolved credentials."""

    return [
        {
            "id": target.id,
            "group_slug": target.group_slug,
            "label": target.label,
            "description": target.description,
            "field_names": list(target.override_names),
        }
        for target in MODEL_TEST_TARGETS
    ]


def model_test_override_names(target_id: str) -> set[str]:
    target = MODEL_TEST_TARGET_BY_ID.get(str(target_id or "").strip())
    return set(target.override_names) if target else set()


def model_test_setting_names() -> set[str]:
    names: set[str] = set()
    for target in MODEL_TEST_TARGETS:
        names.update(target.model_names)
        names.update(target.base_url_names)
        names.update(target.api_key_names)
        names.update(target.api_mode_names)
        names.update(target.stream_mode_names)
        names.update(target.reasoning_effort_names)
    names.update(SHARED_MODEL_ENV_NAMES)
    names.update(LEGACY_SUMMARY_MODEL_ENV_NAMES)
    return names


def _first_value(values: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(values.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_model_test_config(
    target_id: str,
    values: Mapping[str, Any],
    *,
    provider_fallback: Mapping[str, Any] | None = None,
) -> ResolvedModelTestConfig:
    target = MODEL_TEST_TARGET_BY_ID.get(str(target_id or "").strip())
    if target is None:
        raise ValueError("不支持的模型测试目标")

    fallback = dict(provider_fallback or {})
    crossdesk_base_url = str(values.get("CROSSDESK_BASE_URL") or "").strip()
    crossdesk_api_key = str(values.get("CROSSDESK_API_KEY") or "").strip()
    if crossdesk_base_url and crossdesk_api_key:
        fallback = {
            "base_url": crossdesk_base_url,
            "api_key": crossdesk_api_key,
            "model": str(fallback.get("model") or "").strip(),
        }
    shared = resolve_shared_model_config(
        values,
        provider_fallback=fallback,
        default_model=target.default_model,
    )
    model = shared.model
    base_url = shared.base_url
    api_key = shared.api_key
    api_mode = _first_value(values, target.api_mode_names) or target.default_api_mode
    stream_mode = normalize_model_stream_mode(
        shared.stream_mode
    )
    reasoning_effort = shared.reasoning_effort
    effort_resolution = resolve_model_reasoning_effort(model, reasoning_effort)
    return ResolvedModelTestConfig(
        target=target,
        model=model,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        api_mode=api_mode,
        stream_mode=stream_mode,
        reasoning_effort=effort_resolution.configured_effort,
        effective_reasoning_effort=effort_resolution.effective_effort,
    )


def _validate_config(config: ResolvedModelTestConfig) -> str:
    missing = []
    if not config.model:
        missing.append("模型")
    if not config.base_url:
        missing.append("API 地址")
    if not config.api_key:
        missing.append("API Key")
    if missing:
        return "请先配置" + "、".join(missing)
    parsed = urlsplit(config.base_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return "API 地址必须是有效的 http 或 https 地址"
    return ""


def _http_error_message(code: int) -> str:
    if code == 400:
        return "请求格式、接口模式或模型名称不受支持（HTTP 400）"
    if code == 401:
        return "API Key 验证失败（HTTP 401）"
    if code == 403:
        return "模型服务拒绝访问，请检查 API Key 权限（HTTP 403）"
    if code == 404:
        return "模型接口或模型名称不存在（HTTP 404）"
    if code == 408:
        return "模型服务请求超时（HTTP 408）"
    if code == 429:
        return "模型服务触发限流或额度不足（HTTP 429）"
    if 500 <= code <= 599:
        return f"模型服务暂时不可用（HTTP {code}）"
    return f"模型服务返回错误（HTTP {code}）"


def _classified_http_error(
    exc: urllib.error.HTTPError,
    config: ResolvedModelTestConfig,
) -> tuple[str, str]:
    code = int(exc.code)
    try:
        body = exc.read(8192).decode("utf-8", errors="ignore").lower()
    except Exception:
        body = ""
    finally:
        try:
            exc.close()
        except Exception:
            pass

    if config.reasoning_effort and code in {400, 422}:
        mentions_reasoning = any(
            marker in body
            for marker in (
                "reasoning_effort",
                "reasoning.effort",
                '"reasoning"',
                "'reasoning'",
                "reasoning effort",
                "enable_thinking",
                "thinking.type",
                '"thinking"',
                "thinking mode",
            )
        )
        mentions_effort_value = any(
            marker in body
            for marker in (
                f"'{config.reasoning_effort}'",
                f'"{config.reasoning_effort}"',
            )
        )
        invalid_value = any(
            marker in body
            for marker in (
                "invalid value",
                "unsupported value",
                "not an allowed value",
                "must be one",
                "one of",
                "valid values",
                "allowed values",
                "enum",
                "does not exist",
                "not supported",
            )
        )
        unsupported_parameter = any(
            marker in body
            for marker in (
                "unknown parameter",
                "unrecognized parameter",
                "unsupported parameter",
                "not support",
                "not allowed",
                "extra inputs",
                "additional properties",
            )
        )
        if (mentions_reasoning or mentions_effort_value) and invalid_value:
            return (
                f"当前模型或网关不接受思考强度“{config.reasoning_effort}”，请修改或留空使用模型默认值（HTTP {code}）",
                "invalid_reasoning_effort",
            )
        if mentions_reasoning and unsupported_parameter:
            return (
                f"当前模型或网关不支持思考强度参数，请留空后重试（HTTP {code}）",
                "unsupported_reasoning_effort",
            )
    return _http_error_message(code), f"http_{code}"


def test_model_connection(
    target_id: str,
    values: Mapping[str, Any],
    *,
    provider_fallback: Mapping[str, Any] | None = None,
    timeout: float = 20,
    opener=urllib.request.urlopen,
    monotonic=time.monotonic,
) -> dict[str, Any]:
    """Send one small model request and return only non-sensitive diagnostics."""

    try:
        config = resolve_model_test_config(
            target_id,
            values,
            provider_fallback=provider_fallback,
        )
    except UnsupportedReasoningEffortError as exc:
        return {
            "ok": False,
            "target": str(target_id or ""),
            "model": exc.model,
            "error": str(exc),
            "error_code": "invalid_reasoning_effort",
        }
    except ValueError as exc:
        return {"ok": False, "target": str(target_id or ""), "error": str(exc)}

    result: dict[str, Any] = {
        "ok": False,
        "target": config.target.id,
        "label": config.target.label,
        "model": config.model,
    }
    validation_error = _validate_config(config)
    if validation_error:
        result.update({"error": validation_error, "error_code": "invalid_config"})
        return result

    tools = [{"type": config.target.tool_type}] if config.target.tool_type else None
    model_request = build_model_request(
        config.base_url,
        config.model,
        [{"role": "user", "content": MODEL_TEST_PROMPT}],
        max_tokens=256,
        api_mode=config.api_mode,
        tools=tools,
        reasoning_effort=config.reasoning_effort,
        stream=False,
        extra_payload={"stream": False},
    )
    result["api_mode"] = model_request.api_mode
    selected_effective_effort = effective_reasoning_effort(
        config.model,
        config.reasoning_effort,
        api_mode=model_request.api_mode,
    )
    started = monotonic()
    try:
        parsed = request_model_complete(
            model_request,
            config.api_key,
            timeout=max(5.0, min(30.0, float(timeout))),
            stream_mode=config.stream_mode,
            opener=opener,
        )
        if not str(parsed.content or "").strip():
            result.update(
                {
                    "error": "模型已响应，但未返回可用文本",
                    "error_code": "empty_response",
                }
            )
            return result
    except urllib.error.HTTPError as exc:
        error, error_code = _classified_http_error(exc, config)
        result.update(
            {
                "error": error,
                "error_code": error_code,
            }
        )
        return result
    except (TimeoutError, socket.timeout):
        error = "模型连接超时"
        if config.reasoning_effort:
            error += "；较高思考强度可能需要更长时间，暂时无法判断该值是否受支持"
        result.update({"error": error, "error_code": "timeout"})
        return result
    except urllib.error.URLError:
        result.update({"error": "无法连接模型服务，请检查地址和网络", "error_code": "connection_failed"})
        return result
    except ModelResponseParseError:
        result.update({"error": "模型返回格式无法识别", "error_code": "invalid_response"})
        return result
    except (OSError, ValueError):
        result.update({"error": "模型连接失败，请检查接口配置", "error_code": "request_failed"})
        return result
    except Exception:
        result.update({"error": "模型测试失败", "error_code": "unexpected_error"})
        return result

    elapsed_ms = max(0, int(round((monotonic() - started) * 1000)))
    mode_label = "Responses API" if model_request.api_mode == "responses" else "Chat Completions"
    effort_label = (
        f"，思考强度 {config.reasoning_effort}"
        if config.reasoning_effort
        else "，未指定思考强度"
    )
    stream_label = {
        "auto": "流式自动",
        "stream": "强制流式",
        "non_stream": "强制非流式",
    }.get(config.stream_mode, config.stream_mode)
    if "auto_stream_fallback=1" in parsed.detail:
        stream_label += "（已自动切换为流式）"
    if (
        config.reasoning_effort
        and selected_effective_effort != config.reasoning_effort
    ):
        effort_label += f"（按官方规则映射为 {selected_effective_effort}）"
    result.update(
        {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "message": (
                f"{config.target.label}网关已接受当前配置"
                f"（{mode_label}，{stream_label}{effort_label}，{elapsed_ms} ms）"
            ),
        }
    )
    return result
