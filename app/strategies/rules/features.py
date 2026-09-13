"""Versioned feature registry and local, dependency-driven materialization."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..scoring.common import compute_bbi, compute_ema, moving_avg


FeatureCompute = Callable[[list[dict[str, Any]], Mapping[str, Any]], Mapping[str, Any]]
BarRequirement = Callable[[Mapping[str, Any]], int]
ParameterValidator = Callable[[Mapping[str, Any]], None]
MAX_FEATURE_OFFSET_BARS = 499


@dataclass(frozen=True)
class ParameterDefinition:
    kind: str
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] = ()

    def normalize(self, value: Any, *, name: str) -> Any:
        resolved = self.default if value in (None, "") else value
        if self.kind == "int":
            if isinstance(resolved, bool):
                raise ValueError(f"{name} must be an integer")
            try:
                number = int(resolved)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if self.minimum is not None and number < self.minimum:
                raise ValueError(f"{name} must be >= {self.minimum:g}")
            if self.maximum is not None and number > self.maximum:
                raise ValueError(f"{name} must be <= {self.maximum:g}")
            return number
        if self.kind == "float":
            try:
                number = float(resolved)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be numeric") from exc
            if not math.isfinite(number):
                raise ValueError(f"{name} must be finite")
            if self.minimum is not None and number < self.minimum:
                raise ValueError(f"{name} must be >= {self.minimum:g}")
            if self.maximum is not None and number > self.maximum:
                raise ValueError(f"{name} must be <= {self.maximum:g}")
            return number
        if self.kind == "choice":
            if resolved not in self.choices:
                raise ValueError(f"{name} must be one of {self.choices}")
            return resolved
        raise ValueError(f"unsupported parameter kind: {self.kind}")


@dataclass(frozen=True)
class FeatureDefinition:
    feature_id: str
    version: str
    outputs: tuple[str, ...]
    min_bars: int
    compute: FeatureCompute
    aliases: tuple[str, ...] = ()
    parameters: Mapping[str, ParameterDefinition] = field(default_factory=dict)
    supported_timeframes: tuple[str, ...] = ("1d",)
    description: str = ""
    bar_requirement: BarRequirement | None = None
    parameter_validator: ParameterValidator | None = None

    def normalize_parameters(self, values: Mapping[str, Any] | None) -> dict[str, Any]:
        supplied = dict(values or {})
        unknown = sorted(set(supplied) - set(self.parameters))
        if unknown:
            raise ValueError(
                f"{self.feature_id} has unknown parameters: {', '.join(unknown)}"
            )
        normalized = {
            name: definition.normalize(supplied.get(name), name=name)
            for name, definition in self.parameters.items()
        }
        if self.parameter_validator is not None:
            self.parameter_validator(normalized)
        return normalized

    def required_bars(self, parameters: Mapping[str, Any]) -> int:
        if self.bar_requirement is None:
            return max(1, int(self.min_bars))
        return max(int(self.min_bars), int(self.bar_requirement(parameters)))


@dataclass(frozen=True)
class FeatureRequest:
    feature_id: str
    field: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    timeframe: str = "1d"
    feature_version: str = ""
    offset_bars: int = 0


class FeatureRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], FeatureDefinition] = {}
        self._default_versions: dict[str, str] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        definition: FeatureDefinition,
        *,
        make_default: bool = True,
    ) -> None:
        feature_id = str(definition.feature_id or "").strip().lower()
        version = str(definition.version or "").strip()
        if not feature_id:
            raise ValueError("feature_id is required")
        if not version:
            raise ValueError("feature version is required")
        key = (feature_id, version)
        if key in self._definitions:
            raise ValueError(f"feature already registered: {feature_id}#{version}")
        self._definitions[key] = definition
        if make_default or feature_id not in self._default_versions:
            self._default_versions[feature_id] = version
        for alias in (feature_id, *definition.aliases):
            normalized = str(alias or "").strip().lower()
            if normalized and normalized not in self._aliases:
                self._aliases[normalized] = feature_id

    def resolve(
        self,
        feature_id: str,
        feature_version: str = "",
    ) -> FeatureDefinition:
        normalized = str(feature_id or "").strip().lower()
        resolved = self._aliases.get(normalized, normalized)
        version = str(feature_version or self._default_versions.get(resolved) or "")
        definition = self._definitions.get((resolved, version))
        if definition is None:
            suffix = f"#{feature_version}" if feature_version else ""
            raise KeyError(f"unsupported feature: {feature_id}{suffix}")
        return definition

    def capability_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "feature_id": definition.feature_id,
                "version": definition.version,
                "outputs": list(definition.outputs),
                "aliases": list(definition.aliases),
                "min_bars": definition.min_bars,
                "timeframes": list(definition.supported_timeframes),
                "offset_bars": {
                    "kind": "int",
                    "default": 0,
                    "min": 0,
                    "max": MAX_FEATURE_OFFSET_BARS,
                    "description": (
                        "0 is the current evaluation bar; 1 is the previous bar."
                    ),
                },
                "parameters": {
                    name: {
                        "kind": item.kind,
                        "default": item.default,
                        "min": item.minimum,
                        "max": item.maximum,
                        "choices": list(item.choices),
                    }
                    for name, item in definition.parameters.items()
                },
                "description": definition.description,
            }
            for feature_id, version in sorted(self._default_versions.items())
            for definition in [self._definitions[(feature_id, version)]]
        ]


def feature_request_key(
    definition: FeatureDefinition,
    field_name: str,
    parameters: Mapping[str, Any],
    timeframe: str,
    offset_bars: int = 0,
) -> str:
    parameter_text = ",".join(
        f"{key}={parameters[key]}" for key in sorted(parameters)
    )
    suffix = f"[{parameter_text}]" if parameter_text else ""
    offset_suffix = f"~offset={offset_bars}" if offset_bars else ""
    return (
        f"{definition.feature_id}.{field_name}{suffix}@{timeframe}"
        f"#{definition.version}{offset_suffix}"
    )


def normalize_feature_request(
    registry: FeatureRegistry,
    request: FeatureRequest,
) -> tuple[FeatureDefinition, FeatureRequest, str]:
    definition = registry.resolve(request.feature_id, request.feature_version)
    field_name = str(request.field or "").strip().lower()
    if field_name not in definition.outputs:
        raise ValueError(
            f"{definition.feature_id} does not provide output: {field_name}"
        )
    timeframe = str(request.timeframe or "1d").strip().lower()
    if timeframe not in definition.supported_timeframes:
        raise ValueError(
            f"{definition.feature_id} does not support timeframe: {timeframe}"
        )
    parameters = definition.normalize_parameters(request.parameters)
    raw_offset = request.offset_bars
    if isinstance(raw_offset, bool):
        raise ValueError("offset_bars must be an integer")
    try:
        offset_bars = int(raw_offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("offset_bars must be an integer") from exc
    if isinstance(raw_offset, float) and not raw_offset.is_integer():
        raise ValueError("offset_bars must be an integer")
    if not 0 <= offset_bars <= MAX_FEATURE_OFFSET_BARS:
        raise ValueError(
            f"offset_bars must be between 0 and {MAX_FEATURE_OFFSET_BARS}"
        )
    normalized = FeatureRequest(
        feature_id=definition.feature_id,
        field=field_name,
        parameters=parameters,
        timeframe=timeframe,
        feature_version=definition.version,
        offset_bars=offset_bars,
    )
    return (
        definition,
        normalized,
        feature_request_key(
            definition,
            field_name,
            parameters,
            timeframe,
            offset_bars,
        ),
    )


def _row_number(row: Mapping[str, Any], name: str) -> float | None:
    try:
        value = float(row.get(name))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _last_value(rows: list[dict[str, Any]], field_name: str) -> float | None:
    return _row_number(rows[-1], field_name) if rows else None


def _compute_market_value(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    field_name = str(parameters["field"])
    return {"value": _last_value(rows, field_name)}


def _compute_ema_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    closes = [_row_number(row, "close") for row in rows]
    if any(value is None for value in closes):
        return {"value": None}
    values = compute_ema([float(value) for value in closes], int(parameters["period"]))
    return {"value": values[-1] if values else None}


def _compute_ma_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    closes = [_row_number(row, "close") for row in rows]
    if any(value is None for value in closes):
        return {"value": None}
    values = moving_avg([float(value) for value in closes], int(parameters["period"]))
    return {"value": values[-1] if values else None}


def _compute_bbi_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        close = _row_number(row, "close")
        if close is None:
            return {"value": None}
        normalized_rows.append({"close": close})
    values = compute_bbi(normalized_rows)
    return {"value": values[-1] if values else None}


def _compute_kdj_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    period = int(parameters["n"])
    k_smoothing = int(parameters["m1"])
    d_smoothing = int(parameters["m2"])
    k_value = 50.0
    d_value = 50.0
    latest: dict[str, Any] = {"k": None, "d": None, "j": None}
    for index, row in enumerate(rows):
        if index < period - 1:
            continue
        window = rows[index - period + 1:index + 1]
        lows = [_row_number(item, "low") for item in window]
        highs = [_row_number(item, "high") for item in window]
        close = _row_number(row, "close")
        if close is None or any(value is None for value in lows + highs):
            return latest
        lowest = min(float(value) for value in lows)
        highest = max(float(value) for value in highs)
        rsv = 50.0 if highest == lowest else (close - lowest) / (highest - lowest) * 100.0
        k_value = (k_smoothing - 1) / k_smoothing * k_value + rsv / k_smoothing
        d_value = (d_smoothing - 1) / d_smoothing * d_value + k_value / d_smoothing
        latest = {
            "k": k_value,
            "d": d_value,
            "j": 3.0 * k_value - 2.0 * d_value,
        }
    return latest


def _compute_return_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    period = int(parameters["period"])
    if len(rows) <= period:
        return {"pct": None}
    current = _row_number(rows[-1], "close")
    previous = _row_number(rows[-period - 1], "close")
    return {
        "pct": (current / previous - 1.0) * 100.0
        if current is not None and previous not in (None, 0)
        else None
    }


def _compute_volume_ratio_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    short_period = int(parameters["short_period"])
    long_period = int(parameters["long_period"])
    if len(rows) < short_period + long_period:
        return {"value": None}
    short_values = [
        _row_number(row, "volume") for row in rows[-short_period:]
    ]
    prior_values = [
        _row_number(row, "volume")
        for row in rows[-(short_period + long_period):-short_period]
    ]
    if any(value is None for value in short_values + prior_values):
        return {"value": None}
    baseline = statistics.mean(float(value) for value in prior_values)
    return {
        "value": statistics.mean(float(value) for value in short_values) / baseline
        if baseline > 0
        else None
    }


def _compute_volatility_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    period = int(parameters["period"])
    if len(rows) <= period:
        return {"pct": None}
    closes = [_row_number(row, "close") for row in rows[-period - 1:]]
    if any(value is None or value <= 0 for value in closes):
        return {"pct": None}
    returns = [
        (float(right) / float(left) - 1.0) * 100.0
        for left, right in zip(closes, closes[1:])
    ]
    return {"pct": statistics.pstdev(returns) if len(returns) >= 2 else None}


def _compute_rsi_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    period = int(parameters["period"])
    closes = [_row_number(row, "close") for row in rows]
    if len(closes) <= period or any(value is None for value in closes):
        return {"value": None}
    changes = [
        float(right) - float(left)
        for left, right in zip(closes, closes[1:])
    ]
    gains = [max(0.0, value) for value in changes]
    losses = [max(0.0, -value) for value in changes]
    avg_gain = statistics.mean(gains[:period])
    avg_loss = statistics.mean(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return {"value": 100.0 if avg_gain > 0 else 50.0}
    relative_strength = avg_gain / avg_loss
    return {"value": 100.0 - 100.0 / (1.0 + relative_strength)}


def _compute_macd_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    closes = [_row_number(row, "close") for row in rows]
    if any(value is None for value in closes):
        return {"dif": None, "dea": None, "hist": None}
    fast = int(parameters["fast"])
    slow = int(parameters["slow"])
    signal = int(parameters["signal"])
    values = [float(value) for value in closes]
    fast_values = compute_ema(values, fast)
    slow_values = compute_ema(values, slow)
    dif_values = [left - right for left, right in zip(fast_values, slow_values)]
    dea_values = compute_ema(dif_values, signal)
    if not dif_values or not dea_values:
        return {"dif": None, "dea": None, "hist": None}
    dif = dif_values[-1]
    dea = dea_values[-1]
    return {"dif": dif, "dea": dea, "hist": 2.0 * (dif - dea)}


def _compute_boll_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    period = int(parameters["period"])
    deviations = float(parameters["deviations"])
    closes = [_row_number(row, "close") for row in rows[-period:]]
    if len(closes) < period or any(value is None for value in closes):
        return {"middle": None, "upper": None, "lower": None, "width_pct": None}
    values = [float(value) for value in closes]
    middle = statistics.mean(values)
    deviation = statistics.pstdev(values)
    upper = middle + deviations * deviation
    lower = middle - deviations * deviation
    return {
        "middle": middle,
        "upper": upper,
        "lower": lower,
        "width_pct": (upper - lower) / middle * 100.0 if middle else None,
    }


def _compute_atr_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    period = int(parameters["period"])
    if len(rows) <= period:
        return {"value": None, "pct": None}
    true_ranges: list[float] = []
    for previous, current in zip(rows[-period - 1:-1], rows[-period:]):
        previous_close = _row_number(previous, "close")
        high = _row_number(current, "high")
        low = _row_number(current, "low")
        if previous_close is None or high is None or low is None:
            return {"value": None, "pct": None}
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr = statistics.mean(true_ranges)
    close = _row_number(rows[-1], "close")
    return {
        "value": atr,
        "pct": atr / close * 100.0 if close not in (None, 0) else None,
    }


def _compute_price_range_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    period = int(parameters["period"])
    window = rows[-period:]
    highs = [_row_number(row, "high") for row in window]
    lows = [_row_number(row, "low") for row in window]
    if len(window) < period or any(value is None for value in highs + lows):
        return {"highest": None, "lowest": None}
    return {
        "highest": max(float(value) for value in highs),
        "lowest": min(float(value) for value in lows),
    }


def _compute_drawdown_feature(
    rows: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> Mapping[str, Any]:
    period = int(parameters["period"])
    closes = [_row_number(row, "close") for row in rows[-period:]]
    if len(closes) < period or any(value is None for value in closes):
        return {"pct": None}
    peak = max(float(value) for value in closes)
    current = float(closes[-1])
    return {"pct": (current / peak - 1.0) * 100.0 if peak > 0 else None}


def _validate_macd_parameters(parameters: Mapping[str, Any]) -> None:
    if int(parameters["fast"]) >= int(parameters["slow"]):
        raise ValueError("technical.macd fast must be smaller than slow")


def _bounded_recursive_requirement(value: int) -> int:
    """Bound deterministic recursive-indicator warm-up to the data contract."""
    return max(1, min(500, int(value)))


def _ema_v2_requirement(parameters: Mapping[str, Any]) -> int:
    return _bounded_recursive_requirement(int(parameters["period"]) * 5)


def _kdj_v2_requirement(parameters: Mapping[str, Any]) -> int:
    return _bounded_recursive_requirement(
        int(parameters["n"])
        + 10 * max(int(parameters["m1"]), int(parameters["m2"]))
    )


def _rsi_v2_requirement(parameters: Mapping[str, Any]) -> int:
    return _bounded_recursive_requirement(int(parameters["period"]) * 6)


def _macd_v2_requirement(parameters: Mapping[str, Any]) -> int:
    return _bounded_recursive_requirement(
        5 * (int(parameters["slow"]) + int(parameters["signal"]))
    )


def build_default_feature_registry() -> FeatureRegistry:
    registry = FeatureRegistry()
    registry.register(FeatureDefinition(
        feature_id="market.value",
        version="v1",
        outputs=("value",),
        min_bars=1,
        aliases=("price", "ohlcv"),
        parameters={
            "field": ParameterDefinition(
                "choice",
                default="close",
                choices=("open", "high", "low", "close", "volume"),
            )
        },
        compute=_compute_market_value,
        description="Latest open, high, low, close or volume value.",
    ))
    registry.register(FeatureDefinition(
        feature_id="technical.ema",
        version="v1",
        outputs=("value",),
        min_bars=2,
        aliases=("ema", "指数移动平均线"),
        parameters={"period": ParameterDefinition("int", default=20, minimum=2, maximum=500)},
        bar_requirement=lambda params: int(params["period"]),
        compute=_compute_ema_feature,
        description="Exponential moving average of closing prices.",
    ), make_default=False)
    registry.register(FeatureDefinition(
        feature_id="technical.ema",
        version="v2",
        outputs=("value",),
        min_bars=2,
        aliases=("ema", "指数移动平均线"),
        parameters={"period": ParameterDefinition("int", default=20, minimum=2, maximum=500)},
        bar_requirement=_ema_v2_requirement,
        compute=_compute_ema_feature,
        description="EMA with a deterministic five-period warm-up budget.",
    ))
    registry.register(FeatureDefinition(
        feature_id="technical.ma",
        version="v1",
        outputs=("value",),
        min_bars=2,
        aliases=("ma", "移动平均线"),
        parameters={"period": ParameterDefinition("int", default=20, minimum=2, maximum=500)},
        bar_requirement=lambda params: int(params["period"]),
        compute=_compute_ma_feature,
        description="Simple moving average of closing prices.",
    ))
    registry.register(FeatureDefinition(
        feature_id="technical.bbi",
        version="v1",
        outputs=("value",),
        min_bars=24,
        aliases=("bbi", "多空指数"),
        compute=_compute_bbi_feature,
        description="BBI based on MA3, MA6, MA12 and MA24.",
    ))
    registry.register(FeatureDefinition(
        feature_id="technical.kdj",
        version="cn-kdj-v1",
        outputs=("k", "d", "j"),
        min_bars=9,
        aliases=("kdj", "随机指标", "j值"),
        parameters={
            "n": ParameterDefinition("int", default=9, minimum=2, maximum=200),
            "m1": ParameterDefinition("int", default=3, minimum=1, maximum=50),
            "m2": ParameterDefinition("int", default=3, minimum=1, maximum=50),
        },
        bar_requirement=lambda params: int(params["n"]),
        compute=_compute_kdj_feature,
        description="Chinese-market KDJ oscillator with configurable smoothing.",
    ), make_default=False)
    registry.register(FeatureDefinition(
        feature_id="technical.kdj",
        version="cn-kdj-v2",
        outputs=("k", "d", "j"),
        min_bars=9,
        aliases=("kdj", "随机指标", "j值"),
        parameters={
            "n": ParameterDefinition("int", default=9, minimum=2, maximum=200),
            "m1": ParameterDefinition("int", default=3, minimum=1, maximum=50),
            "m2": ParameterDefinition("int", default=3, minimum=1, maximum=50),
        },
        bar_requirement=_kdj_v2_requirement,
        compute=_compute_kdj_feature,
        description="Chinese-market KDJ with a versioned recursive warm-up budget.",
    ))
    registry.register(FeatureDefinition(
        feature_id="return.close",
        version="v1",
        outputs=("pct",),
        min_bars=2,
        aliases=("return", "涨跌幅", "收益率"),
        parameters={"period": ParameterDefinition("int", default=5, minimum=1, maximum=499)},
        bar_requirement=lambda params: int(params["period"]) + 1,
        compute=_compute_return_feature,
        description="Closing-price return over a configurable number of bars.",
    ))
    registry.register(FeatureDefinition(
        feature_id="volume.ratio",
        version="v1",
        outputs=("value",),
        min_bars=2,
        aliases=("volume_ratio", "量比", "均量比"),
        parameters={
            "short_period": ParameterDefinition("int", default=5, minimum=1, maximum=100),
            "long_period": ParameterDefinition("int", default=20, minimum=1, maximum=500),
        },
        bar_requirement=lambda params: int(params["short_period"]) + int(params["long_period"]),
        compute=_compute_volume_ratio_feature,
        description="Average recent volume divided by the preceding average volume.",
    ))
    registry.register(FeatureDefinition(
        feature_id="volatility.close",
        version="v1",
        outputs=("pct",),
        min_bars=3,
        aliases=("volatility", "波动率"),
        parameters={"period": ParameterDefinition("int", default=20, minimum=2, maximum=499)},
        bar_requirement=lambda params: int(params["period"]) + 1,
        compute=_compute_volatility_feature,
        description="Population standard deviation of close-to-close percentage returns.",
    ))
    registry.register(FeatureDefinition(
        feature_id="technical.rsi",
        version="wilder-v1",
        outputs=("value",),
        min_bars=3,
        aliases=("rsi", "相对强弱指标"),
        parameters={"period": ParameterDefinition("int", default=14, minimum=2, maximum=250)},
        bar_requirement=lambda params: int(params["period"]) + 1,
        compute=_compute_rsi_feature,
        description="Wilder RSI of closing prices.",
    ), make_default=False)
    registry.register(FeatureDefinition(
        feature_id="technical.rsi",
        version="wilder-v2",
        outputs=("value",),
        min_bars=3,
        aliases=("rsi", "相对强弱指标"),
        parameters={"period": ParameterDefinition("int", default=14, minimum=2, maximum=250)},
        bar_requirement=_rsi_v2_requirement,
        compute=_compute_rsi_feature,
        description="Wilder RSI with a deterministic recursive warm-up budget.",
    ))
    registry.register(FeatureDefinition(
        feature_id="technical.macd",
        version="cn-macd-v1",
        outputs=("dif", "dea", "hist"),
        min_bars=26,
        aliases=("macd", "指数平滑异同移动平均线"),
        parameters={
            "fast": ParameterDefinition("int", default=12, minimum=2, maximum=100),
            "slow": ParameterDefinition("int", default=26, minimum=3, maximum=250),
            "signal": ParameterDefinition("int", default=9, minimum=2, maximum=100),
        },
        bar_requirement=lambda params: int(params["slow"]) + int(params["signal"]),
        parameter_validator=_validate_macd_parameters,
        compute=_compute_macd_feature,
        description="MACD DIF, DEA and doubled histogram.",
    ), make_default=False)
    registry.register(FeatureDefinition(
        feature_id="technical.macd",
        version="cn-macd-v2",
        outputs=("dif", "dea", "hist"),
        min_bars=26,
        aliases=("macd", "指数平滑异同移动平均线"),
        parameters={
            "fast": ParameterDefinition("int", default=12, minimum=2, maximum=100),
            "slow": ParameterDefinition("int", default=26, minimum=3, maximum=250),
            "signal": ParameterDefinition("int", default=9, minimum=2, maximum=100),
        },
        bar_requirement=_macd_v2_requirement,
        parameter_validator=_validate_macd_parameters,
        compute=_compute_macd_feature,
        description="MACD with a versioned recursive warm-up budget.",
    ))
    registry.register(FeatureDefinition(
        feature_id="technical.boll",
        version="v1",
        outputs=("middle", "upper", "lower", "width_pct"),
        min_bars=3,
        aliases=("boll", "布林带"),
        parameters={
            "period": ParameterDefinition("int", default=20, minimum=2, maximum=500),
            "deviations": ParameterDefinition("float", default=2.0, minimum=0.1, maximum=10.0),
        },
        bar_requirement=lambda params: int(params["period"]),
        compute=_compute_boll_feature,
        description="Bollinger middle, upper, lower and band width.",
    ))
    registry.register(FeatureDefinition(
        feature_id="volatility.atr",
        version="true-range-v1",
        outputs=("value", "pct"),
        min_bars=3,
        aliases=("atr", "真实波幅"),
        parameters={"period": ParameterDefinition("int", default=14, minimum=2, maximum=250)},
        bar_requirement=lambda params: int(params["period"]) + 1,
        compute=_compute_atr_feature,
        description="Average true range in price and percentage units.",
    ))
    registry.register(FeatureDefinition(
        feature_id="price.range",
        version="v1",
        outputs=("highest", "lowest"),
        min_bars=2,
        aliases=("highest", "lowest", "区间高低点"),
        parameters={"period": ParameterDefinition("int", default=20, minimum=2, maximum=500)},
        bar_requirement=lambda params: int(params["period"]),
        compute=_compute_price_range_feature,
        description="Highest high and lowest low over a rolling window.",
    ))
    registry.register(FeatureDefinition(
        feature_id="price.drawdown",
        version="v1",
        outputs=("pct",),
        min_bars=2,
        aliases=("drawdown", "回撤"),
        parameters={"period": ParameterDefinition("int", default=20, minimum=2, maximum=500)},
        bar_requirement=lambda params: int(params["period"]),
        compute=_compute_drawdown_feature,
        description="Current close drawdown from the rolling closing-price peak.",
    ))
    return registry


DEFAULT_FEATURE_REGISTRY = build_default_feature_registry()


def materialize_features(
    requests: Sequence[FeatureRequest],
    rows: Sequence[Mapping[str, Any]],
    *,
    registry: FeatureRegistry = DEFAULT_FEATURE_REGISTRY,
) -> dict[str, Any]:
    normalized_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    facts: dict[str, Any] = {}
    metadata: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    computed: dict[
        tuple[str, str, str, tuple[tuple[str, Any], ...], int],
        Mapping[str, Any],
    ] = {}
    for raw_request in requests:
        try:
            definition, request, fact_key = normalize_feature_request(
                registry, raw_request
            )
        except (KeyError, ValueError) as exc:
            errors.append({"feature_id": raw_request.feature_id, "error": str(exc)})
            continue
        computation_key = (
            definition.feature_id,
            definition.version,
            request.timeframe,
            tuple(sorted(request.parameters.items())),
            request.offset_bars,
        )
        if computation_key not in computed:
            required_bars = (
                definition.required_bars(request.parameters) + request.offset_bars
            )
            feature_rows = (
                normalized_rows
                if request.offset_bars == 0
                else normalized_rows[:-request.offset_bars]
            )
            if len(normalized_rows) < required_bars:
                computed[computation_key] = {}
            else:
                try:
                    computed[computation_key] = dict(
                        definition.compute(feature_rows, request.parameters)
                    )
                except Exception as exc:
                    computed[computation_key] = {}
                    errors.append({
                        "feature_id": definition.feature_id,
                        "error": type(exc).__name__,
                    })
        value = computed[computation_key].get(request.field)
        feature_rows = (
            normalized_rows
            if request.offset_bars == 0
            else normalized_rows[:-request.offset_bars]
        )
        facts[fact_key] = value
        metadata[fact_key] = {
            "feature_id": definition.feature_id,
            "feature_version": definition.version,
            "field": request.field,
            "parameters": dict(request.parameters),
            "timeframe": request.timeframe,
            "offset_bars": request.offset_bars,
            "bar_count": len(feature_rows),
            "bar_time": str((feature_rows[-1] if feature_rows else {}).get("date") or ""),
            "status": "ok" if value is not None else "missing",
        }
    return {"facts": facts, "metadata": metadata, "errors": errors}
