"""Watchlist trend board + paper P&L FastAPI routes."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.trading import watchlist_tracker_service as tracker


LimitEnforcer = Callable[[Request], Awaitable[Response | None]]
JsonResponder = Callable[..., Response]
AdminActionGuard = Callable[..., Awaitable[Response | None]]


def _error_response(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": message},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def create_watchlist_tracker_router(
    *,
    enforce_api_limits: LimitEnforcer,
    require_admin_action: AdminActionGuard,
    json_response: JsonResponder,
) -> APIRouter:
    """Create public board reads and admin-protected watchlist mutations."""

    router = APIRouter(include_in_schema=False)

    async def _guarded_json(
        request: Request,
        producer: Callable[[], dict[str, Any]],
        *,
        require_admin: bool = False,
    ) -> Response:
        if require_admin:
            rejected = await require_admin_action(request)
            if rejected is not None:
                return rejected
        else:
            limited = await enforce_api_limits(request)
            if limited is not None:
                return limited
        if request.method == "HEAD":
            return Response(
                status_code=200,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        try:
            payload = await run_in_threadpool(producer)
        except tracker.JobConflictError as exc:
            return _error_response(str(exc), status_code=409)
        except KeyError as exc:
            return _error_response(str(exc), status_code=404)
        except ValueError as exc:
            return _error_response(str(exc), status_code=400)
        except Exception as exc:
            return _error_response(str(exc), status_code=500)
        return json_response(request, payload, cache_control="no-store")

    @router.api_route("/api/watchlist/board", methods=["GET", "HEAD"])
    async def board(request: Request) -> Response:
        days = tracker.clamp_board_days(request.query_params.get("days"))
        group_raw = str(request.query_params.get("group_id") or "").strip()
        group_id = int(group_raw) if group_raw.isdigit() else None
        q = str(request.query_params.get("q") or "").strip()

        def producer() -> dict[str, Any]:
            return tracker.build_board(days=days, group_id=group_id, q=q)

        return await _guarded_json(request, producer)

    @router.api_route("/api/watchlist/groups", methods=["GET", "HEAD"])
    async def groups(request: Request) -> Response:
        def producer() -> dict[str, Any]:
            return {"groups": tracker.list_groups()}

        return await _guarded_json(request, producer)

    @router.post("/api/watchlist/groups")
    async def create_group(request: Request) -> Response:
        body = await _read_json(request)

        def producer() -> dict[str, Any]:
            group = tracker.create_group(
                name=str(body.get("name") or ""),
                note=str(body.get("note") or ""),
            )
            return {"group": group, "groups": tracker.list_groups()}

        return await _guarded_json(request, producer, require_admin=True)

    @router.api_route("/api/watchlist/groups/{group_id}", methods=["PATCH"])
    async def patch_group(request: Request, group_id: int) -> Response:
        body = await _read_json(request)

        def producer() -> dict[str, Any]:
            group = tracker.update_group(
                group_id,
                name=None if "name" not in body else str(body.get("name") or ""),
                note=None if "note" not in body else str(body.get("note") or ""),
                sort_order=(
                    None
                    if "sort_order" not in body
                    else int(body.get("sort_order") or 0)
                ),
            )
            return {"group": group, "groups": tracker.list_groups()}

        return await _guarded_json(request, producer, require_admin=True)

    @router.api_route("/api/watchlist/groups/{group_id}", methods=["DELETE"])
    async def remove_group(request: Request, group_id: int) -> Response:
        def producer() -> dict[str, Any]:
            tracker.delete_group(group_id)
            return {"deleted": group_id, "groups": tracker.list_groups()}

        return await _guarded_json(request, producer, require_admin=True)

    @router.post("/api/watchlist/stocks")
    async def add_stocks(request: Request) -> Response:
        body = await _read_json(request)
        group_raw = body.get("group_id")
        group_id = None
        if group_raw not in (None, ""):
            group_id = int(group_raw)
        target_raw = body.get("target_amount")
        target_amount = (
            float(target_raw)
            if target_raw not in (None, "")
            else tracker.DEFAULT_TARGET_AMOUNT
        )

        def producer() -> dict[str, Any]:
            return tracker.add_stocks(
                codes=body.get("codes") or body.get("code") or "",
                group_id=group_id,
                note=str(body.get("note") or ""),
                buy_date=None if body.get("buy_date") in (None, "") else str(body.get("buy_date")),
                target_amount=target_amount,
            )

        return await _guarded_json(request, producer, require_admin=True)

    @router.api_route("/api/watchlist/stocks/{code}", methods=["PATCH"])
    async def patch_stock(request: Request, code: str) -> Response:
        body = await _read_json(request)

        def producer() -> dict[str, Any]:
            kwargs: dict[str, Any] = {}
            if "name" in body:
                kwargs["name"] = str(body.get("name") or "")
            if "note" in body:
                kwargs["note"] = str(body.get("note") or "")
            if "group_id" in body:
                kwargs["group_id"] = body.get("group_id")
            if "buy_date" in body:
                kwargs["buy_date"] = str(body.get("buy_date") or "")
            if "target_amount" in body:
                kwargs["target_amount"] = float(
                    body.get("target_amount") or tracker.DEFAULT_TARGET_AMOUNT
                )
            if "active" in body:
                kwargs["active"] = bool(body.get("active"))
            stock = tracker.update_stock(code, **kwargs)
            return {"stock": stock, "board": tracker.build_board()}

        return await _guarded_json(request, producer, require_admin=True)

    @router.api_route("/api/watchlist/stocks/{code}", methods=["DELETE"])
    async def remove_stock(request: Request, code: str) -> Response:
        def producer() -> dict[str, Any]:
            tracker.delete_stock(code)
            return {"deleted": code, "board": tracker.build_board()}

        return await _guarded_json(request, producer, require_admin=True)

    @router.post("/api/watchlist/quotes/update-today")
    async def update_today(request: Request) -> Response:
        body = await _read_json(request)
        codes = body.get("codes")
        force = bool(body.get("force"))

        def producer() -> dict[str, Any]:
            return tracker.update_today_quotes(
                codes=codes if isinstance(codes, list) else None,
                force=force,
            )

        return await _guarded_json(request, producer, require_admin=True)

    @router.post("/api/watchlist/quotes/backfill")
    async def backfill(request: Request) -> Response:
        body = await _read_json(request)
        days = int(body.get("days") or 60)
        missing_only = body.get("missing_only")
        if missing_only is None:
            missing_only = True
        codes = body.get("codes")

        def producer() -> dict[str, Any]:
            return tracker.backfill_quotes(
                days=days,
                codes=codes if isinstance(codes, list) else None,
                missing_only=bool(missing_only),
            )

        return await _guarded_json(request, producer, require_admin=True)

    return router
