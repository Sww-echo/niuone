"""Bounded, structured candidate-news precheck for strategy research."""
from __future__ import annotations

import concurrent.futures
import difflib
import json
import os
import re
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is present in supported installs
    yaml = None

if __package__ and __package__.startswith("app."):
    from ..core.model_api import build_model_request, request_model_complete
    from ..core.shared_model_config import resolve_shared_model_config
    from .iwencai_client import IwencaiClient, IwencaiConfig, IwencaiConfigurationError
else:
    from core.model_api import build_model_request, request_model_complete
    from core.shared_model_config import resolve_shared_model_config
    from market_data.iwencai_client import IwencaiClient, IwencaiConfig, IwencaiConfigurationError


CN_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_MAX_CANDIDATES = 5
IWENCAI_NEWS_SOURCE_VERSION = "iwencai-skills-decision-model-v2"
IWENCAI_NEWS_SKILLS = (
    "announcement-search",
    "news-search",
    "hithink-event-query",
)
TONE_VALUES = {"利好": "positive", "利空": "negative", "中性": "neutral"}
IWENCAI_EVENT_FIELD_MARKERS = (
    "业绩预告", "增发", "配股", "质押", "解禁", "调研", "监管", "问询函",
)
IWENCAI_NON_EVENT_FIELD_MARKERS = (
    "股票代码", "证券代码", "股票简称", "证券简称", "最新价", "涨跌幅", "报告期",
)
IWENCAI_MARKET_DATA_ONLY_MARKERS = (
    "主力资金", "资金净流入", "资金净流出", "资金净买入", "资金净卖出",
    "融资余额", "融资净买入", "融资净卖出", "融券余额", "大宗交易", "龙虎榜",
    "最新价", "收盘价", "涨跌幅",
)
IWENCAI_COMPANY_EVENT_MARKERS = (
    "公告", "澄清", "风险提示", "合作", "合同", "中标", "订单", "业绩", "预告",
    "回购", "增持", "减持", "立案", "调查", "处罚", "诉讼", "冻结", "违约",
    "投产", "获批", "终止", "监管", "问询", "关注函", "解禁", "质押", "调研",
)
@dataclass(frozen=True)
class NewsPrecheckConfig:
    base_url: str
    api_key: str
    model: str = "deepseek-v4-pro"
    decision_base_url: str = ""
    decision_api_key: str = ""
    decision_stream_mode: str = "auto"
    decision_reasoning_effort: str = ""
    decision_timeout_seconds: int = 180
    decision_max_tokens: int = 1200
    source_mode: str = "iwencai"
    timeout_seconds: int = 45
    max_requests: int = 1
    concurrency: int = 5

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "NewsPrecheckConfig | None":
        enabled = str(
            values.get("IWENCAI_NEWS_PRECHECK_ENABLED") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return None
        try:
            iwencai = IwencaiConfig.from_env(values)
        except (ValueError, IwencaiConfigurationError) as exc:
            error_code = getattr(exc, "code", type(exc).__name__)
            raise ValueError(f"iwencai_news_precheck_invalid:{error_code}") from exc
        if not iwencai.enabled:
            raise ValueError("iwencai_news_precheck_source_disabled")
        if not iwencai.api_key:
            raise ValueError("iwencai_news_precheck_not_configured:IWENCAI_API_KEY")
        provider: dict[str, str] = {}
        explicit_decision_base_url = str(
            values.get("DASHBOARD_DECISION_BASE_URL")
            or values.get("CROSSDESK_BASE_URL")
            or ""
        ).strip()
        explicit_decision_api_key = str(
            values.get("DASHBOARD_DECISION_API_KEY")
            or values.get("CROSSDESK_API_KEY")
            or ""
        ).strip()
        if explicit_decision_base_url and explicit_decision_api_key:
            provider = {
                "base_url": explicit_decision_base_url,
                "api_key": explicit_decision_api_key,
            }
        config_path = str(values.get("DASHBOARD_CONFIG") or os.environ.get("DASHBOARD_CONFIG") or "").strip()
        if not (explicit_decision_base_url and explicit_decision_api_key) and config_path and yaml is not None:
            try:
                loaded = yaml.safe_load(
                    Path(config_path).expanduser().read_text(encoding="utf-8")
                ) or {}
            except (OSError, ValueError, TypeError):
                loaded = {}
            for raw_provider in loaded.get("custom_providers", []) if isinstance(loaded, Mapping) else []:
                if not isinstance(raw_provider, Mapping):
                    continue
                if "crossdesk" in str(
                    raw_provider.get("name") or raw_provider.get("base_url") or ""
                ).lower():
                    provider = {
                        "base_url": str(raw_provider.get("base_url") or "").strip(),
                        "api_key": str(raw_provider.get("api_key") or "").strip(),
                    }
                    break
        shared_model = resolve_shared_model_config(values, provider_fallback=provider)
        model = shared_model.model
        decision_base_url = shared_model.base_url
        decision_api_key = shared_model.api_key
        missing = [
            name
            for name, value in (
                ("DASHBOARD_DECISION_MODEL", model),
                ("DASHBOARD_DECISION_BASE_URL", decision_base_url),
                ("DASHBOARD_DECISION_API_KEY", decision_api_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "iwencai_news_precheck_decision_model_not_configured:"
                + ",".join(missing)
            )

        def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(str(values.get(name) or default).strip())
            except ValueError as exc:
                raise ValueError(f"iwencai_news_precheck_invalid:{name}") from exc
            if not minimum <= value <= maximum:
                raise ValueError(f"iwencai_news_precheck_invalid:{name}")
            return value

        def bounded_token_count(raw: str, default: int, minimum: int, maximum: int) -> int:
            compact = str(raw or "").replace(",", "").replace("_", "").strip()
            match = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", compact)
            if not match:
                return default
            multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
            return max(minimum, min(maximum, int(float(match.group(1)) * multiplier)))

        return cls(
            base_url=iwencai.base_url,
            api_key=iwencai.api_key,
            model=model,
            decision_base_url=decision_base_url,
            decision_api_key=decision_api_key,
            decision_stream_mode=shared_model.stream_mode,
            decision_reasoning_effort=shared_model.reasoning_effort,
            decision_timeout_seconds=bounded_int(
                "DASHBOARD_DECISION_TIMEOUT", 180, 10, 1800
            ),
            decision_max_tokens=min(
                bounded_token_count(shared_model.max_tokens, 4096, 256, 131072),
                1200,
            ),
            timeout_seconds=iwencai.timeout_seconds,
            max_requests=iwencai.max_retries + 1,
            concurrency=iwencai.max_concurrency,
        )

    @property
    def provider_label(self) -> str:
        return f"同花顺问财 + {self.model}"


def candidate_label(candidate: Mapping[str, Any]) -> str:
    code = str(candidate.get("code") or "").strip()
    name = str(candidate.get("name") or "").strip()
    return " ".join(part for part in (code, name) if part) or "未知股票"


def build_iwencai_candidate_news_query(
    candidate: Mapping[str, Any],
    source: str = "news-search",
) -> str:
    """Build a concise query for one official iWencai message skill."""

    label = candidate_label(candidate)
    if source == "announcement-search":
        return f"{label} 最近3日公司公告"
    if source == "hithink-event-query":
        return f"{label} 最近3日业绩预告或增发或质押或解禁或机构调研或监管函"
    return f"{label} 最近3日公司新闻和重大事项"


def _iwencai_value_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "；".join(
            text for item in value if (text := _iwencai_value_text(item))
        )
    if isinstance(value, Mapping):
        return "；".join(
            text for item in value.values() if (text := _iwencai_value_text(item))
        )
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text.lower() in {"", "none", "null", "nan", "-", "--"}:
        return ""
    return text


def _candidate_identity_matches(candidate: Mapping[str, Any], text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    code = re.sub(r"\D", "", str(candidate.get("code") or ""))
    name = re.sub(r"\s+", "", str(candidate.get("name") or "")).lower()
    return bool((code and code in normalized) or (name and name in normalized))


def _iwencai_publish_datetime(row: Mapping[str, Any]) -> datetime | None:
    for key in ("publish_time", "publish_date", "发布时间", "公告日期", "事件日期"):
        value = row.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            digits = str(value).strip()
            if len(digits) in {10, 13}:
                seconds = float(digits) / (1000 if len(digits) == 13 else 1)
                try:
                    return datetime.fromtimestamp(seconds, CN_TZ)
                except (OverflowError, OSError, ValueError):
                    continue
            if len(digits) == 8:
                try:
                    return datetime.strptime(digits, "%Y%m%d").replace(tzinfo=CN_TZ)
                except ValueError:
                    continue
        text = str(value).strip().replace("/", "-")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        return parsed.replace(tzinfo=CN_TZ) if parsed.tzinfo is None else parsed.astimezone(CN_TZ)
    return None


def _within_news_window(published_at: datetime | None, fetched_at: datetime) -> bool:
    if published_at is None:
        return False
    earliest = fetched_at.date() - timedelta(days=2)
    return earliest <= published_at.astimezone(CN_TZ).date() <= fetched_at.date()


def _dates_in_text(value: Any) -> list[datetime]:
    text = str(value or "")
    parsed: list[datetime] = []
    seen: set[str] = set()
    patterns = (
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",
        r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?(?!\d)",
    )
    for pattern in patterns:
        for year, month, day in re.findall(pattern, text):
            key = f"{year}-{int(month):02d}-{int(day):02d}"
            if key in seen:
                continue
            try:
                parsed.append(datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=CN_TZ))
            except ValueError:
                continue
            seen.add(key)
    return parsed


def _search_payload_evidence(
    candidate: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    source: str,
    fetched_at: datetime,
) -> list[dict[str, str]]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError(f"{source}_response_missing_data")
    evidence: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        title = _clean_candidate_news_text(row.get("title"))
        summary = _clean_candidate_news_text(row.get("summary") or row.get("source_original"))
        combined = "；".join(part for part in (title, summary) if part)
        published_at = _iwencai_publish_datetime(row)
        if (
            not combined
            or not _candidate_identity_matches(candidate, combined)
            or not _within_news_window(published_at, fetched_at)
            or (
                any(marker in combined for marker in IWENCAI_MARKET_DATA_ONLY_MARKERS)
                and not any(marker in combined for marker in IWENCAI_COMPANY_EVENT_MARKERS)
            )
        ):
            continue
        evidence.append({
            "source": source,
            "title": title or summary[:120],
            "summary": summary[:360],
            "published_at": published_at.isoformat(timespec="seconds") if published_at else "",
            "url": str(row.get("url") or "").strip()[:600],
        })
    return evidence


def _event_payload_evidence(
    candidate: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    fetched_at: datetime,
) -> list[dict[str, str]]:
    rows = payload.get("datas")
    if not isinstance(rows, list):
        raise ValueError("hithink-event-query_response_missing_datas")
    evidence: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        identity_text = " ".join(str(value or "") for value in row.values())
        if not _candidate_identity_matches(candidate, identity_text):
            continue
        parts: list[str] = []
        published_dates: list[datetime] = []
        for key, value in row.items():
            field = str(key or "")
            if any(marker in field for marker in IWENCAI_NON_EVENT_FIELD_MARKERS):
                continue
            if not any(marker in field for marker in IWENCAI_EVENT_FIELD_MARKERS):
                continue
            text = _clean_candidate_news_text(_iwencai_value_text(value))
            dates = _dates_in_text(f"{field} {text}")
            recent_dates = [date for date in dates if _within_news_window(date, fetched_at)]
            if text and recent_dates:
                parts.append(f"{field}：{text}")
                published_dates.extend(recent_dates)
        if not parts:
            continue
        summary = "；".join(parts[:5])
        published_at = max(published_dates)
        evidence.append({
            "source": "hithink-event-query",
            "title": parts[0][:180],
            "summary": summary[:360],
            "published_at": published_at.isoformat(timespec="seconds"),
            "url": "",
        })
    return evidence


def _evidence_title_key(item: Mapping[str, Any]) -> str:
    title = str(item.get("title") or "").lower()
    title = re.sub(r"[\[【][^\]】]{1,12}[\]】]", "", title)
    title = re.sub(r"[（(]?\d{6}(?:\.[a-z]{2})?[）)]?", "", title)
    title = title.replace("关于", "").replace("的公告", "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title)


def _deduplicate_evidence(items: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    keys: list[str] = []
    urls: set[str] = set()
    for item in items:
        url = str(item.get("url") or "").strip()
        key = _evidence_title_key(item)
        duplicate = bool(url and url in urls)
        if not duplicate and key:
            duplicate = any(
                key == existing
                or (min(len(key), len(existing)) >= 12 and (key in existing or existing in key))
                or difflib.SequenceMatcher(a=key, b=existing).ratio() >= 0.78
                for existing in keys
            )
        if duplicate:
            continue
        unique.append(item)
        keys.append(key)
        if url:
            urls.add(url)
    return unique


def parse_iwencai_candidate_news_record(
    candidate: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
    *,
    fetched_at: str,
    source_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    source_errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    parsed_fetched_at = datetime.fromisoformat(fetched_at)
    if parsed_fetched_at.tzinfo is None:
        parsed_fetched_at = parsed_fetched_at.replace(tzinfo=CN_TZ)
    active_payloads = dict(source_payloads or {})
    if payload is not None and not active_payloads:
        active_payloads["hithink-event-query"] = payload
    errors = {str(key): str(value) for key, value in (source_errors or {}).items()}
    evidence: list[dict[str, str]] = []
    source_results: list[dict[str, Any]] = []
    for source in IWENCAI_NEWS_SKILLS:
        source_payload = active_payloads.get(source)
        error = errors.get(source, "")
        source_evidence: list[dict[str, str]] = []
        if source_payload is not None and not error:
            try:
                source_evidence = (
                    _event_payload_evidence(
                        candidate,
                        source_payload,
                        fetched_at=parsed_fetched_at,
                    )
                    if source == "hithink-event-query"
                    else _search_payload_evidence(
                        candidate,
                        source_payload,
                        source=source,
                        fetched_at=parsed_fetched_at,
                    )
                )
            except ValueError as exc:
                error = str(exc)
        evidence.extend(source_evidence)
        source_results.append({
            "skill": source,
            "ok": bool(source_payload is not None and not error),
            "evidence_count": len(source_evidence),
            "error": error,
        })
    evidence = _deduplicate_evidence(evidence)
    completed_sources = sum(1 for item in source_results if item["ok"])
    partial = completed_sources < len(IWENCAI_NEWS_SKILLS)
    available = bool(evidence) or (completed_sources == len(IWENCAI_NEWS_SKILLS))
    if not available:
        return {
            "code": str(candidate.get("code") or ""),
            "name": str(candidate.get("name") or ""),
            "checked": False,
            "available": False,
            "tone": "neutral",
            "tone_label": "不可用",
            "summary": "",
            "provider": "同花顺问财",
            "source_mode": "iwencai",
            "source_version": IWENCAI_NEWS_SOURCE_VERSION,
            "source_scope": list(IWENCAI_NEWS_SKILLS),
            "source_results": source_results,
            "partial": partial,
            "evidence_count": 0,
            "window_days": 3,
            "fetched_at": fetched_at,
            "error": "iwencai_news_precheck_partial_no_evidence",
        }
    if not evidence:
        return {
            "code": str(candidate.get("code") or ""),
            "name": str(candidate.get("name") or ""),
            "checked": True,
            "available": True,
            "tone": "neutral",
            "tone_label": "中性",
            "summary": (
                "事件：最近3日未检索到公司公告、财经新闻或结构化事件；"
                "影响：无有效消息可供模型判断；"
                "舆情：纯问财模式不包含雪球/X舆情（中性）"
            ),
            "provider": "同花顺问财",
            "source_mode": "iwencai",
            "source_version": IWENCAI_NEWS_SOURCE_VERSION,
            "source_scope": list(IWENCAI_NEWS_SKILLS),
            "source_results": source_results,
            "partial": False,
            "evidence": [],
            "evidence_count": 0,
            "judgment_provider": "no_material_evidence",
            "window_days": 3,
            "fetched_at": fetched_at,
            "error": "",
        }
    return {
        "code": str(candidate.get("code") or ""),
        "name": str(candidate.get("name") or ""),
        "checked": False,
        "available": False,
        "tone": "neutral",
        "tone_label": "待判断",
        "summary": "",
        "provider": "同花顺问财",
        "source_mode": "iwencai",
        "source_version": IWENCAI_NEWS_SOURCE_VERSION,
        "source_scope": list(IWENCAI_NEWS_SKILLS),
        "source_results": source_results,
        "partial": partial,
        "evidence": evidence[:5],
        "evidence_count": len(evidence),
        "judgment_provider": "decision_model_pending",
        "window_days": 3,
        "fetched_at": fetched_at,
        "error": "",
    }


def _extract_json_object(content: Any) -> dict[str, Any]:
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("decision_model_invalid_json")


def _model_judgment_messages(record: Mapping[str, Any]) -> list[dict[str, str]]:
    evidence = [
        {
            "source": str(item.get("source") or ""),
            "published_at": str(item.get("published_at") or ""),
            "title": str(item.get("title") or "")[:180],
            "summary": str(item.get("summary") or "")[:360],
        }
        for item in (record.get("evidence") or [])[:8]
        if isinstance(item, Mapping)
    ]
    prompt_payload = {
        "stock": {
            "code": str(record.get("code") or ""),
            "name": str(record.get("name") or ""),
        },
        "window_days": int(record.get("window_days") or 3),
        "evidence": evidence,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是牛牛1号的A股消息面判断器。只能依据用户提供的问财公告、新闻和结构化事件证据，"
                "不得使用外部知识、行情涨跌、资金流或猜测。综合判断这些消息对该股票短期交易的方向为"
                "利好、利空或中性。澄清公告必须按其实际否认、风险与业务落地情况判断，不能仅按标题判断。"
                "只返回一个JSON对象，不要Markdown："
                '{"tone_label":"利好|利空|中性","event":"核心事件，不超过120字",'
                '"impact":"对公司基本面或短期交易的直接影响，不超过160字",'
                '"reason":"判断依据，不超过120字"}'
            ),
        },
        {
            "role": "user",
            "content": json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def judge_iwencai_news_with_decision_model(
    record: Mapping[str, Any],
    config: NewsPrecheckConfig,
    *,
    requester: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Classify filtered iWencai evidence using the configured decision model."""

    result = dict(record)
    if not result.get("evidence"):
        return result
    active_requester = requester or request_model_complete
    try:
        model_request = build_model_request(
            config.decision_base_url,
            config.model,
            _model_judgment_messages(result),
            max_tokens=config.decision_max_tokens,
            api_mode="auto",
            reasoning_effort=config.decision_reasoning_effort,
            stream=False,
            extra_payload={"stream": False},
        )
        parsed_response = active_requester(
            model_request,
            config.decision_api_key,
            timeout=config.decision_timeout_seconds,
            stream_mode=config.decision_stream_mode,
        )
        judgment = _extract_json_object(parsed_response.content)
        label = str(judgment.get("tone_label") or "").strip()
        tone = TONE_VALUES.get(label)
        event = _clean_candidate_news_text(judgment.get("event"))[:120]
        impact = _clean_candidate_news_text(judgment.get("impact"))[:160]
        reason = _clean_candidate_news_text(judgment.get("reason"))[:120]
        if tone is None or not event or not impact or not reason:
            raise ValueError("decision_model_invalid_schema")
    except urllib.error.HTTPError as exc:
        error = f"decision_model_http_{int(exc.code)}"
        try:
            exc.close()
        except Exception:
            pass
    except (TimeoutError, OSError, ValueError) as exc:
        error = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc).startswith("decision_model_")
            else f"decision_model_{type(exc).__name__}"
        )
    except Exception as exc:  # Keep model/gateway details out of persisted data.
        error = f"decision_model_{type(exc).__name__}"
    else:
        result.update({
            "checked": True,
            "available": True,
            "tone": tone,
            "tone_label": label,
            "summary": (
                f"事件：{event}；影响：{impact}；"
                f"舆情：纯问财模式不包含雪球/X舆情（{label}）"
            )[:600],
            "judgment_reason": reason,
            "judgment_provider": "decision_model",
            "judgment_model": config.model,
            "provider": config.provider_label,
            "error": "",
        })
        return result

    result.update({
        "checked": False,
        "available": False,
        "tone": "neutral",
        "tone_label": "判断不可用",
        "summary": "",
        "judgment_provider": "decision_model",
        "judgment_model": config.model,
        "provider": config.provider_label,
        "error": error,
    })
    return result


def request_iwencai_candidate_news_record(
    candidate: Mapping[str, Any],
    config: NewsPrecheckConfig,
    *,
    fetched_at: str,
    client_factory: Callable[[IwencaiConfig], IwencaiClient] | None = None,
) -> dict[str, Any]:
    iwencai_config = IwencaiConfig(
        enabled=True,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout_seconds=config.timeout_seconds,
        max_retries=max(0, config.max_requests - 1),
        max_concurrency=config.concurrency,
    )
    active_client_factory = client_factory or IwencaiClient
    def request_source(source: str) -> tuple[str, Mapping[str, Any] | None, str]:
        client = active_client_factory(iwencai_config)
        try:
            if source == "hithink-event-query":
                payload = client.query(
                    build_iwencai_candidate_news_query(candidate, source),
                    page=1,
                    limit=20,
                    is_cache=False,
                    expand_index=True,
                    skill_id=source,
                )
            else:
                payload = client.comprehensive_search(
                    build_iwencai_candidate_news_query(candidate, source),
                    channel="announcement" if source == "announcement-search" else "news",
                    size=10,
                )
            return source, payload, ""
        except Exception as exc:
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            return source, None, code

    source_payloads: dict[str, Mapping[str, Any]] = {}
    source_errors: dict[str, str] = {}
    workers = min(len(IWENCAI_NEWS_SKILLS), config.concurrency)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(request_source, source) for source in IWENCAI_NEWS_SKILLS]
        for future in concurrent.futures.as_completed(futures):
            source, source_payload, error = future.result()
            if source_payload is not None:
                source_payloads[source] = source_payload
            if error:
                source_errors[source] = error
    return parse_iwencai_candidate_news_record(
        candidate,
        fetched_at=fetched_at,
        source_payloads=source_payloads,
        source_errors=source_errors,
    )


def _clean_candidate_news_text(content: Any) -> str:
    text = str(content or "")
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[\s*\[?\d+\]?\s*\]\s*\(\s*https?://[^)]+\)", "", text)
    text = re.sub(r"\[\[?\d+\]?\]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\(?https?://[^\s)]+\)?", "", text)
    text = re.sub(r"\*\*|__|~~|`", "", text)
    text = re.sub(r"^\s{0,3}(?:#{1,6}\s+|>\s*|[-+]\s+)", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"^(?:代码\s*名称|股票代码\s*股票名称)\s*[：:]\s*",
        "",
        text,
    )
    text = re.sub(r"^[-+]\s+", "", text).strip()
    text = re.sub(
        r"\s*[（(](?:此为|注\s*[：:]?|说明\s*[：:]?)[^）)]*[）)]\s*$",
        "",
        text,
    ).strip()
    return text


def fetch_candidate_news_records(
    candidates: list[dict[str, Any]],
    config: NewsPrecheckConfig,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    now: datetime | None = None,
    model_requester: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    selected = [item for item in candidates[:max_candidates] if isinstance(item, dict)]
    if not selected:
        return []
    current = now or datetime.now(CN_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    fetched_at = current.astimezone(CN_TZ).isoformat(timespec="seconds")
    results: list[dict[str, Any] | None] = [None] * len(selected)

    def fetch(index: int, candidate: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            collected = request_iwencai_candidate_news_record(
                candidate,
                config,
                fetched_at=fetched_at,
            )
            return index, judge_iwencai_news_with_decision_model(
                collected,
                config,
                requester=model_requester,
            )
        except Exception as exc:
            return index, {
                "code": str(candidate.get("code") or ""),
                "name": str(candidate.get("name") or ""),
                "checked": False,
                "available": False,
                "tone": "neutral",
                "tone_label": "不可用",
                "summary": "",
                "provider": config.provider_label,
                "source_mode": config.source_mode,
                "source_version": IWENCAI_NEWS_SOURCE_VERSION,
                "source_scope": list(IWENCAI_NEWS_SKILLS),
                "window_days": 3,
                "fetched_at": fetched_at,
                "error": f"request_{type(exc).__name__}",
            }

    workers = min(config.concurrency, len(selected))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, index, candidate) for index, candidate in enumerate(selected)]
        for future in concurrent.futures.as_completed(futures):
            index, record = future.result()
            results[index] = record
    return [record for record in results if isinstance(record, dict)]


def cached_news_record_matches_source(
    record: Mapping[str, Any] | None,
    source_mode: str,
    judgment_model: str = "",
) -> bool:
    """Accept only completed records from the current retrieval and model path."""

    if not isinstance(record, Mapping) or record.get("checked") is not True:
        return False
    if str(source_mode or "").strip().lower() != "iwencai":
        return False
    cached_source = str(record.get("source_mode") or "").strip().lower()
    source_matches = (
        cached_source == "iwencai"
        and str(record.get("source_version") or "").strip() == IWENCAI_NEWS_SOURCE_VERSION
    )
    if not source_matches:
        return False
    expected_model = str(judgment_model or "").strip()
    if (
        expected_model
        and str(record.get("judgment_provider") or "").strip() == "decision_model"
    ):
        return str(record.get("judgment_model") or "").strip() == expected_model
    return True


def format_cached_news_record(record: Mapping[str, Any]) -> str:
    if record.get("available") and str(record.get("summary") or "").strip():
        summary = str(record.get("summary") or "").strip().lstrip("- ")
        code = str(record.get("code") or "").strip()
        name = str(record.get("name") or "").strip()
        if summary.startswith(candidate_label(record)) or (
            code and summary.startswith(code)
        ) or (name and summary.startswith(name)):
            return f"- {summary}"
        return f"- {candidate_label(record)}：{summary}"
    return (
        f"- {candidate_label(record)}：消息面预检失败"
        f"（{record.get('error') or 'unavailable'}）"
    )


def format_cached_news_records(records: list[Mapping[str, Any]]) -> str:
    lines = [format_cached_news_record(record) for record in records]
    return "【消息面预检（扫描阶段缓存）】\n" + "\n".join(lines) if lines else ""
