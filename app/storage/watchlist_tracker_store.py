"""SQLite store for the watchlist trend + paper P&L tab.

Tables:
  - watchlist_groups: independent groups for stocks
  - watchlist_stocks: stocks assigned to one group, with paper-buy fields
  - watchlist_daily_quotes: persisted daily OHLCV / change series
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.paths import get_dashboard_home

# Match other storage modules: fall back under app/.local-data unless env overrides.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_HOME = get_dashboard_home(PROJECT_ROOT)
DEFAULT_DB_PATH = Path(
    os.environ.get("DASHBOARD_WATCHLIST_DB")
    or str(DASHBOARD_HOME / "watchlist_tracker.db")
)
DEFAULT_TARGET_AMOUNT = 10_000.0

_DB_INIT_LOCK = threading.Lock()
_INITIALIZED: dict[str, tuple[int, int]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def _database_identity(db_path: Path) -> tuple[int, int] | None:
    try:
        stat = db_path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


def _open_connection(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    path_key = str(db_path.resolve())
    identity = _database_identity(db_path)
    if identity is None or _INITIALIZED.get(path_key) != identity:
        with _DB_INIT_LOCK:
            identity = _database_identity(db_path)
            if identity is None or _INITIALIZED.get(path_key) != identity:
                con = _open_connection(db_path)
                con.execute("PRAGMA journal_mode=WAL")
                init_db(con)
                refreshed = _database_identity(db_path)
                if refreshed is not None:
                    _INITIALIZED[path_key] = refreshed
                return con
    return _open_connection(db_path)


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS watchlist_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watchlist_stocks (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            market TEXT NOT NULL DEFAULT '',
            group_id INTEGER,
            note TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            buy_date TEXT NOT NULL DEFAULT '',
            buy_price REAL,
            buy_shares INTEGER NOT NULL DEFAULT 0,
            buy_amount REAL NOT NULL DEFAULT 0,
            target_amount REAL NOT NULL DEFAULT 10000,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(group_id) REFERENCES watchlist_groups(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS watchlist_daily_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open_price REAL,
            close_price REAL,
            high_price REAL,
            low_price REAL,
            change_amount REAL,
            change_percent REAL,
            volume REAL,
            source TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(code, trade_date),
            FOREIGN KEY(code) REFERENCES watchlist_stocks(code) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_watchlist_groups_sort
            ON watchlist_groups(sort_order ASC, id ASC);
        CREATE INDEX IF NOT EXISTS idx_watchlist_stocks_group
            ON watchlist_stocks(group_id, active, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_watchlist_quotes_date
            ON watchlist_daily_quotes(trade_date DESC);
        CREATE INDEX IF NOT EXISTS idx_watchlist_quotes_code_date
            ON watchlist_daily_quotes(code, trade_date DESC);
        """
    )
    con.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def list_groups(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT g.*,
               COALESCE((
                   SELECT COUNT(*) FROM watchlist_stocks s
                   WHERE s.group_id = g.id AND s.active = 1
               ), 0) AS stock_count
        FROM watchlist_groups g
        ORDER BY g.sort_order ASC, g.id ASC
        """
    ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def get_group(con: sqlite3.Connection, group_id: int) -> dict[str, Any] | None:
    row = con.execute(
        "SELECT * FROM watchlist_groups WHERE id = ?",
        (int(group_id),),
    ).fetchone()
    return row_to_dict(row)


def create_group(
    con: sqlite3.Connection,
    *,
    name: str,
    note: str = "",
    sort_order: int | None = None,
) -> dict[str, Any]:
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("分组名称不能为空")
    ts = _now_iso()
    if sort_order is None:
        max_order = con.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM watchlist_groups"
        ).fetchone()["m"]
        sort_order = int(max_order) + 10
    cur = con.execute(
        """
        INSERT INTO watchlist_groups(name, note, sort_order, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (cleaned, str(note or "").strip(), int(sort_order), ts, ts),
    )
    con.commit()
    group = get_group(con, int(cur.lastrowid))
    assert group is not None
    group["stock_count"] = 0
    return group


def update_group(
    con: sqlite3.Connection,
    group_id: int,
    *,
    name: str | None = None,
    note: str | None = None,
    sort_order: int | None = None,
) -> dict[str, Any]:
    current = get_group(con, group_id)
    if current is None:
        raise KeyError(f"分组不存在: {group_id}")
    next_name = current["name"] if name is None else str(name).strip()
    if not next_name:
        raise ValueError("分组名称不能为空")
    next_note = current["note"] if note is None else str(note).strip()
    next_order = current["sort_order"] if sort_order is None else int(sort_order)
    con.execute(
        """
        UPDATE watchlist_groups
        SET name = ?, note = ?, sort_order = ?, updated_at = ?
        WHERE id = ?
        """,
        (next_name, next_note, next_order, _now_iso(), int(group_id)),
    )
    con.commit()
    group = get_group(con, group_id)
    assert group is not None
    return group


def delete_group(con: sqlite3.Connection, group_id: int) -> None:
    current = get_group(con, group_id)
    if current is None:
        raise KeyError(f"分组不存在: {group_id}")
    con.execute(
        "UPDATE watchlist_stocks SET group_id = NULL, updated_at = ? WHERE group_id = ?",
        (_now_iso(), int(group_id)),
    )
    con.execute("DELETE FROM watchlist_groups WHERE id = ?", (int(group_id),))
    con.commit()


def list_stocks(
    con: sqlite3.Connection,
    *,
    group_id: int | None = None,
    q: str = "",
    active_only: bool = True,
) -> list[dict[str, Any]]:
    where: list[str] = []
    args: list[Any] = []
    if active_only:
        where.append("s.active = 1")
    if group_id is not None:
        where.append("s.group_id = ?")
        args.append(int(group_id))
    query = str(q or "").strip()
    if query:
        where.append(
            "(s.code LIKE ? OR s.name LIKE ? OR s.note LIKE ? OR COALESCE(g.name, '') LIKE ?)"
        )
        like = f"%{query}%"
        args.extend([like, like, like, like])
    sql = f"""
        SELECT s.*, g.name AS group_name
        FROM watchlist_stocks s
        LEFT JOIN watchlist_groups g ON g.id = s.group_id
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY COALESCE(g.sort_order, 999999) ASC, s.created_at DESC, s.code ASC
    """
    rows = con.execute(sql, args).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def get_stock(con: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    row = con.execute(
        """
        SELECT s.*, g.name AS group_name
        FROM watchlist_stocks s
        LEFT JOIN watchlist_groups g ON g.id = s.group_id
        WHERE s.code = ?
        """,
        (str(code),),
    ).fetchone()
    return row_to_dict(row)


def upsert_stock(
    con: sqlite3.Connection,
    *,
    code: str,
    name: str = "",
    market: str = "",
    group_id: int | None = None,
    note: str = "",
    active: bool = True,
    buy_date: str = "",
    target_amount: float = DEFAULT_TARGET_AMOUNT,
    buy_price: float | None = None,
    buy_shares: int = 0,
    buy_amount: float = 0.0,
) -> dict[str, Any]:
    code = str(code or "").strip()
    if not code:
        raise ValueError("股票代码不能为空")
    ts = _now_iso()
    existing = get_stock(con, code)
    if existing is None:
        con.execute(
            """
            INSERT INTO watchlist_stocks(
                code, name, market, group_id, note, active,
                buy_date, buy_price, buy_shares, buy_amount, target_amount,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                str(name or "").strip(),
                str(market or "").strip(),
                group_id,
                str(note or "").strip(),
                1 if active else 0,
                str(buy_date or "").strip(),
                buy_price,
                int(buy_shares or 0),
                float(buy_amount or 0),
                float(target_amount or DEFAULT_TARGET_AMOUNT),
                ts,
                ts,
            ),
        )
    else:
        next_name = str(name or "").strip() or existing.get("name") or ""
        next_market = str(market or "").strip() or existing.get("market") or ""
        next_group = group_id if group_id is not None else existing.get("group_id")
        next_note = note if note is not None else existing.get("note") or ""
        con.execute(
            """
            UPDATE watchlist_stocks
            SET name = ?, market = ?, group_id = ?, note = ?, active = ?,
                buy_date = ?, buy_price = ?, buy_shares = ?, buy_amount = ?,
                target_amount = ?, updated_at = ?
            WHERE code = ?
            """,
            (
                next_name,
                next_market,
                next_group,
                str(next_note or "").strip(),
                1 if active else 0,
                str(buy_date or "").strip(),
                buy_price,
                int(buy_shares or 0),
                float(buy_amount or 0),
                float(target_amount or DEFAULT_TARGET_AMOUNT),
                ts,
                code,
            ),
        )
    con.commit()
    stock = get_stock(con, code)
    assert stock is not None
    return stock


def update_stock_fields(
    con: sqlite3.Connection,
    code: str,
    **fields: Any,
) -> dict[str, Any]:
    current = get_stock(con, code)
    if current is None:
        raise KeyError(f"股票不存在: {code}")
    allowed = {
        "name",
        "market",
        "group_id",
        "note",
        "active",
        "buy_date",
        "buy_price",
        "buy_shares",
        "buy_amount",
        "target_amount",
    }
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "active":
            updates[key] = 1 if value else 0
        elif key == "group_id":
            updates[key] = None if value in ("", None) else int(value)
        elif key in {"buy_price", "buy_amount", "target_amount"}:
            updates[key] = None if value in ("", None) else float(value)
        elif key == "buy_shares":
            updates[key] = int(value or 0)
        else:
            updates[key] = str(value or "").strip()
    if not updates:
        return current
    updates["updated_at"] = _now_iso()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    con.execute(
        f"UPDATE watchlist_stocks SET {assignments} WHERE code = ?",
        (*updates.values(), code),
    )
    con.commit()
    stock = get_stock(con, code)
    assert stock is not None
    return stock


def delete_stock(con: sqlite3.Connection, code: str) -> None:
    current = get_stock(con, code)
    if current is None:
        raise KeyError(f"股票不存在: {code}")
    con.execute("DELETE FROM watchlist_daily_quotes WHERE code = ?", (code,))
    con.execute("DELETE FROM watchlist_stocks WHERE code = ?", (code,))
    con.commit()


def upsert_quote(
    con: sqlite3.Connection,
    quote: dict[str, Any],
    *,
    missing_only: bool = False,
) -> bool:
    code = str(quote.get("code") or "").strip()
    trade_date = str(quote.get("trade_date") or "").strip()
    if not code or not trade_date:
        return False
    values = (
        code,
        trade_date,
        quote.get("open_price"),
        quote.get("close_price"),
        quote.get("high_price"),
        quote.get("low_price"),
        quote.get("change_amount"),
        quote.get("change_percent"),
        quote.get("volume"),
        str(quote.get("source") or ""),
        _now_iso(),
    )
    if missing_only:
        cur = con.execute(
            """
            INSERT OR IGNORE INTO watchlist_daily_quotes(
                code, trade_date, open_price, close_price, high_price, low_price,
                change_amount, change_percent, volume, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return cur.rowcount > 0
    con.execute(
        """
        INSERT INTO watchlist_daily_quotes(
            code, trade_date, open_price, close_price, high_price, low_price,
            change_amount, change_percent, volume, source, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, trade_date) DO UPDATE SET
            open_price = excluded.open_price,
            close_price = excluded.close_price,
            high_price = excluded.high_price,
            low_price = excluded.low_price,
            change_amount = excluded.change_amount,
            change_percent = excluded.change_percent,
            volume = excluded.volume,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        values,
    )
    return True


def upsert_quotes(
    con: sqlite3.Connection,
    quotes: list[dict[str, Any]],
    *,
    missing_only: bool = False,
) -> int:
    written = 0
    for quote in quotes:
        if upsert_quote(con, quote, missing_only=missing_only):
            written += 1
    con.commit()
    return written


def recent_trade_dates(con: sqlite3.Connection, days: int) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT trade_date
        FROM watchlist_daily_quotes
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (max(1, int(days)),),
    ).fetchall()
    dates = [str(row["trade_date"]) for row in rows]
    dates.sort()
    return dates


def quotes_for_codes(
    con: sqlite3.Connection,
    codes: list[str],
    trade_dates: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not codes or not trade_dates:
        return {}
    placeholders_dates = ",".join("?" for _ in trade_dates)
    placeholders_codes = ",".join("?" for _ in codes)
    rows = con.execute(
        f"""
        SELECT * FROM watchlist_daily_quotes
        WHERE trade_date IN ({placeholders_dates})
          AND code IN ({placeholders_codes})
        """,
        (*trade_dates, *codes),
    ).fetchall()
    return {
        (str(row["code"]), str(row["trade_date"])): (row_to_dict(row) or {})
        for row in rows
    }


def quote_on_date(
    con: sqlite3.Connection,
    code: str,
    trade_date: str,
) -> dict[str, Any] | None:
    row = con.execute(
        """
        SELECT * FROM watchlist_daily_quotes
        WHERE code = ? AND trade_date = ?
        """,
        (code, trade_date),
    ).fetchone()
    return row_to_dict(row)


def latest_quote(con: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    row = con.execute(
        """
        SELECT * FROM watchlist_daily_quotes
        WHERE code = ?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    return row_to_dict(row)


def stock_quote_dates(con: sqlite3.Connection, code: str) -> list[str]:
    rows = con.execute(
        """
        SELECT trade_date FROM watchlist_daily_quotes
        WHERE code = ?
        ORDER BY trade_date ASC
        """,
        (code,),
    ).fetchall()
    return [str(row["trade_date"]) for row in rows]
