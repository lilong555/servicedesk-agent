#!/usr/bin/env python3
"""Connect this project to an existing CPA service through a private SSH tunnel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from langchain_core.tools import tool

from servicedesk_agent.config import ModelSettings
from servicedesk_agent.usage import UsageRecorder

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
SOCKET = RUNTIME / "cpa-ssh.sock"
ENV_FILE = RUNTIME / "cpa.env"


def ssh_args(host: str) -> list[str]:
    proxy = shlex.join(
        [
            sys.executable,
            str(Path(__file__).with_name("ssh_socket.py")),
            "%h",
            "%p",
        ]
    )
    return [
        "ssh",
        "-o",
        f"ProxyCommand={proxy}",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "IPQoS=none",
        "-C",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-S",
        str(SOCKET),
        host,
    ]


def control(host: str, operation: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-S", str(SOCKET), "-O", operation, host],
        capture_output=True,
        text=True,
        timeout=15,
    )


def unit_name() -> str:
    suffix = hashlib.sha256(str(SOCKET).encode()).hexdigest()[:12]
    return f"servicedesk-cpa-{suffix}"


def stop_service() -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", "stop", unit_name()],
        capture_output=True,
        text=True,
        timeout=15,
    )


def start_tunnel(host: str, local_port: int, remote_port: int) -> None:
    stop_service()
    command = ssh_args(host)
    subprocess.run(
        [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            f"--unit={unit_name()}",
            "--property=Restart=on-failure",
            "--property=RestartSec=2",
            *command[:-1],
            "-M",
            "-NT",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            host,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if control(host, "check").returncode == 0:
            return
        time.sleep(0.2)
    raise ValueError(f"SSH tunnel startup timed out; inspect user service {unit_name()}.")


def connect(host: str, model: str, local_port: int, remote_port: int) -> dict:
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    command = ssh_args(host)
    if control(host, "check").returncode:
        start_tunnel(host, local_port, remote_port)
    # The config is parsed in memory; only the service access key is kept locally.
    response = subprocess.run(
        command + ["sudo -n docker exec cli-proxy-api cat /CLIProxyAPI/config.yaml"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    try:
        configuration = yaml.safe_load(response.stdout)
    except yaml.YAMLError:
        raise ValueError("CPA configuration is invalid YAML.") from None
    if not isinstance(configuration, dict):
        raise ValueError("CPA configuration must be a mapping.")
    keys = configuration.get("api-keys", [])
    if not isinstance(keys, list) or not keys or not isinstance(keys[0], str) or not keys[0]:
        raise ValueError("CPA configuration has no service access key.")
    base_url = f"http://127.0.0.1:{local_port}/v1"
    with httpx.Client(
        headers={"Authorization": f"Bearer {keys[0]}"},
        trust_env=False,
        timeout=20,
    ) as client:
        listing = client.get(f"{base_url}/models")
        listing.raise_for_status()
        models = {item["id"] for item in listing.json()["data"]}
    if model not in models:
        raise ValueError(f"The CPA model list does not contain {model}.")
    values = {
        "DESK_MODEL": model,
        "DESK_MODEL_BASE_URL": base_url,
        "DESK_MODEL_API_KEY": keys[0],
        "DESK_MODEL_TIMEOUT": "60",
        "DESK_MODEL_MAX_TOKENS": "8192",
        "LANGSMITH_TRACING": "false",
    }
    descriptor = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write("".join(f"{key}={json.dumps(value)}\n" for key, value in values.items()))
    ENV_FILE.chmod(0o600)
    return {
        "connected": True,
        "model": model,
        "base_url": base_url,
        "env_file": str(ENV_FILE),
        "ssh_host": host,
    }


def probe() -> dict:
    if not ENV_FILE.is_file():
        raise ValueError("Run the connect command first.")
    load_dotenv(ENV_FILE, override=True)
    settings = ModelSettings.from_env()
    model = settings.build()
    usage = UsageRecorder()

    @tool
    def connection_check(value: str) -> str:
        """Return the supplied connection check value."""
        return value

    try:
        response = model.bind_tools([connection_check], tool_choice="connection_check").invoke(
            "Call connection_check with value 'servicedesk-ready'.",
            {"callbacks": [usage]},
        )
    finally:
        model.root_client.close()
    passed = any(
        item["name"] == "connection_check" and item["args"] == {"value": "servicedesk-ready"}
        for item in response.tool_calls
    )
    result = {
        "model": settings.name,
        "tool_call_verified": passed,
        "usage": usage.summary(settings),
    }
    if not passed:
        raise ValueError("The model response did not contain the expected tool call.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["connect", "probe", "status", "stop"])
    parser.add_argument("--host", default="azure")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--local-port", type=int, default=18317)
    parser.add_argument("--remote-port", type=int, default=8317)
    args = parser.parse_args()
    try:
        if args.action == "connect":
            result = connect(args.host, args.model, args.local_port, args.remote_port)
        elif args.action == "probe":
            result = probe()
        else:
            operation = "exit" if args.action == "stop" else "check"
            response = control(args.host, operation)
            stopped = stop_service().returncode == 0 if args.action == "stop" else False
            result = {"action": args.action, "ok": response.returncode == 0 or stopped}
    except (subprocess.SubprocessError, httpx.HTTPError, OSError) as exc:
        raise SystemExit(f"CPA connection failed ({type(exc).__name__}).") from None
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
