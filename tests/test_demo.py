from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import httpx
import pytest
from conftest import RedmineFixture

from servicedesk_agent import demo
from servicedesk_agent.config import ModelSettings
from servicedesk_agent.domain import Ticket

STAGES = (
    "01-created",
    "02-initial-completed",
    "03-external-changes",
    "04-resume-completed",
)
CHECKS = {
    "separate_process_resume",
    "same_native_checkpoint",
    "current_owner_preserved",
    "ticket_kept_open",
    "keep_open_constraint_preserved",
    "pending_action_preserved",
    "two_verified_actions",
    "current_policy_read_on_resume",
    "current_policy_and_owner_in_new_note",
}


@pytest.fixture
def demo_desk(tmp_path, monkeypatch):
    backend = RedmineFixture()
    access = {
        "admin_api_key": "synthetic-admin-secret",
        "agent_api_key": "synthetic-agent-secret",
        "project_id": 1,
        "first_owner_id": 7,
        "second_owner_id": 8,
        "admin_password": "synthetic-login-secret",
    }
    access_path = tmp_path / "access.json"
    demo._json(access_path, access)
    fixture = SimpleNamespace(
        backend=backend,
        access=access_path,
        output=tmp_path / "output",
        events=[],
        snapshots=[],
        observations=[],
        policies=[],
        worker_active=False,
        wrong_owner_at=None,
    )

    def wrong_owner():
        backend.issue["assigned_to"] = {"id": 9, "name": "Unexpected owner"}

    def handle(request):
        path = request.url.path
        if path == "/projects/1.json":
            backend.requests.append(request)
            return httpx.Response(200, json={"project": {"identifier": "servicedesk-demo"}})
        if path == "/issues.json" and request.method == "POST":
            backend.requests.append(request)
            issue = json.loads(request.content)["issue"]
            backend.issue.update(
                subject=issue["subject"],
                description=issue["description"],
                assigned_to={"id": issue["assigned_to_id"], "name": "Original owner"},
            )
            if fixture.wrong_owner_at == "created":
                wrong_owner()
            fixture.events.append("create")
            return httpx.Response(201, json={"issue": copy.deepcopy(backend.issue)})
        if path == "/issues/42.json" and request.method == "PUT":
            issue = json.loads(request.content)["issue"]
            if "assigned_to_id" in issue:
                assert not fixture.worker_active
                backend.issue["assigned_to"] = {
                    "id": issue["assigned_to_id"],
                    "name": "Current owner",
                }
                if fixture.wrong_owner_at == "external":
                    wrong_owner()
                fixture.events.append("external")
            backend.issue["updated_on"] = f"2026-11-05T00:00:{len(backend.writes) + 1:02d}Z"
        return backend.handle(request)

    http_client = httpx.Client

    def mock_http(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return http_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", mock_http)
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("Unexpected network access"))
    )
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setattr(
        ModelSettings, "build", Mock(side_effect=AssertionError("Unexpected model construction"))
    )
    get_ticket = demo.RedmineClient.get_ticket

    def observe(client, ticket_id):
        ticket = get_ticket(client, ticket_id)
        if client._operations is None:
            fixture.observations.append(ticket.model_dump(mode="json"))
        return ticket

    monkeypatch.setattr(demo.RedmineClient, "get_ticket", observe)
    write_json = demo._json

    def record_json(path, value):
        write_json(path, value)
        if path.parent.name == "stages":
            assert not fixture.worker_active
            fixture.events.append(value["stage"])
            fixture.snapshots.append(value)
            fixture.policies.append(
                json.loads((fixture.output / "state" / "policies.json").read_text())
            )

    monkeypatch.setattr(demo, "_json", record_json)

    def sleep(seconds):
        assert not fixture.worker_active
        assert len(fixture.snapshots) in (1, 2, 3)
        assert not (fixture.output / "summary.json").exists()
        snapshot = fixture.snapshots[-1]
        assert (
            json.loads((fixture.output / "stages" / f"{snapshot['stage']}.json").read_text())
            == snapshot
        )
        fixture.events.append(("sleep", seconds))

    fixture.sleep = Mock(side_effect=sleep)
    monkeypatch.setattr(demo.time, "sleep", fixture.sleep)

    def phase(command, **kwargs):
        name = command[3]
        assert command[:3] == [sys.executable, "-m", "servicedesk_agent.demo"]
        assert command[4:10] == [
            "--access",
            str(access_path),
            "--state",
            str(fixture.output / "state"),
            "--ticket",
            "42",
        ]
        assert command[10:] in ([], ["--live"])
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": 360 if "--live" in command else 90,
        }
        fixture.events.append(f"{name}-start")
        fixture.worker_active = True
        try:
            result = demo.worker(access_path, fixture.output / "state", 42, name, live=False)
        finally:
            fixture.worker_active = False
        result["process_id"] = 101 if name == "initial" else 102
        if fixture.wrong_owner_at == name:
            wrong_owner()
        fixture.events.append(f"{name}-end")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(result), stderr="")

    fixture.process = Mock(side_effect=phase)
    monkeypatch.setattr(demo.subprocess, "run", fixture.process)
    return fixture


@pytest.mark.parametrize("delay", [None, 0, 0.25, 30])
def test_demo_observes_native_snapshots_at_fixture_boundaries(demo_desk, delay):
    fixture = demo_desk
    options = {} if delay is None else {"stage_delay": delay}
    summary = demo.run_demo(fixture.access, fixture.output, **options)
    events = [
        "create",
        STAGES[0],
        "initial-start",
        "initial-end",
        STAGES[1],
        "external",
        STAGES[2],
        "resume-start",
        "resume-end",
        STAGES[3],
    ]
    if delay:
        for stage in STAGES[:3]:
            events.insert(events.index(stage) + 1, ("sleep", delay))
        assert fixture.sleep.call_args_list == [call(delay)] * 3
    else:
        fixture.sleep.assert_not_called()
    assert fixture.events == events
    assert fixture.process.call_count == 2
    assert summary["passed"] is True
    assert summary["checks"] == dict.fromkeys(CHECKS, True)
    assert summary["external_model_requests"] == 0
    assert summary["kind"] == "scripted_framework_and_real_redmine"
    assert summary["model"] == "scripted-fixture"
    assert summary["current_owner"] == "Current owner"
    assert summary["current_status"] == "New"
    assert summary["results_path"] == str(fixture.output)
    assert all(Path(summary[key]).is_file() for key in ("initial_trace", "resumed_trace"))
    assert summary == json.loads((fixture.output / "summary.json").read_text())
    assert list(summary["stages"]) == list(STAGES)
    assert len(fixture.observations) == 4
    timestamps = []
    for index, (name, owner_id, policy_version, journals) in enumerate(
        zip(STAGES, (7, 7, 8, 8), (1, 1, 2, 2), (0, 1, 2, 3), strict=True)
    ):
        path = Path(summary["stages"][name])
        assert path == fixture.output / "stages" / f"{name}.json"
        stage = json.loads(path.read_text())
        assert set(stage) == {"schema_version", "stage", "observed_at", "ticket", "policy_version"}
        assert stage["schema_version"] == 1
        assert stage["stage"] == name
        assert stage["policy_version"] == policy_version
        assert stage["ticket"] == fixture.observations[index]
        ticket = Ticket.model_validate(stage["ticket"])
        assert ticket.model_dump(mode="json") == stage["ticket"]
        assert ticket.assigned_to_id == owner_id
        assert len(ticket.journals) == journals
        assert not ticket.is_closed
        observed_at = datetime.fromisoformat(stage["observed_at"])
        assert observed_at == ticket.evidence.observed_at
        assert observed_at.utcoffset().total_seconds() == 0
        timestamps.append(observed_at)
        policies = fixture.policies[index]
        assert int(policies[-1]["version"]) == policy_version
        assert policies[-1]["valid_to"] is None
        if policy_version == 2:
            assert policies[0]["valid_to"] == policies[1]["valid_from"]
            assert timestamps[1] <= datetime.fromisoformat(policies[1]["valid_from"]) <= observed_at
        assert "synthetic-" not in path.read_text()
    assert timestamps == sorted(timestamps)
    assert (fixture.output / "stages").stat().st_mode & 0o777 == 0o700
    for path in fixture.output.rglob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(fixture.output.rglob("*.tmp"))

    run = json.loads((fixture.output / "run.json").read_text())
    assert run["schema_version"] == 1
    assert run["live"] is False
    assert run["model_mode"] == "scripted"
    assert run["model"] == "scripted-fixture"
    assert run["demo"] == {
        "stage_delay": delay or 0.0,
        "service_url": "http://127.0.0.1:3080",
        "project_id": 1,
        "first_owner_id": 7,
        "second_owner_id": 8,
    }
    assert run["stages"] == summary["stages"]
    assert datetime.fromisoformat(run["started_at"]) <= timestamps[0]
    assert "synthetic-" not in json.dumps(run)
    provenance = run["provenance"]
    archive_path = fixture.output / provenance["source_archive"]
    assert archive_path.name == "source.tar.gz"
    assert archive_path.stat().st_mode & 0o777 == 0o600
    assert (
        hashlib.sha256(archive_path.read_bytes()).hexdigest() == provenance["source_archive_sha256"]
    )
    with tarfile.open(archive_path) as archive:
        assert not any(".runtime" in name for name in archive.getnames())
        for name, expected_hash in provenance["files"].items():
            assert hashlib.sha256(archive.extractfile(name).read()).hexdigest() == expected_hash


def test_live_manifest_uses_existing_public_model_settings(demo_desk, monkeypatch):
    settings = ModelSettings(
        name="test-model", base_url="https://model.test/v1", api_key="synthetic-model-secret"
    )
    monkeypatch.setattr(ModelSettings, "from_env", Mock(return_value=settings))
    summary = demo.run_demo(demo_desk.access, demo_desk.output, live=True)
    run = json.loads((demo_desk.output / "run.json").read_text())
    assert run["live"] is True
    assert run["model_mode"] == "live"
    assert run["model"] == settings.model_dump(mode="json", exclude={"api_key"})
    assert "api_key" not in json.dumps(run)
    assert "synthetic-" not in json.dumps(run)
    assert summary["model"] == "test-model"
    assert all(item.args[0][-1] == "--live" for item in demo_desk.process.call_args_list)
    ModelSettings.build.assert_not_called()


@pytest.mark.parametrize(
    "delay", [True, False, -0.01, 30.01, float("nan"), float("inf"), -float("inf"), None, "1"]
)
@pytest.mark.parametrize("live", [False, True])
def test_invalid_delay_is_rejected_before_side_effects(tmp_path, monkeypatch, delay, live):
    mocks = [
        (demo, "capture_source"),
        (demo.httpx, "Client"),
        (demo, "RedmineClient"),
        (demo.subprocess, "run"),
        (demo.time, "sleep"),
        (ModelSettings, "from_env"),
        (Path, "read_text"),
        (Path, "mkdir"),
    ]
    for target, name in mocks:
        monkeypatch.setattr(
            target, name, Mock(side_effect=AssertionError("Unexpected side effect"))
        )
    with pytest.raises(ValueError, match="stage_delay"):
        demo.run_demo(tmp_path / "missing.json", tmp_path / "output", live=live, stage_delay=delay)
    assert not (tmp_path / "output").exists()
    for target, name in mocks:
        getattr(target, name).assert_not_called()


@pytest.mark.parametrize(
    ("phase", "stage", "processes"), [("created", 0, 0), ("initial", 1, 1), ("external", 2, 1)]
)
def test_owner_mismatch_retains_actual_snapshot(demo_desk, phase, stage, processes):
    demo_desk.wrong_owner_at = phase
    with pytest.raises(AssertionError, match=f"The {STAGES[stage]} ticket owner differs"):
        demo.run_demo(demo_desk.access, demo_desk.output)
    actual = json.loads((demo_desk.output / "stages" / f"{STAGES[stage]}.json").read_text())
    assert actual["ticket"]["assigned_to_id"] == 9
    assert demo_desk.process.call_count == processes
    assert not (demo_desk.output / "summary.json").exists()


def test_final_owner_mismatch_preserves_existing_summary_check(demo_desk):
    demo_desk.wrong_owner_at = "resume"
    with pytest.raises(AssertionError, match="handoff verification failed"):
        demo.run_demo(demo_desk.access, demo_desk.output)
    summary = json.loads((demo_desk.output / "summary.json").read_text())
    assert set(summary["checks"]) == CHECKS
    assert summary["checks"]["current_owner_preserved"] is False
    final = json.loads(Path(summary["stages"][STAGES[-1]]).read_text())
    assert final["ticket"]["assigned_to_id"] == 9
    assert summary["current_owner"] == final["ticket"]["assigned_to_name"]


def test_json_publishes_complete_private_file_atomically(tmp_path, monkeypatch):
    path = tmp_path / "stage.json"
    path.write_text('{"old": true}')
    path.chmod(0o644)
    replace = os.replace

    def publish(source, destination):
        source = Path(source)
        assert source.parent == path.parent
        assert source != path
        assert source.stat().st_mode & 0o777 == 0o600
        assert json.loads(source.read_text()) == {"new": True}
        assert json.loads(path.read_text()) == {"old": True}
        replace(source, destination)

    publish_mock = Mock(side_effect=publish)
    monkeypatch.setattr(demo.os, "replace", publish_mock)
    demo._json(path, {"new": True})
    publish_mock.assert_called_once()
    assert json.loads(path.read_text()) == {"new": True}
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["serialization", "replace"])
def test_failed_json_write_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch, failure):
    path = tmp_path / "stage.json"
    demo._json(path, {"old": True})
    value = {"new": object()} if failure == "serialization" else {"new": True}
    if failure == "replace":
        monkeypatch.setattr(demo.os, "replace", Mock(side_effect=OSError("Replace failed")))
    with pytest.raises(TypeError if failure == "serialization" else OSError):
        demo._json(path, value)
    assert json.loads(path.read_text()) == {"old": True}
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]
