"""Build the bounded public read model for candidates qualified today."""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable


TODAY_CANDIDATES_SCHEMA_VERSION = 4
TODAY_CANDIDATE_INTRADAY_SCHEMA_VERSION = 1
TODAY_CANDIDATE_MAX_TRANSITION_POINTS = 96
TODAY_CANDIDATE_INTRADAY_MAX_ITEMS = 48
TODAY_CANDIDATE_INTRADAY_MAX_POINTS = 242
TODAY_CANDIDATE_INTRADAY_MAX_WORKERS = 4
TODAY_CANDIDATE_INTRADAY_DEADLINE_SECONDS = 10.0

# This page intentionally exposes a smaller subset than the live candidate
# payload.  Full strategy evaluations and market context remain server-side.
TODAY_CANDIDATE_FIELDS = (
    "code",
    "name",
    "best_strategy",
    "score",
    "best_score",
    "score_total",
    "score_basis",
    "entry_threshold",
    "actionable",
    "price",
    "change_pct",
    "amount_yi",
    "industry",
    "sector",
    "signal_theme",
    "signal_theme_attribution_score",
    "signal_theme_attribution_weight",
    "signal_theme_historical_prior_score",
    "signal_theme_cohort_alignment_score",
    "signal_theme_return_correlation_score",
    "signal_theme_return_correlation_rank_score",
    "signal_theme_return_correlation_observation_count",
    "signal_theme_specificity_score",
    "board",
    "board_label",
    "reason",
    "distance_pct",
    "bbi",
    "bbi_upward",
    "above_bbi",
    "min_j_10d",
    "j_recovering",
    "j_oversold",
    "ema20",
    "market_regime",
    "market_score",
    "sector_status",
    "sector_score",
    "stock_sector_rank",
    "niuone_lifecycle_stage",
    "niuone_lifecycle_label",
    "mainline_state",
    "mainline_intraday_state",
    "mainline_score",
    "mainline_mode",
    "mainline_primary",
    "mainline_secondary",
    "mainline_cross_day_confirmed",
    "mainline_cross_day_persistent",
    "mainline_confirmed",
    "mainline_core_overlap_count",
    "strong_stock_count",
    "effective_strong_count",
    "leader_concentration",
    "stock_leader_rank",
    "stock_leader_tier",
    "stock_strong",
    "stock_strong_score",
    "stock_activity_score",
    "stock_market_amount_percentile",
    "stock_theme_amount_percentile",
    "stock_activity_confirmed",
    "reversal_basis",
    "daily_v_reversal",
    "daily_v_left_days",
    "daily_v_right_days",
    "daily_v_decline_pct",
    "daily_v_rebound_pct",
    "daily_v_recovery_ratio",
    "daily_v_trough_date",
    "stop_price",
    "stop_distance_pct",
    "effective_loss_distance_pct",
    "per_trade_risk_budget_pct",
    "max_position_pct_by_risk",
    "position_hint",
    "time_stop",
)


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _public_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def _candidate_score(candidate: Mapping[str, Any]) -> float:
    value = candidate.get("best_score")
    if value is None:
        value = candidate.get("score")
    return _finite_number(value) or 0.0


def _candidate_reached_threshold(candidate: Mapping[str, Any]) -> bool:
    """Compatibility fallback for archives created before ``trade_items``."""
    score = _candidate_score(candidate)
    threshold = _finite_number(candidate.get("entry_threshold"))
    if threshold is None:
        threshold = 8.0
    blockers = candidate.get("hard_blockers")
    has_blockers = isinstance(blockers, list) and any(
        str(value or "").strip() for value in blockers
    )
    return (
        bool(candidate.get("actionable", score >= threshold))
        and score >= threshold
        and not has_blockers
    )


def _qualified_rows(scan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if "trade_items" in scan:
        source = scan.get("trade_items")
        return (
            [item for item in source if isinstance(item, Mapping)]
            if isinstance(source, list)
            else []
        )
    source = scan.get("items") or scan.get("candidates") or []
    if not isinstance(source, list):
        return []
    return [
        item
        for item in source
        if isinstance(item, Mapping) and _candidate_reached_threshold(item)
    ]


def _public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    row = {
        field: _public_scalar(candidate[field])
        for field in TODAY_CANDIDATE_FIELDS
        if field in candidate
    }
    for key in ("hard_blockers", "risk_flags"):
        values = candidate.get(key)
        if isinstance(values, list):
            row[key] = [
                _public_scalar(value)
                for value in values[:12]
                if str(value or "").strip()
            ]
    return row


def _scan_candidates_by_code(scan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for key in ("items", "candidates"):
        source = scan.get(key)
        if not isinstance(source, list):
            continue
        for candidate in source:
            if not isinstance(candidate, Mapping):
                continue
            code = str(candidate.get("code") or "").strip()
            if not code:
                continue
            previous = result.get(code)
            if previous is None or _candidate_score(candidate) >= _candidate_score(previous):
                result[code] = candidate
    return result


def _qualification_transition(
    candidate: Mapping[str, Any] | None,
    generated_at: str,
    *,
    qualified: bool,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "at": generated_at,
        "qualified": qualified,
    }
    if candidate is None:
        return row
    score = _finite_number(candidate.get("best_score"))
    if score is None:
        score = _finite_number(candidate.get("score"))
    if score is not None:
        row["score"] = round(score, 4)
    strategy = str(candidate.get("best_strategy") or "")[:80]
    if strategy:
        row["strategy"] = strategy
    return row


def _append_transition(
    source: Any,
    point: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    points = [value for value in source or [] if isinstance(value, Mapping)]
    points.append(point)
    if len(points) <= TODAY_CANDIDATE_MAX_TRANSITION_POINTS:
        return points
    return [points[0], *points[-(TODAY_CANDIDATE_MAX_TRANSITION_POINTS - 1):]]


def _strategy_meta(scans: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for scan in scans:
        source = scan.get("strategy_meta")
        if not isinstance(source, Mapping):
            continue
        for raw_key, raw_value in source.items():
            key = str(raw_key or "").strip()
            if not key or not isinstance(raw_value, Mapping):
                continue
            meta = {
                field: _public_scalar(raw_value[field])
                for field in ("label", "color")
                if field in raw_value
            }
            if meta:
                result[key] = meta
    return result


def build_today_candidates_payload(
    scans: Iterable[Mapping[str, Any]],
    *,
    current_date: str,
) -> dict[str, Any]:
    """Merge today's trade-ready rows, keeping each stock's best snapshot."""
    dated_scans: list[tuple[str, Mapping[str, Any]]] = []
    seen_scan_times: set[str] = set()
    for scan in scans:
        if not isinstance(scan, Mapping):
            continue
        generated_at = str(scan.get("generated_at") or scan.get("finished_at") or "").strip()
        if generated_at[:10] != current_date or generated_at in seen_scan_times:
            continue
        seen_scan_times.add(generated_at)
        dated_scans.append((generated_at, scan))
    dated_scans.sort(key=lambda item: item[0])

    records: dict[str, dict[str, Any]] = {}
    for generated_at, scan in dated_scans:
        per_scan: dict[str, Mapping[str, Any]] = {}
        for candidate in _qualified_rows(scan):
            code = str(candidate.get("code") or "").strip()
            if not code:
                continue
            previous = per_scan.get(code)
            if previous is None or _candidate_score(candidate) >= _candidate_score(previous):
                per_scan[code] = candidate
        scan_candidates = _scan_candidates_by_code(scan)
        for code, current in records.items():
            if code in per_scan or not current.get("_currently_qualified"):
                continue
            current["qualification_transitions"] = _append_transition(
                current.get("qualification_transitions"),
                _qualification_transition(
                    scan_candidates.get(code),
                    generated_at,
                    qualified=False,
                ),
            )
            current["_currently_qualified"] = False
        for code, candidate in per_scan.items():
            current = records.get(code)
            score = _candidate_score(candidate)
            if current is None:
                row = _public_candidate(candidate)
                row.update({
                    "first_qualified_at": generated_at,
                    "last_qualified_at": generated_at,
                    "best_qualified_at": generated_at,
                    "qualified_count": 1,
                    "qualification_transitions": [
                        _qualification_transition(candidate, generated_at, qualified=True)
                    ],
                    "_currently_qualified": True,
                })
                records[code] = row
                continue
            current["last_qualified_at"] = generated_at
            current["qualified_count"] = int(current.get("qualified_count") or 0) + 1
            qualification_transitions = current.get("qualification_transitions")
            if not current.get("_currently_qualified"):
                qualification_transitions = _append_transition(
                    qualification_transitions,
                    _qualification_transition(candidate, generated_at, qualified=True),
                )
            current["qualification_transitions"] = qualification_transitions
            current["_currently_qualified"] = True
            if score >= _candidate_score(current):
                first_qualified_at = current["first_qualified_at"]
                qualified_count = current["qualified_count"]
                current = _public_candidate(candidate)
                current.update({
                    "first_qualified_at": first_qualified_at,
                    "last_qualified_at": generated_at,
                    "best_qualified_at": generated_at,
                    "qualified_count": qualified_count,
                    "qualification_transitions": qualification_transitions,
                    "_currently_qualified": True,
                })
                records[code] = current

    for record in records.values():
        record["currently_qualified"] = bool(
            record.pop("_currently_qualified", False)
        )
    items = sorted(
        records.values(),
        key=lambda item: (
            -_candidate_score(item),
            -int(item.get("qualified_count") or 0),
            str(item.get("code") or ""),
        ),
    )
    scan_values = [scan for _generated_at, scan in dated_scans]
    generated_at = dated_scans[-1][0] if dated_scans else ""
    return {
        "schema_version": TODAY_CANDIDATES_SCHEMA_VERSION,
        "current_date": current_date,
        "generated_at": generated_at,
        "scan_count": len(dated_scans),
        "count": len(items),
        "current_count": sum(
            1 for item in items if item.get("currently_qualified") is True
        ),
        "items": items,
        "strategy_meta": _strategy_meta(scan_values),
    }


def _candidate_previous_close(candidate: Mapping[str, Any]) -> float | None:
    price = _finite_number(candidate.get("price"))
    change_pct = _finite_number(candidate.get("change_pct"))
    if price is None or price <= 0 or change_pct is None or change_pct <= -99.9:
        return None
    previous_close = price / (1 + change_pct / 100)
    return previous_close if math.isfinite(previous_close) and previous_close > 0 else None


def _public_intraday_point(
    point: Mapping[str, Any],
    *,
    previous_close: float | None,
) -> dict[str, Any] | None:
    price = _finite_number(point.get("price"))
    if price is None or price <= 0:
        return None
    minute_value = _finite_number(point.get("minute"))
    minute = int(minute_value) if minute_value is not None else None
    if minute is not None and not 0 <= minute <= 240:
        minute = None
    pct = _finite_number(point.get("pct"))
    if pct is None and previous_close:
        pct = (price / previous_close - 1) * 100
    row: dict[str, Any] = {
        "time": str(point.get("time") or "")[:5],
        "price": round(price, 3),
        "pct": round(pct, 3) if pct is not None else None,
    }
    if minute is not None:
        row["minute"] = minute
    return row


def _public_intraday_series(
    code: str,
    payload: Mapping[str, Any],
    *,
    fallback_previous_close: float | None,
) -> dict[str, Any] | None:
    previous_close = _finite_number(payload.get("prev_close"))
    if previous_close is None or previous_close <= 0:
        previous_close = fallback_previous_close
    source = payload.get("points")
    if not isinstance(source, list):
        return None
    points = [
        row
        for point in source[-TODAY_CANDIDATE_INTRADAY_MAX_POINTS:]
        if isinstance(point, Mapping)
        and (row := _public_intraday_point(point, previous_close=previous_close)) is not None
    ]
    if len(points) < 2:
        return None
    last_price = _finite_number(payload.get("last_price")) or points[-1]["price"]
    last_pct = _finite_number(payload.get("last_pct"))
    if last_pct is None:
        last_pct = _finite_number(points[-1].get("pct"))
    return {
        "code": code,
        "updated_at": str(payload.get("updated_at") or ""),
        "prev_close": round(previous_close, 3) if previous_close else None,
        "last_price": round(last_price, 3),
        "last_pct": round(last_pct, 3) if last_pct is not None else None,
        "points": points,
    }


def build_today_candidate_intraday_payload(
    candidates: Iterable[Mapping[str, Any]],
    *,
    fetcher: Callable[[str, float | None], Mapping[str, Any]],
    generated_at: str,
    max_items: int = TODAY_CANDIDATE_INTRADAY_MAX_ITEMS,
    max_workers: int = TODAY_CANDIDATE_INTRADAY_MAX_WORKERS,
    deadline_seconds: float = TODAY_CANDIDATE_INTRADAY_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Fetch a bounded batch of candidate minute lines with per-stock fallback."""
    bounded_items = max(1, min(TODAY_CANDIDATE_INTRADAY_MAX_ITEMS, int(max_items)))
    selected: list[tuple[str, float | None]] = []
    seen_codes: set[str] = set()
    source_count = 0
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        code = str(candidate.get("code") or "").strip()
        if not re.fullmatch(r"\d{6}", code) or code in seen_codes:
            continue
        source_count += 1
        if len(selected) >= bounded_items:
            continue
        seen_codes.add(code)
        selected.append((code, _candidate_previous_close(candidate)))

    if not selected:
        return {
            "schema_version": TODAY_CANDIDATE_INTRADAY_SCHEMA_VERSION,
            "generated_at": generated_at,
            "requested_count": 0,
            "count": 0,
            "failed_count": 0,
            "timed_out_count": 0,
            "truncated": False,
            "items": [],
        }

    worker_count = max(1, min(TODAY_CANDIDATE_INTRADAY_MAX_WORKERS, int(max_workers), len(selected)))
    executor = ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="candidate-intraday",
    )
    futures = {
        executor.submit(fetcher, code, previous_close): (index, code, previous_close)
        for index, (code, previous_close) in enumerate(selected)
    }
    done, pending = wait(
        futures,
        timeout=max(0.05, min(TODAY_CANDIDATE_INTRADAY_DEADLINE_SECONDS, float(deadline_seconds))),
    )
    resolved: list[tuple[int, dict[str, Any]]] = []
    for future in done:
        index, code, previous_close = futures[future]
        try:
            payload = future.result()
            if isinstance(payload, Mapping):
                public = _public_intraday_series(
                    code,
                    payload,
                    fallback_previous_close=previous_close,
                )
                if public is not None:
                    resolved.append((index, public))
        except Exception:
            continue
    for future in pending:
        future.cancel()
    executor.shutdown(wait=not pending, cancel_futures=True)

    items = [item for _index, item in sorted(resolved, key=lambda value: value[0])]
    requested_count = len(selected)
    return {
        "schema_version": TODAY_CANDIDATE_INTRADAY_SCHEMA_VERSION,
        "generated_at": generated_at,
        "requested_count": requested_count,
        "count": len(items),
        "failed_count": requested_count - len(items),
        "timed_out_count": len(pending),
        "truncated": source_count > requested_count,
        "items": items,
    }
