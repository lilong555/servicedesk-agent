"""Narrow Redmine REST adapter with read-back receipts and scoped writes."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx
from langgraph.store.base import BaseStore

from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import (
    BusinessError,
    Evidence,
    Receipt,
    TaskContract,
    Ticket,
    fingerprint,
)
from servicedesk_agent.operations import OperationRecords


class RedmineClient:
    def __init__(
        self,
        settings: RedmineSettings,
        *,
        store: BaseStore | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._operations = OperationRecords(store) if store is not None else None
        self._http = httpx.Client(
            base_url=settings.url + "/",
            headers={"X-Redmine-API-Key": settings.api_key.get_secret_value()},
            timeout=settings.timeout,
            follow_redirects=False,
            trust_env=urlsplit(settings.url).hostname not in {"127.0.0.1", "localhost", "::1"},
            transport=transport,
        )

    def __enter__(self) -> RedmineClient:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            code = "write_outcome_unknown" if method != "GET" else "service_unavailable"
            raise BusinessError(
                code, "Read the ticket again to establish the current state."
            ) from exc
        if response.status_code in {401, 403}:
            raise BusinessError(
                "access_denied", "Redmine denied access for the configured API user."
            )
        if response.status_code == 404:
            raise BusinessError("not_found", "The requested Redmine record is unavailable.")
        if response.status_code in {409, 422}:
            raise BusinessError("business_rejection", "Redmine rejected the requested transition.")
        if not response.is_success:
            code = "write_outcome_unknown" if method != "GET" else "service_unavailable"
            raise BusinessError(code, f"Redmine returned HTTP {response.status_code}.")
        if not response.content:
            return {}
        try:
            result = response.json()
        except ValueError as exc:
            code = "invalid_response" if method == "GET" else "write_outcome_unknown"
            raise BusinessError(code, "Redmine returned a non-JSON response.") from exc
        if not isinstance(result, dict):
            code = "invalid_response" if method == "GET" else "write_outcome_unknown"
            raise BusinessError(code, "Redmine returned an unexpected response shape.")
        return result

    def current_user(self) -> dict[str, Any]:
        user = self._request("GET", "users/current.json").get("user")
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            raise BusinessError("invalid_response", "Redmine returned an invalid API user.")
        return user

    def statuses(self) -> list[dict[str, Any]]:
        statuses = self._request("GET", "issue_statuses.json").get("issue_statuses")
        if not isinstance(statuses, list) or any(
            not isinstance(item, dict) or type(item.get("id")) is not int for item in statuses
        ):
            raise BusinessError("invalid_response", "Redmine returned invalid status records.")
        return statuses

    def get_ticket(self, ticket_id: int) -> Ticket:
        if ticket_id <= 0:
            raise BusinessError("invalid_ticket", "A positive ticket ID is required.")
        response = self._request(
            "GET",
            f"issues/{ticket_id}.json",
            params={"include": "journals,relations,allowed_statuses"},
        )
        try:
            return self._ticket_from_response(ticket_id, response["issue"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise BusinessError("invalid_response", "Redmine returned an invalid ticket.") from exc

    def _ticket_from_response(self, ticket_id: int, raw: dict[str, Any]) -> Ticket:
        if raw["id"] != ticket_id or raw["project"]["id"] != self.settings.project_id:
            raise BusinessError("scope_mismatch", "The ticket is outside the configured project.")
        status = raw["status"]
        if "is_closed" not in status:
            status = next((item for item in self.statuses() if item["id"] == status["id"]), {})
        if type(status.get("is_closed")) is not bool:
            raise BusinessError(
                "unknown_status", "The service did not identify the status semantics."
            )
        assignee = raw.get("assigned_to") or {}
        uri = f"{self.settings.url}/issues/{ticket_id}"
        return Ticket(
            id=raw["id"],
            project_id=raw["project"]["id"],
            subject=raw["subject"],
            description=raw.get("description") or "",
            status_id=raw["status"]["id"],
            status_name=raw["status"]["name"],
            is_closed=status["is_closed"],
            assigned_to_id=assignee.get("id"),
            assigned_to_name=assignee.get("name"),
            allowed_statuses=raw.get("allowed_statuses", []),
            relations=raw.get("relations", []),
            journals=raw.get("journals", []),
            evidence=Evidence(
                source_uri=uri,
                source_kind="ticket",
                entity_scope=f"{self.settings.url}/projects/{self.settings.project_id}",
                source_version=fingerprint(raw),
                content=raw,
            ),
        )

    def _check_contract(self, contract: TaskContract) -> None:
        if (
            contract.service_url != self.settings.url
            or contract.project_id != self.settings.project_id
        ):
            raise BusinessError(
                "scope_mismatch", "The contract belongs to another Redmine service."
            )
        if self.current_user()["id"] != contract.principal_id:
            raise BusinessError("identity_changed", "Resume with the original Redmine API user.")

    def add_note(self, contract: TaskContract, note: str, expected_version: str) -> Receipt:
        self._check_contract(contract)
        if not contract.allow_notes:
            raise BusinessError("write_not_authorized", "This task is authorized for read access.")
        note = note.strip()
        if not note or len(note) > 8000:
            raise BusinessError("invalid_note", "Use a nonempty note of at most 8000 characters.")
        return self._write(
            contract, action="add_note", payload={"notes": note}, expected_version=expected_version
        )

    def set_status(self, contract: TaskContract, status_id: int, expected_version: str) -> Receipt:
        self._check_contract(contract)
        if status_id not in contract.allowed_status_ids:
            raise BusinessError("write_not_authorized", "The status is outside this task's grant.")
        target = next((item for item in self.statuses() if item["id"] == status_id), None)
        if target is None or type(target.get("is_closed")) is not bool:
            raise BusinessError("unknown_status", "The target status is unavailable.")
        if target["is_closed"] and not contract.allow_close:
            raise BusinessError("closure_not_authorized", "This task must leave the ticket open.")
        return self._write(
            contract,
            action="set_status",
            payload={"status_id": status_id},
            expected_version=expected_version,
        )

    def _write(
        self,
        contract: TaskContract,
        *,
        action: str,
        payload: dict[str, Any],
        expected_version: str,
    ) -> Receipt:
        operations = self._require_operations()
        with operations.locked(contract.thread_id):
            return self._write_locked(contract, action, payload, expected_version)

    def _require_operations(self) -> OperationRecords:
        if self._operations is None:
            raise BusinessError(
                "operation_store_required",
                "Redmine writes require a persistent Store; use InMemoryStore only in tests.",
            )
        return self._operations

    def reconcile_operations(
        self, contract: TaskContract, *, include_completed: bool = False
    ) -> list[Receipt]:
        """Reconcile pending writes and optionally replay currently verifiable receipts."""
        self._check_contract(contract)
        operations = self._require_operations()
        with operations.locked(contract.thread_id):
            records = operations.load(contract.thread_id)
            pending = records["pending"]
            candidates = [pending] if pending is not None else []
            if include_completed:
                candidates.extend(
                    receipt
                    for operation_id, receipt in records["receipts"].items()
                    if pending is None or operation_id != pending["operation_id"]
                )
            if not candidates:
                return []
            after = (
                self._read_for_reconciliation(contract.ticket_id)
                if pending is not None
                else self.get_ticket(contract.ticket_id)
            )
            receipts = []
            for intent in candidates:
                try:
                    receipt = self._settle(contract, records, intent, after, replayed=True)
                except BusinessError as exc:
                    if include_completed and exc.code in {
                        "action_superseded",
                        "action_evidence_changed",
                    }:
                        continue
                    raise
                receipts.append(receipt)
            return receipts

    @staticmethod
    def _uncertain(ticket: Ticket | None = None) -> BusinessError:
        return BusinessError(
            "operation_uncertain",
            "The session has an unresolved Redmine operation. Reconcile its journal before "
            "submitting further writes; automatic resubmission is blocked.",
            evidence=ticket,
        )

    def _read_for_reconciliation(self, ticket_id: int) -> Ticket:
        try:
            return self.get_ticket(ticket_id)
        except BusinessError as exc:
            raise self._uncertain() from exc

    def _write_locked(
        self, contract: TaskContract, action: str, payload: dict[str, Any], expected_version: str
    ) -> Receipt:
        operations = self._require_operations()
        records = operations.load(contract.thread_id)
        operation_id = fingerprint(
            {"thread": contract.thread_id, "action": action, "payload": payload}
        )
        marker = f"[desk-action:{operation_id}]"
        note = payload.get("notes", f"Service desk status update to {payload.get('status_id')}.")
        journal_note = f"{note}\n\n{marker}"
        pending = records["pending"]
        if pending is not None:
            before = self._read_for_reconciliation(contract.ticket_id)
            try:
                recovered = self._settle(contract, records, pending, before, replayed=True)
            except BusinessError as exc:
                if pending["operation_id"] == operation_id or exc.code not in {
                    "action_superseded",
                    "action_evidence_changed",
                }:
                    raise
            else:
                if pending["operation_id"] == operation_id:
                    return recovered
        else:
            before = self.get_ticket(contract.ticket_id)
        completed = records["receipts"].get(operation_id)
        if completed is not None:
            return self._settle(contract, records, completed, before, replayed=True)
        intent = {
            "operation_id": operation_id,
            "action": action,
            "note_hash": fingerprint(journal_note),
            "target_status": payload.get("status_id"),
            "before_version": before.evidence.source_version,
            "before_status": before.status_id,
            "journal_floor": max(
                (
                    journal["id"]
                    for journal in before.journals
                    if type(journal.get("id")) is int and journal["id"] > 0
                ),
                default=0,
            ),
            "journal_id": None,
        }
        if self._marked_journals(before, marker):
            # A marker without our durable intent needs review, even when its body matches.
            records["pending"] = intent
            operations.save(contract.thread_id, records)
            raise self._uncertain(before)
        if before.evidence.source_version != expected_version:
            raise BusinessError(
                "stale_observation", "Review the current ticket before writing.", evidence=before
            )
        if action == "set_status":
            allowed = {item["id"] for item in before.allowed_statuses}
            if payload["status_id"] not in allowed:
                raise BusinessError(
                    "transition_denied", "The current Redmine workflow excludes this transition."
                )
        # Only intended fields are submitted; old assignee and dependency fields stay untouched.
        update = {**payload, "notes": journal_note}
        records["pending"] = intent
        operations.save(contract.thread_id, records)
        try:
            self._request("PUT", f"issues/{contract.ticket_id}.json", json={"issue": update})
        except BusinessError as exc:
            if exc.code != "write_outcome_unknown":
                records["pending"] = None
                operations.save(contract.thread_id, records)
                raise
        after = self._read_for_reconciliation(contract.ticket_id)
        return self._settle(contract, records, intent, after, replayed=False)

    def _settle(
        self,
        contract: TaskContract,
        records: dict,
        intent: dict,
        after: Ticket,
        *,
        replayed: bool,
    ) -> Receipt:
        operations = self._require_operations()
        journal = self._confirmed_journal(after, intent, contract.principal_id)
        pending = records["pending"]
        owns_pending = pending is not None and pending["operation_id"] == intent["operation_id"]
        if journal is None:
            if intent["journal_id"] is not None:
                if owns_pending:
                    records["receipts"][intent["operation_id"]] = intent
                    records["pending"] = None
                    operations.save(contract.thread_id, records)
                raise BusinessError(
                    "action_evidence_changed",
                    "The completed action's journal evidence has changed.",
                    evidence=after,
                )
            records["pending"] = intent
            operations.save(contract.thread_id, records)
            raise self._uncertain(after)
        if intent["journal_id"] is None or owns_pending:
            # Journal evidence settles submission even when later actions changed the status.
            records["receipts"][intent["operation_id"]] = {**intent, "journal_id": journal["id"]}
            if owns_pending:
                records["pending"] = None
            operations.save(contract.thread_id, records)
        if intent["action"] == "set_status" and after.status_id != intent["target_status"]:
            raise BusinessError(
                "action_superseded", "The completed action has since changed.", evidence=after
            )
        return Receipt(
            operation_id=intent["operation_id"],
            action=intent["action"],
            ticket_id=contract.ticket_id,
            before_version=intent["before_version"],
            after=after,
            journal_id=journal["id"],
            replayed=replayed,
        )

    @staticmethod
    def _marked_journals(ticket: Ticket, marker: str) -> list[dict[str, Any]]:
        return [
            journal
            for journal in ticket.journals
            if isinstance(journal.get("notes"), str) and marker in journal["notes"]
        ]

    @classmethod
    def _confirmed_journal(
        cls, ticket: Ticket, intent: dict, principal_id: int
    ) -> dict[str, Any] | None:
        candidates = cls._marked_journals(ticket, f"[desk-action:{intent['operation_id']}]")
        if len(candidates) != 1:
            return None
        journal = candidates[0]
        author = journal.get("user")
        editor = journal.get("updated_by")
        if (
            fingerprint(journal["notes"]) != intent["note_hash"]
            or type(journal.get("id")) is not int
            or journal["id"] <= intent["journal_floor"]
            or (intent["journal_id"] is not None and journal["id"] != intent["journal_id"])
            or not isinstance(author, dict)
            or type(author.get("id")) is not int
            or author["id"] != principal_id
            or (
                editor is not None
                and (
                    not isinstance(editor, dict)
                    or type(editor.get("id")) is not int
                    or editor["id"] != principal_id
                )
            )
        ):
            return None
        if intent["action"] == "set_status":
            details = journal.get("details")
            if not isinstance(details, list) or any(
                not isinstance(detail, dict) for detail in details
            ):
                return None
            transitions = [
                detail
                for detail in details
                if detail.get("property") == "attr" and detail.get("name") == "status_id"
            ]
            # Redmine omits status details for an authorized, already-current status.
            if intent["before_status"] == intent["target_status"] and not transitions:
                return journal
            if (
                len(transitions) != 1
                or transitions[0].get("new_value") != str(intent["target_status"])
                or transitions[0].get("old_value") != str(intent["before_status"])
            ):
                return None
        return journal
