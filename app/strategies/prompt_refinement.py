"""One-shot, capability-grounded refinement of fuzzy text strategies."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .rules import DEFAULT_FEATURE_REGISTRY, FeatureRegistry
from .rules.schema import canonical_json, sha256_text


JsonRequester = Callable[[list[dict[str, str]]], str | Mapping[str, Any]]
RAW_PROMPT_MARKER = "\n\n用户原始文字策略：\n"


class PromptRefinementResponseError(ValueError):
    """The model response is unsafe to freeze as a local strategy."""


class PromptRefinementParseError(PromptRefinementResponseError):
    """The model did not return one complete JSON strategy object."""


class PromptRefinementCoverageError(PromptRefinementResponseError):
    """The model omitted an explicitly requested, supported condition."""


class PromptRefinementContractError(PromptRefinementResponseError):
    """The model used a known-invalid strategy contract shape."""


@dataclass(frozen=True)
class PromptRefinement:
    refined_spec: dict[str, Any]
    refinement_prompt_sha256: str


def build_refinement_messages(
    raw_prompt: str,
    *,
    registry: FeatureRegistry = DEFAULT_FEATURE_REGISTRY,
) -> list[dict[str, str]]:
    capability_json = canonical_json(registry.capability_catalog())
    system = """你是 NiuOne 文字策略编译助手。你的任务只发生在策略创建阶段：把用户的模糊描述细化为一个可由本地规则引擎执行的 JSON 对象。激活后规则会冻结，运行期不得改写。

严格规则：
1. 只返回 JSON，不返回 Markdown、注释或解释；根对象必须是 strategy_spec。
2. 只能使用 capability_catalog 中的 feature_id、field、参数与 timeframe；禁止生成代码、公式字符串、SQL 或 eval。
3. 必须同时给出 selection、entry、exit 三阶段。若用户只给出买入条件，selection 默认复用买入条件；若卖出条件缺失，必须在 ambiguities 说明且采用保守、明确、可审计的退出条件。
4. 运行期不再调用模型。模糊语义必须在本次细化中转成 compare/all/any/not/crosses_above/crosses_below/for_bars 等确定规则，并把采用的解释写入 assumptions；无法安全量化的内容写入 ambiguities，不能生成 model_judgment 或让模型直接输出买卖动作。
5. KDJ 若用户只说“kdj 数值”，默认解释为 J 值，并把该假设写入 assumptions。
6. A 股固定股数必须是 100 的整数倍；默认 position 为 equity_pct=10、allow_add=false。
7. 默认使用已收盘日线：data_contract={timeframe:"1d",bar_status:"closed",freshness_seconds:129600}；默认缺数据时 hold、冲突时 exit_first、execution_mode 为 simulation。
8. candidate_limit 是完成 selection 过滤后保留的最大候选数，默认 60；max_new_buys_per_cycle 默认 2、最大 5。仓位规则仍受系统单票、总仓位和最低现金硬上限约束。
9. fact 节点仅允许以下运行时事实：account.cash、position.quantity、position.available_shares、position.avg_cost、position.pnl_pct、position.hold_days；它们只能用于 entry/exit，selection 只能使用行情特征。
10. 所有 feature 节点都可使用 offset_bars 引用历史已完成 K 线：0 表示当前求值 K 线，1 表示前一根，最大 499，特征自身预热与偏移合计不得超过 500 根。涉及“今日/昨日、上一日、前 N 根”的精确比较时必须使用 offset_bars，禁止用区间极值或收益率近似替代。例如今日最低价低于昨日最低价应比较两个 market.value 特征，parameters.field 均为 low，offset_bars 分别为 0 和 1；今日最高价高于昨日最高价同理使用 high；今日成交量与昨日成交量也应使用 volume 的 0/1 偏移精确比较。
11. 用户明确提出且 capability_catalog 可以表达的每一个条件，都必须同时出现在相应规则中。买入条件必须完整出现在 selection 和 entry；禁止从规则中删除条件，也禁止在 description、assumptions 或 ambiguities 中声称 offset_bars、今日/昨日 OHLCV 比较“无法实现”“受系统限制”或已被省略。
12. position 只能使用 {"type":"equity_pct","value":10,"allow_add":false} 或 {"type":"fixed_shares","value":100,"allow_add":false}。equity_pct 的 value 是百分数，10 表示 10%，禁止写成 {"equity_pct":0.1}。

strategy_spec 必须包含：schema_version=1、strategy_id、name、description、data_contract、rules、position、exit_quantity="all_available"、candidate_limit、max_new_buys_per_cycle、missing_data_policy="hold"、conflict_policy="exit_first"、execution_mode、assumptions、ambiguities。

feature 节点格式：{"type":"feature","feature_id":"technical.kdj","field":"j","parameters":{"n":9,"m1":3,"m2":3},"timeframe":"1d","offset_bars":0}
compare 节点格式：{"type":"compare","rule_id":"唯一ID","left":<feature或fact或arithmetic>,"operator":"lt|lte|gt|gte|eq|neq|between","right":数值或 between 的两元素数组}
all/any 节点格式：{"type":"all","rule_id":"唯一ID","children":[<规则1>,<规则2>]}，只能使用 children，禁止使用 conditions。
fact 节点格式：{"type":"fact","field":"position.pnl_pct"}
精确今日/昨日行情节点示例：{"type":"feature","feature_id":"market.value","field":"value","parameters":{"field":"low"},"timeframe":"1d","offset_bars":1}
"""
    user = (
        "capability_catalog="
        + capability_json
        + RAW_PROMPT_MARKER
        + str(raw_prompt or "").strip()
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_refinement_json(value: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload: Any = dict(value)
    else:
        text = str(value or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(text)
        except json.JSONDecodeError:
            match = re.search(r"\{", text)
            if match is None:
                raise PromptRefinementParseError("模型未返回文字策略 JSON 对象")
            try:
                payload, _ = decoder.raw_decode(text[match.start():])
            except json.JSONDecodeError as exc:
                raise PromptRefinementParseError(
                    "模型输出不是完整 JSON，可能在流式传输中被截断"
                ) from exc
    if not isinstance(payload, Mapping):
        raise PromptRefinementParseError("模型返回的文字策略必须是 JSON 对象")
    wrapped = payload.get("strategy_spec")
    spec = wrapped if isinstance(wrapped, Mapping) else payload
    if not isinstance(spec, Mapping):
        raise PromptRefinementParseError("模型返回缺少 strategy_spec")
    return dict(spec)


def _contains_term(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    for term in terms:
        if term.isascii():
            if re.search(rf"\b{re.escape(term.lower())}\b", lowered):
                return True
        elif term in text:
            return True
    return False


def _first_term_index(text: str, terms: tuple[str, ...]) -> int | None:
    lowered = text.lower()
    indexes: list[int] = []
    for term in terms:
        if term.isascii():
            match = re.search(rf"\b{re.escape(term.lower())}\b", lowered)
            if match is not None:
                indexes.append(match.start())
        else:
            index = text.find(term)
            if index >= 0:
                indexes.append(index)
    return min(indexes) if indexes else None


def _requested_current_previous_clauses(raw_prompt: str) -> dict[str, str]:
    current_terms = ("今日", "今天", "当日", "today")
    previous_terms = ("昨日", "昨天", "上一日", "前一日", "previous day", "yesterday")
    field_terms = {
        "low": ("最低价", "最低点", "low"),
        "high": ("最高价", "最高点", "high"),
        "volume": ("成交量", "volume"),
    }
    clauses = [
        item.strip()
        for item in re.split(r"[，,。；;\n]|(?:\band\b)|(?:且)|(?:并且)", raw_prompt, flags=re.IGNORECASE)
        if item.strip()
    ]
    requested: dict[str, str] = {}
    for clause in clauses:
        if not _contains_term(clause, previous_terms):
            continue
        for field_name, terms in field_terms.items():
            if not _contains_term(clause, terms):
                continue
            current_is_explicit = _contains_term(clause, current_terms)
            field_index = _first_term_index(clause, terms)
            previous_index = _first_term_index(clause, previous_terms)
            current_is_implied = (
                field_index is not None
                and previous_index is not None
                and field_index < previous_index
            )
            if current_is_explicit or current_is_implied:
                requested.setdefault(field_name, clause)
    return requested


def _requested_current_previous_fields(raw_prompt: str) -> set[str]:
    return set(_requested_current_previous_clauses(raw_prompt))


def _requested_current_previous_comparisons(raw_prompt: str) -> dict[str, str]:
    comparisons: dict[str, str] = {}
    less_terms = ("低于", "小于", "少于", "below", "less than")
    greater_terms = ("高于", "大于", "超过", "above", "greater than")
    for field_name, clause in _requested_current_previous_clauses(raw_prompt).items():
        if "<" in clause or _contains_term(clause, less_terms):
            comparisons[field_name] = "lt"
        elif ">" in clause or _contains_term(clause, greater_terms):
            comparisons[field_name] = "gt"
    return comparisons


def _feature_nodes(value: Any):
    if isinstance(value, Mapping):
        if str(value.get("type") or "").strip().lower() == "feature":
            yield value
        for item in value.values():
            yield from _feature_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _feature_nodes(item)


def _market_value_offsets(rule: Any, field_name: str) -> set[int]:
    offsets: set[int] = set()
    for node in _feature_nodes(rule):
        parameters = node.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        if str(node.get("feature_id") or "").strip().lower() != "market.value":
            continue
        if str(node.get("field") or "value").strip().lower() != "value":
            continue
        if str(parameters.get("field") or "").strip().lower() != field_name:
            continue
        raw_offset = node.get("offset_bars", 0)
        if isinstance(raw_offset, bool):
            continue
        try:
            offsets.add(int(raw_offset))
        except (TypeError, ValueError):
            continue
    return offsets


def _market_value_node(field_name: str, offset: int) -> dict[str, Any]:
    return {
        "type": "feature",
        "feature_id": "market.value",
        "field": "value",
        "parameters": {"field": field_name},
        "timeframe": "1d",
        "offset_bars": offset,
    }


def _current_previous_comparison_node(
    stage: str,
    field_name: str,
    operator: str,
) -> dict[str, Any]:
    return {
        "type": "compare",
        "rule_id": f"{stage}-required-{field_name}-current-vs-previous",
        "left": _market_value_node(field_name, 0),
        "operator": operator,
        "right": _market_value_node(field_name, 1),
    }


def _references_requested_temporal_field(
    value: Any,
    field_names: set[str],
) -> bool:
    for node in _feature_nodes(value):
        feature_id = str(node.get("feature_id") or "").strip().lower()
        parameters = node.get("parameters")
        parameters = parameters if isinstance(parameters, Mapping) else {}
        if (
            feature_id == "market.value"
            and str(parameters.get("field") or "").strip().lower() in field_names
        ):
            return True
        if feature_id == "volume.ratio" and "volume" in field_names:
            return True
        if feature_id == "price.range" and field_names.intersection({"low", "high"}):
            return True
    return False


def _remove_requested_temporal_conditions(
    value: Any,
    field_names: set[str],
) -> Any:
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    node_type = str(value.get("type") or "").strip().lower()
    if node_type in {
        "compare",
        "crosses_above",
        "crosses_below",
        "not",
        "for_bars",
    } and _references_requested_temporal_field(value, field_names):
        return None
    node = {str(key): copy.deepcopy(item) for key, item in value.items()}
    if node_type in {"all", "any"}:
        children = node.get("children")
        if not isinstance(children, list):
            return node
        retained = [
            _remove_requested_temporal_conditions(child, field_names)
            for child in children
        ]
        retained = [child for child in retained if child is not None]
        if not retained:
            return None
        if len(retained) == 1:
            return retained[0]
        node["children"] = retained
    return node


def _contains_omission_claim(value: Any) -> bool:
    text = str(value or "").lower()
    markers = (
        "omitted due to system limitations",
        "system limitations",
        "无法实现",
        "系统限制",
        "已省略",
        "被排除",
    )
    return any(marker in text for marker in markers)


def _remove_omission_claims(value: Any) -> Any:
    if isinstance(value, list):
        return [item for item in value if not _contains_omission_claim(item)]
    text = str(value or "")
    sentences = re.split(r"(?<=[。.!?！？])\s*", text)
    return " ".join(
        sentence.strip()
        for sentence in sentences
        if sentence.strip() and not _contains_omission_claim(sentence)
    )


def normalize_refinement_schema_aliases(
    refined_spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize one unambiguous model-only alias into the frozen rule schema."""

    normalized = copy.deepcopy(dict(refined_spec))
    repairs: list[str] = []

    def normalize_node(value: Any, path: str) -> Any:
        if isinstance(value, list):
            return [
                normalize_node(item, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        if not isinstance(value, Mapping):
            return value
        node = {str(key): copy.deepcopy(item) for key, item in value.items()}
        node_type = str(node.get("type") or "").strip().lower()
        if (
            node_type in {"all", "any"}
            and "children" not in node
            and isinstance(node.get("conditions"), list)
        ):
            node["children"] = node.pop("conditions")
            repairs.append(f"{path}.conditions→children")
        return {
            key: normalize_node(item, f"{path}.{key}")
            for key, item in node.items()
        }

    rules = normalized.get("rules")
    if isinstance(rules, Mapping):
        normalized["rules"] = {
            str(stage): normalize_node(rule, f"rules.{stage}")
            for stage, rule in rules.items()
        }
    if repairs:
        assumptions = normalized.get("assumptions")
        assumptions = list(assumptions) if isinstance(assumptions, list) else []
        assumptions.append(
            "本地结构规范化：" + "、".join(repairs) + "。"
        )
        normalized["assumptions"] = assumptions
    return normalized


def complete_requested_condition_coverage(
    raw_prompt: str,
    refined_spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministically restore unambiguous current/previous-bar conditions."""

    comparisons = _requested_current_previous_comparisons(str(raw_prompt or ""))
    completed = copy.deepcopy(dict(refined_spec))
    if not comparisons:
        return completed
    rules = completed.get("rules")
    if not isinstance(rules, dict):
        return completed
    added: list[str] = []
    labels = {"low": "最低价", "high": "最高价", "volume": "成交量"}
    operator_labels = {"lt": "低于", "gt": "高于"}
    for stage in ("selection", "entry"):
        existing = _remove_requested_temporal_conditions(
            rules.get(stage),
            set(comparisons),
        )
        additions = [
            _current_previous_comparison_node(stage, field_name, operator)
            for field_name, operator in sorted(comparisons.items())
        ]
        children = ([existing] if existing is not None else []) + additions
        rules[stage] = (
            children[0]
            if len(children) == 1
            else {
                "type": "all",
                "rule_id": f"{stage}-with-required-current-previous-conditions",
                "children": children,
            }
        )
        added.extend(
            f"{stage}.{labels[field_name]}{operator_labels[operator]}昨日值"
            for field_name, operator in sorted(comparisons.items())
        )
    if not added:
        return completed
    completed["description"] = _remove_omission_claims(
        completed.get("description")
    )
    completed["ambiguities"] = _remove_omission_claims(
        completed.get("ambiguities")
    )
    assumptions = _remove_omission_claims(completed.get("assumptions"))
    assumptions = list(assumptions) if isinstance(assumptions, list) else []
    assumptions.append(
        "本地完整性补全：根据原始文字策略精确加入 "
        + "、".join(added)
        + "，均使用 market.value 与 offset_bars=0/1。"
    )
    completed["assumptions"] = assumptions
    return completed


def validate_requested_condition_coverage(
    raw_prompt: str,
    refined_spec: Mapping[str, Any],
) -> None:
    """Fail closed when the model drops supported current/previous-bar rules."""

    requested = _requested_current_previous_fields(str(raw_prompt or ""))
    if not requested:
        return
    rules = refined_spec.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    missing: list[str] = []
    labels = {"low": "最低价", "high": "最高价", "volume": "成交量"}
    for stage in ("selection", "entry"):
        rule = rules.get(stage)
        for field_name in sorted(requested):
            offsets = _market_value_offsets(rule, field_name)
            if not {0, 1}.issubset(offsets):
                missing.append(f"{stage}.{labels[field_name]}缺少offset_bars=0/1")
    explanatory_text = " ".join(
        str(refined_spec.get(name) or "")
        for name in ("description", "assumptions", "ambiguities")
    ).lower()
    omission_markers = (
        "omitted due to system limitations",
        "system limitations",
        "无法实现",
        "系统限制",
        "已省略",
        "被排除",
    )
    if any(marker in explanatory_text for marker in omission_markers):
        missing.append("模型错误声称已支持的今日/昨日条件无法实现或已被省略")
    if missing:
        raise PromptRefinementCoverageError(
            "模型遗漏了用户明确要求且本地引擎支持的条件：" + "；".join(missing)
        )


def validate_refinement_contract_shape(refined_spec: Mapping[str, Any]) -> None:
    """Reject common high-impact schema drift before a draft is persisted."""

    position = refined_spec.get("position")
    if not isinstance(position, Mapping):
        raise PromptRefinementContractError(
            "模型返回的 position 必须是包含 type、value、allow_add 的对象"
        )
    keys = {str(key) for key in position}
    if not {"type", "value", "allow_add"}.issubset(keys):
        raise PromptRefinementContractError(
            "模型返回的 position 格式无效；必须使用 type/value/allow_add，"
            "equity_pct 的 value=10 表示10%"
        )
    unknown = sorted(keys - {"type", "value", "allow_add"})
    if unknown:
        raise PromptRefinementContractError(
            "模型返回的 position 包含不支持字段：" + ", ".join(unknown)
        )


def refine_prompt_once(
    raw_prompt: str,
    requester: JsonRequester,
    *,
    registry: FeatureRegistry = DEFAULT_FEATURE_REGISTRY,
) -> PromptRefinement:
    normalized = str(raw_prompt or "").strip()
    if not normalized:
        raise ValueError("文字策略Prompt不能为空")
    messages = build_refinement_messages(normalized, registry=registry)
    return finalize_prompt_refinement(messages, requester(messages))


def finalize_prompt_refinement(
    messages: list[dict[str, str]],
    response: str | Mapping[str, Any],
) -> PromptRefinement:
    """Validate a complete streamed or non-streamed refinement response."""

    refined_spec = parse_refinement_json(response)
    raw_prompt = ""
    for message in reversed(messages):
        if str(message.get("role") or "") != "user":
            continue
        content = str(message.get("content") or "")
        if RAW_PROMPT_MARKER in content:
            raw_prompt = content.split(RAW_PROMPT_MARKER, 1)[1]
            break
    refined_spec = normalize_refinement_schema_aliases(refined_spec)
    refined_spec = complete_requested_condition_coverage(
        raw_prompt,
        refined_spec,
    )
    validate_refinement_contract_shape(refined_spec)
    validate_requested_condition_coverage(raw_prompt, refined_spec)
    return PromptRefinement(
        refined_spec=refined_spec,
        refinement_prompt_sha256=sha256_text(canonical_json(messages)),
    )
