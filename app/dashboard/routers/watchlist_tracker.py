"""Watchlist trend board + paper P&L FastAPI routes."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.trading import watchlist_tracker_service as tracker
from app.dashboard.watchlist_jobs import WatchlistJobManager


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


def _group_id(value: Any, *, allow_ungrouped: bool = False) -> int | str | None:
    if value in (None, ""):
        return None
    if allow_ungrouped and value == "ungrouped":
        return "ungrouped"
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError("分组须为有效的分组编号")
    return int(value)


def _codes(body: dict[str, Any]) -> list[str] | None:
    codes = body.get("codes")
    if codes is not None and (not isinstance(codes, list) or not codes or any(
        not isinstance(code, str) or not tracker.CODE_RE.fullmatch(code) for code in codes
    )):
        raise ValueError("股票代码须为非空的六位代码列表")
    return codes


def _boolean(body: dict[str, Any], key: str, default: bool) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 须为布尔值")
    return value


def create_watchlist_tracker_router(
    *,
    enforce_api_limits: LimitEnforcer,
    require_admin_action: AdminActionGuard,
    json_response: JsonResponder,
    job_manager: WatchlistJobManager | None = None,
) -> APIRouter:
    """Create public board reads and admin-protected watchlist mutations."""

    router = APIRouter(include_in_schema=False)
    jobs = job_manager or WatchlistJobManager()

    async def _guarded_json(
        request: Request,
        producer: Callable[[], dict[str, Any]],
        *,
        require_admin: bool = False,
        status_code: int = 200,
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
        except (ValueError, TypeError, OverflowError) as exc:
            return _error_response(str(exc), status_code=400)
        except Exception as exc:
            return _error_response(str(exc), status_code=500)
        return json_response(request, payload, cache_control="no-store", status_code=status_code)

    @router.api_route("/api/watchlist/board", methods=["GET", "HEAD"])
    async def board(request: Request) -> Response:
        def producer() -> dict[str, Any]:
            days = tracker.clamp_board_days(int(request.query_params.get("days") or 5))
            group_id = _group_id(request.query_params.get("group_id"), allow_ungrouped=True)
            q = str(request.query_params.get("q") or "").strip()
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
        def producer() -> dict[str, Any]:
            group_id = _group_id(body.get("group_id"))
            target_raw = body.get("target_amount")
            target_amount = tracker.validate_target_amount(
                target_raw if target_raw not in (None, "") else tracker.DEFAULT_TARGET_AMOUNT
            )
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
                kwargs["group_id"] = _group_id(body.get("group_id"))
            if "buy_date" in body:
                kwargs["buy_date"] = str(body.get("buy_date") or "")
            if "target_amount" in body:
                kwargs["target_amount"] = tracker.validate_target_amount(body.get("target_amount"))
            if "active" in body:
                kwargs["active"] = _boolean(body, "active", True)
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
        def producer() -> dict[str, Any]:
            return tracker.update_today_quotes(
                codes=_codes(body), force=_boolean(body, "force", False),
            )

        return await _guarded_json(request, producer, require_admin=True)

    @router.post("/api/watchlist/quotes/backfill")
    async def backfill(request: Request) -> Response:
        body = await _read_json(request)
        def producer() -> dict[str, Any]:
            return tracker.backfill_quotes(
                days=tracker.validate_backfill_days(body.get("days", 60)),
                codes=_codes(body), missing_only=_boolean(body, "missing_only", True),
            )

        return await _guarded_json(request, producer, require_admin=True)

    @router.post("/api/watchlist/jobs")
    async def start_job(request: Request) -> Response:
        body = await _read_json(request)

        def producer() -> dict[str, Any]:
            return {"job": jobs.start(
                str(body.get("kind") or ""), codes=_codes(body),
                days=body.get("days", 60), missing_only=_boolean(body, "missing_only", True),
                force=_boolean(body, "force", False),
            )}

        return await _guarded_json(request, producer, require_admin=True, status_code=202)

    @router.api_route("/api/watchlist/jobs/latest", methods=["GET", "HEAD"])
    async def latest_job(request: Request) -> Response:
        return await _guarded_json(request, lambda: {"job": jobs.get()})

    @router.api_route("/api/watchlist/jobs/{job_id}", methods=["GET", "HEAD"])
    async def get_job(request: Request, job_id: str) -> Response:
        return await _guarded_json(request, lambda: {"job": jobs.get(job_id)})

    @router.post("/api/watchlist/jobs/{job_id}/retry")
    async def retry_job(request: Request, job_id: str) -> Response:
        return await _guarded_json(request, lambda: {"job": jobs.retry(job_id)},
                                   require_admin=True, status_code=202)

    return router
