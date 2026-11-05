from __future__ import annotations

import copy
import json

import httpx
import pytest
from langgraph.store.memory import InMemoryStore

from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.redmine import RedmineClient


class RedmineFixture:
    """A REST contract fixture used only by unit tests."""

    def __init__(self):
        self.statuses = [
            {"id": 1, "name": "New", "is_closed": False},
            {"id": 2, "name": "In Progress", "is_closed": False},
            {"id": 5, "name": "Closed", "is_closed": True},
        ]
        self.issue = {
            "id": 42,
            "project": {"id": 1, "name": "Service desk"},
            "subject": "VPN connection failure",
            "description": "Investigate intermittent VPN disconnects.",
            "status": self.statuses[0],
            "assigned_to": {"id": 7, "name": "Original owner"},
            "updated_on": "2026-11-05T00:00:00Z",
            "journals": [],
            "relations": [],
            "allowed_statuses": self.statuses,
        }
        self.principal_id = 1
        self.requests = []
        self.lose_ack = False
        self.apply_writes = True
        self.read_back_fails = False
        self.http_status = None
        self.writes = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.http_status is not None:
            return httpx.Response(self.http_status)
        path = request.url.path
        if path == "/users/current.json":
            return httpx.Response(200, json={"user": {"id": self.principal_id}})
        if path == "/issue_statuses.json":
            return httpx.Response(200, json={"issue_statuses": self.statuses})
        if path == "/issues/42.json" and request.method == "GET":
            if self.read_back_fails and self.writes:
                return httpx.Response(503)
            return httpx.Response(200, json={"issue": copy.deepcopy(self.issue)})
        if path == "/issues/42.json" and request.method == "PUT":
            data = json.loads(request.content)["issue"]
            self.writes.append(data)
            if self.apply_writes:
                details = []
                if "status_id" in data:
                    if self.issue["status"]["id"] != data["status_id"]:
                        details.append(
                            {
                                "property": "attr",
                                "name": "status_id",
                                "old_value": str(self.issue["status"]["id"]),
                                "new_value": str(data["status_id"]),
                            }
                        )
                    self.issue["status"] = next(
                        item for item in self.statuses if item["id"] == data["status_id"]
                    )
                self.issue["journals"].append(
                    {
                        "id": len(self.issue["journals"]) + 1,
                        "notes": data["notes"],
                        "user": {"id": self.principal_id},
                        "details": details,
                    }
                )
            if self.lose_ack:
                raise httpx.ReadTimeout("fixture lost acknowledgement", request=request)
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture
def desk():
    backend = RedmineFixture()
    settings = RedmineSettings(url="https://desk.test", api_key="test-key")
    with RedmineClient(
        settings, store=InMemoryStore(), transport=httpx.MockTransport(backend.handle)
    ) as client:
        yield backend, client


@pytest.fixture
def contract():
    return TaskContract(
        service_url="https://desk.test",
        project_id=1,
        principal_id=1,
        ticket_id=42,
        session_id="vpn-investigation",
        allow_notes=True,
        allowed_status_ids=(2, 5),
        user_constraints=("Investigate and add notes. Keep this ticket open.",),
    )
