"""Bounded, evidence-aware tuning from durable post-exit observations.

Structural stops, account risk budgets, and position caps are deliberately
frozen. Only audited soft-exit, replacement, and re-entry grid values move.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


EXIT_FEEDBACK_ALGORITHM_VERSION = "niuone-exit-feedback-v2"
EXIT_FEEDBACK_DEFAULT_MIN_SAMPLES = 30
EXIT_FEEDBACK_DEFAULT_MIN_MONTHS = 3
EXIT_FEEDBACK_DEFAULT_COOLDOWN_SAMPLES = 10
EXIT_FEEDBACK_ROLLBACK_MIN_SAMPLES = 20
EXIT_FEEDBACK_MAX_WINDOW_SAMPLES = 120
EXIT_FEEDBACK_CONFIDENCE_LEVEL = 0.90
EXIT_FEEDBACK_CONFIDENCE_Z = 1.6448536269514722
EXIT_FEEDBACK_SOFT_MIN_EFFECT_PCT = 1.0
EXIT_FEEDBACK_REPLACEMENT_MIN_EFFECT_PCT = 0.75
EXIT_FEEDBACK_REENTRY_MIN_EFFECT_PCT = 1.5

EXIT_FEEDBACK_DEFAULT_PARAMETERS: dict[str, int | float] = {
    "soft_exit_confirmations": 2,
    "soft_exit_reduce_ratio": 0.5,
    "replacement_priority_margin": 3.0,
    "reentry_volume_ratio": 1.0,
    "reentry_amount_percentile": 60.0,
}

EXIT_FEEDBACK_PARAMETER_BOUNDS: dict[str, tuple[int | float, ...]] = {
    "soft_exit_confirmations": (2, 3),
    "soft_exit_reduce_ratio": (0.25, 0.5),
    "replacement_priority_margin": (3.0, 4.0, 5.0),
    "reentry_volume_ratio": (0.9, 1.0, 1.1),
    "reentry_amount_percentile": (55.0, 60.0, 65.0),
}

SOFT_EXIT_RULES = frozenset({
    "no_progress",
    "profit_protection",
    "sector_retreat",
    "sell_score",
    "position_adjust",
})


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_number(value: Any) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def _bounded_value(name: str, value: Any) -> int | float:
    allowed = EXIT_FEEDBACK_PARAMETER_BOUNDS[name]
    numeric = _number(value, float(EXIT_FEEDBACK_DEFAULT_PARAMETERS[name]))
    selected = min(allowed, key=lambda item: abs(float(item) - numeric))
    if isinstance(EXIT_FEEDBACK_DEFAULT_PARAMETERS[name], int):
        return int(selected)
    return float(selected)


def normalize_exit_feedback_parameters(
    value: Mapping[str, Any] | None,
) -> dict[str, int | float]:
    """Return a complete parameter set restricted to the audited grid."""
    source = value if isinstance(value, Mapping) else {}
    return {
        name: _bounded_value(name, source.get(name, default))
        for name, default in EXIT_FEEDBACK_DEFAULT_PARAMETERS.items()
    }


def effective_exit_feedback_parameters(
    policy: Mapping[str, Any] | None,
) -> dict[str, int | float]:
    """Resolve runtime parameters, failing closed to legacy defaults."""
    if not isinstance(policy, Mapping) or not bool(policy.get("enabled")):
        return dict(EXIT_FEEDBACK_DEFAULT_PARAMETERS)
    return normalize_exit_feedback_parameters(
        policy.get("parameters")
        if isinstance(policy.get("parameters"), Mapping)
        else None
    )


def _rate(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [int(bool(row.get(field))) for row in rows if row.get(field) is not None]
    return sum(values) / len(values) if values else None


def _average(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [
        value
        for row in rows
        if (value := _optional_number(row.get(field))) is not None
    ]
    return sum(values) / len(values) if values else None


def _row_notional(row: Mapping[str, Any]) -> float:
    explicit = _optional_number(row.get("sell_notional"))
    if explicit is not None and explicit > 0:
        return explicit
    price = _number(row.get("sell_price"), 0.0)
    shares = _number(row.get("shares"), 0.0)
    return price * shares if price > 0 and shares > 0 else 1.0


def _cluster_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    code = str(row.get("code") or "")
    day = str(row.get("sell_time") or row.get("observed_at") or "")[:10]
    replacement = str(row.get("replacement_target_code") or "")
    version = str(int(_number(row.get("feedback_policy_version"), 0)))
    if code and day:
        return code, day, replacement, version
    return str(row.get("trade_key") or row.get("audit_key") or id(row)),


def _cluster_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_cluster_key(row), []).append(row)
    result: list[dict[str, Any]] = []
    numeric_fields = (
        "close_return_pct",
        "mae_pct",
        "replacement_regret_pct",
        "future_return_pct",
    )
    binary_fields = (
        "sell_fly",
        "avoided_loss",
        "replacement_regret",
        "eligible",
        "executed",
        "reclaim_passed",
        "volume_supportive",
        "thesis_valid",
    )
    for members in grouped.values():
        merged = dict(members[-1])
        weights = [_row_notional(row) for row in members]
        merged["sell_notional"] = sum(weights)
        merged["shares"] = sum(max(0, int(_number(row.get("shares"), 0))) for row in members)
        for field in numeric_fields:
            pairs = [
                (value, weight)
                for row, weight in zip(members, weights)
                if (value := _optional_number(row.get(field))) is not None
            ]
            if pairs:
                merged[field] = sum(value * weight for value, weight in pairs) / sum(
                    weight for _, weight in pairs
                )
        for field in binary_fields:
            values = [row.get(field) for row in members if row.get(field) is not None]
            if values:
                merged[field] = int(any(bool(value) for value in values))
        result.append(merged)
    return result


def _weighted_statistics(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, float | int | None]:
    values: list[tuple[float, float]] = []
    for row in rows:
        value = _optional_number(row.get(field))
        if value is not None:
            values.append((value, max(1.0, _row_notional(row))))
    if not values:
        return {
            "count": 0,
            "effective_count": 0.0,
            "mean": None,
            "standard_error": None,
            "lower": None,
            "upper": None,
            "total_weight": 0.0,
        }
    total_weight = sum(weight for _, weight in values)
    mean = sum(value * weight for value, weight in values) / total_weight
    weight_square_sum = sum(weight * weight for _, weight in values)
    effective_count = total_weight * total_weight / weight_square_sum
    variance = sum(
        weight * (value - mean) ** 2
        for value, weight in values
    ) / total_weight
    standard_error = math.sqrt(max(0.0, variance) / max(1.0, effective_count))
    margin = EXIT_FEEDBACK_CONFIDENCE_Z * standard_error
    return {
        "count": len(values),
        "effective_count": round(effective_count, 6),
        "mean": round(mean, 6),
        "standard_error": round(standard_error, 6),
        "lower": round(mean - margin, 6),
        "upper": round(mean + margin, 6),
        "total_weight": round(total_weight, 2),
    }


def _has_effective_samples(
    statistics: Mapping[str, Any] | None,
    minimum: int,
) -> bool:
    return bool(
        isinstance(statistics, Mapping)
        and _number(statistics.get("effective_count"), 0.0) >= max(1, minimum)
    )


def _exclusive_path_rates(rows: Sequence[Mapping[str, Any]]) -> dict[str, float | int]:
    sell_fly_only = avoided_only = whipsaw = neutral = 0
    for row in rows:
        sell_fly = bool(row.get("sell_fly"))
        avoided = bool(row.get("avoided_loss"))
        if sell_fly and avoided:
            whipsaw += 1
        elif sell_fly:
            sell_fly_only += 1
        elif avoided:
            avoided_only += 1
        else:
            neutral += 1
    denominator = max(1, len(rows))
    return {
        "sell_fly_only_count": sell_fly_only,
        "avoided_loss_only_count": avoided_only,
        "whipsaw_count": whipsaw,
        "neutral_count": neutral,
        "sell_fly_only_rate": round(sell_fly_only / denominator, 6),
        "avoided_loss_only_rate": round(avoided_only / denominator, 6),
        "whipsaw_rate": round(whipsaw / denominator, 6),
    }


def observation_metrics(
    rows: Sequence[Mapping[str, Any]],
    reentry_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Aggregate independent clusters and capital-weighted tuning evidence."""
    completed = [row for row in rows if int(_number(row.get("completed"), 0)) == 1]
    clustered = _cluster_rows(completed)
    tunable = [row for row in clustered if row.get("price_basis") == "actual_execution"]
    soft = [
        row for row in tunable
        if str(row.get("exit_rule") or "") in SOFT_EXIT_RULES
        and not str(row.get("replacement_target_code") or "").strip()
        and str(row.get("exit_signal") or "") != "niu_priority_replacement"
    ]
    replacements = [
        row for row in clustered
        if int(_number(row.get("replacement_executed"), 0)) == 1
        and row.get("replacement_regret_pct") is not None
    ]
    completed_reentry = _cluster_rows([
        row for row in reentry_rows
        if int(_number(row.get("completed"), 0)) == 1
        and row.get("price_basis") == "actual_execution"
    ])
    blocked_volume = [
        row for row in completed_reentry
        if not bool(row.get("eligible"))
        and bool(row.get("reclaim_passed"))
        and bool(row.get("thesis_valid"))
        and not bool(row.get("volume_supportive"))
    ]
    allowed = [row for row in completed_reentry if bool(row.get("eligible"))]

    soft_stats = _weighted_statistics(soft, "close_return_pct")
    replacement_stats = _weighted_statistics(replacements, "replacement_regret_pct")
    blocked_reentry_stats = _weighted_statistics(blocked_volume, "future_return_pct")
    allowed_reentry_stats = _weighted_statistics(allowed, "future_return_pct")
    sell_fly_rate = _rate(soft, "sell_fly")
    avoided_loss_rate = _rate(soft, "avoided_loss")
    replacement_regret_rate = _rate(replacements, "replacement_regret")
    path_rates = _exclusive_path_rates(soft)
    strategy_counts = Counter(str(row.get("buy_strategy") or "unknown") for row in soft)
    objective_mean = sum(float(stats["mean"] or 0.0) for stats in (soft_stats, replacement_stats))
    objective_lower = sum(float(stats["lower"] or 0.0) for stats in (soft_stats, replacement_stats))
    objective_upper = sum(float(stats["upper"] or 0.0) for stats in (soft_stats, replacement_stats))
    dominant_strategy_share = max(strategy_counts.values()) / len(soft) if soft else 0.0
    return {
        "completed_count": len(completed),
        "effective_exit_cluster_count": len(tunable),
        "soft_exit_count": len(soft),
        "replacement_count": len(replacements),
        "reentry_completed_count": len(completed_reentry),
        "reentry_blocked_volume_count": len(blocked_volume),
        "reentry_allowed_count": len(allowed),
        "soft_exit_span_months": observation_span_months(soft),
        "replacement_span_months": observation_span_months(replacements),
        "reentry_blocked_span_months": observation_span_months(blocked_volume),
        "reentry_allowed_span_months": observation_span_months(allowed),
        "sell_fly_rate": round(sell_fly_rate, 6) if sell_fly_rate is not None else None,
        "avoided_loss_rate": round(avoided_loss_rate, 6) if avoided_loss_rate is not None else None,
        "replacement_regret_rate": round(replacement_regret_rate, 6) if replacement_regret_rate is not None else None,
        **path_rates,
        "avg_close_return_pct": round(value, 6) if (value := _average(soft, "close_return_pct")) is not None else None,
        "avg_mae_pct": round(value, 6) if (value := _average(soft, "mae_pct")) is not None else None,
        "avg_replacement_regret_pct": round(value, 6) if (value := _average(replacements, "replacement_regret_pct")) is not None else None,
        "soft_opportunity_return": soft_stats,
        "replacement_regret_return": replacement_stats,
        "reentry_blocked_return": blocked_reentry_stats,
        "reentry_allowed_return": allowed_reentry_stats,
        "objective_regret_score": round(objective_mean, 6),
        "objective_regret_lower": round(objective_lower, 6),
        "objective_regret_upper": round(objective_upper, 6),
        "strategy_counts": dict(sorted(strategy_counts.items())),
        "dominant_strategy_share": round(dominant_strategy_share, 6),
    }


def observation_span_months(rows: Sequence[Mapping[str, Any]]) -> int:
    dates: list[datetime] = []
    for row in rows:
        try:
            dates.append(datetime.strptime(
                str(row.get("sell_time") or row.get("observed_at") or "")[:10],
                "%Y-%m-%d",
            ))
        except (TypeError, ValueError):
            continue
    if not dates:
        return 0
    return max(0, (max(dates) - min(dates)).days) // 30 + 1


def observation_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    material = [
        {key: row.get(key) for key in (
            "trade_key", "sell_time", "code", "sell_price", "price_basis", "shares",
            "exit_rule", "exit_signal", "buy_strategy", "replacement_target_code",
            "replacement_executed", "close_return_pct", "mae_pct", "sell_fly",
            "avoided_loss", "replacement_regret_pct", "replacement_regret",
            "feedback_policy_version",
        )}
        for row in rows
        if int(_number(row.get("completed"), 0)) == 1
    ]
    encoded = json.dumps(
        sorted(material, key=lambda item: (str(item.get("sell_time")), str(item.get("trade_key")))),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _proposal_fingerprint(
    rows: Sequence[Mapping[str, Any]],
    reentry_rows: Sequence[Mapping[str, Any]],
    current_policy: Mapping[str, Any],
    parameters: Mapping[str, Any],
    action: str,
    *,
    min_samples: int,
    min_months: int,
    cooldown_samples: int,
) -> str:
    reentry_material = [
        {key: row.get(key) for key in (
            "audit_key", "observed_at", "code", "price_basis", "eligible", "executed",
            "reclaim_passed", "volume_supportive", "thesis_valid",
            "future_return_pct", "feedback_policy_version",
        )}
        for row in reentry_rows
        if int(_number(row.get("completed"), 0)) == 1
    ]
    material = {
        "algorithm_version": EXIT_FEEDBACK_ALGORITHM_VERSION,
        "parameter_bounds": EXIT_FEEDBACK_PARAMETER_BOUNDS,
        "current_version": int(_number(current_policy.get("version"), 0)),
        "current_parameters": normalize_exit_feedback_parameters(
            current_policy.get("parameters") if isinstance(current_policy.get("parameters"), Mapping) else None
        ),
        "proposed_parameters": normalize_exit_feedback_parameters(parameters),
        "action": action,
        "configuration": {
            "min_samples": int(min_samples),
            "min_months": int(min_months),
            "cooldown_samples": int(cooldown_samples),
            "max_window_samples": EXIT_FEEDBACK_MAX_WINDOW_SAMPLES,
        },
        "exit_observations": observation_fingerprint(rows),
        "reentry_observations": reentry_material,
    }
    return hashlib.sha256(json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _move(parameters: dict[str, int | float], name: str, direction: int) -> bool:
    allowed = EXIT_FEEDBACK_PARAMETER_BOUNDS[name]
    current = _bounded_value(name, parameters[name])
    index = allowed.index(current)
    target_index = max(0, min(len(allowed) - 1, index + direction))
    if target_index == index:
        return False
    parameters[name] = allowed[target_index]
    return True


def _recent_window(rows: Sequence[Mapping[str, Any]], time_field: str, key_field: str) -> list[Mapping[str, Any]]:
    completed = [row for row in rows if int(_number(row.get("completed"), 0)) == 1]
    completed.sort(key=lambda row: (str(row.get(time_field) or ""), str(row.get(key_field) or "")))
    return completed[-EXIT_FEEDBACK_MAX_WINDOW_SAMPLES:]


def _rollback_needed(
    current_policy: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    reentry_rows: Sequence[Mapping[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    version = int(_number(current_policy.get("version"), 0))
    if version <= 0 or not isinstance(current_policy.get("previous_parameters"), Mapping):
        return False, {}
    version_rows = [row for row in rows if int(_number(row.get("feedback_policy_version"), 0)) == version]
    version_reentry = [row for row in reentry_rows if int(_number(row.get("feedback_policy_version"), 0)) == version]
    metrics = observation_metrics(version_rows, version_reentry)
    baseline = current_policy.get("baseline_metrics")
    if not isinstance(baseline, Mapping):
        return False, metrics
    action = str(current_policy.get("action") or "")
    if action in {"", "hold", "algorithm_upgrade", "automatic_rollback"}:
        return False, metrics
    if action.startswith(("loosen_reentry:", "tighten_reentry:")):
        current_stats = metrics.get("reentry_allowed_return")
        baseline_stats = baseline.get("reentry_allowed_return")
        if (
            not _has_effective_samples(current_stats, EXIT_FEEDBACK_ROLLBACK_MIN_SAMPLES)
            or not isinstance(baseline_stats, Mapping)
        ):
            return False, metrics
        current_upper = _optional_number(current_stats.get("upper"))
        baseline_lower = _optional_number(baseline_stats.get("lower"))
        return bool(
            current_upper is not None
            and baseline_lower is not None
            and current_upper <= baseline_lower - EXIT_FEEDBACK_REENTRY_MIN_EFFECT_PCT
        ), metrics
    if action in {"raise_replacement_margin", "lower_replacement_margin"}:
        current_stats = metrics.get("replacement_regret_return")
        baseline_stats = baseline.get("replacement_regret_return")
        minimum_effect = EXIT_FEEDBACK_REPLACEMENT_MIN_EFFECT_PCT
    else:
        current_stats = metrics.get("soft_opportunity_return")
        baseline_stats = baseline.get("soft_opportunity_return")
        minimum_effect = EXIT_FEEDBACK_SOFT_MIN_EFFECT_PCT
    if (
        not _has_effective_samples(current_stats, EXIT_FEEDBACK_ROLLBACK_MIN_SAMPLES)
        or not isinstance(baseline_stats, Mapping)
    ):
        return False, metrics
    current_lower = _optional_number(current_stats.get("lower"))
    baseline_upper = _optional_number(baseline_stats.get("upper"))
    return bool(
        current_lower is not None
        and baseline_upper is not None
        and current_lower >= baseline_upper + minimum_effect
    ), metrics


def propose_exit_feedback_policy(
    rows: Sequence[Mapping[str, Any]],
    current_policy: Mapping[str, Any] | None,
    *,
    reentry_rows: Sequence[Mapping[str, Any]] = (),
    last_evaluation_count: int = 0,
    min_samples: int = EXIT_FEEDBACK_DEFAULT_MIN_SAMPLES,
    min_months: int = EXIT_FEEDBACK_DEFAULT_MIN_MONTHS,
    cooldown_samples: int = EXIT_FEEDBACK_DEFAULT_COOLDOWN_SAMPLES,
) -> dict[str, Any]:
    """Propose one audited grid move or a non-mutating evaluation result."""
    all_completed = [row for row in rows if int(_number(row.get("completed"), 0)) == 1]
    all_reentry_completed = [
        row for row in reentry_rows
        if int(_number(row.get("completed"), 0)) == 1
    ]
    window = _recent_window(all_completed, "sell_time", "trade_key")
    reentry_window = _recent_window(reentry_rows, "observed_at", "audit_key")
    metrics = observation_metrics(window, reentry_window)
    span_months = observation_span_months([
        row for row in window
        if row.get("price_basis") == "actual_execution"
    ])
    current = current_policy if isinstance(current_policy, Mapping) else {}
    parameters = normalize_exit_feedback_parameters(
        current.get("parameters") if isinstance(current.get("parameters"), Mapping) else None
    )
    current_count = max(int(_number(current.get("observation_count"), 0)), max(0, int(last_evaluation_count)))
    evidence_count = len(all_completed) + len(all_reentry_completed)
    new_count = max(0, evidence_count - current_count)
    base = {
        "algorithm_version": EXIT_FEEDBACK_ALGORITHM_VERSION,
        "parameters": parameters,
        "metrics": metrics,
        "observation_count": evidence_count,
        "new_observation_count": new_count,
        "observation_span_months": span_months,
        "previous_parameters": dict(parameters),
        "rollback_of": None,
    }

    def fingerprint(action: str, proposed: Mapping[str, Any]) -> str:
        return _proposal_fingerprint(
            window,
            reentry_window,
            current,
            proposed,
            action,
            min_samples=min_samples,
            min_months=min_months,
            cooldown_samples=cooldown_samples,
        )

    effective_count = int(metrics.get("effective_exit_cluster_count") or 0)
    if effective_count < max(1, int(min_samples)) or span_months < max(1, int(min_months)):
        action = "sample_gate"
        return {
            **base,
            "source_fingerprint": fingerprint(action, parameters),
            "persist": False,
            "record_evaluation": True,
            "status": "learning",
            "action": action,
            "reason": (
                f"5日独立样本簇{effective_count}/{max(1, int(min_samples))}，"
                f"完整记录{len(all_completed)}，有效跨度{span_months}/{max(1, int(min_months))}个月"
            ),
        }

    rollback, rollback_metrics = _rollback_needed(current, all_completed, reentry_rows)
    if rollback:
        restored = normalize_exit_feedback_parameters(current.get("previous_parameters"))
        action = "automatic_rollback"
        return {
            **base,
            "source_fingerprint": fingerprint(action, restored),
            "persist": True,
            "record_evaluation": True,
            "status": "active",
            "parameters": restored,
            "action": action,
            "reason": "当前版本资金加权后悔收益的90%区间显著恶化，自动恢复上一参数版本",
            "rollback_of": int(_number(current.get("version"), 0)),
            "rollback_metrics": rollback_metrics,
        }

    if (current or last_evaluation_count > 0) and new_count < max(1, int(cooldown_samples)):
        action = "cooldown"
        return {
            **base,
            "source_fingerprint": fingerprint(action, parameters),
            "persist": False,
            "record_evaluation": False,
            "status": "cooldown",
            "action": action,
            "reason": f"新增完整样本{new_count}/{max(1, int(cooldown_samples))}",
        }

    component_min = max(15, max(1, int(min_samples)) // 2)
    current_version = int(_number(current.get("version"), 0))
    if (
        current_version > 0
        and str(current.get("algorithm_version") or "")
        == EXIT_FEEDBACK_ALGORITHM_VERSION
    ):
        version_window = [
            row for row in window
            if int(_number(row.get("feedback_policy_version"), 0))
            == current_version
        ]
        version_reentry_window = [
            row for row in reentry_window
            if int(_number(row.get("feedback_policy_version"), 0))
            == current_version
        ]
        version_metrics = observation_metrics(
            version_window,
            version_reentry_window,
        )
        if (
            int(version_metrics.get("effective_exit_cluster_count") or 0)
            >= component_min
            or int(version_metrics.get("reentry_completed_count") or 0)
            >= component_min
        ):
            metrics = version_metrics
            base["metrics"] = metrics
    next_parameters = dict(parameters)
    action = "hold"
    reason = "资金加权结果或90%置信区间未越过调参滞回区间，保持当前参数"
    soft_stats = metrics.get("soft_opportunity_return")
    if (
        _has_effective_samples(soft_stats, component_min)
        and int(metrics.get("soft_exit_span_months") or 0) >= max(1, int(min_months))
    ):
        assert isinstance(soft_stats, Mapping)
        lower = _optional_number(soft_stats.get("lower"))
        upper = _optional_number(soft_stats.get("upper"))
        sell_fly = _number(metrics.get("sell_fly_only_rate"), 0.0)
        avoided = _number(metrics.get("avoided_loss_only_rate"), 0.0)
        if lower is not None and lower >= EXIT_FEEDBACK_SOFT_MIN_EFFECT_PCT and sell_fly > avoided:
            for name, direction in (("soft_exit_confirmations", 1), ("soft_exit_reduce_ratio", -1)):
                if _move(next_parameters, name, direction):
                    action = f"reduce_sell_fly:{name}"
                    reason = "软退出后的资金加权机会收益显著为正，按一档增加退出耐心"
                    break
        elif upper is not None and upper <= -EXIT_FEEDBACK_SOFT_MIN_EFFECT_PCT and avoided > sell_fly:
            for name, direction in (("soft_exit_reduce_ratio", 1), ("soft_exit_confirmations", -1)):
                if _move(next_parameters, name, direction):
                    action = f"restore_defense:{name}"
                    reason = "软退出后的资金加权机会收益显著为负，按一档恢复防守退出"
                    break

    replacement_stats = metrics.get("replacement_regret_return")
    if (
        action == "hold"
        and _has_effective_samples(replacement_stats, component_min)
        and int(metrics.get("replacement_span_months") or 0) >= max(1, int(min_months))
    ):
        assert isinstance(replacement_stats, Mapping)
        lower = _optional_number(replacement_stats.get("lower"))
        upper = _optional_number(replacement_stats.get("upper"))
        if lower is not None and lower >= EXIT_FEEDBACK_REPLACEMENT_MIN_EFFECT_PCT:
            if _move(next_parameters, "replacement_priority_margin", 1):
                action = "raise_replacement_margin"
                reason = "实际换仓的资金加权后悔收益显著为正，换仓优势门槛上调一档"
        elif upper is not None and upper <= -EXIT_FEEDBACK_REPLACEMENT_MIN_EFFECT_PCT:
            if _move(next_parameters, "replacement_priority_margin", -1):
                action = "lower_replacement_margin"
                reason = "实际换仓持续贡献超额收益，换仓优势门槛下调一档"

    blocked_stats = metrics.get("reentry_blocked_return")
    if (
        action == "hold"
        and _has_effective_samples(blocked_stats, component_min)
        and int(metrics.get("reentry_blocked_span_months") or 0) >= max(1, int(min_months))
    ):
        assert isinstance(blocked_stats, Mapping)
        lower = _optional_number(blocked_stats.get("lower"))
        if lower is not None and lower >= EXIT_FEEDBACK_REENTRY_MIN_EFFECT_PCT:
            for name in ("reentry_volume_ratio", "reentry_amount_percentile"):
                if _move(next_parameters, name, -1):
                    action = f"loosen_reentry:{name}"
                    reason = "被量能门槛拦截的再入影子样本后续收益显著为正，门槛下调一档"
                    break
    allowed_stats = metrics.get("reentry_allowed_return")
    if (
        action == "hold"
        and _has_effective_samples(allowed_stats, component_min)
        and int(metrics.get("reentry_allowed_span_months") or 0) >= max(1, int(min_months))
    ):
        assert isinstance(allowed_stats, Mapping)
        upper = _optional_number(allowed_stats.get("upper"))
        if upper is not None and upper <= -EXIT_FEEDBACK_REENTRY_MIN_EFFECT_PCT:
            for name in ("reentry_amount_percentile", "reentry_volume_ratio"):
                if _move(next_parameters, name, 1):
                    action = f"tighten_reentry:{name}"
                    reason = "获准再入的影子样本后续收益显著为负，门槛上调一档"
                    break

    if (
        action == "hold"
        and int(_number(current.get("version"), 0)) > 0
        and str(current.get("algorithm_version") or "")
        != EXIT_FEEDBACK_ALGORITHM_VERSION
    ):
        action = "algorithm_upgrade"
        reason = "反馈算法升级，参数不变并建立新的可审计基线版本"
    persist = (
        action == "algorithm_upgrade"
        or (action != "hold" and next_parameters != parameters)
    )
    return {
        **base,
        "source_fingerprint": fingerprint(action, next_parameters),
        "persist": persist,
        "record_evaluation": True,
        "status": "active" if persist else "hold",
        "parameters": next_parameters,
        "action": action,
        "reason": reason,
        "baseline_metrics": metrics,
    }


__all__ = [
    "EXIT_FEEDBACK_ALGORITHM_VERSION",
    "EXIT_FEEDBACK_DEFAULT_COOLDOWN_SAMPLES",
    "EXIT_FEEDBACK_DEFAULT_MIN_MONTHS",
    "EXIT_FEEDBACK_DEFAULT_MIN_SAMPLES",
    "EXIT_FEEDBACK_DEFAULT_PARAMETERS",
    "EXIT_FEEDBACK_PARAMETER_BOUNDS",
    "effective_exit_feedback_parameters",
    "normalize_exit_feedback_parameters",
    "observation_metrics",
    "propose_exit_feedback_policy",
]
