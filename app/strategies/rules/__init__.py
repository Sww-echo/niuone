"""Compiled, deterministic execution primitives for prompt strategies."""

from .compiler import CompileError, compile_strategy_spec
from .audit import build_rule_evaluation_audit, replay_rule_evaluation_audit
from .evaluator import EvaluationContext, RuleStatus, evaluate_plan_stage
from .features import (
    DEFAULT_FEATURE_REGISTRY,
    FeatureDefinition,
    FeatureRegistry,
    FeatureRequest,
    materialize_features,
)
from .intents import build_action_intent
from .schema import (
    PROMPT_EXECUTION_PLAN_SCHEMA_VERSION,
    PROMPT_STRATEGY_SPEC_SCHEMA_VERSION,
    canonical_json,
    sha256_json,
    sha256_text,
)

__all__ = [
    "CompileError",
    "DEFAULT_FEATURE_REGISTRY",
    "EvaluationContext",
    "FeatureDefinition",
    "FeatureRegistry",
    "FeatureRequest",
    "PROMPT_EXECUTION_PLAN_SCHEMA_VERSION",
    "PROMPT_STRATEGY_SPEC_SCHEMA_VERSION",
    "RuleStatus",
    "build_action_intent",
    "build_rule_evaluation_audit",
    "canonical_json",
    "compile_strategy_spec",
    "evaluate_plan_stage",
    "materialize_features",
    "replay_rule_evaluation_audit",
    "sha256_json",
    "sha256_text",
]
