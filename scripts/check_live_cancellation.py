#!/usr/bin/env python3
"""Check two-process cancellation on the isolated local Redmine demonstration project."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, messages_to_dict

from servicedesk_agent.agent import build_desk_agent
from servicedesk_agent.config import ModelSettings, RedmineSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.provenance import capture_source, source_manifest
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session, session_storage
from servicedesk_agent.testing import ScriptedChatModel, tool_call
from servicedesk_agent.usage import UsageRecorder

URL = "http://127.0.0.1:3080"
PROJECT = "servicedesk-demo"
OLD_CALL = "pending-cancellation-note"
PENDING = ["Obtain the gateway log sample.", "Await requester confirmation."]
CONSTRAINTS = ("Keep this ticket open. Preserve its owner and dependencies.",)
HANDOFF = "Investigation paused; gateway logs and requester confirmation remain pending."
STOP = (
    "\u6682\u505c\u6b64\u524d\u6240\u6709\u5f85\u6267\u884c\u64cd\u4f5c\u3002"
    "\u53ea\u8bfb\u53d6\u5f53\u524d\u5de5\u5355\u548c\u5df2\u4fdd\u5b58\u7684\u4ea4\u63a5\uff0c"
    "\u539f\u6587\u4fdd\u7559\u6bcf\u4e00\u6761\u5f85\u529e\u548c\u7528\u6237\u7ea6\u675f\u3002"
    "\u7981\u6b62\u65b0\u589e\u5907\u6ce8\u6216\u4fee\u6539\u72b6\u6001\uff0c"
    "\u4fdd\u6301\u5de5\u5355\u5f00\u542f\uff0c\u4ec5\u6c47\u62a5\u5f53\u524d\u72b6\u6001\u3002"
)
MUTATIONS = {"PUT", "POST", "PATCH", "DELETE"}
INCLUDE = "journals,relations,attachments,watchers,children,allowed_statuses"


def write_private(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)


def write_json(path: Path, value) -> None:
    write_private(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def _private_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)


def load_access(path: Path) -> dict:
    access = json.loads(path.read_text())
    if (
        not isinstance(access, dict)
        or type(access.get("project_id")) is not int
        or access["project_id"] <= 0
        or any(
            not isinstance(access.get(key), str) or not access[key].strip()
            for key in ("admin_api_key", "agent_api_key")
        )
        or access.get("url", URL) != URL
    ):
        raise ValueError("Use local Redmine access credentials for 127.0.0.1:3080.")
    return access


def require_project(http, project_id: int) -> None:
    response = http.get(f"projects/{project_id}.json")
    response.raise_for_status()
    project = response.json()["project"]
    if project["id"] != project_id or project["identifier"] != PROJECT:
        raise ValueError("Use the isolated servicedesk-demo project.")


def read_issue(http, ticket_id: int, project_id: int) -> dict:
    response = http.get(f"issues/{ticket_id}.json", params={"include": INCLUDE})
    response.raise_for_status()
    issue = response.json()["issue"]
    if issue["id"] != ticket_id or issue["project"]["id"] != project_id:
        raise ValueError("The test ticket is outside the isolated project.")
    return issue


class ReadOnlyAudit(httpx.BaseTransport):
    """Count attempted requests before forwarding reads to the local service."""

    def __init__(self):
        self.requests: list[dict] = []
        self.transport = httpx.HTTPTransport(retries=0, trust_env=False)

    def handle_request(self, request):
        self.requests.append({"method": request.method, "path": request.url.path})
        if (
            request.url.scheme != "http"
            or request.url.host != "127.0.0.1"
            or request.url.port != 3080
        ):
            raise RuntimeError("Redmine requests must stay on 127.0.0.1:3080.")
        if request.method not in {"GET", "HEAD"}:
            raise RuntimeError("The agent attempted a Redmine mutation.")
        return self.transport.handle_request(request)

    def close(self):
        self.transport.close()


class ModelEvidence(UsageRecorder):
    def __init__(self):
        super().__init__()
        self.requests: list[list[dict]] = []

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        super().on_chat_model_start(serialized, messages, run_id=run_id, **kwargs)
        with self._lock:
            self.requests.extend(messages_to_dict(batch) for batch in messages)


def snapshot(root: Path, client, contract: TaskContract) -> dict:
    with session_storage(root, contract.thread_id) as (saver, directory):
        graph = build_desk_agent(ScriptedChatModel(), client, contract, checkpointer=saver)
        state = graph.get_state({"configurable": {"thread_id": contract.thread_id}})
        values = state.values
        return {
            "thread_id": contract.thread_id,
            "checkpoint_id": state.config["configurable"].get("checkpoint_id"),
            "checkpoint_path": str((directory / "checkpoint.sqlite").resolve()),
            "next": list(state.next),
            "contract": values.get("contract"),
            "pending_actions": values.get("pending_actions", []),
            "handoff_summary": values.get("handoff_summary", ""),
            "receipts": values.get("receipts", {}),
            "messages": messages_to_dict(values.get("messages", [])),
        }


def worker(access_path: Path, output: Path, phase: str, *, live: bool = False) -> dict:
    report = output / f"{phase}.json"
    if report.exists() or report.is_symlink():
        raise FileExistsError("Phase output already exists.")
    access = load_access(access_path)
    run = json.loads((output / "run.json").read_text())
    root = output / "state"
    audit = ReadOnlyAudit()
    observer = ModelEvidence()
    model_settings = None
    data = {
        "phase": phase,
        "process_id": os.getpid(),
        "model_mode": "live" if phase == "resume" and live else "scripted",
        "implementation_sha256": source_manifest()["implementation_sha256"],
        "entrypoint_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    try:
        with ExitStack() as stack:
            store = stack.enter_context(redmine_store(root))
            settings = RedmineSettings(
                url=URL, api_key=access["agent_api_key"], project_id=access["project_id"]
            )
            client = stack.enter_context(RedmineClient(settings, store=store, transport=audit))
            require_project(client._http, settings.project_id)
            contract = TaskContract(
                service_url=URL,
                project_id=settings.project_id,
                principal_id=client.current_user()["id"],
                ticket_id=run["ticket_id"],
                session_id=run["session_id"],
                allow_notes=True,
                user_constraints=CONSTRAINTS,
            )
            before = snapshot(root, client, contract)
            data["start_checkpoint"] = before
            if phase == "initial":
                if before["contract"] is not None:
                    raise ValueError("Initial phase requires a fresh native checkpoint.")
                ticket = client.get_ticket(contract.ticket_id)
                model = ScriptedChatModel(
                    responses=[
                        tool_call(
                            "add_ticket_note",
                            {
                                "note": "Cancellation regression: this note must stay pending.",
                                "expected_version": ticket.evidence.source_version,
                            },
                            OLD_CALL,
                        )
                    ],
                    callbacks=[observer],
                )
                with session_storage(root, contract.thread_id) as (saver, _):
                    graph = build_desk_agent(model, client, contract, checkpointer=saver)
                    graph.invoke(
                        {
                            "messages": [
                                HumanMessage(content="Investigate and append one note; keep open.")
                            ],
                            "contract": contract.model_dump(mode="json"),
                            "pending_actions": PENDING,
                            "handoff_summary": HANDOFF,
                            "receipts": {},
                            "observations": {},
                        },
                        {"configurable": {"thread_id": contract.thread_id}},
                        interrupt_before=["tools"],
                        durability="sync",
                    )
            else:
                contract = TaskContract.model_validate(before["contract"])
                if live:
                    model_settings = ModelSettings.from_env()
                    model = model_settings.build()
                    model.callbacks = [observer]
                    stack.callback(model.root_client.close)
                else:
                    model = ScriptedChatModel(
                        responses=[
                            tool_call("read_handoff", {}, "resume-handoff"),
                            tool_call("read_ticket", {}, "resume-ticket"),
                            tool_call(
                                "save_handoff",
                                {"pending_actions": PENDING, "summary": HANDOFF},
                                "resume-save",
                            ),
                            "\u5df2\u6682\u505c\u5199\u5165\uff0c\u5f85\u529e\u5df2\u4fdd\u7559\u3002",
                        ],
                        callbacks=[observer],
                    )
                data["result"] = execute_session(
                    root, client, contract, model, STOP, resume=True, model_settings=model_settings
                )
            data["checkpoint"] = snapshot(root, client, contract)
    except Exception as exc:
        data["error_type"] = type(exc).__name__
        raise
    finally:
        data["redmine_requests"] = audit.requests
        data["mutation_attempts"] = sum(item["method"] in MUTATIONS for item in audit.requests)
        data["model_requests"] = observer.requests
        data["model_calls"] = observer.calls
        data["usage"] = observer.summary(model_settings)
        write_json(report, data)
        _private_tree(output)
    return data


def acceptance(initial: dict, resumed: dict, before: dict, after: dict, source: dict) -> dict:
    first = initial["checkpoint"]
    start = resumed["start_checkpoint"]
    final = resumed["checkpoint"]
    messages = final["messages"]
    replies = [
        item["data"]
        for item in messages
        if item["type"] == "tool" and item["data"]["tool_call_id"] == OLD_CALL
    ]
    superseded = False
    if len(replies) == 1:
        try:
            value = json.loads(replies[0]["content"])
            superseded = (
                replies[0]["status"] == "error"
                and value["error"] == "superseded_by_user_input"
                and value["retry_cancelled"] is True
            )
        except (ValueError, KeyError, TypeError):
            pass
    requests = resumed["model_requests"]
    humans = [item for item in requests[0] if item["type"] == "human"] if requests else []
    # Command-returning business tools may omit ToolMessage.name.
    call_names = {
        call["id"]: call["name"]
        for item in messages
        if item["type"] == "ai"
        for call in item["data"].get("tool_calls", [])
    }
    successful_tools = set()
    for item in messages:
        if item["type"] == "tool":
            try:
                payload = json.loads(item["data"]["content"])
                if payload.get("ok", True):
                    successful_tools.add(call_names.get(item["data"]["tool_call_id"]))
            except (ValueError, TypeError, AttributeError):
                pass
    return {
        "separate_processes": initial["process_id"] != resumed["process_id"],
        "same_native_checkpoint": (
            first["thread_id"] == start["thread_id"] == final["thread_id"]
            and first["checkpoint_path"] == start["checkpoint_path"] == final["checkpoint_path"]
            and first["checkpoint_id"] == start["checkpoint_id"]
            and first["checkpoint_id"] != final["checkpoint_id"]
        ),
        "initial_pending_tools": first["next"] == ["tools"]
        and any(
            call["id"] == OLD_CALL and call["name"] == "add_ticket_note"
            for item in first["messages"]
            if item["type"] == "ai"
            for call in item["data"].get("tool_calls", [])
        ),
        "initial_scripted": initial["model_mode"] == "scripted",
        "zero_initial_mutations": initial["mutation_attempts"] == 0,
        "zero_resume_mutations": resumed["mutation_attempts"] == 0
        and all(item["method"] not in MUTATIONS for item in resumed["redmine_requests"]),
        "complete_ticket_unchanged": before == after,
        "old_call_superseded": superseded,
        "latest_user_in_first_request": bool(humans and humans[-1]["data"]["content"] == STOP),
        "read_ticket_and_handoff": {"read_ticket", "read_handoff"} <= successful_tools,
        "constraints_preserved": (
            first["contract"] == final["contract"]
            and final["contract"]["user_constraints"] == list(CONSTRAINTS)
            and final["contract"]["allow_close"] is False
        ),
        "pending_preserved": first["pending_actions"] == final["pending_actions"] == PENDING,
        "receipts_preserved": first["receipts"] == final["receipts"] == {},
        "resume_completed": not final["next"],
        "live_usage_retained": (
            resumed["model_mode"] == "scripted"
            or (resumed["usage"]["calls"] > 0 and resumed["usage"]["unknown_usage_calls"] == 0)
        ),
        "source_matches": all(
            phase["implementation_sha256"] == source["implementation_sha256"]
            and phase["entrypoint_sha256"] == source["entrypoint_sha256"]
            for phase in (initial, resumed)
        ),
    }


def run_check(access_path: Path, output: Path, *, live: bool = False) -> dict:
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output already exists.")
    access_path = access_path.resolve()
    access = load_access(access_path)
    if live:
        ModelSettings.from_env().require_live()
    output = output.absolute()
    output.mkdir(parents=True, mode=0o700)
    entrypoint = Path(__file__).read_bytes()
    write_private(output / "check_live_cancellation.py", entrypoint)
    source = {
        **capture_source(output),
        "entrypoint_sha256": hashlib.sha256(entrypoint).hexdigest(),
    }
    write_json(output / "source.json", source)
    with httpx.Client(
        base_url=URL + "/",
        headers={"X-Redmine-API-Key": access["admin_api_key"]},
        timeout=15,
        trust_env=False,
        follow_redirects=False,
    ) as admin:
        require_project(admin, access["project_id"])
        response = admin.post(
            "issues.json",
            json={
                "issue": {
                    "project_id": access["project_id"],
                    "subject": f"Native cancellation regression {uuid4().hex[:8]}",
                    "description": "Isolated cancellation test. Preserve this ticket and its logs.",
                }
            },
        )
        response.raise_for_status()
        ticket_id = response.json()["issue"]["id"]
        write_json(
            output / "run.json",
            {"ticket_id": ticket_id, "session_id": f"cancellation-{uuid4().hex}"},
        )
        before = read_issue(admin, ticket_id, access["project_id"])
        write_json(output / "before.json", before)
        failure = None
        try:
            for phase in ("initial", "resume"):
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    phase,
                    "--access",
                    str(access_path),
                    "--output",
                    str(output),
                ]
                if phase == "resume" and live:
                    command.append("--live")
                completed = subprocess.run(
                    command, capture_output=True, text=True, timeout=360 if live else 90
                )
                if completed.returncode:
                    raise RuntimeError(f"The {phase} worker failed.")
        except Exception as exc:
            failure = type(exc).__name__
        after = read_issue(admin, ticket_id, access["project_id"])
        write_json(output / "after.json", after)
    summary = {
        "initial_model_mode": "scripted",
        "resume_model_mode": "live" if live else "scripted",
        "ticket_url": f"{URL}/issues/{ticket_id}",
        "output": str(output),
        "checks": {},
        "passed": False,
        "error_type": failure,
    }
    if failure is None:
        initial = json.loads((output / "initial.json").read_text())
        resumed = json.loads((output / "resume.json").read_text())
        summary["checks"] = acceptance(initial, resumed, before, after, source)
        summary["passed"] = all(summary["checks"].values())
        summary["phase_usage"] = {"initial": initial["usage"], "resume": resumed["usage"]}
        summary["external_model_requests"] = resumed["usage"]["calls"] if live else 0
        summary["resume_trace"] = resumed["result"]["trace_path"]
    write_json(output / "summary.json", summary)
    _private_tree(output)
    if not summary["passed"]:
        raise RuntimeError(
            "Cancellation verification failed; inspect the private output directory."
        )
    return summary


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--access", type=Path, default=Path(".runtime/redmine-access.json"))
    result.add_argument("--env-file", type=Path, action="append", default=[])
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--live", action="store_true", help="Use a live model only during resume.")
    result.add_argument("--worker", choices=["initial", "resume"], help=argparse.SUPPRESS)
    return result


def main() -> int:
    args = parser().parse_args()
    previous_umask = os.umask(0o077)
    try:
        load_dotenv(".env", override=False)
        for path in args.env_file:
            if not path.is_file():
                raise FileNotFoundError("Environment file not found.")
            load_dotenv(path, override=True)
        if args.worker:
            worker(args.access, args.output, args.worker, live=args.live)
        else:
            result = run_check(args.access, args.output, live=args.live)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
