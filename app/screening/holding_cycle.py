"""Incremental rescoring for the current Practice holdings.

The fast holding cycle deliberately reuses the normal strategy scorers and
candidate-selection gates.  Its only narrower boundary is the input universe:
open positions supplied by the trading account.  It never discovers a new
symbol and never writes the full-scan caches.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from market_data.tencent_kline_cache import (
    DEFAULT_KLINE_COUNT,
    kline_cache_path,
    load_kline_series_map,
)
from screening.multi_strategy import (
    active_strategy_meta,
    active_strategy_score_profiles,
    active_strategy_scorers,
    active_strategy_setting,
    analyze_all_strategies,
    enabled_persona_strategy_setting,
    load_bulk_stock_board_map,
    prepare_strategy_rows,
    resolve_quote_trading_dates,
    strategy_source_setting,
    tencent_batch_quote,
)
from screening.stock_universe import stock_universe_metadata
from storage.prompt_strategies import PromptStrategyStore
from strategies.prompt_runtime import score_prompt_selection
from strategies.registry import (
    STRATEGY_SUITE_PRESET_TEXT,
    active_strategy_suite,
)
from strategies.scoring import (
    NIUONE_STRATEGY_IDS,
    SECTOR_TIDE_STRATEGY_IDS,
    ZETTARANC_STRATEGY_IDS,
)
from strategies.selection import candidate_is_trade_ready, select_trade_candidates


HOLDING_CYCLE_KIND = "holding_fast"
DEFAULT_QUOTE_TIMEOUT_SECONDS = 5.0
DEFAULT_QUOTE_MAX_ATTEMPTS = 2

_ATTRIBUTION_PROFILE_FIELDS = {
    "theme_member_count": "theme_member_count",
    "membership_source": "theme_membership_source",
    "current_score": "current_attribution_score",
    "historical_prior_score": "historical_prior_score",
    "attribution_score": "attribution_score",
    "attribution_weight": "attribution_weight",
    "leadership_eligible": "leadership_eligible",
    "cohort_alignment_score": "cohort_alignment_score",
    "peer_resonance_score": "peer_resonance_score",
    "return_correlation_score": "return_correlation_score",
    "return_correlation_rank_score": "return_correlation_rank_score",
    "return_correlation_observation_count": (
        "return_correlation_observation_count"
    ),
    "return_correlation_peer_count": "return_correlation_peer_count",
    "theme_specificity_score": "theme_specificity_score",
    "observation_count": "attribution_observation_count",
    "wave_count": "attribution_wave_count",
}


def _merge_live_attribution(
    profile: Mapping[str, Any],
    attribution: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(profile)
    for source_key, target_key in _ATTRIBUTION_PROFILE_FIELDS.items():
        if source_key in attribution:
            merged[target_key] = attribution.get(source_key)
    return merged


def merge_niuone_holding_cycle_context(
    full_context: Mapping[str, Any] | None,
    operational_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Combine fresh theme state with scorer-complete stock profiles.

    The dedicated minute cache intentionally persists only per-stock theme
    attributions.  Those compact rows are sufficient for cross-day mainline
    state, but the ordinary NiuOne scorers also require fields such as
    ``strong_score`` and ``theme_profiles``.  Keep the newest operational
    market/theme state while restoring only stock profiles present in the
    latest complete scan.  Compact-only stocks remain absent and therefore
    fail closed instead of being treated as complete scorer inputs.
    """

    complete = dict(full_context) if isinstance(full_context, Mapping) else {}
    operational = (
        dict(operational_context)
        if isinstance(operational_context, Mapping)
        else {}
    )
    merged = dict(complete)
    merged.update({
        key: value
        for key, value in operational.items()
        if key != "stocks"
    })

    complete_stocks = (
        complete.get("stocks")
        if isinstance(complete.get("stocks"), Mapping)
        else {}
    )
    operational_stocks = (
        operational.get("stocks")
        if isinstance(operational.get("stocks"), Mapping)
        else {}
    )
    merged_stocks: dict[str, dict[str, Any]] = {}
    for raw_code, raw_profile in complete_stocks.items():
        if not isinstance(raw_profile, Mapping):
            continue
        code = str(raw_code)
        profile = dict(raw_profile)
        live_stock = operational_stocks.get(code)
        live_attributions = (
            [
                dict(item)
                for item in list(live_stock.get("theme_attributions") or [])
                if isinstance(item, Mapping)
            ]
            if isinstance(live_stock, Mapping)
            else []
        )
        if live_attributions:
            profile["theme_attributions"] = live_attributions
            by_theme = {
                str(item.get("theme") or ""): item
                for item in live_attributions
                if str(item.get("theme") or "")
            }
            profile_theme = str(profile.get("industry") or "")
            if profile_theme in by_theme:
                profile = _merge_live_attribution(
                    profile,
                    by_theme[profile_theme],
                )
            raw_theme_profiles = profile.get("theme_profiles")
            if isinstance(raw_theme_profiles, list):
                profile["theme_profiles"] = [
                    _merge_live_attribution(
                        item,
                        by_theme.get(str(item.get("industry") or ""), {}),
                    )
                    if isinstance(item, Mapping)
                    else item
                    for item in raw_theme_profiles
                ]
        merged_stocks[code] = profile
    merged["stocks"] = merged_stocks
    return merged


def _normalize_code(value: Any) -> str:
    match = re.search(r"\d{6}", str(value or ""))
    return match.group(0) if match else ""


def _tencent_symbol(code: str) -> str:
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def _quote_date(value: Any) -> str:
    matched = re.search(
        r"(?P<year>\d{4})[-/]?(?P<month>\d{2})[-/]?(?P<day>\d{2})",
        str(value or ""),
    )
    if not matched:
        return ""
    return (
        f"{matched.group('year')}-{matched.group('month')}-"
        f"{matched.group('day')}"
    )


def _active_scorers() -> tuple[
    dict[str, Callable[..., dict[str, Any] | None]],
    PromptStrategyStore | None,
    dict[str, Any] | None,
    int,
]:
    """Resolve the same active scorer set used by the complete scan."""

    scorers = dict(active_strategy_scorers())
    prompt_store: PromptStrategyStore | None = None
    prompt_version: dict[str, Any] | None = None
    minimum_rows = 30
    if STRATEGY_SUITE_PRESET_TEXT in scorers:
        prompt_store = PromptStrategyStore()
        prompt_version = prompt_store.active_version()
        if prompt_version is not None:
            stage_requirements = (
                (prompt_version.get("execution_plan") or {}).get(
                    "stage_requirements"
                )
                or {}
            )
            minimum_rows = max(
                1,
                min(
                    500,
                    int(
                        (stage_requirements.get("selection") or {}).get(
                            "minimum_bars",
                            DEFAULT_KLINE_COUNT,
                        )
                    ),
                ),
            )
            scorers[STRATEGY_SUITE_PRESET_TEXT] = (
                lambda rows, version=prompt_version: score_prompt_selection(
                    rows,
                    version,
                    data_context={},
                )
            )
    return scorers, prompt_store, prompt_version, minimum_rows


def _strategy_context(
    scorers: Mapping[str, Any],
    source: Mapping[str, Any],
) -> dict[str, Any]:
    enabled = set(scorers)
    if enabled & NIUONE_STRATEGY_IDS:
        value = source.get("niuone_context")
        return dict(value) if isinstance(value, Mapping) else {}
    if enabled & SECTOR_TIDE_STRATEGY_IDS:
        value = source.get("sector_tide_context")
        return dict(value) if isinstance(value, Mapping) else {}
    if enabled & ZETTARANC_STRATEGY_IDS:
        value = source.get("zettaranc_context")
        if isinstance(value, Mapping):
            return dict(value)
        tide = source.get("sector_tide_context")
        tide = tide if isinstance(tide, Mapping) else {}
        flow = tide.get("industry_money_flow")
        return {
            "industry_money_flow": (
                dict(flow) if isinstance(flow, Mapping) else {}
            )
        }
    return {}


def _project_candidate(
    code: str,
    name: str,
    quote: Mapping[str, Any],
    factual_industry: str,
    multi: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the private decision candidate from an ordinary scorer result."""

    best_strategy = str(multi.get("best_strategy") or "")
    strategies = multi.get("strategies")
    strategies = strategies if isinstance(strategies, Mapping) else {}
    best = strategies.get(best_strategy)
    best = dict(best) if isinstance(best, Mapping) else {}
    niuone_best = best_strategy in NIUONE_STRATEGY_IDS
    classification_industry = str(
        best.get("classification_industry") or factual_industry or ""
    ).strip()
    signal_theme = (
        str(best.get("signal_theme") or best.get("industry") or "").strip()
        if niuone_best
        else ""
    )
    candidate_industry = (
        classification_industry
        if niuone_best
        else str(best.get("industry") or classification_industry or "").strip()
    )
    amount = quote.get("amount")
    candidate = {
        **best,
        "code": code,
        "name": name,
        **stock_universe_metadata(code, name),
        "price": quote.get("price"),
        "change_pct": quote.get("change_pct"),
        "amount": amount,
        "amount_yi": (
            round(float(amount) / 1e8, 1)
            if isinstance(amount, (int, float)) and amount
            else None
        ),
        "turnover": quote.get("turnover"),
        "industry": candidate_industry,
        "sector": candidate_industry,
        "signal_theme": signal_theme,
        "score": best.get("score", 0),
        "score_total": best.get("score_total", 10),
        "verdict": best.get("verdict", ""),
        "best_strategy": best_strategy,
        "best_score": multi.get("best_score"),
        "best_decision_score": multi.get(
            "best_decision_score",
            multi.get("best_score"),
        ),
        "best_verdict": multi.get("best_verdict"),
        "strategies": dict(strategies),
        "consensus_count": multi.get("consensus_count", 0),
        "consensus_boost": multi.get("consensus_boost", 0),
        "quote_time": quote.get("quote_time"),
    }
    candidate["trade_ready"] = candidate_is_trade_ready(candidate)
    return candidate


def build_holding_cycle_payload(
    holdings: list[Mapping[str, Any]],
    context_payload: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    quote_fetcher: Callable[..., Mapping[str, Mapping[str, Any]]] = (
        tencent_batch_quote
    ),
    history_loader: Callable[..., dict[str, list[dict[str, Any]]]] = (
        load_kline_series_map
    ),
    board_loader: Callable[..., Mapping[str, Any]] = load_bulk_stock_board_map,
) -> dict[str, Any]:
    """Rescore only open holdings with the normal strategy machinery.

    Missing or stale market inputs remove the affected holding from the BUY
    candidate pool.  The trading layer still evaluates every open position for
    SELL/HOLD using its normal portfolio and exit paths.
    """

    current = now or datetime.now()
    generated_at = current.strftime("%Y-%m-%d %H:%M:%S")
    source = dict(context_payload) if isinstance(context_payload, Mapping) else {}
    by_code: dict[str, dict[str, Any]] = {}
    for raw in holdings:
        if not isinstance(raw, Mapping):
            continue
        code = _normalize_code(raw.get("code"))
        if not code:
            continue
        by_code[code] = dict(raw)
    codes = sorted(by_code)
    base = {
        "generated_at": generated_at,
        "decision_cycle_kind": HOLDING_CYCLE_KIND,
        "schedule_run_kind": HOLDING_CYCLE_KIND,
        "schedule_triggered_at": generated_at,
        "holding_cycle_only": True,
        "holding_cycle_codes": codes,
        "items": [],
        "trade_items": [],
        "observed_items": [],
        "count": 0,
        "trade_count": 0,
        "holding_cycle_data_status": "empty_portfolio" if not codes else "ready",
    }
    for key in (
        "market_snapshot",
        "market_summary",
        "market_decision_context",
        "sector_tide_context",
        "niuone_context",
        "zettaranc_context",
    ):
        value = source.get(key)
        if isinstance(value, Mapping):
            base[key] = dict(value)
    if not codes:
        return base

    try:
        scorers, prompt_store, prompt_version, minimum_rows = _active_scorers()
    except Exception as exc:
        base["holding_cycle_data_status"] = "scorer_config_unavailable"
        base["holding_cycle_error"] = type(exc).__name__
        return base
    try:
        base.update({
            "strategy_suite": active_strategy_suite(
                active_strategy_setting(),
                strategy_source_setting(),
                enabled_persona_strategy_setting(),
            ),
            "enabled_strategy_ids": sorted(scorers),
            "strategy_meta": active_strategy_meta(),
            "strategy_score_profiles": active_strategy_score_profiles(),
        })
    except Exception as exc:
        base["holding_cycle_data_status"] = "scorer_config_unavailable"
        base["holding_cycle_error"] = type(exc).__name__
        return base
    if not scorers:
        base["holding_cycle_data_status"] = "no_active_scorers"
        return base

    symbols = [_tencent_symbol(code) for code in codes]
    try:
        raw_quotes = quote_fetcher(
            symbols,
            timeout_seconds=DEFAULT_QUOTE_TIMEOUT_SECONDS,
            max_attempts=DEFAULT_QUOTE_MAX_ATTEMPTS,
            backoff_seconds=0.25,
            batch_label="practice-holdings",
        )
    except Exception as exc:
        base["holding_cycle_data_status"] = "quote_unavailable"
        base["holding_cycle_error"] = type(exc).__name__
        return base
    quotes = {
        str(symbol): dict(quote)
        for symbol, quote in (raw_quotes or {}).items()
        if isinstance(quote, Mapping)
    }
    today = current.strftime("%Y-%m-%d")
    quotes = {
        symbol: quote
        for symbol, quote in quotes.items()
        if _quote_date(quote.get("quote_time")) == today
    }
    if not quotes:
        base["holding_cycle_data_status"] = "stale_or_missing_quotes"
        return base

    try:
        as_of_date, previous_trading_day = resolve_quote_trading_dates(
            quotes,
            now=current,
        )
        accepted_dates = {
            value for value in (as_of_date, previous_trading_day) if value
        }
        requested_rows = max(DEFAULT_KLINE_COUNT, minimum_rows)
        histories = history_loader(
            symbols,
            path=kline_cache_path(),
            accepted_last_dates=accepted_dates,
            min_rows=minimum_rows,
            count=requested_rows,
        )
        if not isinstance(histories, Mapping):
            raise TypeError("daily K-line cache returned a non-mapping payload")
    except Exception as exc:
        base["holding_cycle_data_status"] = "history_unavailable"
        base["holding_cycle_error"] = type(exc).__name__
        return base
    try:
        board_map = board_loader(set(codes))
        base["holding_cycle_board_status"] = "ready"
    except Exception as exc:
        board_map = {}
        base["holding_cycle_board_status"] = "unavailable"
        base["holding_cycle_board_error"] = type(exc).__name__
    if not isinstance(board_map, Mapping):
        board_map = {}
        base["holding_cycle_board_status"] = "invalid"
    strategy_context = _strategy_context(scorers, source)
    prompt_only = bool(
        prompt_version is not None
        and set(scorers) == {STRATEGY_SUITE_PRESET_TEXT}
    )
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    scoring_errors: Counter[str] = Counter()
    for code in codes:
        holding = by_code[code]
        symbol = _tencent_symbol(code)
        quote = quotes.get(symbol)
        history = histories.get(symbol)
        if not quote or not history:
            failures.append(code)
            continue
        classification = board_map.get(code)
        factual_industry = str(
            (
                classification.get("industry")
                if isinstance(classification, Mapping)
                else getattr(classification, "industry", "")
            )
            or holding.get("industry")
            or holding.get("sector")
            or ""
        ).strip()
        name = str(
            holding.get("name") or quote.get("name") or code
        ).strip()
        rows = prepare_strategy_rows(
            code,
            symbol,
            quote=dict(quote),
            name=name,
            industry=factual_industry,
            historical_rows=history,
            kline_loader=lambda _symbol, _count: [],
            kline_count=requested_rows,
            enrich_legacy_indicators=not prompt_only,
            minimum_rows=minimum_rows,
        )
        if not rows:
            failures.append(code)
            continue
        try:
            multi = analyze_all_strategies(
                code,
                symbol,
                quote=dict(quote),
                name=name,
                industry=factual_industry,
                rows=rows,
                context=strategy_context,
                scorers=scorers,
                kline_count=requested_rows,
                enrich_legacy_indicators=not prompt_only,
                minimum_rows=minimum_rows,
            )
        except Exception as exc:
            failures.append(code)
            scoring_errors[type(exc).__name__] += 1
            continue
        if not isinstance(multi, Mapping):
            continue
        results.append(
            _project_candidate(
                code,
                name,
                quote,
                factual_industry,
                multi,
            )
        )

    trade_items = select_trade_candidates(
        results,
        limit=max(1, len(results)),
    ) if results else []
    if prompt_store is not None and prompt_version is not None:
        audits = [
            item["prompt_rule_audit"]
            for item in results
            if isinstance(item.get("prompt_rule_audit"), dict)
        ]
        if audits:
            prompt_store.record_evaluations_batch(
                str(prompt_version.get("version_id") or ""),
                audits,
            )
    base.update({
        "items": trade_items,
        "trade_items": trade_items,
        "observed_items": results,
        "count": len(trade_items),
        "trade_count": len(trade_items),
        "holding_cycle_analysis_count": len(results),
        "holding_cycle_unavailable_codes": failures,
        "holding_cycle_scoring_errors": [
            {"error_type": error_type, "count": count}
            for error_type, count in sorted(scoring_errors.items())
        ],
        "holding_cycle_data_status": (
            "ready"
            if len(results) == len(codes)
            else "partial"
            if results
            else "scoring_error"
            if scoring_errors
            else "scoring_unavailable"
        ),
    })
    return base


__all__ = [
    "DEFAULT_QUOTE_MAX_ATTEMPTS",
    "DEFAULT_QUOTE_TIMEOUT_SECONDS",
    "HOLDING_CYCLE_KIND",
    "build_holding_cycle_payload",
    "merge_niuone_holding_cycle_context",
]
