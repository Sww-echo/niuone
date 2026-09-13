"""Recover today's missing market-breadth minutes from verified quote bars.

The live chart is sampled from Tencent full-market snapshots.  If its durable
history is truncated, today's Tencent one-minute OHLC bars can reconstruct
minute-boundary observations while a fresh Tencent snapshot supplies the exact
security universe and daily price limits.  Existing observations always win
at identical timestamps, and both active files are backed up before writing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.request import Request, urlopen

from app.core.json_cache import read_json_cache, write_json_cache
from app.core.paths import get_dashboard_home
from app.dashboard.apis.market_breadth import (
    compact_market_breadth_sample,
    is_market_breadth_session_timestamp,
    roll_market_breadth_history,
)
from app.market_data.eastmoney_turnover import (
    ESTIMATE_MODEL,
    ESTIMATE_MODEL_LABEL,
    PROFILE_INTERVAL_MINUTES,
    SOURCE_NAME as TURNOVER_SOURCE_NAME,
    SOURCE_URL as TURNOVER_SOURCE_URL,
    estimate_full_day_turnover_yi,
    fetch_turnover_profile,
    trading_progress_minutes,
)
from app.market_data.tencent_market_breadth import (
    SOURCE_URL as TENCENT_SOURCE_URL,
    UNIVERSE_LABEL,
    add_turnover_comparison,
    fetch_previous_market_turnover,
    fetch_tencent_market_breadth,
)


CN_TZ = dt.timezone(dt.timedelta(hours=8))
RECOVERY_SOURCE_NAME = "腾讯证券1分钟K线重建（实时股票池及涨跌停价校验）"
RECOVERY_SOURCE_URL = TENCENT_SOURCE_URL
RECOVERY_TURNOVER_SOURCE_NAME = "腾讯证券沪深指数1分钟线"
TENCENT_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
TENCENT_MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/minute/query"
DEFAULT_WORKERS = 8
MAX_WORKERS = 16
DEFAULT_TIMEOUT_SECONDS = 6.0
DEFAULT_DEADLINE_SECONDS = 900.0
DEFAULT_ATTEMPTS = 3
PROGRESS_EVERY = 250
MAX_CONSECUTIVE_FAILURES = 20
MIN_VALIDATION_POINTS = 3
_PERSIST_LOCK = threading.Lock()
_REQUEST_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_REQUEST_SPACING_SECONDS = 0.125


def _finite_float(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _download_stock_kline(
    symbol: str,
    day: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
) -> str:
    global _NEXT_REQUEST_AT
    del day
    request = Request(
        f"{TENCENT_KLINE_URL}?param={symbol},m1,,320",
        headers={
            "User-Agent": "Mozilla/5.0 NiuOne/1.0",
            "Referer": "https://gu.qq.com/",
            "Connection": "close",
        },
    )
    last_error: Exception | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            with _REQUEST_RATE_LOCK:
                now = time.monotonic()
                wait_seconds = max(0.0, _NEXT_REQUEST_AT - now)
                if wait_seconds:
                    time.sleep(wait_seconds)
                _NEXT_REQUEST_AT = max(now, _NEXT_REQUEST_AT) + _REQUEST_SPACING_SECONDS
            with urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
                return response.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.2 * (2**attempt))
    if last_error is not None:
        raise last_error
    raise RuntimeError("Tencent minute K-line request did not run")


def _download_index_minutes(
    symbol: str,
    day: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
) -> str:
    del day
    request = Request(
        f"{TENCENT_MINUTE_URL}?code={symbol}",
        headers={
            "User-Agent": "Mozilla/5.0 NiuOne/1.0",
            "Referer": "https://gu.qq.com/",
            "Connection": "close",
        },
    )
    last_error: Exception | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            with urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
                return response.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.2 * (2**attempt))
    if last_error is not None:
        raise last_error
    raise RuntimeError("Tencent index minute request did not run")


def parse_stock_minute_bars(
    body: str,
    symbol: str,
    day: str,
) -> list[dict[str, Any]]:
    """Strictly parse one stock's unadjusted one-minute OHLC bars."""

    payload = json.loads(str(body or "{}"))
    root_data = payload.get("data") if isinstance(payload, dict) else None
    tencent_data = (
        root_data.get(symbol)
        if isinstance(root_data, dict) and isinstance(root_data.get(symbol), dict)
        else None
    )
    if not isinstance(tencent_data, dict):
        return []
    bars: list[dict[str, Any]] = []
    day_key = day.replace("-", "")
    for raw in tencent_data.get("m1") or []:
        if not isinstance(raw, list) or not raw:
            continue
        timestamp = str(raw[0] or "")
        if not timestamp.startswith(day_key):
            continue
        if len(raw) < 5 or not re.fullmatch(r"\d{12}", timestamp):
            raise ValueError("Tencent minute response contains a malformed timestamp")
        values = [_finite_float(raw[index]) for index in (2, 3, 4)]
        if any(value is None or float(value) <= 0 for value in values):
            raise ValueError("Tencent minute response contains an invalid OHLC bar")
        moment = dt.datetime.strptime(timestamp, "%Y%m%d%H%M")
        bars.append({
            "generated_at": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "minute": moment.hour * 60 + moment.minute,
            "close": float(values[0]),
            "high": float(values[1]),
            "low": float(values[2]),
        })
    bars.sort(key=lambda item: item["minute"])
    if len({bar["minute"] for bar in bars}) != len(bars):
        raise ValueError("Tencent minute response contains duplicate minutes")
    return bars


def _session_minutes(
    day: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[dt.datetime]:
    if start.date().isoformat() != day or end.date().isoformat() != day or end < start:
        return []
    result: list[dt.datetime] = []
    current = start.replace(second=0, microsecond=0)
    while current <= end:
        if is_market_breadth_session_timestamp(current):
            result.append(current)
        current += dt.timedelta(minutes=1)
    return result


def plan_market_breadth_recovery(
    day: str,
    existing: Iterable[dict[str, Any]],
    *,
    expected_through: dt.datetime | None = None,
    allow_pre_gap_validation: bool = False,
) -> dict[str, Any]:
    """Describe the safe current-day recovery window without fetching data."""

    current_day: list[dict[str, Any]] = []
    for raw in existing:
        sample = compact_market_breadth_sample(
            raw if isinstance(raw, dict) else None
        )
        if (
            sample is not None
            and sample["generated_at"][:10] == day
            and is_market_breadth_session_timestamp(sample["generated_at"])
        ):
            current_day.append(sample)
    current_day.sort(key=lambda item: item["generated_at"])
    if not current_day:
        return {
            "status": "waiting_boundary",
            "current_samples": [],
            "backfill_targets": [],
            "validation_targets": [],
        }

    existing_minutes = {
        dt.datetime.strptime(
            sample["generated_at"],
            "%Y-%m-%d %H:%M:%S",
        ).replace(second=0, microsecond=0)
        for sample in current_day
    }
    latest_existing = max(existing_minutes)
    recovery_end = (
        expected_through.replace(second=0, microsecond=0)
        if expected_through is not None
        else latest_existing
    )
    start = dt.datetime.combine(latest_existing.date(), dt.time(9, 31))
    expected_minutes = _session_minutes(day, start, recovery_end)
    backfill_targets = [
        moment for moment in expected_minutes
        if moment not in existing_minutes
    ]
    if not backfill_targets:
        return {
            "status": "complete",
            "current_samples": current_day,
            "backfill_targets": [],
            "validation_targets": [],
        }

    latest_missing = max(backfill_targets)
    available_validation_minutes = sorted(
        moment for moment in existing_minutes
        if moment > latest_missing
    )
    validation_targets = available_validation_minutes[:5]
    if allow_pre_gap_validation and len(validation_targets) < MIN_VALIDATION_POINTS:
        selected = set(validation_targets)
        earlier_candidates = sorted(
            (
                moment for moment in existing_minutes
                if moment < latest_missing and moment not in selected
            ),
            reverse=True,
        )
        validation_targets.extend(
            earlier_candidates[: 5 - len(validation_targets)]
        )
        validation_targets.sort()
    return {
        "status": (
            "ready"
            if len(validation_targets) >= MIN_VALIDATION_POINTS
            else "waiting_validation"
        ),
        "current_samples": current_day,
        "backfill_targets": backfill_targets,
        "validation_targets": validation_targets,
    }


def _empty_aggregate(moment: dt.datetime) -> dict[str, Any]:
    return {
        "generated_at": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "quote_count": 0,
        "limit_price_count": 0,
        "turnover_amount_count": 0,
        "red": 0,
        "green": 0,
        "flat": 0,
        "limit_up": 0,
        "limit_down": 0,
        "broken_limit": 0,
    }


def add_stock_to_aggregates(
    aggregates: list[dict[str, Any]],
    target_minutes: list[int],
    quote: Mapping[str, Any],
    bars: list[dict[str, Any]],
) -> None:
    """Add one security's exact minute states to all target aggregates."""

    previous_close = _finite_float(quote.get("prev_close"))
    upper_limit = _finite_float(quote.get("upper_limit"))
    lower_limit = _finite_float(quote.get("lower_limit"))
    if previous_close is None or previous_close <= 0:
        raise ValueError("Tencent reference quote is missing previous close")
    has_limits = (
        upper_limit is not None
        and lower_limit is not None
        and upper_limit > 0
        and lower_limit > 0
    )
    price = previous_close
    high_seen = previous_close
    bar_index = 0
    for aggregate, target_minute in zip(aggregates, target_minutes):
        while bar_index < len(bars) and int(bars[bar_index]["minute"]) <= target_minute:
            price = float(bars[bar_index]["close"])
            high_seen = max(high_seen, float(bars[bar_index]["high"]))
            bar_index += 1
        aggregate["quote_count"] += 1
        aggregate["turnover_amount_count"] += 1
        if price > previous_close:
            aggregate["red"] += 1
        elif price < previous_close:
            aggregate["green"] += 1
        else:
            aggregate["flat"] += 1
        if not has_limits:
            continue
        aggregate["limit_price_count"] += 1
        if price >= float(upper_limit):
            aggregate["limit_up"] += 1
        elif high_seen >= float(upper_limit):
            aggregate["broken_limit"] += 1
        if price <= float(lower_limit):
            aggregate["limit_down"] += 1


def aggregate_recovered_minutes(
    quotes: Mapping[str, Mapping[str, Any]],
    bars_by_symbol: Mapping[str, list[dict[str, Any]]],
    targets: list[dt.datetime],
) -> list[dict[str, Any]]:
    aggregates = [_empty_aggregate(target) for target in targets]
    target_minutes = [target.hour * 60 + target.minute for target in targets]
    for symbol, quote in quotes.items():
        bars = bars_by_symbol.get(symbol)
        if bars is None:
            raise ValueError("a reference security has no verified minute result")
        add_stock_to_aggregates(aggregates, target_minutes, quote, bars)
    for aggregate in aggregates:
        aggregate.update({
            "source": RECOVERY_SOURCE_NAME,
            "source_url": RECOVERY_SOURCE_URL,
            "universe": UNIVERSE_LABEL,
        })
    return aggregates


def _fetch_reference_quotes() -> dict[str, dict[str, Any]]:
    holder: dict[str, Any] = {}
    snapshot = fetch_tencent_market_breadth(
        min_rows=5_000,
        turnover_estimate_fetcher=lambda _generated, _actual: {},
        previous_turnover_fetcher=lambda _day: None,
        quote_snapshot_consumer=lambda value: holder.update(value),
    )
    quotes = holder.get("quotes")
    quote_count = int(snapshot.get("quote_count") or 0)
    if (
        not isinstance(quotes, dict)
        or len(quotes) != quote_count
        or int(snapshot.get("turnover_amount_count") or 0) != quote_count
    ):
        raise RuntimeError("Tencent reference snapshot did not retain its full quote set")
    if str(snapshot.get("generated_at") or "")[:10] != dt.datetime.now(CN_TZ).date().isoformat():
        raise RuntimeError("Tencent reference snapshot is not from today")
    return {
        str(symbol): dict(quote)
        for symbol, quote in quotes.items()
        if isinstance(quote, Mapping)
    }


def fetch_stock_minute_bars(
    quotes: Mapping[str, Mapping[str, Any]],
    day: str,
    *,
    workers: int = DEFAULT_WORKERS,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    downloader: Callable[..., str] = _download_stock_kline,
    progress: Callable[[int, int, int], None] | None = None,
    consumer: Callable[[str, list[dict[str, Any]]], None] | None = None,
    checkpoint: Callable[[set[str]], None] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch every reference security with a bounded pool and strict coverage."""

    ordered = list(quotes.items())
    deadline = time.monotonic() + max(30.0, float(deadline_seconds))

    def fetch_one(item: tuple[str, Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        symbol, quote = item
        body = downloader(symbol, day)
        bars = parse_stock_minute_bars(body, symbol, day)
        current_volume = _finite_float(quote.get("volume"))
        if not bars and (current_volume is None or current_volume > 0):
            raise ValueError("active Tencent security has no Tencent minute bars")
        return symbol, bars

    results: dict[str, list[dict[str, Any]]] = {}
    verified_symbols: set[str] = set()
    errors: list[str] = []
    consecutive_failures = 0
    pool = ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, int(workers))))
    try:
        futures = [pool.submit(fetch_one, item) for item in ordered]
        try:
            for completed, future in enumerate(
                as_completed(futures, timeout=max(1.0, deadline - time.monotonic())),
                start=1,
            ):
                try:
                    symbol, bars = future.result()
                    if consumer is None:
                        results[symbol] = bars
                    else:
                        consumer(symbol, bars)
                    verified_symbols.add(symbol)
                    consecutive_failures = 0
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    consecutive_failures += 1
                if progress is not None and (
                    completed % PROGRESS_EVERY == 0 or completed == len(futures)
                ):
                    progress(completed, len(futures), len(verified_symbols))
                if checkpoint is not None and (
                    completed % PROGRESS_EVERY == 0 or completed == len(futures)
                ):
                    checkpoint(set(verified_symbols))
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    errors.append("ConsecutiveUpstreamFailures")
                    break
        except FuturesTimeoutError:
            errors.append("TimeoutError")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if checkpoint is not None:
        checkpoint(set(verified_symbols))
    if errors or len(verified_symbols) != len(ordered):
        first_error = errors[0] if errors else "IncompleteCoverage"
        raise RuntimeError(
            f"minute recovery covered {len(verified_symbols)}/{len(ordered)} securities; "
            f"failures={len(errors)} first_error={first_error}"
        )
    return results


def fetch_turnover_series(
    day: str,
    *,
    downloader: Callable[..., str] = _download_index_minutes,
) -> dict[float, float]:
    """Return cumulative Shanghai+Shenzhen turnover by session progress."""

    by_exchange: list[dict[float, float]] = []
    for symbol in ("sh000001", "sz399001"):
        body = downloader(symbol, day)
        payload = json.loads(str(body or "{}"))
        rows = (
            (((payload.get("data") or {}).get(symbol) or {}).get("data") or {}).get("data")
            if isinstance(payload, dict)
            else None
        )
        parsed: dict[float, float] = {}
        for raw in rows or []:
            fields = str(raw or "").split()
            if len(fields) < 4 or not re.fullmatch(r"\d{4}", fields[0]):
                continue
            progress = trading_progress_minutes(
                f"{day} {fields[0][:2]}:{fields[0][2:]}:00"
            )
            amount = _finite_float(fields[3])
            if progress is not None and amount is not None and amount >= 0:
                parsed[progress] = amount
        if not parsed:
            raise ValueError("Tencent index minute turnover is unavailable")
        by_exchange.append(parsed)
    progresses = sorted(set().union(*(set(values) for values in by_exchange)))
    cumulative: dict[float, float] = {}
    for progress in progresses:
        latest_amounts: list[float] = []
        for exchange in by_exchange:
            eligible = [minute for minute in exchange if minute <= progress]
            if not eligible:
                break
            latest_amounts.append(exchange[max(eligible)])
        if len(latest_amounts) == len(by_exchange):
            cumulative[progress] = sum(latest_amounts)
    return cumulative


def _turnover_at(
    series: Mapping[float, float],
    moment: dt.datetime,
) -> float:
    progress = trading_progress_minutes(moment)
    if progress is None:
        raise ValueError("turnover requested outside the A-share session")
    eligible = [minute for minute in series if minute <= progress]
    if not eligible:
        return 0.0
    return round(float(series[max(eligible)]) / 100_000_000, 2)


def enrich_turnover(
    samples: Iterable[dict[str, Any]],
    turnover_series: Mapping[float, float],
    *,
    profile: dict[str, Any] | None = None,
    previous_turnover: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in samples:
        sample = dict(raw)
        moment = dt.datetime.strptime(sample["generated_at"], "%Y-%m-%d %H:%M:%S")
        actual = _turnover_at(turnover_series, moment)
        sample.update({
            "actual_turnover_yi": actual,
            "turnover_actual_source": RECOVERY_TURNOVER_SOURCE_NAME,
            "turnover_actual_source_url": TENCENT_SOURCE_URL,
        })
        if profile is not None:
            estimated = estimate_full_day_turnover_yi(actual, moment, profile)
            if estimated is not None:
                sample.update({
                    "estimated_turnover_yi": estimated,
                    "turnover_estimate_model": str(profile.get("model") or ESTIMATE_MODEL),
                    "turnover_estimate_model_label": str(
                        profile.get("model_label") or ESTIMATE_MODEL_LABEL
                    ),
                    "turnover_estimate_source": str(
                        profile.get("source") or TURNOVER_SOURCE_NAME
                    ),
                    "turnover_estimate_source_url": str(
                        profile.get("source_url") or TURNOVER_SOURCE_URL
                    ),
                    "turnover_profile_days": int(profile.get("profile_days") or 0),
                    "turnover_profile_start": str(profile.get("profile_start") or ""),
                    "turnover_profile_end": str(profile.get("profile_end") or ""),
                    "turnover_profile_interval_minutes": int(
                        profile.get("interval_minutes") or PROFILE_INTERVAL_MINUTES
                    ),
                })
        sample = add_turnover_comparison(sample, previous_turnover)
        compact = compact_market_breadth_sample(sample)
        if compact is None:
            raise ValueError("recovered market-breadth sample failed validation")
        result.append(compact)
    return result


def _history_sources(history: dict[str, Any] | None) -> list[Any]:
    source = history if isinstance(history, dict) else {}
    values: list[Any] = list(source.get("samples") or [])
    for key in ("previous_day", "previous_turnover"):
        archive = source.get(key)
        if isinstance(archive, dict):
            values.extend(archive.get("samples") or [])
    return values


def merge_recovered_history(
    day: str,
    recovered_samples: Iterable[dict[str, Any]],
    *histories: dict[str, Any] | None,
    interval_seconds: int = 30,
) -> dict[str, Any]:
    """Merge recovered points first so existing real timestamps take priority."""

    values: list[Any] = list(recovered_samples)
    for history in histories:
        values.extend(_history_sources(history))
    return roll_market_breadth_history(
        {"samples": values},
        day,
        interval_seconds=interval_seconds,
    )


def validation_summary(
    reconstructed: Iterable[dict[str, Any]],
    existing: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    by_minute = {
        str(sample.get("generated_at") or "")[:16]: sample
        for sample in reconstructed
        if isinstance(sample, dict)
    }
    comparisons: list[dict[str, float]] = []
    for raw in existing:
        sample = compact_market_breadth_sample(raw if isinstance(raw, dict) else None)
        if sample is None:
            continue
        rebuilt = by_minute.get(sample["generated_at"][:16])
        if rebuilt is None:
            continue
        comparisons.append({
            key: abs(float(sample[key]) - float(rebuilt[key]))
            for key in (
                "quote_count",
                "red",
                "green",
                "flat",
                "limit_up",
                "limit_down",
                "broken_limit",
                "actual_turnover_yi",
            )
            if key in sample and key in rebuilt
        })
    summary: dict[str, Any] = {"comparison_points": len(comparisons)}
    for key in (
        "quote_count",
        "red",
        "green",
        "flat",
        "limit_up",
        "limit_down",
        "broken_limit",
        "actual_turnover_yi",
    ):
        values = [item[key] for item in comparisons if key in item]
        summary[f"max_{key}_difference"] = round(max(values), 2) if values else None
    return summary


def validation_is_safe(summary: Mapping[str, Any], quote_count: int) -> bool:
    if int(summary.get("comparison_points") or 0) < MIN_VALIDATION_POINTS:
        return False
    limits = {
        "max_quote_count_difference": 0,
        "max_red_difference": max(30, quote_count * 0.02),
        "max_green_difference": max(30, quote_count * 0.02),
        "max_flat_difference": max(30, quote_count * 0.02),
        "max_limit_up_difference": 10,
        "max_limit_down_difference": 10,
        "max_broken_limit_difference": 10,
        "max_actual_turnover_yi_difference": 150,
    }
    for key, maximum in limits.items():
        value = _finite_float(summary.get(key))
        if value is None or value > maximum:
            return False
    return True


def persist_recovered_history(
    history_file: Path,
    recovered_samples: list[dict[str, Any]],
    day: str,
    *,
    backup_root: Path,
    attempts: int = 3,
) -> tuple[dict[str, Any], Path]:
    """Back up, atomically merge recovery first, and verify both live files."""

    recovery_file = history_file.with_name(
        f"{history_file.stem}.recovery{history_file.suffix or '.json'}"
    )
    stamp = dt.datetime.now(CN_TZ).strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / f"market-breadth-before-recovery-{stamp}"
    expected_times = {sample["generated_at"] for sample in recovered_samples}
    with _PERSIST_LOCK:
        backup_dir.mkdir(parents=True, exist_ok=False)
        for source in (history_file, recovery_file):
            if source.exists():
                shutil.copy2(source, backup_dir / source.name)
        merged: dict[str, Any] = {}
        for _attempt in range(max(1, int(attempts))):
            current = read_json_cache(history_file)
            recovery = read_json_cache(recovery_file)
            interval = int(
                (current or {}).get("interval_seconds")
                or (recovery or {}).get("interval_seconds")
                or 30
            )
            merged = merge_recovered_history(
                day,
                recovered_samples,
                recovery,
                current,
                interval_seconds=interval,
            )
            write_json_cache(recovery_file, merged)
            write_json_cache(history_file, merged)
            time.sleep(0.2)
            verified = [read_json_cache(recovery_file), read_json_cache(history_file)]
            if all(
                expected_times.issubset({
                    str(sample.get("generated_at") or "")
                    for sample in (payload or {}).get("samples") or []
                    if isinstance(sample, dict)
                })
                for payload in verified
            ):
                return merged, backup_dir
        raise RuntimeError("concurrent sampler prevented verified history persistence")


def _load_existing_samples(history_file: Path, recovery_file: Path) -> list[dict[str, Any]]:
    day = dt.datetime.now(CN_TZ).date().isoformat()
    merged = merge_recovered_history(
        day,
        [],
        read_json_cache(recovery_file),
        read_json_cache(history_file),
    )
    return [dict(sample) for sample in merged.get("samples") or []]


def _recovery_fingerprint(
    day: str,
    quotes: Mapping[str, Mapping[str, Any]],
    targets: Iterable[dt.datetime],
) -> str:
    digest = hashlib.sha256()
    digest.update(day.encode("ascii"))
    for target in targets:
        digest.update(target.strftime("%Y-%m-%d %H:%M:%S").encode("ascii"))
    for symbol in sorted(quotes):
        quote = quotes[symbol]
        values = (
            symbol,
            str(quote.get("prev_close") or ""),
            str(quote.get("upper_limit") or ""),
            str(quote.get("lower_limit") or ""),
        )
        digest.update("|".join(values).encode("utf-8"))
    return digest.hexdigest()


def load_recovery_checkpoint(
    path: Path,
    *,
    fingerprint: str,
    targets: list[dt.datetime],
) -> tuple[list[dict[str, Any]], set[str]]:
    payload = read_json_cache(path)
    target_times = [target.strftime("%Y-%m-%d %H:%M:%S") for target in targets]
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or str(payload.get("fingerprint") or "") != fingerprint
        or payload.get("target_times") != target_times
    ):
        return [_empty_aggregate(target) for target in targets], set()
    aggregates = payload.get("aggregates")
    symbols = payload.get("verified_symbols")
    if (
        not isinstance(aggregates, list)
        or len(aggregates) != len(targets)
        or not isinstance(symbols, list)
        or any(not isinstance(item, dict) for item in aggregates)
    ):
        return [_empty_aggregate(target) for target in targets], set()
    return [dict(item) for item in aggregates], {str(symbol) for symbol in symbols}


def save_recovery_checkpoint(
    path: Path,
    *,
    fingerprint: str,
    targets: list[dt.datetime],
    aggregates: list[dict[str, Any]],
    verified_symbols: set[str],
) -> None:
    write_json_cache(path, {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "target_times": [
            target.strftime("%Y-%m-%d %H:%M:%S") for target in targets
        ],
        "verified_symbols": sorted(verified_symbols),
        "aggregates": aggregates,
    })


def _progress(completed: int, total: int, verified: int) -> None:
    print(
        f"Minute bars: requests={completed}/{total} verified={verified}",
        flush=True,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recover today's missing A-share market-breadth minutes from real OHLC bars.",
    )
    parser.add_argument("--write", action="store_true", help="persist after backup and validation")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--deadline-seconds", type=float, default=DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--requests-per-second", type=float, default=8.0)
    return parser


def _project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "app").is_dir() and (candidate / "scripts").is_dir():
            return candidate
    raise RuntimeError("NiuOne project root is unavailable")


def main(argv: list[str] | None = None) -> int:
    global _REQUEST_SPACING_SECONDS
    args = build_argument_parser().parse_args(argv)
    _REQUEST_SPACING_SECONDS = 1.0 / max(1.0, min(20.0, args.requests_per_second))
    root = _project_root()
    dashboard_home = get_dashboard_home(root)
    output_dir = dashboard_home / "cron" / "output"
    history_file = output_dir / "market_breadth_history.json"
    recovery_file = output_dir / "market_breadth_history.recovery.json"
    current = dt.datetime.now(CN_TZ).replace(tzinfo=None)
    day = current.date().isoformat()
    existing = _load_existing_samples(history_file, recovery_file)
    after_close = current.time() >= dt.time(15, 1)
    plan = plan_market_breadth_recovery(
        day,
        existing,
        expected_through=(
            dt.datetime.combine(current.date(), dt.time(15, 0))
            if after_close
            else None
        ),
        allow_pre_gap_validation=after_close,
    )
    current_day = plan["current_samples"]
    if plan["status"] == "waiting_boundary":
        print("Recovery stopped: no verified current-day boundary point exists.", file=sys.stderr)
        return 2
    if plan["status"] == "complete":
        print("No missing current-day market-breadth minutes were found.")
        return 0
    if plan["status"] == "waiting_validation":
        print(
            "Recovery stopped: fewer than three verified validation minutes exist.",
            file=sys.stderr,
        )
        return 2
    backfill_targets = plan["backfill_targets"]
    validation_targets = plan["validation_targets"]
    all_targets = backfill_targets + validation_targets
    print(
        f"Recovery targets: {backfill_targets[0]:%H:%M}-{backfill_targets[-1]:%H:%M}; "
        f"minutes={len(backfill_targets)}",
        flush=True,
    )
    print("Fetching a fresh Tencent reference universe...", flush=True)
    quotes = _fetch_reference_quotes()
    print(f"Reference universe: {len(quotes)} verified securities", flush=True)
    checkpoint_file = (
        dashboard_home
        / "market_data"
        / f"market_breadth_recovery_{day}.json"
    )
    fingerprint = _recovery_fingerprint(day, quotes, all_targets)
    aggregates, previously_verified = load_recovery_checkpoint(
        checkpoint_file,
        fingerprint=fingerprint,
        targets=all_targets,
    )
    previously_verified.intersection_update(quotes)
    if previously_verified:
        print(
            f"Checkpoint resumed: {len(previously_verified)}/{len(quotes)} securities",
            flush=True,
        )
    target_minutes = [target.hour * 60 + target.minute for target in all_targets]

    def consume_bars(symbol: str, bars: list[dict[str, Any]]) -> None:
        add_stock_to_aggregates(
            aggregates,
            target_minutes,
            quotes[symbol],
            bars,
        )

    def checkpoint_verified(symbols: set[str]) -> None:
        save_recovery_checkpoint(
            checkpoint_file,
            fingerprint=fingerprint,
            targets=all_targets,
            aggregates=aggregates,
            verified_symbols=previously_verified | symbols,
        )

    pending_quotes = {
        symbol: quote
        for symbol, quote in quotes.items()
        if symbol not in previously_verified
    }
    fetch_stock_minute_bars(
        pending_quotes,
        day,
        workers=args.workers,
        deadline_seconds=args.deadline_seconds,
        progress=_progress,
        consumer=consume_bars,
        checkpoint=checkpoint_verified,
    )
    for aggregate in aggregates:
        aggregate.update({
            "source": RECOVERY_SOURCE_NAME,
            "source_url": RECOVERY_SOURCE_URL,
            "universe": UNIVERSE_LABEL,
        })
    reconstructed = aggregates
    turnover_series = fetch_turnover_series(day)
    try:
        profile = fetch_turnover_profile(
            dt.date.fromisoformat(day),
            persistent_cache_path=output_dir / "turnover_profile_cache.json",
        )
    except Exception as exc:
        profile = None
        print(f"Turnover estimate retained as unavailable: {type(exc).__name__}", flush=True)
    try:
        previous = fetch_previous_market_turnover(dt.date.fromisoformat(day))
    except Exception as exc:
        previous = None
        print(f"Previous turnover comparison unavailable: {type(exc).__name__}", flush=True)
    reconstructed = enrich_turnover(
        reconstructed,
        turnover_series,
        profile=profile,
        previous_turnover=previous,
    )
    summary = validation_summary(reconstructed, current_day)
    print("Cross-check: " + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    if not validation_is_safe(summary, len(quotes)):
        print("Recovery stopped: cross-source validation exceeded safe bounds.", file=sys.stderr)
        return 3
    recovered = reconstructed[:len(backfill_targets)]
    if not args.write:
        print("Dry run passed. Re-run with --write to back up and merge recovered minutes.")
        return 0
    merged, backup_dir = persist_recovered_history(
        history_file,
        recovered,
        day,
        backup_root=dashboard_home / "backups",
    )
    print(
        f"Recovery persisted: added={len(recovered)} total={len(merged.get('samples') or [])} "
        f"backup={backup_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
