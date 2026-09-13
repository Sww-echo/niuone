"""Bounded NewsNow client and stale-safe realtime-news service."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlparse, urlunparse

from app.core.json_cache import read_json_cache, write_json_cache


DEFAULT_ENDPOINT = "https://newsnow.busiyi.world/api/s"
# The public endpoint's Cloudflare policy rejects application-style agents.
# Keep the caller identifiable while using its accepted browser-compatible form.
NEWSNOW_USER_AGENT = (
    "Mozilla/5.0 (compatible; NiuOne; +https://github.com/kunkundi/niuone)"
)
DEFAULT_SOURCE_IDS = ("cls-telegraph", "jin10", "wallstreetcn-quick")
NEWSNOW_SOURCE_REGISTRY_REVISION = "2173126f804bec0201769f59d933add6c4632d17"
SOURCE_CATEGORY_LABELS = {
    "finance": "财经商业",
    "china": "国内热点",
    "world": "国际资讯",
    "tech": "科技社区",
    "sports": "体育资讯",
}
SOURCE_CATEGORY_COLORS = {
    "finance": "#f59e0b",
    "china": "#ef4444",
    "world": "#8b5cf6",
    "tech": "#3b82f6",
    "sports": "#10b981",
}
SETTINGS_SOURCE_CATEGORY = "finance"

# Canonical sources from NewsNow's source registry. Redirect-only aliases are
# deliberately omitted so the settings page does not show duplicate choices.
_SOURCE_DEFINITIONS = (
    ("v2ex-share", "V2EX · 最新分享", "tech", 600),
    ("zhihu", "知乎", "china", 600),
    ("weibo", "微博 · 实时热搜", "china", 120),
    ("zaobao", "联合早报", "world", 1800),
    ("coolapk", "酷安 · 今日最热", "tech", 600),
    ("mktnews-flash", "MKTNews · 快讯", "finance", 120),
    ("wallstreetcn-quick", "华尔街见闻 · 快讯", "finance", 300),
    ("wallstreetcn-news", "华尔街见闻 · 最新", "finance", 1800),
    ("wallstreetcn-hot", "华尔街见闻 · 最热", "finance", 1800),
    ("36kr-quick", "36氪 · 快讯", "tech", 600),
    ("36kr-renqi", "36氪 · 人气榜", "tech", 600),
    ("douyin", "抖音", "china", 600),
    ("hupu", "虎扑 · 主干道热帖", "sports", 600),
    ("dongqiudi", "懂球帝 · 头条", "sports", 600),
    ("aihot", "AIHOT", "tech", 300),
    ("tieba", "百度贴吧 · 热议", "china", 600),
    ("toutiao", "今日头条", "china", 600),
    ("ithome", "IT之家", "tech", 600),
    ("thepaper", "澎湃新闻 · 热榜", "china", 1800),
    ("sputniknewscn", "卫星通讯社", "world", 600),
    ("cankaoxiaoxi", "参考消息", "world", 1800),
    ("pcbeta-windows11", "远景论坛 · Win11", "tech", 300),
    ("cls-telegraph", "财联社电报", "finance", 300),
    ("cls-depth", "财联社 · 深度", "finance", 600),
    ("cls-hot", "财联社热门", "finance", 600),
    ("xueqiu-hotstock", "雪球 · 热门股票", "finance", 120),
    ("gelonghui", "格隆汇 · 事件", "finance", 120),
    ("fastbull-express", "法布财经 · 快讯", "finance", 120),
    ("fastbull-news", "法布财经 · 头条", "finance", 1800),
    ("solidot", "Solidot", "tech", 3600),
    ("hackernews", "Hacker News", "tech", 600),
    ("producthunt", "Product Hunt", "tech", 600),
    ("github-trending-today", "Github · Today", "tech", 600),
    ("bilibili-hot-search", "哔哩哔哩 · 热搜", "china", 600),
    ("bilibili-hot-video", "哔哩哔哩 · 热门视频", "china", 600),
    ("bilibili-ranking", "哔哩哔哩 · 排行榜", "china", 1800),
    ("kuaishou", "快手", "china", 600),
    ("kaopu", "靠谱新闻", "world", 1800),
    ("jin10", "金十数据", "finance", 600),
    ("baidu", "百度热搜", "china", 600),
    ("nowcoder", "牛客", "china", 600),
    ("sspai", "少数派", "tech", 600),
    ("juejin", "稀土掘金", "tech", 600),
    ("ifeng", "凤凰网 · 热点资讯", "china", 600),
    ("chongbuluo-latest", "虫部落 · 最新", "china", 1800),
    ("chongbuluo-hot", "虫部落 · 最热", "china", 1800),
    ("douban", "豆瓣 · 热门电影", "china", 600),
    ("steam", "Steam · 在线人数", "world", 600),
    ("tencent-hot", "腾讯新闻 · 综合早报", "china", 1800),
    ("freebuf", "Freebuf · 网络安全", "china", 600),
    ("qqvideo-tv-hotsearch", "腾讯视频 · 热搜榜", "china", 1800),
    ("iqiyi-hot-ranklist", "爱奇艺 · 热播榜", "china", 1800),
)
SUPPORTED_SOURCES: dict[str, dict[str, Any]] = {
    source_id: {
        "label": label,
        "category": category,
        "category_label": SOURCE_CATEGORY_LABELS[category],
        "color": SOURCE_CATEGORY_COLORS[category],
        "interval_seconds": interval_seconds,
    }
    for source_id, label, category, interval_seconds in _SOURCE_DEFINITIONS
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_ITEMS = 300
DEFAULT_MAX_IMPORTANT_ITEMS = 50
MIN_MAX_ITEMS = 1
MAX_MAX_ITEMS = 3000
MIN_MAX_IMPORTANT_ITEMS = 1
MAX_MAX_IMPORTANT_ITEMS = 1000
CN_TZ = timezone(timedelta(hours=8))
TRUTHY_VALUES = {"1", "true", "yes", "on"}
IMPORTANT_INFO_MARKERS = {"1", "important", "on", "true", "yes", "✰", "★", "⭐", "重要"}
_TAG_RE = re.compile(r"<[^>]{0,400}>")


class NewsNowError(RuntimeError):
    """Base error carrying a stable, non-sensitive diagnostic code."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class NewsNowConfigurationError(NewsNowError):
    """Raised when the NewsNow runtime configuration is invalid."""


class NewsNowRequestError(NewsNowError):
    """Raised when bounded network attempts are exhausted."""


class NewsNowResponseError(NewsNowError):
    """Raised when NewsNow returns an unexpected response."""


def normalize_endpoint(value: str) -> str:
    """Return a credential-free HTTP(S) NewsNow ``/api/s`` endpoint."""

    normalized = str(value or DEFAULT_ENDPOINT).strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("NEWSNOW_BASE_URL 必须是有效的 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("NEWSNOW_BASE_URL 不能包含凭据、查询参数或片段")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/s"):
        path = f"{path}/api/s" if path else "/api/s"
    return urlunparse((parsed.scheme.lower(), parsed.netloc, path, "", "", ""))


def parse_source_ids(value: str | None) -> tuple[str, ...]:
    """Normalize a comma-separated source list against the supported allowlist."""

    raw_values = re.split(r"[,，\s]+", str(value or ""))
    result: list[str] = []
    invalid: list[str] = []
    for raw in raw_values:
        source_id = raw.strip().lower()
        if not source_id:
            continue
        if source_id not in SUPPORTED_SOURCES:
            invalid.append(source_id)
            continue
        if source_id not in result:
            result.append(source_id)
    if invalid:
        raise ValueError(f"不支持的 NewsNow 数据源: {', '.join(invalid)}")
    if not result:
        raise ValueError("NEWSNOW_SOURCES 至少需要一个数据源")
    return tuple(result)


def source_options() -> list[dict[str, Any]]:
    """Return stable settings metadata for finance sources shown in admin."""

    category_order = {name: index for index, name in enumerate(SOURCE_CATEGORY_LABELS)}
    options = [
        {
            "id": source_id,
            "label": str(metadata["label"]),
            "category": str(metadata["category"]),
            "category_label": str(metadata["category_label"]),
            "color": str(metadata["color"]),
            "interval_seconds": int(metadata["interval_seconds"]),
        }
        for source_id, metadata in SUPPORTED_SOURCES.items()
        if metadata["category"] == SETTINGS_SOURCE_CATEGORY
    ]
    return sorted(
        options,
        key=lambda option: (
            category_order.get(str(option["category"]), 999),
            str(option["label"]).casefold(),
            str(option["id"]),
        ),
    )


def _bounded_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(env.get(name, default) or default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise NewsNowConfigurationError(
            "invalid_configuration",
            f"{name} 必须是整数",
        ) from exc
    if value < minimum or value > maximum:
        raise NewsNowConfigurationError(
            "invalid_configuration",
            f"{name} 必须在 {minimum} 到 {maximum} 之间",
        )
    return value


@dataclass(frozen=True)
class NewsNowConfig:
    """Explicit runtime settings for NewsNow access."""

    enabled: bool
    endpoint: str
    source_ids: tuple[str, ...]
    timeout_seconds: int = 10
    max_retries: int = 1
    max_concurrency: int = 3
    refresh_seconds: int = 60
    max_items: int = DEFAULT_MAX_ITEMS
    max_important_items: int = DEFAULT_MAX_IMPORTANT_ITEMS

    def __post_init__(self) -> None:
        for name, value, minimum, maximum in (
            ("NEWSNOW_MAX_ITEMS", self.max_items, MIN_MAX_ITEMS, MAX_MAX_ITEMS),
            (
                "NEWSNOW_MAX_IMPORTANT_ITEMS",
                self.max_important_items,
                MIN_MAX_IMPORTANT_ITEMS,
                MAX_MAX_IMPORTANT_ITEMS,
            ),
        ):
            if value < minimum or value > maximum:
                raise NewsNowConfigurationError(
                    "invalid_configuration",
                    f"{name} 必须在 {minimum} 到 {maximum} 之间",
                )
        if self.max_important_items > self.max_items:
            raise NewsNowConfigurationError(
                "invalid_configuration",
                "NEWSNOW_MAX_IMPORTANT_ITEMS 不能大于 NEWSNOW_MAX_ITEMS",
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "NewsNowConfig":
        values = os.environ if env is None else env
        enabled = str(values.get("NEWSNOW_ENABLED", "1") or "1").strip().lower() in TRUTHY_VALUES
        try:
            endpoint = normalize_endpoint(str(values.get("NEWSNOW_BASE_URL") or DEFAULT_ENDPOINT))
            source_ids = parse_source_ids(
                str(values.get("NEWSNOW_SOURCES") or ",".join(DEFAULT_SOURCE_IDS))
            )
        except ValueError as exc:
            raise NewsNowConfigurationError("invalid_configuration", str(exc)) from exc
        max_items = _bounded_int(
            values,
            "NEWSNOW_MAX_ITEMS",
            DEFAULT_MAX_ITEMS,
            MIN_MAX_ITEMS,
            MAX_MAX_ITEMS,
        )
        max_important_items = _bounded_int(
            values,
            "NEWSNOW_MAX_IMPORTANT_ITEMS",
            DEFAULT_MAX_IMPORTANT_ITEMS,
            MIN_MAX_IMPORTANT_ITEMS,
            MAX_MAX_IMPORTANT_ITEMS,
        )
        if max_important_items > max_items:
            raise NewsNowConfigurationError(
                "invalid_configuration",
                "NEWSNOW_MAX_IMPORTANT_ITEMS 不能大于 NEWSNOW_MAX_ITEMS",
            )
        return cls(
            enabled=enabled,
            endpoint=endpoint,
            source_ids=source_ids,
            timeout_seconds=_bounded_int(values, "NEWSNOW_TIMEOUT_SECONDS", 10, 2, 30),
            max_retries=_bounded_int(values, "NEWSNOW_MAX_RETRIES", 1, 0, 2),
            max_concurrency=_bounded_int(values, "NEWSNOW_MAX_CONCURRENCY", 3, 1, 3),
            refresh_seconds=_bounded_int(values, "NEWSNOW_REFRESH_SECONDS", 60, 15, 1800),
            max_items=max_items,
            max_important_items=max_important_items,
        )

    @property
    def fingerprint(self) -> str:
        raw = "\n".join((
            self.endpoint,
            *self.source_ids,
            f"max_items={self.max_items}",
            f"max_important_items={self.max_important_items}",
        )).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}
_SEMAPHORE_LOCK = threading.Lock()


def _shared_semaphore(limit: int) -> threading.BoundedSemaphore:
    with _SEMAPHORE_LOCK:
        return _SEMAPHORES.setdefault(limit, threading.BoundedSemaphore(limit))


def _clean_text(value: Any, *, maximum: int) -> str:
    text = html.unescape(_TAG_RE.sub("", str(value or "")))
    return " ".join(text.split())[:maximum]


def _safe_url(value: Any) -> str:
    text = html.unescape(str(value or "").strip())
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return text[:2048]


def _timestamp_ms(value: Any) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        try:
            number = int(parsed.timestamp() * 1000)
        except (OSError, OverflowError, ValueError):
            return None
    if number <= 0:
        return None
    if number < 10_000_000_000:
        number *= 1000
    return number if number < 100_000_000_000_000 else None


def _is_important_info(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    marker = _clean_text(value, maximum=40).casefold()
    return marker in IMPORTANT_INFO_MARKERS


def _iso_from_ms(value: int | None) -> str:
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone(CN_TZ).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _normalized_item(source_id: str, raw: Mapping[str, Any], rank: int) -> dict[str, Any] | None:
    title = _clean_text(raw.get("title"), maximum=1000)
    if not title:
        return None
    external_id = _clean_text(raw.get("id"), maximum=200)
    extra = raw.get("extra") if isinstance(raw.get("extra"), Mapping) else {}
    published_ms = _timestamp_ms(raw.get("pubDate") or extra.get("date"))
    url = _safe_url(raw.get("url") or raw.get("mobileUrl"))
    summary = _clean_text(extra.get("hover"), maximum=3000)
    important = _is_important_info(extra.get("info"))
    if not external_id:
        digest_input = "\n".join((source_id, title, str(published_ms or ""), url)).encode("utf-8")
        external_id = "derived-" + hashlib.sha256(digest_input).hexdigest()[:24]
    return {
        "id": f"{source_id}:{external_id}",
        "external_id": external_id,
        "source_id": source_id,
        "source_name": str(SUPPORTED_SOURCES[source_id]["label"]),
        "title": title,
        "summary": summary,
        "url": url,
        "published_at": _iso_from_ms(published_ms),
        "published_at_ms": published_ms,
        "important": important,
        "rank": rank,
    }


def _item_sort_key(item: Mapping[str, Any]) -> tuple[int, int]:
    return (
        int(item.get("published_at_ms") or item.get("collected_at_ms") or 0),
        -int(item.get("rank") or 0),
    )


def _retain_items(
    items: list[dict[str, Any]],
    *,
    max_items: int,
    max_important_items: int,
) -> list[dict[str, Any]]:
    """Keep a bounded rolling history while reserving room for important items."""

    ordered = sorted(items, key=_item_sort_key, reverse=True)
    important = [item for item in ordered if item.get("important") is True][
        :max_important_items
    ]
    ordinary_limit = max(0, max_items - len(important))
    ordinary = [item for item in ordered if item.get("important") is not True][
        :ordinary_limit
    ]
    return sorted([*important, *ordinary], key=_item_sort_key, reverse=True)


class NewsNowClient:
    """Fetch and validate one NewsNow source with bounded I/O and retries."""

    def __init__(
        self,
        config: NewsNowConfig,
        *,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        semaphore: threading.BoundedSemaphore | None = None,
    ):
        self.config = config
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._semaphore = semaphore or _shared_semaphore(config.max_concurrency)

    def fetch(self, source_id: str) -> dict[str, Any]:
        if source_id not in self.config.source_ids or source_id not in SUPPORTED_SOURCES:
            raise ValueError(f"unsupported NewsNow source: {source_id}")
        query = urlencode({"id": source_id, "latest": "true"})
        request = urllib.request.Request(
            f"{self.config.endpoint}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": NEWSNOW_USER_AGENT,
            },
            method="GET",
        )
        last_error: NewsNowRequestError | None = None
        for attempt in range(self.config.max_retries + 1):
            acquired = self._semaphore.acquire(timeout=self.config.timeout_seconds)
            if not acquired:
                raise NewsNowRequestError("concurrency_timeout", "NewsNow 请求并发等待超时")
            try:
                try:
                    with self._opener(request, timeout=self.config.timeout_seconds) as response:
                        body = response.read(MAX_RESPONSE_BYTES + 1)
                        headers = getattr(response, "headers", None)
                        content_type = str(headers.get("Content-Type", "") if headers else "")
                except urllib.error.HTTPError as exc:
                    last_error = NewsNowRequestError(
                        f"http_{exc.code}",
                        f"NewsNow 返回 HTTP {exc.code}",
                        status_code=exc.code,
                    )
                    retryable = exc.code == 429 or 500 <= exc.code < 600
                    if not retryable or attempt >= self.config.max_retries:
                        raise last_error from exc
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last_error = NewsNowRequestError(
                        "network_error",
                        f"NewsNow 网络请求失败: {type(exc).__name__}",
                    )
                    if attempt >= self.config.max_retries:
                        raise last_error from exc
                else:
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise NewsNowResponseError("response_too_large", "NewsNow 响应超过大小上限")
                    if content_type and "json" not in content_type.lower():
                        raise NewsNowResponseError("invalid_content_type", "NewsNow 未返回 JSON")
                    return self._parse_response(
                        source_id,
                        body,
                        item_limit=self.config.max_items,
                    )
            finally:
                self._semaphore.release()
            if attempt < self.config.max_retries:
                self._sleep(min(0.25 * (2**attempt), 1.0))
        if last_error is not None:  # pragma: no cover - defensive guard
            raise last_error
        raise NewsNowRequestError("request_failed", "NewsNow 请求失败")

    @staticmethod
    def _parse_response(
        source_id: str,
        body: bytes,
        *,
        item_limit: int = DEFAULT_MAX_ITEMS,
    ) -> dict[str, Any]:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NewsNowResponseError("invalid_json", "NewsNow 响应不是有效 JSON") from exc
        if not isinstance(parsed, dict):
            raise NewsNowResponseError("invalid_response", "NewsNow 响应必须是 JSON 对象")
        status = str(parsed.get("status") or "").strip().lower()
        items = parsed.get("items")
        if status not in {"success", "cache"} or not isinstance(items, list):
            raise NewsNowResponseError("invalid_response", "NewsNow 响应缺少有效状态或新闻列表")
        normalized_items = []
        for rank, raw in enumerate(items[:item_limit], start=1):
            if not isinstance(raw, Mapping):
                continue
            item = _normalized_item(source_id, raw, rank)
            if item is not None:
                normalized_items.append(item)
        return {
            "id": source_id,
            "label": str(SUPPORTED_SOURCES[source_id]["label"]),
            "status": status,
            "updated_at_ms": _timestamp_ms(parsed.get("updatedTime")),
            "items": normalized_items,
        }


class NewsNowService:
    """Aggregate sources and preserve the last valid source on failures."""

    def __init__(
        self,
        cache_path: Path,
        *,
        client_factory: Callable[[NewsNowConfig], NewsNowClient] = NewsNowClient,
        now: Callable[[], float] = time.time,
    ):
        self.cache_path = Path(cache_path)
        self._client_factory = client_factory
        self._now = now
        self._lock = threading.Lock()
        self._last_attempt_ms = 0
        self._last_fingerprint = ""
        self._last_result: dict[str, Any] | None = None

    def get_news(self, config: NewsNowConfig) -> dict[str, Any]:
        now_ms = int(self._now() * 1000)
        if not config.enabled:
            return self._disabled_payload(config, now_ms)
        with self._lock:
            if (
                self._last_result is not None
                and self._last_fingerprint == config.fingerprint
                and now_ms - self._last_attempt_ms < config.refresh_seconds * 1000
            ):
                return self._cached_copy(self._last_result)

            persisted = read_json_cache(self.cache_path)
            if (
                self._last_result is None
                and self._cache_matches(persisted, config)
                and now_ms - int((persisted or {}).get("attempted_at_ms") or 0)
                < config.refresh_seconds * 1000
            ):
                self._last_result = dict(persisted or {})
                self._last_attempt_ms = int(self._last_result.get("attempted_at_ms") or now_ms)
                self._last_fingerprint = config.fingerprint
                return self._cached_copy(self._last_result)

            result = self._refresh(config, persisted, now_ms)
            self._last_attempt_ms = now_ms
            self._last_fingerprint = config.fingerprint
            self._last_result = result
            if result.get("items") and result.get("successful_source_count"):
                write_json_cache(self.cache_path, result)
            return dict(result)

    @staticmethod
    def _cache_matches(payload: dict[str, Any] | None, config: NewsNowConfig) -> bool:
        if not isinstance(payload, dict):
            return False
        return (
            payload.get("schema_version") == 1
            and str(payload.get("config_fingerprint") or "") == config.fingerprint
            and isinstance(payload.get("items"), list)
        )

    @staticmethod
    def _cached_copy(payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        result["served_from_local_cache"] = True
        return result

    @staticmethod
    def _disabled_payload(config: NewsNowConfig, now_ms: int) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "enabled": False,
            "available": False,
            "status": "disabled",
            "stale": False,
            "source": "NewsNow",
            "generated_at": _iso_from_ms(now_ms),
            "attempted_at_ms": now_ms,
            "successful_source_count": 0,
            "source_ids": list(config.source_ids),
            "max_items": config.max_items,
            "max_important_items": config.max_important_items,
            "sources": [],
            "items": [],
            "error": "realtime_news_disabled",
        }

    def _refresh(
        self,
        config: NewsNowConfig,
        persisted: dict[str, Any] | None,
        now_ms: int,
    ) -> dict[str, Any]:
        cached_sources = {
            str(source.get("id") or ""): source
            for source in ((persisted or {}).get("sources") or [])
            if isinstance(source, dict)
        }
        cached_items: dict[str, list[dict[str, Any]]] = {}
        for item in ((persisted or {}).get("items") or []):
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_id") or "")
            cached_items.setdefault(source_id, []).append(item)

        client = self._client_factory(config)
        successes: dict[str, dict[str, Any]] = {}
        errors: dict[str, NewsNowError] = {}
        workers = min(config.max_concurrency, len(config.source_ids))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="newsnow") as executor:
            futures = {executor.submit(client.fetch, source_id): source_id for source_id in config.source_ids}
            for future in as_completed(futures):
                source_id = futures[future]
                try:
                    successes[source_id] = future.result()
                except NewsNowError as exc:
                    errors[source_id] = exc
                except Exception as exc:  # defensive conversion; never expose details
                    errors[source_id] = NewsNowRequestError(
                        "unexpected_error",
                        f"NewsNow 请求异常: {type(exc).__name__}",
                    )

        source_rows: list[dict[str, Any]] = []
        all_items: list[dict[str, Any]] = []
        stale = False
        for source_id in config.source_ids:
            meta = SUPPORTED_SOURCES[source_id]
            success = successes.get(source_id)
            if success is not None:
                items = []
                for item in success["items"]:
                    collected = dict(item)
                    collected["collected_at_ms"] = now_ms
                    items.append(collected)
                source_rows.append({
                    "id": source_id,
                    "label": meta["label"],
                    "available": True,
                    "status": success["status"],
                    "stale": False,
                    "count": len(items),
                    "updated_at": _iso_from_ms(success.get("updated_at_ms")),
                    "updated_at_ms": success.get("updated_at_ms"),
                    "interval_seconds": meta["interval_seconds"],
                    "error": "",
                })
                all_items.extend(items)
                all_items.extend(cached_items.get(source_id) or [])
                continue

            stale = True
            cached = list(cached_items.get(source_id) or [])
            error = errors.get(source_id)
            cached_meta = cached_sources.get(source_id) or {}
            source_rows.append({
                "id": source_id,
                "label": meta["label"],
                "available": bool(cached),
                "status": "cache" if cached else "unavailable",
                "stale": bool(cached),
                "count": len(cached),
                "updated_at": str(cached_meta.get("updated_at") or ""),
                "updated_at_ms": cached_meta.get("updated_at_ms"),
                "interval_seconds": meta["interval_seconds"],
                "error": error.code if error is not None else "request_failed",
            })
            all_items.extend(cached)

        deduplicated: dict[str, dict[str, Any]] = {}
        for item in all_items:
            item_id = str(item.get("id") or "")
            if item_id and item_id not in deduplicated:
                deduplicated[item_id] = item
        items = _retain_items(
            list(deduplicated.values()),
            max_items=config.max_items,
            max_important_items=config.max_important_items,
        )
        retained_counts: dict[str, int] = {}
        for item in items:
            source_id = str(item.get("source_id") or "")
            retained_counts[source_id] = retained_counts.get(source_id, 0) + 1
        for source in source_rows:
            source["count"] = retained_counts.get(str(source.get("id") or ""), 0)
        successful_count = len(successes)
        if successful_count == len(config.source_ids):
            status = "success"
        elif successful_count:
            status = "partial"
        elif items:
            status = "cache"
        else:
            status = "unavailable"
        return {
            "schema_version": 1,
            "config_fingerprint": config.fingerprint,
            "enabled": True,
            "available": bool(items),
            "status": status,
            "stale": stale,
            "served_from_local_cache": False,
            "source": "NewsNow",
            "generated_at": _iso_from_ms(now_ms),
            "attempted_at_ms": now_ms,
            "successful_source_count": successful_count,
            "source_ids": list(config.source_ids),
            "max_items": config.max_items,
            "max_important_items": config.max_important_items,
            "sources": source_rows,
            "items": items,
            "error": "" if successful_count else next(iter(errors.values())).code if errors else "request_failed",
        }


_SHARED_SERVICE_LOCK = threading.Lock()
_SHARED_SERVICES: dict[str, NewsNowService] = {}


def shared_newsnow_service(cache_path: Path) -> NewsNowService:
    """Return one process-local service per cache path.

    Dashboard rendering and trading decisions share the same bounded refresh
    state so they do not race separate refreshes or duplicate upstream calls.
    """

    path = Path(cache_path).expanduser()
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path.absolute())
    with _SHARED_SERVICE_LOCK:
        service = _SHARED_SERVICES.get(key)
        if service is None:
            service = NewsNowService(path)
            _SHARED_SERVICES[key] = service
        return service
