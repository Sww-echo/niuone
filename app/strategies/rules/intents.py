"""Translate successful rule evaluations into non-executing action intents."""

from __future__ import annotations

from typing import Any


def build_action_intent(
    plan: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    code: str,
    name: str = "",
) -> dict[str, Any] | None:
    if str(evaluation.get("status") or "") != "true":
        return None
    stage = str(evaluation.get("stage") or "")
    if stage == "selection":
        action = "OBSERVE"
        quantity_policy: dict[str, Any] = {}
    elif stage == "entry":
        action = "BUY"
        quantity_policy = dict((plan.get("strategy") or {}).get("position") or {})
    elif stage == "exit":
        action = "SELL"
        quantity_policy = {
            "type": str(
                (plan.get("strategy") or {}).get("exit_quantity")
                or "all_available"
            )
        }
    else:
        raise ValueError(f"unsupported evaluation stage: {stage}")
    return {
        "action": action,
        "code": str(code or ""),
        "name": str(name or ""),
        "strategy_id": str((plan.get("strategy") or {}).get("strategy_id") or ""),
        "plan_sha256": str(plan.get("plan_sha256") or ""),
        "stage": stage,
        "quantity_policy": quantity_policy,
        "rule_evidence": evaluation.get("root") or {},
    }
