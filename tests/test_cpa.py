from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml
from dotenv import dotenv_values

SERVICE_KEY = "synthetic-cpa-service-key"
UPSTREAM_KEY = "synthetic-upstream-key"
MODEL = "test-model"


@pytest.fixture
def cpa(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts" / "cpa.py"
    spec = importlib.util.spec_from_file_location("test_cpa_script", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("DESK_", "OPENAI_", "LANGCHAIN_", "LANGSMITH_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "RUNTIME", tmp_path / ".runtime")
    monkeypatch.setattr(module, "SOCKET", module.RUNTIME / "cpa-ssh.sock")
    monkeypatch.setattr(module, "ENV_FILE", module.RUNTIME / "cpa.env")
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("Unexpected network access"))
    )
    configuration = yaml.safe_dump(
        {"api-keys": [SERVICE_KEY], "upstream-provider": {"api-key": UPSTREAM_KEY}}
    )
    ssh = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=configuration, stderr=""))
    monkeypatch.setattr(module.subprocess, "run", ssh)
    clock = SimpleNamespace(now=100.0)

    def advance_time(seconds):
        clock.now += seconds

    timer = SimpleNamespace(
        monotonic=Mock(side_effect=lambda: clock.now),
        sleep=Mock(side_effect=advance_time),
    )
    monkeypatch.setattr(module, "time", timer)
    responses = SimpleNamespace(status=200, body={"data": [{"id": MODEL}]}, error=None)
    requests = []
    clients = []
    real_client = httpx.Client

    def handle(request):
        requests.append(request)
        if responses.error is not None:
            raise responses.error
        return httpx.Response(responses.status, json=responses.body)

    def client_factory(**kwargs):
        client = real_client(**kwargs, transport=httpx.MockTransport(handle))
        clients.append(client)
        return client

    factory = Mock(side_effect=client_factory)
    monkeypatch.setattr(module.httpx, "Client", factory)
    return SimpleNamespace(
        module=module,
        ssh=ssh,
        configuration=configuration,
        responses=responses,
        requests=requests,
        clients=clients,
        factory=factory,
        timer=timer,
    )


@pytest.mark.parametrize("existing_tunnel", [False, True])
def test_connect_uses_private_ssh_forward_and_authenticated_mock_http(cpa, existing_tunnel):
    module = cpa.module
    cpa.ssh.side_effect = [
        subprocess.CompletedProcess([], 0 if existing_tunnel else 1, stdout="", stderr=""),
        *(
            []
            if existing_tunnel
            else [
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 1),
                subprocess.CompletedProcess([], 0),
            ]
        ),
        subprocess.CompletedProcess([], 0, stdout=cpa.configuration, stderr=""),
    ]
    result = module.connect("mock-host", MODEL, 19417, 9317)
    calls = cpa.ssh.call_args_list
    assert calls[0].args[0] == ["ssh", "-S", str(module.SOCKET), "-O", "check", "mock-host"]
    assert calls[0].kwargs == {"capture_output": True, "text": True, "timeout": 15}
    if not existing_tunnel:
        assert calls[1].args[0] == ["systemctl", "--user", "stop", module.unit_name()]
        assert calls[1].kwargs == {"capture_output": True, "text": True, "timeout": 15}
        forward = calls[2]
        assert forward.args[0] == [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            f"--unit={module.unit_name()}",
            "--property=Restart=on-failure",
            "--property=RestartSec=2",
            *module.ssh_args("mock-host")[:-1],
            "-M",
            "-NT",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            "127.0.0.1:19417:127.0.0.1:9317",
            "mock-host",
        ]
        assert forward.kwargs == {
            "capture_output": True,
            "text": True,
            "check": True,
            "timeout": 30,
        }
        assert calls[3] == calls[4] == calls[0]
        cpa.timer.sleep.assert_called_once_with(0.2)
    else:
        cpa.timer.sleep.assert_not_called()
    assert calls[-1].args[0] == module.ssh_args("mock-host") + [
        "sudo -n docker exec cli-proxy-api cat /CLIProxyAPI/config.yaml"
    ]
    assert calls[-1].kwargs == {
        "capture_output": True,
        "text": True,
        "check": True,
        "timeout": 30,
    }
    assert "BatchMode=yes" in calls[-1].args[0]
    assert "ConnectTimeout=10" in calls[-1].args[0]
    for call in [calls[-1]] if existing_tunnel else [calls[2], calls[-1]]:
        proxy_options = [value for value in call.args[0] if value.startswith("ProxyCommand=")]
        assert len(proxy_options) == 1
        assert shlex.split(proxy_options[0].removeprefix("ProxyCommand=")) == [
            sys.executable,
            str(Path(module.__file__).with_name("ssh_socket.py")),
            "%h",
            "%p",
        ]
    assert len(calls) == (2 if existing_tunnel else 6)
    assert len(cpa.requests) == 1
    assert cpa.requests[0].method == "GET"
    assert str(cpa.requests[0].url) == "http://127.0.0.1:19417/v1/models"
    assert cpa.requests[0].headers["Authorization"] == f"Bearer {SERVICE_KEY}"
    cpa.factory.assert_called_once_with(
        headers={"Authorization": f"Bearer {SERVICE_KEY}"},
        trust_env=False,
        timeout=20,
    )
    assert all(client.is_closed for client in cpa.clients)
    assert result == {
        "connected": True,
        "model": MODEL,
        "base_url": "http://127.0.0.1:19417/v1",
        "env_file": str(module.ENV_FILE),
        "ssh_host": "mock-host",
    }
    assert SERVICE_KEY not in json.dumps(result)
    assert UPSTREAM_KEY not in json.dumps(result)
    assert module.RUNTIME.stat().st_mode & 0o777 == 0o700
    assert module.ENV_FILE.stat().st_mode & 0o777 == 0o600
    assert dotenv_values(module.ENV_FILE) == {
        "DESK_MODEL": MODEL,
        "DESK_MODEL_BASE_URL": "http://127.0.0.1:19417/v1",
        "DESK_MODEL_API_KEY": SERVICE_KEY,
        "DESK_MODEL_TIMEOUT": "60",
        "DESK_MODEL_MAX_TOKENS": "8192",
        "LANGSMITH_TRACING": "false",
    }
    assert UPSTREAM_KEY not in module.ENV_FILE.read_text()
    assert list(module.RUNTIME.iterdir()) == [module.ENV_FILE]


def test_unit_name_is_stable_and_scoped_to_the_control_socket(cpa, tmp_path, monkeypatch):
    socket_path = cpa.module.SOCKET
    expected = f"servicedesk-cpa-{hashlib.sha256(str(socket_path).encode()).hexdigest()[:12]}"
    assert cpa.module.unit_name() == expected
    assert cpa.module.unit_name() == expected
    monkeypatch.setattr(cpa.module, "ROOT", tmp_path / "another-root")
    assert cpa.module.unit_name() == expected
    monkeypatch.setattr(cpa.module, "SOCKET", tmp_path / "another-root" / "cpa-ssh.sock")
    assert cpa.module.unit_name() != expected
    cpa.ssh.assert_not_called()


@pytest.mark.parametrize("returncode", [0, 5])
def test_stop_service_returns_the_user_service_result(cpa, returncode):
    cpa.ssh.return_value.returncode = returncode
    response = cpa.module.stop_service()
    assert response is cpa.ssh.return_value
    assert response.returncode == returncode
    cpa.ssh.assert_called_once_with(
        ["systemctl", "--user", "stop", cpa.module.unit_name()],
        capture_output=True,
        text=True,
        timeout=15,
    )
    cpa.factory.assert_not_called()


def test_start_tunnel_replaces_missing_service_and_waits_for_control_socket(cpa):
    cpa.ssh.side_effect = [
        subprocess.CompletedProcess([], 5),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 1),
        subprocess.CompletedProcess([], 1),
        subprocess.CompletedProcess([], 0),
    ]
    assert cpa.module.start_tunnel("mock-host", 19417, 9317) is None
    calls = cpa.ssh.call_args_list
    assert calls[0].args[0] == ["systemctl", "--user", "stop", cpa.module.unit_name()]
    assert calls[1].args[0][0] == "systemd-run"
    assert all(
        call.args[0] == ["ssh", "-S", str(cpa.module.SOCKET), "-O", "check", "mock-host"]
        for call in calls[2:]
    )
    assert len(calls) == 5
    assert [call.args for call in cpa.timer.sleep.call_args_list] == [(0.2,), (0.2,)]
    cpa.factory.assert_not_called()


def test_startup_timeout_stops_before_config_read_and_http(cpa, monkeypatch, capsys):
    cpa.ssh.side_effect = [
        subprocess.CompletedProcess([], 1),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 1),
    ]
    cpa.timer.monotonic.side_effect = [100.0, 100.0, 115.0]
    monkeypatch.setattr(sys, "argv", ["cpa.py", "connect", "--host", "mock-host", "--model", MODEL])
    with pytest.raises(SystemExit, match="SSH tunnel startup timed out") as raised:
        cpa.module.main()
    assert cpa.module.unit_name() in str(raised.value)
    assert [call.args[0][0] for call in cpa.ssh.call_args_list] == [
        "ssh",
        "systemctl",
        "systemd-run",
        "ssh",
    ]
    cpa.timer.sleep.assert_called_once_with(0.2)
    cpa.factory.assert_not_called()
    assert not cpa.module.ENV_FILE.exists()
    output = capsys.readouterr()
    assert SERVICE_KEY not in str(raised.value) + output.out + output.err
    assert UPSTREAM_KEY not in str(raised.value) + output.out + output.err


@pytest.mark.parametrize("directory_name", ["checkout", "checkout with spaces"])
def test_ssh_proxy_command_preserves_executable_and_script_paths(
    cpa, tmp_path, monkeypatch, directory_name
):
    checkout = tmp_path / directory_name
    executable = checkout / "python environment" / "bin" / "python"
    script = checkout / "scripts" / "cpa.py"
    monkeypatch.setattr(cpa.module, "__file__", str(script))
    monkeypatch.setattr(sys, "executable", str(executable))
    arguments = cpa.module.ssh_args("mock-host")
    proxy = next(value for value in arguments if value.startswith("ProxyCommand="))
    assert shlex.split(proxy.removeprefix("ProxyCommand=")) == [
        str(executable),
        str(script.with_name("ssh_socket.py")),
        "%h",
        "%p",
    ]
    assert arguments[arguments.index(proxy) - 1] == "-o"
    assert arguments[-3:] == ["-S", str(cpa.module.SOCKET), "mock-host"]
    cpa.ssh.assert_not_called()
    cpa.factory.assert_not_called()


def test_connect_restricts_existing_env_file_and_replaces_stale_values(cpa):
    module = cpa.module
    module.RUNTIME.mkdir(mode=0o700)
    module.ENV_FILE.write_text("STALE=old-value\n")
    module.ENV_FILE.chmod(0o644)
    module.connect("mock-host", MODEL, 19417, 9317)
    assert module.ENV_FILE.stat().st_mode & 0o777 == 0o600
    assert "STALE" not in dotenv_values(module.ENV_FILE)


@pytest.mark.parametrize(
    "configuration",
    [{}, {"api-keys": []}, {"api-keys": [""]}, {"api-keys": [None]}, {"api-keys": [123]}],
)
def test_connect_rejects_missing_or_invalid_service_keys_before_http(cpa, configuration):
    cpa.ssh.return_value.stdout = yaml.safe_dump(configuration)
    with pytest.raises(ValueError, match="service access key"):
        cpa.module.connect("mock-host", MODEL, 19417, 9317)
    cpa.factory.assert_not_called()
    assert not cpa.module.ENV_FILE.exists()


@pytest.mark.parametrize(
    "configuration",
    [None, [], {"api-keys": SERVICE_KEY}, {"api-keys": {0: SERVICE_KEY}}],
)
def test_connect_rejects_invalid_config_shapes_before_http(cpa, configuration):
    cpa.ssh.return_value.stdout = yaml.safe_dump(configuration)
    with pytest.raises(ValueError):
        cpa.module.connect("mock-host", MODEL, 19417, 9317)
    cpa.factory.assert_not_called()
    assert not cpa.module.ENV_FILE.exists()


def test_missing_model_preserves_existing_env_file(cpa):
    module = cpa.module
    module.RUNTIME.mkdir(mode=0o700)
    module.ENV_FILE.write_text("DESK_MODEL=previous-model\n")
    cpa.responses.body = {"data": [{"id": "another-model"}]}
    with pytest.raises(ValueError, match="model list"):
        module.connect("mock-host", MODEL, 19417, 9317)
    assert module.ENV_FILE.read_text() == "DESK_MODEL=previous-model\n"
    assert all(client.is_closed for client in cpa.clients)


@pytest.mark.parametrize("status", [401, 403, 503])
def test_http_failure_does_not_create_env_file(cpa, status):
    cpa.responses.status = status
    with pytest.raises(httpx.HTTPStatusError):
        cpa.module.connect("mock-host", MODEL, 19417, 9317)
    assert not cpa.module.ENV_FILE.exists()
    assert all(client.is_closed for client in cpa.clients)


def test_main_success_output_contains_connection_metadata_only(cpa, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cpa.py", "connect", "--host", "mock-host", "--model", MODEL])
    cpa.module.main()
    output = capsys.readouterr()
    assert json.loads(output.out)["connected"] is True
    assert SERVICE_KEY not in output.out + output.err
    assert UPSTREAM_KEY not in output.out + output.err
    assert output.err == ""


@pytest.mark.parametrize(
    "failure",
    [
        "ssh_exit",
        "ssh_timeout",
        "http_connect",
        "http_status",
        "systemd_exit",
        "systemd_timeout",
        "systemd_unavailable",
    ],
)
def test_main_redacts_subprocess_and_http_errors(cpa, monkeypatch, capsys, failure):
    if failure.startswith("systemd_"):
        if failure == "systemd_exit":
            error = subprocess.CalledProcessError(
                1,
                ["systemd-run"],
                output=SERVICE_KEY,
                stderr=UPSTREAM_KEY,
            )
        elif failure == "systemd_timeout":
            error = subprocess.TimeoutExpired(
                ["systemd-run"],
                30,
                output=SERVICE_KEY,
                stderr=UPSTREAM_KEY,
            )
        else:
            error = FileNotFoundError(f"Missing systemd-run: {SERVICE_KEY}")
        cpa.ssh.side_effect = [
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
            error,
        ]
    elif failure == "ssh_exit":
        cpa.ssh.side_effect = subprocess.CalledProcessError(
            1,
            ["ssh"],
            output=SERVICE_KEY,
            stderr=UPSTREAM_KEY,
        )
    elif failure == "ssh_timeout":
        cpa.ssh.side_effect = subprocess.TimeoutExpired(
            ["ssh"],
            15,
            output=SERVICE_KEY,
            stderr=UPSTREAM_KEY,
        )
    elif failure == "http_connect":
        cpa.responses.error = httpx.ConnectError(f"Connection failed: {SERVICE_KEY}")
    else:
        cpa.responses.status = 403
        cpa.responses.body = {"error": SERVICE_KEY}
    monkeypatch.setattr(sys, "argv", ["cpa.py", "connect", "--host", "mock-host", "--model", MODEL])
    with pytest.raises(SystemExit, match="CPA connection failed") as raised:
        cpa.module.main()
    output = capsys.readouterr()
    diagnostic = str(raised.value) + output.out + output.err
    assert SERVICE_KEY not in diagnostic
    assert UPSTREAM_KEY not in diagnostic
    assert not cpa.module.ENV_FILE.exists()
    if failure.startswith("systemd_"):
        assert [call.args[0][0] for call in cpa.ssh.call_args_list] == [
            "ssh",
            "systemctl",
            "systemd-run",
        ]
        cpa.factory.assert_not_called()
        cpa.timer.sleep.assert_not_called()


def test_main_redacts_malformed_yaml_containing_service_keys(cpa, monkeypatch, capsys):
    cpa.ssh.return_value.stdout = f'api-keys: ["{SERVICE_KEY}"\n'
    monkeypatch.setattr(sys, "argv", ["cpa.py", "connect", "--host", "mock-host", "--model", MODEL])
    with pytest.raises(SystemExit) as raised:
        cpa.module.main()
    output = capsys.readouterr()
    assert SERVICE_KEY not in str(raised.value) + output.out + output.err
    assert UPSTREAM_KEY not in str(raised.value) + output.out + output.err
    assert not cpa.module.ENV_FILE.exists()


@pytest.mark.parametrize("returncode", [0, 1])
def test_status_only_checks_ssh_control(cpa, monkeypatch, capsys, returncode):
    cpa.ssh.return_value.returncode = returncode
    monkeypatch.setattr(sys, "argv", ["cpa.py", "status", "--host", "mock-host"])
    cpa.module.main()
    assert json.loads(capsys.readouterr().out) == {"action": "status", "ok": returncode == 0}
    cpa.ssh.assert_called_once_with(
        ["ssh", "-S", str(cpa.module.SOCKET), "-O", "check", "mock-host"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    cpa.factory.assert_not_called()
    assert not cpa.module.ENV_FILE.exists()


@pytest.mark.parametrize("control_returncode", [0, 1])
@pytest.mark.parametrize("service_returncode", [0, 5])
def test_stop_exits_ssh_and_stops_user_service(
    cpa, monkeypatch, capsys, control_returncode, service_returncode
):
    cpa.ssh.side_effect = [
        subprocess.CompletedProcess([], control_returncode),
        subprocess.CompletedProcess([], service_returncode),
    ]
    monkeypatch.setattr(sys, "argv", ["cpa.py", "stop", "--host", "mock-host"])
    cpa.module.main()
    assert json.loads(capsys.readouterr().out) == {
        "action": "stop",
        "ok": control_returncode == 0 or service_returncode == 0,
    }
    calls = cpa.ssh.call_args_list
    assert [call.args[0] for call in calls] == [
        ["ssh", "-S", str(cpa.module.SOCKET), "-O", "exit", "mock-host"],
        ["systemctl", "--user", "stop", cpa.module.unit_name()],
    ]
    assert all(
        call.kwargs == {"capture_output": True, "text": True, "timeout": 15} for call in calls
    )
    cpa.factory.assert_not_called()
    assert not cpa.module.ENV_FILE.exists()
