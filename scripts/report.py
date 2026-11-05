#!/usr/bin/env python3
"""Combine disjoint A/B evaluation runs while preserving every attempted episode."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from servicedesk_agent.usage import summarize_episodes

CONTROL_FIELDS = (
    "model_mode",
    "model",
    "split",
    "tasks",
    "source_commit",
    "seed",
    "max_calls",
    "max_turns",
    "user_simulator",
    "semantic_judge_calls",
    "native_summarization",
)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def combine(runs: list[Path]) -> dict:
    if not runs:
        raise ValueError("Provide at least one evaluation directory.")
    configuration = read_json(runs[0] / "config.json")
    implementation = configuration.get("provenance", {}).get("implementation_sha256")
    episodes = []
    seen = set()
    inputs = []
    for directory in runs:
        candidate = read_json(directory / "config.json")
        if any(candidate.get(key) != configuration.get(key) for key in CONTROL_FIELDS):
            raise ValueError("Comparison runs must have the same task and model configuration.")
        if candidate.get("provenance", {}).get("implementation_sha256") != implementation:
            raise ValueError("Comparison runs must have the same implementation digest.")
        path = directory / "episodes.json"
        inputs.append(
            {
                "directory": str(directory.resolve()),
                "episodes_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        for episode in read_json(path):
            identity = (episode["task_id"], episode["group"])
            if identity in seen:
                raise ValueError("Each task/group pair may occur once in a paired comparison.")
            seen.add(identity)
            episodes.append(episode)

    groups = {}
    for group in sorted({item["group"] for item in episodes}):
        selected = [item for item in episodes if item["group"] == group]
        metrics = summarize_episodes(selected)
        for role in ("agent", "user"):
            metrics[f"{role}_usage"] = {
                key: sum(item[f"{role}_usage"][key] for item in selected)
                for key in (
                    "calls",
                    "failed_calls",
                    "unknown_usage_calls",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                )
            }
        durations = [item.get("trace", {}).get("elapsed_seconds") for item in selected]
        metrics["elapsed_seconds"] = (
            sum(durations) if all(item is not None for item in durations) else None
        )
        metrics["tool_calls"] = sum(
            len(item.get("trace", {}).get("tool_calls_detail", item.get("tool_events", [])))
            for item in selected
        )
        groups[group] = metrics

    pairs = {
        task_id: {item["group"]: item["success"] for item in episodes if item["task_id"] == task_id}
        for task_id in configuration["tasks"]
    }
    comparable = [
        pair
        for pair in pairs.values()
        if set(pair) == {"A", "B"} and all(value is not None for value in pair.values())
    ]
    return {
        "model_mode": configuration["model_mode"],
        "model": configuration["model"],
        "split": configuration["split"],
        "source_commit": configuration["source_commit"],
        "implementation_sha256": implementation,
        "planned_tasks": configuration["tasks"],
        "inputs": inputs,
        "groups": groups,
        "B_vs_A": {
            "wins": sum(pair["B"] and not pair["A"] for pair in comparable),
            "losses": sum(pair["A"] and not pair["B"] for pair in comparable),
            "ties": sum(pair["A"] == pair["B"] for pair in comparable),
            "unscored_pairs": len(pairs) - len(comparable),
        },
        "task_results": pairs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = combine(args.run)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
