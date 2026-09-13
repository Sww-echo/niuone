"""Compile an AI-refined prompt strategy into an immutable execution plan."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .features import (
    DEFAULT_FEATURE_REGISTRY,
    FeatureRegistry,
    FeatureRequest,
    normalize_feature_request,
)
from .schema import (
    BAR_STATUSES,
    CONFLICT_POLICIES,
    EXECUTION_MODES,
    MISSING_DATA_POLICIES,
    PROMPT_EXECUTION_PLAN_SCHEMA_VERSION,
    PROMPT_RULE_ENGINE_VERSION,
    PROMPT_STRATEGY_SPEC_SCHEMA_VERSION,
    RULE_STAGES,
    TIMEFRAMES,
    sha256_json,
)


BOOLEAN_NODE_TYPES = frozenset({"all", "any", "not"})
COMPARE_OPERATORS = frozenset({"lt", "lte", "gt", "gte", "eq", "neq", "between"})
ARITHMETIC_OPERATORS = frozenset({"add", "sub", "mul", "div", "pct_diff", "min", "max"})
EVENT_NODE_TYPES = frozenset({"crosses_above", "crosses_below", "for_bars"})
RUNTIME_FACT_FIELDS = frozenset({
    "account.cash",
    "position.quantity",
    "position.available_shares",
    "position.avg_cost",
    "position.pnl_pct",
    "position.hold_days",
})


class CompileError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(str(item) for item in errors if str(item or ""))
        super().__init__("；".join(self.errors) or "策略编译失败")


def _reject_unknown_keys(
    value: Mapping[str, Any],
    allowed: set[str] | frozenset[str],
    *,
    path: str,
) -> None:
    unknown = sorted(str(key) for key in set(value) - set(allowed))
    if unknown:
        raise CompileError([f"{path} 包含未支持字段: {', '.join(unknown)}"])


def _contains_runtime_model_node(node: Mapping[str, Any]) -> bool:
    if str(node.get("type") or "") == "model_judgment":
        return True
    nested: list[Any] = []
    nested.extend(node.get("children") or [])
    nested.extend(node.get("operands") or [])
    nested.extend([node.get("child"), node.get("left"), node.get("right")])
    return any(
        _contains_runtime_model_node(item)
        for item in nested
        if isinstance(item, Mapping)
    )


def _runtime_fact_fields(node: Mapping[str, Any]) -> set[str]:
    fields = (
        {str(node.get("field") or "")}
        if str(node.get("type") or "") == "fact"
        else set()
    )
    nested: list[Any] = []
    nested.extend(node.get("children") or [])
    nested.extend(node.get("operands") or [])
    nested.extend([node.get("child"), node.get("left"), node.get("right")])
    for item in nested:
        if isinstance(item, Mapping):
            fields.update(_runtime_fact_fields(item))
    return fields


def _history_extension(node: Mapping[str, Any]) -> int:
    node_type = str(node.get("type") or "")
    own = 1 if node_type in {"crosses_above", "crosses_below"} else 0
    if node_type == "for_bars":
        child = node.get("child") if isinstance(node.get("child"), Mapping) else {}
        return max(0, int(node.get("bars") or 0) - 1) + _history_extension(child)
    nested: list[Any] = []
    nested.extend(node.get("children") or [])
    nested.extend([node.get("child"), node.get("left"), node.get("right")])
    return max(
        [own]
        + [
            _history_extension(item)
            for item in nested
            if isinstance(item, Mapping)
        ]
    )


def _as_mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CompileError([f"{path} 必须是对象"])
    return value


def _normalize_literal(value: Any, *, path: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return {"type": "literal", "value": value}
    if isinstance(value, (list, tuple)) and all(
        item is None or isinstance(item, (str, bool, int, float))
        for item in value
    ):
        return {"type": "literal", "value": list(value)}
    raise CompileError([f"{path} 不是受支持的常量"])


def _parse_feature_request(
    value: Mapping[str, Any],
    *,
    default_timeframe: str,
    registry: FeatureRegistry,
    path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_feature_id = str(value.get("feature_id") or value.get("name") or "").strip()
    raw_field = str(value.get("field") or "value").strip().lower()
    request = FeatureRequest(
        feature_id=raw_feature_id,
        field=raw_field,
        parameters=dict(value.get("parameters") or {}),
        timeframe=str(value.get("timeframe") or default_timeframe),
        offset_bars=value.get("offset_bars", 0),
    )
    try:
        definition, normalized, fact_key = normalize_feature_request(registry, request)
    except (KeyError, ValueError) as exc:
        raise CompileError([f"{path}: {exc}"]) from exc
    request_payload = {
        "feature_id": normalized.feature_id,
        "feature_version": definition.version,
        "field": normalized.field,
        "parameters": dict(normalized.parameters),
        "timeframe": normalized.timeframe,
        "offset_bars": normalized.offset_bars,
        "fact_key": fact_key,
        "min_bars": (
            definition.required_bars(normalized.parameters)
            + normalized.offset_bars
        ),
    }
    node = {
        "type": "feature",
        **request_payload,
    }
    return node, request_payload


def _normalize_node(
    value: Any,
    *,
    path: str,
    default_timeframe: str,
    registry: FeatureRegistry,
    required: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    raw = _as_mapping(value, path=path)
    node_type = str(raw.get("type") or "").strip().lower()
    if node_type == "predicate":
        node_type = "compare"
    rule_id = str(raw.get("rule_id") or path.replace(".", "-").replace("[", "-").replace("]", ""))

    if node_type == "literal":
        _reject_unknown_keys(raw, {"type", "value"}, path=path)
        literal = raw.get("value")
        if literal is not None and not isinstance(literal, (str, bool, int, float)):
            raise CompileError([f"{path}.value 不是受支持的常量"])
        return {"type": "literal", "value": literal}

    if node_type == "feature":
        _reject_unknown_keys(
            raw,
            {
                "type",
                "feature_id",
                "name",
                "field",
                "parameters",
                "timeframe",
                "offset_bars",
            },
            path=path,
        )
        node, request = _parse_feature_request(
            raw,
            default_timeframe=default_timeframe,
            registry=registry,
            path=path,
        )
        required[request["fact_key"]] = request
        return node

    if node_type == "fact":
        _reject_unknown_keys(raw, {"type", "field"}, path=path)
        field_name = str(raw.get("field") or "").strip()
        if field_name not in RUNTIME_FACT_FIELDS:
            raise CompileError([
                f"{path}.field 不是受支持的运行时事实: {field_name or 'missing'}"
            ])
        return {"type": "fact", "field": field_name}

    if node_type == "compare":
        _reject_unknown_keys(
            raw,
            {"type", "rule_id", "operator", "left", "right"},
            path=path,
        )
        operator = str(raw.get("operator") or "").strip().lower()
        if operator not in COMPARE_OPERATORS:
            raise CompileError([f"{path}.operator 不受支持: {operator}"])
        left = _normalize_node(
            raw.get("left"),
            path=f"{path}.left",
            default_timeframe=default_timeframe,
            registry=registry,
            required=required,
        )
        right = _normalize_node(
            _normalize_literal(raw.get("right"), path=f"{path}.right"),
            path=f"{path}.right",
            default_timeframe=default_timeframe,
            registry=registry,
            required=required,
        )
        return {
            "type": "compare",
            "rule_id": rule_id,
            "operator": operator,
            "left": left,
            "right": right,
        }

    if node_type in {"all", "any"}:
        _reject_unknown_keys(
            raw,
            {"type", "rule_id", "children"},
            path=path,
        )
        children = raw.get("children")
        if not isinstance(children, list) or not children:
            raise CompileError([f"{path}.children 必须是非空数组"])
        return {
            "type": node_type,
            "rule_id": rule_id,
            "children": [
                _normalize_node(
                    child,
                    path=f"{path}.children[{index}]",
                    default_timeframe=default_timeframe,
                    registry=registry,
                    required=required,
                )
                for index, child in enumerate(children)
            ],
        }

    if node_type == "not":
        _reject_unknown_keys(
            raw,
            {"type", "rule_id", "child"},
            path=path,
        )
        return {
            "type": "not",
            "rule_id": rule_id,
            "child": _normalize_node(
                raw.get("child"),
                path=f"{path}.child",
                default_timeframe=default_timeframe,
                registry=registry,
                required=required,
            ),
        }

    if node_type == "arithmetic":
        _reject_unknown_keys(
            raw,
            {"type", "rule_id", "operator", "operands"},
            path=path,
        )
        operator = str(raw.get("operator") or "").strip().lower()
        if operator not in ARITHMETIC_OPERATORS:
            raise CompileError([f"{path}.operator 不受支持: {operator}"])
        operands = raw.get("operands")
        if not isinstance(operands, list) or len(operands) < 2:
            raise CompileError([f"{path}.operands 必须是至少包含两个元素的数组"])
        return {
            "type": "arithmetic",
            "rule_id": rule_id,
            "operator": operator,
            "operands": [
                _normalize_node(
                    _normalize_literal(item, path=f"{path}.operands[{index}]"),
                    path=f"{path}.operands[{index}]",
                    default_timeframe=default_timeframe,
                    registry=registry,
                    required=required,
                )
                for index, item in enumerate(operands)
            ],
        }

    if node_type in {"crosses_above", "crosses_below"}:
        _reject_unknown_keys(
            raw,
            {"type", "rule_id", "left", "right"},
            path=path,
        )
        return {
            "type": node_type,
            "rule_id": rule_id,
            "left": _normalize_node(
                raw.get("left"),
                path=f"{path}.left",
                default_timeframe=default_timeframe,
                registry=registry,
                required=required,
            ),
            "right": _normalize_node(
                _normalize_literal(raw.get("right"), path=f"{path}.right"),
                path=f"{path}.right",
                default_timeframe=default_timeframe,
                registry=registry,
                required=required,
            ),
        }

    if node_type == "for_bars":
        _reject_unknown_keys(
            raw,
            {"type", "rule_id", "bars", "child"},
            path=path,
        )
        try:
            bars = int(raw.get("bars"))
        except (TypeError, ValueError) as exc:
            raise CompileError([f"{path}.bars 必须是整数"]) from exc
        if not 2 <= bars <= 250:
            raise CompileError([f"{path}.bars 必须在2到250之间"])
        return {
            "type": "for_bars",
            "rule_id": rule_id,
            "bars": bars,
            "child": _normalize_node(
                raw.get("child"),
                path=f"{path}.child",
                default_timeframe=default_timeframe,
                registry=registry,
                required=required,
            ),
        }

    if node_type == "model_judgment":
        _reject_unknown_keys(
            raw,
            {
                "type",
                "rule_id",
                "instruction",
                "required_features",
                "minimum_confidence",
            },
            path=path,
        )
        instruction = " ".join(str(raw.get("instruction") or "").split()).strip()
        if not instruction:
            raise CompileError([f"{path}.instruction 不能为空"])
        requested = raw.get("required_features")
        if not isinstance(requested, list) or not requested:
            raise CompileError([f"{path}.required_features 必须是非空数组"])
        feature_keys: list[str] = []
        for index, request_value in enumerate(requested):
            request_mapping = _as_mapping(
                request_value, path=f"{path}.required_features[{index}]"
            )
            _node, request = _parse_feature_request(
                request_mapping,
                default_timeframe=default_timeframe,
                registry=registry,
                path=f"{path}.required_features[{index}]",
            )
            required[request["fact_key"]] = request
            feature_keys.append(request["fact_key"])
        return {
            "type": "model_judgment",
            "rule_id": rule_id,
            "instruction": instruction[:1000],
            "required_fact_keys": feature_keys,
            "minimum_confidence": max(
                0.0, min(1.0, float(raw.get("minimum_confidence") or 0.6))
            ),
        }

    raise CompileError([f"{path}.type 不受支持: {node_type or 'missing'}"])


def _normalize_position(value: Any) -> dict[str, Any]:
    raw = _as_mapping(value, path="position")
    _reject_unknown_keys(raw, {"type", "value", "allow_add"}, path="position")
    position_type = str(raw.get("type") or "equity_pct").strip().lower()
    if position_type not in {"equity_pct", "fixed_shares"}:
        raise CompileError([f"position.type 不受支持: {position_type}"])
    normalized: dict[str, Any] = {
        "type": position_type,
        "allow_add": bool(raw.get("allow_add", False)),
    }
    if position_type == "equity_pct":
        try:
            value_number = float(raw.get("value"))
        except (TypeError, ValueError) as exc:
            raise CompileError(["position.value 必须是权益百分比"]) from exc
        if not 0 < value_number <= 100:
            raise CompileError(["position.value 必须大于0且不超过100"])
        normalized["value"] = value_number
    elif position_type == "fixed_shares":
        try:
            shares = int(raw.get("value"))
        except (TypeError, ValueError) as exc:
            raise CompileError(["position.value 必须是股数"]) from exc
        if shares <= 0 or shares % 100:
            raise CompileError(["固定股数必须是正的100股整数倍"])
        normalized["value"] = shares
    return normalized


def compile_strategy_spec(
    value: Mapping[str, Any],
    *,
    registry: FeatureRegistry = DEFAULT_FEATURE_REGISTRY,
) -> dict[str, Any]:
    spec = _as_mapping(value, path="strategy")
    _reject_unknown_keys(
        spec,
        {
            "schema_version",
            "strategy_id",
            "name",
            "description",
            "data_contract",
            "rules",
            "position",
            "exit_quantity",
            "candidate_limit",
            "max_new_buys_per_cycle",
            "missing_data_policy",
            "conflict_policy",
            "execution_mode",
            "assumptions",
            "ambiguities",
        },
        path="strategy",
    )
    if spec.get("schema_version") != PROMPT_STRATEGY_SPEC_SCHEMA_VERSION:
        raise CompileError(["文字策略结构化规则版本不受支持"])
    data_contract = _as_mapping(spec.get("data_contract"), path="data_contract")
    _reject_unknown_keys(
        data_contract,
        {"timeframe", "bar_status", "freshness_seconds"},
        path="data_contract",
    )
    timeframe = str(data_contract.get("timeframe") or "1d").strip().lower()
    bar_status = str(data_contract.get("bar_status") or "closed").strip().lower()
    if timeframe not in TIMEFRAMES:
        raise CompileError([f"data_contract.timeframe 不受支持: {timeframe}"])
    if bar_status not in BAR_STATUSES:
        raise CompileError([f"data_contract.bar_status 不受支持: {bar_status}"])
    try:
        freshness_seconds = int(
            129600
            if data_contract.get("freshness_seconds") is None
            else data_contract.get("freshness_seconds")
        )
    except (TypeError, ValueError) as exc:
        raise CompileError(["data_contract.freshness_seconds 必须是整数"]) from exc
    if freshness_seconds <= 0 or freshness_seconds > 31 * 86400:
        raise CompileError(["data_contract.freshness_seconds 超出支持范围"])

    raw_rules = _as_mapping(spec.get("rules"), path="rules")
    _reject_unknown_keys(raw_rules, set(RULE_STAGES), path="rules")
    rules: dict[str, dict[str, Any]] = {}
    required_features: dict[str, list[dict[str, Any]]] = {}
    for stage in RULE_STAGES:
        required: dict[str, dict[str, Any]] = {}
        rules[stage] = _normalize_node(
            raw_rules.get(stage),
            path=f"rules.{stage}",
            default_timeframe=timeframe,
            registry=registry,
            required=required,
        )
        required_features[stage] = [required[key] for key in sorted(required)]
    if any(_contains_runtime_model_node(node) for node in rules.values()):
        raise CompileError(["当前文字策略只允许创建阶段调用模型，运行规则不能包含 model_judgment"])
    selection_runtime_facts = sorted(_runtime_fact_fields(rules["selection"]))
    if selection_runtime_facts:
        raise CompileError([
            "selection 阶段不能使用账户或持仓事实: "
            + ", ".join(selection_runtime_facts)
        ])

    missing_policy = str(spec.get("missing_data_policy") or "hold").strip().lower()
    conflict_policy = str(spec.get("conflict_policy") or "exit_first").strip().lower()
    execution_mode = str(spec.get("execution_mode") or "recommend_only").strip().lower()
    if missing_policy not in MISSING_DATA_POLICIES:
        raise CompileError([f"missing_data_policy 不受支持: {missing_policy}"])
    if conflict_policy not in CONFLICT_POLICIES:
        raise CompileError([f"conflict_policy 不受支持: {conflict_policy}"])
    if execution_mode not in EXECUTION_MODES:
        raise CompileError([f"execution_mode 不受支持: {execution_mode}"])
    try:
        candidate_limit = int(
            60 if spec.get("candidate_limit") is None else spec.get("candidate_limit")
        )
        max_new_buys = int(
            2
            if spec.get("max_new_buys_per_cycle") is None
            else spec.get("max_new_buys_per_cycle")
        )
    except (TypeError, ValueError) as exc:
        raise CompileError(["候选数量和单轮买入数量必须是整数"]) from exc
    if not 1 <= candidate_limit <= 100:
        raise CompileError(["candidate_limit 必须在1到100之间"])
    if not 0 <= max_new_buys <= 5:
        raise CompileError(["max_new_buys_per_cycle 必须在0到5之间"])

    exit_quantity = str(spec.get("exit_quantity") or "all_available").strip().lower()
    if exit_quantity != "all_available":
        raise CompileError(["exit_quantity 当前只支持 all_available"])
    assumptions = spec.get("assumptions")
    ambiguities = spec.get("ambiguities")
    if not isinstance(assumptions, list):
        raise CompileError(["assumptions 必须是数组"])
    if not isinstance(ambiguities, list):
        raise CompileError(["ambiguities 必须是数组"])

    normalized_spec = {
        "schema_version": PROMPT_STRATEGY_SPEC_SCHEMA_VERSION,
        "strategy_id": str(spec.get("strategy_id") or "prompt-strategy").strip()[:120],
        "name": str(spec.get("name") or "文字策略").strip()[:200],
        "description": str(spec.get("description") or "").strip()[:2000],
        "data_contract": {
            "timeframe": timeframe,
            "bar_status": bar_status,
            "freshness_seconds": freshness_seconds,
        },
        "rules": rules,
        "position": _normalize_position(spec.get("position")),
        "exit_quantity": exit_quantity,
        "candidate_limit": candidate_limit,
        "max_new_buys_per_cycle": max_new_buys,
        "missing_data_policy": missing_policy,
        "conflict_policy": conflict_policy,
        "execution_mode": execution_mode,
        "assumptions": [str(item)[:500] for item in assumptions if str(item).strip()][:20],
        "ambiguities": [str(item)[:500] for item in ambiguities if str(item).strip()][:20],
    }
    stage_requirements = {
        stage: {
            "minimum_bars": max(
                [int(item.get("min_bars") or 1) for item in required_features[stage]]
                or [1]
            )
            + _history_extension(rules[stage]),
            "history_extension": _history_extension(rules[stage]),
        }
        for stage in RULE_STAGES
    }
    oversized_stages = [
        stage
        for stage, requirement in stage_requirements.items()
        if int(requirement["minimum_bars"]) > 500
    ]
    if oversized_stages:
        raise CompileError([
            "以下阶段所需历史超过500根K线: " + ", ".join(oversized_stages)
        ])
    plan = {
        "schema_version": PROMPT_EXECUTION_PLAN_SCHEMA_VERSION,
        "engine_version": PROMPT_RULE_ENGINE_VERSION,
        "strategy": normalized_spec,
        "required_features": required_features,
        "stage_requirements": stage_requirements,
    }
    plan["plan_sha256"] = sha256_json(plan)
    return plan
