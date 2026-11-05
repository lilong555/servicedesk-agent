#!/usr/bin/env python3
"""Manage only this project's loopback-only Redmine demonstration container."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "servicedesk-agent-redmine"


def docker() -> str:
    installed = shutil.which("docker")
    windows = Path("/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe")
    if installed:
        return installed
    if windows.exists():
        return str(windows)
    raise RuntimeError("Start a Docker engine and install the Docker CLI.")


def call(*args: str, input_text: str | None = None, timeout: int = 180) -> str:
    result = subprocess.run(
        [docker(), *args],
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "The local Redmine command failed.")
    return result.stdout


def require_demo() -> None:
    info = json.loads(call("inspect", CONTAINER))[0]
    if info["Config"].get("Labels", {}).get("io.servicedesk-agent.demo") != "true":
        raise RuntimeError("The container must carry this project's demo label.")


def seed(*, rotate_keys: bool = False) -> dict:
    require_demo()
    output = call(
        "exec",
        "-i",
        "-e",
        f"DESK_ROTATE_KEYS={int(rotate_keys)}",
        CONTAINER,
        "bundle",
        "exec",
        "rails",
        "runner",
        "-",
        input_text=(ROOT / "scripts/redmine_seed.rb").read_text(),
    )
    line = next(line for line in output.splitlines() if line.startswith("DESK_SEED_JSON="))
    data = json.loads(line.partition("=")[2])
    directory = ROOT / ".runtime"
    directory.mkdir(exist_ok=True, mode=0o700)
    access = directory / "redmine-access.json"
    access.write_text(json.dumps(data, indent=2) + "\n")
    access.chmod(0o600)
    env_file = directory / "redmine.env"
    env_file.write_text(
        "DESK_REDMINE_URL=http://127.0.0.1:3080\n"
        f"DESK_REDMINE_API_KEY={data['agent_api_key']}\n"
        f"DESK_PROJECT_ID={data['project_id']}\n"
        f"DESK_POLICY_CATALOG={ROOT / 'src/servicedesk_agent/assets/policies.json'}\n"
        "DESK_KNOWLEDGE_SCOPE=demo-service-desk\n"
    )
    env_file.chmod(0o600)
    return {
        "url": "http://127.0.0.1:3080",
        "ticket_id": data["ticket_id"],
        "project_id": data["project_id"],
        "login": "desk.agent",
        "credentials_file": str(access),
        "env_file": str(env_file),
    }


def up() -> dict:
    # Named volumes avoid Windows/WSL bind-mount path translation.
    call(
        "compose",
        "-f",
        "-",
        "up",
        "-d",
        "--pull",
        "missing",
        input_text=(ROOT / "compose.yaml").read_text(),
    )
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with opener.open("http://127.0.0.1:3080/login", timeout=3) as response:
                if response.status == 200:
                    return seed()
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(2)
    raise RuntimeError("Redmine startup timed out; inspect the isolated container's logs.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["up", "seed", "rotate-keys", "stop", "status"])
    args = parser.parse_args()
    if args.action == "up":
        result = up()
    elif args.action in {"seed", "rotate-keys"}:
        result = seed(rotate_keys=args.action == "rotate-keys")
    elif args.action == "stop":
        require_demo()
        call("stop", CONTAINER)
        result = {"stopped": CONTAINER, "data_volume_preserved": True}
    else:
        require_demo()
        info = json.loads(call("inspect", CONTAINER))[0]
        result = {"container": CONTAINER, "state": info["State"]["Status"]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
