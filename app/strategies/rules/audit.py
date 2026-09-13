"""Tamper-evident audit payloads and deterministic replay checks."""

from __future__ import annotations

from typing import Any

from .evaluator import EvaluationContext, evaluate_plan_stage
from .schema import sha256_json


def build_rule_evaluation_audit(
    *,
    strategy_version_id: str,
    plan: dict[str, Any],
    stage: str,
    code: str,
    fact_snapshot: dict[str, Any],
    evaluation: dict[str, Any],
    action_intent: dict[str, Any] | None,
    model_judgments: dict[str, Any] | None = None,
    previous_facts: dict[str, Any] | None = None,
    history_facts: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    runtime_facts: dict[str, Any] | None = None,
    data_quality: dict[str, Any] | None = None,
    evaluated_at: str = "",
) -> dict[str, Any]:
    replay_context = {
        "facts": dict(fact_snapshot),
        "previous_facts": dict(previous_facts or {}),
        "history_facts": [dict(item) for item in (history_facts or ())],
        "runtime_facts": dict(runtime_facts or {}),
        "model_judgments": dict(model_judgments or {}),
        "data_quality": dict(data_quality or {}),
    }
    payload = {
        "schema_version": 1,
        "strategy_version_id": str(strategy_version_id or ""),
        "plan_sha256": str(plan.get("plan_sha256") or ""),
        "engine_version": str(plan.get("engine_version") or ""),
        "stage": str(stage or ""),
        "code": str(code or ""),
        "evaluated_at": str(evaluated_at or evaluation.get("as_of") or ""),
        "replay_context": replay_context,
        "replay_context_sha256": sha256_json(replay_context),
        "evaluation": dict(evaluation),
        "action_intent": dict(action_intent) if action_intent else None,
    }
    payload["audit_sha256"] = sha256_json(payload)
    return payload


def replay_rule_evaluation_audit(
    audit: dict[str, Any],
    *,
    plan: dict[str, Any],
) -> dict[str, Any]:
    recorded = dict(audit)
    recorded_sha256 = str(recorded.pop("audit_sha256", ""))
    if recorded_sha256 != sha256_json(recorded):
        return {"ok": False, "error": "audit_fingerprint_mismatch"}
    if str(recorded.get("plan_sha256") or "") != str(plan.get("plan_sha256") or ""):
        return {"ok": False, "error": "execution_plan_mismatch"}
    replay_context = (
        recorded.get("replay_context")
        if isinstance(recorded.get("replay_context"), dict)
        else {}
    )
    if str(recorded.get("replay_context_sha256") or "") != sha256_json(replay_context):
        return {"ok": False, "error": "replay_context_mismatch"}
    facts = replay_context.get("facts") if isinstance(replay_context.get("facts"), dict) else {}
    previous_facts = (
        replay_context.get("previous_facts")
        if isinstance(replay_context.get("previous_facts"), dict)
        else {}
    )
    history_facts = replay_context.get("history_facts")
    if not isinstance(history_facts, list) or not all(
        isinstance(item, dict) for item in history_facts
    ):
        return {"ok": False, "error": "invalid_history_context"}
    runtime_facts = (
        replay_context.get("runtime_facts")
        if isinstance(replay_context.get("runtime_facts"), dict)
        else {}
    )
    model_judgments = (
        replay_context.get("model_judgments")
        if isinstance(replay_context.get("model_judgments"), dict)
        else {}
    )
    replayed = evaluate_plan_stage(
        plan,
        str(recorded.get("stage") or ""),
        EvaluationContext(
            facts=dict(facts),
            previous_facts=dict(previous_facts),
            history_facts=tuple(dict(item) for item in history_facts),
            runtime_facts=dict(runtime_facts),
            model_judgments=dict(model_judgments),
            as_of=str(recorded.get("evaluated_at") or ""),
        ),
    )
    expected = recorded.get("evaluation") or {}
    return {
        "ok": replayed == expected,
        "error": "" if replayed == expected else "evaluation_replay_mismatch",
        "recorded": expected,
        "replayed": replayed,
    }
