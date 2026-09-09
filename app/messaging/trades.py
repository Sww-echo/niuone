"""Trade execution notification formatting and delivery entry point."""
from __future__ import annotations

import html
import math
import re
from typing import Any, Callable, Iterable, Mapping

from . import dispatcher as _dispatcher
from .models import Clock, DeliveryResult, JsonTransport, Notification
from .transport import _sanitized_error


_MAX_TRADE_REASON_CHARS = 1000


def _clean_trade_text(value: Any, max_chars: int = 120) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= max_chars else text[: max(1, max_chars - 1)].rstrip() + "…"


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _position_quantity(value: Any) -> int | None:
    number = _finite_float(value)
    if isinstance(value, bool) or number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _sell_position_details(
    trade: Mapping[str, Any], shares: int,
) -> tuple[str | None, int | None]:
    """Resolve reduction against all held shares, without rounded weight guesses."""
    before = _position_quantity(trade.get("position_before_qty"))
    after = _position_quantity(trade.get("position_after_qty"))
    if shares <= 0 or _position_quantity(trade.get("shares")) != shares or any(
        trade.get(field) is not None and value is None
        for field, value in (("position_before_qty", before), ("position_after_qty", after))
    ):
        return None, None
    if before is not None:
        expected_after = before - shares
        if expected_after < 0 or (after is not None and after != expected_after):
            return None, None
        after = expected_after
    elif after is not None:
        before = shares + after
    elif trade.get("position_fully_closed") is True:
        before, after = shares, 0
    else:
        return None, None
    if (
        (trade.get("position_fully_closed") is True and after != 0)
        or (trade.get("position_fully_closed") is False and after == 0)
    ):
        return None, None
    if after == 0:
        return "100%", 0
    ratio = shares / before * 100
    displayed = round(ratio, 2)
    ratio_text = "<100%" if displayed >= 100 else "<0.01%" if displayed <= 0 else f"{ratio:.2f}%"
    return ratio_text, after


def _money(value: Any) -> str:
    number = _finite_float(value)
    return f"¥{number:,.2f}" if number is not None else "-"


def _price(value: Any) -> str:
    number = _finite_float(value)
    return f"¥{number:,.3f}" if number is not None else "-"


def _percentage(value: Any) -> str:
    number = _finite_float(value)
    return f"{number:.2f}%" if number is not None else "-"


def _signed_money(value: float) -> str:
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}¥{abs(value):,.2f}"


def _signed_percentage(value: float) -> str:
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}{abs(value):.2f}%"


def _markdown_text(value: Any) -> str:
    """Escape untrusted trade labels without changing their rendered text."""

    text = html.escape(str(value or ""), quote=False)
    return re.sub(r"([\\`*_\[\]~#])", r"\\\1", text)


def _append_card_field(
    card_fields: list[dict[str, Any]],
    label: str,
    value: str,
    *,
    short: bool = True,
    color: str = "",
) -> None:
    card_field = {
        "label": label,
        "value": value,
        "short": short,
    }
    if color:
        card_field["color"] = color
    card_fields.append(card_field)


def _append_rich_field(
    plain_lines: list[str],
    markdown_lines: list[str],
    html_lines: list[str],
    card_fields: list[dict[str, Any]],
    label: str,
    value: str,
    *,
    short: bool = True,
    color: str = "",
    include_card: bool = True,
    card_label: str = "",
) -> None:
    plain_lines.append(f"{label}：{value}")
    markdown_value = _markdown_text(value)
    html_label = html.escape(label, quote=False)
    html_value = html.escape(value, quote=False)
    if color in {"red", "green"}:
        markdown_lines.append(f"**{label}**　**{markdown_value}**  ")
        html_lines.append(f"<b>{html_label}　{html_value}</b>")
    else:
        markdown_lines.append(f"**{label}**　{markdown_value}  ")
        html_lines.append(f"<b>{html_label}</b>　{html_value}")
    if include_card:
        _append_card_field(
            card_fields,
            card_label or label,
            value,
            short=short,
            color=color,
        )


def _trade_notification(trades: Iterable[Mapping[str, Any]]) -> Notification | None:
    normalized: list[Mapping[str, Any]] = []
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        if str(trade.get("action") or "").strip().upper() in {"BUY", "SELL"}:
            normalized.append(trade)
    if not normalized:
        return None

    plain_lines: list[str] = []
    markdown_lines: list[str] = []
    html_lines: list[str] = []
    card_sections: list[dict[str, Any]] = []
    actions: list[str] = []
    for index, trade in enumerate(normalized, 1):
        action = str(trade.get("action") or "").strip().upper()
        actions.append(action)
        label = "买入" if action == "BUY" else "卖出"
        name = _clean_trade_text(trade.get("name"), 32) or "未知股票"
        code = _clean_trade_text(trade.get("code"), 24) or "-"
        try:
            shares = int(float(trade.get("shares") or 0))
        except (TypeError, ValueError, OverflowError):
            shares = 0

        sell_ratio, remaining_shares = (
            _sell_position_details(trade, shares) if action == "SELL" else (None, None)
        )
        if sell_ratio is not None:
            label = f"卖出{sell_ratio}持仓"
            if remaining_shares == 0:
                label = f"清仓 · {label}"
        heading = f"{index}. {label}｜{name}（{code}）"
        card_fields: list[dict[str, Any]] = []
        if plain_lines:
            plain_lines.append("")
            markdown_lines.append("")
            html_lines.append("")
        plain_lines.append(heading)
        markdown_lines.append(f"#### {_markdown_text(heading)}")
        html_lines.append(f"<b>{html.escape(heading, quote=False)}</b>")

        if action == "SELL":
            _append_rich_field(
                plain_lines, markdown_lines, html_lines, card_fields,
                "卖出比例（占该股持仓）", sell_ratio or "暂不可用",
                color="green", short=False,
            )
            if remaining_shares is not None:
                _append_rich_field(
                    plain_lines, markdown_lines, html_lines, card_fields,
                    "卖后持股", f"{remaining_shares:,} 股", short=False,
                )
        _append_rich_field(
            plain_lines,
            markdown_lines,
            html_lines,
            card_fields,
            "成交",
            f"{shares:,} 股 × {_price(trade.get('price'))}",
            include_card=False,
        )
        _append_card_field(card_fields, "成交数量", f"{shares:,} 股")
        _append_card_field(card_fields, "成交价格", _price(trade.get("price")))
        _append_rich_field(
            plain_lines,
            markdown_lines,
            html_lines,
            card_fields,
            "金额",
            _money(trade.get("amount")),
            card_label="成交金额",
        )
        order_position_pct = trade.get("order_position_pct")
        order_position_number = _finite_float(order_position_pct)
        if order_position_number is not None:
            _append_rich_field(
                plain_lines,
                markdown_lines,
                html_lines,
                card_fields,
                "卖出仓位（占总资产）" if action == "SELL" else "本笔成交仓位",
                _percentage(order_position_pct),
                color="green" if action == "SELL" else "red" if order_position_number > 10 else "",
            )
        if action == "SELL":
            pnl = _finite_float(trade.get("cumulative_realized_pnl"))
            pnl_pct = _finite_float(trade.get("realized_return_pct"))
            pnl_text = "暂不可用（持仓历史不完整）"
            if pnl is not None:
                pnl_text = _signed_money(pnl)
                if pnl_pct is not None:
                    pnl_text += f"（{_signed_percentage(pnl_pct)}）"
            _append_rich_field(
                plain_lines,
                markdown_lines,
                html_lines,
                card_fields,
                "已实现盈亏 / 收益率",
                pnl_text,
                short=False,
            )
        trade_time = _clean_trade_text(trade.get("time"), 32)
        if trade_time:
            _append_rich_field(
                plain_lines,
                markdown_lines,
                html_lines,
                card_fields,
                "时间",
                trade_time,
                short=False,
            )

        strategy = _clean_trade_text(
            trade.get("exit_rule") if action == "SELL" else trade.get("buy_strategy"),
            60,
        )
        reason = _clean_trade_text(trade.get("reason"), _MAX_TRADE_REASON_CHARS)
        if strategy:
            _append_rich_field(
                plain_lines,
                markdown_lines,
                html_lines,
                card_fields,
                "策略",
                strategy,
                short=False,
            )
        if reason:
            _append_rich_field(
                plain_lines,
                markdown_lines,
                html_lines,
                card_fields,
                "理由",
                reason,
                short=False,
            )
        card_sections.append({
            "title": heading,
            "sequence": index,
            "action": action,
            "action_label": label,
            "emphasize_action": sell_ratio is not None,
            "name": name,
            "code": code,
            "fields": tuple(card_fields),
        })

    count = len(normalized)
    return Notification(
        event_type="trade.executed",
        title=f"成交信息（{count}笔）",
        text="\n".join(plain_lines),
        metadata={"trade_count": count, "actions": tuple(actions)},
        markdown="\n".join(markdown_lines),
        html="\n".join(html_lines),
        card_sections=tuple(card_sections),
    )


TradeDispatcher = Callable[..., list[DeliveryResult]]
_DISPATCH_UNSET = object()


def notify_trade_executions(
    trades: Iterable[Mapping[str, Any]],
    env: Mapping[str, Any] | None = None,
    *,
    transport: JsonTransport | None = None,
    clock: Clock | None = None,
    _dispatch: TradeDispatcher | object = _DISPATCH_UNSET,
) -> list[DeliveryResult]:
    """Format a persisted BUY/SELL batch and dispatch it to configured channels."""

    try:
        notification = _trade_notification(trades)
    except Exception as exc:
        return [DeliveryResult("notification", False, _sanitized_error(exc))]
    if notification is None:
        return []
    try:
        selected_dispatch = _dispatcher.dispatch if _dispatch is _DISPATCH_UNSET else _dispatch
        return selected_dispatch(notification, env, transport=transport, clock=clock)
    except Exception as exc:  # final safety boundary for callers in trading code
        return [DeliveryResult("notification", False, _sanitized_error(exc))]


__all__ = ["notify_trade_executions"]
