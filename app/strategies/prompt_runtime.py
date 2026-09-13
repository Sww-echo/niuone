"""Execute a frozen prompt-strategy version against explicit local facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .rules import (
    DEFAULT_FEATURE_REGISTRY,
    EvaluationContext,
    FeatureRegistry,
    FeatureRequest,
    build_action_intent,
    build_rule_evaluation_audit,
    evaluate_plan_stage,
    materialize_features,
)


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    digits = "".join(character for character in text if character.isdigit())
    return (
        f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        if len(digits) >= 8
        else ""
    )


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("T", " ").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        digits = "".join(character for character in text if character.isdigit())
        for length, pattern in (
            (14, "%Y%m%d%H%M%S"),
            (12, "%Y%m%d%H%M"),
            (8, "%Y%m%d"),
        ):
            if len(digits) >= length:
                try:
                    return datetime.strptime(digits[:length], pattern)
                except ValueError:
                    continue
    return None


def validate_strategy_data_contract(
    plan: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    data_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contract = ((plan.get("strategy") or {}).get("data_contract") or {})
    bar_status = str(contract.get("bar_status") or "closed")
    freshness_seconds = int(contract.get("freshness_seconds") or 129600)
    normalized_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    closed_rows = [
        row for row in normalized_rows
        if str(row.get("bar_status") or "closed") != "live"
    ]
    live_rows = [
        row for row in normalized_rows
        if str(row.get("bar_status") or "closed") == "live"
    ]
    context_supplied = data_context is not None
    context = dict(data_context or {})
    expected_closed_date = _date_text(context.get("expected_closed_date"))
    expected_live_date = _date_text(context.get("expected_live_date"))
    evaluated_at = _timestamp(context.get("evaluated_at"))
    selected_rows = closed_rows if bar_status == "closed" else live_rows
    latest = selected_rows[-1] if selected_rows else {}
    actual_date = _date_text(latest.get("date"))
    observed_at_text = str(
        latest.get("observed_at")
        or latest.get("quote_time")
        or context.get("observed_at")
        or ""
    )
    observed_at = _timestamp(observed_at_text)
    expected_date = expected_closed_date if bar_status == "closed" else expected_live_date
    errors: list[str] = []
    if context_supplied and not expected_date:
        errors.append(f"无法确定{bar_status}行情的预期交易日")
    if not actual_date:
        errors.append(f"缺少{bar_status}行情日期")
    if expected_date and actual_date and actual_date != expected_date:
        errors.append(f"行情日期{actual_date}不等于预期交易日{expected_date}")
    age_seconds: float | None = None
    if bar_status == "live":
        if observed_at is None:
            errors.append("实时行情缺少可解析的观测时间")
        elif evaluated_at is not None:
            try:
                age_seconds = (evaluated_at - observed_at).total_seconds()
            except TypeError:
                errors.append("实时行情与求值时间的时区不一致")
            else:
                if age_seconds < -300:
                    errors.append("实时行情时间晚于求值时间")
                elif age_seconds > freshness_seconds:
                    errors.append(
                        f"实时行情已过期{int(age_seconds)}秒，超过{freshness_seconds}秒"
                    )
    return {
        "status": "ok" if not errors else "stale",
        "bar_status": bar_status,
        "expected_date": expected_date,
        "actual_date": actual_date,
        "evaluated_at": str(context.get("evaluated_at") or ""),
        "observed_at": observed_at_text,
        "freshness_seconds": freshness_seconds,
        "age_seconds": age_seconds,
        "errors": errors,
    }


def stage_feature_requests(plan: Mapping[str, Any], stage: str) -> list[FeatureRequest]:
    raw_requests = (plan.get("required_features") or {}).get(str(stage or "")) or []
    return [
        FeatureRequest(
            feature_id=str(item.get("feature_id") or ""),
            field=str(item.get("field") or ""),
            parameters=dict(item.get("parameters") or {}),
            timeframe=str(item.get("timeframe") or "1d"),
            feature_version=str(item.get("feature_version") or ""),
            offset_bars=item.get("offset_bars", 0),
        )
        for item in raw_requests
        if isinstance(item, Mapping)
    ]


def _context_depth(node: Mapping[str, Any]) -> tuple[bool, int]:
    node_type = str(node.get("type") or "")
    if node_type in {"crosses_above", "crosses_below"}:
        return True, 1
    if node_type == "for_bars":
        child_cross, child_history = _context_depth(node.get("child") or {})
        return (
            child_cross,
            max(0, int(node.get("bars") or 0) - 1) + child_history,
        )
    children: list[Mapping[str, Any]] = []
    if node_type in {"all", "any"}:
        children = [item for item in node.get("children") or [] if isinstance(item, Mapping)]
    elif node_type == "not" and isinstance(node.get("child"), Mapping):
        children = [node["child"]]
    needs_previous = False
    history_depth = 0
    for child in children:
        child_previous, child_history = _context_depth(child)
        needs_previous = needs_previous or child_previous
        history_depth = max(history_depth, child_history)
    return needs_previous, history_depth


def materialize_stage_context(
    plan: Mapping[str, Any],
    stage: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    runtime_facts: Mapping[str, Any] | None = None,
    model_judgments: Mapping[str, Mapping[str, Any]] | None = None,
    data_context: Mapping[str, Any] | None = None,
    registry: FeatureRegistry = DEFAULT_FEATURE_REGISTRY,
) -> dict[str, Any]:
    normalized_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    data_contract = (plan.get("strategy") or {}).get("data_contract") or {}
    if str(data_contract.get("bar_status") or "closed") == "closed":
        normalized_rows = [
            row
            for row in normalized_rows
            if str(row.get("bar_status") or "closed") != "live"
        ]
    requests = stage_feature_requests(plan, stage)
    data_quality = validate_strategy_data_contract(
        plan,
        rows,
        data_context=data_context,
    )
    current = materialize_features(requests, normalized_rows, registry=registry)
    node = (((plan.get("strategy") or {}).get("rules") or {}).get(stage) or {})
    needs_previous, history_depth = _context_depth(node)
    previous = (
        materialize_features(requests, normalized_rows[:-1], registry=registry)
        if needs_previous and normalized_rows
        else {"facts": {}, "metadata": {}, "errors": []}
    )
    history: list[dict[str, Any]] = []
    history_metadata: list[dict[str, Any]] = []
    history_errors: list[dict[str, str]] = []
    if history_depth > 0:
        first_end = max(0, len(normalized_rows) - history_depth)
        for end in range(first_end, len(normalized_rows)):
            if end >= len(normalized_rows):
                break
            snapshot = materialize_features(
                requests,
                normalized_rows[:end],
                registry=registry,
            )
            history.append(dict(snapshot["facts"]))
            history_metadata.append(dict(snapshot["metadata"]))
            history_errors.extend(snapshot["errors"])
    as_of = str((normalized_rows[-1] if normalized_rows else {}).get("date") or "")
    if data_quality["status"] != "ok":
        current["facts"] = {key: None for key in current["facts"]}
        previous["facts"] = {key: None for key in previous["facts"]}
        history = [{key: None for key in snapshot} for snapshot in history]
        for metadata in current["metadata"].values():
            metadata["status"] = "stale"
    return {
        "context": EvaluationContext(
            facts=dict(current["facts"]),
            previous_facts=dict(previous["facts"]),
            history_facts=tuple(history),
            runtime_facts=dict(runtime_facts or {}),
            model_judgments={
                str(key): dict(value)
                for key, value in (model_judgments or {}).items()
                if isinstance(value, Mapping)
            },
            as_of=as_of,
        ),
        "feature_metadata": dict(current["metadata"]),
        "previous_feature_metadata": dict(previous["metadata"]),
        "history_feature_metadata": history_metadata,
        "feature_errors": [
            *current["errors"],
            *previous["errors"],
            *history_errors,
            *[
                {"feature_id": "data_contract", "error": error}
                for error in data_quality["errors"]
            ],
        ],
        "data_quality": data_quality,
    }


def evaluate_frozen_strategy_stage(
    version: Mapping[str, Any],
    stage: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    code: str,
    name: str = "",
    runtime_facts: Mapping[str, Any] | None = None,
    model_judgments: Mapping[str, Mapping[str, Any]] | None = None,
    data_context: Mapping[str, Any] | None = None,
    registry: FeatureRegistry = DEFAULT_FEATURE_REGISTRY,
) -> dict[str, Any]:
    plan = version.get("execution_plan")
    if not isinstance(plan, Mapping):
        raise ValueError("文字策略版本缺少执行计划")
    materialized = materialize_stage_context(
        plan,
        stage,
        rows,
        runtime_facts=runtime_facts,
        model_judgments=model_judgments,
        data_context=data_context,
        registry=registry,
    )
    context = materialized.pop("context")
    evaluation = evaluate_plan_stage(dict(plan), stage, context)
    intent = build_action_intent(
        dict(plan),
        evaluation,
        code=str(code or ""),
        name=str(name or ""),
    )
    audit = build_rule_evaluation_audit(
        strategy_version_id=str(version.get("version_id") or ""),
        plan=dict(plan),
        stage=stage,
        code=str(code or ""),
        fact_snapshot=context.facts,
        previous_facts=context.previous_facts,
        history_facts=context.history_facts,
        runtime_facts=context.runtime_facts,
        model_judgments=context.model_judgments,
        evaluation=evaluation,
        action_intent=intent,
        evaluated_at=context.as_of,
        data_quality=materialized.get("data_quality") or {},
    )
    return {
        "strategy_version_id": str(version.get("version_id") or ""),
        "plan_sha256": str(plan.get("plan_sha256") or ""),
        "evaluation": evaluation,
        "action_intent": intent,
        "audit": audit,
        **materialized,
    }


def score_prompt_selection(
    rows: list[dict[str, Any]],
    version: Mapping[str, Any],
    *,
    data_context: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not rows:
        return None
    recent = rows[-1]
    code = str(recent.get("symbol_code") or "")
    name = str(recent.get("stock_name") or "")
    result = evaluate_frozen_strategy_stage(
        version,
        "selection",
        rows,
        code=code,
        name=name,
        data_context=data_context,
    )
    status = str(result["evaluation"].get("status") or "unknown")
    root_evidence = str((result["evaluation"].get("root") or {}).get("evidence") or "")
    blocker = "" if status == "true" else (
        "文字策略选股条件不成立" if status == "false" else "文字策略选股数据不足"
    )
    facts = dict((result["audit"].get("replay_context") or {}).get("facts") or {})
    return {
        "strategy_id": "preset_text",
        "score": 10.0 if status == "true" else 0.0,
        "score_total": 10,
        "verdict": root_evidence or f"文字策略选股结果：{status}",
        "actionable": status == "true",
        "entry_threshold": 10.0,
        "hard_blockers": [blocker] if blocker else [],
        "distance_pct": None,
        "prompt_strategy_version_id": result["strategy_version_id"],
        "prompt_plan_sha256": result["plan_sha256"],
        "prompt_rule_status": status,
        "prompt_rule_evaluation": result["evaluation"],
        "prompt_rule_audit": result["audit"],
        "prompt_feature_metadata": result["feature_metadata"],
        "prompt_feature_errors": result["feature_errors"],
        "prompt_facts": facts,
    }


def resolve_prompt_order_shares(
    quantity_policy: Mapping[str, Any],
    *,
    price: float,
    total_equity: float,
    current_position_value: float = 0.0,
    existing_quantity: int = 0,
) -> dict[str, Any]:
    policy_type = str(quantity_policy.get("type") or "")
    allow_add = bool(quantity_policy.get("allow_add", False))
    if existing_quantity > 0 and not allow_add:
        return {"shares": 0, "error": "文字策略禁止加仓"}
    if price <= 0 or total_equity <= 0:
        return {"shares": 0, "error": "文字策略仓位计算缺少有效价格或权益"}
    if policy_type == "fixed_shares":
        shares = int(quantity_policy.get("value") or 0)
    elif policy_type == "equity_pct":
        target_value = total_equity * float(quantity_policy.get("value") or 0) / 100.0
        available_value = max(0.0, target_value - max(0.0, current_position_value))
        shares = int(available_value / price) // 100 * 100
    else:
        return {"shares": 0, "error": "文字策略仓位类型不受支持"}
    if shares <= 0 or shares % 100:
        return {"shares": 0, "error": "文字策略计算仓位不足100股"}
    return {"shares": shares, "error": ""}
