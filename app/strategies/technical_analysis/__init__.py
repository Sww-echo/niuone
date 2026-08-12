"""NiuOne pure technical-analysis algorithms.

All public functions accept JSON-like values and return JSON-safe dictionaries.
The package deliberately contains no HTTP, caching, licensing, scanning or
process state; those responsibilities belong to NiuOne's composition layers.
"""

from .breakout import analyze_breakout, calc_n, calc_true_range
from .canslim import analyze_canslim
from .chanlun import analyze_chanlun_daily, calc_daily_macd
from .engine import analyze_technical
from .patterns import analyze_patterns
from .trend import analyze_trend
from .volume_price import analyze_volume_price

from .minute_chanlun import analyze_minute_technical


__all__ = [
    "analyze_breakout",
    "analyze_canslim",
    "analyze_chanlun_daily",
    "analyze_minute_technical",
    "analyze_patterns",
    "analyze_technical",
    "analyze_trend",
    "analyze_volume_price",
    "calc_daily_macd",
    "calc_n",
    "calc_true_range",
]
