from __future__ import annotations

import json
import os
import socket
import sys
from unittest.mock import Mock

import pytest

from servicedesk_agent import cli
from servicedesk_agent.config import ModelSettings
from servicedesk_agent.domain import BusinessError


@pytest.fixture(autouse=True)
def isolated_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("DESK_", "OPENAI_", "LANGCHAIN_", "LANGSMITH_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("DESK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("Unexpected network access"))
    )
    build = Mock(side_effect=AssertionError("Unexpected model construction"))
    monkeypatch.setattr(ModelSettings, "build", build)
    return build


@pytest.fixture
def cli_desk(desk, monkeypatch):
    backend, client = desk
    monkeypatch.setenv("DESK_REDMINE_URL", client.settings.url)
    monkeypatch.setenv("DESK_REDMINE_API_KEY", "synthetic-redmine-key")
    factory = Mock(return_value=client)
    monkeypatch.setattr(cli, "RedmineClient", factory)
    return backend, client, factory


def test_env_files_overlay_in_order_and_preserve_unrelated_values(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "DESK_MODEL=default-model\nDESK_PROJECT_ID=3\nDESK_REDMINE_API_KEY=default-key\n"
    )
    first = tmp_path / "redmine.env"
    first.write_text(
        "DESK_REDMINE_API_KEY=synthetic-redmine-key\nDESK_PROJECT_ID=4\nDESK_MODEL=first-model\n"
    )
    second = tmp_path / "model.env"
    second.write_text("DESK_MODEL=selected-model\nDESK_MODEL_API_KEY=synthetic-model-key\n")
    monkeypatch.setenv("DESK_MODEL", "inherited-model")
    captured = {}

    def dispatch(args):
        captured.update(
            {key: value for key, value in os.environ.items() if key.startswith("DESK_")}
        )
        assert args.env_file == [first, second]
        return {"ok": True}

    monkeypatch.setattr(cli, "dispatch", dispatch)
    monkeypatch.setattr(
        sys, "argv", ["desk-agent", "--env-file", str(first), "--env-file", str(second), "doctor"]
    )
    cli.main()
    assert captured["DESK_MODEL"] == "selected-model"
    assert captured["DESK_PROJECT_ID"] == "4"
    assert captured["DESK_REDMINE_API_KEY"] == "synthetic-redmine-key"
    assert captured["DESK_MODEL_API_KEY"] == "synthetic-model-key"
    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": True}
    assert "synthetic-" not in output.out + output.err


def test_default_dotenv_preserves_existing_environment(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("DESK_MODEL=file-model\nDESK_PROJECT_ID=3\n")
    monkeypatch.setenv("DESK_MODEL", "inherited-model")
    dispatch = Mock(side_effect=lambda _args: {"model": os.getenv("DESK_MODEL")})
    monkeypatch.setattr(cli, "dispatch", dispatch)
    monkeypatch.setattr(sys, "argv", ["desk-agent", "doctor"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"model": "inherited-model"}
    assert os.environ["DESK_PROJECT_ID"] == "3"


def test_missing_env_file_stops_before_dispatch(tmp_path, monkeypatch):
    dispatch = Mock()
    monkeypatch.setattr(cli, "dispatch", dispatch)
    monkeypatch.setattr(
        sys, "argv", ["desk-agent", "--env-file", str(tmp_path / "missing.env"), "doctor"]
    )
    with pytest.raises(SystemExit, match="Environment file not found"):
        cli.main()
    dispatch.assert_not_called()


def test_doctor_without_credentials_is_offline(monkeypatch, isolated_cli):
    redmine = Mock(side_effect=AssertionError("Unexpected Redmine construction"))
    monkeypatch.setattr(cli, "RedmineClient", redmine)
    result = cli.dispatch(cli.parser().parse_args(["doctor"]))
    assert result["live_model_configured"] is False
    assert result["redmine_configured"] is False
    assert set(result["versions"]) == {"deepagents", "langgraph", "langgraph-checkpoint-sqlite"}
    redmine.assert_not_called()
    isolated_cli.assert_not_called()


@pytest.mark.parametrize("model_url", ["https://model.test/v1", "http://remote.test/v1"])
def test_doctor_checks_configuration_and_ticket_without_model_calls(
    cli_desk, monkeypatch, isolated_cli, model_url
):
    backend, _, _ = cli_desk
    monkeypatch.setenv("DESK_MODEL", "test-model")
    monkeypatch.setenv("DESK_MODEL_BASE_URL", model_url)
    monkeypatch.setenv("DESK_MODEL_API_KEY", "synthetic-model-key")
    result = cli.dispatch(cli.parser().parse_args(["doctor", "--ticket", "42"]))
    assert result["live_model_configured"] is model_url.startswith("https:")
    assert result["redmine_principal_id"] == 1
    assert result["ticket"] == {"id": 42, "status": "New"}
    assert backend.writes == []
    assert all(request.method == "GET" for request in backend.requests)
    assert "synthetic-" not in json.dumps(result)
    isolated_cli.assert_not_called()


def test_doctor_reports_redmine_denial_without_model_calls(cli_desk, isolated_cli):
    backend, _, _ = cli_desk
    backend.http_status = 403
    result = cli.dispatch(cli.parser().parse_args(["doctor"]))
    assert result["redmine_error"]["error"] == "access_denied"
    assert backend.writes == []
    isolated_cli.assert_not_called()


def test_status_uses_persisted_grants_and_receipts_without_model_calls(
    cli_desk, contract, monkeypatch, isolated_cli
):
    backend, client, _ = cli_desk
    receipt = {
        "operation_id": "operation-1",
        "action": "add_note",
        "journal_id": 12,
        "observed_at": "2026-11-05T00:00:00Z",
        "after": {
            "evidence": {
                "source_uri": "https://desk.test/issues/42",
                "source_version": "version-1",
                "content": {"description": "Private ticket text"},
            }
        },
    }
    load = Mock(
        return_value={
            "contract": contract.model_dump(mode="json"),
            "pending_actions": ["Collect logs"],
            "handoff_summary": "Investigation remains open.",
            "receipts": {"operation-1": receipt},
        }
    )
    monkeypatch.setattr(cli, "load_session", load)
    result = cli.dispatch(
        cli.parser().parse_args(["status", "--ticket", "42", "--session", contract.session_id])
    )
    assert result["contract"] == contract.model_dump(mode="json")
    assert result["pending_actions"] == ["Collect logs"]
    assert result["handoff_summary"] == "Investigation remains open."
    assert result["verified_receipts"][0] == {
        key: receipt[key] for key in ("operation_id", "action", "journal_id", "observed_at")
    } | {
        "source_uri": "https://desk.test/issues/42",
        "source_version": "version-1",
    }
    assert load.call_args.args[1] is client
    assert load.call_args.args[2].thread_id == contract.thread_id
    assert "Private ticket text" not in json.dumps(result)
    assert backend.writes == []
    isolated_cli.assert_not_called()


def test_status_missing_session_is_a_business_error(cli_desk, monkeypatch, isolated_cli):
    monkeypatch.setattr(cli, "load_session", Mock(return_value={}))
    with pytest.raises(BusinessError) as raised:
        cli.dispatch(cli.parser().parse_args(["status", "--ticket", "42", "--session", "missing"]))
    assert raised.value.code == "session_missing"
    isolated_cli.assert_not_called()


@pytest.mark.parametrize("explicit_grants", [False, True])
def test_start_requires_explicit_action_grants(cli_desk, monkeypatch, explicit_grants):
    backend, _, _ = cli_desk
    model = Mock()
    monkeypatch.setattr(ModelSettings, "build", Mock(return_value=model))
    bundle = Mock(return_value=None)
    monkeypatch.setattr(cli, "_bundle", bundle)
    execute = Mock(return_value={"verified_receipts": []})
    monkeypatch.setattr(cli, "execute_session", execute)
    arguments = ["start", "--ticket", "42", "--session", "test", "--message", "Close the ticket"]
    if explicit_grants:
        arguments.extend(
            [
                "--allow-notes",
                "--allow-close",
                "--allow-status",
                "5",
                "--allow-status",
                "2",
                "--allow-status",
                "2",
                "--constraint",
                "Keep owner",
                "--constraint",
                "Retain evidence",
                "--components",
            ]
        )
    cli.dispatch(cli.parser().parse_args(arguments))
    contract = execute.call_args.args[2]
    assert contract.allow_notes is explicit_grants
    assert contract.allow_close is explicit_grants
    assert contract.allowed_status_ids == ((2, 5) if explicit_grants else ())
    assert contract.user_constraints == (
        ("Keep owner", "Retain evidence") if explicit_grants else ()
    )
    assert contract.variant == ("B" if explicit_grants else "A")
    assert (contract.project_id, contract.principal_id, contract.ticket_id) == (1, 1, 42)
    assert execute.call_args.kwargs["resume"] is False
    assert bundle.call_args.args[1] == contract
    model.root_client.close.assert_called_once_with()
    assert backend.writes == []


def test_resume_uses_stored_authority_and_closes_model_on_failure(cli_desk, contract, monkeypatch):
    monkeypatch.setattr(
        cli, "load_session", Mock(return_value={"contract": contract.model_dump(mode="json")})
    )
    monkeypatch.setattr(cli, "_bundle", Mock(return_value=None))
    model = Mock()
    monkeypatch.setattr(ModelSettings, "build", Mock(return_value=model))
    execute = Mock(side_effect=BusinessError("session_busy", "Session is owned."))
    monkeypatch.setattr(cli, "execute_session", execute)
    with pytest.raises(BusinessError) as raised:
        cli.dispatch(
            cli.parser().parse_args(["resume", "--ticket", "42", "--session", contract.session_id])
        )
    assert raised.value.code == "session_busy"
    assert execute.call_args.args[2] == contract
    assert execute.call_args.kwargs["resume"] is True
    model.root_client.close.assert_called_once_with()


@pytest.mark.parametrize("command", ["resume", "status"])
@pytest.mark.parametrize("grant", ["--allow-notes", "--allow-close", "--allow-status"])
def test_existing_session_commands_reject_new_grant_flags(command, grant):
    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args([command, "--ticket", "42", "--session", "test", grant])
    assert raised.value.code == 2


def test_main_formats_business_errors_as_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["desk-agent", "doctor"])
    monkeypatch.setattr(
        cli, "dispatch", Mock(side_effect=BusinessError("access_denied", "Access denied."))
    )
    with pytest.raises(SystemExit) as raised:
        cli.main()
    output = capsys.readouterr()
    assert raised.value.code == 2
    assert output.out == ""
    assert json.loads(output.err) == {
        "ok": False,
        "error": "access_denied",
        "message": "Access denied.",
    }


def test_main_redacts_credentials_from_invalid_service_url(monkeypatch, capsys):
    monkeypatch.setenv("DESK_REDMINE_URL", "https://user:synthetic-url-password@desk.test")
    monkeypatch.setattr(sys, "argv", ["desk-agent", "doctor"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    output = capsys.readouterr()
    assert raised.value.code == 2
    assert "synthetic-url-password" not in output.out + output.err + str(raised.value)
    assert json.loads(output.err)["error"]
