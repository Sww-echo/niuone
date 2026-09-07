#!/usr/bin/env python3
"""实战页面 · SQLite 数据库层

替代 JSON 文件存储，提供：
  - daily_equity 每日资金快照
  - position_snapshots 每日持仓快照
  - trades 交易记录
  - decisions 决策记录
  - 首次运行自动从 JSON 迁移历史数据
"""
import hashlib
import json
import math
import os
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from a_share_calendar import is_a_share_trading_day
from niuone_paths import get_dashboard_home

DASHBOARD_HOME = get_dashboard_home(Path(__file__).resolve().parents[1])
DB_PATH = Path(os.environ.get("DASHBOARD_NIUNIU_DB", DASHBOARD_HOME / "niuniu.db")).expanduser()
STATE_FILE = Path(
    os.environ.get(
        "DASHBOARD_PORTFOLIO_STATE",
        DASHBOARD_HOME / "cron" / "output" / "niuniu_practice_portfolio.json",
    )
).expanduser()

ACCOUNT_HISTORY_KINDS = frozenset({
    "trade_log",
    "decision_log",
    "equity_history",
    "daily_equity_history",
})


def _json_safe(value):
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_payload(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decision_event_key(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _history_logical_key(kind: str, value: Any, payload_json: str) -> str:
    """Return the active-record identity without discarding archived revisions."""
    if isinstance(value, Mapping):
        if kind in {"equity_history", "daily_equity_history"}:
            time_text = str(value.get("time") or value.get("date") or "")
            if time_text:
                return f"time:{time_text}"
        if kind == "trade_log":
            identity = {
                field: value.get(field, "")
                for field in (
                    "time",
                    "action",
                    "code",
                    "shares",
                    "price",
                    "reason",
                )
            }
            return "trade:" + hashlib.sha256(
                _canonical_payload(identity).encode("utf-8")
            ).hexdigest()
    return "payload:" + _decision_event_key(payload_json)


def _history_event_time(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return str(value.get("time") or value.get("date") or "")


def _is_trading_day_text(value: str) -> bool:
    try:
        return is_a_share_trading_day(datetime.strptime(str(value or "")[:10], "%Y-%m-%d"))
    except Exception:
        return True


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    """初始化数据库表结构。"""
    conn = _connect()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS daily_equity (
        date       TEXT PRIMARY KEY,   -- 'YYYY-MM-DD'
        equity     REAL NOT NULL,      -- 总权益
        cash       REAL NOT NULL,      -- 现金
        market_value REAL NOT NULL,    -- 持仓市值
        pnl_pct    REAL NOT NULL,      -- 累计收益率%
        account_created_at TEXT NOT NULL DEFAULT '', -- 账户会话创建时间
        created_at TEXT NOT NULL       -- 'YYYY-MM-DD HH:MM:SS'
    );
    
    CREATE TABLE IF NOT EXISTS position_snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        date       TEXT NOT NULL,      -- 'YYYY-MM-DD'
        code       TEXT NOT NULL,      -- 股票代码
        name       TEXT DEFAULT '',
        shares     INTEGER NOT NULL,
        avg_cost   REAL NOT NULL,
        last_price REAL NOT NULL,
        market_value REAL NOT NULL,
        pnl        REAL NOT NULL,
        pnl_pct    REAL NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(date, code)
    );
    
    CREATE TABLE IF NOT EXISTS trades (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        time       TEXT NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS'
        action     TEXT NOT NULL,      -- 'BUY' | 'SELL'
        code       TEXT NOT NULL,
        name       TEXT DEFAULT '',
        shares     INTEGER NOT NULL,
        price      REAL NOT NULL,
        amount     REAL NOT NULL,
        commission REAL DEFAULT 0,
        transfer_fee REAL DEFAULT 0,
        stamp_duty REAL DEFAULT 0,
        pnl        REAL,               -- SELL时才有的盈亏
        reason     TEXT DEFAULT '',
        position_lifecycle_id TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '', -- 完整成交证据，供严格前向评估
        created_at TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS decisions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        time       TEXT NOT NULL,
        model      TEXT DEFAULT '',
        provider   TEXT DEFAULT '',
        trade_allowed INTEGER DEFAULT 1,
        trade_reason TEXT DEFAULT '',
        summary    TEXT DEFAULT '',
        actions_json TEXT DEFAULT '',   -- JSON array of actions
        error      TEXT DEFAULT '',
        b1_generated_at TEXT DEFAULT '',
        schedule_slot TEXT DEFAULT '',
        schedule_run_kind TEXT DEFAULT '',
        event_key TEXT,
        payload_json TEXT NOT NULL DEFAULT '', -- 完整候选与决策证据
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS account_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        history_kind TEXT NOT NULL,
        event_key    TEXT NOT NULL,
        logical_key  TEXT NOT NULL,
        event_time   TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        archived_at  TEXT NOT NULL,
        UNIQUE(history_kind, event_key)
    );

    CREATE TABLE IF NOT EXISTS probe_chase_outcomes (
        observation_key TEXT PRIMARY KEY,
        protocol_version TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS post_exit_observations (
        trade_key TEXT NOT NULL,
        horizon INTEGER NOT NULL,
        sell_time TEXT NOT NULL,
        code TEXT NOT NULL,
        sell_price REAL NOT NULL,
        sell_notional REAL NOT NULL DEFAULT 0,
        price_basis TEXT NOT NULL DEFAULT '',
        shares INTEGER NOT NULL,
        full_exit INTEGER NOT NULL,
        exit_rule TEXT NOT NULL DEFAULT '',
        exit_signal TEXT NOT NULL DEFAULT '',
        buy_strategy TEXT NOT NULL DEFAULT '',
        replacement_target_code TEXT NOT NULL DEFAULT '',
        sessions_observed INTEGER NOT NULL DEFAULT 0,
        observation_date TEXT NOT NULL DEFAULT '',
        close_return_pct REAL,
        mfe_pct REAL,
        mae_pct REAL,
        benchmark_return_pct REAL,
        excess_return_pct REAL,
        replacement_return_pct REAL,
        replacement_counterfactual_return_pct REAL,
        replacement_regret_pct REAL,
        replacement_regret INTEGER,
        replacement_executed INTEGER NOT NULL DEFAULT 0,
        replacement_buy_time TEXT NOT NULL DEFAULT '',
        replacement_buy_price REAL,
        replacement_buy_shares INTEGER NOT NULL DEFAULT 0,
        replacement_buy_fee REAL NOT NULL DEFAULT 0,
        feedback_policy_version INTEGER NOT NULL DEFAULT 0,
        sell_fly_threshold_pct REAL,
        sell_fly INTEGER,
        avoided_loss INTEGER,
        completed INTEGER NOT NULL DEFAULT 0,
        quality_status TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY(trade_key, horizon)
    );

    CREATE TABLE IF NOT EXISTS post_exit_reentry_observations (
        audit_key TEXT PRIMARY KEY,
        observed_at TEXT NOT NULL,
        code TEXT NOT NULL,
        candidate_price REAL NOT NULL,
        price_basis TEXT NOT NULL DEFAULT '',
        exit_date TEXT NOT NULL DEFAULT '',
        feedback_policy_version INTEGER NOT NULL DEFAULT 0,
        eligible INTEGER NOT NULL DEFAULT 0,
        executed INTEGER NOT NULL DEFAULT 0,
        reclaim_passed INTEGER NOT NULL DEFAULT 0,
        volume_supportive INTEGER NOT NULL DEFAULT 0,
        thesis_valid INTEGER NOT NULL DEFAULT 0,
        volume_ratio REAL,
        amount_percentile REAL,
        required_volume_ratio REAL,
        required_amount_percentile REAL,
        sessions_observed INTEGER NOT NULL DEFAULT 0,
        observation_date TEXT NOT NULL DEFAULT '',
        future_return_pct REAL,
        completed INTEGER NOT NULL DEFAULT 0,
        quality_status TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS exit_feedback_policies (
        version INTEGER PRIMARY KEY AUTOINCREMENT,
        algorithm_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        effective_date TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active',
        action TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        observation_count INTEGER NOT NULL DEFAULT 0,
        new_observation_count INTEGER NOT NULL DEFAULT 0,
        observation_span_months INTEGER NOT NULL DEFAULT 0,
        source_fingerprint TEXT NOT NULL UNIQUE,
        parameters_json TEXT NOT NULL,
        metrics_json TEXT NOT NULL DEFAULT '{}',
        baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
        previous_parameters_json TEXT NOT NULL DEFAULT '{}',
        previous_version INTEGER,
        rollback_of INTEGER
    );

    CREATE TABLE IF NOT EXISTS exit_feedback_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evaluated_at TEXT NOT NULL,
        algorithm_version TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        observation_count INTEGER NOT NULL DEFAULT 0,
        observation_span_months INTEGER NOT NULL DEFAULT 0,
        policy_version INTEGER NOT NULL DEFAULT 0,
        parameters_json TEXT NOT NULL DEFAULT '{}',
        metrics_json TEXT NOT NULL DEFAULT '{}'
    );
    
    CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time);
    CREATE INDEX IF NOT EXISTS idx_trades_code ON trades(code);
    CREATE INDEX IF NOT EXISTS idx_positions_date ON position_snapshots(date);
    CREATE INDEX IF NOT EXISTS idx_daily_equity_date ON daily_equity(date);
    CREATE INDEX IF NOT EXISTS idx_account_history_kind_time
        ON account_history(history_kind, event_time, id);
    CREATE INDEX IF NOT EXISTS idx_account_history_kind_logical
        ON account_history(history_kind, logical_key, id);
    CREATE INDEX IF NOT EXISTS idx_post_exit_code_time
        ON post_exit_observations(code, sell_time);
    CREATE INDEX IF NOT EXISTS idx_post_exit_horizon_completed
        ON post_exit_observations(horizon, completed);
    CREATE INDEX IF NOT EXISTS idx_post_exit_reentry_completed
        ON post_exit_reentry_observations(completed, observed_at);
    CREATE INDEX IF NOT EXISTS idx_exit_feedback_evaluations_time
        ON exit_feedback_evaluations(evaluated_at, id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_exit_feedback_active
        ON exit_feedback_policies(active) WHERE active=1;
    CREATE TRIGGER IF NOT EXISTS account_history_no_update
        BEFORE UPDATE ON account_history
        BEGIN
            SELECT RAISE(ABORT, 'account_history is append-only');
        END;
    CREATE TRIGGER IF NOT EXISTS account_history_no_delete
        BEFORE DELETE ON account_history
        BEGIN
            SELECT RAISE(ABORT, 'account_history is append-only');
        END;
    """)
    _ensure_trade_evidence_columns(conn)
    _ensure_decision_evidence_columns(conn)
    _ensure_daily_equity_evidence_columns(conn)
    _ensure_post_exit_observation_columns(conn)
    _deduplicate_trades(conn)
    _deduplicate_trade_idempotency_keys(conn)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique_event
        ON trades(time, action, code, shares, price, amount, reason)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_idempotency_key
        ON trades(idempotency_key)
        WHERE idempotency_key <> ''
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_unique_event
        ON decisions(event_key)
        WHERE event_key IS NOT NULL AND event_key <> ''
    """)
    conn.commit()
    conn.close()


def _ensure_trade_evidence_columns(conn: sqlite3.Connection):
    """Add lossless payload and execution identities without rewriting facts."""
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(trades)").fetchall()
    }
    if "payload_json" not in columns:
        conn.execute(
            "ALTER TABLE trades ADD COLUMN payload_json TEXT NOT NULL DEFAULT ''"
        )
    if "position_lifecycle_id" not in columns:
        conn.execute(
            "ALTER TABLE trades ADD COLUMN "
            "position_lifecycle_id TEXT NOT NULL DEFAULT ''"
        )
    if "idempotency_key" not in columns:
        conn.execute(
            "ALTER TABLE trades ADD COLUMN "
            "idempotency_key TEXT NOT NULL DEFAULT ''"
        )


def _ensure_decision_evidence_columns(conn: sqlite3.Connection):
    """Add durable decision evidence without rewriting legacy rows."""
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(decisions)").fetchall()
    }
    additions = {
        "b1_generated_at": "TEXT DEFAULT ''",
        "schedule_slot": "TEXT DEFAULT ''",
        "schedule_run_kind": "TEXT DEFAULT ''",
        "event_key": "TEXT",
        "payload_json": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE decisions ADD COLUMN {name} {definition}"
            )


def _ensure_daily_equity_evidence_columns(conn: sqlite3.Connection):
    """Add account-session continuity without rewriting historical marks."""
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(daily_equity)").fetchall()
    }
    if "account_created_at" not in columns:
        conn.execute(
            "ALTER TABLE daily_equity ADD COLUMN account_created_at "
            "TEXT NOT NULL DEFAULT ''"
        )


def _ensure_post_exit_observation_columns(conn: sqlite3.Connection) -> None:
    """Extend derived observations without rewriting immutable trade facts."""
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(post_exit_observations)"
        ).fetchall()
    }
    for name, definition in {
        "sell_notional": "REAL NOT NULL DEFAULT 0",
        "price_basis": "TEXT NOT NULL DEFAULT ''",
        "replacement_counterfactual_return_pct": "REAL",
        "replacement_regret": "INTEGER",
        "replacement_executed": "INTEGER NOT NULL DEFAULT 0",
        "replacement_buy_time": "TEXT NOT NULL DEFAULT ''",
        "replacement_buy_price": "REAL",
        "replacement_buy_shares": "INTEGER NOT NULL DEFAULT 0",
        "replacement_buy_fee": "REAL NOT NULL DEFAULT 0",
        "feedback_policy_version": "INTEGER NOT NULL DEFAULT 0",
        "sell_fly_threshold_pct": "REAL",
    }.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE post_exit_observations ADD COLUMN {name} {definition}"
            )


def _deduplicate_trades(conn: sqlite3.Connection):
    """Keep one row per simulated trade event before enforcing uniqueness."""
    conn.execute("UPDATE trades SET reason = '' WHERE reason IS NULL")
    conn.execute("""
        DELETE FROM trades
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM trades
            GROUP BY time, action, code, shares, price, amount, reason
        )
    """)


def _deduplicate_trade_idempotency_keys(conn: sqlite3.Connection):
    """Keep historical facts while reserving each non-empty guard key once."""
    conn.execute("""
        UPDATE trades
        SET idempotency_key = ''
        WHERE idempotency_key <> ''
          AND id NOT IN (
              SELECT MIN(id)
              FROM trades
              WHERE idempotency_key <> ''
              GROUP BY idempotency_key
          )
    """)


def _archive_account_history_conn(
    conn: sqlite3.Connection,
    state: Mapping[str, Any],
) -> int:
    """Append lossless history payloads inside the caller's transaction."""
    archived_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: list[tuple[str, str, str, str, str, str]] = []
    for kind in ACCOUNT_HISTORY_KINDS:
        values = state.get(kind)
        if not isinstance(values, list):
            continue
        for value in values:
            payload_json = _canonical_payload(value)
            event_key = _decision_event_key(payload_json)
            rows.append((
                kind,
                event_key,
                _history_logical_key(kind, value, payload_json),
                _history_event_time(value),
                payload_json,
                archived_at,
            ))
    if rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO account_history (
                history_kind, event_key, logical_key, event_time,
                payload_json, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def archive_account_history(state: Mapping[str, Any]) -> bool:
    """Atomically append all JSON account history before the file is compacted."""
    conn = None
    try:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        _archive_account_history_conn(conn, state)
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except sqlite3.Error:
                pass
        print(
            "[niuniu_db] 归档账户历史失败: "
            f"{type(exc).__name__}",
        )
        return False


def archive_state_file_history() -> bool:
    """Idempotently seed the immutable archive from a legacy full-state JSON."""
    if not STATE_FILE.exists():
        return True
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            "[niuniu_db] 读取账户历史 JSON 失败: "
            f"{type(exc).__name__}",
        )
        return False
    if not isinstance(state, Mapping):
        return False
    return archive_account_history(state)


def query_account_history(kind: str, limit: int | None = None) -> list[Any]:
    """Read active history rows while keeping every older revision in SQLite."""
    if kind not in ACCOUNT_HISTORY_KINDS:
        raise ValueError(f"unsupported account history kind: {kind}")
    conn = None
    try:
        conn = _connect()
        params: list[Any] = [kind]
        active_rows_sql = """
            SELECT h.id, h.event_time, h.payload_json
            FROM account_history AS h
            WHERE h.history_kind = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM account_history AS newer
                  WHERE newer.history_kind = h.history_kind
                    AND newer.logical_key = h.logical_key
                    AND newer.id > h.id
              )
        """
        if limit is not None:
            resolved_limit = max(0, int(limit))
            if resolved_limit == 0:
                conn.close()
                return []
            query = f"""
                SELECT payload_json
                FROM ({active_rows_sql}
                      ORDER BY h.event_time DESC, h.id DESC
                      LIMIT ?)
                ORDER BY event_time, id
            """
            params.append(resolved_limit)
        else:
            query = f"""
                SELECT payload_json
                FROM ({active_rows_sql})
                ORDER BY event_time, id
            """
        raw_rows = conn.execute(query, params).fetchall()
        conn.close()
        restored: list[Any] = []
        invalid_count = 0
        for (payload_json,) in raw_rows:
            try:
                restored.append(json.loads(payload_json))
            except (TypeError, json.JSONDecodeError):
                invalid_count += 1
        if invalid_count:
            print(
                "[niuniu_db] 跳过无法解析的账户历史记录: "
                f"{invalid_count} 条"
            )
        return restored
    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        print(
            "[niuniu_db] 查询账户历史失败: "
            f"{type(exc).__name__}",
        )
        return []


def migrate_from_json():
    """从 niuniu_practice_portfolio.json 迁移历史数据到 SQLite。"""
    json_path = STATE_FILE
    if not json_path.exists():
        return
    
    conn = _connect()
    try:
        state = json.loads(json_path.read_text(encoding="utf-8"))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Archive the source payloads first. The structured summary tables below
        # remain useful for existing reports, while account_history is the
        # lossless, append-only source used before compacting the JSON state.
        _archive_account_history_conn(conn, state)
        
        # 1. 迁移每日资金快照
        daily_history = state.get("daily_equity_history", [])
        if daily_history:
            migrated = 0
            for pt in daily_history:
                date = pt.get("time", "")[:10]
                if not date:
                    continue
                conn.execute("""
                    INSERT OR IGNORE INTO daily_equity (date, equity, cash, market_value, pnl_pct, account_created_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (date, pt.get("equity", 0), pt.get("cash", 0), pt.get("market_value", 0), pt.get("pnl_pct", 0), state.get("created_at", ""), pt.get("time", now)))
                migrated += 1
            print(f"[niuniu_db] 迁移 daily_equity: {migrated} 条")
        
        # 2. 迁移交易日志
        trade_log = state.get("trade_log", [])
        if trade_log:
            migrated = 0
            for t in trade_log:
                action = t.get("action", "")
                if not action:
                    continue
                conn.execute("""
                    INSERT OR IGNORE INTO trades (
                        time, action, code, name, shares, price, amount,
                        commission, transfer_fee, stamp_duty, pnl, reason,
                        position_lifecycle_id, idempotency_key,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    t.get("time", now), action, t.get("code", ""), t.get("name", ""),
                    t.get("shares", 0), t.get("price", 0), t.get("amount", 0),
                    t.get("commission", 0), t.get("transfer_fee", 0), t.get("stamp_duty", 0),
                    t.get("pnl"), t.get("reason", ""),
                    t.get("position_lifecycle_id", ""),
                    t.get("idempotency_key", ""),
                    json.dumps(t, ensure_ascii=False, sort_keys=True), t.get("time", now)
                ))
                migrated += 1
            print(f"[niuniu_db] 迁移 trades: {migrated} 条")
        
        # 3. 迁移决策日志
        decision_log = state.get("decision_log", [])
        if decision_log:
            migrated = 0
            for d in decision_log:
                dec = d.get("decision", {})
                payload_json = _canonical_payload(d)
                conn.execute("""
                    INSERT OR IGNORE INTO decisions (
                        time, model, provider, trade_allowed, trade_reason,
                        summary, actions_json, error, b1_generated_at,
                        schedule_slot, schedule_run_kind, event_key,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    d.get("time", now), dec.get("model", ""), dec.get("provider", ""),
                    int(d.get("trade_allowed", True)), d.get("trade_reason", ""),
                    dec.get("summary", ""), json.dumps(dec.get("actions", []), ensure_ascii=False),
                    dec.get("error", ""), d.get("b1_generated_at", ""),
                    d.get("schedule_slot", ""), d.get("schedule_run_kind", ""),
                    _decision_event_key(payload_json), payload_json,
                    d.get("time", now),
                ))
                migrated += 1
            print(f"[niuniu_db] 迁移 decisions: {migrated} 条")
        
        # 4. 当前持仓快照
        positions = state.get("positions", {})
        if positions:
            today = datetime.now().strftime("%Y-%m-%d")
            migrated = 0
            for code, p in positions.items():
                qty = int(p.get("qty") or p.get("shares") or 0)
                avg_cost = float(p.get("avg_cost", 0))
                last_price = float(p.get("last_price", avg_cost))
                mv = last_price * qty
                pnl = (last_price - avg_cost) * qty
                pnl_pct_val = ((last_price / avg_cost - 1) * 100) if avg_cost > 0 else 0
                conn.execute("""
                    INSERT OR IGNORE INTO position_snapshots (date, code, name, shares, avg_cost, last_price, market_value, pnl, pnl_pct, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (today, code, p.get("name", ""), qty, avg_cost, last_price, mv, pnl, pnl_pct_val, now))
                migrated += 1
            print(f"[niuniu_db] 迁移 positions: {migrated} 条")
        
        conn.commit()
        print("[niuniu_db] 迁移完成")
    except Exception as e:
        conn.rollback()
        print(f"[niuniu_db] 迁移失败: {e}")
    finally:
        conn.close()


def record_daily_equity(pt: dict):
    """记录每日资金快照到 DB。pt 包含 time, equity, cash, market_value, pnl_pct。"""
    try:
        conn = _connect()
        date = pt.get("time", "")[:10]
        if not _is_trading_day_text(date):
            conn.close()
            return
        conn.execute("""
            INSERT OR REPLACE INTO daily_equity (date, equity, cash, market_value, pnl_pct, account_created_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (date, pt.get("equity", 0), pt.get("cash", 0), pt.get("market_value", 0), pt.get("pnl_pct", 0), pt.get("account_created_at", ""), pt.get("time", "")))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[niuniu_db] 写入 daily_equity 失败: {e}")


def record_trade(t: dict) -> bool:
    """记录单笔交易到 DB。"""
    conn = None
    try:
        conn = _connect()
        payload_json = _canonical_payload(t)
        conn.execute("""
            INSERT OR IGNORE INTO trades (
                time, action, code, name, shares, price, amount,
                commission, transfer_fee, stamp_duty, pnl, reason,
                position_lifecycle_id, idempotency_key,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            t.get("time", ""), t.get("action", ""), t.get("code", ""), t.get("name", ""),
            t.get("shares", 0), t.get("price", 0), t.get("amount", 0),
            t.get("commission", 0), t.get("transfer_fee", 0), t.get("stamp_duty", 0),
            t.get("pnl"), t.get("reason", ""),
            t.get("position_lifecycle_id", ""),
            t.get("idempotency_key", ""),
            payload_json, t.get("time", "")
        ))
        conn.execute("""
            UPDATE trades
            SET payload_json = CASE
                    WHEN payload_json = '' THEN ? ELSE payload_json END,
                position_lifecycle_id = CASE
                    WHEN position_lifecycle_id = '' THEN ?
                    ELSE position_lifecycle_id END,
                idempotency_key = CASE
                    WHEN idempotency_key = '' THEN ? ELSE idempotency_key END
            WHERE time = ? AND action = ? AND code = ? AND shares = ?
              AND price = ? AND amount = ? AND reason = ?
        """, (
            payload_json,
            t.get("position_lifecycle_id", ""),
            t.get("idempotency_key", ""),
            t.get("time", ""), t.get("action", ""), t.get("code", ""),
            t.get("shares", 0), t.get("price", 0), t.get("amount", 0),
            t.get("reason", ""),
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except sqlite3.Error:
                pass
        print(f"[niuniu_db] 写入 trade 失败: {type(e).__name__}")
        return False


def query_trade_idempotency_keys() -> set[str]:
    """Return active durable execution keys used to reject duplicate fills."""
    conn = None
    try:
        conn = _connect()
        structured_rows = conn.execute(
            "SELECT idempotency_key FROM trades WHERE idempotency_key <> ''"
        ).fetchall()
        history_rows = conn.execute("""
            SELECT h.payload_json
            FROM account_history AS h
            WHERE h.history_kind = 'trade_log'
              AND NOT EXISTS (
                  SELECT 1
                  FROM account_history AS newer
                  WHERE newer.history_kind = h.history_kind
                    AND newer.logical_key = h.logical_key
                    AND newer.id > h.id
              )
        """).fetchall()
        conn.close()
        structured_keys = {
            str(row[0])
            for row in structured_rows
            if str(row[0] or "")
        }
        history_keys: set[str] = set()
        inactive_history_keys: set[str] = set()
        inactive_statuses = {"cancelled", "rejected", "reversed", "voided"}
        for (payload_json,) in history_rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            key = str(payload.get("idempotency_key") or "")
            if not key:
                continue
            status = str(payload.get("accounting_status") or "").strip().lower()
            if (
                payload.get("accounting_rejected") is True
                or payload.get("voided") is True
                or status in inactive_statuses
            ):
                inactive_history_keys.add(key)
            else:
                history_keys.add(key)
        return history_keys | (structured_keys - inactive_history_keys)
    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        print(
            "[niuniu_db] 查询成交幂等键失败: "
            f"{type(exc).__name__}",
        )
        return set()


def query_latest_position_lifecycle_ids(codes: list[str]) -> dict[str, str]:
    """Return the latest durable position cycle for the requested securities."""
    normalized_codes = sorted({
        str(code or "").strip()
        for code in codes
        if str(code or "").strip()
    })
    if not normalized_codes:
        return {}
    conn = None
    try:
        conn = _connect()
        placeholders = ",".join("?" for _ in normalized_codes)
        structured_rows = conn.execute(
            f"""
            SELECT time, id, code, position_lifecycle_id
            FROM trades
            WHERE code IN ({placeholders})
              AND position_lifecycle_id <> ''
            """,
            normalized_codes,
        ).fetchall()
        history_rows = conn.execute("""
            SELECT h.event_time, h.id, h.payload_json
            FROM account_history AS h
            WHERE h.history_kind = 'trade_log'
              AND NOT EXISTS (
                  SELECT 1
                  FROM account_history AS newer
                  WHERE newer.history_kind = h.history_kind
                    AND newer.logical_key = h.logical_key
                    AND newer.id > h.id
              )
        """).fetchall()
        conn.close()
        events = [
            (
                str(event_time or ""),
                int(row_id),
                str(code or ""),
                str(lifecycle_id or ""),
            )
            for event_time, row_id, code, lifecycle_id in structured_rows
            if str(code or "") and str(lifecycle_id or "")
        ]
        requested = set(normalized_codes)
        for event_time, row_id, payload_json in history_rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            code = str(payload.get("code") or "")
            lifecycle_id = str(payload.get("position_lifecycle_id") or "")
            if code in requested and lifecycle_id:
                events.append((
                    str(event_time or payload.get("time") or ""),
                    int(row_id),
                    code,
                    lifecycle_id,
                ))
        events.sort(key=lambda item: (item[0], item[1]))
        return {
            code: lifecycle_id
            for _event_time, _row_id, code, lifecycle_id in events
        }
    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        print(
            "[niuniu_db] 查询持仓周期失败: "
            f"{type(exc).__name__}",
        )
        return {}


def _is_nearby_replacement_buy(
    buy_time: str,
    sell_time: str,
    *,
    maximum_minutes: int = 30,
) -> bool:
    if not buy_time or not sell_time or buy_time[:10] != sell_time[:10]:
        return False
    try:
        elapsed = datetime.fromisoformat(buy_time) - datetime.fromisoformat(sell_time)
    except ValueError:
        return buy_time >= sell_time
    return 0 <= elapsed.total_seconds() <= max(1, maximum_minutes) * 60


def query_post_exit_sell_trades(limit: int = 2000) -> list[dict[str, Any]]:
    """Return lossless SELL payloads used to build derived exit observations."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT time, code, name, shares, price, amount, pnl, reason, "
            "payload_json FROM trades WHERE action='SELL' "
            "ORDER BY time DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        earliest_sell_time = min(
            (str(row[0] or "") for row in rows),
            default="",
        )
        buy_rows = (
            conn.execute(
                "SELECT time, code, shares, price, commission, transfer_fee, "
                "stamp_duty, payload_json FROM trades WHERE action='BUY' "
                "AND time >= ? ORDER BY time, id",
                (earliest_sell_time,),
            ).fetchall()
            if earliest_sell_time
            else []
        )
    finally:
        conn.close()
    replacement_buys: list[dict[str, Any]] = []
    for row in buy_rows:
        payload: dict[str, Any] = {}
        try:
            decoded = json.loads(str(row[7] or "{}"))
            if isinstance(decoded, dict):
                payload = decoded
        except (TypeError, ValueError):
            payload = {}
        replacement_buys.append({
            "time": str(row[0] or ""),
            "code": str(row[1] or ""),
            "shares": int(row[2] or 0),
            "price": float(row[3] or 0),
            "fee": float(row[4] or 0) + float(row[5] or 0) + float(row[6] or 0),
            **payload,
        })
    result: list[dict[str, Any]] = []
    for row in rows:
        payload: dict[str, Any] = {}
        try:
            decoded = json.loads(str(row[8] or "{}"))
            if isinstance(decoded, dict):
                payload = decoded
        except (TypeError, ValueError):
            payload = {}
        trade = {
            "time": row[0],
            "action": "SELL",
            "code": row[1],
            "name": row[2],
            "shares": row[3],
            "price": row[4],
            "amount": row[5],
            "pnl": row[6],
            "reason": row[7],
            **payload,
        }
        sell_code = "".join(character for character in str(trade.get("code") or "") if character.isdigit())[-6:]
        target_code = "".join(
            character
            for character in str(trade.get("replacement_target_code") or "")
            if character.isdigit()
        )[-6:]
        sell_time = str(trade.get("time") or "")
        matched = next((
            buy
            for buy in replacement_buys
            if target_code
            and "".join(character for character in str(buy.get("code") or "") if character.isdigit())[-6:] == target_code
            and "".join(character for character in str(buy.get("replacement_source_code") or "") if character.isdigit())[-6:] == sell_code
            and _is_nearby_replacement_buy(str(buy.get("time") or ""), sell_time)
        ), None)
        if matched is not None:
            trade.update({
                "replacement_execution_time": str(matched.get("time") or ""),
                "replacement_execution_price": float(matched.get("price") or 0),
                "replacement_execution_shares": int(matched.get("shares") or 0),
                "replacement_execution_fee": float(matched.get("fee") or 0),
            })
        result.append(trade)
    return result


def query_post_exit_reentry_audits(limit: int = 5000) -> list[dict[str, Any]]:
    """Extract direct allowed and blocked re-entry audits from durable decisions."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT time, payload_json FROM decisions "
            "WHERE payload_json <> '' ORDER BY time DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()
    audits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for decision_time, payload_json in rows:
        try:
            payload = json.loads(str(payload_json or "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        decision = payload.get("decision")
        actions = decision.get("actions") if isinstance(decision, Mapping) else []
        executed = payload.get("executed")
        executed_codes = {
            "".join(character for character in str(item.get("code") or "") if character.isdigit())[-6:]
            for item in (executed or [])
            if isinstance(item, Mapping)
            and str(item.get("action") or "").upper() == "BUY"
            and isinstance(item.get("post_exit_reentry_audit"), Mapping)
        }
        for action in actions or []:
            if not isinstance(action, Mapping):
                continue
            audit = action.get("post_exit_reentry_audit")
            if not isinstance(audit, Mapping):
                continue
            code = "".join(
                character for character in str(action.get("code") or "")
                if character.isdigit()
            )[-6:]
            observed_at = str(decision_time or payload.get("time") or "")
            identity = {
                "observed_at": observed_at,
                "code": code,
                "exit_date": str(audit.get("exit_date") or ""),
                "price": audit.get("execution_price"),
                "policy_version": audit.get("exit_feedback_policy_version"),
            }
            audit_key = hashlib.sha256(
                _canonical_payload(identity).encode("utf-8")
            ).hexdigest()
            if not code or audit_key in seen:
                continue
            seen.add(audit_key)
            audits.append({
                "audit_key": audit_key,
                "observed_at": observed_at,
                "code": code,
                "candidate_price": float(audit.get("execution_price") or 0),
                "exit_date": str(audit.get("exit_date") or ""),
                "feedback_policy_version": int(
                    audit.get("exit_feedback_policy_version") or 0
                ),
                "eligible": int(bool(audit.get("eligible"))),
                "executed": int(code in executed_codes),
                "reclaim_passed": int(bool(audit.get("reclaim_passed"))),
                "volume_supportive": int(bool(audit.get("volume_supportive"))),
                "thesis_valid": int(bool(audit.get("thesis_valid"))),
                "volume_ratio": audit.get("volume_ratio"),
                "amount_percentile": audit.get("amount_percentile"),
                "required_volume_ratio": audit.get("required_volume_ratio"),
                "required_amount_percentile": audit.get(
                    "required_amount_percentile"
                ),
            })
    return audits


def record_probe_chase_outcomes(rows: list[dict[str, Any]]) -> int:
    """Only append mature shadow outcomes; retry or missing data cannot rewrite them."""
    values = [
        (row["observation_key"], row["protocol_version"],
         json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        for row in rows if row.get("completed") is True
    ]
    if not values:
        return 0
    conn = _connect()
    try:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO probe_chase_outcomes "
            "(observation_key,protocol_version,payload_json,recorded_at) VALUES (?,?,?,?)",
            values,
        )
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


def upsert_post_exit_observations(rows: list[dict[str, Any]]) -> int:
    """Idempotently refresh derived observations without changing trade facts."""
    if not rows:
        return 0
    columns = (
        "trade_key", "horizon", "sell_time", "code", "sell_price",
        "sell_notional", "price_basis", "shares",
        "full_exit", "exit_rule", "exit_signal", "buy_strategy",
        "replacement_target_code", "sessions_observed", "observation_date",
        "close_return_pct", "mfe_pct", "mae_pct", "benchmark_return_pct",
        "excess_return_pct", "replacement_return_pct",
        "replacement_counterfactual_return_pct", "replacement_regret_pct",
        "replacement_regret", "replacement_executed", "replacement_buy_time",
        "replacement_buy_price", "replacement_buy_shares", "replacement_buy_fee",
        "feedback_policy_version", "sell_fly_threshold_pct", "sell_fly",
        "avoided_loss", "completed",
        "quality_status", "updated_at",
    )
    placeholders = ",".join("?" for _ in columns)
    immutable = {
        "trade_key", "horizon", "sell_time", "code", "sell_price",
        "sell_notional", "shares", "full_exit", "exit_rule", "exit_signal",
        "buy_strategy", "replacement_target_code", "feedback_policy_version",
    }
    derived = [column for column in columns if column not in immutable]
    advances = (
        "excluded.completed > post_exit_observations.completed OR "
        "(post_exit_observations.completed=0 AND "
        "excluded.sessions_observed >= post_exit_observations.sessions_observed) OR "
        "(post_exit_observations.completed=1 AND "
        "post_exit_observations.price_basis='' AND excluded.completed=1)"
    )
    update_parts = [
        f"{column}=CASE WHEN {advances} THEN excluded.{column} "
        f"ELSE post_exit_observations.{column} END"
        for column in derived
    ]
    update_parts.extend(
        f"{column}=CASE WHEN post_exit_observations.{column} IN ('', 0) "
        f"THEN excluded.{column} ELSE post_exit_observations.{column} END"
        for column in columns
        if column in immutable and column not in {"trade_key", "horizon"}
    )
    update_clause = ",".join(update_parts)

    def resolved_value(row: Mapping[str, Any], column: str) -> Any:
        if column == "sell_notional":
            return row.get(column) or (
                float(row.get("sell_price") or 0)
                * int(row.get("shares") or 0)
            )
        if column in {"price_basis", "replacement_buy_time"}:
            return row.get(column) or ""
        if column in {
            "feedback_policy_version", "replacement_executed",
            "replacement_buy_shares", "replacement_buy_fee",
        }:
            return row.get(column) or 0
        return row.get(column)

    values = [
        tuple(resolved_value(row, column) for column in columns)
        for row in rows
    ]
    conn = _connect()
    try:
        conn.executemany(
            f"INSERT INTO post_exit_observations ({','.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT(trade_key, horizon) "
            f"DO UPDATE SET {update_clause}",
            values,
        )
        conn.commit()
    finally:
        conn.close()
    return len(values)


def upsert_post_exit_reentry_observations(rows: list[dict[str, Any]]) -> int:
    """Advance direct re-entry audits without downgrading mature outcomes."""
    if not rows:
        return 0
    columns = (
        "audit_key", "observed_at", "code", "candidate_price", "price_basis", "exit_date",
        "feedback_policy_version", "eligible", "executed", "reclaim_passed",
        "volume_supportive", "thesis_valid", "volume_ratio", "amount_percentile",
        "required_volume_ratio", "required_amount_percentile", "sessions_observed",
        "observation_date", "future_return_pct", "completed", "quality_status",
        "updated_at",
    )
    placeholders = ",".join("?" for _ in columns)
    immutable = set(columns[:16])
    advances = (
        "excluded.completed > post_exit_reentry_observations.completed OR "
        "(post_exit_reentry_observations.completed=0 AND "
        "excluded.sessions_observed >= post_exit_reentry_observations.sessions_observed)"
    )
    update_clause = ",".join(
        [
            f"{column}=CASE WHEN {advances} THEN excluded.{column} "
            f"ELSE post_exit_reentry_observations.{column} END"
            for column in columns
            if column not in immutable and column != "audit_key"
        ]
        + [
            f"{column}=post_exit_reentry_observations.{column}"
            for column in immutable
            if column != "audit_key"
        ]
    )
    values = [tuple(row.get(column) for column in columns) for row in rows]
    conn = _connect()
    try:
        conn.executemany(
            f"INSERT INTO post_exit_reentry_observations ({','.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT(audit_key) "
            f"DO UPDATE SET {update_clause}",
            values,
        )
        conn.commit()
    finally:
        conn.close()
    return len(values)


def query_exit_feedback_tuning_observations() -> list[dict[str, Any]]:
    """Return completed five-session aggregates used by the local tuner."""
    conn = _connect()
    try:
        columns = (
            "trade_key", "sell_time", "code", "sell_price", "sell_notional", "price_basis",
            "shares", "full_exit", "exit_rule", "exit_signal", "buy_strategy",
            "replacement_target_code", "replacement_executed", "close_return_pct",
            "mae_pct", "replacement_regret_pct", "replacement_regret",
            "sell_fly", "avoided_loss", "completed",
            "feedback_policy_version",
        )
        rows = conn.execute(
            f"SELECT {','.join(columns)} FROM post_exit_observations "
            "WHERE horizon=5 AND completed=1 ORDER BY sell_time, trade_key"
        ).fetchall()
    finally:
        conn.close()
    return [dict(zip(columns, row)) for row in rows]


def query_exit_feedback_reentry_observations() -> list[dict[str, Any]]:
    """Return completed direct re-entry shadow outcomes for bounded tuning."""
    conn = _connect()
    try:
        columns = (
            "audit_key", "observed_at", "code", "candidate_price", "price_basis", "exit_date",
            "feedback_policy_version", "eligible", "executed", "reclaim_passed",
            "volume_supportive", "thesis_valid", "volume_ratio", "amount_percentile",
            "required_volume_ratio", "required_amount_percentile", "future_return_pct",
            "completed",
        )
        rows = conn.execute(
            f"SELECT {','.join(columns)} FROM post_exit_reentry_observations "
            "WHERE completed=1 ORDER BY observed_at, audit_key"
        ).fetchall()
    finally:
        conn.close()
    return [dict(zip(columns, row)) for row in rows]


def _decode_exit_feedback_policy(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    columns = (
        "version", "algorithm_version", "created_at", "effective_date",
        "active", "status", "action", "reason", "observation_count",
        "new_observation_count", "observation_span_months",
        "source_fingerprint", "parameters_json", "metrics_json",
        "baseline_metrics_json", "previous_parameters_json",
        "previous_version", "rollback_of",
    )
    payload = dict(zip(columns, row))
    for source, target in (
        ("parameters_json", "parameters"),
        ("metrics_json", "metrics"),
        ("baseline_metrics_json", "baseline_metrics"),
        ("previous_parameters_json", "previous_parameters"),
    ):
        try:
            decoded = json.loads(str(payload.pop(source) or "{}"))
        except (TypeError, ValueError):
            decoded = {}
        payload[target] = decoded if isinstance(decoded, dict) else {}
    payload["active"] = bool(payload.get("active"))
    return payload


def query_active_exit_feedback_policy() -> dict[str, Any] | None:
    """Read the one active version without exposing individual trade rows."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT version,algorithm_version,created_at,effective_date,active,"
            "status,action,reason,observation_count,new_observation_count,"
            "observation_span_months,source_fingerprint,parameters_json,"
            "metrics_json,baseline_metrics_json,previous_parameters_json,"
            "previous_version,rollback_of FROM exit_feedback_policies "
            "WHERE active=1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return _decode_exit_feedback_policy(row)


def query_latest_exit_feedback_evaluation() -> dict[str, Any] | None:
    """Read the latest evaluator checkpoint used by the sample cooldown."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT evaluated_at,algorithm_version,source_fingerprint,status,"
            "action,reason,observation_count,observation_span_months,policy_version,"
            "parameters_json,metrics_json FROM exit_feedback_evaluations "
            "ORDER BY evaluated_at DESC,id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    columns = (
        "evaluated_at", "algorithm_version", "source_fingerprint", "status",
        "action", "reason", "observation_count", "observation_span_months",
        "policy_version", "parameters_json", "metrics_json",
    )
    payload = dict(zip(columns, row))
    for source, target in (("parameters_json", "parameters"), ("metrics_json", "metrics")):
        try:
            decoded = json.loads(str(payload.pop(source) or "{}"))
        except (TypeError, ValueError):
            decoded = {}
        payload[target] = decoded if isinstance(decoded, dict) else {}
    return payload


def record_exit_feedback_evaluation(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    """Append one idempotent evaluation checkpoint without creating a policy."""
    fingerprint = str(evaluation.get("source_fingerprint") or "").strip()
    if not fingerprint:
        raise ValueError("exit feedback evaluation requires source_fingerprint")
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO exit_feedback_evaluations ("
            "evaluated_at,algorithm_version,source_fingerprint,status,action,reason,"
            "observation_count,observation_span_months,policy_version,parameters_json,"
            "metrics_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(evaluation.get("evaluated_at") or ""),
                str(evaluation.get("algorithm_version") or ""),
                fingerprint,
                str(evaluation.get("status") or ""),
                str(evaluation.get("action") or ""),
                str(evaluation.get("reason") or ""),
                int(evaluation.get("observation_count") or 0),
                int(evaluation.get("observation_span_months") or 0),
                int(evaluation.get("policy_version") or 0),
                _canonical_payload(evaluation.get("parameters") or {}),
                _canonical_payload(evaluation.get("metrics") or {}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    latest = query_latest_exit_feedback_evaluation()
    if latest is None:  # pragma: no cover - insert or prior row must exist
        raise RuntimeError("failed to persist exit feedback evaluation")
    return latest


def record_exit_feedback_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically activate one idempotent, immutable feedback-policy version."""
    fingerprint = str(policy.get("source_fingerprint") or "").strip()
    if not fingerprint:
        raise ValueError("exit feedback policy requires source_fingerprint")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT version,algorithm_version,created_at,effective_date,active,"
            "status,action,reason,observation_count,new_observation_count,"
            "observation_span_months,source_fingerprint,parameters_json,"
            "metrics_json,baseline_metrics_json,previous_parameters_json,"
            "previous_version,rollback_of FROM exit_feedback_policies "
            "WHERE source_fingerprint=? LIMIT 1",
            (fingerprint,),
        ).fetchone()
        if existing is not None:
            decoded = _decode_exit_feedback_policy(existing)
            if decoded is None:  # pragma: no cover - row was just fetched
                raise RuntimeError("failed to decode exit feedback policy")
            if not decoded.get("active"):
                raise RuntimeError(
                    "exit feedback fingerprint belongs to an inactive policy"
                )
            conn.commit()
            return decoded
        active = conn.execute(
            "SELECT version FROM exit_feedback_policies WHERE active=1 LIMIT 1"
        ).fetchone()
        previous_version = (
            int(policy.get("previous_version") or 0)
            or (int(active[0]) if active is not None else None)
        )
        conn.execute("UPDATE exit_feedback_policies SET active=0 WHERE active=1")
        cursor = conn.execute(
            "INSERT INTO exit_feedback_policies ("
            "algorithm_version,created_at,effective_date,active,status,action,"
            "reason,observation_count,new_observation_count,"
            "observation_span_months,source_fingerprint,parameters_json,"
            "metrics_json,baseline_metrics_json,previous_parameters_json,"
            "previous_version,rollback_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(policy.get("algorithm_version") or ""),
                str(policy.get("created_at") or ""),
                str(policy.get("effective_date") or ""),
                1,
                str(policy.get("status") or "active"),
                str(policy.get("action") or ""),
                str(policy.get("reason") or ""),
                int(policy.get("observation_count") or 0),
                int(policy.get("new_observation_count") or 0),
                int(policy.get("observation_span_months") or 0),
                fingerprint,
                _canonical_payload(policy.get("parameters") or {}),
                _canonical_payload(policy.get("metrics") or {}),
                _canonical_payload(policy.get("baseline_metrics") or {}),
                _canonical_payload(policy.get("previous_parameters") or {}),
                previous_version,
                int(policy.get("rollback_of") or 0) or None,
            ),
        )
        version = int(cursor.lastrowid)
        row = conn.execute(
            "SELECT version,algorithm_version,created_at,effective_date,active,"
            "status,action,reason,observation_count,new_observation_count,"
            "observation_span_months,source_fingerprint,parameters_json,"
            "metrics_json,baseline_metrics_json,previous_parameters_json,"
            "previous_version,rollback_of FROM exit_feedback_policies "
            "WHERE version=?",
            (version,),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    decoded = _decode_exit_feedback_policy(row)
    if decoded is None:  # pragma: no cover - inserted row must exist
        raise RuntimeError("failed to persist exit feedback policy")
    return decoded


def query_post_exit_observation_summary() -> dict[str, Any]:
    """Return aggregate labels only; individual private trades stay in SQLite."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT count(*), sum(CASE WHEN sell_fly=1 THEN 1 ELSE 0 END), "
            "sum(CASE WHEN avoided_loss=1 THEN 1 ELSE 0 END), "
            "sum(CASE WHEN replacement_regret=1 THEN 1 ELSE 0 END), "
            "avg(close_return_pct), avg(mfe_pct), avg(mae_pct), "
            "avg(replacement_regret_pct), max(updated_at) "
            "FROM post_exit_observations WHERE horizon=5 AND completed=1"
        ).fetchone()
        reentry_row = conn.execute(
            "SELECT count(*),sum(CASE WHEN eligible=0 AND reclaim_passed=1 "
            "AND thesis_valid=1 AND volume_supportive=0 THEN 1 ELSE 0 END),"
            "sum(CASE WHEN eligible=1 THEN 1 ELSE 0 END),avg(future_return_pct) "
            "FROM post_exit_reentry_observations WHERE completed=1"
        ).fetchone()
    finally:
        conn.close()
    return {
        "completed_5d_count": int(row[0] or 0),
        "sell_fly_5d_count": int(row[1] or 0),
        "avoided_loss_5d_count": int(row[2] or 0),
        "replacement_regret_5d_count": int(row[3] or 0),
        "avg_close_return_5d_pct": round(float(row[4]), 4) if row[4] is not None else None,
        "avg_mfe_5d_pct": round(float(row[5]), 4) if row[5] is not None else None,
        "avg_mae_5d_pct": round(float(row[6]), 4) if row[6] is not None else None,
        "avg_replacement_regret_5d_pct": round(float(row[7]), 4) if row[7] is not None else None,
        "reentry_completed_5d_count": int(reentry_row[0] or 0),
        "reentry_blocked_volume_5d_count": int(reentry_row[1] or 0),
        "reentry_allowed_5d_count": int(reentry_row[2] or 0),
        "avg_reentry_return_5d_pct": (
            round(float(reentry_row[3]), 4)
            if reentry_row[3] is not None
            else None
        ),
        "updated_at": str(row[8] or ""),
    }


def record_decision(d: dict) -> bool:
    """记录单条决策到 DB。"""
    conn = None
    try:
        conn = _connect()
        dec = d.get("decision", {})
        payload_json = _canonical_payload(d)
        event_key = _decision_event_key(payload_json)
        conn.execute("""
            INSERT OR IGNORE INTO decisions (
                time, model, provider, trade_allowed, trade_reason,
                summary, actions_json, error, b1_generated_at,
                schedule_slot, schedule_run_kind, event_key,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            d.get("time", ""), dec.get("model", ""), dec.get("provider", ""),
            int(d.get("trade_allowed", True)), d.get("trade_reason", ""),
            dec.get("summary", ""), json.dumps(dec.get("actions", []), ensure_ascii=False),
            dec.get("error", ""), d.get("b1_generated_at", ""),
            d.get("schedule_slot", ""), d.get("schedule_run_kind", ""),
            event_key, payload_json, d.get("time", ""),
        ))
        conn.commit()
        persisted = conn.execute(
            "SELECT 1 FROM decisions WHERE event_key = ? LIMIT 1",
            (event_key,),
        ).fetchone()
        conn.close()
        return persisted is not None
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except sqlite3.Error:
                pass
        print(f"[niuniu_db] 写入 decision 失败: {type(e).__name__}")
        return False


def snapshot_positions(positions: dict):
    """保存当前持仓快照到 DB。"""
    try:
        conn = _connect()
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("DELETE FROM position_snapshots WHERE date = ?", (today,))
        for code, p in positions.items():
            qty = int(p.get("qty") or p.get("shares") or 0)
            if qty <= 0:
                continue
            avg_cost = float(p.get("avg_cost", 0))
            last_price = float(p.get("last_price", avg_cost))
            mv = last_price * qty
            pnl = (last_price - avg_cost) * qty
            pnl_pct_val = ((last_price / avg_cost - 1) * 100) if avg_cost > 0 else 0
            conn.execute("""
                INSERT OR REPLACE INTO position_snapshots (date, code, name, shares, avg_cost, last_price, market_value, pnl, pnl_pct, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (today, code, p.get("name", ""), qty, avg_cost, last_price, mv, pnl, pnl_pct_val, now))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[niuniu_db] 快照持仓失败: {e}")


def query_daily_equity() -> list[dict]:
    """查询每日资金快照，用于累计收益曲线。"""
    try:
        conn = _connect()
        cur = conn.execute("SELECT date, equity, cash, market_value, pnl_pct, account_created_at, created_at FROM daily_equity ORDER BY date")
        rows = [{"time": r[6] or (r[0] + " 15:00:00"), "date": r[0], "equity": r[1], "cash": r[2], "market_value": r[3], "pnl_pct": r[4], "account_created_at": r[5]} for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[niuniu_db] 查询 daily_equity 失败: {e}")
        return []


def has_daily_equity_table() -> bool:
    conn = _connect()
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_equity'").fetchone()
        return bool(row)
    finally:
        conn.close()


# ======== 自动初始化 ========
if not DB_PATH.exists() or DB_PATH.stat().st_size < 1024:
    init_db()
    migrate_from_json()
    archive_state_file_history()
elif not has_daily_equity_table():
    init_db()
    migrate_from_json()
    archive_state_file_history()
else:
    init_db()
    archive_state_file_history()
