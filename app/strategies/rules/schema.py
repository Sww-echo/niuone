"""Stable schema identifiers and canonical serialization helpers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


PROMPT_STRATEGY_SPEC_SCHEMA_VERSION = 1
PROMPT_EXECUTION_PLAN_SCHEMA_VERSION = 1
PROMPT_RULE_ENGINE_VERSION = "prompt-rules-v3"
SUPPORTED_PROMPT_RULE_ENGINE_VERSIONS = frozenset({
    "prompt-rules-v2",
    PROMPT_RULE_ENGINE_VERSION,
})

RULE_STAGES = ("selection", "entry", "exit")
BAR_STATUSES = frozenset({"closed", "live"})
TIMEFRAMES = frozenset({"1d"})
MISSING_DATA_POLICIES = frozenset({"hold"})
CONFLICT_POLICIES = frozenset({"exit_first"})
EXECUTION_MODES = frozenset({"recommend_only", "simulation"})


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
