#!/usr/bin/env python3
"""Accept session write revocation on one dedicated local Redmine demo ticket."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from deepagents.backends import StateBackend
from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.summarization import SUMMARIZATION_EVENT_KEY, SummarizationMiddleware
from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict

from servicedesk_agent.agent import build_desk_agent
from servicedesk_agent.components import ComponentBundle
from servicedesk_agent.config import ModelSettings, RedmineSettings
from servicedesk_agent.domain import BusinessError, TaskContract
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.provenance import capture_source, source_manifest
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session, revoke_session_writes, session_storage
from servicedesk_agent.testing import ScriptedChatModel, tool_call

if __package__:
    from . import check_live_cancellation as common
else:
    import check_live_cancellation as common

URL, PROJECT = common.URL, common.PROJECT
PENDING, CONSTRAINTS, HANDOFF = common.PENDING, common.CONSTRAINTS, common.HANDOFF
OLD_NOTE, OLD_STATUS = "queued-revocation-note", "queued-revocation-status"
PROBE_NOTE, PROBE_STATUS = "revoked-note-probe", "revoked-status-probe"
SEED_NOTE = "Revocation acceptance: initial investigation verified; awaiting gateway logs."
RAW_MARKER = "REVOCATION-RAW-EVIDENCE"
SUMMARY = "Investigation continues; original evidence is retained."
RESUME = (
    "Session write permissions have been revoked. Read the saved handoff and current ticket, "
    "retain every pending action and user constraint verbatim, preserve the handoff summary "
    "and verified receipts, and report the current open ticket and outstanding work."
)
PHASES = ("initial", "revoke", "probe")


class TicketAudit(common.ReadOnlyAudit):
    """Allow one setup note, then audit and block every service mutation."""

    def __init__(self, project_id: int, ticket_id: int, *, seed: bool = False):
        super().__init__()
        self.ticket_path = f"/issues/{ticket_id}.json"
        self.paths = {
            self.ticket_path,
            f"/projects/{project_id}.json",
            "/users/current.json",
            "/issue_statuses.json",
        }
        self.seed = seed

    def handle_request(self, request):
        local = (
            request.url.scheme == "http"
            and request.url.host == "127.0.0.1"
            and request.url.port == 3080
        )
        if not local or request.url.path not in self.paths:
            self.requests.append({"method": request.method, "path": request.url.path})
            raise RuntimeError("Requests must stay within the isolated ticket and service.")
        if self.seed and request.method == "PUT" and request.url.path == self.ticket_path:
            self.requests.append({"method": request.method, "path": request.url.path})
            payload = json.loads(request.content)
            update = payload.get("issue", {})
            if (
                set(payload) != {"issue"}
                or set(update) != {"notes"}
                or not update["notes"].startswith(SEED_NOTE + "\n\n[desk-action:")
                or not update["notes"].endswith("]")
            ):
                raise RuntimeError("Only the dedicated setup note is authorized.")
            self.seed = False
            return self.transport.handle_request(request)
        return super().handle_request(request)


def snapshot(root: Path, client, contract: TaskContract) -> dict:
    with session_storage(root, contract.thread_id) as (saver, directory):
        graph = build_desk_agent(ScriptedChatModel(), client, contract, checkpointer=saver)
        state = graph.get_state({"configurable": {"thread_id": contract.thread_id}})
        values = state.values
        event = values.get(SUMMARIZATION_EVENT_KEY)
        return {
            "thread_id": contract.thread_id,
            "checkpoint_path": str((directory / "checkpoint.sqlite").resolve()),
            "checkpoint_id": state.config["configurable"].get("checkpoint_id"),
            "next": list(state.next),
            "contract": values.get("contract"),
            "pending_actions": values.get("pending_actions", []),
            "handoff_summary": values.get("handoff_summary", ""),
            "receipts": values.get("receipts", {}),
            "observations": values.get("observations", {}),
            "messages": messages_to_dict(values.get("messages", [])),
            "files": values.get("files", {}),
            "compaction": (
                {**event, "summary_message": messages_to_dict([event["summary_message"]])[0]}
                if event
                else None
            ),
        }


def _prepare(root, client, contract, observer, data):
    ticket = client.get_ticket(contract.ticket_id)
    if ticket.is_closed:
        raise ValueError("The dedicated test ticket must be open.")
    model = ScriptedChatModel(
        responses=[
            tool_call("read_ticket", {}, "seed-read"),
            tool_call(
                "add_ticket_note",
                {"note": SEED_NOTE, "expected_version": ticket.evidence.source_version},
                "seed-note",
            ),
            tool_call(
                "save_handoff", {"pending_actions": PENDING, "summary": HANDOFF}, "seed-save"
            ),
            "Initial investigation saved.",
        ],
        callbacks=[observer],
    )
    data["seed_result"] = execute_session(
        root, client, contract, model, "Record one investigation note and preserve the open ticket."
    )
    summary_observer = common.ModelEvidence()
    backend = StateBackend()
    components = ComponentBundle(
        agent_kwargs={
            "backend": backend,
            "middleware": [
                SummarizationMiddleware(
                    model=ScriptedChatModel(responses=[SUMMARY], callbacks=[summary_observer]),
                    backend=backend,
                    trigger=("messages", 7),
                    keep=("messages", 2),
                    trim_tokens_to_summarize=None,
                )
            ],
        }
    )
    data["compact_result"] = execute_session(
        root,
        client,
        contract,
        ScriptedChatModel(responses=["Compaction saved."], callbacks=[observer]),
        "Retain the investigation for handoff.",
        resume=True,
        components=components,
    )
    data["summarization_requests"] = summary_observer.requests
    data["summarization_calls"] = summary_observer.calls
    version = client.get_ticket(contract.ticket_id).evidence.source_version
    queued = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "add_ticket_note",
                "id": OLD_NOTE,
                "args": {"note": "Queued note must stay cancelled.", "expected_version": version},
            },
            {
                "name": "change_ticket_status",
                "id": OLD_STATUS,
                "args": {"status_id": contract.allowed_status_ids[0], "expected_version": version},
            },
        ],
    )
    with session_storage(root, contract.thread_id) as (saver, _):
        graph = build_desk_agent(
            ScriptedChatModel(responses=[queued], callbacks=[observer]),
            client,
            contract,
            checkpointer=saver,
        )
        graph.invoke(
            {"messages": [HumanMessage(content="Pause before the next queued ticket operations.")]},
            {"configurable": {"thread_id": contract.thread_id}},
            interrupt_before=["tools"],
            durability="sync",
        )


def _revoke(root, client, contract, observer, data):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("model_generation")
        raise RuntimeError("Revocation must use zero model calls.")

    data["forbidden_model_attempts"] = attempts
    with (
        patch.object(BaseChatModel, "generate", forbidden),
        patch.object(BaseChatModel, "agenerate", forbidden),
        patch.object(ModelSettings, "build", forbidden),
    ):
        data["result"] = revoke_session_writes(root, client, contract)
        readonly = TaskContract.model_validate(data["result"]["contract"])
        data["first_revocation"] = snapshot(root, client, readonly)
        data["repeat_result"] = revoke_session_writes(root, client, contract)
        data["repeat_checkpoint"] = snapshot(root, client, readonly)
        try:
            execute_session(
                root,
                client,
                contract,
                ScriptedChatModel(callbacks=[observer]),
                RESUME,
                resume=True,
            )
        except BusinessError as exc:
            data["stale_contract_error"] = exc.code
        else:
            raise RuntimeError("The old write contract was accepted.")


def worker(access_path: Path, output: Path, phase: str) -> dict:
    if phase not in (*PHASES, "live"):
        raise ValueError("Unknown phase.")
    report = output / f"{phase}.json"
    if report.exists() or report.is_symlink():
        raise FileExistsError("Phase output already exists.")
    access = common.load_access(access_path)
    run = json.loads((output / "run.json").read_text())
    root = output / "state"
    audit = TicketAudit(access["project_id"], run["ticket_id"], seed=phase == "initial")
    observer = common.ModelEvidence()
    model_settings = None
    data = {
        "phase": phase,
        "process_id": os.getpid(),
        "model_mode": "live" if phase == "live" else "none" if phase == "revoke" else "scripted",
        "implementation_sha256": source_manifest()["implementation_sha256"],
        "entrypoint_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    try:
        with ExitStack() as stack:
            store = stack.enter_context(redmine_store(root))
            client = stack.enter_context(
                RedmineClient(
                    RedmineSettings(
                        url=URL, api_key=access["agent_api_key"], project_id=access["project_id"]
                    ),
                    store=store,
                    transport=audit,
                )
            )
            common.require_project(client._http, access["project_id"])
            if phase == "initial":
                ticket = client.get_ticket(run["ticket_id"])
                targets = [s["id"] for s in ticket.allowed_statuses if s["id"] != ticket.status_id]
                if not targets:
                    raise ValueError("The demo ticket requires a distinct allowed status.")
                contract = TaskContract(
                    service_url=URL,
                    project_id=access["project_id"],
                    principal_id=client.current_user()["id"],
                    ticket_id=run["ticket_id"],
                    session_id=run["session_id"],
                    allow_notes=True,
                    allow_close=True,
                    allowed_status_ids=tuple(targets),
                    user_constraints=CONSTRAINTS,
                )
            else:
                initial = json.loads((output / "initial.json").read_text())
                contract = TaskContract.model_validate(initial["checkpoint"]["contract"])
            data["start_checkpoint"] = snapshot(root, client, contract)
            if phase == "initial":
                if data["start_checkpoint"]["contract"] is not None:
                    raise ValueError("Preparation requires a fresh checkpoint.")
                _prepare(root, client, contract, observer, data)
            elif phase == "revoke":
                _revoke(root, client, contract, observer, data)
            else:
                status_id = contract.allowed_status_ids[0]
                contract = TaskContract.model_validate(data["start_checkpoint"]["contract"])
                if phase == "live":
                    model_settings = ModelSettings.from_env()
                    model_settings.require_live()
                    model = model_settings.build()
                    model.callbacks = [observer]
                    stack.callback(model.root_client.close)
                else:
                    version = client.get_ticket(contract.ticket_id).evidence.source_version
                    model = ScriptedChatModel(
                        responses=[
                            tool_call("read_handoff", {}, "probe-handoff"),
                            tool_call("read_ticket", {}, "probe-read"),
                            tool_call(
                                "add_ticket_note",
                                {"note": "Forbidden probe note.", "expected_version": version},
                                PROBE_NOTE,
                            ),
                            tool_call(
                                "change_ticket_status",
                                {"status_id": status_id, "expected_version": version},
                                PROBE_STATUS,
                            ),
                            tool_call(
                                "save_handoff",
                                {"pending_actions": PENDING, "summary": HANDOFF},
                                "probe-save",
                            ),
                            "Write permissions revoked; open ticket and pending work retained.",
                        ],
                        callbacks=[observer],
                    )
                data["result"] = execute_session(
                    root,
                    client,
                    contract,
                    model,
                    RESUME,
                    resume=True,
                    model_settings=model_settings,
                )
            data["checkpoint"] = snapshot(root, client, contract)
            data["ticket"] = common.read_issue(client._http, run["ticket_id"], access["project_id"])
    except Exception as exc:
        data["error_type"] = type(exc).__name__
        raise
    finally:
        data["redmine_requests"] = audit.requests
        data["mutation_attempts"] = sum(r["method"] in common.MUTATIONS for r in audit.requests)
        data["model_requests"] = observer.requests
        data["model_calls"] = observer.calls
        data["usage"] = observer.summary(model_settings)
        data["ending_implementation_sha256"] = source_manifest()["implementation_sha256"]
        common.write_json(report, data)
        common._private_tree(output)
    return data


def _pairs(messages: list[dict]) -> tuple[dict, bool]:
    """Validate chronological provider tool pairing, including duplicate and orphan IDs."""
    calls, replies, pending = {}, {}, set()
    valid = bool(messages)
    for message in messages:
        kind, item = message["type"], message["data"]
        if pending and kind != "tool":
            valid = False
        if kind == "ai":
            if item.get("invalid_tool_calls"):
                valid = False
            for call in item.get("tool_calls", []):
                if not call["id"] or call["id"] in calls:
                    valid = False
                calls[call["id"]] = call
                pending.add(call["id"])
        elif kind == "tool":
            key = item["tool_call_id"]
            if key not in pending or key in replies:
                valid = False
            pending.discard(key)
            try:
                payload = json.loads(item["content"])
            except (ValueError, TypeError):
                payload = None
            replies[key] = {"message": item, "payload": payload, "call": calls.get(key)}
    return replies, valid and not pending


def _success_names(messages: list[dict]) -> set[str]:
    replies, valid = _pairs(messages)
    if not valid:
        return set()
    return {
        item["call"]["name"]
        for item in replies.values()
        if isinstance(item["payload"], dict)
        and item["payload"].get("ok", True) is True
        and item["message"].get("status", "success") == "success"
    }


def acceptance(initial, revoked, probe, before, after, source, live=None) -> dict:
    phases = [initial, revoked, probe, *([live] if live is not None else [])]
    first, rev, final = initial["checkpoint"], revoked["checkpoint"], probe["checkpoint"]
    readonly = {
        **first["contract"],
        "allow_notes": False,
        "allow_close": False,
        "allowed_status_ids": [],
    }
    restored = [
        revoked["first_revocation"],
        revoked["repeat_checkpoint"],
        rev,
        probe["start_checkpoint"],
        final,
    ]
    if live is not None:
        restored.extend([live["start_checkpoint"], live["checkpoint"]])
    replies, paired = _pairs(final["messages"])
    cancelled, rev_paired = _pairs(rev["messages"])
    initial_calls = [
        call
        for m in first["messages"]
        if m["type"] == "ai"
        for call in m["data"].get("tool_calls", [])
    ]
    pending_ids = {
        call["id"]
        for call in initial_calls
        if not any(
            m["type"] == "tool" and m["data"]["tool_call_id"] == call["id"]
            for m in first["messages"]
        )
    }
    event = first["compaction"]
    history = (
        file_data_to_string(first["files"][event["file_path"]])
        if event and event.get("file_path") in first["files"]
        else ""
    )
    receipt_values = list(first["receipts"].values())
    generated = [initial, probe, *([live] if live is not None else [])]
    checks = {
        "separate_processes": len({p["process_id"] for p in phases}) == len(phases),
        "source_matches": bool(source.get("implementation_sha256"))
        and all(
            p["implementation_sha256"]
            == p["ending_implementation_sha256"]
            == source["implementation_sha256"]
            and p["entrypoint_sha256"] == source["entrypoint_sha256"]
            for p in phases
        ),
        "phase_modes": [p["model_mode"] for p in phases]
        == ["scripted", "none", "scripted", *(["live"] if live is not None else [])],
        "no_worker_errors": all(not p.get("error_type") for p in phases),
        "nonempty_model_evidence": all(
            len(p["model_requests"]) == len(p["model_calls"]) == p["usage"]["calls"] > 0
            and all(request for request in p["model_requests"])
            and p["usage"]["failed_calls"] == 0
            for p in generated
        ),
        "zero_model_revocation": (
            revoked["model_requests"] == revoked["model_calls"] == []
            and revoked["usage"]["calls"] == 0
            and revoked["forbidden_model_attempts"] == []
        ),
        "one_verified_setup_write": (
            initial["mutation_attempts"] == 1
            and [r for r in initial["redmine_requests"] if r["method"] in common.MUTATIONS]
            == [{"method": "PUT", "path": f"/issues/{before['id']}.json"}]
            and len(receipt_values) == 1
            and all(
                r["verified"] is True
                and r["action"] == "add_note"
                and r["ticket_id"] == before["id"]
                and any(
                    j["id"] == r["journal_id"] and SEED_NOTE in j.get("notes", "")
                    for j in before["journals"]
                )
                for r in receipt_values
            )
        ),
        "zero_post_revocation_mutations": all(
            p["mutation_attempts"] == 0
            and bool(p["redmine_requests"])
            and all(r["method"] in {"GET", "HEAD"} for r in p["redmine_requests"])
            for p in phases[1:]
        ),
        "complete_ticket_unchanged": bool(before) and before == after,
        "native_checkpoint_chain": all(
            earlier["checkpoint"]["checkpoint_id"] == later["start_checkpoint"]["checkpoint_id"]
            for earlier, later in zip(phases, phases[1:], strict=False)
        )
        and all(
            s["thread_id"] == first["thread_id"]
            and s["checkpoint_path"] == first["checkpoint_path"]
            and bool(s["checkpoint_id"])
            for s in restored
        )
        and first["checkpoint_id"] != rev["checkpoint_id"] != final["checkpoint_id"],
        "initial_write_grants": (
            first["contract"]["allow_notes"] is True
            and first["contract"]["allow_close"] is True
            and bool(first["contract"]["allowed_status_ids"])
        ),
        "queued_writes_present": (
            bool(first["next"])
            and set(first["next"]) == {"tools"}
            and pending_ids == {OLD_NOTE, OLD_STATUS}
            and {(c["id"], c["name"]) for c in initial_calls if c["id"] in pending_ids}
            == {(OLD_NOTE, "add_ticket_note"), (OLD_STATUS, "change_ticket_status")}
        ),
        "old_calls_cancelled_and_paired": rev_paired
        and paired
        and all(
            key in cancelled
            and isinstance(cancelled[key]["payload"], dict)
            and cancelled[key]["message"]["status"] == "error"
            and cancelled[key]["payload"].get("error") == "session_writes_revoked"
            and cancelled[key]["payload"].get("retry_cancelled") is True
            and key in replies
            and replies[key] == cancelled[key]
            for key in (OLD_NOTE, OLD_STATUS)
        ),
        "write_tools_denied": paired
        and all(
            key in replies
            and replies[key]["call"]["name"] == name
            and isinstance(replies[key]["payload"], dict)
            and replies[key]["payload"].get("ok") is False
            and replies[key]["payload"].get("error") == "write_not_authorized"
            and all(
                key not in {c["id"] for c in m["data"].get("tool_calls", [])}
                for m in probe["start_checkpoint"]["messages"]
                if m["type"] == "ai"
            )
            for key, name in (
                (PROBE_NOTE, "add_ticket_note"),
                (PROBE_STATUS, "change_ticket_status"),
            )
        ),
        "old_contract_rejected": revoked.get("stale_contract_error") == "contract_changed",
        "provider_tool_pairing": all(
            bool(p["model_requests"]) and all(_pairs(request)[1] for request in p["model_requests"])
            for p in [probe, *([live] if live is not None else [])]
        ),
        "idempotent_revocation": (
            revoked["result"] == revoked["repeat_result"]
            and revoked["first_revocation"] == revoked["repeat_checkpoint"] == rev
        ),
        "readonly_contract_and_constraints": (
            readonly["user_constraints"] == list(CONSTRAINTS)
            and all(s["contract"] == readonly for s in restored)
            and revoked["result"]["contract"] == readonly
        ),
        "pending_and_handoff_preserved": (
            first["pending_actions"] == PENDING
            and first["handoff_summary"] == HANDOFF
            and all(
                s["pending_actions"] == PENDING and s["handoff_summary"] == HANDOFF
                for s in restored
            )
        ),
        "verified_receipts_preserved": bool(receipt_values)
        and all(s["receipts"] == first["receipts"] for s in restored)
        and all(p["result"]["verified_receipts"] == receipt_values for p in phases[1:]),
        "observations_preserved_by_revocation": bool(first["observations"])
        and all(
            s["observations"] == first["observations"]
            for s in (revoked["first_revocation"], revoked["repeat_checkpoint"], rev)
        ),
        "native_compaction_retained": (
            bool(event)
            and event["cutoff_index"] > 0
            and event["summary_message"]["data"]["additional_kwargs"].get("lc_source")
            == "summarization"
            and RAW_MARKER in history
            and len(initial["summarization_requests"]) == len(initial["summarization_calls"]) == 1
            and bool(initial["summarization_requests"][0])
            and all(s["files"] == first["files"] and s["compaction"] == event for s in restored)
        ),
        "read_ticket_and_handoff": all(
            {"read_ticket", "read_handoff"}
            <= _success_names(p["checkpoint"]["messages"][len(p["start_checkpoint"]["messages"]) :])
            for p in [probe, *([live] if live is not None else [])]
        ),
        "latest_user_in_first_request": all(
            bool(p["model_requests"])
            and bool(humans := [m for m in p["model_requests"][0] if m["type"] == "human"])
            and humans[-1]["data"]["content"] == RESUME
            for p in [probe, *([live] if live is not None else [])]
        ),
        "completed_without_queued_tasks": all(s["next"] == [] for s in restored),
    }
    if live is not None:
        checks["live_usage_retained"] = (
            live["usage"]["calls"] > 0
            and live["usage"]["unknown_usage_calls"] == 0
            and live["usage"]["input_tokens"] > 0
            and live["usage"]["output_tokens"] > 0
        )
    return checks


def _launch(access_path: Path, output: Path, phase: str, secrets: list[str]) -> None:
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
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=360 if phase == "live" else 90
        )
        stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, returncode = exc.stdout or b"", exc.stderr or b"", None
    redactions = 0
    for suffix, content in (("stdout", stdout), ("stderr", stderr)):
        for secret in secrets:
            if secret:
                redactions += content.count(secret.encode())
                content = content.replace(secret.encode(), b"[redacted]")
        common.write_private(output / f"{phase}.{suffix}.log", content)
    common.write_json(
        output / f"{phase}.process.json",
        {
            "phase": phase,
            "returncode": returncode,
            "log_redactions": redactions,
        },
    )
    if returncode != 0 or redactions:
        raise RuntimeError(f"The {phase} worker failed; inspect the private logs.")


def run_check(access_path: Path, output: Path, *, live: bool = False) -> dict:
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output already exists.")
    access_path = access_path.resolve()
    access = common.load_access(access_path)
    secrets = [access["admin_api_key"], access["agent_api_key"]]
    if live:
        settings = ModelSettings.from_env()
        settings.require_live()
        secrets.append(settings.api_key.get_secret_value())
    output = output.absolute()
    output.mkdir(parents=True, mode=0o700)
    entrypoint = Path(__file__).read_bytes()
    common.write_private(output / Path(__file__).name, entrypoint)
    source = {**capture_source(output), "entrypoint_sha256": hashlib.sha256(entrypoint).hexdigest()}
    common.write_json(output / "source.json", source)
    summary = {
        "passed": False,
        "checks": {},
        "output": str(output),
        "external_model_requests": 0,
        "error_type": None,
    }
    try:
        with httpx.Client(
            base_url=URL + "/",
            headers={"X-Redmine-API-Key": access["admin_api_key"]},
            timeout=15,
            trust_env=False,
            follow_redirects=False,
        ) as admin:
            common.require_project(admin, access["project_id"])
            response = admin.post(
                "issues.json",
                json={
                    "issue": {
                        "project_id": access["project_id"],
                        "subject": f"Native revocation acceptance {uuid4().hex[:8]}",
                        "description": RAW_MARKER
                        + ": preserve this dedicated ticket and its evidence.",
                    }
                },
            )
            response.raise_for_status()
            ticket_id = response.json()["issue"]["id"]
            common.write_json(
                output / "run.json",
                {
                    "ticket_id": ticket_id,
                    "session_id": f"revocation-{uuid4().hex}",
                },
            )
            common.write_json(
                output / "created.json", common.read_issue(admin, ticket_id, access["project_id"])
            )
            summary["ticket_url"] = f"{URL}/issues/{ticket_id}"
            try:
                _launch(access_path, output, "initial", secrets)
                before = common.read_issue(admin, ticket_id, access["project_id"])
                common.write_json(output / "before.json", before)
                for phase in ("revoke", "probe", *(("live",) if live else ())):
                    if phase == "live":
                        core = [
                            json.loads((output / f"{name}.json").read_text()) for name in PHASES
                        ]
                        summary["checks"] = acceptance(
                            *core,
                            before,
                            json.loads((output / "after-probe.json").read_text()),
                            source,
                        )
                        summary["checks"]["all_full_readbacks_unchanged"] = all(
                            json.loads((output / f"after-{name}.json").read_text()) == before
                            for name in ("revoke", "probe")
                        )
                        if not all(summary["checks"].values()):
                            raise RuntimeError("Scripted acceptance must pass before live resume.")
                        summary["external_model_requests"] = None
                    _launch(access_path, output, phase, secrets)
                    common.write_json(
                        output / f"after-{phase}.json",
                        common.read_issue(admin, ticket_id, access["project_id"]),
                    )
            finally:
                common.write_json(
                    output / "after.json", common.read_issue(admin, ticket_id, access["project_id"])
                )
        phases = {
            phase: json.loads((output / f"{phase}.json").read_text())
            for phase in (*PHASES, *(("live",) if live else ()))
        }
        after = json.loads((output / "after.json").read_text())
        checks = acceptance(
            phases["initial"],
            phases["revoke"],
            phases["probe"],
            before,
            after,
            source,
            phases.get("live"),
        )
        checks["all_full_readbacks_unchanged"] = all(
            json.loads((output / f"after-{phase}.json").read_text()) == before
            for phase in phases
            if phase != "initial"
        )
        checks["source_stable_at_finish"] = (
            source_manifest()["implementation_sha256"] == source["implementation_sha256"]
        )
        summary.update(
            {
                "checks": checks,
                "passed": all(checks.values()),
                "phase_usage": {name: p["usage"] for name, p in phases.items()},
                "external_model_requests": phases["live"]["usage"]["calls"] if live else 0,
                "traces": {
                    name: p["result"]["trace_path"]
                    for name, p in phases.items()
                    if name in {"probe", "live"}
                },
            }
        )
    except Exception as exc:
        summary["error_type"] = type(exc).__name__
    finally:
        phase_usage = {}
        for phase in (*PHASES, *(("live",) if live else ())):
            try:
                report = json.loads((output / f"{phase}.json").read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(report, dict):
                continue
            usage = report.get("usage")
            if isinstance(usage, dict):
                phase_usage[phase] = usage
            if phase == "live":
                counts = [usage.get("calls")] if isinstance(usage, dict) else []
                counts.extend(
                    len(report[key])
                    for key in ("model_requests", "model_calls")
                    if isinstance(report.get(key), list)
                )
                summary["external_model_requests"] = max(
                    (count for count in counts if type(count) is int and count >= 0), default=None
                )
        summary["phase_usage"] = phase_usage
        common.write_json(output / "summary.json", summary)
        common._private_tree(output)
    if not summary["passed"]:
        raise RuntimeError("Revocation verification failed; inspect the private output directory.")
    return summary


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--access", type=Path, default=Path(".runtime/redmine-access.json"))
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--env-file", type=Path, action="append", default=[])
    result.add_argument("--live", action="store_true", help="Add a separate live read-only resume.")
    result.add_argument("--worker", choices=[*PHASES, "live"], help=argparse.SUPPRESS)
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
            worker(args.access, args.output, args.worker)
        else:
            print(json.dumps(run_check(args.access, args.output, live=args.live), indent=2))
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
