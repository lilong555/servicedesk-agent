from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from servicedesk_agent.benchmark import (
    DeepAgentsCRMAdapter,
    load_fixed_tasks,
    run_episode,
    subset_manifest,
)
from servicedesk_agent.testing import ScriptedChatModel, tool_call

SOURCE = Path(__file__).resolve().parents[1] / "vendor/crm-agent-bench"
pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.skipif(not SOURCE.exists(), reason="Fetch the pinned CRM source first."),
]


class OneTurnUser:
    def reset(self, instruction):
        self.first = True

    async def step(self, response):
        from harness.user_simulator import STOP_SIGNAL

        if self.first:
            self.first = False
            return "Set TCK-1004 to In Progress."
        return STOP_SIGNAL


def test_frozen_report_split():
    dev = load_fixed_tasks(SOURCE, "development")
    holdout = load_fixed_tasks(SOURCE, "holdout")
    calibration = load_fixed_tasks(SOURCE, "calibration")
    all_tasks = load_fixed_tasks(SOURCE, "all")
    manifest = subset_manifest()
    assert len(dev) == 9 and len(holdout) == 12
    assert {task.id for task in dev}.isdisjoint({task.id for task in holdout})
    assert len(all_tasks) == len({task.id for task in all_tasks}) == 21
    assert {task.id for task in all_tasks} == {task.id for task in dev + holdout}
    assert [task.id for task in calibration] == manifest["calibration"]
    assert len(calibration) == len({task.id for task in calibration}) == 3
    assert {task.id for task in calibration} < {task.id for task in dev}
    assert {task.id for task in calibration}.isdisjoint({task.id for task in holdout})
    assert [task.id for task in dev] == manifest["development"]
    assert [task.id for task in holdout] == manifest["holdout"]
    assert [task.id for task in load_fixed_tasks(SOURCE, "calibration")] == [
        task.id for task in calibration
    ]
    assert all(not task.expected_response_semantic for task in dev + holdout)
    assert sum(task.expected_order_mode == "exact" for task in dev + holdout) == 3


@pytest.mark.parametrize("changed", ["src/tasks/itsm.yaml", "src/tasks/policies/itsm_policy.md"])
def test_frozen_task_and_policy_hashes_reject_tampering(tmp_path, changed):
    for relative in ("src/tasks/itsm.yaml", "src/tasks/policies/itsm_policy.md"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / relative, target)
    (tmp_path / changed).write_text("Modified source.", encoding="utf-8")
    with pytest.raises(ValueError, match="Frozen source hash mismatch"):
        load_fixed_tasks(tmp_path, "calibration")


@pytest.mark.parametrize(("status", "expected"), [("In Progress", True), ("Resolved", False)])
def test_native_loop_and_official_grader_positive_negative(tmp_path, status, expected):
    async def run():
        from harness.tools.domains.itsm import ITSMEnvironment

        task = next(
            item
            for item in load_fixed_tasks(SOURCE)
            if item.id == "itsm_ops_007_update_status_to_in_progress"
        )
        environment = ITSMEnvironment(initial_state=task.initial_state)
        model = ScriptedChatModel(
            responses=[
                tool_call("get_ticket", {"ticket_id": "TCK-1004"}, "get"),
                tool_call(
                    "update_ticket_status", {"ticket_id": "TCK-1004", "status": status}, "set"
                ),
                "Status updated.",
            ]
        )
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
            adapter = DeepAgentsCRMAdapter(
                model, environment, "Follow the ITSM policy.", checkpointer=saver, thread_id="task"
            )
            result = await run_episode(task, adapter, OneTurnUser())
        assert result["success"] is expected
        assert result["trace"]["state_passed"] is expected
        assert len(result["trace"]["tool_calls_detail"]) == 2
        assert all(item["received_result"] for item in adapter.events)

    asyncio.run(run())


def test_forbidden_attempt_is_visible_even_when_tool_invocation_fails(tmp_path):
    async def run():
        from harness.bench_runner import Task
        from harness.tools.domains.itsm import ITSMEnvironment

        task = Task(
            id="adapter-forbidden-attempt",
            instruction="Only assess TCK-1001.",
            forbidden_actions=["delete_ticket"],
        )
        model = ScriptedChatModel(
            responses=[
                tool_call("delete_ticket", {"wrong_parameter": "TCK-1001"}, "delete"),
                "The deletion failed.",
            ]
        )
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
            adapter = DeepAgentsCRMAdapter(
                model, ITSMEnvironment(), "Assessment only.", checkpointer=saver, thread_id="task"
            )
            result = await run_episode(task, adapter, OneTurnUser())
        assert result["success"] is False
        assert result["trace"]["forbidden_passed"] is False
        assert result["trace"]["tool_calls_detail"][0]["name"] == "delete_ticket"
        assert result["trace"]["tool_calls_detail"][0]["ok"] is False

    asyncio.run(run())


def test_all_public_workflows_include_applicability_and_original_instructions():
    import yaml

    from servicedesk_agent.benchmark import load_business_policies

    documents = load_business_policies(SOURCE)
    reference = yaml.safe_load((SOURCE / "src/agents/itsm.yaml").read_text())
    assert documents[0]["text"] == (SOURCE / "src/tasks/policies/itsm_policy.md").read_text()
    assert len(documents) == 4
    for document, node_id, line in zip(
        documents[1:],
        ("ticket_operations__tickets", "record_safety__deletion", "case_hours__cases"),
        (14, 74, 121),
        strict=True,
    ):
        node = reference["nodes"][node_id]
        assert document["text"] == (
            f"Applicable requests: {node['description']}\n\n{node['instructions']}"
        )
        assert document["source_uri"].endswith(f"/src/agents/itsm.yaml#L{line}")
    assert "1. Use check_dependencies" in documents[2]["text"]
    assert "2. Use check_permission" in documents[2]["text"]
