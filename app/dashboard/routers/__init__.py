"""FastAPI router factories for the Dashboard HTTP composition layer."""

from .admin import AdminAccess, create_admin_router
from .backtesting import create_backtesting_router
from .market import create_market_router
from .messages import create_messages_router
from .practice import create_practice_router
from .system import create_system_router
from .watchlist_tracker import create_watchlist_tracker_router
from .technical_analysis import create_technical_analysis_router

__all__ = [
    "AdminAccess",
    "create_admin_router",
    "create_backtesting_router",
    "create_market_router",
    "create_messages_router",
    "create_practice_router",
    "create_system_router",
    "create_watchlist_tracker_router",
    "create_technical_analysis_router",
]
