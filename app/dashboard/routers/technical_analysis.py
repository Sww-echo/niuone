"""FastAPI routes for technical analysis and bounded cache scans."""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.dashboard.technical_analysis_service import (
    TechnicalScanManager,
    analyze_minute_symbol,
    analyze_symbol,
    get_technical_scan_manager,
)
from app.market_data.technical_analysis import TechnicalMarketDataError


LimitEnforcer = Callable[[Request], Awaitable[Response | None]]
JsonResponder = Callable[..., Response]


def create_technical_analysis_router(
    *,
    enforce_api_limits: LimitEnforcer,
    json_response: JsonResponder,
    analyze_service: Callable[..., dict[str, Any]] = analyze_symbol,
    minute_analyze_service: Callable[..., dict[str, Any]] = analyze_minute_symbol,
    scan_manager: TechnicalScanManager | None = None,
) -> APIRouter:
    """Create injectable, network-free-testable technical-analysis routes."""
    router = APIRouter(include_in_schema=False)
    scans = scan_manager or get_technical_scan_manager()

    @router.api_route("/api/technical-analysis/analyze", methods=["GET", "HEAD"])
    async def analyze(request: Request) -> Response:
        limited = await enforce_api_limits(request)
        if limited is not None:
            return limited
        if request.method == "HEAD":
            return Response(status_code=200, media_type="application/json", headers={"Cache-Control": "no-store"})
        symbol = re.sub(r"[^A-Za-z0-9]", "", str(request.query_params.get("symbol") or ""))
        period = str(request.query_params.get("period") or "day").lower()
        if not re.fullmatch(r"(?:(?:sh|sz|bj)\d{6}|\d{6})", symbol, re.I):
            return JSONResponse(
                {"error": "invalid_symbol", "message": "请输入 6 位 A 股代码"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        if period not in {"day", "week"}:
            return JSONResponse(
                {"error": "unsupported_period"}, status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            payload = await run_in_threadpool(
                lambda: analyze_service(symbol, period=period)
            )
        except TechnicalMarketDataError as exc:
            return JSONResponse(
                {"error": str(exc), "message": "行情数据暂不可用，请稍后重试"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as exc:
            return JSONResponse(
                {"error": str(exc) or "invalid_request"}, status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        return json_response(request, payload, cache_control="no-store")

    @router.api_route("/api/technical-analysis/minute", methods=["GET", "HEAD"])
    async def analyze_minute(request: Request) -> Response:
        limited = await enforce_api_limits(request)
        if limited is not None:
            return limited
        if request.method == "HEAD":
            return Response(status_code=200, media_type="application/json", headers={"Cache-Control": "no-store"})
        symbol = re.sub(r"[^A-Za-z0-9]", "", str(request.query_params.get("symbol") or ""))
        if not re.fullmatch(r"(?:(?:sh|sz|bj)\d{6}|\d{6})", symbol, re.I):
            return JSONResponse(
                {"error": "invalid_symbol"}, status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            payload = await run_in_threadpool(minute_analyze_service, symbol)
        except TechnicalMarketDataError as exc:
            return JSONResponse(
                {"error": str(exc), "message": "分时数据暂不可用"}, status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return json_response(request, payload, cache_control="no-store")

    @router.post("/api/technical-analysis/scans")
    async def start_scan(request: Request) -> Response:
        limited = await enforce_api_limits(request)
        if limited is not None:
            return limited
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            payload = await run_in_threadpool(
                scans.start,
                period=str(body.get("period") or "day"),
                limit=int(body.get("limit") or 1000),
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse(
                {"error": str(exc) or "invalid_scan_request"}, status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        return json_response(request, payload, cache_control="no-store", status_code=202)

    @router.api_route("/api/technical-analysis/scans/{job_id}", methods=["GET", "HEAD"])
    async def scan_status(request: Request, job_id: str) -> Response:
        limited = await enforce_api_limits(request)
        if limited is not None:
            return limited
        if not re.fullmatch(r"[0-9a-f]{32}", str(job_id or "")):
            return JSONResponse(
                {"error": "scan_not_found"}, status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        if request.method == "HEAD":
            return Response(status_code=200, media_type="application/json", headers={"Cache-Control": "no-store"})
        payload = await run_in_threadpool(scans.get, job_id)
        if payload is None:
            return JSONResponse(
                {"error": "scan_not_found"}, status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        return json_response(request, payload, cache_control="no-store")

    @router.delete("/api/technical-analysis/scans/{job_id}")
    async def cancel_scan(request: Request, job_id: str) -> Response:
        limited = await enforce_api_limits(request)
        if limited is not None:
            return limited
        if not re.fullmatch(r"[0-9a-f]{32}", str(job_id or "")):
            return JSONResponse(
                {"error": "scan_not_found"}, status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        payload = await run_in_threadpool(scans.cancel, job_id)
        if payload is None:
            return JSONResponse(
                {"error": "scan_not_found"}, status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        return json_response(request, payload, cache_control="no-store")

    return router


__all__ = ["create_technical_analysis_router"]
