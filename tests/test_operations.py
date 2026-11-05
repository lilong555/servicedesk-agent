from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import httpx
import pytest
from langgraph.store.memory import InMemoryStore
from pydantic import SecretStr

from servicedesk_agent.domain import BusinessError, fingerprint
from servicedesk_agent.operations import OperationRecords, redmine_store
from servicedesk_agent.redmine import RedmineClient


def uncertain(call):
    with pytest.raises(BusinessError) as raised:
        call()
    assert raised.value.code == "operation_uncertain"


def test_client_without_store_is_read_only(desk, contract):
    backend, original = desk
    with RedmineClient(original.settings, transport=httpx.MockTransport(backend.handle)) as client:
        version = client.get_ticket(42).evidence.source_version
        with pytest.raises(BusinessError) as raised:
            client.add_note(contract, "Note.", version)
        assert raised.value.code == "operation_store_required"
        assert backend.writes == []


@pytest.mark.parametrize("change", ["body", "editor", "body_and_editor", "duplicate"])
def test_pending_edited_marker_blocks_all_session_writes(desk, contract, change):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.read_back_fails = True
    uncertain(lambda: client.add_note(contract, "Investigated.", version))
    backend.read_back_fails = False
    journal = backend.issue["journals"][0]
    if change in {"body", "body_and_editor"}:
        journal["notes"] = journal["notes"].replace("Investigated.", "Corrected.")
    if change in {"editor", "body_and_editor"}:
        journal["updated_by"] = {"id": 7}
    if change == "duplicate":
        backend.issue["journals"].append({**journal, "id": 2})
    current = client.get_ticket(42).evidence.source_version
    uncertain(lambda: client.add_note(contract, "Investigated.", current))
    uncertain(lambda: client.add_note(contract, "New call, revised note.", current))
    uncertain(lambda: client.set_status(contract, 2, current))
    assert len(backend.writes) == 1


def test_forged_old_status_journal_is_pending_even_when_current_state_matches(desk, contract):
    backend, client = desk
    operation_id = fingerprint(
        {"thread": contract.thread_id, "action": "set_status", "payload": {"status_id": 2}}
    )
    backend.issue["status"] = backend.statuses[1]
    backend.issue["journals"] = [
        {
            "id": 1,
            "user": {"id": 1},
            "updated_by": {"id": 7},
            "notes": f"Service desk status update to 2.\n\n[desk-action:{operation_id}]",
            "details": [],
        }
    ]
    uncertain(lambda: client.set_status(contract, 2, "version-before-the-edit"))
    uncertain(lambda: client.reconcile_operations(contract))
    assert backend.writes == []


@pytest.mark.parametrize(
    "details",
    [
        [],
        None,
        [{"property": "attr", "name": "assigned_to_id", "old_value": "1", "new_value": "2"}],
        [{"property": "attr", "name": "status_id", "old_value": "1", "new_value": "5"}],
        [{"property": "attr", "name": "status_id", "old_value": "5", "new_value": "2"}],
    ],
)
def test_transition_requires_its_real_status_detail(desk, contract, details):
    backend, original = desk

    def handle(request):
        response = backend.handle(request)
        if request.method == "PUT":
            backend.issue["journals"][-1]["details"] = details
        return response

    with RedmineClient(
        original.settings, store=InMemoryStore(), transport=httpx.MockTransport(handle)
    ) as client:
        version = client.get_ticket(42).evidence.source_version
        uncertain(lambda: client.set_status(contract, 2, version))
        uncertain(lambda: client.set_status(contract, 2, version))
    assert len(backend.writes) == 1


def test_authorized_unchanged_status_has_a_new_note_journal_without_transition(desk, contract):
    backend, client = desk
    backend.issue["status"] = backend.statuses[1]
    version = client.get_ticket(42).evidence.source_version
    receipt = client.set_status(contract, 2, version)
    assert receipt.verified
    assert backend.issue["journals"][0]["details"] == []
    assert client.set_status(contract, 2, version).replayed
    assert len(backend.writes) == 1


def test_readonly_reconciliation_clears_pending_without_granting_writes(desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.read_back_fails = True
    uncertain(lambda: client.add_note(contract, "Investigated.", version))
    backend.read_back_fails = False
    readonly = contract.model_copy(update={"allow_notes": False, "allowed_status_ids": ()})
    receipts = client.reconcile_operations(readonly)
    assert len(receipts) == 1 and receipts[0].replayed
    with pytest.raises(BusinessError) as raised:
        client.add_note(
            readonly, "Further investigation.", receipts[0].after.evidence.source_version
        )
    assert raised.value.code == "write_not_authorized"
    next_receipt = client.add_note(
        contract, "Further investigation.", receipts[0].after.evidence.source_version
    )
    assert next_receipt.verified
    assert len(backend.writes) == 2


def test_new_payload_can_proceed_after_automatic_readback_settles_pending(desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.read_back_fails = True
    uncertain(lambda: client.add_note(contract, "Investigated.", version))
    backend.read_back_fails = False
    current = client.get_ticket(42).evidence.source_version
    receipt = client.add_note(contract, "Next step.", current)
    assert receipt.verified and not receipt.replayed
    assert client.add_note(contract, "Investigated.", version).replayed
    assert len(backend.writes) == 2


@pytest.mark.parametrize("fail_at", [1, 2])
def test_store_failure_retains_safe_submission_boundary(desk, contract, fail_at):
    backend, original = desk

    class FaultyStore(InMemoryStore):
        puts = 0

        def put(self, *args, **kwargs):
            self.puts += 1
            if self.puts == fail_at:
                raise OSError("Synthetic disk failure")
            return super().put(*args, **kwargs)

    store = FaultyStore()
    with RedmineClient(
        original.settings, store=store, transport=httpx.MockTransport(backend.handle)
    ) as client:
        version = client.get_ticket(42).evidence.source_version
        with pytest.raises(BusinessError) as raised:
            client.add_note(contract, "Investigated.", version)
        assert raised.value.code == "operation_store_unavailable"
        assert len(backend.writes) == fail_at - 1
        receipt = client.add_note(contract, "Investigated.", version)
        assert receipt.verified
        assert receipt.replayed is (fail_at == 2)
        assert len(backend.writes) == 1


def test_intent_is_visible_before_http_and_contains_no_body_or_credentials(desk, contract):
    backend, original = desk
    store = InMemoryStore()

    def handle(request):
        if request.method == "PUT":
            records = store.get(("redmine_operations", contract.thread_id), "writes").value
            assert records["pending"]["journal_id"] is None
            assert records["pending"]["before_version"] == version
            encoded = json.dumps(records)
            assert "Confidential investigation body." not in encoded
            assert original.settings.api_key.get_secret_value() not in encoded
            assert "X-Redmine-API-Key" not in encoded
        return backend.handle(request)

    with RedmineClient(
        original.settings, store=store, transport=httpx.MockTransport(handle)
    ) as client:
        version = client.get_ticket(42).evidence.source_version
        receipt = client.add_note(contract, "Confidential investigation body.", version)
        records = store.get(("redmine_operations", contract.thread_id), "writes").value
        assert records["pending"] is None
        assert records["receipts"][receipt.operation_id]["journal_id"] == receipt.journal_id


@pytest.mark.parametrize("field,value", [("principal_id", 2), ("session_id", "other")])
def test_receipt_namespaces_are_isolated(desk, contract, field, value):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    original = client.add_note(contract, "Investigated.", version)
    changed = contract.model_copy(update={field: value})
    backend.principal_id = changed.principal_id
    receipt = client.add_note(
        changed, "Investigated.", client.get_ticket(42).evidence.source_version
    )
    assert not receipt.replayed
    assert receipt.operation_id != original.operation_id
    assert len(backend.writes) == 2


def test_api_key_rotation_preserves_pending_and_principal_check(desk, contract):
    backend, original = desk
    store = InMemoryStore()
    version = original.get_ticket(42).evidence.source_version
    backend.apply_writes = False
    backend.lose_ack = True
    with RedmineClient(
        original.settings, store=store, transport=httpx.MockTransport(backend.handle)
    ) as client:
        uncertain(lambda: client.add_note(contract, "Investigated.", version))
    settings = original.settings.model_copy(update={"api_key": SecretStr("rotated-synthetic-key")})
    with RedmineClient(
        settings, store=store, transport=httpx.MockTransport(backend.handle)
    ) as client:
        uncertain(lambda: client.add_note(contract, "New call.", version))
        backend.principal_id = 2
        with pytest.raises(BusinessError) as raised:
            client.reconcile_operations(contract)
        assert raised.value.code == "identity_changed"
    assert len(backend.writes) == 1


def test_parallel_clients_share_the_pending_slot(desk, contract):
    backend, original = desk
    store = InMemoryStore()
    started, release = Event(), Event()

    def handle(request):
        if request.method == "PUT":
            started.set()
            assert release.wait(10)
        return backend.handle(request)

    with (
        RedmineClient(
            original.settings, store=store, transport=httpx.MockTransport(handle)
        ) as first,
        RedmineClient(
            original.settings, store=store, transport=httpx.MockTransport(handle)
        ) as second,
        ThreadPoolExecutor() as executor,
    ):
        version = first.get_ticket(42).evidence.source_version
        future = executor.submit(first.add_note, contract, "Investigated.", version)
        try:
            assert started.wait(10)
            with pytest.raises(BusinessError) as raised:
                second.add_note(contract, "Concurrent payload.", version)
            assert raised.value.code == "operation_busy"
        finally:
            release.set()
        assert future.result(timeout=10).verified
        assert second.add_note(contract, "Investigated.", version).replayed
    assert len(backend.writes) == 1


_PROCESS = """
import json, os
from pathlib import Path
from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import BusinessError, TaskContract
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.redmine import RedmineClient
contract = TaskContract.model_validate_json(os.environ["TEST_CONTRACT"])
settings = RedmineSettings(url=contract.service_url, api_key="synthetic-key", timeout=0.2)
with redmine_store(Path(os.environ["TEST_ROOT"])) as store:
    with RedmineClient(settings, store=store) as client:
        try:
            if os.environ["TEST_ACTION"] in {"reconcile", "reconcile_all"}:
                readonly = contract.model_copy(update={"allow_notes": False,
                                                        "allowed_status_ids": ()})
                receipts = client.reconcile_operations(
                    readonly, include_completed=os.environ["TEST_ACTION"] == "reconcile_all"
                )
                result = [r.model_dump(mode="json") for r in receipts]
            else:
                version = client.get_ticket(contract.ticket_id).evidence.source_version
                result = client.add_note(contract, os.environ["TEST_NOTE"], version)
                result = result.model_dump(mode="json")
            print(json.dumps({"ok": True, "result": result}))
        except BusinessError as exc:
            print(json.dumps(exc.as_dict()))
"""


def process(root, contract, *, action="note", note="Investigated."):
    result = subprocess.run(
        [sys.executable, "-c", _PROCESS],
        env={
            **os.environ,
            "TEST_ROOT": str(root),
            "TEST_CONTRACT": contract.model_dump_json(),
            "TEST_ACTION": action,
            "TEST_NOTE": note,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


@contextmanager
def local_redmine(backend, *, delayed=False):
    release, committed = Event(), Event()
    submitted = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.respond()

        def do_PUT(self):
            self.respond()

        def respond(self):
            content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = httpx.Request(self.command, f"http://127.0.0.1{self.path}", content=content)
            if self.command == "PUT":
                submitted.append(json.loads(content))
                if delayed and len(submitted) == 1:
                    release.wait(15)
            response = backend.handle(request)
            if self.command == "PUT":
                committed.set()
            try:
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", submitted, release, committed
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_real_http_late_commit_survives_new_process_and_new_call_payload(tmp_path, desk, contract):
    backend, _ = desk
    with local_redmine(backend, delayed=True) as (url, submitted, release, committed):
        contract = contract.model_copy(update={"service_url": url})
        first = process(tmp_path, contract)
        assert first["error"] == "operation_uncertain"
        assert len(submitted) == 1
        assert backend.issue["journals"] == []
        for note in ["Investigated.", "New call id with revised payload."]:
            retry = process(tmp_path, contract, note=note)
            assert retry["error"] == "operation_uncertain"
        assert len(submitted) == 1
        release.set()
        assert committed.wait(10)
        recovered = process(tmp_path, contract, action="reconcile")
        assert recovered["ok"] and len(recovered["result"]) == 1
        assert recovered["result"][0]["replayed"]
        replay = process(tmp_path, contract)
        assert replay["ok"] and replay["result"]["replayed"]
        next_action = process(tmp_path, contract, note="Next authorized action.")
        assert next_action["ok"]
        assert len(submitted) == 2
        assert len(backend.issue["journals"]) == 2
    assert (tmp_path / "memory.sqlite").stat().st_mode & 0o777 == 0o600


def test_real_http_edited_journal_stays_pending_across_processes(tmp_path, desk, contract):
    backend, _ = desk
    backend.read_back_fails = True
    with local_redmine(backend) as (url, submitted, _, _):
        contract = contract.model_copy(update={"service_url": url})
        assert process(tmp_path, contract)["error"] == "operation_uncertain"
        backend.read_back_fails = False
        original = deepcopy(backend.issue["journals"][0])
        backend.issue["journals"][0]["notes"] = original["notes"].replace(
            "Investigated.", "Corrected by owner."
        )
        backend.issue["journals"][0]["updated_by"] = {"id": 7}
        assert process(tmp_path, contract)["error"] == "operation_uncertain"
        assert process(tmp_path, contract, action="reconcile")["error"] == "operation_uncertain"
        assert len(submitted) == 1
        backend.issue["journals"][0] = original
        assert process(tmp_path, contract, action="reconcile")["ok"]
        assert process(tmp_path, contract)["result"]["replayed"]
        assert len(submitted) == 1


def test_process_lock_blocks_overlapping_sqlite_clients(tmp_path, desk, contract):
    backend, _ = desk
    with local_redmine(backend) as (url, submitted, _, _):
        contract = contract.model_copy(update={"service_url": url})
        with redmine_store(tmp_path) as store:
            records = OperationRecords(store)
            with records.locked(contract.thread_id):
                assert process(tmp_path, contract)["error"] == "operation_busy"
            assert process(tmp_path, contract)["ok"]
        assert len(submitted) == 1


def test_native_new_call_id_cannot_bypass_an_unresolved_operation(tmp_path, desk, contract):
    from servicedesk_agent.session import execute_session
    from servicedesk_agent.testing import ScriptedChatModel, tool_call

    backend, client = desk
    backend.apply_writes = False
    backend.lose_ack = True
    version = client.get_ticket(42).evidence.source_version
    first_model = ScriptedChatModel(
        responses=[
            tool_call(
                "add_ticket_note",
                {"note": "Investigated.", "expected_version": version},
                "original-native-call",
            ),
            "The operation requires reconciliation.",
        ]
    )
    result = execute_session(tmp_path, client, contract, first_model, "Investigate.")
    assert result["verified_receipts"] == []
    second_model = ScriptedChatModel(
        responses=[
            tool_call(
                "add_ticket_note",
                {"note": "Investigated again.", "expected_version": version},
                "new-native-call",
            ),
            "The operation remains unresolved.",
        ]
    )
    result = execute_session(
        tmp_path, client, contract, second_model, "Continue investigation.", resume=True
    )
    errors = [
        json.loads(message.content)
        for message in second_model.requests[-1]
        if message.type == "tool" and message.tool_call_id == "new-native-call"
    ]
    assert errors[0]["error"] == "operation_uncertain"
    assert result["verified_receipts"] == []
    assert len(backend.writes) == 1


@pytest.mark.parametrize(
    "scope_change",
    [{"project_id": 2}, {"service_url": "https://another.test"}, {"principal_id": 7}],
)
def test_reconciliation_revalidates_scope_before_reading_receipts(desk, contract, scope_change):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.read_back_fails = True
    uncertain(lambda: client.add_note(contract, "Investigated.", version))
    backend.read_back_fails = False
    with pytest.raises(BusinessError) as raised:
        client.reconcile_operations(contract.model_copy(update=scope_change))
    assert raised.value.code in {"identity_changed", "scope_mismatch"}
    assert client.reconcile_operations(contract)[0].verified
    assert len(backend.writes) == 1


def test_known_rejection_clears_intent_for_a_later_authorized_attempt(desk, contract):
    backend, original = desk
    reject = True
    store = InMemoryStore()

    def handle(request):
        if request.method == "PUT" and reject:
            return httpx.Response(422)
        return backend.handle(request)

    with RedmineClient(
        original.settings, store=store, transport=httpx.MockTransport(handle)
    ) as client:
        version = client.get_ticket(42).evidence.source_version
        with pytest.raises(BusinessError) as raised:
            client.add_note(contract, "Investigated.", version)
        assert raised.value.code == "business_rejection"
        assert (
            store.get(("redmine_operations", contract.thread_id), "writes").value["pending"] is None
        )
        reject = False
        assert client.add_note(contract, "Investigated.", version).verified
        assert len(backend.writes) == 1


@pytest.mark.parametrize("recovery", ["retry", "reconcile", "include_completed", "new_action"])
def test_lost_status_readback_is_completed_even_after_status_changes(desk, contract, recovery):
    backend, client = desk
    backend.lose_ack = True
    backend.read_back_fails = True
    version = client.get_ticket(42).evidence.source_version
    uncertain(lambda: client.set_status(contract, 2, version))
    records = client._operations.load(contract.thread_id)
    operation_id = records["pending"]["operation_id"]
    assert records["pending"]["journal_id"] is None
    backend.read_back_fails = False
    backend.lose_ack = False
    backend.issue["status"] = backend.statuses[0]
    current = client.get_ticket(42).evidence.source_version
    if recovery in {"retry", "reconcile"}:
        with pytest.raises(BusinessError) as raised:
            if recovery == "retry":
                client.set_status(contract, 2, current)
            else:
                client.reconcile_operations(contract)
        assert raised.value.code == "action_superseded"
    elif recovery == "include_completed":
        assert client.reconcile_operations(contract, include_completed=True) == []
    else:
        assert client.add_note(contract, "Independent next action.", current).verified
    records = client._operations.load(contract.thread_id)
    assert records["pending"] is None
    assert records["receipts"][operation_id]["journal_id"] == backend.issue["journals"][0]["id"]
    assert records["receipts"][operation_id]["before_version"] == version
    with pytest.raises(BusinessError) as raised:
        client.set_status(contract, 2, current)
    assert raised.value.code == "action_superseded"
    if recovery != "new_action":
        assert client.add_note(contract, "Independent next action.", current).verified
    assert len(backend.writes) == 2


@pytest.mark.parametrize("change", ["body", "editor", "removed", "status_details"])
def test_changed_completed_evidence_preserves_completion_and_allows_other_writes(
    desk, contract, change
):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    action = client.set_status if change == "status_details" else client.add_note
    payload = 2 if change == "status_details" else "Investigated."
    receipt = action(contract, payload, version)
    original_records = client._operations.load(contract.thread_id)
    journal = backend.issue["journals"][0]
    if change == "body":
        journal["notes"] = journal["notes"].replace("Investigated.", "Edited by owner.")
    elif change == "editor":
        journal["updated_by"] = {"id": 7}
    elif change == "removed":
        backend.issue["journals"] = []
    else:
        journal["details"] = []
    for _ in range(2):
        with pytest.raises(BusinessError) as raised:
            action(contract, payload, client.get_ticket(42).evidence.source_version)
        assert raised.value.code == "action_evidence_changed"
    assert client.reconcile_operations(contract, include_completed=True) == []
    assert client._operations.load(contract.thread_id) == original_records
    current = client.get_ticket(42).evidence.source_version
    assert client.add_note(contract, "Independent next action.", current).verified
    records = client._operations.load(contract.thread_id)
    assert records["pending"] is None
    assert (
        records["receipts"][receipt.operation_id]
        == original_records["receipts"][receipt.operation_id]
    )
    assert len(backend.writes) == 2


def test_include_completed_rebuilds_receipts_without_rewriting_store(desk, contract, monkeypatch):
    backend, client = desk
    original = client.add_note(
        contract, "Investigated.", client.get_ticket(42).evidence.source_version
    )
    readonly = contract.model_copy(update={"allow_notes": False, "allowed_status_ids": ()})
    assert client.reconcile_operations(readonly) == []
    backend.requests.clear()

    def denied_save(*_args, **_kwargs):
        raise AssertionError("Completed receipt replay must only read the Store.")

    monkeypatch.setattr(client._operations, "save", denied_save)
    for _ in range(2):
        receipts = client.reconcile_operations(readonly, include_completed=True)
        assert len(receipts) == 1
        assert receipts[0].operation_id == original.operation_id
        assert receipts[0].before_version == original.before_version
        assert receipts[0].journal_id == original.journal_id
        assert receipts[0].verified and receipts[0].replayed
    assert all(request.method == "GET" for request in backend.requests)
    assert len(backend.writes) == 1


def test_include_completed_skips_changed_history_and_keeps_all_compact_records(desk, contract):
    backend, client = desk
    edited = client.add_note(
        contract, "Investigated.", client.get_ticket(42).evidence.source_version
    )
    superseded = client.set_status(contract, 2, client.get_ticket(42).evidence.source_version)
    valid = client.add_note(contract, "Reviewed.", client.get_ticket(42).evidence.source_version)
    records = client._operations.load(contract.thread_id)
    backend.issue["journals"][0]["updated_by"] = {"id": 7}
    backend.issue["status"] = backend.statuses[0]
    receipts = client.reconcile_operations(contract, include_completed=True)
    assert [receipt.operation_id for receipt in receipts] == [valid.operation_id]
    assert set(records["receipts"]) == {
        edited.operation_id,
        superseded.operation_id,
        valid.operation_id,
    }
    assert client._operations.load(contract.thread_id) == records
    assert len(backend.writes) == 3


def test_include_completed_keeps_unresolved_submission_explicit(desk, contract):
    backend, client = desk
    complete = client.add_note(
        contract, "Investigated.", client.get_ticket(42).evidence.source_version
    )
    backend.apply_writes = False
    uncertain(
        lambda: client.add_note(
            contract, "Unresolved.", client.get_ticket(42).evidence.source_version
        )
    )
    records = client._operations.load(contract.thread_id)
    uncertain(lambda: client.reconcile_operations(contract, include_completed=True))
    assert client._operations.load(contract.thread_id) == records
    assert records["receipts"][complete.operation_id]["journal_id"] == complete.journal_id
    assert len(backend.writes) == 2


def test_include_completed_reconciles_pending_once_alongside_history(desk, contract, monkeypatch):
    backend, client = desk
    complete = client.add_note(
        contract, "Investigated.", client.get_ticket(42).evidence.source_version
    )
    current = client.get_ticket(42).evidence.source_version
    backend.lose_ack = True
    original_get = client.get_ticket
    reads = 0

    def read_once(ticket_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            backend.read_back_fails = True
        return original_get(ticket_id)

    with monkeypatch.context() as patch:
        patch.setattr(client, "get_ticket", read_once)
        uncertain(lambda: client.add_note(contract, "Reviewed.", current))
    backend.read_back_fails = False
    pending_id = client._operations.load(contract.thread_id)["pending"]["operation_id"]
    receipts = client.reconcile_operations(contract, include_completed=True)
    assert {receipt.operation_id for receipt in receipts} == {complete.operation_id, pending_id}
    assert len(receipts) == 2
    assert all(receipt.replayed for receipt in receipts)
    assert client._operations.load(contract.thread_id)["pending"] is None
    assert len(backend.writes) == 2


def test_completed_readback_failure_preserves_records_without_pending(desk, contract):
    backend, client = desk
    client.add_note(contract, "Investigated.", client.get_ticket(42).evidence.source_version)
    records = client._operations.load(contract.thread_id)
    backend.read_back_fails = True
    with pytest.raises(BusinessError) as raised:
        client.reconcile_operations(contract, include_completed=True)
    assert raised.value.code == "service_unavailable"
    assert client._operations.load(contract.thread_id) == records
    assert records["pending"] is None


def test_completed_receipt_is_recoverable_in_a_new_process_before_checkpoint_update(
    tmp_path, desk, contract
):
    backend, _ = desk
    with local_redmine(backend) as (url, submitted, _, _):
        contract = contract.model_copy(update={"service_url": url})
        first = process(tmp_path, contract)
        assert first["ok"]
        recovered = process(tmp_path, contract, action="reconcile_all")
        assert recovered["ok"] and len(recovered["result"]) == 1
        receipt = recovered["result"][0]
        assert receipt["operation_id"] == first["result"]["operation_id"]
        assert receipt["before_version"] == first["result"]["before_version"]
        assert receipt["journal_id"] == first["result"]["journal_id"]
        assert receipt["replayed"]
        assert len(submitted) == 1
