"""Bounded, auditable Market Flash evidence for A-share decisions."""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.monitoring.news import (
    NewsNowConfig,
    NewsNowConfigurationError,
    NewsNowService,
    shared_newsnow_service,
)
from app.reports.a_share.calendar import trading_day_status


CN_TZ = ZoneInfo("Asia/Shanghai")
A_SHARE_CLOSE_TIME = time(15, 0)
DEFAULT_DECISION_NEWS_MAX_ITEMS = 10
DECISION_NEWS_SCHEMA_VERSION = 1
TradingDayLookup = Callable[..., Mapping[str, Any]]


def _cn_datetime(value: datetime | None = None) -> datetime:
    current = value or datetime.now(CN_TZ)
    if current.tzinfo is None:
        return current.replace(tzinfo=CN_TZ)
    return current.astimezone(CN_TZ)


def _published_datetime(item: Mapping[str, Any]) -> datetime | None:
    raw_ms = item.get("published_at_ms")
    try:
        if raw_ms not in (None, ""):
            value = int(raw_ms)
            if 0 < value < 100_000_000_000:
                value *= 1000
            if 0 < value < 100_000_000_000_000:
                return datetime.fromtimestamp(value / 1000, tz=CN_TZ)
    except (OSError, OverflowError, TypeError, ValueError):
        pass
    text = str(item.get("published_at") or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _cn_datetime(parsed)


def _status_for(day: date, lookup: TradingDayLookup) -> Mapping[str, Any]:
    try:
        return lookup(day, allow_refresh=False)
    except TypeError:
        return lookup(day)


def _is_trading_day(day: date, lookup: TradingDayLookup) -> bool:
    try:
        return bool(_status_for(day, lookup).get("is_trading_day"))
    except Exception:
        return day.weekday() < 5


def _next_trading_day(day: date, lookup: TradingDayLookup) -> date:
    try:
        next_text = str(_status_for(day, lookup).get("next_trading_day") or "")[:10]
        if next_text:
            candidate = datetime.strptime(next_text, "%Y-%m-%d").date()
            if candidate > day:
                return candidate
    except (TypeError, ValueError):
        pass
    except Exception:
        pass
    for offset in range(1, 15):
        candidate = day + timedelta(days=offset)
        if _is_trading_day(candidate, lookup):
            return candidate
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def decision_target_trading_day(
    as_of: datetime | None = None,
    *,
    trading_day_lookup: TradingDayLookup = trading_day_status,
) -> date:
    """Return the session whose decision may consume news at ``as_of``."""

    current = _cn_datetime(as_of)
    if _is_trading_day(current.date(), trading_day_lookup) and current.time() < A_SHARE_CLOSE_TIME:
        return current.date()
    return _next_trading_day(current.date(), trading_day_lookup)


def news_effective_trading_day(
    published_at: datetime,
    *,
    trading_day_lookup: TradingDayLookup = trading_day_status,
) -> tuple[date, str]:
    """Map one item to its same-day or next-session decision role."""

    published = _cn_datetime(published_at)
    if (
        _is_trading_day(published.date(), trading_day_lookup)
        and published.time() < A_SHARE_CLOSE_TIME
    ):
        return published.date(), "intraday"
    return _next_trading_day(published.date(), trading_day_lookup), "next_trading_day_auxiliary"


def _compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def build_important_news_decision_context(
    payload: Mapping[str, Any] | None,
    *,
    as_of: datetime | None = None,
    max_items: int = DEFAULT_DECISION_NEWS_MAX_ITEMS,
    trading_day_lookup: TradingDayLookup = trading_day_status,
) -> dict[str, Any]:
    """Select important items that belong to the current decision session."""

    current = _cn_datetime(as_of)
    target_day = decision_target_trading_day(
        current,
        trading_day_lookup=trading_day_lookup,
    )
    source = payload if isinstance(payload, Mapping) else {}
    selected: list[dict[str, Any]] = []
    important_count = 0
    skipped_without_published_at = 0
    for raw in source.get("items") or []:
        if not isinstance(raw, Mapping) or raw.get("important") is not True:
            continue
        important_count += 1
        published = _published_datetime(raw)
        if published is None:
            skipped_without_published_at += 1
            continue
        if published > current:
            continue
        effective_day, role = news_effective_trading_day(
            published,
            trading_day_lookup=trading_day_lookup,
        )
        if effective_day != target_day:
            continue
        selected.append(
            {
                "id": _compact_text(raw.get("id"), 240),
                "source_id": _compact_text(raw.get("source_id"), 80),
                "source_name": _compact_text(raw.get("source_name"), 80),
                "title": _compact_text(raw.get("title"), 320),
                "summary": _compact_text(raw.get("summary"), 500),
                "published_at": published.isoformat(timespec="seconds"),
                "published_at_ms": int(published.timestamp() * 1000),
                "decision_trading_date": effective_day.isoformat(),
                "decision_role": role,
            }
        )
    limit = max(1, min(50, int(max_items or DEFAULT_DECISION_NEWS_MAX_ITEMS)))
    selected.sort(key=lambda item: int(item.get("published_at_ms") or 0), reverse=True)
    selected = selected[:limit]
    return {
        "schema_version": DECISION_NEWS_SCHEMA_VERSION,
        "enabled": True,
        "available": bool(selected),
        "status": str(source.get("status") or ("success" if selected else "empty")),
        "stale": bool(source.get("stale")),
        "as_of": current.isoformat(timespec="seconds"),
        "target_trading_date": target_day.isoformat(),
        "source_generated_at": str(source.get("generated_at") or ""),
        "important_item_count": important_count,
        "skipped_without_published_at": skipped_without_published_at,
        "items": selected,
        "error": str(source.get("error") or ""),
    }


def runtime_newsnow_config(env: Mapping[str, str] | None = None) -> NewsNowConfig:
    values = dict(os.environ if env is None else env)
    bundled_endpoint = str(values.get("NIUONE_BUNDLED_NEWSNOW_URL") or "").strip()
    if bundled_endpoint and not str(values.get("NEWSNOW_BASE_URL") or "").strip():
        values["NEWSNOW_BASE_URL"] = bundled_endpoint
    return NewsNowConfig.from_env(values)


def load_important_realtime_news_decision_context(
    cache_path: Path,
    *,
    enabled: bool,
    as_of: datetime | None = None,
    max_items: int = DEFAULT_DECISION_NEWS_MAX_ITEMS,
    env: Mapping[str, str] | None = None,
    service: NewsNowService | None = None,
    trading_day_lookup: TradingDayLookup = trading_day_status,
) -> dict[str, Any]:
    """Refresh NewsNow under its existing limits and build decision evidence."""

    current = _cn_datetime(as_of)
    if not enabled:
        return {
            "schema_version": DECISION_NEWS_SCHEMA_VERSION,
            "enabled": False,
            "available": False,
            "as_of": current.isoformat(timespec="seconds"),
            "items": [],
        }
    try:
        config = runtime_newsnow_config(env)
        payload = (service or shared_newsnow_service(cache_path)).get_news(config)
        return build_important_news_decision_context(
            payload,
            as_of=current,
            max_items=min(max_items, config.max_important_items),
            trading_day_lookup=trading_day_lookup,
        )
    except NewsNowConfigurationError as exc:
        error = exc.code
    except Exception as exc:
        error = f"{type(exc).__name__}"
    return {
        "schema_version": DECISION_NEWS_SCHEMA_VERSION,
        "enabled": True,
        "available": False,
        "status": "unavailable",
        "stale": False,
        "as_of": current.isoformat(timespec="seconds"),
        "target_trading_date": decision_target_trading_day(
            current,
            trading_day_lookup=trading_day_lookup,
        ).isoformat(),
        "items": [],
        "error": error,
    }


def format_important_realtime_news_for_prompt(context: Mapping[str, Any] | None) -> str:
    data = context if isinstance(context, Mapping) else {}
    if not data.get("enabled"):
        return ""
    target = str(data.get("target_trading_date") or "")
    lines = [
        "【财经快讯重要信息】",
        f"决策归属交易日：{target or '未知'}。",
        (
            "规则：A股交易日15:00前发布的信息辅助当日盘中决策；15:00后及休市日发布的信息只辅助下一交易日。"
            "快讯不能自行新增候选、放宽买入资格或突破仓位与风控。"
        ),
    ]
    items = data.get("items") or []
    if not items:
        status = str(data.get("status") or "empty")
        lines.append(f"当前没有归属该交易日的可用重要快讯（状态：{status}）。")
        return "\n".join(lines)
    for item in items:
        if not isinstance(item, Mapping):
            continue
        role = (
            "次日辅助"
            if item.get("decision_role") == "next_trading_day_auxiliary"
            else "当日盘中"
        )
        published = str(item.get("published_at") or "")
        display_time = published[11:16] if len(published) >= 16 else published
        source = str(item.get("source_name") or item.get("source_id") or "财经快讯")
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        detail = f"；{summary}" if summary and summary != title else ""
        lines.append(f"- [{role}][{display_time}] {source}：{title}{detail}")
    if data.get("stale"):
        lines.append("来源含陈旧缓存，仅可降级参考，不得据此提高仓位或确定性。")
    return "\n".join(lines)
