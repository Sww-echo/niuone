#!/usr/bin/env python3
"""Run a private, offline paired entry study on minimized observation records."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.backtesting.entry_risk_study import compare_entry_variants, entry_risk_groups


def run_study(records: list[dict]) -> dict:
    groups = defaultdict(list)
    excluded = Counter()
    flagged = Counter()
    for record in records:
        names = entry_risk_groups(**record["context"])
        if not names:
            continue
        flagged.update(names)
        if record.get("exclusion"):
            excluded[record["exclusion"]] += 1
            continue
        try:
            result = compare_entry_variants(entry_price=record["entry_price"],
                                            structural_stop=record["structural_stop"], bars=record["bars"])
        except (ValueError, KeyError, TypeError):
            excluded["invalid_or_incomplete_replay_inputs"] += 1
            continue
        for name in names:
            groups[name].append(result)
    summary = {}
    for name in ("diverging_weak_chase", "climax_wide_stop"):
        samples = groups[name]
        variants = {}
        for variant in ("baseline", "half_initial", "wait_confirmation"):
            rows = [sample[variant] for sample in samples]
            values = [row["return_on_budget_pct"] for row in rows]
            variants[variant] = {
                "entered": sum(row["entered"] for row in rows),
                "mean_return_on_budget_pct": round(statistics.mean(values), 4) if values else None,
                "worst_return_on_budget_pct": min(values) if values else None,
                "positive_outcomes": sum(value > 0 for value in values),
                "negative_outcomes": sum(value < 0 for value in values),
                "better_than_baseline": sum(sample[variant]["net_pnl"] > sample["baseline"]["net_pnl"] for sample in samples),
                "worse_than_baseline": sum(sample[variant]["net_pnl"] < sample["baseline"]["net_pnl"] for sample in samples),
            }
        summary[name] = {"flagged": flagged[name], "paired_samples": len(samples), "variants": variants}
    return {"schema_version": 1, "research_only": True, "records": len(records),
            "groups": summary, "excluded": dict(excluded),
            "limitations": ["observed executed entries only; selection bias", "mixed historical protocols; not strict-forward evidence",
                            "five-session structural-stop stress test, not full strategy P&L", "price confirmation does not verify theme recovery",
                            "fixed fees and no extra slippage; no portfolio capital reuse", "daily bars cannot validate minute cost confirmation"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    result = {"source_quality": payload.get("source_quality", {}), **run_study(payload["records"])}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_written": True, "paired_samples": {key: row["paired_samples"] for key, row in result["groups"].items()}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
