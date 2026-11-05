"""Scripted wiring regressions for native compaction, persistence and ticket writes."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from deepagents.backends import StateBackend
from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.summarization import (
    SUMMARIZATION_EVENT_KEY,
    SummarizationMiddleware,
)

from servicedesk_agent.components import ComponentBundle
from servicedesk_agent.domain import BusinessError
from servicedesk_agent.session import execute_session, load_session
from servicedesk_agent.testing import ScriptedChatModel, tool_call

PENDING = ["Obtain the gateway log sample.", "Confirm the dependency owner's findings."]
SUMMARY = "Investigation continues."


@pytest.fixture
def compacted_session(tmp_path, desk, contract):
    backend, client = desk
    backend.issue["description"] = "\n".join(
        f"VPN-EVIDENCE-{index:02d}: gateway timeout after authentication; retry={index}."
        for index in range(12)
    )
    backend.issue["journals"] = [
        {
            "id": 91,
            "user": {"id": 7, "name": "Original owner"},
            "notes": "RAW-START: trace 4f72\nGateway reset at 03:17:42.\nRAW-END: code E104",
        }
    ]
    backend.issue["relations"] = [
        {"id": 12, "issue_id": 42, "issue_to_id": 43, "relation_type": "blocked"}
    ]
    initial_model = ScriptedChatModel(
        responses=[
            tool_call("read_ticket", {}, "original-evidence"),
            tool_call(
                "save_handoff",
                {"pending_actions": PENDING, "summary": "Awaiting original gateway logs."},
                "save-pending",
            ),
            "Initial investigation saved.",
        ]
    )
    execute_session(tmp_path, client, contract, initial_model, "Investigate and keep open.")
    before = load_session(tmp_path, client, contract)
    assert not before.get(SUMMARIZATION_EVENT_KEY)

    # Only the trigger is reduced; the native middleware performs all compaction and offload.
    summarizer = ScriptedChatModel(responses=[SUMMARY])
    file_backend = StateBackend()
    components = ComponentBundle(
        agent_kwargs={
            "backend": file_backend,
            "middleware": [
                SummarizationMiddleware(
                    model=summarizer,
                    backend=file_backend,
                    trigger=("messages", 7),
                    keep=("messages", 2),
                    trim_tokens_to_summarize=None,
                )
            ],
        }
    )
    compact_model = ScriptedChatModel(responses=["Compaction checkpoint saved."])
    result = execute_session(
        tmp_path,
        client,
        contract,
        compact_model,
        "Continue the investigation.",
        resume=True,
        components=components,
    )
    state = load_session(tmp_path, client, contract)
    event = state[SUMMARIZATION_EVENT_KEY]
    assert event["cutoff_index"] > 0
    assert event["file_path"] in state["files"]
    assert event["summary_message"].additional_kwargs["lc_source"] == "summarization"
    assert SUMMARY in event["summary_message"].text
    assert len(summarizer.requests) == 1
    assert len(compact_model.requests) == 1
    original = next(
        message
        for message in before["messages"]
        if message.type == "tool" and message.tool_call_id == "original-evidence"
    )
    assert original.text in "\n".join(message.text for message in summarizer.requests[0])
    assert all("VPN-EVIDENCE-" not in message.text for message in compact_model.requests[0])
    assert len([m for m in compact_model.requests[0] if m.type != "system"]) < len(
        before["messages"]
    )
    assert result["contract"] == state["contract"] == contract.model_dump(mode="json")
    assert result["pending_actions"] == state["pending_actions"] == PENDING
    assert state["observations"] == before["observations"]
    assert state["handoff_summary"] == before["handoff_summary"]
    return state


def test_native_compaction_preserves_contract_and_pending_after_sqlite_rebuild(
    tmp_path, desk, contract, compacted_session
):
    backend, client = desk
    saved = compacted_session
    assert saved["contract"]["allow_close"] is False
    assert "allow_close" not in saved[SUMMARIZATION_EVENT_KEY]["summary_message"].text
    assert all(
        action not in saved[SUMMARIZATION_EVENT_KEY]["summary_message"].text for action in PENDING
    )
    version = next(iter(saved["observations"]))
    model = ScriptedChatModel(
        responses=[
            tool_call("read_handoff", {}, "restored-handoff"),
            tool_call(
                "change_ticket_status",
                {"status_id": 5, "expected_version": version},
                "forbidden-close",
            ),
            "The ticket remains open; pending actions are retained.",
        ]
    )
    result = execute_session(
        tmp_path, client, contract, model, "Resume from the saved handoff.", resume=True
    )
    restored = load_session(tmp_path, client, contract)
    replies = {
        message.tool_call_id: json.loads(message.text)
        for message in model.requests[-1]
        if message.type == "tool"
    }
    assert replies["restored-handoff"]["contract"] == saved["contract"]
    assert replies["restored-handoff"]["pending_actions"] == PENDING
    assert replies["forbidden-close"]["ok"] is False
    assert replies["forbidden-close"]["error"] == "closure_not_authorized"
    assert result["contract"] == restored["contract"] == saved["contract"]
    assert restored["contract"]["allow_close"] is False
    assert result["pending_actions"] == restored["pending_actions"] == PENDING
    assert restored[SUMMARIZATION_EVENT_KEY] == saved[SUMMARIZATION_EVENT_KEY]
    assert restored["files"] == saved["files"]
    assert backend.writes == []
    assert backend.issue["status"]["id"] == 1


def test_native_read_file_recovers_full_offloaded_evidence_after_sqlite_rebuild(
    tmp_path, desk, contract, compacted_session
):
    backend, client = desk
    saved = compacted_session
    path = saved[SUMMARIZATION_EVENT_KEY]["file_path"]
    history = file_data_to_string(saved["files"][path])
    original = next(
        message
        for message in saved["messages"]
        if message.type == "tool" and message.tool_call_id == "original-evidence"
    )
    assert original.text in history
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "read_file",
                {"file_path": path, "offset": 0, "limit": len(history.splitlines()) + 1},
                "recover-raw-evidence",
            ),
            "Original ticket evidence recovered.",
        ]
    )
    result = execute_session(
        tmp_path, client, contract, model, "Read the saved original evidence.", resume=True
    )
    first_request = model.requests[0]
    assert any(path in message.text for message in first_request)
    assert all("VPN-EVIDENCE-" not in message.text for message in first_request)
    recovered = next(
        message
        for message in model.requests[-1]
        if message.type == "tool" and message.tool_call_id == "recover-raw-evidence"
    )
    assert recovered.status == "success"
    header, separator, body = recovered.text.partition("\n")
    line_count = len(history.splitlines())
    assert header == f"@@ lines 1-{line_count} of {line_count} @@"
    assert separator == "\n"
    assert body == history.removesuffix("\n")
    assert original.text in recovered.text
    assert "RAW-START" in recovered.text and "RAW-END" in recovered.text
    assert all(f"VPN-EVIDENCE-{index:02d}" in recovered.text for index in range(12))
    assert all("read_file" in names for names in model.visible_tools)
    assert result["contract"] == saved["contract"]
    assert result["pending_actions"] == PENDING
    assert load_session(tmp_path, client, contract)["files"][path] == saved["files"][path]
    assert backend.writes == []


@pytest.mark.parametrize("action", ["add_note", "set_status"])
@pytest.mark.parametrize("dependency_change", ["add", "remove", "retarget"])
def test_dependency_change_rejects_old_version_and_survives_narrow_write(
    desk, contract, action, dependency_change
):
    backend, client = desk
    backend.issue["relations"] = [
        {"id": 12, "issue_id": 42, "issue_to_id": 43, "relation_type": "blocked"}
    ]
    before = client.get_ticket(42)
    if dependency_change == "add":
        backend.issue["relations"].append(
            {"id": 13, "issue_id": 42, "issue_to_id": 44, "relation_type": "precedes"}
        )
    elif dependency_change == "remove":
        backend.issue["relations"].clear()
    else:
        backend.issue["relations"][0]["issue_to_id"] = 45
    relations = deepcopy(backend.issue["relations"])
    current = client.get_ticket(42)
    assert current.evidence.content["updated_on"] == before.evidence.content["updated_on"]
    assert current.evidence.source_version != before.evidence.source_version
    assert current.relations == relations
    value = "Reviewed the updated dependency." if action == "add_note" else 2
    write = getattr(client, action)
    with pytest.raises(BusinessError) as raised:
        write(contract, value, before.evidence.source_version)
    assert raised.value.code == "stale_observation"
    assert raised.value.evidence.relations == relations
    assert raised.value.evidence.evidence.source_version == current.evidence.source_version
    assert backend.writes == []

    receipt = write(contract, value, current.evidence.source_version)
    assert receipt.verified is True
    assert receipt.replayed is False
    assert receipt.before_version == current.evidence.source_version
    assert receipt.after.relations == backend.issue["relations"] == relations
    assert receipt.after.evidence.content["relations"] == relations
    assert receipt.after.assigned_to_id == before.assigned_to_id
    assert receipt.after.description == before.description
    assert receipt.after.is_closed is False
    assert receipt.after.status_id == (2 if action == "set_status" else before.status_id)
    assert len(backend.writes) == 1
    assert set(backend.writes[0]) == ({"notes"} if action == "add_note" else {"status_id", "notes"})
    assert all(
        "relations" in request.url.params.get("include", "").split(",")
        for request in backend.requests
        if request.method == "GET" and request.url.path == "/issues/42.json"
    )
