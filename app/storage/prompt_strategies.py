"""Private SQLite storage for prompt-strategy drafts, versions, and audits."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
import zlib
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ == "app.storage":
    from app.core.paths import get_dashboard_home
    from app.strategies.rules import compile_strategy_spec, replay_rule_evaluation_audit
    from app.strategies.rules.schema import canonical_json, sha256_json, sha256_text
else:  # pragma: no cover - exercised by the legacy top-level import contract
    from core.paths import get_dashboard_home
    from strategies.rules import compile_strategy_spec, replay_rule_evaluation_audit
    from strategies.rules.schema import canonical_json, sha256_json, sha256_text


DEFAULT_STRATEGY_KEY = "preset_text"
MAX_RAW_PROMPT_CHARS = 8000


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_prompt_strategy_db_path() -> Path:
    dashboard_home = get_dashboard_home(Path(__file__).resolve().parents[1])
    return Path(
        os.environ.get(
            "DASHBOARD_PROMPT_STRATEGY_DB",
            dashboard_home / "prompt_strategies.db",
        )
    ).expanduser()


class PromptStrategyStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or default_prompt_strategy_db_path()).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS prompt_strategy_drafts (
                draft_id TEXT PRIMARY KEY,
                strategy_key TEXT NOT NULL,
                raw_prompt TEXT NOT NULL,
                raw_prompt_sha256 TEXT NOT NULL,
                refined_spec_json TEXT NOT NULL DEFAULT '',
                execution_plan_json TEXT NOT NULL DEFAULT '',
                plan_sha256 TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                refinement_prompt_sha256 TEXT NOT NULL DEFAULT '',
                validation_errors_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                activated_version_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS prompt_strategy_versions (
                version_id TEXT PRIMARY KEY,
                strategy_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                draft_id TEXT NOT NULL,
                raw_prompt TEXT NOT NULL,
                raw_prompt_sha256 TEXT NOT NULL,
                refined_spec_json TEXT NOT NULL,
                refined_spec_sha256 TEXT NOT NULL,
                execution_plan_json TEXT NOT NULL,
                plan_sha256 TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                retired_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(draft_id) REFERENCES prompt_strategy_drafts(draft_id),
                UNIQUE(strategy_key, revision)
            );

            CREATE TABLE IF NOT EXISTS prompt_strategy_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                strategy_version_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                audit_sha256 TEXT NOT NULL,
                audit_json TEXT NOT NULL,
                audit_encoding TEXT NOT NULL DEFAULT 'json',
                audit_zlib BLOB,
                created_at TEXT NOT NULL,
                FOREIGN KEY(strategy_version_id)
                    REFERENCES prompt_strategy_versions(version_id)
            );

            CREATE TABLE IF NOT EXISTS prompt_position_bindings (
                binding_id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                strategy_version_id TEXT NOT NULL,
                entry_evaluation_id TEXT NOT NULL DEFAULT '',
                entry_trade_key TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                bound_at TEXT NOT NULL,
                released_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(strategy_version_id)
                    REFERENCES prompt_strategy_versions(version_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_strategy_one_active
                ON prompt_strategy_versions(strategy_key)
                WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS idx_prompt_drafts_created
                ON prompt_strategy_drafts(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_prompt_versions_created
                ON prompt_strategy_versions(strategy_key, revision DESC);
            CREATE INDEX IF NOT EXISTS idx_prompt_evaluations_version_time
                ON prompt_strategy_evaluations(strategy_version_id, evaluated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_prompt_bindings_code_active
                ON prompt_position_bindings(code, active);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_bindings_one_active_code
                ON prompt_position_bindings(code)
                WHERE active = 1;

            DROP TRIGGER IF EXISTS prompt_version_content_immutable;
            CREATE TRIGGER prompt_version_content_immutable
                BEFORE UPDATE ON prompt_strategy_versions
                WHEN NEW.version_id <> OLD.version_id
                  OR NEW.strategy_key <> OLD.strategy_key
                  OR NEW.revision <> OLD.revision
                  OR NEW.draft_id <> OLD.draft_id
                  OR NEW.raw_prompt <> OLD.raw_prompt
                  OR NEW.raw_prompt_sha256 <> OLD.raw_prompt_sha256
                  OR NEW.refined_spec_json <> OLD.refined_spec_json
                  OR NEW.refined_spec_sha256 <> OLD.refined_spec_sha256
                  OR NEW.execution_plan_json <> OLD.execution_plan_json
                  OR NEW.plan_sha256 <> OLD.plan_sha256
                  OR NEW.engine_version <> OLD.engine_version
                  OR NEW.execution_mode <> OLD.execution_mode
                  OR NEW.model <> OLD.model
                  OR NEW.provider <> OLD.provider
                  OR NEW.created_at <> OLD.created_at
                BEGIN
                    SELECT RAISE(ABORT, 'prompt strategy version content is immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS prompt_version_no_delete
                BEFORE DELETE ON prompt_strategy_versions
                BEGIN
                    SELECT RAISE(ABORT, 'prompt strategy versions are immutable');
                END;

            CREATE TRIGGER IF NOT EXISTS prompt_evaluation_no_update
                BEFORE UPDATE ON prompt_strategy_evaluations
                BEGIN
                    SELECT RAISE(ABORT, 'prompt strategy evaluations are append-only');
                END;
            CREATE TRIGGER IF NOT EXISTS prompt_evaluation_no_delete
                BEFORE DELETE ON prompt_strategy_evaluations
                BEGIN
                    SELECT RAISE(ABORT, 'prompt strategy evaluations are append-only');
                END;
            """)
            evaluation_columns = {
                str(row["name"] or "")
                for row in conn.execute(
                    "PRAGMA table_info(prompt_strategy_evaluations)"
                )
            }
            if "idempotency_key" not in evaluation_columns:
                conn.execute(
                    "ALTER TABLE prompt_strategy_evaluations "
                    "ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''"
                )
            if "audit_encoding" not in evaluation_columns:
                conn.execute(
                    "ALTER TABLE prompt_strategy_evaluations "
                    "ADD COLUMN audit_encoding TEXT NOT NULL DEFAULT 'json'"
                )
            if "audit_zlib" not in evaluation_columns:
                conn.execute(
                    "ALTER TABLE prompt_strategy_evaluations ADD COLUMN audit_zlib BLOB"
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_evaluation_idempotency
                ON prompt_strategy_evaluations(idempotency_key)
                WHERE idempotency_key <> ''
                """
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _draft_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for source, target, default in (
            ("refined_spec_json", "refined_spec", None),
            ("execution_plan_json", "execution_plan", None),
            ("validation_errors_json", "validation_errors", []),
        ):
            raw = str(value.pop(source, "") or "")
            try:
                value[target] = json.loads(raw) if raw else default
            except json.JSONDecodeError:
                value[target] = default
        return value

    @staticmethod
    def _version_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for source, target in (
            ("refined_spec_json", "refined_spec"),
            ("execution_plan_json", "execution_plan"),
        ):
            raw = str(value.pop(source, "") or "")
            try:
                value[target] = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                value[target] = None
        spec = value.get("refined_spec")
        plan = value.get("execution_plan")
        plan_without_hash = dict(plan) if isinstance(plan, Mapping) else {}
        embedded_plan_sha256 = str(plan_without_hash.pop("plan_sha256", ""))
        integrity_ok = (
            isinstance(spec, Mapping)
            and isinstance(plan, Mapping)
            and sha256_text(str(value.get("raw_prompt") or ""))
            == str(value.get("raw_prompt_sha256") or "")
            and sha256_json(spec) == str(value.get("refined_spec_sha256") or "")
            and embedded_plan_sha256 == str(value.get("plan_sha256") or "")
            and sha256_json(plan_without_hash) == embedded_plan_sha256
            and str(plan.get("engine_version") or "")
            == str(value.get("engine_version") or "")
        )
        if not integrity_ok:
            raise ValueError("文字策略冻结版本完整性校验失败")
        return value

    @staticmethod
    def _validated_audit_payload(
        version: Mapping[str, Any],
        strategy_version_id: str,
        audit: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        payload = dict(audit)
        recorded_sha256 = str(payload.pop("audit_sha256", ""))
        if not recorded_sha256 or recorded_sha256 != sha256_json(payload):
            raise ValueError("文字策略审计指纹无效")
        payload["audit_sha256"] = recorded_sha256
        if str(payload.get("strategy_version_id") or "") != strategy_version_id:
            raise ValueError("文字策略审计与版本不匹配")
        plan = version.get("execution_plan")
        if not isinstance(plan, Mapping):
            raise ValueError("文字策略版本缺少执行计划")
        if str(payload.get("plan_sha256") or "") != str(
            version.get("plan_sha256") or ""
        ):
            raise ValueError("文字策略审计计划指纹与冻结版本不匹配")
        replay = replay_rule_evaluation_audit(payload, plan=dict(plan))
        if not replay.get("ok"):
            raise ValueError("文字策略审计无法确定性重放")
        return payload, recorded_sha256

    @staticmethod
    def _encode_audit(payload: Mapping[str, Any]) -> tuple[str, str, bytes]:
        raw = canonical_json(payload).encode("utf-8")
        return "zlib-json", "", zlib.compress(raw, level=9)

    @staticmethod
    def _decode_audit(row: Mapping[str, Any]) -> dict[str, Any]:
        encoding = str(row.get("audit_encoding") or "json")
        try:
            if encoding == "zlib-json":
                blob = row.get("audit_zlib")
                if not isinstance(blob, (bytes, bytearray, memoryview)):
                    raise ValueError("文字策略压缩审计记录缺失")
                raw = zlib.decompress(bytes(blob)).decode("utf-8")
            elif encoding == "json":
                raw = str(row.get("audit_json") or "")
            else:
                raise ValueError("文字策略审计编码不受支持")
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, zlib.error) as exc:
            raise ValueError("文字策略审计记录无法解析") from exc
        if not isinstance(value, dict):
            raise ValueError("文字策略审计记录必须是对象")
        return value

    @staticmethod
    def _evaluation_idempotency_key(
        version_id: str,
        payload: Mapping[str, Any],
    ) -> str:
        replay_context = payload.get("replay_context")
        replay_context = dict(replay_context) if isinstance(replay_context, Mapping) else {}
        data_quality = replay_context.get("data_quality")
        stable_quality = dict(data_quality) if isinstance(data_quality, Mapping) else {}
        stable = {
            "strategy_version_id": str(version_id or ""),
            "code": str(payload.get("code") or ""),
            "stage": str(payload.get("stage") or ""),
            "evaluated_at": str(payload.get("evaluated_at") or ""),
            "facts": replay_context.get("facts") or {},
            "previous_facts": replay_context.get("previous_facts") or {},
            "history_facts": replay_context.get("history_facts") or [],
            "runtime_facts": replay_context.get("runtime_facts") or {},
            "data_quality": stable_quality,
            "evaluation": payload.get("evaluation") or {},
            "action_intent": payload.get("action_intent"),
        }
        return sha256_json(stable)

    def create_draft(
        self,
        raw_prompt: str,
        *,
        strategy_key: str = DEFAULT_STRATEGY_KEY,
    ) -> dict[str, Any]:
        self.init()
        normalized = str(raw_prompt or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError("文字策略Prompt不能为空")
        if len(normalized) > MAX_RAW_PROMPT_CHARS:
            raise ValueError(f"文字策略Prompt最多{MAX_RAW_PROMPT_CHARS}字")
        draft_id = f"draft-{uuid.uuid4().hex}"
        created_at = _now()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO prompt_strategy_drafts (
                    draft_id, strategy_key, raw_prompt, raw_prompt_sha256,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    draft_id,
                    str(strategy_key or DEFAULT_STRATEGY_KEY),
                    normalized,
                    sha256_text(normalized),
                    created_at,
                    created_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_draft(draft_id) or {}

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        self.init()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM prompt_strategy_drafts WHERE draft_id = ?",
                (str(draft_id or ""),),
            ).fetchone()
            return self._draft_payload(row)
        finally:
            conn.close()

    def list_drafts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.init()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM prompt_strategy_drafts ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
            return [self._draft_payload(row) or {} for row in rows]
        finally:
            conn.close()

    def claim_refinement(self, draft_id: str) -> dict[str, Any]:
        """Atomically reserve a draft before making its one model request."""
        self.init()
        stale_before = (datetime.now() - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE prompt_strategy_drafts
                SET status = 'refining', updated_at = ?
                WHERE draft_id = ?
                  AND (status = 'draft' OR (status = 'refining' AND updated_at < ?))
                  AND refined_spec_json = ''
                """,
                (_now(), str(draft_id or ""), stale_before),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT status FROM prompt_strategy_drafts WHERE draft_id = ?",
                    (str(draft_id or ""),),
                ).fetchone()
                if row is None:
                    raise ValueError("文字策略草案不存在")
                raise ValueError("文字策略草案已在细化或已细化，请创建新草案")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_draft(draft_id) or {}

    def release_refinement_claim(self, draft_id: str) -> bool:
        """Make a draft retryable only when its reserved request did not finish."""
        self.init()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE prompt_strategy_drafts
                SET status = 'draft', updated_at = ?
                WHERE draft_id = ? AND status = 'refining'
                  AND refined_spec_json = ''
                """,
                (_now(), str(draft_id or "")),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def save_refinement(
        self,
        draft_id: str,
        refined_spec: Mapping[str, Any],
        *,
        model: str,
        provider: str,
        refinement_prompt_sha256: str = "",
    ) -> dict[str, Any]:
        self.init()
        draft = self.get_draft(draft_id)
        if draft is None:
            raise ValueError("文字策略草案不存在")
        if draft["status"] == "activated":
            raise ValueError("已激活草案不可修改，请创建新草案")
        if draft.get("refined_spec") is not None:
            raise ValueError("文字策略草案已经细化，请创建新草案后再调整")
        validation_errors: list[str] = []
        execution_plan: dict[str, Any] | None = None
        try:
            execution_plan = compile_strategy_spec(dict(refined_spec))
        except ValueError as exc:
            validation_errors = list(getattr(exc, "errors", ()) or [str(exc)])
        status = "pending_confirmation" if execution_plan else "validation_failed"
        updated_at = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE prompt_strategy_drafts
                SET refined_spec_json = ?, execution_plan_json = ?,
                    plan_sha256 = ?, status = ?, model = ?, provider = ?,
                    refinement_prompt_sha256 = ?, validation_errors_json = ?,
                    updated_at = ?
                WHERE draft_id = ? AND status IN ('draft', 'refining')
                  AND refined_spec_json = ''
                """,
                (
                    canonical_json(refined_spec),
                    canonical_json(execution_plan) if execution_plan else "",
                    str((execution_plan or {}).get("plan_sha256") or ""),
                    status,
                    str(model or ""),
                    str(provider or ""),
                    str(refinement_prompt_sha256 or ""),
                    canonical_json(validation_errors),
                    updated_at,
                    str(draft_id or ""),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("文字策略草案已细化，请创建新草案后再调整")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_draft(draft_id) or {}

    def prepare_activation(self, draft_id: str) -> dict[str, Any]:
        """Create an immutable pending version without retiring the live version."""
        self.init()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM prompt_strategy_drafts WHERE draft_id = ?",
                (str(draft_id or ""),),
            ).fetchone()
            draft = self._draft_payload(row)
            if draft is None:
                raise ValueError("文字策略草案不存在")
            if draft["status"] == "activating":
                pending_row = conn.execute(
                    """
                    SELECT * FROM prompt_strategy_versions
                    WHERE version_id = ? AND draft_id = ?
                      AND status = 'pending_activation'
                    """,
                    (
                        str(draft.get("activated_version_id") or ""),
                        str(draft_id or ""),
                    ),
                ).fetchone()
                if pending_row is None:
                    raise ValueError("文字策略待激活版本缺失")
                pending = self._version_payload(pending_row)
                conn.commit()
                return pending or {}
            if draft["status"] != "pending_confirmation":
                raise ValueError("只有校验通过且待确认的草案可以激活")
            spec = draft.get("refined_spec")
            if not isinstance(spec, Mapping):
                raise ValueError("文字策略结构化规则缺失")
            plan = compile_strategy_spec(dict(spec))
            if str(plan.get("plan_sha256") or "") != str(draft.get("plan_sha256") or ""):
                raise ValueError("文字策略执行计划在确认前发生变化")
            strategy_key = str(draft.get("strategy_key") or DEFAULT_STRATEGY_KEY)
            current_revision = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM prompt_strategy_versions WHERE strategy_key = ?",
                (strategy_key,),
            ).fetchone()[0]
            revision = int(current_revision or 0) + 1
            version_id = f"{strategy_key}-v{revision}-{uuid.uuid4().hex[:12]}"
            created_at = _now()
            conn.execute(
                """
                INSERT INTO prompt_strategy_versions (
                    version_id, strategy_key, revision, draft_id,
                    raw_prompt, raw_prompt_sha256, refined_spec_json,
                    refined_spec_sha256, execution_plan_json, plan_sha256,
                    engine_version, execution_mode, model, provider, status,
                    created_at, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_activation', ?, '')
                """,
                (
                    version_id,
                    strategy_key,
                    revision,
                    draft_id,
                    draft["raw_prompt"],
                    draft["raw_prompt_sha256"],
                    canonical_json(spec),
                    sha256_json(spec),
                    canonical_json(plan),
                    plan["plan_sha256"],
                    plan["engine_version"],
                    plan["strategy"]["execution_mode"],
                    draft["model"],
                    draft["provider"],
                    created_at,
                ),
            )
            conn.execute(
                """
                UPDATE prompt_strategy_drafts
                SET status = 'activating', activated_version_id = ?, updated_at = ?
                WHERE draft_id = ?
                """,
                (version_id, created_at, draft_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_version(version_id) or {}

    def commit_activation(self, version_id: str) -> dict[str, Any]:
        """Atomically retire the previous version and publish a prepared version."""
        self.init()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM prompt_strategy_versions WHERE version_id = ?",
                (str(version_id or ""),),
            ).fetchone()
            if row is not None and str(row["status"] or "") == "active":
                conn.commit()
                return self._version_payload(row) or {}
            if row is None or str(row["status"] or "") != "pending_activation":
                raise ValueError("文字策略待激活版本不存在或状态已变化")
            activated_at = _now()
            conn.execute(
                """
                UPDATE prompt_strategy_versions
                SET status = 'retired', retired_at = ?
                WHERE strategy_key = ? AND status = 'active'
                """,
                (activated_at, str(row["strategy_key"] or "")),
            )
            cursor = conn.execute(
                """
                UPDATE prompt_strategy_versions
                SET status = 'active', activated_at = ?, retired_at = ''
                WHERE version_id = ? AND status = 'pending_activation'
                """,
                (activated_at, str(version_id or "")),
            )
            if cursor.rowcount != 1:
                raise ValueError("文字策略待激活版本提交失败")
            conn.execute(
                """
                UPDATE prompt_strategy_drafts
                SET status = 'activated', activated_version_id = ?, updated_at = ?
                WHERE draft_id = ? AND status = 'activating'
                """,
                (str(version_id or ""), activated_at, str(row["draft_id"] or "")),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_version(version_id) or {}

    def fail_activation(self, version_id: str) -> bool:
        """Return a failed two-phase activation to a reviewable draft state."""
        self.init()
        failed_at = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT draft_id FROM prompt_strategy_versions WHERE version_id = ?",
                (str(version_id or ""),),
            ).fetchone()
            cursor = conn.execute(
                """
                UPDATE prompt_strategy_versions
                SET status = 'activation_failed', retired_at = ?
                WHERE version_id = ? AND status = 'pending_activation'
                """,
                (failed_at, str(version_id or "")),
            )
            if cursor.rowcount == 1 and row is not None:
                conn.execute(
                    """
                    UPDATE prompt_strategy_drafts
                    SET status = 'pending_confirmation', activated_version_id = '',
                        updated_at = ?
                    WHERE draft_id = ? AND status = 'activating'
                    """,
                    (failed_at, str(row["draft_id"] or "")),
                )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def activate_draft(self, draft_id: str) -> dict[str, Any]:
        prepared = self.prepare_activation(draft_id)
        version_id = str(prepared.get("version_id") or "")
        try:
            return self.commit_activation(version_id)
        except Exception:
            self.fail_activation(version_id)
            raise

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        self.init()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM prompt_strategy_versions WHERE version_id = ?",
                (str(version_id or ""),),
            ).fetchone()
            return self._version_payload(row)
        finally:
            conn.close()

    def active_version(
        self,
        strategy_key: str = DEFAULT_STRATEGY_KEY,
    ) -> dict[str, Any] | None:
        self.init()
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM prompt_strategy_versions
                WHERE strategy_key = ? AND status = 'active'
                ORDER BY revision DESC LIMIT 1
                """,
                (str(strategy_key or DEFAULT_STRATEGY_KEY),),
            ).fetchone()
            return self._version_payload(row)
        finally:
            conn.close()

    def list_versions(
        self,
        strategy_key: str = DEFAULT_STRATEGY_KEY,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.init()
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM prompt_strategy_versions
                WHERE strategy_key = ?
                ORDER BY revision DESC LIMIT ?
                """,
                (str(strategy_key or DEFAULT_STRATEGY_KEY), max(1, min(200, int(limit)))),
            ).fetchall()
            return [self._version_payload(row) or {} for row in rows]
        finally:
            conn.close()

    def record_evaluation(
        self,
        strategy_version_id: str,
        audit: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.init()
        evaluation_id = f"evaluation-{uuid.uuid4().hex}"
        created_at = _now()
        version_id = str(strategy_version_id or "")
        version = self.get_version(version_id)
        if version is None:
            raise ValueError("文字策略版本不存在")
        audit_payload, recorded_sha256 = self._validated_audit_payload(
            version,
            version_id,
            audit,
        )
        audit_sha256 = recorded_sha256
        idempotency_key = self._evaluation_idempotency_key(
            version_id,
            audit_payload,
        )
        audit_encoding, audit_json, audit_zlib = self._encode_audit(audit_payload)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT evaluation_id, audit_sha256, created_at
                FROM prompt_strategy_evaluations
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {
                    "evaluation_id": str(existing["evaluation_id"] or ""),
                    "audit_sha256": str(existing["audit_sha256"] or ""),
                    "created_at": str(existing["created_at"] or ""),
                    "deduplicated": True,
                }
            conn.execute(
                """
                INSERT INTO prompt_strategy_evaluations (
                    evaluation_id, strategy_version_id, idempotency_key,
                    code, stage, status, evaluated_at, audit_sha256,
                    audit_json, audit_encoding, audit_zlib, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    version_id,
                    idempotency_key,
                    str(audit_payload.get("code") or ""),
                    str(audit_payload.get("stage") or ""),
                    str((audit_payload.get("evaluation") or {}).get("status") or ""),
                    str(audit_payload.get("evaluated_at") or created_at),
                    audit_sha256,
                    audit_json,
                    audit_encoding,
                    audit_zlib,
                    created_at,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "evaluation_id": evaluation_id,
            "audit_sha256": audit_sha256,
            "created_at": created_at,
            "deduplicated": False,
        }

    def record_evaluations_batch(
        self,
        strategy_version_id: str,
        audits: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        self.init()
        version_id = str(strategy_version_id or "")
        version = self.get_version(version_id)
        if version is None:
            raise ValueError("文字策略版本不存在")
        prepared: list[dict[str, Any]] = []
        for audit in audits:
            payload, recorded_sha256 = self._validated_audit_payload(
                version,
                version_id,
                audit,
            )
            evaluation_id = f"evaluation-{uuid.uuid4().hex}"
            created_at = _now()
            audit_encoding, audit_json, audit_zlib = self._encode_audit(payload)
            prepared.append({
                "evaluation_id": evaluation_id,
                "audit_sha256": recorded_sha256,
                "created_at": created_at,
                "idempotency_key": self._evaluation_idempotency_key(
                    version_id,
                    payload,
                ),
                "payload": payload,
                "audit_json": audit_json,
                "audit_encoding": audit_encoding,
                "audit_zlib": audit_zlib,
            })
        if not prepared:
            return []
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            results: list[dict[str, Any]] = []
            for item in prepared:
                payload = item["payload"]
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO prompt_strategy_evaluations (
                        evaluation_id, strategy_version_id, idempotency_key,
                        code, stage, status, evaluated_at, audit_sha256,
                        audit_json, audit_encoding, audit_zlib, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["evaluation_id"],
                        version_id,
                        item["idempotency_key"],
                        str(payload.get("code") or ""),
                        str(payload.get("stage") or ""),
                        str((payload.get("evaluation") or {}).get("status") or ""),
                        str(payload.get("evaluated_at") or item["created_at"]),
                        item["audit_sha256"],
                        item["audit_json"],
                        item["audit_encoding"],
                        item["audit_zlib"],
                        item["created_at"],
                    ),
                )
                if cursor.rowcount == 1:
                    results.append({
                        "evaluation_id": item["evaluation_id"],
                        "audit_sha256": item["audit_sha256"],
                        "created_at": item["created_at"],
                        "deduplicated": False,
                    })
                else:
                    existing = conn.execute(
                        """
                        SELECT evaluation_id, audit_sha256, created_at
                        FROM prompt_strategy_evaluations
                        WHERE idempotency_key = ?
                        """,
                        (item["idempotency_key"],),
                    ).fetchone()
                    if existing is None:
                        raise sqlite3.IntegrityError("审计幂等写入失败")
                    results.append({
                        "evaluation_id": str(existing["evaluation_id"] or ""),
                        "audit_sha256": str(existing["audit_sha256"] or ""),
                        "created_at": str(existing["created_at"] or ""),
                        "deduplicated": True,
                    })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return results

    def list_evaluations(
        self,
        strategy_version_id: str,
        *,
        code: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.init()
        version = self.get_version(strategy_version_id)
        if version is None:
            raise ValueError("文字策略版本不存在")
        conn = self._connect()
        try:
            params: list[Any] = [str(strategy_version_id or "")]
            code_filter = ""
            if str(code or "").strip():
                code_filter = " AND code = ?"
                params.append(str(code).strip())
            params.append(max(1, min(500, int(limit))))
            rows = conn.execute(
                f"""
                SELECT * FROM prompt_strategy_evaluations
                WHERE strategy_version_id = ?{code_filter}
                ORDER BY evaluated_at DESC, created_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["audit"] = self._decode_audit(item)
                item.pop("audit_json", None)
                item.pop("audit_zlib", None)
                self._validated_audit_payload(
                    version,
                    str(strategy_version_id or ""),
                    item["audit"],
                )
                results.append(item)
            return results
        finally:
            conn.close()

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        """Load and replay-validate one exact append-only evaluation record."""
        self.init()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM prompt_strategy_evaluations WHERE evaluation_id = ?",
                (str(evaluation_id or ""),),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
        finally:
            conn.close()
        version_id = str(item.get("strategy_version_id") or "")
        version = self.get_version(version_id)
        if version is None:
            raise ValueError("文字策略审计关联版本不存在")
        item["audit"] = self._decode_audit(item)
        item.pop("audit_json", None)
        item.pop("audit_zlib", None)
        self._validated_audit_payload(version, version_id, item["audit"])
        return item

    def bind_position(
        self,
        *,
        code: str,
        strategy_version_id: str,
        entry_evaluation_id: str = "",
        entry_trade_key: str = "",
    ) -> dict[str, Any]:
        self.init()
        binding_id = f"binding-{uuid.uuid4().hex}"
        bound_at = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE prompt_position_bindings
                SET active = 0, released_at = ?
                WHERE code = ? AND active = 1
                """,
                (bound_at, str(code or "")),
            )
            conn.execute(
                """
                INSERT INTO prompt_position_bindings (
                    binding_id, code, strategy_version_id,
                    entry_evaluation_id, entry_trade_key, active, bound_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    binding_id,
                    str(code or ""),
                    str(strategy_version_id or ""),
                    str(entry_evaluation_id or ""),
                    str(entry_trade_key or ""),
                    bound_at,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "binding_id": binding_id,
            "code": str(code or ""),
            "strategy_version_id": str(strategy_version_id or ""),
            "bound_at": bound_at,
        }

    def active_position_binding(self, code: str) -> dict[str, Any] | None:
        self.init()
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM prompt_position_bindings
                WHERE code = ? AND active = 1
                ORDER BY bound_at DESC LIMIT 1
                """,
                (str(code or ""),),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def release_position(self, code: str) -> bool:
        self.init()
        released_at = _now()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE prompt_position_bindings
                SET active = 0, released_at = ?
                WHERE code = ? AND active = 1
                """,
                (released_at, str(code or "")),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()
