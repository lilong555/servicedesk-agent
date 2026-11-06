#!/usr/bin/env python3
"""Frozen confirmed-preference experiment using the production session and memory paths."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import re
import subprocess
import tarfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import messages_to_dict
from langchain_core.utils.function_calling import convert_to_openai_tool

from servicedesk_agent.components import (
    build_components,
    memory_namespace,
    remember_confirmed_preference,
)
from servicedesk_agent.config import ModelSettings, RedmineSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.knowledge import PolicyCatalog, PolicyVersion
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session
from servicedesk_agent.usage import UsageRecorder

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "benchmarks/preference-memory/scenarios.json"
CONDITIONS = ("available", "empty")
MAX_USER_TURNS = 3
POLICY = "Append only an authorized ticket note. Keep status, owner and dependencies unchanged."
MEMORY_GUIDANCE = (
    "The namespace already fixes the current service, project, API identity and ticket. "
    "All records in it are explicitly confirmed preferences. Use filter=null to retrieve them; "
    "the native stored value is an Evidence record nested under content. "
    "The query string is passed to the native SQLite search."
)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def write(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def output_directory(path):
    path = Path(path).absolute()
    if path.parent != ROOT / "results/runs" or not path.name.startswith("preference-memory-"):
        raise ValueError("Use a new results/runs/preference-memory-* directory.")
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return path


def source_paths():
    paths = [
        Path(__file__),
        DATA,
        ROOT / "tests/test_preference_memory_benchmark.py",
        ROOT / "tests/conftest.py",
        ROOT / "pyproject.toml",
    ]
    paths.extend(
        p
        for p in (ROOT / "src/servicedesk_agent").rglob("*")
        if p.is_file() and p.suffix in {".py", ".json", ".md"}
    )
    return sorted(paths)


def source_manifest():
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths()
    }


def schedule(phase):
    ids = [s["id"] for s in read(DATA)["scenarios"]]
    if phase == "pilot":
        ids = ["note_reference", "override_reference"]
    rows = []
    for repeat in range(1 if phase == "pilot" else 2):
        for index, scenario in enumerate(ids):
            order = CONDITIONS if (index + repeat) % 2 == 0 else CONDITIONS[::-1]
            for condition in order:
                rows.append({"scenario": scenario, "repeat": repeat + 1, "condition": condition})
    return rows


def public_settings(settings):
    return settings.model_dump(mode="json", exclude={"api_key", "base_url"})


def freeze(output, settings):
    output = output_directory(output)
    sources = source_manifest()
    protocol = {
        "schema_version": 1,
        "split": "synthetic_development",
        "model": public_settings(settings),
        "sampling": {
            "temperature": 0,
            "max_retries": 0,
            "parallel_tool_calls": False,
            "provider_seed": None,
        },
        "scenario_sha256": hashlib.sha256(DATA.read_bytes()).hexdigest(),
        "source_files": sources,
        "implementation_sha256": digest(sources),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in (
                "deepagents",
                "langchain",
                "langchain-core",
                "langchain-openai",
                "langgraph",
                "langgraph-checkpoint-sqlite",
                "langmem",
                "haystack-ai",
                "openai",
                "httpx",
            )
        },
        "conditions": {
            "available": "Native LangMem confirmed preference search result.",
            "empty": "Same native search and schema; return the agreed empty JSON list.",
        },
        "shared_memory_parameter_guidance": MEMORY_GUIDANCE,
        "pilot_schedule": schedule("pilot"),
        "formal_schedule": schedule("formal"),
        "max_user_turns": MAX_USER_TURNS,
        "recursion_limit": 48,
        "per_attempt_model_call_limit": 36,
        "backend": "Synthetic RedmineFixture through an actual loopback HTTP server.",
        "scope": "Same server URL/project=1/API principal=1/ticket=42 within a run.",
        "reset": "New fixture and SQLite Store per condition/repeat. Store closed after explicit "
        "confirmation, reopened for a new thread with no previous conversation.",
        "cache": "Application state reset; provider prompt cache uncontrolled and measured.",
        "setup": "Production remember_confirmed_preference; explicit synthetic confirmation; "
        "zero setup model requests.",
        "clarification": "Question classification by frozen terms, followed by the scenario's "
        "fixed answer. No correction after any business write.",
        "grading": "Exactly one matching note, one verified journal receipt, no collateral "
        "business change. Empty recall is eligible for full task success.",
        "failure_policy": "Retain every started execution, failed call and unknown usage. "
        "No automatic episode retry; planned/unstarted counts retained.",
        "pilot_gate": "Four scored executions with complete usage, no initial preference leaks, "
        "actual nonempty/empty recall branches, and at least one verified note in each condition. "
        "Scored business failures are retained and do not by themselves block formal execution.",
        "primary_metric": "Preference clarification turns over all started history-required runs.",
        "secondary_metrics": [
            "task_success",
            "current_instruction_priority",
            "model_calls",
            "logical_input_tokens",
            "output_tokens",
            "cache_read_tokens",
        ],
    }
    record = {
        "protocol": protocol,
        "freeze_id": digest(protocol),
        "created_at": datetime.now(UTC).isoformat(),
    }
    with tarfile.open(output / "source.tar.gz", "w:gz") as archive:
        for path in source_paths():
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    record["source_archive_sha256"] = hashlib.sha256(
        (output / "source.tar.gz").read_bytes()
    ).hexdigest()
    write(output / "freeze.json", record)
    return record


def validate_freeze(path, settings):
    record = read(path)
    if digest(record["protocol"]) != record["freeze_id"]:
        raise ValueError("Freeze hash mismatch.")
    if source_manifest() != record["protocol"]["source_files"]:
        raise ValueError("Frozen implementation changed; create a separate freeze.")
    if public_settings(settings) != record["protocol"]["model"]:
        raise ValueError("Model settings differ from the freeze.")
    return record


def fixture_type():
    spec = importlib.util.spec_from_file_location(
        "_preference_memory_redmine_fixture", ROOT / "tests/conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RedmineFixture


@contextmanager
def synthetic_http():
    state = {"backend": fixture_type()(), "http": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.respond()

        def do_PUT(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = httpx.Request(self.command, f"http://127.0.0.1{self.path}", content=body)
            response = state["backend"].handle(request)
            state["http"].append(
                {
                    "method": self.command,
                    "path": self.path,
                    "status": response.status_code,
                    "body": json.loads(body) if body else None,
                }
            )
            content = response.content
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def component_bundle(scope, store, condition, searches):
    if condition not in CONDITIONS:
        raise ValueError("Unknown recall condition.")
    catalog = PolicyCatalog(
        [
            PolicyVersion(
                policy_id="note-safety",
                version="1",
                source_uri="synthetic://note-safety/v1",
                scope="memory-benchmark",
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                text=POLICY,
            )
        ],
        "memory-benchmark",
    )
    bundle = build_components(scope, store, catalog)
    for file in bundle.initial_state["files"].values():
        file["created_at"] = file["modified_at"] = "2026-11-06T00:00:00+00:00"
    original = next(t for t in bundle.tools if t.name == "recall_confirmed_preferences")

    def project(value, kwargs):
        returned = value if condition == "available" else "[]"
        searches.append(
            {
                "arguments": kwargs,
                "stored_result": json.loads(value),
                "returned_result": json.loads(returned),
            }
        )
        return returned

    def search(query: str, *, limit: int = 10, offset: int = 0, filter=None):
        kwargs = {"query": query, "limit": limit, "offset": offset, "filter": filter}
        return project(original.func(**kwargs), kwargs)

    async def asearch(query: str, *, limit: int = 10, offset: int = 0, filter=None):
        kwargs = {"query": query, "limit": limit, "offset": offset, "filter": filter}
        return project(await original.coroutine(**kwargs), kwargs)

    replacement = original.model_copy(
        update={
            "func": search,
            "coroutine": asearch,
            "description": original.description + "\n" + MEMORY_GUIDANCE,
        }
    )
    bundle.tools = [replacement if t is original else t for t in bundle.tools]
    return bundle


class RequestCapture(BaseCallbackHandler):
    raise_error = True

    def __init__(self):
        self.requests = []

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if len(self.requests) >= 36:
            raise RuntimeError("Frozen model call limit reached.")
        self.requests.append(
            {
                "run_id": str(run_id),
                "messages": [messages_to_dict(group) for group in messages],
                "tools": kwargs.get("invocation_params", {}).get("tools", []),
            }
        )


def clarification(scenario, response):
    text = response.casefold()
    question = "?" in text or any(
        term in text
        for term in (
            "please provide",
            "please confirm",
            "please share",
            "could you",
            "can you",
            "need your",
            "need the",
            "don't have",
            "do not have",
            "not available",
        )
    )
    if question and any(term in text for term in scenario["question_terms"]):
        return "preference_clarification", scenario["clarification"]
    return "task_followup", (
        "Please complete the requested authorized note. If a required preference detail is "
        "missing, ask me specifically for that detail before writing."
    )


def normalized(text):
    return " ".join(text.split())


def grade(scenario, before, after, writes, receipts):
    notes = [re.sub(r"\n\n\[desk-action:[^\]]+\]$", "", w.get("notes", "")) for w in writes]
    preserved = all(after.get(k) == v for k, v in before.items() if k != "journals")
    journals = after.get("journals", [])
    checked = {
        "one_http_write": len(writes) == 1,
        "note_matches": len(notes) == 1
        and normalized(notes[0]) == normalized(scenario["expected_note"]),
        "one_journal": len(journals) == 1,
        "no_collateral_change": preserved,
        "verified_receipt": len(receipts) == 1
        and receipts[0].get("verified") is True
        and len(journals) == 1
        and receipts[0]["journal_id"] == journals[0]["id"],
    }
    return {"success": all(checked.values()), "checks": checked, "actual_notes": notes}


def run_attempt(directory, scenario, condition, repeat, url, state, settings, model=None):
    directory.mkdir(mode=0o700)
    root = directory / "state"
    state["backend"], state["http"] = fixture_type()(), []
    backend = state["backend"]
    backend.issue["subject"] = "Synthetic service investigation"
    backend.issue["description"] = "Record the user's authorized investigation update."
    before = copy.deepcopy(backend.issue)
    contract = TaskContract(
        service_url=url,
        project_id=1,
        principal_id=1,
        ticket_id=42,
        session_id="new-conversation",
        variant="B",
        allow_notes=True,
        user_constraints=(),
        allowed_status_ids=(),
        allow_close=False,
    )
    source_contract = contract.model_copy(update={"session_id": "confirmation-session"})
    setup_started = time.monotonic()
    with redmine_store(root) as store:
        remember_confirmed_preference(contract.scope, scenario["confirmed_preference"], store)
        stored = store.search(memory_namespace(contract.scope))
        setup = {
            "mode": "explicit_confirmation_without_model",
            "model_calls": 0,
            "source_contract": source_contract.model_dump(mode="json"),
            "target_contract": contract.model_dump(mode="json"),
            "namespace": list(memory_namespace(contract.scope)),
            "memory": [item.dict() for item in stored],
            "store_closed_before_execution": True,
            "elapsed_seconds": time.monotonic() - setup_started,
        }
    write(directory / "setup.json", setup)
    write(directory / "before.json", before)
    recorder, capture = UsageRecorder("agent"), RequestCapture()
    searches, turns, results, error_type = [], [], [], None
    input_message = read(DATA)["shared_request"] + "\n\n" + scenario["request"]
    supplied = 0
    started = time.monotonic()
    target = None
    try:
        target = model if model is not None else settings.build()
        target = target.model_copy(update={"callbacks": [recorder, capture]})
        with (
            redmine_store(root) as store,
            RedmineClient(
                RedmineSettings(url=url, api_key="synthetic-fixture-key", project_id=1),
                store=store,
            ) as client,
        ):
            bundle = component_bundle(contract.scope, store, condition, searches)
            write(
                directory / "tool_schemas.json", [convert_to_openai_tool(t) for t in bundle.tools]
            )
            for turn in range(MAX_USER_TURNS):
                response = execute_session(
                    root,
                    client,
                    contract,
                    target,
                    input_message,
                    resume=turn > 0,
                    components=bundle,
                    model_settings=settings,
                    recursion_limit=48,
                )
                results.append(response)
                turns.append(
                    {"turn": turn + 1, "user": input_message, "assistant": response["message"]}
                )
                if backend.writes or turn + 1 == MAX_USER_TURNS:
                    break
                kind, input_message = clarification(scenario, response["message"])
                supplied += kind == "preference_clarification"
                turns[-1]["followup_kind"] = kind
    except Exception as exc:
        error_type = type(exc).__name__
    finally:
        if target is not None and model is None:
            target.root_client.close()
            import asyncio

            asyncio.run(target.root_async_client.close())
    elapsed = time.monotonic() - started
    receipts = results[-1]["verified_receipts"] if results else []
    grading = grade(scenario, before, backend.issue, backend.writes, receipts)
    first = json.dumps(capture.requests[0]["messages"]) if capture.requests else ""
    leakage = [marker for marker in scenario["private_markers"] if marker in first]
    result = {
        "scenario": scenario["id"],
        "kind": scenario["kind"],
        "condition": condition,
        "repeat": repeat,
        "started": True,
        "success": grading["success"] if error_type is None else None,
        "business_grade": grading,
        "error_type": error_type,
        "preference_clarifications": supplied,
        "user_turns": len(turns),
        "memory_searches": len(searches),
        "nonempty_recalls": sum(bool(s["returned_result"]) for s in searches),
        "new_thread": contract.thread_id != source_contract.thread_id,
        "first_request_preference_leaks": leakage,
        "usage": recorder.summary(settings),
        "captured_model_requests": len(capture.requests),
        "execution_seconds": elapsed,
        "setup_seconds": setup["elapsed_seconds"],
        "total_seconds": elapsed + setup["elapsed_seconds"],
        "model_mode": "scripted" if model is not None else "live",
        "final_receipts": receipts,
    }
    for name, value in (
        ("after.json", backend.issue),
        ("http.json", state["http"]),
        ("writes.json", backend.writes),
        ("turns.json", turns),
        ("sessions.json", results),
        ("model_calls.json", recorder.calls),
        ("model_requests.json", capture.requests),
        ("memory_searches.json", searches),
        ("result.json", result),
    ):
        write(directory / name, value)
    return result


def pilot_gate(episodes):
    return (
        len(episodes) == 4
        and all(
            r["success"] is not None
            and not r["first_request_preference_leaks"]
            and r["usage"]["unknown_usage_calls"] == 0
            and r["usage"]["failed_calls"] == 0
            for r in episodes
        )
        and all(
            any(r["condition"] == c and r["success"] is True for r in episodes) for c in CONDITIONS
        )
        and any(r["nonempty_recalls"] > 0 for r in episodes if r["condition"] == "available")
        and any(
            r["memory_searches"] > 0 and r["nonempty_recalls"] == 0
            for r in episodes
            if r["condition"] == "empty"
        )
    )


def summarize(episodes):
    groups = {}
    for condition in CONDITIONS:
        rows = [r for r in episodes if r["condition"] == condition]
        groups[condition] = {
            "started": len(rows),
            "successes": sum(r["success"] is True for r in rows),
            "scored": sum(r["success"] is not None for r in rows),
            "infrastructure_errors": sum(r["success"] is None for r in rows),
            "preference_clarifications": sum(r["preference_clarifications"] for r in rows),
            "memory_searches": sum(r["memory_searches"] for r in rows),
            "usage": {
                key: sum(r["usage"][key] for r in rows)
                for key in (
                    "calls",
                    "failed_calls",
                    "unknown_usage_calls",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                )
            },
            "by_kind": {
                kind: {
                    "started": sum(r["kind"] == kind for r in rows),
                    "successes": sum(r["kind"] == kind and r["success"] is True for r in rows),
                    "preference_clarifications": sum(
                        r["preference_clarifications"] for r in rows if r["kind"] == kind
                    ),
                    "calls": sum(r["usage"]["calls"] for r in rows if r["kind"] == kind),
                }
                for kind in ("history_required", "current_override")
            },
        }
    pairs = []
    for scenario, repeat in sorted({(r["scenario"], r["repeat"]) for r in episodes}):
        pair = {
            r["condition"]: r
            for r in episodes
            if r["scenario"] == scenario and r["repeat"] == repeat
        }
        if set(pair) == set(CONDITIONS):
            pairs.append(
                {
                    "scenario": scenario,
                    "repeat": repeat,
                    "success": {k: v["success"] for k, v in pair.items()},
                    "clarifications": {k: v["preference_clarifications"] for k, v in pair.items()},
                    "calls": {k: v["usage"]["calls"] for k, v in pair.items()},
                }
            )
    return {"groups": groups, "pairs": pairs}


def run(output, freeze_path, phase, settings, pilot=None):
    record = validate_freeze(freeze_path, settings)
    if phase == "formal":
        prior = read(Path(pilot) / "summary.json") if pilot else {}
        if prior.get("freeze_id") != record["freeze_id"] or not prior.get("pilot_gate_passed"):
            raise ValueError("Formal execution requires a passing pilot under this freeze.")
    output = output_directory(output)
    write(output / "freeze.json", record)
    write(output / "model.json", public_settings(settings))
    order = record["protocol"][f"{phase}_schedule"]
    episodes = []
    summary = {
        "phase": phase,
        "freeze_id": record["freeze_id"],
        "planned_executions": len(order),
        "completed": False,
        "started_executions": 0,
    }
    write(output / "summary.json", summary)
    write(
        output / "execution.json",
        {
            "started_at": datetime.now(UTC).isoformat(),
            "schedule": order,
            "source_implementation_sha256": record["protocol"]["implementation_sha256"],
        },
    )
    scenarios = {s["id"]: s for s in read(DATA)["scenarios"]}
    try:
        with synthetic_http() as (url, state):
            for index, item in enumerate(order):
                validate_freeze(freeze_path, settings)
                directory = (
                    output
                    / f"{index + 1:02d}-{item['scenario']}-{item['condition']}-r{item['repeat']}"
                )
                result = run_attempt(
                    directory,
                    scenarios[item["scenario"]],
                    item["condition"],
                    item["repeat"],
                    url,
                    state,
                    settings,
                )
                result["directory"] = directory.name
                episodes.append(result)
                write(output / "episodes.json", episodes)
                summary.update(summarize(episodes), started_executions=len(episodes))
                write(output / "summary.json", summary)
                print(
                    json.dumps(
                        {
                            k: result[k]
                            for k in (
                                "scenario",
                                "condition",
                                "repeat",
                                "success",
                                "preference_clarifications",
                                "error_type",
                            )
                        }
                    ),
                    flush=True,
                )
    finally:
        summary.update(
            summarize(episodes),
            started_executions=len(episodes),
            unstarted_executions=len(order) - len(episodes),
            completed=len(episodes) == len(order),
            finished_at=datetime.now(UTC).isoformat(),
        )
        summary["source_matches_finish"] = source_manifest() == record["protocol"]["source_files"]
        summary["pilot_gate_passed"] = (
            phase == "pilot" and pilot_gate(episodes) and summary["source_matches_finish"]
        )
        write(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".runtime/cpa.env")
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--phase", choices=("pilot", "formal"), default="pilot")
    parser.add_argument("--pilot", type=Path)
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)
    settings = ModelSettings.from_env()
    settings.require_live()
    if settings.name != "gpt-6-luna":
        raise ValueError("This protocol requires the recorded gpt-6-luna model.")
    if args.action == "freeze":
        result = freeze(args.output, settings)
        print(json.dumps({"freeze_id": result["freeze_id"]}))
    else:
        result = run(args.output, args.freeze, args.phase, settings, args.pilot)
        print(
            json.dumps(
                {
                    "completed": result["completed"],
                    "started_executions": result["started_executions"],
                    "pilot_gate_passed": result["pilot_gate_passed"],
                }
            )
        )


if __name__ == "__main__":
    main()
