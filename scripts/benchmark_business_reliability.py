#!/usr/bin/env python3
"""Frozen, offline Redmine retry/reconciliation and revocation experiments."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
import traceback
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from servicedesk_agent.agent import build_desk_agent
from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import BusinessError, TaskContract, fingerprint
from servicedesk_agent.operations import OperationRecords, redmine_store
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import (
    execute_session,
    load_session,
    revoke_session_writes,
    session_storage,
)
from servicedesk_agent.testing import ScriptedChatModel, tool_call

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "benchmarks/business-reliability"
STRATEGIES = ("at_least_once", "reconcile")
GAP_EXIT = 86


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def database(path):
    connection = sqlite3.connect(path, timeout=10)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class SyntheticRedmine:
    """Each commit is durable before the transport reports a lost response."""

    def __init__(self, root: Path, phase="initial", tick=0):
        self.root, self.phase, self.tick = root, phase, tick
        self.spec = read_json(root / "case.json")
        self.path = root / "server.sqlite"
        with database(self.path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS events "
                "(seq INTEGER PRIMARY KEY, tick INTEGER, phase TEXT, pid INTEGER, "
                "kind TEXT, body TEXT)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS deferred (id INTEGER PRIMARY KEY, body TEXT)")
            conn.execute(
                "INSERT OR IGNORE INTO state VALUES (1, ?)",
                (json.dumps(self.spec["fixture"]["issue"]),),
            )

    def _event(self, conn, kind, **payload):
        conn.execute(
            "INSERT INTO events(tick, phase, pid, kind, body) VALUES (?, ?, ?, ?, ?)",
            (self.tick, self.phase, os.getpid(), kind, json.dumps(payload)),
        )

    def event(self, kind, **payload):
        with database(self.path) as conn:
            self._event(conn, kind, **payload)

    def events(self):
        with database(self.path) as conn:
            return [
                dict(
                    seq=row[0],
                    tick=row[1],
                    phase=row[2],
                    pid=row[3],
                    kind=row[4],
                    **json.loads(row[5]),
                )
                for row in conn.execute("SELECT * FROM events ORDER BY seq")
            ]

    def issue(self):
        with database(self.path) as conn:
            return json.loads(conn.execute("SELECT body FROM state WHERE id=1").fetchone()[0])

    def commit(self, data, *, deferred_id=None):
        with database(self.path) as conn:
            issue = json.loads(conn.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            details = []
            if "status_id" in data:
                target = next(
                    status
                    for status in self.spec["fixture"]["statuses"]
                    if status["id"] == data["status_id"]
                )
                if issue["status"]["id"] != target["id"]:
                    details.append(
                        {
                            "property": "attr",
                            "name": "status_id",
                            "old_value": str(issue["status"]["id"]),
                            "new_value": str(target["id"]),
                        }
                    )
                issue["status"] = target
            journal = {
                "id": len(issue["journals"]) + 1,
                "notes": data.get("notes", ""),
                "user": {"id": self.spec["fixture"]["principal_id"]},
                "details": details,
            }
            issue["journals"].append(journal)
            conn.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(issue),))
            if deferred_id is not None:
                conn.execute("DELETE FROM deferred WHERE id=?", (deferred_id,))
            self._event(conn, "server_commit", journal=journal, deferred_id=deferred_id)

    def release(self):
        with database(self.path) as conn:
            deferred = conn.execute("SELECT id, body FROM deferred ORDER BY id").fetchall()
        for key, body in deferred:
            self.commit(json.loads(body), deferred_id=key)

    def response(self, request, status, body=None):
        self.event("http_response", method=request.method, status=status, body=body)
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    def handle(self, request: httpx.Request):
        body = json.loads(request.content) if request.content else None
        self.event(
            "http_request",
            method=request.method,
            path=request.url.path,
            query=request.url.query.decode(),
            body=body,
        )
        if request.method == "GET":
            if request.url.path == "/users/current.json":
                return self.response(
                    request, 200, {"user": {"id": self.spec["fixture"]["principal_id"]}}
                )
            if request.url.path == "/issue_statuses.json":
                return self.response(
                    request, 200, {"issue_statuses": self.spec["fixture"]["statuses"]}
                )
            if request.url.path == "/issues/42.json":
                return self.response(request, 200, {"issue": self.issue()})
        if request.method == "PUT" and request.url.path == "/issues/42.json":
            self.event("write_admitted", body=body)
            number = sum(event["kind"] == "write_admitted" for event in self.events())
            if number == 1 and self.spec["fault"] == "late_commit":
                with database(self.path) as conn:
                    conn.execute(
                        "INSERT INTO deferred(body) VALUES (?)", (json.dumps(body["issue"]),)
                    )
                    self._event(conn, "fault_hit", fault="late_commit", committed=False)
                raise httpx.ReadTimeout("Synthetic deferred commit", request=request)
            self.commit(body["issue"])
            if self.spec["fault"] == "double_loss" or (
                number == 1 and self.spec["fault"] == "response_loss"
            ):
                self.event("fault_hit", fault="response_loss", committed=True)
                raise httpx.ReadTimeout(
                    "Synthetic response lost after SQLite commit", request=request
                )
            return self.response(request, 204)
        return self.response(request, 404)


def create_case(root, operation_id, strategy, fault, *, native=False, fixture=None):
    root.mkdir(parents=True, exist_ok=False)
    contract = TaskContract(
        service_url="https://business-reliability.invalid",
        project_id=1,
        principal_id=1,
        ticket_id=42,
        session_id=operation_id,
        allow_notes=True,
        allowed_status_ids=(2,),
        user_constraints=("Append the authorized note. Preserve owner and status.",),
    )
    fixture = deepcopy(fixture if fixture is not None else read_json(FIXTURES / "redmine.json"))
    note = f"Investigation completed for {operation_id}."
    marker_id = fingerprint(
        {
            "thread": contract.thread_id,
            "action": "add_note",
            "payload": {"notes": note},
        }
    )
    spec = {
        "operation_id": operation_id,
        "strategy": strategy,
        "fault": fault,
        "native": native,
        "contract": contract.model_dump(mode="json"),
        "note": note,
        "body": {"issue": {"notes": f"{note}\n\n[desk-action:{marker_id}]"}},
        "expected_version": fingerprint(fixture["issue"]),
        "fixture": fixture,
    }
    write_json(root / "case.json", spec)
    return spec


def baseline_state(store, contract):
    item = store.get(("benchmark_at_least_once", contract.thread_id), "task")
    return deepcopy(item.value) if item else None


def baseline_attempt(client, store, contract, backend):
    """Durable at-least-once task; retransmit at the raw HTTP layer once."""
    state = baseline_state(store, contract)
    if state is not None and (state["ack"] is not None or state["sends"] >= 2):
        return state["ack"]
    spec = backend.spec
    client._check_contract(contract)
    if not contract.allow_notes:
        raise BusinessError("write_not_authorized", "This task grants read access.")
    note = spec["note"].strip()
    if not note or len(note) > 8000:
        raise BusinessError("invalid_note", "Use a valid note.")
    current = client.get_ticket(contract.ticket_id)
    initial = spec["fixture"]["issue"]
    if (
        current.status_id != initial["status"]["id"]
        or current.assigned_to_id != initial["assigned_to"]["id"]
    ):
        raise BusinessError("stale_observation", "The note task's business state changed.")
    if state is None:
        if current.evidence.source_version != spec["expected_version"]:
            raise BusinessError("stale_observation", "The initial observation changed.")
        state = {
            "contract": contract.model_dump(mode="json"),
            "note": note,
            "expected_version": spec["expected_version"],
            "body": spec["body"],
            "sends": 0,
            "ack": None,
        }
    state["sends"] += 1
    store.put(("benchmark_at_least_once", contract.thread_id), "task", state, index=False)
    backend.event("baseline_send_checkpoint", state=state)
    with httpx.Client(
        base_url=contract.service_url,
        transport=httpx.MockTransport(backend.handle),
        headers={"X-Redmine-API-Key": "synthetic-key"},
        trust_env=False,
    ) as raw:
        response = raw.put(f"/issues/{contract.ticket_id}.json", json=state["body"])
        response.raise_for_status()
    if spec["fault"] == "checkpoint_gap" and backend.phase == "initial":
        backend.event(
            "checkpoint_gap", boundary="after_HTTP_success_before_baseline_ack", exit=GAP_EXIT
        )
        os._exit(GAP_EXIT)
    state["ack"] = {"kind": "http_ack", "status_code": response.status_code, "send": state["sends"]}
    store.put(("benchmark_at_least_once", contract.thread_id), "task", state, index=False)
    backend.event("baseline_ack_checkpoint", state=state)
    return state["ack"]


def readonly(contract):
    return contract.model_copy(
        update={"allow_notes": False, "allowed_status_ids": (), "allow_close": False}
    )


def serialized_session(root, client, contract):
    state = load_session(root, client, contract)
    if "messages" in state:
        state["messages"] = messages_to_dict(state["messages"])
    return state


def native_attempt(root, client, contract, backend):
    if backend.phase == "initial":
        if backend.spec["fault"] == "checkpoint_gap":
            add_note = client.add_note

            def exit_after_operation_receipt(*args, **kwargs):
                receipt = add_note(*args, **kwargs)
                backend.event(
                    "checkpoint_gap",
                    boundary="after_operation_receipt_before_tool_Command",
                    receipt=receipt.model_dump(mode="json"),
                    exit=GAP_EXIT,
                )
                os._exit(GAP_EXIT)

            client.add_note = exit_after_operation_receipt
        model = ScriptedChatModel(
            responses=[
                tool_call(
                    "add_ticket_note",
                    {
                        "note": backend.spec["note"],
                        "expected_version": backend.spec["expected_version"],
                    },
                    "initial-note",
                ),
                "Investigation result retained.",
            ]
        )
        message, resume = "Append the authorized investigation note.", False
    else:
        model = ScriptedChatModel(
            responses=[
                tool_call("reconcile_ticket_operations", {}, f"reconcile-{backend.phase}"),
                "Read-only reconciliation finished.",
            ]
        )
        message, resume = (
            "Only reconcile the previous operation. Keep further writes stopped.",
            True,
        )
    result = execute_session(root, client, contract, model, message, resume=resume)
    write_json(root / f"{backend.phase}.session.json", serialized_session(root, client, contract))
    return result


def run_phase(root, phase, tick):
    backend = SyntheticRedmine(root, phase, tick)
    spec = backend.spec
    contract = TaskContract.model_validate(spec["contract"])
    outcome = {"phase": phase, "tick": tick, "pid": os.getpid(), "ok": True}
    backend.event("phase_start")
    with (
        redmine_store(root / "runtime") as store,
        RedmineClient(
            RedmineSettings(url=contract.service_url, api_key="synthetic-key"),
            store=store,
            transport=httpx.MockTransport(backend.handle),
        ) as client,
    ):
        try:
            if spec["strategy"] == "at_least_once":
                outcome["result"] = baseline_attempt(client, store, contract, backend)
            elif spec["native"]:
                outcome["result"] = native_attempt(root / "runtime", client, contract, backend)
            elif phase == "initial":
                outcome["result"] = client.add_note(
                    contract,
                    spec["note"],
                    spec["expected_version"],
                ).model_dump(mode="json")
            else:
                outcome["result"] = [
                    receipt.model_dump(mode="json")
                    for receipt in client.reconcile_operations(
                        readonly(contract),
                        include_completed=True,
                    )
                ]
        except (BusinessError, httpx.RequestError) as exc:
            outcome.update(
                ok=False,
                error=getattr(exc, "code", type(exc).__name__),
                message=str(exc),
            )
        except Exception as exc:
            outcome.update(ok=False, error=type(exc).__name__, unexpected=True)
            outcome["traceback"] = traceback.format_exc()
        if spec["strategy"] == "at_least_once":
            state = baseline_state(store, contract)
            outcome["unknown"] = bool(state and state["sends"] and state["ack"] is None)
        else:
            state = OperationRecords(store).load(contract.thread_id)
            outcome["unknown"] = state["pending"] is not None
        outcome["durable_state"] = state
    backend.event("phase_end", outcome=outcome)
    write_json(root / f"{phase}.json", outcome)
    return outcome


def child(root, phase, tick=0, *, revoke=False):
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_revocation" if revoke else "_phase",
        str(root),
        phase,
        str(tick),
    ]
    backend = SyntheticRedmine(root, phase, tick)
    backend.event("process_launch", command=command)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=45,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "LANGCHAIN_TRACING_V2": "false",
                "LANGSMITH_TRACING": "false",
            },
        )
        result = {
            "command": command,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        result = {
            "command": command,
            "exit_code": None,
            "timeout": True,
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
        }
    write_json(root / f"{phase}.process.json", result)
    backend.event("process_exit", result=result)
    if result["exit_code"] == GAP_EXIT:
        # The crashed process cannot write its phase snapshot; reopen its durable store.
        contract = TaskContract.model_validate(backend.spec["contract"])
        with redmine_store(root / "runtime") as store:
            state = (
                baseline_state(store, contract)
                if backend.spec["strategy"] == "at_least_once"
                else OperationRecords(store).load(contract.thread_id)
            )
        outcome = {
            "phase": phase,
            "tick": tick,
            "ok": False,
            "error": "injected_process_exit",
            "unknown": backend.spec["strategy"] == "at_least_once",
            "durable_state": state,
        }
        write_json(root / f"{phase}.json", outcome)
    return result


def revocation_phase(root, phase, tick):
    backend = SyntheticRedmine(root, phase, tick)
    identity = TaskContract.model_validate(backend.spec["contract"])
    runtime = root / "runtime"
    outcome = {"phase": phase, "tick": tick, "pid": os.getpid(), "ok": True}
    with (
        redmine_store(runtime) as store,
        RedmineClient(
            RedmineSettings(url=identity.service_url, api_key="synthetic-key"),
            store=store,
            transport=httpx.MockTransport(backend.handle),
        ) as client,
    ):
        try:
            version = backend.spec["expected_version"]
            if phase == "queue":
                calls = [
                    *tool_call(
                        "add_ticket_note",
                        {"note": "Queued note.", "expected_version": version},
                        "queued-note",
                    ).tool_calls,
                    *tool_call(
                        "change_ticket_status",
                        {"status_id": 2, "expected_version": version},
                        "queued-status",
                    ).tool_calls,
                ]
                with session_storage(runtime, identity.thread_id) as (saver, _):
                    graph = build_desk_agent(
                        ScriptedChatModel(responses=[AIMessage(content="", tool_calls=calls)]),
                        client,
                        identity,
                        checkpointer=saver,
                    )
                    config = {"configurable": {"thread_id": identity.thread_id}}
                    graph.invoke(
                        {
                            "messages": [HumanMessage(content="Append the authorized note.")],
                            "contract": identity.model_dump(mode="json"),
                            "receipts": {},
                            "observations": {},
                            "pending_actions": ["Read investigation logs."],
                            "handoff_summary": "Retain status and owner.",
                        },
                        config,
                        interrupt_before=["tools"],
                    )
                    outcome["queued_nodes"] = list(graph.get_state(config).next)
                backend.event("queued_tools", ids=[call["id"] for call in calls])
            elif phase in {"locked_revoke", "revoke"}:
                outcome["result"] = revoke_session_writes(runtime, client, identity)
                backend.event("revoke_committed", result=outcome["result"])
            else:
                model = ScriptedChatModel(responses=["Unused stale contract probe."])
                try:
                    execute_session(runtime, client, identity, model, "Resume.", resume=True)
                except BusinessError as exc:
                    outcome["stale_contract_error"] = exc.code
                outcome["stale_model_requests"] = len(model.requests)
                persisted = load_session(runtime, client, identity)
                contract = TaskContract.model_validate(persisted["contract"])
                outcome["cancelled"] = {
                    message.tool_call_id: json.loads(message.text)
                    for message in persisted["messages"]
                    if isinstance(message, ToolMessage)
                }
                model = ScriptedChatModel(
                    responses=[
                        tool_call(
                            "add_ticket_note",
                            {"note": "Post-revoke note.", "expected_version": version},
                            "denied-note",
                        ),
                        tool_call(
                            "change_ticket_status",
                            {"status_id": 2, "expected_version": version},
                            "denied-status",
                        ),
                        tool_call("reconcile_ticket_operations", {}, "readonly-reconcile"),
                        "Read-only recovery complete.",
                    ]
                )
                outcome["result"] = execute_session(
                    runtime,
                    client,
                    contract,
                    model,
                    "Read current evidence.",
                    resume=True,
                )
                outcome["tool_results"] = {
                    message.tool_call_id: json.loads(message.text)
                    for message in model.requests[-1]
                    if isinstance(message, ToolMessage)
                }
            if phase != "locked_revoke":
                write_json(
                    root / f"{phase}.session.json",
                    serialized_session(runtime, client, identity),
                )
        except Exception as exc:
            outcome.update(ok=False, error=getattr(exc, "code", type(exc).__name__))
            outcome["traceback"] = traceback.format_exc()
    backend.event("revocation_phase_end", outcome=outcome)
    write_json(root / f"{phase}.json", outcome)
    return outcome


def run_revocation(root, fixture=None):
    spec = create_case(root, "queued-revocation", "reconcile", "none", fixture=fixture)
    child(root, "queue", 0, revoke=True)
    identity = TaskContract.model_validate(spec["contract"])
    with session_storage(root / "runtime", identity.thread_id):
        child(root, "locked_revoke", 1, revoke=True)
    child(root, "revoke", 2, revoke=True)
    child(root, "resume", 3, revoke=True)
    return score_revocation(root)


def export_events(root, events):
    with (root / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=True) + "\n")


def score_case(root):
    backend = SyntheticRedmine(root, "score", 4)
    spec, events, final = backend.spec, backend.events(), backend.issue()
    intended = [
        journal
        for journal in final["journals"]
        if journal["notes"] == spec["body"]["issue"]["notes"]
        and journal["user"]["id"] == spec["contract"]["principal_id"]
    ]
    preserved = all(
        final[key] == spec["fixture"]["issue"][key]
        for key in ("id", "project", "status", "assigned_to", "relations")
    )
    phases = sorted(
        [
            read_json(path)
            for path in root.glob("*.json")
            if path.stem
            in {
                "initial",
                "recover",
                "observe",
            }
        ],
        key=lambda phase: phase["tick"],
    )
    contract = TaskContract.model_validate(spec["contract"])
    with redmine_store(root / "runtime") as store:
        if spec["strategy"] == "at_least_once":
            state = baseline_state(store, contract)
            receipts = []
            acknowledgements = [state["ack"]] if state and state["ack"] else []
            unknown = bool(state and state["sends"] and state["ack"] is None)
        else:
            state = OperationRecords(store).load(contract.thread_id)
            receipts = list(state["receipts"].values())
            acknowledgements = []
            unknown = state["pending"] is not None
    unknown_ticks = sum(
        right["tick"] - left["tick"]
        for left, right in zip(phases, phases[1:], strict=False)
        if left["unknown"]
    )
    faults = [event for event in events if event["kind"] in {"fault_hit", "checkpoint_gap"}]
    result = {
        "operation_id": spec["operation_id"],
        "strategy": spec["strategy"],
        "fault": spec["fault"],
        "http_requests": sum(event["kind"] == "http_request" for event in events),
        "http_reads": sum(
            event["kind"] == "http_request" and event["method"] == "GET" for event in events
        ),
        "http_writes": sum(event["kind"] == "write_admitted" for event in events),
        "server_commits": sum(event["kind"] == "server_commit" for event in events),
        "journal_count": len(final["journals"]),
        "intended_journals": len(intended),
        "business_complete": bool(intended) and preserved,
        "exactly_once": len(intended) == 1 and preserved,
        "duplicate_entries": max(0, len(intended) - 1),
        "duplicate_operation": len(intended) > 1,
        "fault_hits": len(faults),
        "fault_hit": bool(faults),
        "commit_then_loss_hits": sum(
            event["kind"] == "fault_hit" and event.get("committed") is True for event in faults
        ),
        "http_acknowledgements": acknowledgements,
        "production_receipts": receipts,
        "http_ack_count": len(acknowledgements),
        "verified_receipt_count": len(receipts),
        "observer_unique_journal": len(intended) == 1,
        "final_unknown": unknown,
        "unknown_at_any_phase": any(p["unknown"] for p in phases),
        "unknown_logical_ticks": unknown_ticks,
        "phase_count": len(phases),
        "unexpected_errors": sum(p.get("unexpected", False) for p in phases),
        "phases": phases,
    }
    write_json(root / "final-server.json", final)
    write_json(root / "result.json", result)
    export_events(root, events)
    return result


def score_revocation(root):
    backend = SyntheticRedmine(root)
    events = backend.events()
    phases = {
        phase: read_json(root / f"{phase}.json") if (root / f"{phase}.json").exists() else {}
        for phase in ("queue", "locked_revoke", "revoke", "resume")
    }
    boundaries = [event["seq"] for event in events if event["kind"] == "revoke_committed"]
    boundary = boundaries[0] if len(boundaries) == 1 else None
    post = [event for event in events if boundary is not None and event["seq"] > boundary]
    resume = phases["resume"]
    cancelled = resume.get("cancelled", {})
    probes = resume.get("tool_results", {})
    checks = {
        "queued_two_tools": phases["queue"].get("queued_nodes") == ["tools", "tools"],
        "locked_revoke_busy": phases["locked_revoke"].get("error") == "session_busy",
        "locked_revoke_zero_http": all(
            event["phase"] != "locked_revoke" or event["kind"] != "http_request" for event in events
        ),
        "revoke_success": phases["revoke"].get("ok") is True and boundary is not None,
        "stale_contract_rejected": resume.get("stale_contract_error") == "contract_changed",
        "stale_model_unused": resume.get("stale_model_requests") == 0,
        "queued_writes_cancelled": all(
            cancelled.get(key, {}).get("error") == "session_writes_revoked"
            for key in ("queued-note", "queued-status")
        ),
        "fresh_probes_denied": all(
            probes.get(key, {}).get("error") == "write_not_authorized"
            for key in ("denied-note", "denied-status")
        ),
        "readonly_reconcile_succeeded": probes.get("readonly-reconcile", {}).get("ok") is True,
        "zero_post_revoke_writes": boundary is not None
        and not any(event["kind"] == "write_admitted" for event in post),
    }
    result = {
        "independent_schedules": 1,
        "checks": checks,
        "passed": all(checks.values()),
        "revoke_boundary_seq": boundary,
        "post_revoke_http_requests": sum(event["kind"] == "http_request" for event in post),
        "post_revoke_http_writes": sum(event["kind"] == "write_admitted" for event in post),
        "all_http_writes": sum(event["kind"] == "write_admitted" for event in events),
        "child_processes": len(list(root.glob("*.process.json"))),
        "phases": phases,
    }
    write_json(root / "result.json", result)
    export_events(root, events)
    return result


SUM_FIELDS = (
    "http_requests",
    "http_reads",
    "http_writes",
    "server_commits",
    "journal_count",
    "business_complete",
    "exactly_once",
    "duplicate_entries",
    "duplicate_operation",
    "fault_hits",
    "commit_then_loss_hits",
    "http_ack_count",
    "verified_receipt_count",
    "observer_unique_journal",
    "final_unknown",
    "unknown_at_any_phase",
    "unknown_logical_ticks",
    "unexpected_errors",
)


def summarize(root):
    frozen = read_json(root / "freeze.json")
    protocol = read_json(root / "protocol.json")
    cases = [score_case(path.parent) for path in sorted((root / "main").glob("*/*/case.json"))]
    main = {}
    for strategy in STRATEGIES:
        main[strategy] = {}
        for cohort in ("all", "normal", "fault"):
            selected = [
                case
                for case in cases
                if case["strategy"] == strategy
                and (cohort == "all" or case["fault_hit"] == (cohort == "fault"))
            ]
            main[strategy][cohort] = {
                "logical_operations": len(selected),
                **{key: sum(case[key] for case in selected) for key in SUM_FIELDS},
            }
    extras = [
        score_case(path.parent) for path in sorted((root / "deterministic").glob("*/*/case.json"))
    ]
    revocation = score_revocation(root / "revocation")
    checks = {
        "frozen_protocol_hash": sha256(root / "protocol.json") == frozen["protocol_sha256"],
        "all_main_slots": len(cases) == 2 * protocol["main_operations_per_strategy"],
        "all_main_phases": all(case["phase_count"] == 2 for case in cases),
        "main_fault_hits": all(main[s]["all"]["commit_then_loss_hits"] == 50 for s in STRATEGIES),
        "all_main_business_complete": all(case["business_complete"] for case in cases),
        "production_exactly_once": all(
            case["exactly_once"] for case in cases if case["strategy"] == "reconcile"
        ),
        "main_no_unknown": all(not case["final_unknown"] for case in cases),
        "raw_counts_consistent": all(
            case["http_writes"] == case["server_commits"] == case["journal_count"]
            for case in [*cases, *extras]
        ),
        "four_deterministic_cases": len(extras) == 4,
        "all_deterministic_phases": all(
            case["phase_count"] == (3 if case["fault"] == "late_commit" else 2) for case in extras
        ),
        "deterministic_business_complete": all(case["business_complete"] for case in extras),
        "deterministic_production_exactly_once": all(
            case["exactly_once"] for case in extras if case["strategy"] == "reconcile"
        ),
        "no_unexpected_phase_errors": all(
            case["unexpected_errors"] == 0 for case in [*cases, *extras]
        ),
        "revocation_passed": revocation["passed"],
    }
    checks["deterministic_new_processes"] = all(
        len(
            {
                event["pid"]
                for event in SyntheticRedmine(path.parent).events()
                if event["kind"] == "phase_start"
            }
        )
        == (3 if "late_commit" in path.parts else 2)
        for path in (root / "deterministic").glob("*/*/case.json")
    )
    checks["same_loss_sequence"] = all(
        case["fault_hit"]
        == (int(case["operation_id"].split("-")[-1]) in protocol["loss_operation_indices"])
        for case in cases
    )
    checks["child_exits"] = all(
        read_json(path)["exit_code"]
        == (
            GAP_EXIT
            if path.name == "initial.process.json" and "checkpoint_gap" in path.parts
            else 0
        )
        for path in root.rglob("*.process.json")
    )
    summary = {
        "protocol_id": protocol["protocol_id"],
        "service": protocol["service"],
        "planned_main_slots": 200,
        "observed_main_slots": len(cases),
        "main": main,
        "deterministic": extras,
        "revocation": revocation,
        "checks": checks,
        "passed": all(checks.values()),
        "external_model_requests": 0,
        "real_business_writes": 0,
    }
    write_json(root / "summary.json", summary)
    with (root / "operations.csv").open("w", encoding="utf-8", newline="") as handle:
        columns = ["operation_id", "strategy", "fault", *SUM_FIELDS]
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([*cases, *extras])
    report = [
        "# ServiceDesk 业务故障与撤权实验",
        "",
        "本报告来自固定合成 Redmine REST 服务、生产 RedmineClient/SqliteStore 与会话恢复路径。",
        "主实验含 100 对独立逻辑备注操作；每种策略 50 个正常操作、50 个提交后丢响应操作。",
        "结果按逻辑操作计量，描述该固定故障机制；真实 Redmine 撤权证据保持独立。",
        "",
        "| 策略 / 子集 | 操作 | HTTP请求 | 写请求 | 提交/journal |"
        " 完成 | 恰好一次 | 重复条数 | 最终未知 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for strategy in STRATEGIES:
        for cohort in ("normal", "fault", "all"):
            row = main[strategy][cohort]
            report.append(
                f"| {strategy} / {cohort} | {row['logical_operations']} | "
                f"{row['http_requests']} | {row['http_writes']} | {row['server_commits']} | "
                f"{row['business_complete']} | {row['exactly_once']} | "
                f"{row['duplicate_entries']} | {row['final_unknown']} |"
            )
    report.extend(
        [
            "",
            "## 回执与未知状态",
            "",
            "基线持久化 HTTP 成功确认；生产方案持久化经过 journal 校验的回执。"
            "同一逻辑操作的额外 journal 计为重复条数，HTTP 确认与已验证回执分别保留。",
        ]
    )
    for strategy in STRATEGIES:
        row = main[strategy]["all"]
        report.append(
            f"- {strategy}：HTTP 确认 {row['http_ack_count']}，生产校验回执 "
            f"{row['verified_receipt_count']}，唯一 journal 证据 "
            f"{row['observer_unique_journal']}/100；阶段末曾未知 "
            f"{row['unknown_at_any_phase']}/100，"
            f"未知持续 {row['unknown_logical_ticks']} 逻辑 tick。"
        )
    report.extend(
        [
            "",
            "逻辑 tick 表示预设调度间隔；本实验的耗时口径为逻辑调度，"
            "网络延迟与墙钟 MTTR 留待独立采集。"
            "主实验丢失率为首发操作的 50/100，重发响应均正常送达。",
            "",
            "## 确定性恢复",
            "",
            "| 案例 | 策略 | 写请求 | journal | 重复 | 完成 | 最终未知 |",
            "| --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for case in extras:
        report.append(
            f"| {case['fault']} | {case['strategy']} | {case['http_writes']} | "
            f"{case['journal_count']} | {case['duplicate_entries']} | "
            f"{case['business_complete']} | {case['final_unknown']} |"
        )
    report.extend(
        [
            "",
            "迟到提交在 tick 2 释放，观察至 tick 3；checkpoint 间隙通过进程退出码 86 注入。"
            "各案例都从新 Python 进程打开持久状态，生产案例执行 execute_session(resume=True)"
            " 与 reconcile_ticket_operations。每个案例每种策略各 1 次，独立于主实验比例。",
            "",
            "## 撤权调度",
            "",
            f"1 个确定性调度、{revocation['child_processes']} 个子进程："
            "持久化两个排队写工具，锁内撤权返回 session_busy，释放锁后撤权事务成功，"
            "新进程拒绝旧授权并按持久只读合同恢复。",
            f"撤权成功返回之后记录 {revocation['post_revoke_http_requests']} 次 HTTP 请求、"
            f"{revocation['post_revoke_http_writes']} 次 HTTP 写准入。"
            "两个排队写调用取消，两个新写探测按授权拒绝；撤权时在途写为 0。",
            "该调度测量排队工具与进程恢复行为。已执行写入的抢占语义另行验证；检查项数量单独保留。",
            "",
            "既有真实 Redmine 撤权记录："
            "[revocation-live-20261105](../revocation-live-20261105)，"
            "本次合成数据单独计量。",
            "",
            "## 复现与核算",
            "",
            "```bash",
            "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/benchmark_business_reliability.py "
            "run --output results/runs/business-reliability-20261106-reproduce",
            f"PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/benchmark_business_reliability.py "
            f"summarize {root.relative_to(REPO)}",
            "```",
            "",
            "freeze.json 固定协议、夹具、源码 SHA 与依赖版本；source.zip 保存执行源码。"
            "每个操作保留 case.json、server.sqlite、events.jsonl、阶段结果和生产 memory.sqlite；"
            "会话案例另保留原生 checkpoint、trace、子进程退出和标准输出/错误。",
            f"协议验收：{'通过' if summary['passed'] else '存在失败，详见 summary.json checks'}。"
            "全部运行及故障轨迹保留；失败进入原定分母。",
        ]
    )
    (root / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return summary


def freeze(root):
    root.mkdir(parents=True, exist_ok=False)
    sources = [
        REPO / "scripts/benchmark_business_reliability.py",
        REPO / "tests/test_business_reliability_benchmark.py",
        REPO / "pyproject.toml",
        REPO / "uv.lock",
        *sorted((REPO / "src/servicedesk_agent").rglob("*.py")),
        *sorted((REPO / "src/servicedesk_agent/assets").rglob("*")),
        *sorted(FIXTURES.glob("*")),
    ]
    hashes = {str(path.relative_to(REPO)): sha256(path) for path in sources if path.is_file()}
    with zipfile.ZipFile(root / "source.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:
            archive.write(REPO / name, name)
    for name in ("protocol.json", "redmine.json"):
        (root / name).write_bytes((FIXTURES / name).read_bytes())
    data = {
        "created_utc": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            text=True,
        ).strip(),
        "worktree_status": subprocess.check_output(
            ["git", "status", "--short"],
            cwd=REPO,
            text=True,
        ),
        "python": sys.version,
        "platform": platform.platform(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in (
                "httpx",
                "pydantic",
                "deepagents",
                "langgraph",
                "langchain-core",
                "langgraph-checkpoint-sqlite",
            )
        },
        "source_hashes": hashes,
        "protocol_sha256": sha256(root / "protocol.json"),
        "fixture_sha256": sha256(root / "redmine.json"),
        "source_archive_sha256": sha256(root / "source.zip"),
    }
    write_json(root / "freeze.json", data)


def run(root):
    freeze(root)
    protocol, fixture = read_json(root / "protocol.json"), read_json(root / "redmine.json")
    for index in range(protocol["main_operations_per_strategy"]):
        fault = "response_loss" if index in protocol["loss_operation_indices"] else "none"
        order = STRATEGIES if index % 2 == 0 else STRATEGIES[::-1]
        for strategy in order:
            case = root / "main" / f"note-{index:03d}" / strategy
            create_case(case, f"note-{index:03d}", strategy, fault, fixture=fixture)
            run_phase(case, "initial", 0)
            run_phase(case, "recover", 1)
        if index % 20 == 19:
            print(f"main pairs completed: {index + 1}/100", flush=True)
    for fault in ("late_commit", "checkpoint_gap"):
        for strategy in STRATEGIES:
            case = root / "deterministic" / fault / strategy
            create_case(case, fault, strategy, fault, native=True, fixture=fixture)
            child(case, "initial", 0)
            child(case, "recover", 1)
            if fault == "late_commit":
                SyntheticRedmine(case, "release", 2).release()
                child(case, "observe", 3)
    run_revocation(root / "revocation", fixture)
    return summarize(root)


def output_path(value):
    root = Path(value).resolve()
    if root.parent != REPO / "results/runs" or not root.name.startswith(
        "business-reliability-20261106"
    ):
        raise ValueError("Use results/runs/business-reliability-20261106* for output.")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--output", required=True, type=output_path)
    summary = sub.add_parser("summarize")
    summary.add_argument("root", type=output_path)
    for command in ("_phase", "_revocation"):
        worker = sub.add_parser(command)
        worker.add_argument("root", type=Path)
        worker.add_argument("phase")
        worker.add_argument("tick", type=int)
    args = parser.parse_args()
    if args.action in {"_phase", "_revocation"}:
        result = (run_phase if args.action == "_phase" else revocation_phase)(
            args.root,
            args.phase,
            args.tick,
        )
        print(json.dumps({"ok": result["ok"], "pid": os.getpid()}))
        return int(result.get("unexpected", False))
    result = run(args.output) if args.action == "run" else summarize(args.root)
    print(json.dumps({"passed": result["passed"], "main": result["main"]}, indent=2))
    return int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
