"""Offline recovery priority checks using native graphs and SQLite checkpoints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import interrupt

from servicedesk_agent.agent import build_desk_agent
from servicedesk_agent.components import ComponentBundle
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session, load_session, session_storage
from servicedesk_agent.testing import ScriptedChatModel, tool_call


def assert_tool_pairs(messages):
    pending = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
        else:
            assert not pending
            if isinstance(message, AIMessage):
                pending.update(call["id"] for call in message.tool_calls)
    assert not pending


def checkpoint_before_tools(root, client, contract, calls):
    config = {"configurable": {"thread_id": contract.thread_id}}
    with session_storage(root, contract.thread_id) as (saver, directory):
        graph = build_desk_agent(
            ScriptedChatModel(responses=[AIMessage(content="", tool_calls=calls)]),
            client,
            contract,
            checkpointer=saver,
        )
        graph.invoke(
            {
                "messages": [HumanMessage(content="Investigate and append a note.")],
                "contract": contract.model_dump(mode="json"),
                "pending_actions": ["Obtain gateway logs."],
                "handoff_summary": "Keep the ticket open.",
                "receipts": {},
                "observations": {},
            },
            config,
            interrupt_before=["tools"],
        )
        state = graph.get_state(config)
        assert state.next == ("tools",) * len(calls)
        assert (directory / "checkpoint.sqlite").is_file()
        return dict(state.values)


@pytest.mark.parametrize(
    "message",
    [
        "Stop. Do not write any notes.",
        "\u6682\u505c\u5199\u5165\uff0c\u4ec5\u6c47\u62a5\u5f53\u524d\u72b6\u6001\u3002",
        "The owner has changed. Review the handoff before deciding what to do next.",
    ],
)
def test_new_input_precedes_unexecuted_write_after_sqlite_rebuild(
    tmp_path, desk, contract, message
):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    saved = checkpoint_before_tools(
        tmp_path,
        client,
        contract,
        tool_call(
            "add_ticket_note", {"note": "Investigated.", "expected_version": version}, "old-note"
        ).tool_calls,
    )
    assert backend.writes == []
    model = ScriptedChatModel(responses=["Reviewed the latest request."])
    result = execute_session(tmp_path, client, contract, model, message, resume=True)
    assert backend.writes == []
    assert len(model.requests) == 1
    request = model.requests[0]
    assert isinstance(request[-1], HumanMessage) and request[-1].text == message
    assert_tool_pairs(request)
    cancelled = next(item for item in request if isinstance(item, ToolMessage))
    assert cancelled.tool_call_id == "old-note" and cancelled.status == "error"
    assert json.loads(cancelled.text)["error"] == "superseded_by_user_input"
    assert json.loads(cancelled.text)["execution_outcome"] == "unknown"
    restored = load_session(tmp_path, client, contract)
    for field in ("contract", "pending_actions", "handoff_summary", "receipts"):
        assert restored[field] == saved[field]
    assert result["verified_receipts"] == []


def test_failed_tool_is_cancelled_before_resume_input_reaches_model(
    tmp_path, desk, contract, monkeypatch
):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version

    def crash_before_write(*args, **kwargs):
        raise RuntimeError("Synthetic process failure before PUT")

    with monkeypatch.context() as patch:
        patch.setattr(client, "add_note", crash_before_write)
        with pytest.raises(RuntimeError, match="Synthetic process failure"):
            execute_session(
                tmp_path,
                client,
                contract,
                ScriptedChatModel(
                    responses=[
                        tool_call(
                            "add_ticket_note",
                            {"note": "Investigated.", "expected_version": version},
                            "pending-note",
                        )
                    ]
                ),
                "Investigate and append a note.",
            )
    assert backend.writes == []
    model = ScriptedChatModel(responses=["Stopped."])
    execute_session(tmp_path, client, contract, model, "Stop. Do not write any notes.", resume=True)
    assert backend.writes == []
    assert len(model.requests) == 1
    assert model.requests[0][-1].text == "Stop. Do not write any notes."
    assert_tool_pairs(model.requests[0])


def test_read_write_batch_is_replanned_after_new_input(tmp_path, desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    calls = [
        *tool_call("read_ticket", {}, "old-read").tool_calls,
        *tool_call(
            "add_ticket_note", {"note": "Old note.", "expected_version": version}, "old-note"
        ).tool_calls,
        *tool_call(
            "change_ticket_status", {"status_id": 2, "expected_version": version}, "old-status"
        ).tool_calls,
        *tool_call("read_handoff", {}, "old-handoff").tool_calls,
    ]
    checkpoint_before_tools(tmp_path, client, contract, calls)
    ticket_reads = sum(request.url.path == "/issues/42.json" for request in backend.requests)
    message = "Leave the status unchanged. Read current evidence and add the revised note."

    def replan(messages):
        assert messages[-1].text == message
        assert backend.writes == []
        assert (
            sum(request.url.path == "/issues/42.json" for request in backend.requests)
            == ticket_reads
        )
        results = [item for item in messages if isinstance(item, ToolMessage)]
        assert {item.tool_call_id for item in results} == {call["id"] for call in calls}
        assert all(json.loads(item.text)["retry_cancelled"] for item in results)
        assert_tool_pairs(messages)
        return tool_call("read_ticket", {}, "fresh-read")

    def revised_note(messages):
        assert_tool_pairs(messages)
        ticket = json.loads(messages[-1].text)["ticket"]
        return tool_call(
            "add_ticket_note",
            {"note": "Revised note.", "expected_version": ticket["evidence"]["source_version"]},
            "fresh-note",
        )

    model = ScriptedChatModel(responses=[replan, revised_note, "Revised note verified."])
    result = execute_session(tmp_path, client, contract, model, message, resume=True)
    assert len(backend.writes) == len(result["verified_receipts"]) == 1
    assert backend.writes[0]["notes"].startswith("Revised note.")
    assert backend.issue["status"]["id"] == 1
    assert_tool_pairs(load_session(tmp_path, client, contract)["messages"])


def test_completed_parallel_outputs_survive_cancellation_of_interrupted_sibling(
    tmp_path, desk, contract
):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    pause_calls = []

    @tool
    def await_owner() -> str:
        """Ask the ticket owner for clarification."""
        pause_calls.append(True)
        return interrupt("Owner clarification required.")

    components = ComponentBundle(tools=[await_owner])
    calls = [
        *tool_call("read_ticket", {}, "completed-read").tool_calls,
        *tool_call(
            "add_ticket_note",
            {"note": "Completed sibling.", "expected_version": version},
            "completed-note",
        ).tool_calls,
        *tool_call(
            "save_handoff",
            {"pending_actions": ["Obtain gateway logs."], "summary": "Awaiting owner."},
            "completed-handoff",
        ).tool_calls,
        *tool_call("await_owner", {}, "interrupted-owner").tool_calls,
    ]
    execute_session(
        tmp_path,
        client,
        contract,
        ScriptedChatModel(responses=[AIMessage(content="", tool_calls=calls)]),
        "Investigate and append a note.",
        components=components,
    )
    config = {"configurable": {"thread_id": contract.thread_id}}
    with session_storage(tmp_path, contract.thread_id) as (saver, _):
        graph = build_desk_agent(
            ScriptedChatModel(), client, contract, checkpointer=saver, components=components
        )
        saved = graph.get_state(config)
        assert saved.next == ("tools",)
        assert len(saved.tasks) == 4
        assert sum(task.result is not None for task in saved.tasks) == 3
        assert saved.interrupts
        assert len(saved.values["receipts"]) == 1
    assert len(backend.writes) == 1
    model = ScriptedChatModel(responses=["Stopped; the completed note remains recorded."])
    result = execute_session(
        tmp_path,
        client,
        contract,
        model,
        "Stop all outstanding actions.",
        resume=True,
        components=components,
    )
    assert len(pause_calls) == 1
    assert len(backend.writes) == len(result["verified_receipts"]) == 1
    assert model.requests[0][-1].text == "Stop all outstanding actions."
    restored = load_session(tmp_path, client, contract)
    assert_tool_pairs(restored["messages"])
    for field in ("contract", "receipts", "observations", "pending_actions", "handoff_summary"):
        assert restored[field] == saved.values[field]
    results = {
        item.tool_call_id: item for item in model.requests[0] if isinstance(item, ToolMessage)
    }
    assert all(
        json.loads(results[call_id].text)["ok"]
        for call_id in ("completed-read", "completed-note", "completed-handoff")
    )
    assert json.loads(results["interrupted-owner"].text)["retry_cancelled"]


@pytest.mark.parametrize("committed", [False, True])
def test_pending_write_in_fresh_process_is_only_read_back(
    tmp_path, desk, contract, monkeypatch, committed
):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    calls = tool_call(
        "add_ticket_note", {"note": "Investigated.", "expected_version": version}, "pending-note"
    ).tool_calls
    if committed:
        add_note = client.add_note

        def crash_after_put(*args, **kwargs):
            add_note(*args, **kwargs)
            raise RuntimeError("Process lost before checkpointing the receipt")

        with monkeypatch.context() as patch:
            patch.setattr(client, "add_note", crash_after_put)
            with pytest.raises(RuntimeError, match="Process lost"):
                execute_session(
                    tmp_path,
                    client,
                    contract,
                    ScriptedChatModel(responses=[AIMessage(content="", tool_calls=calls)]),
                    "Investigate and append a note.",
                )
    else:
        checkpoint_before_tools(tmp_path, client, contract, calls)
    assert len(backend.writes) == int(committed)
    code = """
import json, os, sys
from pathlib import Path
import httpx
sys.path.insert(0, os.environ["TEST_SUPPORT"])
from conftest import RedmineFixture
from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session, load_session
from servicedesk_agent.testing import ScriptedChatModel, tool_call

backend = RedmineFixture()
backend.issue = json.loads(os.environ["TEST_ISSUE"])
contract = TaskContract.model_validate_json(os.environ["TEST_CONTRACT"])
root = Path(os.environ["TEST_ROOT"])
message = "Stop writing. Read current evidence and report unresolved outcomes."

def read_after_new_input(messages):
    assert messages[-1].type == "human" and messages[-1].text == message
    assert backend.writes == []
    result = next(m for m in messages if m.type == "tool" and m.tool_call_id == "pending-note")
    assert json.loads(result.text)["execution_outcome"] == "unknown"
    return tool_call("read_ticket", {}, "reconcile-read")

with RedmineClient(
    RedmineSettings(url="https://desk.test", api_key="test-key"),
    transport=httpx.MockTransport(backend.handle),
) as client:
    model = ScriptedChatModel(responses=[read_after_new_input, "Read-back evidence available."])
    result = execute_session(root, client, contract, model, message, resume=True)
    state = load_session(root, client, contract)
    assert backend.writes == []
    assert len(model.requests) == 2
    assert result["verified_receipts"] == []
    tool_result = json.loads(model.requests[1][-1].text)
    assert len(tool_result["ticket"]["journals"]) == int(os.environ["TEST_COMMITTED"])
    print(json.dumps({"restored": True, "writes": len(backend.writes)}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "TEST_SUPPORT": str(Path(__file__).parent),
            "TEST_CONTRACT": contract.model_dump_json(),
            "TEST_ROOT": str(tmp_path),
            "TEST_ISSUE": json.dumps(backend.issue),
            "TEST_COMMITTED": str(int(committed)),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(completed.stdout) == {"restored": True, "writes": 0}
    restored = load_session(tmp_path, client, contract)
    assert_tool_pairs(restored["messages"])
    assert restored["contract"] == contract.model_dump(mode="json")
    assert restored["receipts"] == {}


def test_new_input_and_cancellation_survive_another_model_failure(tmp_path, desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    checkpoint_before_tools(
        tmp_path,
        client,
        contract,
        tool_call(
            "add_ticket_note", {"note": "Old note.", "expected_version": version}, "old-note"
        ).tool_calls,
    )
    with pytest.raises(ValueError, match="exhausted"):
        execute_session(
            tmp_path, client, contract, ScriptedChatModel(), "Stop writing.", resume=True
        )
    model = ScriptedChatModel(responses=["Only reporting current state."])
    execute_session(tmp_path, client, contract, model, "Report current state.", resume=True)
    assert len(model.requests) == 1
    assert backend.writes == []
    messages = model.requests[0]
    assert [item.text for item in messages if isinstance(item, HumanMessage)][-2:] == [
        "Stop writing.",
        "Report current state.",
    ]
    assert len([item for item in messages if isinstance(item, ToolMessage)]) == 1
    assert_tool_pairs(messages)


def test_replanned_write_respects_persistent_unresolved_intent(tmp_path, desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.apply_writes = False
    backend.lose_ack = True
    with (
        redmine_store(tmp_path) as store,
        RedmineClient(
            client.settings, store=store, transport=httpx.MockTransport(backend.handle)
        ) as first_client,
        pytest.raises(ValueError, match="exhausted"),
    ):
        execute_session(
            tmp_path,
            first_client,
            contract,
            ScriptedChatModel(
                responses=[
                    tool_call(
                        "add_ticket_note",
                        {"note": "Original note.", "expected_version": version},
                        "original-call",
                    )
                ]
            ),
            "Investigate and append a note.",
        )
    assert len(backend.writes) == 1
    backend.apply_writes = True
    backend.lose_ack = False

    def replan(messages):
        assert messages[-1].text == "Review the uncertain result before proceeding."
        assert len(backend.writes) == 1
        return tool_call(
            "add_ticket_note",
            {"note": "Reworded note.", "expected_version": version},
            "different-call-id",
        )

    model = ScriptedChatModel(responses=[replan, "The prior operation remains unresolved."])
    with (
        redmine_store(tmp_path) as store,
        RedmineClient(
            client.settings, store=store, transport=httpx.MockTransport(backend.handle)
        ) as second_client,
    ):
        result = execute_session(
            tmp_path,
            second_client,
            contract,
            model,
            "Review the uncertain result before proceeding.",
            resume=True,
        )
    assert len(backend.writes) == 1
    assert result["verified_receipts"] == []
    replies = {
        item.tool_call_id: json.loads(item.text)
        for item in model.requests[-1]
        if isinstance(item, ToolMessage)
    }
    assert replies["different-call-id"]["error"] == "operation_uncertain"
    assert_tool_pairs(model.requests[-1])


def test_finished_task_outputs_survive_a_lost_superstep_checkpoint(
    tmp_path, desk, contract, monkeypatch
):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    config = {"configurable": {"thread_id": contract.thread_id}}
    with session_storage(tmp_path, contract.thread_id) as (saver, _):
        put = saver.put

        def fail_completed_checkpoint(config, checkpoint, metadata, new_versions):
            if checkpoint["channel_values"].get("receipts"):
                raise RuntimeError("Lost superstep checkpoint")
            return put(config, checkpoint, metadata, new_versions)

        graph = build_desk_agent(
            ScriptedChatModel(
                responses=[
                    tool_call(
                        "add_ticket_note",
                        {"note": "Completed write.", "expected_version": version},
                        "completed-note",
                    )
                ]
            ),
            client,
            contract,
            checkpointer=saver,
        )
        with monkeypatch.context() as patch:
            patch.setattr(saver, "put", fail_completed_checkpoint)
            with pytest.raises(RuntimeError, match="Lost superstep checkpoint"):
                graph.invoke(
                    {
                        "messages": [HumanMessage(content="Investigate and append a note.")],
                        "contract": contract.model_dump(mode="json"),
                        "receipts": {},
                        "observations": {},
                    },
                    config,
                    durability="sync",
                )
        saved = graph.get_state(config)
        assert saved.next == ()
        assert len(saved.tasks) == 1 and saved.tasks[0].result is not None
        assert len(saved.values["receipts"]) == 1
    model = ScriptedChatModel(responses=["The completed note is retained."])
    result = execute_session(tmp_path, client, contract, model, "Stop further writes.", resume=True)
    assert len(backend.writes) == len(result["verified_receipts"]) == 1
    assert len(model.requests) == 1
    assert model.requests[0][-1].text == "Stop further writes."
    restored = load_session(tmp_path, client, contract)
    assert restored["receipts"] == saved.values["receipts"]
    assert restored["observations"] == saved.values["observations"]
    assert_tool_pairs(restored["messages"])
    replies = [item for item in restored["messages"] if isinstance(item, ToolMessage)]
    assert len(replies) == 1 and json.loads(replies[0].text)["ok"]
