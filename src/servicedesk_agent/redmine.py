"""Narrow Redmine REST adapter with read-back receipts and scoped writes."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import (
    BusinessError,
    Evidence,
    Receipt,
    TaskContract,
    Ticket,
    fingerprint,
)


class RedmineClient:
    def __init__(
        self, settings: RedmineSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
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
        before = self.get_ticket(contract.ticket_id)
        operation_id = fingerprint(
            {"thread": contract.thread_id, "action": action, "payload": payload}
        )
        marker = f"[desk-action:{operation_id}]"
        note = payload.get("notes", f"Service desk status update to {payload.get('status_id')}.")
        journal_note = f"{note}\n\n{marker}"
        existing = self._confirmed_journal(before, journal_note, contract.principal_id)
        if existing:
            if action == "set_status" and before.status_id != payload["status_id"]:
                raise BusinessError(
                    "action_superseded", "The completed action has since changed.", evidence=before
                )
            return Receipt(
                operation_id=operation_id,
                action=action,
                ticket_id=contract.ticket_id,
                before_version=before.evidence.source_version,
                after=before,
                journal_id=existing["id"],
                replayed=True,
            )
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
        try:
            self._request("PUT", f"issues/{contract.ticket_id}.json", json={"issue": update})
        except BusinessError as exc:
            if exc.code != "write_outcome_unknown":
                raise
            # A lost acknowledgement can still be recovered from the persisted journal marker.
            try:
                after = self.get_ticket(contract.ticket_id)
            except BusinessError:
                raise exc from None
            if self._confirmed_journal(after, journal_note, contract.principal_id) is None:
                raise exc from None
        else:
            try:
                after = self.get_ticket(contract.ticket_id)
            except BusinessError as exc:
                raise BusinessError(
                    "write_outcome_unknown", "The write requires a successful read-back."
                ) from exc
        journal = self._confirmed_journal(after, journal_note, contract.principal_id)
        if journal is None or (action == "set_status" and after.status_id != payload["status_id"]):
            raise BusinessError(
                "write_outcome_unknown",
                "Read-back did not confirm the requested action.",
                evidence=after,
            )
        return Receipt(
            operation_id=operation_id,
            action=action,
            ticket_id=contract.ticket_id,
            before_version=before.evidence.source_version,
            after=after,
            journal_id=journal["id"],
        )

    @staticmethod
    def _confirmed_journal(ticket: Ticket, note: str, principal_id: int) -> dict[str, Any] | None:
        return next(
            (
                journal
                for journal in ticket.journals
                if journal.get("notes") == note
                and type(journal.get("id")) is int
                and journal["id"] > 0
                and isinstance(journal.get("user"), dict)
                and type(journal["user"].get("id")) is int
                and journal["user"]["id"] == principal_id
            ),
            None,
        )
