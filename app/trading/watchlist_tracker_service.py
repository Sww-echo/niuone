"""Watchlist trend board + fixed-amount paper P&L service.

Independent from the LLM practice trader. Each stock can pick a buy date and
is notionally filled with about 10,000 CNY of whole shares on that day's close.
"""
from __future__ import annotations

import math
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.storage import watchlist_tracker_store as store
from app.market_data.tencent_kline_cache import quote_trade_date
from app.reports.a_share.calendar import trading_day_status

DEFAULT_TARGET_AMOUNT = store.DEFAULT_TARGET_AMOUNT
ALLOWED_BOARD_DAYS = (5, 7, 15, 60)
DEFAULT_BOARD_DAYS = 5
CODE_RE = re.compile(r"\b(\d{6})\b")

_JOB_LOCK = threading.Lock()
_JOB_RUNNING: str | None = None


class JobConflictError(RuntimeError):
    """Raised when update-today / backfill already running."""


class JobInterruptedError(RuntimeError):
    """A stopping service must leave unfinished codes available for retry."""


class QuoteDataError(ValueError):
    """A safe, user-facing explanation for unusable provider data."""


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
    except (TypeError, ValueError, OverflowError):
        return default
    if days in ALLOWED_BOARD_DAYS:
        return days
    # nearest allowed option
    return min(ALLOWED_BOARD_DAYS, key=lambda item: abs(item - days))


def today_shanghai() -> str:
    return now_shanghai().date().isoformat()


def now_shanghai() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def valid_trade_date(value: Any) -> bool:
    text = str(value or "")
    try:
        if date.fromisoformat(text).isoformat() != text or text > today_shanghai():
            return False
    except ValueError:
        return False
    return bool(trading_day_status(text, allow_refresh=False)["is_trading_day"])


def latest_session_date(*, completed: bool = False) -> str:
    now = now_shanghai()
    status = trading_day_status(now.date(), allow_refresh=False)
    boundary = (15, 0) if completed else (9, 25)
    if status["is_trading_day"] and (now.hour, now.minute) >= boundary:
        return now.date().isoformat()
    return str(status["previous_trading_day"])


def validate_buy_date(value: Any) -> str:
    text = str(value or "").strip()
    if text and not valid_trade_date(text):
        raise ValueError("买入日期须为已发生的交易日，不能是未来日期或休市日")
    return text


def positive_number(value: Any) -> float | None:
    number = finite_number(value)
    return number if number is not None and number > 0 else None


def finite_number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def validate_target_amount(value: Any) -> float:
    number = positive_number(value)
    if number is None:
        raise ValueError("模拟买入金额须为大于零的有效数字")
    return number


def validate_backfill_days(value: Any) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 365:
        raise ValueError("回补天数须为 1 至 365 的整数")
    return int(value)


def compute_paper_fill(
    close_price: float | None,
    target_amount: float = DEFAULT_TARGET_AMOUNT,
) -> dict[str, Any]:
    """Buy as many whole shares as possible without exceeding target_amount."""
    target = positive_number(target_amount) or DEFAULT_TARGET_AMOUNT
    price = positive_number(close_price)
    if price is None:
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
    last_price = positive_number(last_price)
    if shares <= 0 or cost <= 0 or last_price is None:
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
    trade_date = quote_trade_date(quote)
    if not valid_trade_date(trade_date) or positive_number(price) is None:
        raise QuoteDataError("报价缺少有效交易时间或价格，已保留原行情")
    observed_at = datetime.fromisoformat(str(quote["quote_time"]))
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    observed_at = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    if observed_at > now_shanghai():
        raise QuoteDataError("报价时间晚于当前时间，已保留原行情")
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
        "quote_time": str(quote.get("quote_time") or ""),
        "is_final": observed_at.hour >= 15,
    }


def _validated_quote(code: str, quote: dict[str, Any]) -> dict[str, Any]:
    if str(quote.get("code") or code) != code:
        raise QuoteDataError("报价代码不匹配，已保留原行情")
    if not valid_trade_date(quote.get("trade_date")) or positive_number(quote.get("close_price")) is None:
        raise QuoteDataError("报价缺少有效交易日期或价格，已保留原行情")
    return {
        **quote,
        "code": code,
        "is_final": bool(quote.get("is_final", str(quote["trade_date"]) <= latest_session_date(completed=True))),
        **{field: finite_number(quote.get(field)) for field in (
            "open_price", "close_price", "high_price", "low_price",
            "change_amount", "change_percent", "volume",
        )},
    }


def _validated_history(code: str, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = []
    for quote in history:
        try:
            valid.append(_validated_quote(code, quote))
        except ValueError:
            continue
    if not valid:
        raise QuoteDataError("未取得有效的历史行情，已保留原数据")
    return valid


def refresh_paper_fill_for_stock(con: Any, code: str) -> dict[str, Any] | None:
    stock = store.get_stock(con, code)
    if stock is None:
        return None
    buy_date = str(stock.get("buy_date") or "").strip()
    target = float(stock.get("target_amount") or DEFAULT_TARGET_AMOUNT)
    if not buy_date:
        return store.fill_stock_if_unchanged(
            con,
            stock,
            buy_price=None,
            buy_shares=0,
            buy_amount=0,
        )
    if (positive_number(stock.get("buy_price")) is not None
            and int(stock.get("buy_shares") or 0) > 0
            and positive_number(stock.get("buy_amount")) is not None):
        # A saved fill is an observation. Only an explicit edit resets it.
        return stock
    quote = store.quote_on_date(con, code, buy_date)
    close_price = None if quote is None else quote.get("close_price")
    if not valid_trade_date(buy_date) or buy_date > latest_session_date(completed=True):
        return stock
    if positive_number(close_price) is None or not quote.get("is_final", True):
        # An incomplete download must not erase a previously saved fill.
        return stock
    fill = compute_paper_fill(close_price, target)
    return store.fill_stock_if_unchanged(
        con,
        stock,
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


def _stock_row(
    stock: dict[str, Any],
    latest: dict[str, Any] | None,
    trade_dates: list[str],
    quote_map: dict[tuple[str, str], dict[str, Any]],
    *,
    expected_date: str,
    completed_date: str,
) -> dict[str, Any]:
    code = str(stock["code"])
    buy_date = str(stock.get("buy_date") or "").strip()
    buy_shares = int(stock.get("buy_shares") or 0)
    buy_amount = finite_number(stock.get("buy_amount")) or 0.0
    buy_price = positive_number(stock.get("buy_price"))
    has_fill = buy_shares > 0 and buy_amount > 0 and buy_price is not None
    latest_date = str((latest or {}).get("trade_date") or "")
    last_price = positive_number((latest or {}).get("close_price"))
    usable_quote = last_price is not None and latest_date >= buy_date
    quote_final = bool((latest or {}).get("is_final", True))
    quote_stale = bool(latest_date and (latest_date < expected_date or (
        not quote_final and latest_date <= completed_date
    )))
    fill_status, fill_hint = "filled", ""
    if not buy_date:
        fill_status, fill_hint = "no_buy_date", "未设置买入日"
    elif not valid_trade_date(buy_date):
        fill_status, fill_hint = "invalid_buy_date", "买入日期无效，请调整"
        usable_quote = False
    elif not has_fill:
        if buy_date > completed_date:
            fill_status, fill_hint = "awaiting_close", "买入日尚未收盘，收盘后更新行情"
        else:
            fill_status, fill_hint = "pending_fill", "买入日行情缺失，请回补历史"
            if buy_price is not None and buy_shares == 0:
                fill_hint = "股价高于模拟买入金额，请调整金额"
    elif not usable_quote:
        fill_status, fill_hint = "pending_quote", "缺少买入日及之后的有效报价"
    elif quote_stale:
        fill_hint = f"按 {latest_date} 最近有效价估值"

    pnl = compute_paper_pnl(
        buy_shares=buy_shares if has_fill else 0,
        buy_amount=buy_amount,
        last_price=last_price if usable_quote else None,
    )
    quotes = []
    for trade_date in trade_dates:
        quote = quote_map.get((code, trade_date))
        if quote is None or positive_number(quote.get("close_price")) is None:
            quotes.append(None)
        else:
            quotes.append({
                "trade_date": trade_date,
                **{field: finite_number(quote.get(field)) for field in (
                    "open_price", "close_price", "change_amount", "change_percent",
                )},
            })
    return {
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
        "target_amount": positive_number(stock.get("target_amount")) or DEFAULT_TARGET_AMOUNT,
        **pnl,
        "last_price": last_price,
        "last_quote_date": latest_date,
        "last_quote_time": (latest or {}).get("quote_time") or "",
        "quote_final": quote_final,
        "quote_stale": quote_stale,
        "price_change": round(last_price - buy_price, 4)
        if usable_quote and buy_price is not None else None,
        "has_fill": has_fill,
        "fill_status": fill_status,
        "fill_hint": fill_hint,
        "quotes": quotes,
    }


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    filled = [row for row in rows if row["has_fill"]]
    missing = sum(row["market_value"] is None for row in filled)
    cost = round(sum(row["buy_amount"] for row in filled), 2)
    market = None if missing else round(sum(row["market_value"] for row in filled), 2)
    pnl = None if market is None else round(market - cost, 2)
    return {
        "stock_count": len(rows),
        "filled_count": len(filled),
        "pending_count": sum(row["fill_status"] not in ("filled", "no_buy_date") for row in rows),
        "missing_quote_count": missing,
        "stale_count": sum(row["quote_stale"] for row in filled),
        "valuation_complete": missing == 0,
        "total_cost": cost,
        "total_market_value": market,
        "total_pnl": pnl,
        "total_pnl_percent": round(pnl / cost * 100, 2) if pnl is not None and cost > 0 else None,
    }


def build_board(
    *,
    days: int = DEFAULT_BOARD_DAYS,
    group_id: int | str | None = None,
    q: str = "",
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    days = clamp_board_days(days)

    def _build(con: Any) -> dict[str, Any]:
        groups = store.list_groups(con)
        all_stocks = store.list_stocks(con, active_only=True)
        selected_codes = {row["code"] for row in store.list_stocks(con, group_id=group_id, q=q)}
        valid_dates = [day for day in store.all_quote_dates(con) if valid_trade_date(day)]
        trade_dates = valid_dates[-days:]
        # Valuation always uses all valid history, regardless of visible columns.
        latest = store.latest_quotes_for_codes(con, [row["code"] for row in all_stocks], valid_dates)
        quote_map = store.quotes_for_codes(con, list(selected_codes), trade_dates)
        expected_date = latest_session_date()
        completed_date = latest_session_date(completed=True)
        all_rows = [_stock_row(
            stock, latest.get(stock["code"]), trade_dates, quote_map,
            expected_date=expected_date, completed_date=completed_date,
        ) for stock in all_stocks]
        rows = [row for row in all_rows if row["code"] in selected_codes]
        enriched_groups = [{
            **group,
            **_summarize_rows([row for row in all_rows if row["group_id"] == group["id"]]),
        } for group in groups]
        sections = []
        for gid, name in [(group["id"], group["name"]) for group in groups] + [(None, "未分组")]:
            section_rows = [row for row in rows if row["group_id"] == gid]
            if section_rows:
                sections.append({
                    "group_id": gid, "group_name": name,
                    **_summarize_rows(section_rows), "stocks": section_rows,
                })
        return {
            "days": days,
            "allowed_days": list(ALLOWED_BOARD_DAYS),
            "trade_dates": trade_dates,
            "latest_trade_date": max((row["last_quote_date"] for row in rows), default=""),
            "expected_trade_date": expected_date,
            "default_buy_date": completed_date,
            "group_id": group_id,
            "q": q,
            "groups": enriched_groups,
            "sections": sections,
            "stocks": rows,
            "total": len(rows),
            "total_all": len(all_rows),
            "ungrouped_count": sum(row["group_id"] is None for row in all_rows),
            "summary": {**_summarize_rows(rows), "target_amount_default": DEFAULT_TARGET_AMOUNT},
            "generated_at": now_shanghai().isoformat(timespec="seconds"),
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
    resolved_buy_date = validate_buy_date(buy_date) or latest_session_date(completed=True)
    target = validate_target_amount(target_amount)

    def _add(con: Any) -> dict[str, Any]:
        if group_id is not None and store.get_group(con, group_id) is None:
            raise KeyError(f"分组不存在: {group_id}")
        added = []
        failed = []
        skipped_existing = []
        for code in code_list:
            try:
                meta = normalize_code(code)
                inserted = store.insert_stock_if_missing(
                    con,
                    code=meta["code"],
                    market=meta["market"],
                    group_id=group_id,
                    note=note,
                    buy_date=resolved_buy_date,
                    target_amount=target,
                )
                if not inserted:
                    skipped_existing.append(meta["code"])
                    continue
                # best-effort history + name fill
                try:
                    history = _validated_history(meta["code"], fetch_history_quotes(meta["code"], max(backfill_days, 30)))
                    store.upsert_quotes(con, history, missing_only=True)
                    live = fetch_today_quote(meta["code"])
                    if live:
                        live = _validated_quote(meta["code"], live)
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
                                    "quote_time": live.get("quote_time") or "",
                                    "is_final": live["is_final"],
                                },
                                missing_only=False,
                            )
                            con.commit()
                except Exception as exc:  # network optional on add
                    failed.append({"code": meta["code"], "error": quote_error(exc)})
                refresh_paper_fill_for_stock(con, meta["code"])
                added.append(meta["code"])
            except Exception as exc:
                failed.append({"code": code, "error": quote_error(exc)})
        board = build_board(days=DEFAULT_BOARD_DAYS, group_id=group_id, db_path=db_path)
        return {
            "added": added,
            "skipped_existing": skipped_existing,
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
        current = store.get_stock(con, code)
        if current is None:
            raise KeyError(f"股票不存在: {code}")
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
            fields["buy_date"] = validate_buy_date(buy_date)
        if target_amount is not None:
            fields["target_amount"] = validate_target_amount(target_amount)
        if active is not None:
            fields["active"] = bool(active)
        reset_fill = any(key in fields and fields[key] != current[key]
                         for key in ("buy_date", "target_amount"))
        if reset_fill:
            fields.update(buy_price=None, buy_shares=0, buy_amount=0)
        if fields:
            store.update_stock_fields(con, code, **fields)
        if reset_fill:
            refresh_paper_fill_for_stock(con, code)
        stock = store.get_stock(con, code)
        if stock is None:
            raise KeyError(f"股票不存在: {code}")
        dates = [day for day in store.all_quote_dates(con) if valid_trade_date(day)]
        latest = store.latest_quotes_for_codes(con, [code], dates).get(code)
        row = _stock_row(
            stock, latest, [], {}, expected_date=latest_session_date(),
            completed_date=latest_session_date(completed=True),
        )
        return {**stock, **row}

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


def _job_stocks(con: Any, codes: list[str] | None) -> list[dict[str, Any]]:
    stocks = store.list_stocks(con, active_only=True)
    if codes is not None:
        wanted = set(parse_codes(codes))
        stocks = [item for item in stocks if item["code"] in wanted]
    return stocks


def quote_error(exc: Exception) -> str:
    if isinstance(exc, QuoteDataError):
        return str(exc)
    if "timeout" in type(exc).__name__.lower():
        return "行情请求超时，可重试"
    return f"行情处理失败（{type(exc).__name__}），可重试"


def _check_job_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop is not None and should_stop():
        raise JobInterruptedError("服务停止，未完成股票可重试")


def update_today_quotes(
    *,
    codes: list[str] | None = None,
    force: bool = False,
    db_path: Path | str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    include_board: bool = True,
    _job_reserved: bool = False,
) -> dict[str, Any]:
    if not _job_reserved:
        _acquire_job("update-today")
    try:
        def _run(con: Any) -> dict[str, Any]:
            stocks = _job_stocks(con, codes)
            updated = skipped_count = 0
            failed: list[dict[str, str]] = []
            trade_dates: set[str] = set()
            today = today_shanghai()
            for stock in stocks:
                _check_job_stop(should_stop)
                code = stock["code"]
                if progress:
                    progress({"code": code, "status": "running"})
                event = {"code": code, "status": "succeeded"}
                try:
                    raw = fetch_today_quote(code)
                    if not raw:
                        raise QuoteDataError("暂无有效报价，已保留原行情")
                    live = _validated_quote(code, raw)
                    _check_job_stop(should_stop)
                    trade_date = str(live["trade_date"])
                    trade_dates.add(trade_date)
                    if not force and trade_date != today:
                        skipped_count += 1
                        event["status"] = "skipped"
                        event["reason"] = f"最新报价为 {trade_date}，未写入今日行情"
                    else:
                        if live.get("name") and not stock.get("name"):
                            store.update_stock_fields(con, code, name=live["name"])
                        written = store.upsert_quote(con, live, missing_only=False)
                        con.commit()
                        refresh_paper_fill_for_stock(con, code)
                        if written:
                            updated += 1
                        else:
                            skipped_count += 1
                            event["status"] = "skipped"
                            event["reason"] = "已有完整的收盘行情，保留原数据"
                except JobInterruptedError:
                    raise
                except Exception as exc:
                    con.rollback()
                    item = {"code": code, "error": quote_error(exc)}
                    failed.append(item)
                    event.update(status="failed", error=item["error"])
                if progress:
                    progress(event)
            result = {
                "updated": updated,
                "failed": failed,
                "skipped": bool(skipped_count and not updated),
                "skipped_count": skipped_count,
                "trade_dates": sorted(trade_dates),
            }
            if include_board:
                result["board"] = build_board(db_path=db_path)
            return result

        return _with_conn(db_path, _run)
    finally:
        if not _job_reserved:
            _release_job()


def backfill_quotes(
    *,
    days: int = 60,
    codes: list[str] | None = None,
    missing_only: bool = True,
    db_path: Path | str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    include_board: bool = True,
    _job_reserved: bool = False,
) -> dict[str, Any]:
    days = validate_backfill_days(days)
    if not _job_reserved:
        _acquire_job("backfill")
    try:
        def _run(con: Any) -> dict[str, Any]:
            stocks = _job_stocks(con, codes)
            quote_count = succeeded = skipped_count = 0
            failed: list[dict[str, str]] = []
            for stock in stocks:
                _check_job_stop(should_stop)
                code = stock["code"]
                if progress:
                    progress({"code": code, "status": "running"})
                event: dict[str, Any] = {"code": code, "status": "succeeded"}
                try:
                    history = _validated_history(code, fetch_history_quotes(code, days))
                    _check_job_stop(should_stop)
                    written = store.upsert_quotes(con, history, missing_only=missing_only)
                    quote_count += written
                    refresh_paper_fill_for_stock(con, code)
                    if written:
                        succeeded += 1
                    else:
                        skipped_count += 1
                        event["status"] = "skipped"
                        event["reason"] = "历史行情已完整，无需重复写入"
                    event["quotes"] = written
                except JobInterruptedError:
                    raise
                except Exception as exc:
                    con.rollback()
                    item = {"code": code, "error": quote_error(exc)}
                    failed.append(item)
                    event.update(status="failed", error=item["error"])
                if progress:
                    progress(event)
            result = {
                "days": days, "stocks": len(stocks), "quotes": quote_count,
                "succeeded": succeeded, "skipped_count": skipped_count,
                "missing_only": missing_only, "failed": failed,
            }
            if include_board:
                result["board"] = build_board(days=min(days, 60), db_path=db_path)
            return result

        return _with_conn(db_path, _run)
    finally:
        if not _job_reserved:
            _release_job()
