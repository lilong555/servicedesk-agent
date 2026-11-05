"""Business records emitted by trusted adapters."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_uri: str
    source_kind: Literal["ticket", "policy", "user", "receipt"]
    entity_scope: str
    source_version: str
    observed_at: datetime = Field(default_factory=utc_now)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: Literal["observed", "historical", "superseded"] = "observed"
    content: dict[str, Any] | str


class Ticket(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    project_id: int
    subject: str
    description: str = ""
    status_id: int
    status_name: str
    is_closed: bool
    assigned_to_id: int | None = None
    assigned_to_name: str | None = None
    allowed_statuses: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    journals: list[dict[str, Any]] = Field(default_factory=list)
    evidence: Evidence


class TaskContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    service_url: str
    project_id: int = Field(gt=0)
    principal_id: int = Field(gt=0)
    ticket_id: int = Field(gt=0)
    session_id: str = Field(min_length=1, max_length=128)
    variant: Literal["A", "B"] = "A"
    allow_notes: bool = False
    allowed_status_ids: tuple[int, ...] = ()
    allow_close: bool = False
    user_constraints: tuple[str, ...] = ()

    @field_validator("allowed_status_ids")
    @classmethod
    def positive_status_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(item <= 0 for item in value):
            raise ValueError("Status IDs must be positive.")
        return tuple(sorted(set(value)))

    @property
    def scope(self) -> str:
        return (
            f"{self.service_url}/projects/{self.project_id}"
            f"/principals/{self.principal_id}/issues/{self.ticket_id}"
        )

    @property
    def thread_id(self) -> str:
        return fingerprint({"scope": self.scope, "session_id": self.session_id})


class Receipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    action: Literal["add_note", "set_status"]
    ticket_id: int
    before_version: str
    after: Ticket
    journal_id: int
    verified: bool = True
    replayed: bool = False
    observed_at: datetime = Field(default_factory=utc_now)


class BusinessError(Exception):
    def __init__(self, code: str, message: str, *, evidence: Ticket | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": self.code, "message": str(self)}
        if self.evidence:
            result["current_ticket"] = self.evidence.model_dump(mode="json")
        return result
