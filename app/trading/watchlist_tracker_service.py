"""Watchlist trend board + fixed-amount paper P&L service.

Independent from the LLM practice trader. Each stock can pick a buy date and
is notionally filled with about 10,000 CNY of whole shares on that day's close.
"""
from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from app.storage import watchlist_tracker_store as store

DEFAULT_TARGET_AMOUNT = store.DEFAULT_TARGET_AMOUNT
ALLOWED_BOARD_DAYS = (5, 7, 15, 60)
DEFAULT_BOARD_DAYS = 5
CODE_RE = re.compile(r"\b(\d{6})\b")

_JOB_LOCK = threading.Lock()
_JOB_RUNNING: str | None = None


class JobConflictError(RuntimeError):
    """Raised when update-today / backfill already running."""


def _import_cn_stock_tools():
    try:
        from app.market_data import cn_stock_tools as tools  # type: ignore
        return tools
    except Exception:
        import cn_stock_tools as tools  # type: ignore
        return tools


def normalize_code(raw: str) -> dict[str, str]:
    tools = _import_cn_stock_tools()
    return tools.normalize_symbol(str(raw or "").strip())


def parse_codes(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        text = " ".join(str(item) for item in raw)
    else:
        text = str(raw)
    codes: list[str] = []
    seen: set[str] = set()
    for match in CODE_RE.findall(text):
        if match in seen:
            continue
        seen.add(match)
        codes.append(match)
    return codes


def clamp_board_days(value: Any, default: int = DEFAULT_BOARD_DAYS) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return default
    if days in ALLOWED_BOARD_DAYS:
        return days
    # nearest allowed option
    return min(ALLOWED_BOARD_DAYS, key=lambda item: abs(item - days))


def today_shanghai() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    except Exception:
        return date.today().isoformat()


def compute_paper_fill(
    close_price: float | None,
    target_amount: float = DEFAULT_TARGET_AMOUNT,
) -> dict[str, Any]:
    """Buy as many whole shares as possible without exceeding target_amount."""
    target = float(target_amount or DEFAULT_TARGET_AMOUNT)
    if target <= 0:
        target = DEFAULT_TARGET_AMOUNT
    price = float(close_price) if close_price is not None else None
    if price is None or price <= 0:
        return {
            "buy_price": None,
            "buy_shares": 0,
            "buy_amount": 0.0,
            "filled": False,
            "reason": "missing_price",
        }
    shares = int(target // price)
    if shares <= 0:
        return {
            "buy_price": round(price, 4),
            "buy_shares": 0,
            "buy_amount": 0.0,
            "filled": False,
            "reason": "price_above_budget",
        }
    amount = round(shares * price, 2)
    return {
        "buy_price": round(price, 4),
        "buy_shares": shares,
        "buy_amount": amount,
        "filled": True,
        "reason": "ok",
    }


def compute_paper_pnl(
    *,
    buy_shares: int,
    buy_amount: float,
    last_price: float | None,
) -> dict[str, Any]:
    shares = int(buy_shares or 0)
    cost = float(buy_amount or 0)
    if shares <= 0 or cost <= 0 or last_price is None or last_price <= 0:
        return {
            "last_price": last_price,
            "market_value": None,
            "pnl": None,
            "pnl_percent": None,
        }
    market_value = round(shares * float(last_price), 2)
    pnl = round(market_value - cost, 2)
    pnl_percent = round((pnl / cost) * 100, 2) if cost else None
    return {
        "last_price": round(float(last_price), 4),
        "market_value": market_value,
        "pnl": pnl,
        "pnl_percent": pnl_percent,
    }


def _with_conn(db_path: Path | str | None, fn: Callable[[Any], Any]) -> Any:
    con = store.connect(db_path)
    try:
        return fn(con)
    finally:
        con.close()


def _kline_to_quote(code: str, row: dict[str, Any], *, source: str) -> dict[str, Any]:
    open_price = float(row["open"])
    close_price = float(row["close"])
    prev = row.get("prev_close")
    if prev is None:
        change_amount = None
        change_percent = None
    else:
        prev_close = float(prev)
        change_amount = round(close_price - prev_close, 4)
        change_percent = (
            round((change_amount / prev_close) * 100, 4) if prev_close else None
        )
    return {
        "code": code,
        "trade_date": str(row["date"])[:10],
        "open_price": open_price,
        "close_price": close_price,
        "high_price": float(row.get("high") or close_price),
        "low_price": float(row.get("low") or close_price),
        "change_amount": change_amount,
        "change_percent": change_percent,
        "volume": float(row.get("volume") or 0),
        "source": source,
    }


def fetch_history_quotes(code: str, days: int) -> list[dict[str, Any]]:
    tools = _import_cn_stock_tools()
    # pull a few extra bars so change_percent can use previous close
    rows = tools.get_klines(code, count=max(days + 5, 10))
    quotes: list[dict[str, Any]] = []
    prev_close: float | None = None
    for row in rows:
        enriched = dict(row)
        enriched["prev_close"] = prev_close
        quotes.append(_kline_to_quote(code, enriched, source="tencent-kline"))
        prev_close = float(row["close"])
    return quotes[-days:] if days > 0 else quotes


def fetch_today_quote(code: str) -> dict[str, Any] | None:
    tools = _import_cn_stock_tools()
    quote = tools.get_quote(code)
    if not quote:
        return None
    price = quote.get("price")
    open_price = quote.get("open")
    prev_close = quote.get("prev_close")
    change = quote.get("change")
    change_pct = quote.get("change_pct")
    # cn_stock_tools change_pct is already percent points (e.g. 1.23)
    trade_date = today_shanghai()
    return {
        "code": str(quote.get("code") or code),
        "name": str(quote.get("name") or ""),
        "trade_date": trade_date,
        "open_price": open_price,
        "close_price": price,
        "high_price": quote.get("high"),
        "low_price": quote.get("low"),
        "change_amount": change,
        "change_percent": change_pct,
        "volume": quote.get("volume_lots"),
        "source": str(quote.get("source") or "live-quote"),
        "prev_close": prev_close,
    }


def refresh_paper_fill_for_stock(con: Any, code: str) -> dict[str, Any] | None:
    stock = store.get_stock(con, code)
    if stock is None:
        return None
    buy_date = str(stock.get("buy_date") or "").strip()
    target = float(stock.get("target_amount") or DEFAULT_TARGET_AMOUNT)
    if not buy_date:
        return store.update_stock_fields(
            con,
            code,
            buy_price=None,
            buy_shares=0,
            buy_amount=0,
        )
    quote = store.quote_on_date(con, code, buy_date)
    close_price = None if quote is None else quote.get("close_price")
    fill = compute_paper_fill(close_price, target)
    return store.update_stock_fields(
        con,
        code,
        buy_price=fill["buy_price"],
        buy_shares=fill["buy_shares"],
        buy_amount=fill["buy_amount"],
    )


def refresh_all_paper_fills(con: Any, codes: list[str] | None = None) -> int:
    stocks = store.list_stocks(con, active_only=False)
    if codes is not None:
        wanted = set(codes)
        stocks = [item for item in stocks if item["code"] in wanted]
    count = 0
    for stock in stocks:
        refresh_paper_fill_for_stock(con, stock["code"])
        count += 1
    return count


def _empty_group_bucket(group_id: int | None, group_name: str) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "group_name": group_name,
        "stock_count": 0,
        "filled_count": 0,
        "total_cost": 0.0,
        "total_market_value": 0.0,
        "total_pnl": 0.0,
        "total_pnl_percent": None,
        "stocks": [],
    }


def _finalize_group_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    filled = int(bucket["filled_count"] or 0)
    cost = float(bucket["total_cost"] or 0)
    market = float(bucket["total_market_value"] or 0)
    if filled and cost > 0:
        pnl = round(market - cost, 2)
        bucket["total_pnl"] = pnl
        bucket["total_pnl_percent"] = round((pnl / cost) * 100, 2)
    else:
        bucket["total_pnl"] = 0.0
        bucket["total_pnl_percent"] = None
    bucket["total_cost"] = round(cost, 2)
    bucket["total_market_value"] = round(market, 2)
    bucket["stock_count"] = len(bucket["stocks"])
    return bucket


def build_board(
    *,
    days: int = DEFAULT_BOARD_DAYS,
    group_id: int | None = None,
    q: str = "",
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    days = clamp_board_days(days)

    def _build(con: Any) -> dict[str, Any]:
        groups = store.list_groups(con)
        stocks = store.list_stocks(con, group_id=group_id, q=q, active_only=True)
        trade_dates = store.recent_trade_dates(con, days)
        latest_trade_date = trade_dates[-1] if trade_dates else ""
        codes = [str(item["code"]) for item in stocks]
        quote_map = store.quotes_for_codes(con, codes, trade_dates)
        rows: list[dict[str, Any]] = []
        total_cost = 0.0
        total_market = 0.0
        filled_count = 0
        pending_count = 0

        # Preserve configured group order, then append ungrouped.
        section_map: dict[str, dict[str, Any]] = {}
        section_order: list[str] = []
        for group in groups:
            key = f"g:{group['id']}"
            section_map[key] = _empty_group_bucket(int(group["id"]), str(group["name"]))
            section_order.append(key)
        ungrouped_key = "g:none"
        section_map[ungrouped_key] = _empty_group_bucket(None, "未分组")
        section_order.append(ungrouped_key)

        for stock in stocks:
            code = str(stock["code"])
            quotes = []
            for trade_date in trade_dates:
                item = quote_map.get((code, trade_date))
                quotes.append(
                    None
                    if item is None
                    else {
                        "trade_date": trade_date,
                        "open_price": item.get("open_price"),
                        "close_price": item.get("close_price"),
                        "change_amount": item.get("change_amount"),
                        "change_percent": item.get("change_percent"),
                    }
                )
            latest = None
            for item in reversed(quotes):
                if item and item.get("close_price") is not None:
                    latest = item
                    break

            buy_date = str(stock.get("buy_date") or "").strip()
            buy_shares = int(stock.get("buy_shares") or 0)
            buy_amount = float(stock.get("buy_amount") or 0)
            buy_price = stock.get("buy_price")
            fill_status = "filled"
            fill_hint = ""
            if not buy_date:
                fill_status = "no_buy_date"
                fill_hint = "未设置买入日"
            elif buy_shares <= 0 or buy_amount <= 0 or buy_price in (None, ""):
                fill_status = "pending_fill"
                fill_hint = "买入日行情缺失，请回补历史"
                pending_count += 1
            elif latest is None:
                fill_status = "pending_quote"
                fill_hint = "暂无最新行情"
                pending_count += 1
            elif buy_date == latest_trade_date:
                fill_hint = "买入日=最新交易日，浮动盈亏暂为 0"

            pnl = compute_paper_pnl(
                buy_shares=buy_shares,
                buy_amount=buy_amount,
                last_price=None if latest is None else latest.get("close_price"),
            )
            # Always expose numeric zeros once filled, so UI never looks "empty".
            if fill_status == "filled":
                if pnl["pnl"] is None:
                    pnl["pnl"] = 0.0
                if pnl["pnl_percent"] is None and buy_amount > 0:
                    pnl["pnl_percent"] = 0.0
                filled_count += 1
                total_cost += buy_amount
                if pnl["market_value"] is not None:
                    total_market += float(pnl["market_value"])

            price_change = None
            if buy_price not in (None, "") and pnl["last_price"] not in (None, ""):
                try:
                    price_change = round(float(pnl["last_price"]) - float(buy_price), 4)
                except (TypeError, ValueError):
                    price_change = None

            row = {
                "code": code,
                "name": stock.get("name") or "",
                "market": stock.get("market") or "",
                "group_id": stock.get("group_id"),
                "group_name": stock.get("group_name") or "未分组",
                "note": stock.get("note") or "",
                "active": bool(stock.get("active")),
                "buy_date": buy_date,
                "buy_price": buy_price,
                "buy_shares": buy_shares,
                "buy_amount": buy_amount,
                "target_amount": float(stock.get("target_amount") or DEFAULT_TARGET_AMOUNT),
                "last_price": pnl["last_price"],
                "market_value": pnl["market_value"],
                "pnl": pnl["pnl"],
                "pnl_percent": pnl["pnl_percent"],
                "price_change": price_change,
                "fill_status": fill_status,
                "fill_hint": fill_hint,
                "quotes": quotes,
            }
            rows.append(row)

            raw_gid = stock.get("group_id")
            key = f"g:{int(raw_gid)}" if raw_gid not in (None, "") else ungrouped_key
            if key not in section_map:
                section_map[key] = _empty_group_bucket(
                    None if raw_gid in (None, "") else int(raw_gid),
                    str(stock.get("group_name") or "未分组"),
                )
                section_order.append(key)
            bucket = section_map[key]
            bucket["stocks"].append(row)
            if fill_status == "filled":
                bucket["filled_count"] += 1
                bucket["total_cost"] += buy_amount
                if pnl["market_value"] is not None:
                    bucket["total_market_value"] += float(pnl["market_value"])

        # Keep empty groups visible (except hide empty 未分组 when nothing ungrouped and filter active).
        sections = []
        for key in section_order:
            bucket = _finalize_group_bucket(section_map[key])
            if key == ungrouped_key and bucket["stock_count"] == 0 and group_id is not None:
                continue
            if key != ungrouped_key or bucket["stock_count"] > 0 or not groups:
                sections.append(bucket)
            elif bucket["stock_count"] > 0:
                sections.append(bucket)
        # Ensure ungrouped with stocks always present.
        if section_map[ungrouped_key]["stocks"] and section_map[ungrouped_key] not in sections:
            sections.append(_finalize_group_bucket(section_map[ungrouped_key]))

        # Enrich group list with live summary for chips.
        summary_by_id = {
            item["group_id"]: item
            for item in sections
            if item["group_id"] is not None
        }
        enriched_groups = []
        for group in groups:
            live = summary_by_id.get(int(group["id"]))
            enriched = dict(group)
            if live:
                enriched["stock_count"] = live["stock_count"]
                enriched["filled_count"] = live["filled_count"]
                enriched["total_cost"] = live["total_cost"]
                enriched["total_market_value"] = live["total_market_value"]
                enriched["total_pnl"] = live["total_pnl"]
                enriched["total_pnl_percent"] = live["total_pnl_percent"]
            else:
                enriched.setdefault("stock_count", 0)
                enriched["filled_count"] = 0
                enriched["total_cost"] = 0
                enriched["total_market_value"] = 0
                enriched["total_pnl"] = 0
                enriched["total_pnl_percent"] = None
            enriched_groups.append(enriched)

        summary_pnl = round(total_market - total_cost, 2) if filled_count else 0.0
        summary_pct = (
            round((summary_pnl / total_cost) * 100, 2)
            if filled_count and total_cost > 0
            else (0.0 if filled_count else None)
        )
        return {
            "days": days,
            "allowed_days": list(ALLOWED_BOARD_DAYS),
            "trade_dates": trade_dates,
            "latest_trade_date": latest_trade_date,
            "group_id": group_id,
            "q": q,
            "groups": enriched_groups,
            "sections": sections,
            "stocks": rows,
            "total": len(rows),
            "summary": {
                "filled_count": filled_count,
                "pending_count": pending_count,
                "total_cost": round(total_cost, 2) if filled_count else 0,
                "total_market_value": round(total_market, 2) if filled_count else 0,
                "total_pnl": summary_pnl if filled_count else 0,
                "total_pnl_percent": summary_pct,
                "target_amount_default": DEFAULT_TARGET_AMOUNT,
            },
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    return _with_conn(db_path, _build)


def list_groups(*, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    return _with_conn(db_path, store.list_groups)


def create_group(
    *,
    name: str,
    note: str = "",
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    return _with_conn(db_path, lambda con: store.create_group(con, name=name, note=note))


def update_group(
    group_id: int,
    *,
    name: str | None = None,
    note: str | None = None,
    sort_order: int | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    return _with_conn(
        db_path,
        lambda con: store.update_group(
            con,
            group_id,
            name=name,
            note=note,
            sort_order=sort_order,
        ),
    )


def delete_group(group_id: int, *, db_path: Path | str | None = None) -> None:
    _with_conn(db_path, lambda con: store.delete_group(con, group_id))


def add_stocks(
    *,
    codes: str | list[str],
    group_id: int | None = None,
    note: str = "",
    buy_date: str | None = None,
    target_amount: float = DEFAULT_TARGET_AMOUNT,
    backfill_days: int = 60,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    code_list = parse_codes(codes)
    if not code_list:
        raise ValueError("请输入有效的股票代码")
    resolved_buy_date = str(buy_date or "").strip() or today_shanghai()
    target = float(target_amount or DEFAULT_TARGET_AMOUNT)

    def _add(con: Any) -> dict[str, Any]:
        if group_id is not None and store.get_group(con, group_id) is None:
            raise KeyError(f"分组不存在: {group_id}")
        added = []
        failed = []
        for code in code_list:
            try:
                meta = normalize_code(code)
                store.upsert_stock(
                    con,
                    code=meta["code"],
                    name="",
                    market=meta["market"],
                    group_id=group_id,
                    note=note,
                    active=True,
                    buy_date=resolved_buy_date,
                    target_amount=target,
                )
                # best-effort history + name fill
                try:
                    history = fetch_history_quotes(meta["code"], max(backfill_days, 30))
                    if history:
                        store.upsert_quotes(con, history, missing_only=False)
                        # use latest kline name via live quote if possible
                    live = fetch_today_quote(meta["code"])
                    if live:
                        if live.get("name"):
                            store.update_stock_fields(con, meta["code"], name=live["name"])
                        # only write today bar if it looks like a real session price
                        if live.get("close_price") is not None:
                            store.upsert_quote(
                                con,
                                {
                                    "code": meta["code"],
                                    "trade_date": live["trade_date"],
                                    "open_price": live.get("open_price"),
                                    "close_price": live.get("close_price"),
                                    "high_price": live.get("high_price"),
                                    "low_price": live.get("low_price"),
                                    "change_amount": live.get("change_amount"),
                                    "change_percent": live.get("change_percent"),
                                    "volume": live.get("volume"),
                                    "source": live.get("source") or "live-quote",
                                },
                                missing_only=False,
                            )
                            con.commit()
                except Exception as exc:  # network optional on add
                    failed.append({"code": meta["code"], "error": str(exc)})
                refresh_paper_fill_for_stock(con, meta["code"])
                added.append(meta["code"])
            except Exception as exc:
                failed.append({"code": code, "error": str(exc)})
        board = build_board(days=DEFAULT_BOARD_DAYS, group_id=group_id, db_path=db_path)
        return {
            "added": added,
            "failed": failed,
            "buy_date": resolved_buy_date,
            "board": board,
        }

    return _with_conn(db_path, _add)


_UNSET = object()


def update_stock(
    code: str,
    *,
    name: str | None = None,
    note: str | None = None,
    group_id: Any = _UNSET,
    buy_date: str | None = None,
    target_amount: float | None = None,
    active: bool | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    meta = normalize_code(code)
    code = meta["code"]

    def _update(con: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name
        if note is not None:
            fields["note"] = note
        if group_id is not _UNSET:
            if group_id in ("", None):
                fields["group_id"] = None
            else:
                gid = int(group_id)
                if store.get_group(con, gid) is None:
                    raise KeyError(f"分组不存在: {gid}")
                fields["group_id"] = gid
        if buy_date is not None:
            fields["buy_date"] = str(buy_date).strip()
        if target_amount is not None:
            fields["target_amount"] = float(target_amount)
        if active is not None:
            fields["active"] = bool(active)
        if fields:
            store.update_stock_fields(con, code, **fields)
        if "buy_date" in fields or "target_amount" in fields:
            refresh_paper_fill_for_stock(con, code)
        stock = store.get_stock(con, code)
        if stock is None:
            raise KeyError(f"股票不存在: {code}")
        latest = store.latest_quote(con, code)
        pnl = compute_paper_pnl(
            buy_shares=int(stock.get("buy_shares") or 0),
            buy_amount=float(stock.get("buy_amount") or 0),
            last_price=None if latest is None else latest.get("close_price"),
        )
        return {**stock, **pnl}

    return _with_conn(db_path, _update)


def delete_stock(code: str, *, db_path: Path | str | None = None) -> None:
    meta = normalize_code(code)
    _with_conn(db_path, lambda con: store.delete_stock(con, meta["code"]))


def _acquire_job(name: str) -> None:
    global _JOB_RUNNING
    with _JOB_LOCK:
        if _JOB_RUNNING:
            raise JobConflictError(f"任务进行中: {_JOB_RUNNING}")
        _JOB_RUNNING = name


def _release_job() -> None:
    global _JOB_RUNNING
    with _JOB_LOCK:
        _JOB_RUNNING = None


def update_today_quotes(
    *,
    codes: list[str] | None = None,
    force: bool = False,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    _acquire_job("update-today")
    try:
        def _run(con: Any) -> dict[str, Any]:
            stocks = store.list_stocks(con, active_only=True)
            if codes:
                wanted = set(parse_codes(codes))
                stocks = [item for item in stocks if item["code"] in wanted]
            updated = 0
            failed: list[dict[str, str]] = []
            trade_dates: set[str] = set()
            today = today_shanghai()
            for stock in stocks:
                code = stock["code"]
                try:
                    live = fetch_today_quote(code)
                    if not live or live.get("close_price") is None:
                        failed.append({"code": code, "error": "no_quote"})
                        continue
                    trade_date = str(live.get("trade_date") or today)
                    trade_dates.add(trade_date)
                    if not force and trade_date != today:
                        # non-trading day: skip writes
                        continue
                    if live.get("name") and not stock.get("name"):
                        store.update_stock_fields(con, code, name=live["name"])
                    store.upsert_quote(
                        con,
                        {
                            "code": code,
                            "trade_date": trade_date,
                            "open_price": live.get("open_price"),
                            "close_price": live.get("close_price"),
                            "high_price": live.get("high_price"),
                            "low_price": live.get("low_price"),
                            "change_amount": live.get("change_amount"),
                            "change_percent": live.get("change_percent"),
                            "volume": live.get("volume"),
                            "source": live.get("source") or "live-quote",
                        },
                        missing_only=False,
                    )
                    con.commit()
                    refresh_paper_fill_for_stock(con, code)
                    updated += 1
                except Exception as exc:
                    failed.append({"code": code, "error": str(exc)})
            skipped = (not force) and trade_dates and today not in trade_dates
            return {
                "updated": updated,
                "failed": failed,
                "skipped": bool(skipped),
                "trade_dates": sorted(trade_dates),
                "board": build_board(db_path=db_path),
            }

        return _with_conn(db_path, _run)
    finally:
        _release_job()


def backfill_quotes(
    *,
    days: int = 60,
    codes: list[str] | None = None,
    missing_only: bool = True,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    days = max(1, min(int(days or 60), 365))
    _acquire_job("backfill")
    try:
        def _run(con: Any) -> dict[str, Any]:
            stocks = store.list_stocks(con, active_only=True)
            if codes:
                wanted = set(parse_codes(codes))
                stocks = [item for item in stocks if item["code"] in wanted]
            quote_count = 0
            failed: list[dict[str, str]] = []
            for stock in stocks:
                code = stock["code"]
                try:
                    history = fetch_history_quotes(code, days)
                    written = store.upsert_quotes(
                        con,
                        history,
                        missing_only=missing_only,
                    )
                    quote_count += written
                    if history and not stock.get("name"):
                        # name still empty — try live once
                        try:
                            live = fetch_today_quote(code)
                            if live and live.get("name"):
                                store.update_stock_fields(con, code, name=live["name"])
                        except Exception:
                            pass
                    refresh_paper_fill_for_stock(con, code)
                except Exception as exc:
                    failed.append({"code": code, "error": str(exc)})
            return {
                "days": days,
                "stocks": len(stocks),
                "quotes": quote_count,
                "missing_only": missing_only,
                "failed": failed,
                "board": build_board(days=min(days, 60), db_path=db_path),
            }

        return _with_conn(db_path, _run)
    finally:
        _release_job()
