"""Offline acceptance of the isolated multi-process revocation entry point."""

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
from conftest import RedmineFixture
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from servicedesk_agent.testing import ScriptedChatModel, tool_call

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_live_revocation.py"


@pytest.fixture(scope="module")
def revocation():
    with pytest.MonkeyPatch.context() as patcher:
        patcher.syspath_prepend(str(SCRIPT.parent))
        spec = importlib.util.spec_from_file_location("check_live_revocation", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Revocation acceptance tests require offline HTTP and model fixtures.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def mock_service(patcher, backend, *, identifier="servicedesk-demo"):
    client_type = httpx.Client
    mutations = []

    def handle(request):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            mutations.append({"method": request.method, "path": request.url.path})
        if request.url.path == "/projects/1.json":
            return httpx.Response(200, json={"project": {"id": 1, "identifier": identifier}})
        if request.method == "POST" and request.url.path == "/issues.json":
            data = json.loads(request.content)["issue"]
            assert data["project_id"] == 1
            backend.issue.update({key: data[key] for key in ("subject", "description")})
            return httpx.Response(201, json={"issue": deepcopy(backend.issue)})
        return backend.handle(request)

    def client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handle))
        return client_type(*args, **kwargs)

    patcher.setattr(httpx, "Client", client)
    patcher.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    return mutations


def make_access(revocation, root):
    path = root / "access.json"
    revocation.common.write_json(
        path,
        {
            "project_id": 1,
            "agent_api_key": "private-agent-key",
            "admin_api_key": "private-admin-key",
        },
    )
    return path


@pytest.fixture(scope="module")
def evidence(revocation, tmp_path_factory):
    root = tmp_path_factory.mktemp("revocation")
    access = make_access(revocation, root)
    output = root / "result"
    backend = RedmineFixture()
    backend.issue.update(
        {
            "custom_fields": [{"id": 8, "name": "Business owner", "value": "retained"}],
            "attachments": [{"id": 12, "filename": "gateway.txt"}],
            "watchers": [{"id": 9, "name": "Watcher"}],
            "children": [],
            "relations": [{"id": 3, "issue_id": 42, "issue_to_id": 43, "relation_type": "relates"}],
        }
    )
    issue_path = root / "offline-issue.json"
    real_run = subprocess.run
    commands = []
    bootstrap = """
import json, os, runpy, socket, sys
from pathlib import Path
import httpx
sys.path.insert(0, os.environ["OFFLINE_SUPPORT"])
from conftest import RedmineFixture
from servicedesk_agent.config import ModelSettings
backend = RedmineFixture()
issue_path = Path(os.environ["OFFLINE_ISSUE"])
backend.issue = json.loads(issue_path.read_text())
def handle(request):
    if request.url.path == "/projects/1.json":
        return httpx.Response(200, json={"project": {"id": 1, "identifier": "servicedesk-demo"}})
    return backend.handle(request)
def forbidden(*args, **kwargs):
    raise AssertionError("Offline worker attempted external I/O or live model construction")
httpx.HTTPTransport = lambda **kwargs: httpx.MockTransport(handle)
socket.socket.connect = forbidden
socket.create_connection = forbidden
ModelSettings.build = forbidden
script = sys.argv.pop(1)
sys.path.insert(0, str(Path(script).parent))
try:
    runpy.run_path(script, run_name="__main__")
finally:
    issue_path.write_text(json.dumps(backend.issue))
"""
    with pytest.MonkeyPatch.context() as patcher:
        mutations = mock_service(patcher, backend)

        def offline_process(command, **kwargs):
            commands.append(command)
            issue_path.write_text(json.dumps(backend.issue))
            result = real_run(
                [sys.executable, "-c", bootstrap, *command[1:]],
                env={
                    **os.environ,
                    "OFFLINE_SUPPORT": str(ROOT / "tests"),
                    "OFFLINE_ISSUE": str(issue_path),
                },
                **kwargs,
            )
            backend.issue = json.loads(issue_path.read_text())
            return result

        patcher.setattr(revocation.subprocess, "run", offline_process)
        try:
            result = revocation.run_check(access, output)
        except RuntimeError:
            reports = {"output": str(output)}
            for p in output.glob("*.json"):
                if p.name in {"summary.json", "initial.json", "revoke.json", "probe.json"}:
                    value = json.loads(p.read_text())
                    reports[p.name] = {
                        "error_type": value.get("error_type"),
                        "checks": value.get("checks"),
                    }
            pytest.fail(json.dumps(reports))
        assert mutations == [{"method": "POST", "path": "/issues.json"}]
        assert [c[c.index("--worker") + 1] for c in commands] == ["initial", "revoke", "probe"]
        assert all("--live" not in c for c in commands)
    return output, result


def load_evidence(output):
    return [
        json.loads((output / name).read_text())
        for name in (
            "initial.json",
            "revoke.json",
            "probe.json",
            "before.json",
            "after.json",
            "source.json",
        )
    ]


def test_standard_entrypoint_and_environment_order(revocation, monkeypatch, tmp_path):
    default = revocation.parser().parse_args(["--output", "newdir"])
    assert default.access == Path(".runtime/redmine-access.json")
    assert default.live is False and default.env_file == []
    args = revocation.parser().parse_args(
        ["--output", "newdir", "--env-file", ".runtime/cpa.env", "--live"]
    )
    assert args.env_file == [Path(".runtime/cpa.env")] and args.live
    with pytest.raises(SystemExit):
        revocation.parser().parse_args([])
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

    monkeypatch.setattr(revocation, "run_check", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--output",
            "result",
            "--live",
            "--env-file",
            str(first),
            "--env-file",
            str(second),
        ],
    )
    assert revocation.main() == 0
    assert seen == [("second", "private-model-key", True)]


@pytest.mark.parametrize("kind", ["directory", "file", "dangling_symlink"])
def test_existing_output_is_preserved_before_access_read(revocation, tmp_path, kind):
    output = tmp_path / "existing"
    if kind == "directory":
        output.mkdir()
    elif kind == "file":
        output.write_bytes(b"retain")
    else:
        output.symlink_to(tmp_path / "absent")
    with pytest.raises(FileExistsError, match="Output already exists"):
        revocation.run_check(tmp_path / "absent-access", output)
    if kind == "file":
        assert output.read_bytes() == b"retain"
    elif kind == "dangling_symlink":
        assert output.is_symlink()


def test_foreign_project_rejected_before_any_mutation(revocation, monkeypatch, tmp_path):
    access = make_access(revocation, tmp_path)
    mutations = mock_service(monkeypatch, RedmineFixture(), identifier="production")
    with pytest.raises(RuntimeError):
        revocation.run_check(access, tmp_path / "output")
    assert mutations == []


@pytest.mark.parametrize(
    "url,method",
    [
        ("http://127.0.0.1:3080/issues/42.json", "PUT"),
        ("http://127.0.0.1:3080/issues/42.json", "POST"),
        ("http://127.0.0.1:3080/issues/42.json", "PATCH"),
        ("http://127.0.0.1:3080/issues/42.json", "DELETE"),
        ("https://desk.example/issues/42.json", "GET"),
        ("http://127.0.0.1:3080/issues/43.json", "GET"),
    ],
)
def test_audit_blocks_out_of_scope_requests_and_redacts_headers(revocation, url, method):
    with revocation.TicketAudit(1, 42) as audit:
        with pytest.raises(RuntimeError):
            audit.handle_request(
                httpx.Request(
                    method,
                    url + "?api_key=secret-query",
                    headers={"X-Redmine-API-Key": "secret-header"},
                )
            )
        assert audit.requests == [{"method": method, "path": httpx.URL(url).path}]


def test_three_process_native_evidence_and_private_files(revocation, evidence):
    output, result = evidence
    assert result["passed"] and all(result["checks"].values())
    assert result["external_model_requests"] == 0
    initial, revoked, probe, before, after, source = load_evidence(output)
    assert len({p["process_id"] for p in (initial, revoked, probe)}) == 3
    assert initial["checkpoint"]["next"] == ["tools", "tools"]
    assert revoked["checkpoint"]["next"] == probe["checkpoint"]["next"] == []
    assert len(initial["checkpoint"]["receipts"]) == 1
    assert revoked["model_calls"] == revoked["model_requests"] == []
    assert revoked["stale_contract_error"] == "contract_changed"
    assert before == after and len(before["journals"]) == 1
    assert Path(probe["checkpoint"]["checkpoint_path"]).read_bytes().startswith(b"SQLite format 3")
    assert source["entrypoint_sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    with tarfile.open(output / "source.tar.gz") as archive:
        assert {
            "src/servicedesk_agent/session.py",
            "scripts/check_live_revocation.py",
            "scripts/check_live_cancellation.py",
        } <= set(archive.getnames())
    for name in ("initial", "revoke", "probe"):
        assert (output / f"{name}.stdout.log").is_file()
        assert (output / f"{name}.stderr.log").is_file()
        assert json.loads((output / f"{name}.process.json").read_text())["returncode"] == 0
    for trace in result["traces"].values():
        data = json.loads(Path(trace).read_text())
        assert data["messages"] == probe["checkpoint"]["messages"]
        assert data["usage"] == probe["usage"]
    for path in output.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        if path.suffix in {".json", ".log"}:
            assert b"private-agent-key" not in path.read_bytes()
            assert b"private-admin-key" not in path.read_bytes()


@pytest.mark.parametrize("field", ["model_requests", "model_calls", "redmine_requests"])
def test_empty_probe_trajectory_fails_even_with_zero_counts(revocation, evidence, field):
    args = load_evidence(evidence[0])
    probe = args[2]
    probe[field] = []
    if field in {"model_requests", "model_calls"}:
        probe["usage"]["calls"] = 0
    checks = revocation.acceptance(*args)
    assert not all(checks.values())
    expected = (
        "zero_post_revocation_mutations"
        if field == "redmine_requests"
        else "nonempty_model_evidence"
    )
    assert checks[expected] is False


@pytest.mark.parametrize("key", ["revoked-note-probe", "revoked-status-probe"])
@pytest.mark.parametrize(
    "damage", ["missing_call", "missing_reply", "duplicate_reply", "wrong_error"]
)
def test_tool_denial_requires_real_paired_calls(revocation, evidence, key, damage):
    args = load_evidence(evidence[0])
    messages = args[2]["checkpoint"]["messages"]
    reply = next(m for m in messages if m["type"] == "tool" and m["data"]["tool_call_id"] == key)
    if damage == "missing_call":
        for message in messages:
            if message["type"] == "ai":
                message["data"]["tool_calls"] = [
                    c for c in message["data"].get("tool_calls", []) if c["id"] != key
                ]
    elif damage == "missing_reply":
        messages.remove(reply)
    elif damage == "duplicate_reply":
        messages.append(deepcopy(reply))
    else:
        reply["data"]["content"] = json.dumps({"ok": False, "error": "stale_observation"})
    checks = revocation.acceptance(*args)
    assert not all(checks.values())


@pytest.mark.parametrize(
    "damage,failed_check",
    [
        ("mutation", "zero_post_revocation_mutations"),
        ("empty_receipts", "verified_receipts_preserved"),
        ("empty_compaction", "native_compaction_retained"),
        ("empty_summary_trace", "native_compaction_retained"),
        ("lost_pending", "pending_and_handoff_preserved"),
        ("regained_authority", "readonly_contract_and_constraints"),
        ("changed_source", "source_matches"),
        ("changed_attachment", "complete_ticket_unchanged"),
        ("old_contract_accepted", "old_contract_rejected"),
        ("revocation_model_call", "zero_model_revocation"),
        ("changed_repeat", "idempotent_revocation"),
        ("lost_queued_pair", "old_calls_cancelled_and_paired"),
        ("queue_remaining", "completed_without_queued_tasks"),
    ],
)
def test_evidence_regressions_fail_closed(revocation, evidence, damage, failed_check):
    args = load_evidence(evidence[0])
    initial, revoked, probe, _, after, _ = args
    if damage == "mutation":
        probe["redmine_requests"].append({"method": "PUT", "path": "/issues/42.json"})
        assert probe["mutation_attempts"] == 0
    elif damage == "empty_receipts":
        for phase in (initial, revoked, probe):
            phase["checkpoint"]["receipts"] = {}
    elif damage == "empty_compaction":
        initial["checkpoint"]["compaction"] = None
    elif damage == "empty_summary_trace":
        initial["summarization_requests"] = []
    elif damage == "lost_pending":
        probe["checkpoint"]["pending_actions"] = []
    elif damage == "regained_authority":
        probe["checkpoint"]["contract"]["allow_notes"] = True
    elif damage == "changed_source":
        revoked["ending_implementation_sha256"] = "different"
    elif damage == "changed_attachment":
        after["attachments"].clear()
    elif damage == "old_contract_accepted":
        revoked["stale_contract_error"] = None
    elif damage == "revocation_model_call":
        revoked["forbidden_model_attempts"] = ["model_generation"]
    elif damage == "changed_repeat":
        revoked["repeat_checkpoint"]["checkpoint_id"] = "different"
    elif damage == "lost_queued_pair":
        revoked["checkpoint"]["messages"] = [
            m
            for m in revoked["checkpoint"]["messages"]
            if m["type"] != "tool" or m["data"]["tool_call_id"] != revocation.OLD_NOTE
        ]
    elif damage == "queue_remaining":
        probe["checkpoint"]["next"] = ["tools"]
    checks = revocation.acceptance(*args)
    assert checks[failed_check] is False


def test_tool_pairing_rejects_empty_or_interleaved_history(revocation):
    assert revocation._pairs([])[1] is False
    messages = messages_to_dict(
        [
            tool_call("read_ticket", {}, "read"),
            HumanMessage(content="interleaved"),
            ToolMessage(content='{"ok": true}', tool_call_id="read"),
        ]
    )
    assert revocation._pairs(messages)[1] is False


def test_worker_output_refuses_overwrite(revocation, tmp_path):
    revocation.common.write_json(tmp_path / "revoke.json", {"retain": True})
    with pytest.raises(FileExistsError):
        revocation.worker(tmp_path / "absent", tmp_path, "revoke")
    assert json.loads((tmp_path / "revoke.json").read_text()) == {"retain": True}


def test_worker_logs_redact_keys_and_retain_timeout_output(revocation, monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            "worker", 90, output=b"partial private-key", stderr=b"error"
        )

    monkeypatch.setattr(revocation.subprocess, "run", timeout)
    with pytest.raises(RuntimeError):
        revocation._launch(tmp_path / "access", tmp_path, "probe", ["private-key"])
    assert (tmp_path / "probe.stdout.log").read_bytes() == b"partial [redacted]"
    assert (tmp_path / "probe.stderr.log").read_bytes() == b"error"
    assert json.loads((tmp_path / "probe.process.json").read_text()) == {
        "phase": "probe",
        "returncode": None,
        "log_redactions": 1,
    }


@pytest.mark.parametrize("save_handoff", [True, False])
def test_optional_live_worker_reads_and_retains_real_usage(
    revocation, evidence, monkeypatch, tmp_path, save_handoff
):
    import shutil

    original = evidence[0]
    output = tmp_path / "live-fixture"
    shutil.copytree(original, output)
    access = make_access(revocation, tmp_path)
    backend = RedmineFixture()
    backend.issue = json.loads((output / "after.json").read_text())
    mock_service(monkeypatch, backend)
    monkeypatch.setenv("DESK_MODEL", "fixture-model")
    monkeypatch.setenv("DESK_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("DESK_MODEL_API_KEY", "private-live-key")
    builds, closes = [], []
    usage = {"input_tokens": 25, "output_tokens": 5, "total_tokens": 30}

    class FixtureModel(ScriptedChatModel):
        @property
        def root_client(self):
            return SimpleNamespace(close=lambda: closes.append(True))

    def build(settings):
        builds.append(True)
        responses = [
            tool_call("read_handoff", {}, "live-handoff"),
            tool_call("read_ticket", {}, "live-read"),
        ]
        if save_handoff:
            responses.append(
                tool_call(
                    "save_handoff",
                    {"pending_actions": revocation.PENDING, "summary": revocation.HANDOFF},
                    "live-save",
                )
            )
        responses.append(
            AIMessage(content="Ticket remains open; pending actions and evidence retained.")
        )
        return FixtureModel(
            responses=[
                response.model_copy(update={"usage_metadata": usage}) for response in responses
            ]
        )

    monkeypatch.setattr(revocation.ModelSettings, "build", build)
    live = revocation.worker(access, output, "live")
    assert builds == closes == [True]
    calls = 4 if save_handoff else 3
    assert live["model_mode"] == "live" and live["usage"]["calls"] == calls
    assert live["usage"]["input_tokens"] == calls * 25
    assert live["usage"]["output_tokens"] == calls * 5
    assert live["usage"]["unknown_usage_calls"] == 0 and live["mutation_attempts"] == 0
    assert all(call["usage"] == usage for call in live["model_calls"])
    assert live["checkpoint"]["receipts"] == live["start_checkpoint"]["receipts"]
    args = load_evidence(original)
    # The fixture's copied SQLite file has a new path, while the production run reuses one root.
    for checkpoint in (live["start_checkpoint"], live["checkpoint"]):
        checkpoint["checkpoint_path"] = args[2]["checkpoint"]["checkpoint_path"]
    assert all(revocation.acceptance(*args, live=live).values())
    live["model_requests"] = []
    assert revocation.acceptance(*args, live=live)["nonempty_model_evidence"] is False


@pytest.mark.parametrize("failure", ["worker", "acceptance", "missing_report", "invalid_report"])
def test_failed_live_run_recovers_usage_from_retained_reports(
    revocation, monkeypatch, tmp_path, failure
):
    access = make_access(revocation, tmp_path)
    output = tmp_path / "output"
    mock_service(monkeypatch, RedmineFixture())
    monkeypatch.setenv("DESK_MODEL", "fixture-model")
    monkeypatch.setenv("DESK_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("DESK_MODEL_API_KEY", "private-live-key")
    phases = []
    usage = {"calls": 2, "input_tokens": 50, "output_tokens": 10, "failed_calls": 0}

    def launch(access_path, directory, phase, secrets):
        phases.append(phase)
        if phase == "live" and failure == "missing_report":
            raise RuntimeError("Worker stopped before its report.")
        if phase == "live" and failure == "invalid_report":
            revocation.common.write_private(directory / "live.json", b'{"usage":')
            raise RuntimeError("Worker report incomplete.")
        revocation.common.write_json(
            directory / f"{phase}.json",
            {
                "usage": usage,
                "model_calls": [{}, {}],
                "model_requests": [[], [], []],
            },
        )
        if phase == "live" and failure == "worker":
            raise RuntimeError("Worker failed after its report.")

    def accept(initial, revoked, probe, before, after, source, live=None):
        if live is not None:
            raise RuntimeError("Acceptance failed after model calls.")
        return {"scripted_checks": True}

    monkeypatch.setattr(revocation, "_launch", launch)
    monkeypatch.setattr(revocation, "acceptance", accept)
    with pytest.raises(RuntimeError):
        revocation.run_check(access, output, live=True)
    summary = json.loads((output / "summary.json").read_text())
    assert phases == ["initial", "revoke", "probe", "live"]
    assert summary["passed"] is False
    assert summary["phase_usage"]["probe"] == usage
    if failure in {"missing_report", "invalid_report"}:
        assert summary["external_model_requests"] is None
        assert "live" not in summary["phase_usage"]
    else:
        assert summary["phase_usage"]["live"] == usage
        assert summary["external_model_requests"] == 3


def test_live_launch_requires_passing_scripted_acceptance(revocation, monkeypatch, tmp_path):
    access = make_access(revocation, tmp_path)
    output = tmp_path / "output"
    mock_service(monkeypatch, RedmineFixture())
    monkeypatch.setenv("DESK_MODEL", "fixture-model")
    monkeypatch.setenv("DESK_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("DESK_MODEL_API_KEY", "private-live-key")
    phases = []

    def launch(access_path, directory, phase, secrets):
        phases.append(phase)
        revocation.common.write_json(directory / f"{phase}.json", {"usage": {"calls": 0}})

    monkeypatch.setattr(revocation, "_launch", launch)
    monkeypatch.setattr(revocation, "acceptance", lambda *args, **kwargs: {"tool_denial": False})
    with pytest.raises(RuntimeError):
        revocation.run_check(access, output, live=True)
    summary = json.loads((output / "summary.json").read_text())
    assert phases == ["initial", "revoke", "probe"]
    assert summary["checks"]["tool_denial"] is False
    assert summary["external_model_requests"] == 0 and summary["passed"] is False
