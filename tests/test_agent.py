from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from servicedesk_agent import session
from servicedesk_agent.components import ComponentBundle
from servicedesk_agent.domain import BusinessError
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session, load_session, session_storage
from servicedesk_agent.testing import ScriptedChatModel, tool_call


def note_after_read(messages):
    payload = json.loads(
        next(message.content for message in reversed(messages) if message.type == "tool")
    )
    return tool_call(
        "add_ticket_note",
        {
            "note": "Investigated VPN logs.",
            "expected_version": payload["ticket"]["evidence"]["source_version"],
        },
        "note-1",
    )


def test_native_tools_receipts_and_handoff_survive_rebuild(tmp_path, desk, contract):
    backend, client = desk
    first_model = ScriptedChatModel(
        responses=[
            tool_call("read_ticket", {}, "read-1"),
            note_after_read,
            tool_call(
                "save_handoff",
                {
                    "pending_actions": ["Check VPN gateway logs."],
                    "summary": "Initial note verified.",
                },
                "handoff-1",
            ),
            "Initial investigation saved.",
        ]
    )
    result = execute_session(tmp_path, client, contract, first_model, "Investigate and keep open.")
    assert len(result["verified_receipts"]) == 1
    assert result["pending_actions"] == ["Check VPN gateway logs."]
    assert all(
        "task" not in names and "execute" not in names for names in first_model.visible_tools
    )
    assert len(backend.writes) == 1

    backend.issue["assigned_to"] = {"id": 8, "name": "New owner"}
    second_model = ScriptedChatModel(
        responses=[
            tool_call("read_handoff", {}, "handoff-2"),
            tool_call("read_ticket", {}, "read-2"),
            "The current owner is New owner; the ticket remains open.",
        ]
    )
    resumed = execute_session(
        tmp_path, client, contract, second_model, "Continue investigating.", resume=True
    )
    assert resumed["contract"]["allow_close"] is False
    assert resumed["pending_actions"] == result["pending_actions"]
    assert len(resumed["verified_receipts"]) == 1
    assert backend.issue["assigned_to"]["id"] == 8
    assert backend.issue["status"]["id"] == 1
    assert "New owner" in json.dumps(
        [message.content for message in second_model.requests[-1]], default=str
    )


def test_missing_existing_and_modified_contracts_are_rejected(tmp_path, desk, contract):
    _, client = desk
    with pytest.raises(BusinessError) as raised:
        execute_session(tmp_path, client, contract, ScriptedChatModel(), "Continue", resume=True)
    assert raised.value.code == "session_missing"
    execute_session(tmp_path, client, contract, ScriptedChatModel(responses=["Ready"]), "Start")
    with pytest.raises(BusinessError) as raised:
        execute_session(tmp_path, client, contract, ScriptedChatModel(), "Start")
    assert raised.value.code == "session_exists"
    modified = contract.model_copy(update={"allow_close": True})
    with pytest.raises(BusinessError) as raised:
        execute_session(tmp_path, client, modified, ScriptedChatModel(), "Continue", resume=True)
    assert raised.value.code == "contract_changed"


def test_scoped_session_has_no_other_ticket_or_user_state(tmp_path, desk, contract):
    _, client = desk
    execute_session(tmp_path, client, contract, ScriptedChatModel(responses=["Saved"]), "Start")
    for updates in ({"principal_id": 2}, {"ticket_id": 43}, {"session_id": "another"}):
        other = contract.model_copy(update=updates)
        assert load_session(tmp_path, client, other) == {}


def test_single_writer_lock(tmp_path, contract):
    with session_storage(tmp_path, contract.thread_id):
        with pytest.raises(BusinessError) as raised:
            with session_storage(tmp_path, contract.thread_id):
                pass
        assert raised.value.code == "session_busy"


def test_checkpoint_can_be_opened_in_a_new_process(tmp_path, desk, contract):
    _, client = desk
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "save_handoff",
                {"pending_actions": ["Read raw evidence."], "summary": "Resume investigation."},
                "handoff",
            ),
            "Saved.",
        ]
    )
    execute_session(tmp_path, client, contract, model, "Investigate")
    code = """
import json, os
from pathlib import Path
from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import load_session
contract = TaskContract.model_validate_json(os.environ["TEST_CONTRACT"])
with RedmineClient(RedmineSettings(url="https://desk.test")) as client:
    state = load_session(Path(os.environ["TEST_ROOT"]), client, contract)
    assert state["contract"]["allow_close"] is False
    assert state["pending_actions"] == ["Read raw evidence."]
    print(json.dumps({"restored": True}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "TEST_CONTRACT": contract.model_dump_json(), "TEST_ROOT": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout)["restored"] is True


@pytest.mark.parametrize("resume", [False, True])
def test_execution_rechecks_the_api_principal_before_model_access(tmp_path, desk, contract, resume):
    backend, client = desk
    if resume:
        execute_session(tmp_path, client, contract, ScriptedChatModel(responses=["Saved"]), "Start")
    before = load_session(tmp_path, client, contract)
    backend.principal_id = 2
    model = ScriptedChatModel(responses=["Read private history"])
    with pytest.raises(BusinessError) as raised:
        execute_session(tmp_path, client, contract, model, "Continue", resume=resume)
    assert raised.value.code == "identity_changed"
    assert model.requests == []
    assert backend.writes == []
    assert load_session(tmp_path, client, contract) == before


@pytest.mark.parametrize(
    "updates",
    [
        {"allow_notes": False},
        {"allow_close": True},
        {"allowed_status_ids": (2,)},
        {"user_constraints": ()},
        {"variant": "B"},
    ],
)
def test_resume_preserves_every_persisted_authority_field(tmp_path, desk, contract, updates):
    _, client = desk
    execute_session(tmp_path, client, contract, ScriptedChatModel(responses=["Saved"]), "Start")
    modified = contract.model_copy(update=updates)
    model = ScriptedChatModel(responses=["Changed"])
    with pytest.raises(BusinessError) as raised:
        execute_session(tmp_path, client, modified, model, "Continue", resume=True)
    assert raised.value.code == "contract_changed"
    assert model.requests == []
    assert load_session(tmp_path, client, contract)["contract"] == contract.model_dump(mode="json")


@pytest.mark.parametrize(
    "field",
    ["contract", "receipts", "observations", "pending_actions", "handoff_summary", "messages"],
)
def test_component_initial_state_preserves_trusted_session_records(tmp_path, desk, contract, field):
    _, client = desk
    components = ComponentBundle(initial_state={field: {}})
    model = ScriptedChatModel(responses=["Saved"])
    with pytest.raises(BusinessError) as raised:
        execute_session(tmp_path, client, contract, model, "Start", components=components)
    assert raised.value.code == "invalid_component_state"
    assert model.requests == []
    assert load_session(tmp_path, client, contract) == {}


def test_loading_rejects_a_checkpoint_with_a_different_contract_scope(tmp_path, desk, contract):
    _, client = desk
    execute_session(tmp_path, client, contract, ScriptedChatModel(responses=["Saved"]), "Start")
    with session_storage(tmp_path, contract.thread_id) as (saver, _):
        graph = session.build_desk_agent(ScriptedChatModel(), client, contract, checkpointer=saver)
        graph.update_state(
            {"configurable": {"thread_id": contract.thread_id}},
            {"contract": contract.model_copy(update={"ticket_id": 43}).model_dump(mode="json")},
        )
    with pytest.raises(BusinessError) as raised:
        load_session(tmp_path, client, contract)
    assert raised.value.code == "contract_changed"


def test_handoff_and_model_claims_do_not_create_verified_receipts(tmp_path, desk, contract):
    backend, client = desk
    result = execute_session(
        tmp_path,
        client,
        contract,
        ScriptedChatModel(
            responses=[
                tool_call(
                    "save_handoff",
                    {"pending_actions": [], "summary": "Status changed; receipt verified."},
                    "untrusted-claim",
                ),
                "The ticket has been closed successfully.",
            ]
        ),
        "Investigate",
    )
    assert result["verified_receipts"] == []
    assert load_session(tmp_path, client, contract)["receipts"] == {}
    assert json.loads(Path(result["trace_path"]).read_text())["receipt_ids"] == []
    assert backend.writes == []


@pytest.mark.parametrize("finalization_failure", ["state_read", "trace_write"])
def test_finalization_keeps_the_original_model_failure(
    tmp_path, desk, contract, monkeypatch, finalization_failure
):
    _, client = desk
    if finalization_failure == "trace_write":
        write_text = Path.write_text

        def fail_trace(path, *args, **kwargs):
            if path.name.startswith("trace-"):
                raise OSError("Trace disk unavailable")
            return write_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_trace)
    else:
        build = session.build_desk_agent

        def build_with_failed_final_read(*args, **kwargs):
            graph = build(*args, **kwargs)
            get_state = graph.get_state
            calls = 0

            def get_state_once(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise OSError("Checkpoint temporarily unavailable")
                return get_state(*args, **kwargs)

            monkeypatch.setattr(graph, "get_state", get_state_once)
            return graph

        monkeypatch.setattr(session, "build_desk_agent", build_with_failed_final_read)
    with pytest.raises(ValueError, match="exhausted") as raised:
        execute_session(tmp_path, client, contract, ScriptedChatModel(), "Start")
    assert "Session trace finalization failed (OSError)." in raised.value.__notes__
    with session_storage(tmp_path, contract.thread_id):
        pass


def test_interrupted_turn_resumes_without_repeating_the_committed_write(tmp_path, desk, contract):
    backend, client = desk
    with pytest.raises(ValueError, match="exhausted"):
        execute_session(
            tmp_path,
            client,
            contract,
            ScriptedChatModel(responses=[tool_call("read_ticket", {}, "read-1"), note_after_read]),
            "Investigate",
        )
    saved = load_session(tmp_path, client, contract)
    assert len(saved["receipts"]) == 1
    traces = list((tmp_path / "sessions" / contract.thread_id).glob("trace-*.json"))
    trace = json.loads(traces[0].read_text())
    assert trace["error_type"] == "ValueError"
    assert trace["receipt_ids"] == list(saved["receipts"])
    assert traces[0].stat().st_mode & 0o777 == 0o600
    model = ScriptedChatModel(responses=["Continued."])
    result = execute_session(tmp_path, client, contract, model, "Continue", resume=True)
    assert len(result["verified_receipts"]) == 1
    assert len(backend.writes) == 1
    assert result["message"] == "Continued."
    assert len(model.requests) == 1
    assert [message.text for message in model.requests[0] if message.type == "human"] == [
        "Investigate",
        "Continue",
    ]


def test_cross_process_lock_excludes_a_writer_and_releases_on_exit(tmp_path, contract):
    code = """
import os
from pathlib import Path
from servicedesk_agent.domain import BusinessError
from servicedesk_agent.session import session_storage
try:
    with session_storage(Path(os.environ["TEST_ROOT"]), os.environ["TEST_THREAD"]):
        print("acquired")
except BusinessError as exc:
    print(exc.code)
"""
    env = {**os.environ, "TEST_ROOT": str(tmp_path), "TEST_THREAD": contract.thread_id}
    with session_storage(tmp_path, contract.thread_id):
        blocked = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert blocked.stdout.strip() == "session_busy"
    acquired = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert acquired.stdout.strip() == "acquired"


def test_lost_read_back_recovers_receipt_in_a_new_process(tmp_path, desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.read_back_fails = True
    with (
        redmine_store(tmp_path) as store,
        RedmineClient(
            client.settings, store=store, transport=httpx.MockTransport(backend.handle)
        ) as persistent_client,
        pytest.raises(ValueError, match="exhausted"),
    ):
        execute_session(
            tmp_path,
            persistent_client,
            contract,
            ScriptedChatModel(responses=[tool_call("read_ticket", {}, "read-1"), note_after_read]),
            "Investigate",
        )
    assert len(backend.writes) == 1
    assert load_session(tmp_path, client, contract)["receipts"] == {}
    code = """
import json, os
from pathlib import Path
import httpx
from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session
from servicedesk_agent.testing import ScriptedChatModel, tool_call
contract = TaskContract.model_validate_json(os.environ["TEST_CONTRACT"])
issue = json.loads(os.environ["TEST_ISSUE"])
def handle(request):
    assert request.method == "GET", "Recovery attempted a duplicate write"
    if request.url.path == "/users/current.json":
        return httpx.Response(200, json={"user": {"id": contract.principal_id}})
    if request.url.path == "/issues/42.json":
        return httpx.Response(200, json={"issue": issue})
    return httpx.Response(404)
def reconcile_after_input(messages):
    assert messages[-1].type == "human" and messages[-1].text == "Continue"
    return tool_call("add_ticket_note", {
        "note": "Investigated VPN logs.",
        "expected_version": os.environ["TEST_VERSION"],
    }, "retry-note")
model = ScriptedChatModel(responses=[
    reconcile_after_input,
    "Recovered interrupted turn.",
])
with redmine_store(Path(os.environ["TEST_ROOT"])) as store, RedmineClient(
    RedmineSettings(url="https://desk.test"), store=store,
    transport=httpx.MockTransport(handle),
) as client:
    result = execute_session(Path(os.environ["TEST_ROOT"]), client, contract,
                             model, "Continue", resume=True)
    assert len(model.requests) == 2
    assert result["contract"] == contract.model_dump(mode="json")
    assert len(result["verified_receipts"]) == 1
    assert result["verified_receipts"][0]["replayed"] is True
    print(json.dumps({"restored": True}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "TEST_ROOT": str(tmp_path),
            "TEST_CONTRACT": contract.model_dump_json(),
            "TEST_ISSUE": json.dumps(backend.issue),
            "TEST_VERSION": version,
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout)["restored"] is True
    saved = load_session(tmp_path, client, contract)
    assert saved["contract"] == contract.model_dump(mode="json")
    assert len(saved["receipts"]) == 1
    assert next(iter(saved["receipts"].values()))["replayed"] is True
    assert len(backend.writes) == 1
