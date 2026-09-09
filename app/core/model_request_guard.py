"""Cross-process model admission and minute-scale upstream backoff.

Only hashes, lease IDs and timing metadata are persisted. Portfolio locks and
market-data requests never use this coordinator.
"""
from __future__ import annotations

import hashlib
import inspect
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from .paths import get_dashboard_home

MIN_REQUEST_INTERVAL = 2.0
MAX_QUEUE_SECONDS = 5.0
DEFAULT_RATE_BACKOFF = 60.0
MAX_RATE_BACKOFF = 300.0
_deadline: ContextVar[float | None] = ContextVar("model_deadline", default=None)


class ModelRequestExpired(TimeoutError):
    """The model result can no longer be used within this request budget."""


class ModelAdmissionError(RuntimeError):
    """A model request was not sent; a later cycle may try again."""

    def __init__(self, reason: str, retry_after: float = 0.0):
        self.reason = reason
        self.retry_after = max(0.0, retry_after)
        super().__init__(f"model_request_{reason}; retry_after={self.retry_after:.1f}s")


def remaining_model_seconds(timeout: float) -> float:
    deadline = _deadline.get()
    remaining = min(float(timeout), deadline - time.monotonic()) if deadline is not None else float(timeout)
    if not math.isfinite(remaining) or remaining <= 0:
        raise ModelRequestExpired("model_request_deadline_exceeded")
    return remaining


def budgeted_model_call(function: Callable[..., Any]) -> Callable[..., Any]:
    """Share one deadline across transport and malformed-response retries."""
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        timeout = float(bound.arguments.get("timeout", 60))
        with model_request_budget(timeout):
            result = function(*args, **kwargs)
            remaining_model_seconds(timeout)
            return result
    return wrapped


@contextmanager
def model_request_budget(timeout: float) -> Iterator[None]:
    remaining_model_seconds(timeout)
    own_deadline = time.monotonic() + timeout
    inherited = _deadline.get()
    token = _deadline.set(min(own_deadline, inherited) if inherited is not None else own_deadline)
    try:
        yield
        remaining_model_seconds(timeout)
    finally:
        _deadline.reset(token)


def retry_after_seconds(value: str | None, now: float) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def request_scope(endpoint: str, api_key: str) -> str:
    url = urlsplit(endpoint)
    # Chat/Responses, models, and processes share the provider's credential
    # budget. Neither the credential nor endpoint appears in the database.
    identity = f"{url.scheme.lower()}://{url.netloc.lower()}\0{api_key}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class ModelRequestCoordinator:
    def __init__(self, path: Path, *, interval: float = MIN_REQUEST_INTERVAL,
                 clock: Callable[[], float] = time.time):
        self.path = path
        self.interval = max(0.0, float(interval))
        self.clock = clock

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=0.5)
            conn.execute("CREATE TABLE IF NOT EXISTS model_admission (scope TEXT PRIMARY KEY, next_start REAL NOT NULL DEFAULT 0, cooldown REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, lease TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0)")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                conn.rollback()
            raise ModelAdmissionError("coordination_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()

    def acquire(self, scope: str, *, duration: float) -> str:
        if not math.isfinite(duration) or duration <= 0:
            raise ModelRequestExpired("model_request_deadline_exceeded")
        now = self.clock()
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO model_admission(scope) VALUES (?)", (scope,))
            next_start, cooldown, failures, lease, lease_until = conn.execute(
                "SELECT next_start,cooldown,failures,lease,lease_until FROM model_admission WHERE scope=?", (scope,)
            ).fetchone()
            if cooldown > now:
                raise ModelAdmissionError("rate_limited", cooldown - now)
            if lease and lease_until > now:
                raise ModelAdmissionError("busy", lease_until - now)
            if next_start > now:
                raise ModelAdmissionError("spacing", next_start - now)
            token = uuid.uuid4().hex
            conn.execute("UPDATE model_admission SET next_start=?,lease=?,lease_until=? WHERE scope=?",
                         (now + self.interval, token, now + duration + 5, scope))
            return token

    def release(self, scope: str, lease: str, *, success: bool = False) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE model_admission SET lease='',lease_until=0,failures=CASE WHEN ? THEN 0 ELSE failures END WHERE scope=? AND lease=?",
                         (success, scope, lease))

    def rate_limited(self, scope: str, lease: str, retry_after: str | None) -> float:
        now = self.clock()
        with self._transaction() as conn:
            row = conn.execute("SELECT failures FROM model_admission WHERE scope=? AND lease=?", (scope, lease)).fetchone()
            if row is None:
                return DEFAULT_RATE_BACKOFF
            failures = min(10, int(row[0]) + 1)
            delay = min(MAX_RATE_BACKOFF, DEFAULT_RATE_BACKOFF * 2 ** (failures - 1))
            requested = retry_after_seconds(retry_after, now)
            # A long Retry-After is a durable do-not-send timestamp, never a
            # blocking sleep. Do not retry early by truncating the header.
            delay = max(delay, requested or 0.0)
            conn.execute("UPDATE model_admission SET cooldown=MAX(cooldown,?),failures=? WHERE scope=? AND lease=?",
                         (now + delay, failures, scope, lease))
            return delay


def shared_model_coordinator() -> ModelRequestCoordinator:
    root = Path(__file__).resolve().parents[2]
    return ModelRequestCoordinator(get_dashboard_home(root) / "model_admission.sqlite3")
