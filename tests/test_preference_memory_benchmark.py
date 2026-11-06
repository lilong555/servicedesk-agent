from __future__ import annotations

import importlib.util
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from servicedesk_agent.components import memory_namespace, remember_confirmed_preference
from servicedesk_agent.config import ModelSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.testing import ScriptedChatModel, tool_call


@pytest.fixture(scope="module")
def benchmark():
    path = Path(__file__).parents[1] / "scripts/benchmark_preference_memory.py"
    spec = importlib.util.spec_from_file_location("preference_memory_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_schedule_balances_order_and_has_24_executions(benchmark):
    rows = benchmark.schedule("formal")
    assert len(rows) == 24
    assert len({(r["scenario"], r["repeat"], r["condition"]) for r in rows}) == 24
    assert Counter(r["condition"] for r in rows) == {"available": 12, "empty": 12}
    for scenario in {r["scenario"] for r in rows}:
        first = [r["condition"] for r in rows if r["scenario"] == scenario and r["repeat"] == 1]
        second = [r["condition"] for r in rows if r["scenario"] == scenario and r["repeat"] == 2]
        assert first == second[::-1]
    assert len(benchmark.schedule("pilot")) == 4


def test_scenario_partition_and_initial_information_separation(benchmark):
    data = benchmark.read(benchmark.DATA)
    assert Counter(s["kind"] for s in data["scenarios"]) == {
        "history_required": 4,
        "current_override": 2,
    }
    for scenario in data["scenarios"]:
        prompt = data["shared_request"] + scenario["request"]
        assert all(marker not in prompt for marker in scenario["private_markers"])
        assert all(
            marker in scenario["confirmed_preference"] for marker in scenario["private_markers"]
        )


def test_conditions_share_native_schema_skills_and_policy(tmp_path, benchmark):
    with redmine_store(tmp_path) as store:
        bundles = [
            benchmark.component_bundle("same-scope", store, c, []) for c in benchmark.CONDITIONS
        ]
        schemas = [[convert_to_openai_tool(t) for t in b.tools] for b in bundles]
        assert schemas[0] == schemas[1]
        assert bundles[0].initial_state == bundles[1].initial_state
        assert bundles[0].agent_kwargs["skills"] == bundles[1].agent_kwargs["skills"]
        memory = next(t for t in bundles[0].tools if t.name == "recall_confirmed_preferences")
        assert "Use filter=null" in memory.description


def test_production_memory_reopens_and_isolates_ticket_and_principal(tmp_path):
    contract = TaskContract(
        service_url="http://127.0.0.1:19442",
        project_id=1,
        principal_id=1,
        ticket_id=42,
        session_id="confirmation",
    )
    with redmine_store(tmp_path) as store:
        remember_confirmed_preference(contract.scope, "Use reference P-42.", store)
    with redmine_store(tmp_path) as store:
        resumed = contract.model_copy(update={"session_id": "new-conversation"})
        assert resumed.thread_id != contract.thread_id
        assert len(store.search(memory_namespace(resumed.scope))) == 1
        for change in ({"ticket_id": 43}, {"principal_id": 2}):
            other = contract.model_copy(update=change)
            assert store.search(memory_namespace(other.scope)) == []


@pytest.mark.parametrize(
    "response",
    [
        "Which note-reference prefix should I use?",
        "Please provide your preferred note format.",
        "I don't have your confirmed reference convention.",
    ],
)
def test_preference_questions_get_fixed_answer(benchmark, response):
    scenario = benchmark.read(benchmark.DATA)["scenarios"][0]
    kind, answer = benchmark.clarification(scenario, response)
    assert kind == "preference_clarification"
    assert answer == scenario["clarification"]


def test_unrelated_followup_does_not_disclose_preference(benchmark):
    scenario = benchmark.read(benchmark.DATA)["scenarios"][0]
    kind, answer = benchmark.clarification(scenario, "The ticket was read.")
    assert kind == "task_followup"
    assert "OPS-27" not in answer


def note_action(expected):
    def respond(messages):
        payloads = []
        for message in messages:
            if message.type == "tool":
                try:
                    payloads.append(json.loads(message.content))
                except (ValueError, TypeError):
                    pass
        ticket = next(
            p["ticket"] for p in reversed(payloads) if isinstance(p, dict) and "ticket" in p
        )
        return tool_call(
            "add_ticket_note",
            {
                "note": expected,
                "expected_version": ticket["evidence"]["source_version"],
            },
            "write-note",
        )

    return respond


@pytest.mark.parametrize("condition", ["available", "empty"])
def test_production_session_real_langmem_and_http_writes(tmp_path, benchmark, condition):
    scenario = benchmark.read(benchmark.DATA)["scenarios"][0]
    responses = [tool_call("recall_confirmed_preferences", {"query": "note reference"}, "recall")]
    if condition == "empty":
        responses.append("Which note-reference prefix should I use?")
    responses.extend(
        [
            tool_call("read_ticket", {}, "read"),
            note_action(scenario["expected_note"]),
            "The note has a verified receipt.",
        ]
    )
    model = ScriptedChatModel(responses=responses)
    with benchmark.synthetic_http() as (url, state):
        result = benchmark.run_attempt(
            tmp_path / condition,
            scenario,
            condition,
            1,
            url,
            state,
            ModelSettings(name="gpt-6-luna"),
            model=model,
        )
    assert result["success"] is True
    assert result["preference_clarifications"] == (condition == "empty")
    assert result["memory_searches"] == 1
    assert result["nonempty_recalls"] == (condition == "available")
    assert result["first_request_preference_leaks"] == []
    assert result["new_thread"] is True
    assert result["usage"]["calls"] == (5 if condition == "empty" else 4)
    assert result["usage"]["unknown_usage_calls"] == result["usage"]["calls"]
    calls = benchmark.read(tmp_path / condition / "model_calls.json")
    assert len(calls) == len({c["run_id"] for c in calls})
    http = benchmark.read(tmp_path / condition / "http.json")
    assert sum(r["method"] == "PUT" for r in http) == 1
    searches = benchmark.read(tmp_path / condition / "memory_searches.json")
    assert len(searches[0]["stored_result"]) == 1
    assert bool(searches[0]["returned_result"]) == (condition == "available")


def test_failure_preserves_unknown_usage_and_empty_business_state(tmp_path, benchmark):
    scenario = benchmark.read(benchmark.DATA)["scenarios"][0]
    with benchmark.synthetic_http() as (url, state):
        result = benchmark.run_attempt(
            tmp_path / "failed",
            scenario,
            "empty",
            1,
            url,
            state,
            ModelSettings(name="gpt-6-luna"),
            model=ScriptedChatModel(responses=[]),
        )
    assert result["success"] is None
    assert result["error_type"] == "ValueError"
    assert result["usage"]["calls"] == 1
    assert result["usage"]["failed_calls"] == 1
    assert result["usage"]["unknown_usage_calls"] == 1
    assert benchmark.read(tmp_path / "failed" / "writes.json") == []
    assert list((tmp_path / "failed" / "state/sessions").glob("*/trace-*.json"))


def test_wrong_preference_and_collateral_changes_fail_independently(benchmark):
    scenario = benchmark.read(benchmark.DATA)["scenarios"][4]
    before = {"status": {"id": 1}, "journals": []}
    after = deepcopy(before)
    after["journals"] = [{"id": 1, "notes": scenario["expected_note"]}]
    receipts = [{"verified": True, "journal_id": 1}]
    assert benchmark.grade(
        scenario, before, after, [{"notes": scenario["expected_note"]}], receipts
    )["success"]
    wrong = benchmark.grade(
        scenario, before, after, [{"notes": "[OPS-27] | Rollback approved."}], receipts
    )
    assert wrong["checks"]["note_matches"] is False
    after["status"]["id"] = 5
    assert (
        benchmark.grade(scenario, before, after, [{"notes": scenario["expected_note"]}], receipts)[
            "checks"
        ]["no_collateral_change"]
        is False
    )


def test_summary_keeps_infrastructure_failures_in_started_denominator(benchmark):
    rows = []
    for condition, success in (("available", True), ("empty", None)):
        rows.append(
            {
                "scenario": "note_reference",
                "kind": "history_required",
                "condition": condition,
                "repeat": 1,
                "success": success,
                "preference_clarifications": 0,
                "memory_searches": 0,
                "usage": {
                    key: 0
                    for key in (
                        "calls",
                        "failed_calls",
                        "unknown_usage_calls",
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                    )
                },
            }
        )
    summary = benchmark.summarize(rows)
    assert summary["groups"]["empty"]["started"] == 1
    assert summary["groups"]["empty"]["scored"] == 0
    assert summary["groups"]["empty"]["infrastructure_errors"] == 1
    assert summary["pairs"][0]["success"]["empty"] is None


def test_pilot_gate_keeps_scored_business_failure_eligible(benchmark):
    rows = [
        {
            "condition": c,
            "success": success,
            "first_request_preference_leaks": [],
            "usage": {"unknown_usage_calls": 0, "failed_calls": 0},
            "nonempty_recalls": int(c == "available"),
            "memory_searches": 1,
        }
        for c, success in (
            ("available", True),
            ("empty", True),
            ("empty", False),
            ("available", True),
        )
    ]
    assert benchmark.pilot_gate(rows)
    rows[2]["success"] = None
    assert not benchmark.pilot_gate(rows)
    rows[2]["success"] = False
    rows[0]["usage"]["unknown_usage_calls"] = 1
    assert not benchmark.pilot_gate(rows)
