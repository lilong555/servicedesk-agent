from __future__ import annotations

import httpx
import pytest

from servicedesk_agent.domain import BusinessError, fingerprint
from servicedesk_agent.redmine import RedmineClient


def test_source_version_detects_same_timestamp_owner_change(desk):
    backend, client = desk
    first = client.get_ticket(42)
    backend.issue["assigned_to"] = {"id": 8, "name": "New owner"}
    second = client.get_ticket(42)
    assert first.evidence.source_version != second.evidence.source_version
    assert first.evidence.observed_at.tzinfo is not None
    assert second.assigned_to_id == 8


def test_read_back_receipt_and_idempotency(desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    receipt = client.add_note(contract, "Investigated VPN logs.", version)
    replay = client.add_note(contract, "Investigated VPN logs.", version)
    assert receipt.verified and not receipt.replayed
    assert replay.replayed
    assert receipt.operation_id == replay.operation_id
    assert len(backend.writes) == 1
    assert set(backend.writes[0]) == {"notes"}
    assert receipt.after.assigned_to_id == 7


def test_stale_observation_blocks_write_and_returns_current_evidence(desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.issue["assigned_to"] = {"id": 8, "name": "New owner"}
    with pytest.raises(BusinessError, match="current ticket") as raised:
        client.add_note(contract, "Investigated.", version)
    assert raised.value.code == "stale_observation"
    assert raised.value.evidence.assigned_to_id == 8
    assert backend.writes == []


def test_closure_is_checked_against_service_status_semantics(desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.statuses[-1]["name"] = "Completed by service"
    with pytest.raises(BusinessError) as raised:
        client.set_status(contract, 5, version)
    assert raised.value.code == "closure_not_authorized"
    assert backend.writes == []


def test_allowed_status_transition(desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    receipt = client.set_status(contract, 2, version)
    assert receipt.after.status_id == 2
    assert set(backend.writes[0]) == {"status_id", "notes"}


def test_read_only_contract(desk, contract):
    backend, client = desk
    readonly = contract.model_copy(update={"allow_notes": False, "allowed_status_ids": ()})
    version = client.get_ticket(42).evidence.source_version
    for mutation in (
        lambda: client.add_note(readonly, "Note", version),
        lambda: client.set_status(readonly, 2, version),
    ):
        with pytest.raises(BusinessError) as raised:
            mutation()
        assert raised.value.code == "write_not_authorized"
    assert backend.writes == []


def test_redmine_workflow_is_authoritative(desk, contract):
    backend, client = desk
    backend.issue["allowed_statuses"] = [backend.statuses[0]]
    version = client.get_ticket(42).evidence.source_version
    with pytest.raises(BusinessError) as raised:
        client.set_status(contract, 2, version)
    assert raised.value.code == "transition_denied"
    assert backend.writes == []


def test_project_and_principal_isolation(desk, contract):
    backend, client = desk
    backend.issue["project"]["id"] = 9
    with pytest.raises(BusinessError) as raised:
        client.get_ticket(42)
    assert raised.value.code == "scope_mismatch"
    backend.issue["project"]["id"] = 1
    backend.principal_id = 2
    with pytest.raises(BusinessError) as raised:
        client.add_note(contract, "Note", "old")
    assert raised.value.code == "identity_changed"
    assert backend.writes == []


def test_lost_acknowledgement_recovers_from_journal(desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.lose_ack = True
    receipt = client.add_note(contract, "One note.", version)
    assert receipt.verified
    assert len(backend.writes) == 1


@pytest.mark.parametrize("failure", ["apply_writes", "read_back_fails"])
def test_unverified_write_is_not_reported_as_complete(desk, contract, failure):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    setattr(backend, failure, failure == "read_back_fails")
    with pytest.raises(BusinessError) as raised:
        client.add_note(contract, "Note.", version)
    assert raised.value.code == "write_outcome_unknown"
    assert len(backend.writes) == 1


def test_replayed_status_that_changed_again_is_superseded(desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    client.set_status(contract, 2, version)
    backend.issue["status"] = backend.statuses[0]
    with pytest.raises(BusinessError) as raised:
        client.set_status(contract, 2, client.get_ticket(42).evidence.source_version)
    assert raised.value.code == "action_superseded"
    assert len(backend.writes) == 1


@pytest.mark.parametrize("code", [301, 401, 403, 404, 409, 422, 503])
def test_http_failures_are_explicit(desk, code):
    backend, client = desk
    backend.http_status = code
    with pytest.raises(BusinessError):
        client.get_ticket(42)


@pytest.mark.parametrize("ack", ["non_json", "wrong_shape", "decoding_error", "server_error"])
@pytest.mark.parametrize("action", ["add_note", "set_status"])
def test_ambiguous_acknowledgement_uses_one_write_and_read_back(desk, contract, ack, action):
    backend, original = desk

    def handle(request):
        response = backend.handle(request)
        if request.method == "PUT":
            if ack == "decoding_error":
                raise httpx.DecodingError("Damaged response encoding", request=request)
            if ack == "server_error":
                return httpx.Response(502)
            if ack == "wrong_shape":
                return httpx.Response(200, json=[])
            return httpx.Response(200, text="Acknowledgement lost at proxy")
        return response

    with RedmineClient(original.settings, transport=httpx.MockTransport(handle)) as client:
        version = client.get_ticket(42).evidence.source_version
        value = "Investigation recorded." if action == "add_note" else 2
        receipt = getattr(client, action)(contract, value, version)
        assert receipt.verified
        assert receipt.journal_id == backend.issue["journals"][0]["id"]
        assert receipt.after.status_id == (1 if action == "add_note" else 2)
        assert len(backend.writes) == 1


@pytest.mark.parametrize("body", [{}, {"issue": []}, {"issue": {"id": 42}}])
@pytest.mark.parametrize("lose_ack", [False, True])
def test_invalid_read_back_remains_unknown_and_can_be_retried(desk, contract, body, lose_ack):
    backend, original = desk
    backend.lose_ack = lose_ack
    invalid_read_back = True

    def handle(request):
        if request.method == "GET" and request.url.path == "/issues/42.json":
            if backend.writes and invalid_read_back:
                return httpx.Response(200, json=body)
        return backend.handle(request)

    with RedmineClient(original.settings, transport=httpx.MockTransport(handle)) as client:
        version = client.get_ticket(42).evidence.source_version
        with pytest.raises(BusinessError) as raised:
            client.add_note(contract, "One persisted note.", version)
        assert raised.value.code == "write_outcome_unknown"
        invalid_read_back = False
        receipt = client.add_note(contract, "One persisted note.", version)
        assert receipt.verified and receipt.replayed
        assert len(backend.writes) == 1


@pytest.mark.parametrize(
    "metadata",
    [
        {"user": {"id": 7}},
        {"user": {}},
        {"user": None},
        {"user": {"id": True}},
        {"id": None},
        {"id": 0},
        {"id": True},
    ],
)
def test_replay_requires_the_original_principal_and_a_journal_id(desk, contract, metadata):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    client.add_note(contract, "Review completed.", version)
    backend.issue["journals"][0].update(metadata)
    with pytest.raises(BusinessError) as raised:
        client.add_note(contract, "Review completed.", version)
    assert raised.value.code == "stale_observation"
    assert len(backend.writes) == 1
    current = client.get_ticket(42).evidence.source_version
    receipt = client.add_note(contract, "Review completed.", current)
    assert receipt.verified and not receipt.replayed
    assert receipt.journal_id == 2
    assert len(backend.writes) == 2


@pytest.mark.parametrize("lose_ack", [False, True])
def test_read_back_from_another_author_does_not_verify_our_write(desk, contract, lose_ack):
    backend, original = desk

    def handle(request):
        response = backend.handle(request)
        if request.method == "PUT":
            backend.issue["journals"][-1]["user"]["id"] = 7
            if lose_ack:
                raise httpx.ReadTimeout("Lost response", request=request)
        return response

    with RedmineClient(original.settings, transport=httpx.MockTransport(handle)) as client:
        version = client.get_ticket(42).evidence.source_version
        with pytest.raises(BusinessError) as raised:
            client.add_note(contract, "One note.", version)
        assert raised.value.code == "write_outcome_unknown"
        assert len(backend.writes) == 1


@pytest.mark.parametrize("lose_ack", [False, True])
def test_status_receipt_requires_both_journal_and_target_state(desk, contract, lose_ack):
    backend, original = desk

    def handle(request):
        response = backend.handle(request)
        if request.method == "PUT":
            backend.issue["status"] = backend.statuses[0]
            if lose_ack:
                raise httpx.ReadTimeout("Lost response", request=request)
        return response

    with RedmineClient(original.settings, transport=httpx.MockTransport(handle)) as client:
        version = client.get_ticket(42).evidence.source_version
        with pytest.raises(BusinessError) as raised:
            client.set_status(contract, 2, version)
        assert raised.value.code == "write_outcome_unknown"
        assert raised.value.evidence.status_id == 1
        assert len(backend.writes) == 1


def test_marker_text_inside_someone_elses_note_is_not_a_receipt(desk, contract):
    backend, client = desk
    operation_id = fingerprint(
        {"thread": contract.thread_id, "action": "add_note", "payload": {"notes": "Reviewed."}}
    )
    backend.issue["journals"] = [
        {
            "id": 1,
            "user": {"id": 7},
            "notes": f"Reviewed.\n\n[desk-action:{operation_id}]",
        }
    ]
    version = client.get_ticket(42).evidence.source_version
    receipt = client.add_note(contract, "Reviewed.", version)
    assert receipt.verified and not receipt.replayed
    assert receipt.journal_id == 2
    assert len(backend.writes) == 1


def test_network_failure_without_commit_is_not_retried_automatically(desk, contract):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.lose_ack = True
    backend.apply_writes = False
    with pytest.raises(BusinessError) as raised:
        client.add_note(contract, "One note.", version)
    assert raised.value.code == "write_outcome_unknown"
    assert len(backend.writes) == 1
    assert backend.issue["journals"] == []
    backend.apply_writes = True
    receipt = client.add_note(contract, "One note.", version)
    assert receipt.verified
    assert len(backend.writes) == 2
    assert len(backend.issue["journals"]) == 1


@pytest.mark.parametrize("status", [401, 403, 409, 422])
def test_explicit_write_denial_produces_no_receipt(desk, contract, status):
    backend, original = desk

    def handle(request):
        if request.method == "PUT":
            return httpx.Response(status)
        return backend.handle(request)

    with RedmineClient(original.settings, transport=httpx.MockTransport(handle)) as client:
        version = client.get_ticket(42).evidence.source_version
        with pytest.raises(BusinessError) as raised:
            client.add_note(contract, "Rejected note.", version)
        assert raised.value.code == (
            "access_denied" if status in {401, 403} else "business_rejection"
        )
        assert backend.writes == []


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_status_semantics_require_an_explicit_boolean(desk, contract, value):
    backend, client = desk
    version = client.get_ticket(42).evidence.source_version
    backend.statuses[-1]["is_closed"] = value
    with pytest.raises(BusinessError) as raised:
        client.set_status(contract, 5, version)
    assert raised.value.code == "unknown_status"
    assert backend.writes == []


@pytest.mark.parametrize("action", ["add_note", "set_status"])
def test_narrow_write_preserves_owner_and_dependencies_changed_during_put(desk, contract, action):
    backend, original = desk
    relations = [{"id": 10, "issue_id": 42, "issue_to_id": 43, "relation_type": "blocks"}]

    def handle(request):
        if request.method == "PUT":
            backend.issue["assigned_to"] = {"id": 8, "name": "Concurrent owner"}
            backend.issue["relations"] = relations
        return backend.handle(request)

    with RedmineClient(original.settings, transport=httpx.MockTransport(handle)) as client:
        version = client.get_ticket(42).evidence.source_version
        value = "Recorded." if action == "add_note" else 2
        receipt = getattr(client, action)(contract, value, version)
        assert receipt.after.assigned_to_id == 8
        assert receipt.after.relations == relations
        assert set(backend.writes[0]) == (
            {"notes"} if action == "add_note" else {"notes", "status_id"}
        )
