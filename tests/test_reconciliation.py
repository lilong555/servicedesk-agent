from __future__ import annotations

import json

import httpx
import pytest

from servicedesk_agent.operations import redmine_store
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session, load_session
from servicedesk_agent.testing import ScriptedChatModel, tool_call


def test_readonly_tool_recovers_receipt_after_native_checkpoint_gap(
    tmp_path, desk, contract, monkeypatch
):
    backend, original = desk
    version = original.get_ticket(42).evidence.source_version
    with (
        redmine_store(tmp_path) as store,
        RedmineClient(
            original.settings, store=store, transport=httpx.MockTransport(backend.handle)
        ) as client,
    ):
        write = client.add_note

        def crash_after_ack(*args, **kwargs):
            write(*args, **kwargs)
            raise RuntimeError("Process stopped before persisting the Command.")

        with monkeypatch.context() as patch:
            patch.setattr(client, "add_note", crash_after_ack)
            with pytest.raises(RuntimeError, match="before persisting"):
                execute_session(
                    tmp_path,
                    client,
                    contract,
                    ScriptedChatModel(
                        responses=[
                            tool_call(
                                "add_ticket_note",
                                {"note": "Verified investigation.", "expected_version": version},
                                "lost-receipt",
                            )
                        ]
                    ),
                    "Investigate and add a note.",
                )
        assert load_session(tmp_path, client, contract)["receipts"] == {}
    assert len(backend.writes) == 1
    backend.issue["assigned_to"] = {"id": 9, "name": "Current Owner"}
    first_recovery_request = len(backend.requests)
    with (
        redmine_store(tmp_path) as store,
        RedmineClient(
            original.settings, store=store, transport=httpx.MockTransport(backend.handle)
        ) as client,
    ):
        model = ScriptedChatModel(
            responses=[
                tool_call("reconcile_ticket_operations", {}, "read-back"),
                "The existing investigation note is verified. No new note was submitted.",
            ]
        )
        result = execute_session(
            tmp_path,
            client,
            contract,
            model,
            "Stop further changes. Recover the evidence for the previous note.",
            resume=True,
        )
        saved = load_session(tmp_path, client, contract)
    assert len(backend.writes) == 1
    assert all(request.method == "GET" for request in backend.requests[first_recovery_request:])
    assert len(result["verified_receipts"]) == len(saved["receipts"]) == 1
    receipt = result["verified_receipts"][0]
    assert receipt["verified"] and receipt["replayed"]
    assert receipt["journal_id"] == backend.issue["journals"][0]["id"]
    assert receipt["after"]["assigned_to_id"] == 9
    assert model.requests[0][-1].text.startswith("Stop further changes.")


def test_readonly_tool_keeps_uncommitted_write_uncertain(tmp_path, desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.apply_writes = False
    backend.lose_ack = True
    execute_session(
        tmp_path,
        client,
        contract,
        ScriptedChatModel(
            responses=[
                tool_call(
                    "add_ticket_note",
                    {"note": "Unconfirmed note.", "expected_version": version},
                    "uncertain-note",
                ),
                "The write requires reconciliation.",
            ]
        ),
        "Investigate.",
    )
    first_recovery_request = len(backend.requests)
    model = ScriptedChatModel(
        responses=[
            tool_call("reconcile_ticket_operations", {}, "read-back"),
            "The outcome remains unresolved.",
        ]
    )
    result = execute_session(
        tmp_path,
        client,
        contract,
        model,
        "Only reconcile. Keep all new writes stopped.",
        resume=True,
    )
    response = json.loads(model.requests[-1][-1].content)
    assert response["error"] == "operation_uncertain"
    assert result["verified_receipts"] == []
    assert len(backend.writes) == 1
    assert all(request.method == "GET" for request in backend.requests[first_recovery_request:])


def test_reconciliation_tool_retains_readonly_authority(tmp_path, desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    client.add_note(contract, "Prior confirmed note.", version)
    readonly = contract.model_copy(update={"allow_notes": False, "allowed_status_ids": ()})
    start = len(backend.requests)
    result = execute_session(
        tmp_path,
        client,
        readonly,
        ScriptedChatModel(
            responses=[
                tool_call("reconcile_ticket_operations", {}, "read-back"),
                "The previous note is verified.",
            ]
        ),
        "Read back the prior note.",
    )
    assert result["contract"]["allow_notes"] is False
    assert result["contract"]["allowed_status_ids"] == []
    assert len(result["verified_receipts"]) == len(backend.writes) == 1
    assert all(request.method == "GET" for request in backend.requests[start:])
