"""Realtime financial-news FastAPI route."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response


CachedResponder = Callable[..., Awaitable[Response]]


def create_realtime_news_router(
    *,
    services: Any,
    cached_response: CachedResponder,
) -> APIRouter:
    """Expose the bounded NewsNow read model through the same-origin API."""

    router = APIRouter(include_in_schema=False)

    @router.api_route("/api/realtime-news", methods=["GET", "HEAD"])
    async def realtime_news(request: Request) -> Response:
        ttl = services.API_TTLS["realtime_news"]
        return await cached_response(
            request,
            cache_key="realtime_news:v1",
            ttl=ttl,
            producer=services.produce_realtime_news_data,
            edge_ttl=ttl,
            browser_ttl=5,
        )

    return router
