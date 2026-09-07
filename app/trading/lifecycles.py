"""Pure zero-position to zero-position accounting shared by live reports."""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from typing import Any

from .accounting import trade_counts_for_account

COMPLETE_TRADE_DEFINITION = (
    "verified_zero_to_zero_net_of_all_buy_and_sell_fees; "
    "first_entry_strategy; partial_sells_and_adds_are_one_trade; "
    "open_or_unverified_cycles_excluded; breakeven_in_denominator"
)

def _trade_identity(trade: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the same stable fill identity used by the practice ledger."""
    return tuple(
        json.dumps(trade.get(field, ""), ensure_ascii=False, sort_keys=True)
        for field in ("time", "action", "code", "shares", "price", "reason")
    )


def _date_value(value: Any, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        raise ValueError(f"{field_name} must use YYYY-MM-DD") from None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _shares(value: Any) -> int:
    number = _number(value)
    if number is None or number <= 0 or not float(number).is_integer():
        return 0
    return int(number)


def _optional_quantity(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0 or not float(number).is_integer():
        return None
    return int(number)


def _entry_context(trade: Mapping[str, Any]) -> dict[str, Any]:
    context = trade.get("niuone_entry_context")
    return dict(context) if isinstance(context, Mapping) else {}


def _exit_context(trade: Mapping[str, Any]) -> dict[str, Any]:
    context = trade.get("niuone_lifecycle_evidence")
    return dict(context) if isinstance(context, Mapping) else {}


def _buy_cost(trade: Mapping[str, Any]) -> float | None:
    explicit = _number(trade.get("total_cost"))
    if explicit is not None and explicit > 0:
        return explicit
    amount = _number(trade.get("amount"))
    fee = _number(trade.get("fee"))
    if fee is None:
        fee = sum(_number(trade.get(key)) or 0.0 for key in ("commission", "transfer_fee", "stamp_duty"))
    if amount is None or amount <= 0:
        return None
    return amount + fee


def _sell_proceeds(trade: Mapping[str, Any]) -> float | None:
    explicit = _number(trade.get("net_proceeds"))
    if explicit is not None and explicit >= 0:
        return explicit
    amount = _number(trade.get("amount"))
    fee = _number(trade.get("fee"))
    if fee is None:
        fee = sum(_number(trade.get(key)) or 0.0 for key in ("commission", "transfer_fee", "stamp_duty"))
    if amount is None or amount <= 0 or amount < fee:
        return None
    return amount - fee


def reconstruct_trade_lifecycles(
    trades: Iterable[Mapping[str, Any]],
    *,
    as_of: str | date | None = None,
    strategy_resolver: Callable[[Mapping[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Rebuild facts without inferring missing opening fills or changing inputs."""
    cutoff = _date_value(as_of or date.max, field_name="as_of")
    strategy_resolver = strategy_resolver or (lambda row: str(
        row.get("buy_strategy") or row.get("entry_strategy") or "unknown"
    ))
    normalized: list[tuple[date, int, Mapping[str, Any]]] = []
    seen_trade_ids: set[tuple[str, ...]] = set()
    duplicate_trade_count = 0
    invalid_timestamp_count = 0
    inactive_accounting_trade_count = 0
    for index, trade in enumerate(trades):
        if not isinstance(trade, Mapping):
            invalid_timestamp_count += 1
            continue
        if not trade_counts_for_account(trade):
            inactive_accounting_trade_count += 1
            continue
        identity = _trade_identity(trade)
        if identity in seen_trade_ids:
            duplicate_trade_count += 1
            continue
        seen_trade_ids.add(identity)
        try:
            trade_date = _date_value(trade.get("time"), field_name="trade time")
        except ValueError:
            invalid_timestamp_count += 1
            continue
        if trade_date <= cutoff:
            normalized.append((trade_date, index, trade))
    normalized.sort(key=lambda item: (item[0], str(item[2].get("time") or ""), item[1]))

    active: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    orphan_sell_count = 0
    invalid_trade_count = 0
    oversold_lifecycle_count = 0
    unverified_open_count = 0
    inconsistent_quantity_count = 0
    for trade_date, _index, trade in normalized:
        action = str(trade.get("action") or "").upper()
        code = str(trade.get("code") or "").strip()
        if action not in {"BUY", "SELL"} or not code:
            continue
        quantity = _shares(trade.get("shares"))
        if quantity <= 0:
            invalid_trade_count += 1
            if code in active:
                active[code]["verified_open"] = False
            continue
        if action == "BUY":
            cost = _buy_cost(trade)
            if cost is None:
                invalid_trade_count += 1
                if code in active:
                    active[code]["verified_open"] = False
                continue
            before_quantity = _optional_quantity(
                trade.get("position_before_qty")
            )
            after_quantity = _optional_quantity(
                trade.get("position_after_qty")
            )
            lifecycle = active.get(code)
            if lifecycle is None:
                verified_open = before_quantity == 0
                if not verified_open:
                    unverified_open_count += 1
                lifecycle = {
                    "entry_date": trade_date,
                    "entry_time": str(trade.get("time") or ""),
                    "entry_strategy": strategy_resolver(trade),
                    "code": code,
                    "name": str(trade.get("name") or ""),
                    "entry_context": _entry_context(trade),
                    "entry_fill_shares": quantity,
                    "entry_payload_available": trade.get(
                        "_forward_payload_available"
                    ),
                    "exit_context": {},
                    "exit_payload_available": None,
                    "quantity": before_quantity or 0,
                    "buy_cost": 0.0,
                    "sell_proceeds": 0.0,
                    "transaction_count": 0,
                    "verified_open": verified_open,
                }
                active[code] = lifecycle
            elif (
                before_quantity is not None
                and before_quantity != int(lifecycle["quantity"])
            ):
                lifecycle["verified_open"] = False
                inconsistent_quantity_count += 1
            expected_after = int(lifecycle["quantity"]) + quantity
            if after_quantity is not None and after_quantity != expected_after:
                lifecycle["verified_open"] = False
                inconsistent_quantity_count += 1
            lifecycle["quantity"] = (
                after_quantity if after_quantity is not None else expected_after
            )
            lifecycle["buy_cost"] += cost
            lifecycle["transaction_count"] += 1
            continue

        lifecycle = active.get(code)
        if lifecycle is None:
            orphan_sell_count += 1
            continue
        proceeds = _sell_proceeds(trade)
        if proceeds is None:
            invalid_trade_count += 1
            if code in active:
                active[code]["verified_open"] = False
            continue
        before_quantity = _optional_quantity(trade.get("position_before_qty"))
        after_quantity = _optional_quantity(trade.get("position_after_qty"))
        if (
            before_quantity is not None
            and before_quantity != int(lifecycle["quantity"])
        ):
            lifecycle["verified_open"] = False
            inconsistent_quantity_count += 1
        if quantity > int(lifecycle["quantity"]):
            oversold_lifecycle_count += 1
            active.pop(code, None)
            continue
        expected_after = int(lifecycle["quantity"]) - quantity
        if after_quantity is not None and after_quantity != expected_after:
            lifecycle["verified_open"] = False
            inconsistent_quantity_count += 1
        lifecycle["quantity"] = (
            after_quantity if after_quantity is not None else expected_after
        )
        lifecycle["sell_proceeds"] += proceeds
        lifecycle["transaction_count"] += 1
        lifecycle["exit_context"] = _exit_context(trade)
        lifecycle["exit_payload_available"] = trade.get(
            "_forward_payload_available"
        )
        if lifecycle["quantity"] > 0:
            continue

        active.pop(code, None)
        entry_date = lifecycle["entry_date"]
        entry_strategy = str(lifecycle["entry_strategy"] or "")
        buy_cost = float(lifecycle["buy_cost"])
        if (
            buy_cost <= 0
            or lifecycle["verified_open"] is not True
        ):
            continue
        realized_pnl = round(float(lifecycle["sell_proceeds"]) - buy_cost, 2)
        context = lifecycle["entry_context"]
        completed.append({
            "code": code,
            "name": lifecycle["name"],
            "entry_date": entry_date.isoformat(),
            "entry_time": lifecycle["entry_time"],
            "exit_date": trade_date.isoformat(),
            "exit_time": str(trade.get("time") or ""),
            "entry_strategy": entry_strategy,
            "net_return_pct": realized_pnl / buy_cost * 100.0,
            "realized_pnl": realized_pnl,
            "holding_calendar_days": (trade_date - entry_date).days,
            "transaction_count": int(lifecycle["transaction_count"]),
            "entry_context": context,
            "entry_fill_shares": lifecycle["entry_fill_shares"],
            "entry_payload_available": lifecycle[
                "entry_payload_available"
            ],
            "exit_context": lifecycle["exit_context"],
            "exit_payload_available": lifecycle[
                "exit_payload_available"
            ],
        })

    return {
        "normalized": normalized, "active": active, "completed": completed,
        "coverage": {
            "duplicate_trade_count": duplicate_trade_count,
            "invalid_timestamp_count": invalid_timestamp_count,
            "inactive_accounting_trade_count": inactive_accounting_trade_count,
            "orphan_sell_count": orphan_sell_count,
            "invalid_trade_count": invalid_trade_count,
            "oversold_lifecycle_count": oversold_lifecycle_count,
            "unverified_open_count": unverified_open_count,
            "inconsistent_quantity_count": inconsistent_quantity_count,
        },
    }
