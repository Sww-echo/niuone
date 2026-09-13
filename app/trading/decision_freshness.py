"""Explicit, serializable execution deadlines for model-generated orders."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

DECISION_MAX_AGE_SECONDS = 180


def decision_expiry(anchor: datetime) -> str:
    return (anchor + timedelta(seconds=DECISION_MAX_AGE_SECONDS)).isoformat(sep=" ")


def decision_is_expired(decision: Mapping[str, Any], now: datetime) -> bool:
    value = decision.get("decision_expires_at")
    if value is None:  # Historical direct callers retain their existing API.
        return False
    try:
        expiry = datetime.fromisoformat(str(value))
        return now >= expiry
    except (TypeError, ValueError):
        return True  # Malformed explicit deadlines must never authorize orders.
