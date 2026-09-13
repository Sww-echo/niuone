"""Read-only, per-fill realized returns for the current holding cycle."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .accounting import trade_counts_for_account
from .lifecycles import (
    _buy_cost, _number, _optional_quantity, _sell_proceeds, _shares,
    _trade_identity,
)


REALIZED_RETURN_FIELDS = (
    "cumulative_realized_pnl", "realized_return_pct", "realized_buy_cost",
    "realized_return_status", "realized_cycle_key",
)


def annotate_realized_returns(
    trades: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Copy fills with cumulative realized P&L / all buy costs as of each sell.

    An explicit zero-position opening and a consistent quantity chain are
    required. Recorded fill P&L stays authoritative; no ledger fields change.
    """
    retained: dict[tuple[str, ...], dict[str, Any]] = {}
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        key = _trade_identity(trade)
        if key not in retained or not trade_counts_for_account(trade):
            retained[key] = dict(trade)
    rows = sorted(retained.values(), key=lambda row: str(row.get("time") or ""))
    active: dict[str, dict[str, Any]] = {}
    for row in rows:
        for field in REALIZED_RETURN_FIELDS:
            row.pop(field, None)
        if not trade_counts_for_account(row):
            continue
        action = str(row.get("action") or "").upper()
        code = str(row.get("code") or "").strip()
        if action not in {"BUY", "SELL"} or not code:
            continue
        if action == "SELL":
            row.update({
                "cumulative_realized_pnl": None,
                "realized_return_pct": None,
                "realized_buy_cost": None,
                "realized_return_status": "incomplete_history",
            })
        try:
            datetime.fromisoformat(str(row.get("time") or ""))
        except ValueError:
            active.pop(code, None)
            continue
        quantity = _shares(row.get("shares"))
        before = _optional_quantity(row.get("position_before_qty"))
        after = _optional_quantity(row.get("position_after_qty"))
        cycle = active.get(code)
        if action == "BUY" and (before == 0 or row.get("position_opened") is True):
            cycle = {
                "key": f"{code}:{row['time']}", "quantity": 0,
                "buy_cost": 0.0, "remaining_cost": 0.0, "pnl": 0.0,
                "id": str(row.get("position_lifecycle_id") or ""),
            }
            active[code] = cycle
        if cycle is None:
            continue
        lifecycle_id = str(row.get("position_lifecycle_id") or "")
        expected_after = cycle["quantity"] + (quantity if action == "BUY" else -quantity)
        if (
            quantity <= 0 or expected_after < 0
            or (before is not None and before != cycle["quantity"])
            or (after is not None and after != expected_after)
            or (lifecycle_id and cycle["id"] and lifecycle_id != cycle["id"])
            or (row.get("position_fully_closed") is True and expected_after != 0)
        ):
            active.pop(code, None)
            continue
        cycle["id"] = lifecycle_id or cycle["id"]
        if action == "BUY":
            cost = _buy_cost(row)
            if cost is None or cost <= 0:
                active.pop(code, None)
                continue
            cycle["buy_cost"] += cost
            cycle["remaining_cost"] += cost
        else:
            proceeds = _sell_proceeds(row)
            if proceeds is None:
                active.pop(code, None)
                continue
            pnl = _number(row.get("pnl"))
            if pnl is None:
                # Legacy fills without P&L can use the verified cost chain.
                cost = cycle["remaining_cost"] * quantity / cycle["quantity"]
                pnl = proceeds - cost
            cycle["pnl"] += pnl
            cycle["remaining_cost"] -= proceeds - pnl
            row.update({
                "cumulative_realized_pnl": round(cycle["pnl"], 2),
                "realized_return_pct": round(cycle["pnl"] / cycle["buy_cost"] * 100, 2),
                "realized_buy_cost": round(cycle["buy_cost"], 2),
                "realized_return_status": "verified",
                "realized_cycle_key": cycle["key"],
            })
        cycle["quantity"] = expected_after
        if expected_after == 0:
            active.pop(code, None)
    return rows
