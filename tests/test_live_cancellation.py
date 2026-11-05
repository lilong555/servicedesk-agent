"""Offline checks for the real-service cancellation entry point."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage

from servicedesk_agent.testing import ScriptedChatModel, tool_call

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_live_cancellation.py"


@pytest.fixture(scope="module")
def cancellation():
    spec = importlib.util.spec_from_file_location("check_live_cancellation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Cancellation tests must use offline HTTP/model fixtures.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def access(cancellation, tmp_path):
    path = tmp_path / "access.json"
    cancellation.write_json(
        path,
        {
            "project_id": 1,
            "agent_api_key": "private-agent-key",
            "admin_api_key": "private-admin-key",
        },
    )
    return path


def mock_redmine(monkeypatch, backend, *, identifier="servicedesk-demo"):
    client_type = httpx.Client
    mutations = []

    def handle(request):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            mutations.append({"method": request.method, "path": request.url.path})
        if request.url.path == "/projects/1.json":
            return httpx.Response(200, json={"project": {"id": 1, "identifier": identifier}})
        if request.method == "POST" and request.url.path == "/issues.json":
            assert json.loads(request.content)["issue"]["project_id"] == 1
            return httpx.Response(201, json={"issue": deepcopy(backend.issue)})
        return backend.handle(request)

    def client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handle))
        return client_type(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    return mutations


def test_parser_requires_output_and_appends_environment_files(cancellation):
    with pytest.raises(SystemExit) as raised:
        cancellation.parser().parse_args([])
    assert raised.value.code == 2
    default = cancellation.parser().parse_args(["--output", "results/cancellation"])
    assert default.access == Path(".runtime/redmine-access.json")
    assert default.live is False and default.env_file == []
    args = cancellation.parser().parse_args(
        ["--output", "result", "--env-file", "base.env", "--env-file", "model.env", "--live"]
    )
    assert args.env_file == [Path("base.env"), Path("model.env")]
    assert args.live is True


def test_env_files_override_in_order_without_archiving_credentials(
    cancellation, monkeypatch, tmp_path
):
    first, second = tmp_path / "first.env", tmp_path / "second.env"
    first.write_text("DESK_MODEL=first\nDESK_MODEL_API_KEY=private-model-key\n")
    second.write_text("DESK_MODEL=second\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DESK_MODEL", "original")
    monkeypatch.setenv("DESK_MODEL_API_KEY", "original-key")
    seen = []

    def run(access, output, *, live):
        seen.append((os.environ["DESK_MODEL"], os.environ["DESK_MODEL_API_KEY"], live))
        return {"passed": True}

    monkeypatch.setattr(cancellation, "run_check", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--output",
            "result",
            "--env-file",
            str(first),
            "--env-file",
            str(second),
            "--live",
        ],
    )
    assert cancellation.main() == 0
    assert seen == [("second", "private-model-key", True)]


@pytest.mark.parametrize("kind", ["directory", "file", "dangling_symlink"])
def test_output_refuses_overwrite_before_loading_access(cancellation, tmp_path, kind):
    output = tmp_path / "existing"
    if kind == "directory":
        output.mkdir()
    elif kind == "file":
        output.write_bytes(b"retain")
    else:
        output.symlink_to(tmp_path / "absent")
    with pytest.raises(FileExistsError, match="Output already exists"):
        cancellation.run_check(tmp_path / "missing-access", output)
    if kind == "file":
        assert output.read_bytes() == b"retain"
    elif kind == "dangling_symlink":
        assert output.is_symlink()


@pytest.mark.parametrize(
    "change", [{"url": "https://desk.example"}, {"project_id": True}, {"agent_api_key": ""}]
)
def test_access_validation(cancellation, access, change):
    value = json.loads(access.read_text())
    access.write_text(json.dumps({**value, **change}))
    with pytest.raises(ValueError, match="127.0.0.1:3080"):
        cancellation.load_access(access)


def test_foreign_project_is_rejected_before_ticket_creation(
    cancellation, monkeypatch, desk, access, tmp_path
):
    mutations = mock_redmine(monkeypatch, desk[0], identifier="production")
    with pytest.raises(ValueError, match="isolated servicedesk-demo"):
        cancellation.run_check(access, tmp_path / "output")
    assert mutations == []


@pytest.mark.parametrize("method", ["PUT", "POST", "PATCH", "DELETE"])
def test_mutation_attempt_is_recorded_and_blocked_without_authentication_leak(cancellation, method):
    with cancellation.ReadOnlyAudit() as audit:
        with pytest.raises(RuntimeError, match="attempted a Redmine mutation"):
            audit.handle_request(
                httpx.Request(
                    method,
                    cancellation.URL + "/issues/42.json?api_key=secret-query",
                    headers={"X-Redmine-API-Key": "secret-header"},
                )
            )
        assert audit.requests == [{"method": method, "path": "/issues/42.json"}]


@pytest.fixture
def evidence(cancellation, monkeypatch, desk, access, tmp_path):
    backend = desk[0]
    backend.issue["custom_fields"] = [{"id": 8, "name": "Business owner", "value": "retain"}]
    mutations = mock_redmine(monkeypatch, backend)
    real_run = subprocess.run
    commands = []
    bootstrap = """
import json, os, runpy, socket, sys
import httpx
sys.path.insert(0, os.environ["OFFLINE_TEST_SUPPORT"])
from conftest import RedmineFixture
from servicedesk_agent.config import ModelSettings
backend = RedmineFixture()
backend.issue = json.loads(os.environ["OFFLINE_TEST_ISSUE"])
def handle(request):
    if request.url.path == "/projects/1.json":
        return httpx.Response(200, json={"project": {"id": 1, "identifier": "servicedesk-demo"}})
    return backend.handle(request)
def forbidden(*args, **kwargs):
    raise AssertionError("Offline fixture attempted external I/O")
httpx.HTTPTransport = lambda **kwargs: httpx.MockTransport(handle)
socket.socket.connect = forbidden
socket.create_connection = forbidden
ModelSettings.build = forbidden
script = sys.argv.pop(1)
runpy.run_path(script, run_name="__main__")
"""

    def offline_process(command, **kwargs):
        commands.append(command)
        return real_run(
            [sys.executable, "-c", bootstrap, *command[1:]],
            env={
                **os.environ,
                "OFFLINE_TEST_SUPPORT": str(ROOT / "tests"),
                "OFFLINE_TEST_ISSUE": json.dumps(backend.issue),
            },
            **kwargs,
        )

    monkeypatch.setattr(cancellation.subprocess, "run", offline_process)
    output = tmp_path / "result"
    result = cancellation.run_check(access, output)
    assert mutations == [{"method": "POST", "path": "/issues.json"}]
    assert len(commands) == 2
    assert [command[command.index("--worker") + 1] for command in commands] == ["initial", "resume"]
    assert all("--live" not in command for command in commands)
    return output, result


def test_native_sqlite_two_process_acceptance_and_private_evidence(cancellation, evidence):
    output, result = evidence
    assert result["passed"] and all(result["checks"].values())
    assert result["initial_model_mode"] == result["resume_model_mode"] == "scripted"
    assert result["external_model_requests"] == 0
    initial = json.loads((output / "initial.json").read_text())
    resumed = json.loads((output / "resume.json").read_text())
    assert initial["process_id"] != resumed["process_id"]
    assert initial["usage"]["calls"] == 1
    assert resumed["usage"]["calls"] == 4
    assert initial["checkpoint"]["next"] == ["tools"]
    assert resumed["checkpoint"]["next"] == []
    assert (
        Path(initial["checkpoint"]["checkpoint_path"]).read_bytes().startswith(b"SQLite format 3")
    )
    assert all(set(item) == {"method", "path"} for item in resumed["redmine_requests"])
    assert all(item["method"] == "GET" for item in resumed["redmine_requests"])
    source = json.loads((output / "source.json").read_text())
    assert source["entrypoint_sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    assert "deepagents" in {name.lower() for name in source["packages"]}
    with tarfile.open(output / "source.tar.gz") as archive:
        assert "src/servicedesk_agent/session.py" in archive.getnames()
    assert Path(result["resume_trace"]).is_file()
    assert json.loads(Path(result["resume_trace"]).read_text())["usage"] == resumed["usage"]
    for path in output.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        if path.suffix == ".json":
            content = path.read_text()
            assert "private-agent-key" not in content and "private-admin-key" not in content


def test_acceptance_detects_state_priority_and_usage_regressions(cancellation, evidence):
    output, _ = evidence
    initial = json.loads((output / "initial.json").read_text())
    resumed = json.loads((output / "resume.json").read_text())
    before = json.loads((output / "before.json").read_text())
    after = json.loads((output / "after.json").read_text())
    source = json.loads((output / "source.json").read_text())
    changed = deepcopy(resumed)
    changed["process_id"] = initial["process_id"]
    changed["start_checkpoint"]["checkpoint_id"] = "different-checkpoint"
    changed["redmine_requests"].append({"method": "PUT", "path": "/issues/42.json"})
    changed["model_requests"][0] = []
    changed["checkpoint"]["pending_actions"] = []
    changed["checkpoint"]["contract"]["user_constraints"] = []
    changed["checkpoint"]["messages"] = [
        item
        for item in changed["checkpoint"]["messages"]
        if item["type"] != "tool" or item["data"]["tool_call_id"] != cancellation.OLD_CALL
    ]
    changed["model_mode"] = "live"
    changed["usage"]["unknown_usage_calls"] = 1
    after["custom_fields"][0]["value"] = "modified"
    checks = cancellation.acceptance(initial, changed, before, after, source)
    for name in (
        "separate_processes",
        "same_native_checkpoint",
        "zero_resume_mutations",
        "latest_user_in_first_request",
        "pending_preserved",
        "constraints_preserved",
        "old_call_superseded",
        "live_usage_retained",
        "complete_ticket_unchanged",
    ):
        assert checks[name] is False, name
    after = deepcopy(before)
    after["journals"].append({"id": 100, "notes": "Unexpected note"})
    assert not cancellation.acceptance(initial, resumed, before, after, source)[
        "complete_ticket_unchanged"
    ]


def test_live_build_only_occurs_on_resume_and_usage_is_retained(
    cancellation, monkeypatch, desk, access, tmp_path
):
    mock_redmine(monkeypatch, desk[0])
    output = tmp_path / "live-fixture"
    output.mkdir()
    cancellation.write_json(output / "run.json", {"ticket_id": 42, "session_id": "live-fixture"})
    built, closed = [], []
    usage = {"input_tokens": 25, "output_tokens": 5, "total_tokens": 30}

    class FixtureModel(ScriptedChatModel):
        @property
        def root_client(self):
            return SimpleNamespace(close=lambda: closed.append(True))

    def build(settings):
        built.append(True)
        return FixtureModel(
            responses=[
                response.model_copy(update={"usage_metadata": usage})
                for response in [
                    tool_call("read_handoff", {}, "live-handoff"),
                    tool_call("read_ticket", {}, "live-read"),
                    AIMessage(content="Paused; outstanding actions retained."),
                ]
            ]
        )

    monkeypatch.setattr(cancellation.ModelSettings, "build", build)
    initial = cancellation.worker(access, output, "initial", live=True)
    assert initial["model_mode"] == "scripted" and built == []
    resumed = cancellation.worker(access, output, "resume", live=True)
    assert resumed["model_mode"] == "live" and built == [True] and closed == [True]
    assert resumed["usage"]["calls"] == 3
    assert resumed["usage"]["input_tokens"] == 75
    assert resumed["usage"]["output_tokens"] == 15
    assert resumed["usage"]["unknown_usage_calls"] == 0
    assert all(call["usage"] == usage for call in resumed["model_calls"])
