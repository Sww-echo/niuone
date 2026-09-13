"""Deterministic three-valued evaluation for compiled prompt rules."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RuleStatus(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EvaluationContext:
    facts: dict[str, Any]
    previous_facts: dict[str, Any] = field(default_factory=dict)
    history_facts: tuple[dict[str, Any], ...] = ()
    runtime_facts: dict[str, Any] = field(default_factory=dict)
    model_judgments: dict[str, dict[str, Any]] = field(default_factory=dict)
    as_of: str = ""


def _status_result(
    node: dict[str, Any],
    status: RuleStatus,
    *,
    evidence: str,
    observed: Any = None,
    expected: Any = None,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = {
        "rule_id": str(node.get("rule_id") or node.get("type") or "rule"),
        "type": str(node.get("type") or ""),
        "status": status.value,
        "evidence": evidence,
    }
    if observed is not None:
        result["observed"] = observed
    if expected is not None:
        result["expected"] = expected
    if children is not None:
        result["children"] = children
    return result


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _operand(
    node: dict[str, Any],
    context: EvaluationContext,
    *,
    previous: bool = False,
) -> tuple[Any, str]:
    node_type = str(node.get("type") or "")
    if node_type == "literal":
        return node.get("value"), repr(node.get("value"))
    if node_type == "feature":
        key = str(node.get("fact_key") or "")
        source = context.previous_facts if previous else context.facts
        return source.get(key), key
    if node_type == "fact":
        key = str(node.get("field") or "")
        return context.runtime_facts.get(key), key
    if node_type == "arithmetic":
        values: list[float] = []
        labels: list[str] = []
        for item in node.get("operands") or []:
            value, label = _operand(item, context, previous=previous)
            number = _finite_number(value)
            if number is None:
                return None, label
            values.append(number)
            labels.append(label)
        if len(values) < 2:
            return None, "arithmetic"
        operator = str(node.get("operator") or "")
        if operator == "add":
            result = sum(values)
        elif operator == "sub":
            result = values[0] - sum(values[1:])
        elif operator == "mul":
            result = math.prod(values)
        elif operator == "div":
            if any(value == 0 for value in values[1:]):
                return None, "division by zero"
            result = values[0]
            for value in values[1:]:
                result /= value
        elif operator == "pct_diff":
            if values[1] == 0:
                return None, "percentage base is zero"
            result = (values[0] / values[1] - 1.0) * 100.0
        elif operator == "min":
            result = min(values)
        elif operator == "max":
            result = max(values)
        else:
            return None, operator
        return result, f"{operator}({', '.join(labels)})"
    return None, node_type or "missing operand"


def _compare(operator: str, left: Any, right: Any) -> bool | None:
    if operator in {"eq", "neq"}:
        if left is None or right is None:
            return None
        return (left == right) if operator == "eq" else (left != right)
    left_number = _finite_number(left)
    if left_number is None:
        return None
    if operator == "between":
        if not isinstance(right, (list, tuple)) or len(right) != 2:
            return None
        lower = _finite_number(right[0])
        upper = _finite_number(right[1])
        return None if lower is None or upper is None else lower <= left_number <= upper
    right_number = _finite_number(right)
    if right_number is None:
        return None
    return {
        "lt": left_number < right_number,
        "lte": left_number <= right_number,
        "gt": left_number > right_number,
        "gte": left_number >= right_number,
    }.get(operator)


def evaluate_rule(node: dict[str, Any], context: EvaluationContext) -> dict[str, Any]:
    node_type = str(node.get("type") or "")
    if node_type == "compare":
        left, left_label = _operand(node.get("left") or {}, context)
        right, right_label = _operand(node.get("right") or {}, context)
        operator = str(node.get("operator") or "")
        matched = _compare(operator, left, right)
        if matched is None:
            return _status_result(
                node,
                RuleStatus.UNKNOWN,
                evidence=f"缺少可比较数据：{left_label} {operator} {right_label}",
                observed=left,
                expected=right,
            )
        return _status_result(
            node,
            RuleStatus.TRUE if matched else RuleStatus.FALSE,
            evidence=f"{left_label}={left} {operator} {right_label}={right}",
            observed=left,
            expected=right,
        )
    if node_type in {"all", "any"}:
        children = [evaluate_rule(child, context) for child in node.get("children") or []]
        statuses = [child["status"] for child in children]
        if node_type == "all":
            status = (
                RuleStatus.FALSE
                if RuleStatus.FALSE.value in statuses
                else RuleStatus.UNKNOWN
                if RuleStatus.UNKNOWN.value in statuses
                else RuleStatus.TRUE
            )
        else:
            status = (
                RuleStatus.TRUE
                if RuleStatus.TRUE.value in statuses
                else RuleStatus.UNKNOWN
                if RuleStatus.UNKNOWN.value in statuses
                else RuleStatus.FALSE
            )
        return _status_result(
            node,
            status,
            evidence=f"{node_type}({', '.join(statuses)})",
            children=children,
        )
    if node_type == "not":
        child = evaluate_rule(node.get("child") or {}, context)
        status = {
            RuleStatus.TRUE.value: RuleStatus.FALSE,
            RuleStatus.FALSE.value: RuleStatus.TRUE,
        }.get(child["status"], RuleStatus.UNKNOWN)
        return _status_result(
            node, status, evidence=f"not({child['status']})", children=[child]
        )
    if node_type in {"crosses_above", "crosses_below"}:
        left_now, left_label = _operand(node.get("left") or {}, context)
        right_now, right_label = _operand(node.get("right") or {}, context)
        left_previous, _ = _operand(node.get("left") or {}, context, previous=True)
        right_previous, _ = _operand(node.get("right") or {}, context, previous=True)
        values = [
            _finite_number(left_now),
            _finite_number(right_now),
            _finite_number(left_previous),
            _finite_number(right_previous),
        ]
        if any(value is None for value in values):
            return _status_result(
                node,
                RuleStatus.UNKNOWN,
                evidence="缺少当前或上一周期交叉数据",
            )
        if node_type == "crosses_above":
            matched = values[2] <= values[3] and values[0] > values[1]
        else:
            matched = values[2] >= values[3] and values[0] < values[1]
        return _status_result(
            node,
            RuleStatus.TRUE if matched else RuleStatus.FALSE,
            evidence=(
                f"{left_label}:{values[2]}→{values[0]}；"
                f"{right_label}:{values[3]}→{values[1]}"
            ),
        )
    if node_type == "for_bars":
        bars = int(node.get("bars") or 0)
        timeline = list(context.history_facts) + [context.facts]
        if len(timeline) < bars:
            return _status_result(
                node,
                RuleStatus.UNKNOWN,
                evidence=f"需要连续{bars}周期，只有{len(timeline)}周期事实",
            )
        selected_indices = range(len(timeline) - bars, len(timeline))
        children = [
            evaluate_rule(
                node.get("child") or {},
                EvaluationContext(
                    facts=timeline[index],
                    previous_facts=(timeline[index - 1] if index > 0 else {}),
                    history_facts=tuple(timeline[:index]),
                    runtime_facts=context.runtime_facts,
                    model_judgments=context.model_judgments,
                    as_of=context.as_of,
                ),
            )
            for index in selected_indices
        ]
        statuses = [child["status"] for child in children]
        status = (
            RuleStatus.FALSE
            if RuleStatus.FALSE.value in statuses
            else RuleStatus.UNKNOWN
            if RuleStatus.UNKNOWN.value in statuses
            else RuleStatus.TRUE
        )
        return _status_result(
            node,
            status,
            evidence=f"连续{bars}周期结果：{', '.join(statuses)}",
            children=children,
        )
    if node_type == "model_judgment":
        rule_id = str(node.get("rule_id") or "")
        judgment = context.model_judgments.get(rule_id)
        if not isinstance(judgment, dict):
            return _status_result(
                node,
                RuleStatus.UNKNOWN,
                evidence="尚无模型判断结果",
            )
        raw_status = str(judgment.get("status") or "").lower()
        confidence = _finite_number(judgment.get("confidence"))
        minimum = float(node.get("minimum_confidence") or 0.6)
        if raw_status not in {item.value for item in RuleStatus}:
            raw_status = RuleStatus.UNKNOWN.value
        if confidence is None or confidence < minimum:
            raw_status = RuleStatus.UNKNOWN.value
        return _status_result(
            node,
            RuleStatus(raw_status),
            evidence=str(judgment.get("evidence") or "模型未提供证据")[:1000],
            observed={"confidence": confidence},
            expected={"minimum_confidence": minimum},
        )
    return _status_result(
        node,
        RuleStatus.UNKNOWN,
        evidence=f"无法求值的规则类型：{node_type or 'missing'}",
    )


def evaluate_plan_stage(
    plan: dict[str, Any],
    stage: str,
    context: EvaluationContext,
) -> dict[str, Any]:
    normalized_stage = str(stage or "").strip().lower()
    node = ((plan.get("strategy") or {}).get("rules") or {}).get(normalized_stage)
    if not isinstance(node, dict):
        raise ValueError(f"execution plan does not contain stage: {normalized_stage}")
    result = evaluate_rule(node, context)
    return {
        "strategy_id": str((plan.get("strategy") or {}).get("strategy_id") or ""),
        "plan_sha256": str(plan.get("plan_sha256") or ""),
        "engine_version": str(plan.get("engine_version") or ""),
        "stage": normalized_stage,
        "as_of": context.as_of,
        "status": result["status"],
        "root": result,
    }
