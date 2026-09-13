"""Message history and lightweight revision FastAPI routes."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response


CachedResponder = Callable[..., Awaitable[Response]]


def messages_revision_payload(
    payload: dict[str, Any],
    category: str,
) -> dict[str, Any]:
    """Project a message page to the fields needed for change detection."""

    records = payload.get("records")
    normalized_records = records if isinstance(records, list) else []
    latest = normalized_records[0] if normalized_records else {}
    if not isinstance(latest, dict):
        latest = {}
    category_data = (payload.get("categories") or {}).get(category) or {}
    if not isinstance(category_data, dict):
        category_data = {}
    result: dict[str, Any] = {
        "category": category,
        "count": int(category_data.get("count") or 0),
        "latest": {
            "id": str(latest.get("id") or ""),
            "timestamp": latest.get("timestamp"),
            "content_hash": str(latest.get("content_hash") or ""),
            "updated_at": str(latest.get("updated_at") or ""),
        },
    }
    return result


def create_messages_router(*, services: Any, cached_response: CachedResponder) -> APIRouter:
    """Create native read routes for message pages and their revisions."""

    router = APIRouter(include_in_schema=False)

    @router.api_route("/api/messages", methods=["GET", "HEAD"])
    async def messages(request: Request) -> Response:
        limit = services.clamp_limit(request.query_params.get("limit"))
        offset = services.clamp_offset(request.query_params.get("offset"))
        category = str(request.query_params.get("category") or "").strip() or None
        ttl = services.API_TTLS["messages"]
        return await cached_response(
            request,
            cache_key=f"messages:v4:{category or 'all'}:{limit}:{offset}",
            ttl=ttl,
            producer=lambda: services.merge_records_from_db(
                limit=limit,
                category=category,
                offset=offset,
            ),
            edge_ttl=ttl,
            browser_ttl=5,
        )

    @router.api_route("/api/messages/revision", methods=["GET", "HEAD"])
    async def messages_revision(request: Request) -> Response:
        category = str(request.query_params.get("category") or "").strip()
        if re.fullmatch(r"[a-z0-9_]{1,64}", category) is None:
            return JSONResponse(
                {"error": "message_category_required"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        ttl = services.API_TTLS["messages"]
        return await cached_response(
            request,
            cache_key=f"messages-revision:v1:{category}",
            ttl=ttl,
            producer=lambda: messages_revision_payload(
                services.merge_records_from_db(
                    limit=1,
                    category=category,
                    offset=0,
                ),
                category,
            ),
            edge_ttl=ttl,
            browser_ttl=5,
        )

    return router
