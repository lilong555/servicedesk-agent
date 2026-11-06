#!/usr/bin/env python3
"""Paired policy applicability experiments using the production Agent and policy catalog."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import subprocess
import time
from collections import Counter
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import httpx
from dotenv import load_dotenv
from langgraph.store.sqlite import SqliteStore

from servicedesk_agent.components import build_components
from servicedesk_agent.config import ModelSettings, RedmineSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.knowledge import PolicyCatalog, PolicyVersion
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "benchmarks/policy-lifecycle/cases.json"
ARMS = ("unfiltered_time", "filtered_time")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def documents(data: dict) -> list[PolicyVersion]:
    result = []
    for policy in data["policies"]:
        for index, interval in enumerate(data["versions"]):
            result.append(
                PolicyVersion(
                    **interval,
                    policy_id=policy["policy_id"],
                    scope=policy["scope"],
                    source_uri=(
                        f"https://policies.example.test/{policy['scope']}/"
                        f"{policy['policy_id']}/{interval['version']}"
                    ),
                    text=(
                        f"{policy['title']}. This policy covers {policy['topic']}. "
                        f"{policy['instructions'][index]} "
                        f"The action code is {policy['actions'][index]}. "
                        "Record the applicable policy ID, version and action code in the ticket. "
                        "The policy does not itself grant authority to restart or close tickets."
                    ),
                )
            )
    return result


class ExperimentalCatalog(PolicyCatalog):
    """The control removes only search-time validity filtering, preserving scope and reading."""

    def __init__(self, source, scope: str, *, filter_time: bool, clock: datetime):
        super().__init__(source, scope)
        self.filter_time = filter_time
        self.clock = clock

    def search(self, query: str, *, top_k: int = 3, now: datetime | None = None):
        clock = now or self.clock
        if self.filter_time:
            return super().search(query, top_k=top_k, now=clock)
        if not query.strip() or not 1 <= top_k <= 5:
            return super().search(query, top_k=top_k, now=clock)
        self._load()
        candidates = {item.key: item for item in self._documents if item.scope == self.scope}
        if not candidates:
            return []
        result = self._retriever.run(
            query=query,
            top_k=top_k,
            filters={"field": "meta.scope", "operator": "==", "value": self.scope},
        )
        return [
            {
                "policy_id": candidates[item.id].policy_id,
                "version": candidates[item.id].version,
                "evidence": candidates[item.id].evidence(clock, limit=700).model_dump(mode="json"),
            }
            for item in result["documents"]
        ]

    def read(self, policy_id: str, version: str = "", *, now: datetime | None = None):
        return super().read(policy_id, version, now=now or self.clock)


class TicketBackend:
    """Isolated synthetic HTTP service; business mutations run through RedmineClient."""

    def __init__(self):
        self.statuses = [
            {"id": 1, "name": "New", "is_closed": False},
            {"id": 2, "name": "In Progress", "is_closed": False},
            {"id": 5, "name": "Closed", "is_closed": True},
        ]
        self.issue = {
            "id": 42,
            "project": {"id": 1, "name": "Corporate IT"},
            "subject": "Service investigation",
            "description": "Investigate and document the authorized handling procedure.",
            "status": self.statuses[0],
            "assigned_to": {"id": 7, "name": "Service owner"},
            "updated_on": "2026-11-04T00:00:00Z",
            "journals": [],
            "relations": [],
            "allowed_statuses": self.statuses,
        }
        self.requests: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": request.url.path, "body": body})
        if request.method == "GET" and request.url.path == "/users/current.json":
            return httpx.Response(200, json={"user": {"id": 1}})
        if request.method == "GET" and request.url.path == "/issue_statuses.json":
            return httpx.Response(200, json={"issue_statuses": self.statuses})
        if request.method == "GET" and request.url.path == "/issues/42.json":
            return httpx.Response(200, json={"issue": copy.deepcopy(self.issue)})
        if request.method == "PUT" and request.url.path == "/issues/42.json":
            update = body["issue"]
            if "status_id" in update:
                self.issue["status"] = next(
                    status for status in self.statuses if status["id"] == update["status_id"]
                )
            self.issue["journals"].append(
                {
                    "id": len(self.issue["journals"]) + 1,
                    "notes": update["notes"],
                    "user": {"id": 1},
                    "details": [],
                }
            )
            return httpx.Response(204)
        return httpx.Response(404)


def score_retrieval(query: dict, results: list[dict]) -> dict:
    gold = {tuple(value) for value in query["gold"]}
    pairs = [(item["policy_id"], item["version"]) for item in results]
    clock = at(query["time"])
    invalid = sum(
        at(item["evidence"]["valid_from"]) > clock
        or (item["evidence"]["valid_to"] is not None and clock >= at(item["evidence"]["valid_to"]))
        for item in results
    )
    outside = sum(item["evidence"]["entity_scope"] != query["scope"] for item in results)
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


def retrieval_experiment(data: dict) -> dict:
    rows = []
    for query in data["queries"]:
        for arm in ARMS:
            catalog = ExperimentalCatalog(
                documents(data),
                query["scope"],
                filter_time=arm == "filtered_time",
                clock=at(query["time"]),
            )
            found = catalog.search(query["query"], top_k=3)
            rows.append(
                {
                    "query": query,
                    "arm": arm,
                    "results": found,
                    "metrics": score_retrieval(query, found),
                }
            )
    summary = {}
    for arm in ARMS:
        selected = [row for row in rows if row["arm"] == arm]
        answerable = [row for row in selected if row["metrics"]["answerable"]]
        empty = [row for row in selected if row["query"]["kind"] == "empty-eligible"]
        summary[arm] = {
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
    return {"corpus_documents": len(documents(data)), "rows": rows, "summary": summary}


def contract() -> TaskContract:
    return TaskContract(
        service_url="https://desk.example.test",
        project_id=1,
        principal_id=1,
        ticket_id=42,
        session_id="policy-investigation",
        variant="B",
        allow_notes=True,
        user_constraints=(
            "Keep the ticket open and leave its status unchanged.",
            "Add at most one note after explicit authorization in the latest user message.",
        ),
    )


def task_message(scenario: dict, *, prepare: bool = False) -> str:
    if prepare:
        return (
            f"The scenario clock is 2026-11-04T00:00:00Z. Investigate {scenario['title']} "
            f"for this ticket using policy {scenario['policy_id']}. Read the policy now, "
            "but do not add notes or change status yet. Save the pending work and handoff. "
            "I will authorize a note in a later message."
        )
    return (
        f"The scenario clock is 2026-11-06T00:00:00Z. Investigate {scenario['title']} "
        f"and consult policy {scenario['policy_id']} applicable at this time. "
        "You are now authorized to add exactly one note; preserve the ticket status. "
        "Write the applicable policy and action code in this form: "
        "'POLICY: <policy_id>@<version>; CODE: <action code>'. "
        "Obtain the version and code from policy evidence. Finish after the note is verified."
    )


def score_business(scenario: dict, data: dict, backend: TicketBackend, stages: list[dict]) -> dict:
    definition = next(
        item
        for item in data["policies"]
        if item["policy_id"] == scenario["policy_id"] and item["scope"] == data["scope"]
    )
    current_index = next(
        index
        for index, interval in enumerate(data["versions"])
        if at(interval["valid_from"]) <= at("2026-11-06T00:00:00Z")
        and (interval["valid_to"] is None or at("2026-11-06T00:00:00Z") < at(interval["valid_to"]))
    )
    expected = f"{scenario['policy_id']}@{data['versions'][current_index]['version']}"
    notes = [item["notes"] for item in backend.issue["journals"]]
    receipts = stages[-1]["result"]["verified_receipts"] if stages else []
    checks = {
        "exactly_one_note": len(notes) == 1,
        "correct_policy_version": len(notes) == 1 and expected in notes[0],
        "correct_action": len(notes) == 1 and definition["actions"][current_index] in notes[0],
        "unchanged_status": backend.issue["status"]["id"] == 1,
        "verified_receipt": len(receipts) == 1,
        "prepare_writes_zero": not scenario["resume"]
        or (bool(stages) and stages[0]["journal_count"] == 0),
    }
    return {"success": all(checks.values()), "checks": checks}


def trace_metrics(stages: list[dict]) -> dict:
    calls = [call for stage in stages for call in stage["trace"]["model_calls"]]
    seen = set()
    tools = Counter()
    policy_versions = []
    messages = stages[-1]["trace"]["messages"] if stages else []
    for message in messages:
        record = message["data"]
        for call in record.get("tool_calls", []):
            if call["id"] not in seen:
                tools[call["name"]] += 1
                seen.add(call["id"])
                if call["name"] == "read_policy":
                    policy_versions.append(call["args"])
    known = [call["usage"] for call in calls if call["usage"] is not None]
    return {
        "model_requests": len(calls),
        "unknown_usage_requests": len(calls) - len(known),
        "input_tokens": sum(usage.get("input_tokens", 0) for usage in known),
        "output_tokens": sum(usage.get("output_tokens", 0) for usage in known),
        "cache_read_tokens": sum(
            (usage.get("input_token_details") or {}).get("cache_read", 0) for usage in known
        ),
        "tools": dict(tools),
        "policy_reads": policy_versions,
    }


def run_scenario(
    data: dict, scenario: dict, arm: str, repeat: int, output: Path, settings: ModelSettings
) -> dict:
    identity = f"{scenario['id']}--{arm}--r{repeat}"
    folder = output / "episodes" / identity
    folder.mkdir(parents=True, exist_ok=False)
    state_root = output / "private-state" / identity
    state_root.mkdir(parents=True)
    backend = TicketBackend()
    before = copy.deepcopy(backend.issue)
    authority = contract()
    policy = ExperimentalCatalog(
        documents(data),
        data["scope"],
        filter_time=arm == "filtered_time",
        clock=at("2026-11-04T00:00:00Z") if scenario["resume"] else at("2026-11-06T00:00:00Z"),
    )
    stages = []
    error = None
    started = time.monotonic()
    model = None
    try:
        model = settings.build()
        with SqliteStore.from_conn_string(str(state_root / "store.sqlite")) as store:
            store.setup()
            components = build_components(authority.scope, store, policy)
            for tool in components.tools:
                if tool.name == "search_policy":
                    tool.description = (
                        "Search scoped policy candidates by BM25 and return versioned evidence. "
                        "Inspect validity dates before applying a version."
                    )
            with RedmineClient(
                RedmineSettings(url=authority.service_url, api_key="synthetic-test-key"),
                store=store,
                transport=httpx.MockTransport(backend.handle),
            ) as client:
                for prepare in [True, False] if scenario["resume"] else [False]:
                    if not prepare:
                        policy.clock = at("2026-11-06T00:00:00Z")
                    result = execute_session(
                        state_root,
                        client,
                        authority,
                        model,
                        task_message(scenario, prepare=prepare),
                        resume=scenario["resume"] and not prepare,
                        components=components,
                        model_settings=settings,
                        recursion_limit=50,
                    )
                    trace = read_json(Path(result["trace_path"]))
                    result["trace_path"] = f"stage-{len(stages)}-trace.json"
                    save_json(folder / result["trace_path"], trace)
                    stages.append(
                        {
                            "prepare": prepare,
                            "result": result,
                            "trace": trace,
                            "journal_count": len(backend.issue["journals"]),
                        }
                    )
    except Exception as exc:
        error = {
            "type": type(exc).__name__,
            "message": str(exc).replace(settings.api_key.get_secret_value(), "[redacted]")[:1000],
        }
    finally:
        if model is not None:
            model.root_client.close()
    grading = score_business(scenario, data, backend, stages)
    if error:
        grading["success"] = None
    metrics = trace_metrics(stages)
    # Failed session traces also carry request accounting.
    known_runs = {stage["trace"]["run_id"] for stage in stages}
    failed_traces = []
    for trace_path in state_root.glob("sessions/*/trace-*.json"):
        trace = read_json(trace_path)
        if trace["run_id"] not in known_runs:
            failed_traces.append(trace)
            save_json(folder / f"failed-{trace['run_id']}.json", trace)
            for call in trace["model_calls"]:
                metrics["model_requests"] += 1
                if call["usage"] is None:
                    metrics["unknown_usage_requests"] += 1
                else:
                    metrics["input_tokens"] += call["usage"].get("input_tokens", 0)
                    metrics["output_tokens"] += call["usage"].get("output_tokens", 0)
                    metrics["cache_read_tokens"] += (
                        call["usage"].get("input_token_details") or {}
                    ).get("cache_read", 0)
    row = {
        "id": identity,
        "scenario": scenario["id"],
        "arm": arm,
        "repeat": repeat,
        "backend": "synthetic-http-redmine",
        "model_mode": "live",
        "elapsed_seconds": time.monotonic() - started,
        **grading,
        "error": error,
        "metrics": metrics,
        "before": before,
        "after": backend.issue,
        "http_requests": backend.requests,
        "stage_results": [{k: v for k, v in stage.items() if k != "trace"} for stage in stages],
        "failed_trace_count": len(failed_traces),
    }
    save_json(folder / "result.json", row)
    return row


def implementation_identity() -> dict:
    paths = [
        Path(__file__).resolve(),
        CASES,
        *sorted((ROOT / "src/servicedesk_agent").glob("*.py")),
    ]
    hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    }
    return {
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "files": hashes,
        "implementation_sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest(),
    }


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for arm in ARMS:
        selected = [row for row in rows if row["arm"] == arm]
        summary[arm] = {
            "started": len(selected),
            "successes": sum(row["success"] is True for row in selected),
            "failures": sum(row["success"] is False for row in selected),
            "infrastructure_errors": sum(row["success"] is None for row in selected),
            **{
                key: sum(row["metrics"][key] for row in selected)
                for key in (
                    "model_requests",
                    "unknown_usage_requests",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                )
            },
            "search_used_episodes": sum(
                row["metrics"]["tools"].get("search_policy", 0) > 0 for row in selected
            ),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".runtime/cpa.env")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    os.environ.setdefault("HAYSTACK_TELEMETRY_ENABLED", "false")
    logging.getLogger("haystack").setLevel(logging.ERROR)
    data = read_json(CASES)
    if args.live:
        load_dotenv(args.env_file, override=True)
        settings = ModelSettings.from_env()
        settings.require_live()
    else:
        settings = None
    args.output.mkdir(parents=True, exist_ok=False)
    order = (
        [
            {"scenario": scenario, "arm": arm, "repeat": repeat}
            for repeat in range(args.repeats)
            for index, scenario in enumerate(data["agent_scenarios"])
            for arm in (ARMS if (repeat + index) % 2 == 0 else ARMS[::-1])
        ]
        if args.live
        else []
    )
    protocol = {
        "name": "policy-lifecycle-v1",
        "split": "synthetic-development",
        "source": implementation_identity(),
        "dataset": data,
        "independent_variable": "search candidate valid-time filter; scope and read are fixed",
        "model": (
            {
                "name": settings.name,
                "temperature": 0,
                "max_tokens": settings.max_tokens,
                "timeout": settings.timeout,
                "client_max_retries": 0,
            }
            if settings
            else None
        ),
        "packages": {
            package: version(package)
            for package in ("deepagents", "langgraph", "haystack-ai", "langmem", "langchain-openai")
        },
        "order": order,
        "planned_episodes": len(order),
        "grading": [
            "one note",
            "current policy ID/version",
            "matching action code",
            "unchanged ticket status",
            "verified receipt",
            "no prepare-stage write",
        ],
        "notes": [
            "Real model, synthetic policy documents and HTTP business service.",
            "Policy lifecycle resumes the same production thread and task contract.",
            "Retrieval metrics and downstream business outcomes are separate.",
            "No retries or exclusions of failed episodes; setup failures are retained.",
        ],
    }
    save_json(args.output / "protocol.json", protocol)
    save_json(
        args.output / "policies.json", [item.model_dump(mode="json") for item in documents(data)]
    )
    retrieval = retrieval_experiment(data)
    save_json(args.output / "retrieval.json", retrieval)
    rows = []
    for entry in order:
        row = run_scenario(
            data, entry["scenario"], entry["arm"], entry["repeat"], args.output, settings
        )
        rows.append(row)
        save_json(args.output / "episodes.json", rows)
        print(
            json.dumps({"id": row["id"], "success": row["success"], "error": row["error"]}),
            flush=True,
        )
    result = {"retrieval": retrieval["summary"], "agent": summarize(rows)}
    save_json(args.output / "summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
