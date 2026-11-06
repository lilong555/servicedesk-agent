#!/usr/bin/env python3
"""Recompute recorded policy results and strictly verify business evidence offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

ARMS = ("unfiltered_time", "filtered_time")
USAGE_KEYS = (
    "model_requests",
    "unknown_usage_requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
)
MARKER = re.compile(r"\n\n\[desk-action:([0-9a-f]{64})\]$")


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def retrieval_metrics(query: dict, results: list[dict]) -> dict:
    gold = {tuple(item) for item in query["gold"]}
    pairs = [(item["policy_id"], item["version"]) for item in results]
    clock = timestamp(query["time"])
    invalid = 0
    outside = 0
    for result in results:
        evidence = result["evidence"]
        invalid += timestamp(evidence["valid_from"]) > clock or (
            evidence["valid_to"] is not None and clock >= timestamp(evidence["valid_to"])
        )
        outside += evidence["entity_scope"] != query["scope"]
    return {
        "answerable": bool(gold),
        "hit_at_1": bool(pairs and pairs[0] in gold) if gold else None,
        "hit_at_3": bool(gold.intersection(pairs)) if gold else None,
        "relevant_returned": len(gold.intersection(pairs)),
        "required_documents": len(gold),
        "returned": len(results),
        "time_inapplicable_returned": invalid,
        "out_of_scope_returned": outside,
        "empty_result": not results,
    }


def strict_business(row: dict, scenario: dict, data: dict) -> dict:
    clock = timestamp("2026-11-06T00:00:00Z")
    index = next(
        i
        for i, version in enumerate(data["versions"])
        if timestamp(version["valid_from"]) <= clock
        and (version["valid_to"] is None or clock < timestamp(version["valid_to"]))
    )
    policy = next(
        item
        for item in data["policies"]
        if item["policy_id"] == scenario["policy_id"] and item["scope"] == data["scope"]
    )
    expected = (
        f"POLICY: {policy['policy_id']}@{data['versions'][index]['version']}; "
        f"CODE: {policy['actions'][index]}"
    )
    journals = row["after"]["journals"]
    note = journals[0].get("notes", "") if len(journals) == 1 else ""
    marker = MARKER.search(note)
    body = note[: marker.start()] if marker else note
    stages = row["stage_results"]
    receipts = stages[-1]["result"]["verified_receipts"] if stages else []
    writes = [
        request
        for request in row["http_requests"]
        if request["method"] not in ("GET", "HEAD", "OPTIONS")
    ]
    receipt_matches = (
        len(receipts) == len(journals) == 1
        and marker is not None
        and receipts[0].get("verified") is True
        and receipts[0].get("operation_id") == marker.group(1)
        and receipts[0].get("journal_id") == journals[0]["id"]
        and receipts[0].get("ticket_id") == row["after"]["id"]
        and receipts[0].get("after", {}).get("journals") == journals
    )
    checks = {
        "exactly_one_journal": len(journals) == 1,
        "exact_note_body": body == expected,
        "receipt_matches_journal": receipt_matches,
        "one_matching_http_write": len(writes) == 1
        and writes[0]["method"] == "PUT"
        and writes[0]["path"] == f"/issues/{row['after']['id']}.json"
        and writes[0]["body"] == {"issue": {"notes": note}},
        "other_fields_preserved": all(
            row["after"].get(key) == value
            for key, value in row["before"].items()
            if key != "journals"
        ),
        "prepare_writes_zero": not scenario["resume"]
        or (
            len(stages) == 2
            and stages[0]["prepare"] is True
            and stages[0]["journal_count"] == 0
        ),
    }
    return {
        "success": all(checks.values()) and row["error"] is None,
        "expected_note": expected,
        "actual_note": body,
        "checks": checks,
    }


def trace_metrics(folder: Path, row: dict) -> dict:
    traces = [
        read(folder / stage["result"]["trace_path"])
        for stage in row["stage_results"]
    ]
    traces.extend(read(path) for path in sorted(folder.glob("failed-*.json")))
    calls = [call for trace in traces for call in trace["model_calls"]]
    if len({call["run_id"] for call in calls}) != len(calls):
        raise ValueError(f"Duplicate model request identifiers in {row['id']}")
    known = [call["usage"] for call in calls if call["usage"] is not None]
    seen = set()
    tools = Counter()
    reads = []
    for trace in traces:
        for message in trace["messages"]:
            for call in message["data"].get("tool_calls", []):
                if call["id"] in seen:
                    continue
                seen.add(call["id"])
                tools[call["name"]] += 1
                if call["name"] == "read_policy":
                    reads.append(call["args"])
    return {
        "model_requests": len(calls),
        "unknown_usage_requests": len(calls) - len(known),
        "input_tokens": sum(usage.get("input_tokens", 0) for usage in known),
        "output_tokens": sum(usage.get("output_tokens", 0) for usage in known),
        "cache_read_tokens": sum(
            (usage.get("input_token_details") or {}).get("cache_read", 0) for usage in known
        ),
        "tools": dict(tools),
        "policy_reads": reads,
    }


def analyze(directory: Path) -> dict:
    protocol = read(directory / "protocol.json")
    recorded = read(directory / "summary.json")
    retrieval = read(directory / "retrieval.json")
    episodes = read(directory / "episodes.json")
    data = protocol["dataset"]
    issues = []
    expected_queries = Counter(
        (query["id"], arm) for query in data["queries"] for arm in ARMS
    )
    observed_queries = Counter(
        (row["query"]["id"], row["arm"]) for row in retrieval["rows"]
    )
    if expected_queries != observed_queries:
        issues.append("retrieval-query-schedule")
    queries = {query["id"]: query for query in data["queries"]}
    for row in retrieval["rows"]:
        query = queries[row["query"]["id"]]
        if row["query"] != query:
            issues.append(f"query-content:{query['id']}:{row['arm']}")
        if retrieval_metrics(query, row["results"]) != row["metrics"]:
            issues.append(f"retrieval-metrics:{query['id']}:{row['arm']}")
    retrieval_summary = {}
    for arm in ARMS:
        selected = [row for row in retrieval["rows"] if row["arm"] == arm]
        answerable = [row for row in selected if row["metrics"]["answerable"]]
        empty = [row for row in selected if row["query"]["kind"] == "empty-eligible"]
        retrieval_summary[arm] = {
            "queries": len(selected),
            "answerable_queries": len(answerable),
            "hit_at_1": sum(row["metrics"]["hit_at_1"] for row in answerable),
            "hit_at_3": sum(row["metrics"]["hit_at_3"] for row in answerable),
            "returned_documents": sum(row["metrics"]["returned"] for row in selected),
            "time_inapplicable_returned": sum(
                row["metrics"]["time_inapplicable_returned"] for row in selected
            ),
            "out_of_scope_returned": sum(
                row["metrics"]["out_of_scope_returned"] for row in selected
            ),
            "empty_eligible_cases": len(empty),
            "correct_empty_results": sum(row["metrics"]["empty_result"] for row in empty),
            "unrelated_query_returns": [
                row["metrics"]["returned"]
                for row in selected
                if row["query"]["kind"] == "unrelated"
            ],
        }
    if retrieval_summary != recorded["retrieval"] or retrieval_summary != retrieval["summary"]:
        issues.append("retrieval-summary")
    expected_episodes = [
        f"{item['scenario']['id']}--{item['arm']}--r{item['repeat']}"
        for item in protocol["order"]
    ]
    if [row["id"] for row in episodes] != expected_episodes:
        issues.append("episode-schedule")
    scenarios = {item["id"]: item for item in data["agent_scenarios"]}
    strict_rows = []
    for row in episodes:
        folder = directory / "episodes" / row["id"]
        if read(folder / "result.json") != row:
            issues.append(f"episode-copy:{row['id']}")
        metrics = trace_metrics(folder, row)
        if metrics != row["metrics"]:
            issues.append(f"trace-metrics:{row['id']}")
        strict = strict_business(row, scenarios[row["scenario"]], data)
        if strict["success"] != row["success"]:
            issues.append(f"strict-business-disagreement:{row['id']}")
        strict_rows.append({"id": row["id"], "arm": row["arm"], **strict})
    agent_summary = {}
    for arm in ARMS:
        selected = [row for row in episodes if row["arm"] == arm]
        agent_summary[arm] = {
            "started": len(selected),
            "successes": sum(row["success"] is True for row in selected),
            "failures": sum(row["success"] is False for row in selected),
            "infrastructure_errors": sum(row["success"] is None for row in selected),
            **{key: sum(row["metrics"][key] for row in selected) for key in USAGE_KEYS},
            "search_used_episodes": sum(
                row["metrics"]["tools"].get("search_policy", 0) > 0 for row in selected
            ),
        }
    if agent_summary != recorded["agent"]:
        issues.append("agent-summary")
    files = {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*.json"))
        if "private-state" not in path.parts and path.name != "offline-analysis.json"
    }
    return {
        "schema_version": 1,
        "analysis": "offline-strict-policy-evidence",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "input_files": files,
        "issues": issues,
        "verified": not issues,
        "retrieval": retrieval_summary,
        "agent": agent_summary,
        "strict_business": strict_rows,
        "planned_episodes": protocol["planned_episodes"],
        "recorded_episodes": len(episodes),
        "interpretation": (
            "Post-run exact-note and receipt-link audit. Original frozen scores are preserved."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({key: result[key] for key in ("verified", "issues", "recorded_episodes")}))
    raise SystemExit(0 if result["verified"] else 1)


if __name__ == "__main__":
    main()
