"""Session-scoped revocation using native checkpoints and business adapters."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from deepagents.middleware.summarization import SUMMARIZATION_EVENT_KEY
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import interrupt
from test_lifecycle import compacted_session  # noqa: F401
from test_resume_priority import assert_tool_pairs, checkpoint_before_tools

from servicedesk_agent.agent import build_desk_agent
from servicedesk_agent.components import ComponentBundle, build_components
from servicedesk_agent.domain import BusinessError, TaskContract
from servicedesk_agent.knowledge import PolicyCatalog
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import (
    _AtomicSqliteSaver,
    execute_session,
    load_session,
    revoke_session_writes,
    session_storage,
)
from servicedesk_agent.testing import ScriptedChatModel, tool_call


def snapshot(root, client, identity):
    with session_storage(root, identity.thread_id) as (saver, _):
        graph = build_desk_agent(ScriptedChatModel(), client, identity, checkpointer=saver)
        return graph.get_state({"configurable": {"thread_id": identity.thread_id}})


def assert_readonly(value, original):
    expected = original.model_dump(mode="json")
    expected.update(allow_notes=False, allowed_status_ids=[], allow_close=False)
    assert value == expected


@pytest.mark.parametrize("initial_readonly", [False, True])
def test_revoke_is_idempotent_and_preserves_saved_contract_fields(
    tmp_path, desk, contract, initial_readonly, monkeypatch
):
    backend, client = desk
    saved_contract = contract.model_copy(
        update={
            "variant": "B",
            "allow_notes": not initial_readonly,
            "allowed_status_ids": () if initial_readonly else (2, 5),
            "allow_close": not initial_readonly,
        }
    )
    execute_session(
        tmp_path, client, saved_contract, ScriptedChatModel(responses=["Saved."]), "Investigate."
    )
    before = snapshot(tmp_path, client, contract)

    def no_model(*args, **kwargs):
        pytest.fail("Revocation invoked a model")

    monkeypatch.setattr(ScriptedChatModel, "_generate", no_model)
    result = revoke_session_writes(tmp_path, client, contract)
    after = snapshot(tmp_path, client, contract)
    assert_readonly(result["contract"], saved_contract)
    assert result["thread_id"] == saved_contract.thread_id
    assert after.values["messages"] == before.values["messages"]
    assert after.next == () and after.tasks == ()
    assert revoke_session_writes(tmp_path, client, saved_contract) == result
    repeated = snapshot(tmp_path, client, contract)
    assert repeated.config == after.config
    if initial_readonly:
        assert after.config == before.config
    assert backend.writes == []


def test_missing_session(tmp_path, desk, contract):
    backend, client = desk
    with pytest.raises(BusinessError) as raised:
        revoke_session_writes(tmp_path, client, contract)
    assert raised.value.code == "session_missing"
    assert load_session(tmp_path, client, contract) == {}
    assert backend.writes == []


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"principal_id": 9}, "identity_changed"),
        ({"service_url": "https://other.test"}, "scope_mismatch"),
        ({"project_id": 9}, "scope_mismatch"),
    ],
)
def test_identity_validation_precedes_changes(tmp_path, desk, contract, change, error):
    backend, client = desk
    execute_session(
        tmp_path, client, contract, ScriptedChatModel(responses=["Saved."]), "Investigate."
    )
    before = snapshot(tmp_path, client, contract)
    with pytest.raises(BusinessError) as raised:
        revoke_session_writes(tmp_path, client, contract.model_copy(update=change))
    assert raised.value.code == error
    assert snapshot(tmp_path, client, contract).config == before.config
    assert backend.writes == []


def test_mismatched_persisted_scope_is_rejected(tmp_path, desk, contract):
    backend, client = desk
    with session_storage(tmp_path, contract.thread_id) as (saver, _):
        graph = build_desk_agent(ScriptedChatModel(), client, contract, checkpointer=saver)
        graph.update_state(
            {"configurable": {"thread_id": contract.thread_id}},
            {"contract": contract.model_copy(update={"ticket_id": 43}).model_dump(mode="json")},
        )
    before = snapshot(tmp_path, client, contract)
    with pytest.raises(BusinessError) as raised:
        revoke_session_writes(tmp_path, client, contract)
    assert raised.value.code == "contract_changed"
    assert snapshot(tmp_path, client, contract).config == before.config
    assert backend.writes == []


def test_session_lock_covers_revocation(tmp_path, desk, contract):
    backend, client = desk
    with session_storage(tmp_path, contract.thread_id):
        with pytest.raises(BusinessError) as raised:
            revoke_session_writes(tmp_path, client, contract)
    assert raised.value.code == "session_busy"
    assert backend.requests == [] and backend.writes == []


def test_pending_tools_cancelled_and_replanned_writes_rejected(tmp_path, desk, contract):
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
    ]
    saved = checkpoint_before_tools(tmp_path, client, contract, calls)
    result = revoke_session_writes(tmp_path, client, contract)
    restored = snapshot(tmp_path, client, contract)
    assert restored.next == () and restored.tasks == ()
    assert_readonly(restored.values["contract"], contract)
    for field in ("pending_actions", "handoff_summary", "receipts", "observations"):
        assert restored.values[field] == saved[field]
    assert_tool_pairs(restored.values["messages"])
    cancelled = [m for m in restored.values["messages"] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in cancelled} == {call["id"] for call in calls}
    for message in cancelled:
        payload = json.loads(message.text)
        assert message.status == "error"
        assert payload["error"] == "session_writes_revoked"
        assert payload["retry_cancelled"] is True
        assert payload["execution_outcome"] == "unknown"
        assert "revoked" in payload["message"] and "read-only" in payload["message"]
    assert backend.writes == []
    stale_model = ScriptedChatModel(responses=["Should stay unused."])
    with pytest.raises(BusinessError) as raised:
        execute_session(tmp_path, client, contract, stale_model, "Continue.", resume=True)
    assert raised.value.code == "contract_changed"
    assert stale_model.requests == []
    readonly = TaskContract.model_validate(result["contract"])
    model = ScriptedChatModel(
        responses=[
            tool_call("read_ticket", {}, "fresh-read"),
            tool_call(
                "add_ticket_note",
                {"note": "Still write.", "expected_version": version},
                "denied-note",
            ),
            tool_call(
                "change_ticket_status",
                {"status_id": 5, "expected_version": version},
                "denied-status",
            ),
            tool_call("reconcile_ticket_operations", {}, "read-only-reconcile"),
            tool_call("read_handoff", {}, "fresh-handoff"),
            "Read-only investigation retained.",
        ]
    )
    execute_session(tmp_path, client, readonly, model, "Try writing, then read.", resume=True)
    replies = {
        m.tool_call_id: json.loads(m.text) for m in model.requests[-1] if isinstance(m, ToolMessage)
    }
    for call_id in ("denied-note", "denied-status"):
        assert replies[call_id]["error"] == "write_not_authorized"
    assert replies["fresh-read"]["ok"] and replies["read-only-reconcile"]["ok"]
    assert replies["fresh-handoff"]["contract"] == result["contract"]
    system = next(m.text for m in model.requests[0] if m.type == "system")
    assert readonly.model_dump_json() in system
    assert contract.model_dump_json() not in system
    assert_tool_pairs(model.requests[-1])
    assert load_session(tmp_path, client, readonly)["contract"] == result["contract"]
    assert backend.writes == []


def test_completed_siblings_survive_revocation_without_rerunning(tmp_path, desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    pauses = []

    @tool
    def await_owner() -> str:
        """Await clarification from the ticket owner."""
        pauses.append(True)
        return interrupt("Await owner.")

    calls = [
        *tool_call("read_ticket", {}, "completed-read").tool_calls,
        *tool_call(
            "add_ticket_note",
            {"note": "Completed note.", "expected_version": version},
            "completed-note",
        ).tool_calls,
        *tool_call(
            "save_handoff",
            {"pending_actions": ["Obtain logs."], "summary": "Await owner."},
            "completed-handoff",
        ).tool_calls,
        *tool_call("await_owner", {}, "pending-owner").tool_calls,
    ]
    execute_session(
        tmp_path,
        client,
        contract,
        ScriptedChatModel(responses=[AIMessage(content="", tool_calls=calls)]),
        "Investigate.",
        components=ComponentBundle(tools=[await_owner]),
    )
    before = snapshot(tmp_path, client, contract)
    assert len(before.tasks) == 4
    assert sum(task.result is not None for task in before.tasks) == 3
    result = revoke_session_writes(tmp_path, client, contract)
    restored = snapshot(tmp_path, client, contract)
    assert restored.next == () and restored.tasks == ()
    assert len(result["verified_receipts"]) == len(backend.writes) == 1
    assert pauses == [True]
    for field in ("receipts", "observations", "pending_actions", "handoff_summary"):
        assert restored.values[field] == before.values[field]
    assert_tool_pairs(restored.values["messages"])
    replies = {
        m.tool_call_id: json.loads(m.text)
        for m in restored.values["messages"]
        if isinstance(m, ToolMessage)
    }
    for call_id in ("completed-read", "completed-note", "completed-handoff"):
        assert replies[call_id]["ok"]
    assert replies["pending-owner"]["error"] == "session_writes_revoked"


def test_completed_write_survives_lost_superstep_checkpoint(tmp_path, desk, contract, monkeypatch):
    backend, client = desk
    checkpoint_before_tools(
        tmp_path,
        client,
        contract,
        tool_call(
            "add_ticket_note",
            {
                "note": "Completed.",
                "expected_version": client.get_ticket(42).evidence.source_version,
            },
            "completed-note",
        ).tool_calls,
    )
    config = {"configurable": {"thread_id": contract.thread_id}}
    with session_storage(tmp_path, contract.thread_id) as (saver, _):
        original_put = saver.put

        def fail_completed_checkpoint(config, checkpoint, metadata, new_versions):
            if checkpoint["channel_values"].get("receipts"):
                raise RuntimeError("Lost superstep checkpoint")
            return original_put(config, checkpoint, metadata, new_versions)

        graph = build_desk_agent(ScriptedChatModel(), client, contract, checkpointer=saver)
        with monkeypatch.context() as patch:
            patch.setattr(saver, "put", fail_completed_checkpoint)
            with pytest.raises(RuntimeError, match="Lost superstep checkpoint"):
                graph.invoke(None, config, durability="sync")
        before = graph.get_state(config)
        assert before.next == ()
        assert len(before.tasks) == 1 and before.tasks[0].result is not None
    result = revoke_session_writes(tmp_path, client, contract)
    restored = snapshot(tmp_path, client, contract)
    assert restored.next == () and restored.tasks == ()
    assert len(result["verified_receipts"]) == len(backend.writes) == 1
    for field in ("messages", "receipts", "observations", "pending_actions", "handoff_summary"):
        assert restored.values[field] == before.values[field]
    assert_tool_pairs(restored.values["messages"])
    assert_readonly(restored.values["contract"], contract)


def test_readonly_completed_read_is_fully_stopped_on_first_revocation(
    tmp_path, desk, contract, monkeypatch
):
    backend, client = desk
    readonly = contract.model_copy(
        update={"allow_notes": False, "allowed_status_ids": (), "allow_close": False}
    )
    checkpoint_before_tools(
        tmp_path, client, readonly, tool_call("read_ticket", {}, "completed-read").tool_calls
    )
    config = {"configurable": {"thread_id": readonly.thread_id}}
    with session_storage(tmp_path, readonly.thread_id) as (saver, _):
        original_put = saver.put

        def fail_completed_checkpoint(config, checkpoint, metadata, new_versions):
            if checkpoint["channel_values"].get("observations"):
                raise RuntimeError("Lost read superstep checkpoint")
            return original_put(config, checkpoint, metadata, new_versions)

        graph = build_desk_agent(ScriptedChatModel(), client, readonly, checkpointer=saver)
        with monkeypatch.context() as patch:
            patch.setattr(saver, "put", fail_completed_checkpoint)
            with pytest.raises(RuntimeError, match="Lost read superstep checkpoint"):
                graph.invoke(None, config, durability="sync")
        before = graph.get_state(config)
        assert before.next == ()
        assert len(before.tasks) == 1 and before.tasks[0].result is not None
    result = revoke_session_writes(tmp_path, client, readonly)
    after = snapshot(tmp_path, client, readonly)
    assert after.next == () and after.tasks == ()
    assert after.values == before.values
    assert_tool_pairs(after.values["messages"])
    assert revoke_session_writes(tmp_path, client, readonly) == result
    assert snapshot(tmp_path, client, readonly).config == after.config
    assert backend.writes == []


def test_readonly_native_b_skills_queue_cleared_and_private_state_preserved(
    tmp_path, desk, contract, monkeypatch
):
    backend, client = desk
    readonly = contract.model_copy(
        update={
            "variant": "B",
            "allow_notes": False,
            "allowed_status_ids": (),
            "allow_close": False,
        }
    )
    config = {"configurable": {"thread_id": readonly.thread_id}}
    with redmine_store(tmp_path) as store:
        components = build_components(
            readonly.scope,
            store,
            PolicyCatalog(
                Path(__file__).parents[1] / "src/servicedesk_agent/assets/policies.json",
                "demo-service-desk",
            ),
        )
        with session_storage(tmp_path, readonly.thread_id) as (saver, _):
            graph = build_desk_agent(
                ScriptedChatModel(), client, readonly, checkpointer=saver, components=components
            )
            graph.invoke(
                {
                    **components.initial_state,
                    "messages": [HumanMessage(content="Review the saved skill.")],
                    "contract": readonly.model_dump(mode="json"),
                    "pending_actions": ["Obtain gateway logs."],
                    "handoff_summary": "Await evidence.",
                    "receipts": {},
                    "observations": {},
                    "pinned_skills": ["resume-ticket"],
                },
                config,
                interrupt_before=["SkillsMiddleware.before_model"],
            )
            before = graph.get_state(config)
            assert before.next == ("SkillsMiddleware.before_model",)
            assert before.values["skills_metadata"]
            assert before.values["pinned_skills"] == ["resume-ticket"]
        simplified = snapshot(tmp_path, client, readonly)
        assert simplified.tasks == ()

        def forbidden(*args, **kwargs):
            pytest.fail("Revocation ran a model or loaded configured B components")

        monkeypatch.setattr(ScriptedChatModel, "_generate", forbidden)
        monkeypatch.setattr("servicedesk_agent.components.build_components", forbidden)
        result = revoke_session_writes(tmp_path, client, readonly)
        with session_storage(tmp_path, readonly.thread_id) as (saver, _):
            graph = build_desk_agent(
                ScriptedChatModel(), client, readonly, checkpointer=saver, components=components
            )
            after = graph.get_state(config)
            assert after.next == () and after.tasks == ()
            assert after.values == before.values
            assert after.config != before.config
        assert revoke_session_writes(tmp_path, client, readonly) == result
        with session_storage(tmp_path, readonly.thread_id) as (saver, _):
            graph = build_desk_agent(
                ScriptedChatModel(), client, readonly, checkpointer=saver, components=components
            )
            repeated = graph.get_state(config)
            assert repeated.config == after.config
            assert repeated.next == () and repeated.tasks == ()
            assert repeated.values == after.values
    assert backend.writes == []


def test_native_compaction_preserved_by_revocation(
    tmp_path,
    desk,
    contract,
    compacted_session,  # noqa: F811
):
    backend, client = desk
    saved = compacted_session
    result = revoke_session_writes(tmp_path, client, contract)
    restored = load_session(tmp_path, client, contract)
    for field in (
        "messages",
        "receipts",
        "observations",
        "pending_actions",
        "handoff_summary",
        "files",
        SUMMARIZATION_EVENT_KEY,
    ):
        assert restored[field] == saved[field]
    readonly = TaskContract.model_validate(result["contract"])
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "add_ticket_note",
                {
                    "note": "Write after compaction.",
                    "expected_version": next(iter(saved["observations"])),
                },
                "denied-compacted",
            ),
            "Read-only state retained.",
        ]
    )
    execute_session(tmp_path, client, readonly, model, "Continue.", resume=True)
    assert readonly.model_dump_json() in model.requests[0][0].text
    assert json.loads(model.requests[-1][-1].text)["error"] == "write_not_authorized"
    assert backend.writes == []


@pytest.mark.parametrize("failure_at", [1, 2, 3])
def test_checkpoint_failure_rolls_back_whole_revocation(
    tmp_path, desk, contract, monkeypatch, failure_at
):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    checkpoint_before_tools(
        tmp_path,
        client,
        contract,
        tool_call(
            "add_ticket_note", {"note": "Pending.", "expected_version": version}, "pending-note"
        ).tool_calls,
    )
    before = snapshot(tmp_path, client, contract)
    original_put = _AtomicSqliteSaver.put
    calls = []

    def fail_put(self, *args, **kwargs):
        result = original_put(self, *args, **kwargs)
        calls.append(True)
        if len(calls) == failure_at:
            raise RuntimeError("Checkpoint failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(_AtomicSqliteSaver, "put", fail_put)
        with pytest.raises(RuntimeError, match="Checkpoint failure"):
            revoke_session_writes(tmp_path, client, contract)
    after = snapshot(tmp_path, client, contract)
    assert after.config == before.config
    assert after.values == before.values
    assert after.next == before.next
    assert after.tasks == before.tasks
    assert_readonly(revoke_session_writes(tmp_path, client, contract)["contract"], contract)
    assert backend.writes == []


@pytest.mark.parametrize("committed", [False, True])
def test_pending_intent_retained_and_readonly_reconciliation_available(
    tmp_path, desk, contract, committed
):
    backend, base_client = desk
    backend.apply_writes = committed
    backend.lose_ack = True
    with (
        redmine_store(tmp_path) as store,
        RedmineClient(
            base_client.settings, store=store, transport=httpx.MockTransport(backend.handle)
        ) as client,
    ):
        version = client.get_ticket(42).evidence.source_version
        backend.read_back_fails = True
        with pytest.raises(BusinessError) as raised:
            client.add_note(contract, "Uncertain note.", version)
        assert raised.value.code == "operation_uncertain"
        execute_session(
            tmp_path, client, contract, ScriptedChatModel(responses=["Await evidence."]), "Review."
        )
        before = client._operations.load(contract.thread_id)
        assert before["pending"] is not None
        result = revoke_session_writes(tmp_path, client, contract)
        assert client._operations.load(contract.thread_id) == before
        assert result["verified_receipts"] == []
    backend.read_back_fails = False
    with (
        redmine_store(tmp_path) as store,
        RedmineClient(
            base_client.settings, store=store, transport=httpx.MockTransport(backend.handle)
        ) as client,
    ):
        readonly = TaskContract.model_validate(load_session(tmp_path, client, contract)["contract"])
        model = ScriptedChatModel(
            responses=[
                tool_call("reconcile_ticket_operations", {}, "reconcile"),
                "Read-back finished.",
            ]
        )
        result = execute_session(tmp_path, client, readonly, model, "Reconcile.", resume=True)
        records = client._operations.load(contract.thread_id)
        if committed:
            assert len(result["verified_receipts"]) == 1 and records["pending"] is None
        else:
            assert records == before
            assert result["verified_receipts"] == []
            assert json.loads(model.requests[-1][-1].text)["error"] == "operation_uncertain"
    assert len(backend.writes) == 1


def test_revocation_persists_across_processes(tmp_path, desk, contract):
    backend, client = desk
    checkpoint_before_tools(
        tmp_path,
        client,
        contract,
        tool_call(
            "add_ticket_note",
            {"note": "Pending.", "expected_version": client.get_ticket(42).evidence.source_version},
            "pending-note",
        ).tool_calls,
    )
    code = """
import json, os, sys
from pathlib import Path
import httpx
sys.path.insert(0, os.environ["TEST_SUPPORT"])
from conftest import RedmineFixture
from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import revoke_session_writes
backend = RedmineFixture()
with RedmineClient(
    RedmineSettings(url="https://desk.test", api_key="test-key"),
    transport=httpx.MockTransport(backend.handle),
) as client:
    result = revoke_session_writes(
        Path(os.environ["TEST_ROOT"]), client,
        TaskContract.model_validate_json(os.environ["TEST_CONTRACT"]),
    )
    assert backend.writes == []
    print(json.dumps(result))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "TEST_SUPPORT": str(Path(__file__).parent),
            "TEST_ROOT": str(tmp_path),
            "TEST_CONTRACT": contract.model_dump_json(),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(completed.stdout)
    restored = snapshot(tmp_path, client, contract)
    assert_readonly(result["contract"], contract)
    assert restored.values["contract"] == result["contract"]
    assert restored.next == () and restored.tasks == ()
    assert_tool_pairs(restored.values["messages"])
    assert backend.writes == []
