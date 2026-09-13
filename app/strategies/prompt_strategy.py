"""Pure helpers for versioned, auditable prompt-driven strategy policies."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from .registry import STRATEGY_SOURCE_PRESET_TEXT


PRESET_STRATEGY_SNAPSHOT_SCHEMA_VERSION = 1
PRESET_STRATEGY_AUDIT_SCHEMA_VERSION = 1
PRESET_STRATEGY_PROMPT_PROTOCOL = "preset-prompt-v1"
PRESET_STRATEGY_INTERPRETATION_KEYS = (
    "selection_rules",
    "entry_rules",
    "exit_rules",
    "position_rules",
    "time_rules",
    "ambiguities",
)

PRESET_CANDIDATE_FACT_FIELDS = (
    "code",
    "name",
    "price",
    "change_pct",
    "amount_yi",
    "turnover",
    "industry",
    "return_5d_pct",
    "return_20d_pct",
    "distance_ema20_pct",
    "distance_bbi_pct",
    "distance_high_20d_pct",
    "volume_ratio_5d",
    "volatility_20d_pct",
    "current_j",
    "above_ema20",
    "above_bbi",
    "risk_flags",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_prompt_strategy_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def prompt_strategy_text_sha256(value: Any) -> str:
    return _sha256_text(normalize_prompt_strategy_text(value))


def build_preset_strategy_snapshot(
    text: Any,
    *,
    captured_at: str,
) -> dict[str, Any]:
    normalized = normalize_prompt_strategy_text(text)
    return {
        "schema_version": PRESET_STRATEGY_SNAPSHOT_SCHEMA_VERSION,
        "strategy_id": STRATEGY_SOURCE_PRESET_TEXT,
        "label": "预设文字策略",
        "policy_source": "prompt",
        "text": normalized,
        "text_sha256": prompt_strategy_text_sha256(normalized),
        "captured_at": str(captured_at or ""),
    }


def validate_preset_strategy_snapshot(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return "缺少预设文字策略快照"
    if value.get("schema_version") != PRESET_STRATEGY_SNAPSHOT_SCHEMA_VERSION:
        return "预设文字策略快照版本不受支持"
    if str(value.get("strategy_id") or "") != STRATEGY_SOURCE_PRESET_TEXT:
        return "预设文字策略快照身份不一致"
    text = normalize_prompt_strategy_text(value.get("text"))
    if not text:
        return "预设文字为空"
    expected = prompt_strategy_text_sha256(text)
    if str(value.get("text_sha256") or "") != expected:
        return "预设文字策略快照指纹不一致"
    return None


def normalize_preset_strategy_interpretation(value: Any) -> dict[str, list[str]] | None:
    if not isinstance(value, Mapping):
        return None
    normalized: dict[str, list[str]] = {}
    for key in PRESET_STRATEGY_INTERPRETATION_KEYS:
        raw = value.get(key)
        if not isinstance(raw, list):
            return None
        items: list[str] = []
        for item in raw[:20]:
            text = " ".join(str(item or "").split()).strip()
            if text:
                items.append(text[:500])
        normalized[key] = items
    if any(
        not normalized[key]
        for key in (
            "selection_rules",
            "entry_rules",
            "exit_rules",
            "position_rules",
            "time_rules",
        )
    ):
        return None
    return normalized


def preset_strategy_interpretation_sha256(value: Any) -> str:
    normalized = normalize_preset_strategy_interpretation(value)
    return _sha256_text(_canonical_json(normalized)) if normalized is not None else ""


def _audit_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_audit_scalar(item) for item in value[:20]]
    return str(value)[:500]


def preset_candidate_facts(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: _audit_scalar(candidate.get(field))
        for field in PRESET_CANDIDATE_FACT_FIELDS
        if candidate.get(field) not in (None, "", [])
    }


def build_preset_candidate_audit(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    facts = [preset_candidate_facts(item) for item in candidates if isinstance(item, Mapping)]
    codes = [str(item.get("code") or "") for item in facts if str(item.get("code") or "")]
    return {
        "count": len(facts),
        "codes": codes,
        "facts": facts,
        "facts_sha256": _sha256_text(_canonical_json(facts)),
    }


def build_preset_decision_audit(
    *,
    snapshot: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    interpretation: Mapping[str, Any],
    prompt: str,
    generated_at: str,
) -> dict[str, Any]:
    normalized_interpretation = normalize_preset_strategy_interpretation(interpretation)
    return {
        "schema_version": PRESET_STRATEGY_AUDIT_SCHEMA_VERSION,
        "strategy_id": STRATEGY_SOURCE_PRESET_TEXT,
        "policy_source": "prompt",
        "prompt_protocol": PRESET_STRATEGY_PROMPT_PROTOCOL,
        "snapshot": dict(snapshot),
        "candidate_pool": build_preset_candidate_audit(candidates),
        "interpretation": normalized_interpretation,
        "interpretation_sha256": preset_strategy_interpretation_sha256(
            normalized_interpretation
        ),
        "prompt_sha256": _sha256_text(str(prompt or "")),
        "generated_at": str(generated_at or ""),
    }


def validate_preset_buy_audit(
    audit: Any,
    *,
    code: str,
    candidates: Sequence[Mapping[str, Any]],
    current_text: Any,
) -> str | None:
    if not isinstance(audit, Mapping):
        return "缺少预设文字策略决策审计"
    if audit.get("schema_version") != PRESET_STRATEGY_AUDIT_SCHEMA_VERSION:
        return "预设文字策略决策审计版本不受支持"
    if str(audit.get("strategy_id") or "") != STRATEGY_SOURCE_PRESET_TEXT:
        return "预设文字策略决策审计身份不一致"
    if str(audit.get("policy_source") or "") != "prompt":
        return "预设文字策略决策审计来源不一致"
    if str(audit.get("prompt_protocol") or "") != PRESET_STRATEGY_PROMPT_PROTOCOL:
        return "预设文字策略Prompt协议不受支持"
    snapshot = audit.get("snapshot")
    snapshot_error = validate_preset_strategy_snapshot(snapshot)
    if snapshot_error:
        return snapshot_error
    if str((snapshot or {}).get("text_sha256") or "") != prompt_strategy_text_sha256(current_text):
        return "预设文字策略已在决策后变更，旧决策不得买入"
    interpretation = normalize_preset_strategy_interpretation(audit.get("interpretation"))
    if interpretation is None:
        return "模型未返回完整的文字策略结构化解释"
    if str(audit.get("interpretation_sha256") or "") != preset_strategy_interpretation_sha256(
        interpretation
    ):
        return "文字策略结构化解释指纹不一致"
    candidate_pool = audit.get("candidate_pool")
    expected_pool = build_preset_candidate_audit(candidates)
    if not isinstance(candidate_pool, Mapping) or dict(candidate_pool) != expected_pool:
        return "预设文字策略候选池审计不一致"
    if str(code or "") not in expected_pool["codes"]:
        return "买入标的不在预设文字策略已审计候选池"
    prompt_sha256 = str(audit.get("prompt_sha256") or "")
    if len(prompt_sha256) != 64:
        return "预设文字策略Prompt指纹缺失"
    return None


def build_preset_exit_audit(
    position_contexts: Sequence[Mapping[str, Any]],
    *,
    prompt: str,
    generated_at: str,
) -> dict[str, Any]:
    positions: dict[str, dict[str, Any]] = {}
    for item in position_contexts:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "")
        snapshot = item.get("snapshot")
        interpretation = item.get("interpretation")
        interpretation_sha256 = str(item.get("interpretation_sha256") or "")
        if (
            not code
            or validate_preset_strategy_snapshot(snapshot)
            or not interpretation_sha256
            or interpretation_sha256
            != preset_strategy_interpretation_sha256(interpretation)
        ):
            continue
        positions[code] = {
            "text_sha256": str((snapshot or {}).get("text_sha256") or ""),
            "interpretation_sha256": interpretation_sha256,
            "captured_at": str((snapshot or {}).get("captured_at") or ""),
        }
    return {
        "schema_version": PRESET_STRATEGY_AUDIT_SCHEMA_VERSION,
        "strategy_id": STRATEGY_SOURCE_PRESET_TEXT,
        "prompt_protocol": PRESET_STRATEGY_PROMPT_PROTOCOL,
        "positions": positions,
        "prompt_sha256": _sha256_text(str(prompt or "")),
        "generated_at": str(generated_at or ""),
    }


def validate_preset_sell_audit(
    audit: Any,
    *,
    code: str,
    position_snapshot: Any,
    position_interpretation: Any,
    position_interpretation_sha256: str,
) -> str | None:
    snapshot_error = validate_preset_strategy_snapshot(position_snapshot)
    if snapshot_error:
        return snapshot_error
    if not isinstance(audit, Mapping):
        return "缺少预设文字策略退出审计"
    expected_interpretation_sha256 = preset_strategy_interpretation_sha256(
        position_interpretation
    )
    if (
        not expected_interpretation_sha256
        or str(position_interpretation_sha256 or "")
        != expected_interpretation_sha256
    ):
        return "买入时文字策略结构化解释缺失或指纹不一致"
    if audit.get("schema_version") != PRESET_STRATEGY_AUDIT_SCHEMA_VERSION:
        return "预设文字策略退出审计版本不受支持"
    if str(audit.get("strategy_id") or "") != STRATEGY_SOURCE_PRESET_TEXT:
        return "预设文字策略退出审计身份不一致"
    if str(audit.get("prompt_protocol") or "") != PRESET_STRATEGY_PROMPT_PROTOCOL:
        return "预设文字策略退出Prompt协议不受支持"
    positions = audit.get("positions")
    recorded = positions.get(str(code or "")) if isinstance(positions, Mapping) else None
    if not isinstance(recorded, Mapping):
        return "本轮Prompt未装载该持仓的冻结文字策略"
    if str(recorded.get("text_sha256") or "") != str((position_snapshot or {}).get("text_sha256") or ""):
        return "预设文字策略退出快照指纹不一致"
    if str(recorded.get("interpretation_sha256") or "") != expected_interpretation_sha256:
        return "预设文字策略退出结构化解释指纹不一致"
    prompt_sha256 = str(audit.get("prompt_sha256") or "")
    if len(prompt_sha256) != 64:
        return "预设文字策略退出Prompt指纹缺失"
    return None


def format_frozen_preset_exit_section(
    position_contexts: Sequence[Mapping[str, Any]],
) -> str:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    invalid_codes: list[str] = []
    for item in position_contexts:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "")
        snapshot = item.get("snapshot")
        interpretation = item.get("interpretation")
        interpretation_sha256 = str(item.get("interpretation_sha256") or "")
        if not code:
            continue
        if (
            validate_preset_strategy_snapshot(snapshot)
            or not interpretation_sha256
            or interpretation_sha256
            != preset_strategy_interpretation_sha256(interpretation)
        ):
            invalid_codes.append(code)
            continue
        fingerprint = str((snapshot or {}).get("text_sha256") or "")
        group = groups.setdefault(
            (fingerprint, interpretation_sha256),
            {
                "snapshot": dict(snapshot),
                "codes": [],
                "interpretation": normalize_preset_strategy_interpretation(
                    interpretation
                ),
            },
        )
        group["codes"].append(code)

    sections: list[str] = []
    for (fingerprint, interpretation_sha256), group in groups.items():
        snapshot = group["snapshot"]
        interpretation = group["interpretation"] or {}
        interpretation_lines: list[str] = []
        labels = {
            "selection_rules": "选股",
            "entry_rules": "买入",
            "exit_rules": "退出",
            "position_rules": "仓位",
            "time_rules": "时间",
            "ambiguities": "歧义",
        }
        interpretation_lines.extend(
            f"- [{labels[key]}] {rule}"
            for key in PRESET_STRATEGY_INTERPRETATION_KEYS
            for rule in interpretation.get(key, [])
        )
        structured = "\n".join(dict.fromkeys(interpretation_lines)) or "- 按冻结原文保守解释；无法确认则HOLD。"
        sections.append(
            "预设文字历史持仓退出纪律（仅适用于代码"
            + "、".join(group["codes"])
            + f"，策略指纹 {fingerprint[:12]}，解释指纹 {interpretation_sha256[:12]}）：\n"
            + "买入时冻结原文：\n"
            + str(snapshot.get("text") or "")
            + "\n买入时完整结构化规则：\n"
            + structured
            + "\n- 只能用本节判断上述持仓的SELL/HOLD；若当前策略版本相同且需要加仓，必须原样返回本节结构化规则。当前新开仓策略及后来修改的文字不得覆盖此快照。"
        )
    if invalid_codes:
        sections.append(
            "预设文字历史持仓审计异常（代码"
            + "、".join(invalid_codes)
            + "）：缺少或损坏买入时策略快照，本轮只能HOLD，不得猜测卖出规则。"
        )
    return "\n\n".join(sections)
