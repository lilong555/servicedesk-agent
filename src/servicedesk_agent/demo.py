"""Verify ticket handoff across two processes against an isolated real Redmine."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import httpx

from servicedesk_agent.components import build_components
from servicedesk_agent.config import ModelSettings, RedmineSettings
from servicedesk_agent.domain import TaskContract, Ticket, utc_now
from servicedesk_agent.knowledge import PolicyCatalog
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.provenance import capture_source
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session, load_session
from servicedesk_agent.testing import ScriptedChatModel, tool_call


def _json(path: Path, value) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_stage_delay(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 30
        or not math.isfinite(value)
    ):
        raise ValueError("stage_delay must be a finite number from 0 to 30 seconds.")
    return float(value)


def _note_from_observed_evidence(messages):
    ticket = None
    policy = None
    for message in messages:
        if message.type != "tool" or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except ValueError:
            continue
        if isinstance(payload, dict) and "ticket" in payload:
            ticket = payload["ticket"]
        if isinstance(payload, dict) and payload.get("source_kind") == "policy":
            policy = payload
    if ticket is None or policy is None:
        raise ValueError("The demo requires actual ticket and policy tool observations.")
    note = (
        f"VPN investigation: observed owner {ticket['assigned_to_name']}; "
        f"policy {policy['source_uri']}. "
        f"Authentication review: {
            'Identity Operations'
            if 'Identity Operations' in policy['content']
            else 'Network Operations'
        }. "
        "Gateway log sample is still pending. Keep this ticket open for the requester."
    )
    return tool_call(
        "add_ticket_note",
        {"note": note, "expected_version": ticket["evidence"]["source_version"]},
        f"note-{uuid4().hex}",
    )


def worker(
    access_path: Path, directory: Path, ticket_id: int, phase: str, *, live: bool = False
) -> dict:
    access = json.loads(access_path.read_text())
    settings = RedmineSettings(
        url="http://127.0.0.1:3080",
        api_key=access["agent_api_key"],
        project_id=access["project_id"],
    )
    with ExitStack() as stack:
        store = stack.enter_context(redmine_store(directory))
        client = stack.enter_context(RedmineClient(settings, store=store))
        identity = TaskContract(
            service_url=settings.url,
            project_id=settings.project_id,
            principal_id=client.current_user()["id"],
            ticket_id=ticket_id,
            session_id="vpn-handoff",
        )
        if phase == "initial":
            contract = identity.model_copy(
                update={
                    "variant": "B",
                    "allow_notes": True,
                    "allowed_status_ids": (2, 5),
                    "user_constraints": ("Investigate and add notes. Keep this ticket open.",),
                }
            )
            pending = ["Obtain the gateway log sample.", "Review the current owner and policy."]
            responses = []
        else:
            saved = load_session(directory, client, identity)
            contract = TaskContract.model_validate(saved["contract"])
            if "Obtain the gateway log sample." not in saved["pending_actions"]:
                raise AssertionError("The pending action was lost across processes.")
            pending = ["Obtain the gateway log sample.", "Await requester confirmation."]
            responses = [tool_call("read_handoff", {}, "handoff-read")]
        responses.extend(
            [
                tool_call("read_ticket", {}, f"ticket-{phase}"),
                tool_call("read_policy", {"policy_id": "vpn-investigation"}, f"policy-{phase}"),
                _note_from_observed_evidence,
                tool_call(
                    "save_handoff",
                    {
                        "pending_actions": pending,
                        "summary": "Investigation note verified; ticket open.",
                    },
                    f"save-{phase}",
                ),
                "The investigation note is verified. The ticket remains open.",
            ]
        )
        bundle = build_components(
            contract.scope,
            store,
            PolicyCatalog(directory / "policies.json", "demo-service-desk"),
        )
        model_settings = ModelSettings.from_env() if live else None
        model = model_settings.build() if live else ScriptedChatModel(responses=responses)
        message = (
            "Investigate the VPN problem and add one investigation note. Use the current "
            "vpn-investigation policy. In every investigation note include the current "
            "owner's full name, the policy source_uri, and the team responsible for "
            "reviewing authentication failures. Keep the ticket open. The gateway log "
            "sample is still unavailable; save this unresolved action verbatim: "
            "'Obtain the gateway log sample.'. Then pause for a later handoff."
            if phase == "initial"
            else "Continue the saved investigation and add an updated investigation note. "
            "The gateway log sample is still unavailable."
        )
        try:
            result = execute_session(
                directory,
                client,
                contract,
                model,
                message,
                resume=phase == "resume",
                components=bundle,
                model_settings=model_settings,
            )
        finally:
            if live:
                model.root_client.close()
        return {**result, "process_id": os.getpid()}


def _policy_observed(trace_path: str, source_uri: str, *, exclude_calls: set[str]) -> bool:
    trace = json.loads(Path(trace_path).read_text())
    for message in trace["messages"]:
        if message["type"] != "tool" or message["data"]["tool_call_id"] in exclude_calls:
            continue
        try:
            value = json.loads(message["data"]["content"])
        except (ValueError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            evidence = candidate.get("evidence", candidate)
            if evidence.get("source_kind") == "policy" and evidence.get("source_uri") == source_uri:
                return True
    return False


def run_demo(
    access_path: Path, output: Path, *, live: bool = False, stage_delay: float = 0
) -> dict:
    stage_delay = validate_stage_delay(stage_delay)
    model_settings = ModelSettings.from_env() if live else None
    if model_settings is not None:
        model_settings.require_live()
    access_path = access_path.resolve()
    access = json.loads(access_path.read_text())
    settings = RedmineSettings(
        url="http://127.0.0.1:3080",
        api_key=access["agent_api_key"],
        project_id=access["project_id"],
    )
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    state = output / "state"
    state.mkdir(mode=0o700)
    (output / "stages").mkdir(mode=0o700)
    stages = {
        name: str(output / "stages" / f"{name}.json")
        for name in (
            "01-created",
            "02-initial-completed",
            "03-external-changes",
            "04-resume-completed",
        )
    }
    _json(
        output / "run.json",
        {
            "schema_version": 1,
            "started_at": utc_now().isoformat(),
            "live": live,
            "model_mode": "live" if live else "scripted",
            "model": model_settings.model_dump(mode="json", exclude={"api_key"})
            if model_settings is not None
            else "scripted-fixture",
            "provenance": capture_source(output),
            "demo": {
                "stage_delay": stage_delay,
                "service_url": settings.url,
                "project_id": settings.project_id,
                "first_owner_id": access["first_owner_id"],
                "second_owner_id": access["second_owner_id"],
            },
            "stages": stages,
        },
    )
    versions = json.loads(files("servicedesk_agent").joinpath("assets/policies.json").read_text())
    versions[0]["valid_to"] = None
    _json(state / "policies.json", versions[:1])
    with (
        httpx.Client(
            base_url="http://127.0.0.1:3080/",
            headers={"X-Redmine-API-Key": access["admin_api_key"]},
            timeout=15,
            trust_env=False,
        ) as admin,
        RedmineClient(settings) as observer,
    ):
        project_response = admin.get(f"projects/{access['project_id']}.json")
        project_response.raise_for_status()
        if project_response.json()["project"]["identifier"] != "servicedesk-demo":
            raise ValueError("Use the isolated servicedesk-demo project.")
        created = admin.post(
            "issues.json",
            json={
                "issue": {
                    "project_id": access["project_id"],
                    "subject": f"VPN cross-session handoff {uuid4().hex[:8]}",
                    "description": "Investigate, add notes, and keep this ticket open.",
                    "assigned_to_id": access["first_owner_id"],
                }
            },
        )
        created.raise_for_status()
        ticket_id = created.json()["issue"]["id"]

        def snapshot(
            name: str, policy_version: int, *, expected_owner: int | None = None
        ) -> Ticket:
            ticket = observer.get_ticket(ticket_id)
            _json(
                Path(stages[name]),
                {
                    "schema_version": 1,
                    "stage": name,
                    "observed_at": ticket.evidence.observed_at.isoformat(),
                    "ticket": ticket.model_dump(mode="json"),
                    "policy_version": policy_version,
                },
            )
            if expected_owner is not None and ticket.assigned_to_id != expected_owner:
                raise AssertionError(f"The {name} ticket owner differs; inspect {stages[name]}.")
            return ticket

        def phase(name):
            command = [
                sys.executable,
                "-m",
                "servicedesk_agent.demo",
                name,
                "--access",
                str(access_path),
                "--state",
                str(state),
                "--ticket",
                str(ticket_id),
            ]
            if live:
                command.append("--live")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=360 if live else 90,
            )
            if result.returncode:
                _json(
                    output / f"{name}-error.json",
                    {
                        "returncode": result.returncode,
                        "stderr": result.stderr,
                    },
                )
                raise RuntimeError(f"The {name} process failed; inspect {output}.")
            data = json.loads(result.stdout)
            _json(output / f"{name}.json", data)
            return data

        snapshot("01-created", 1, expected_owner=access["first_owner_id"])
        if stage_delay:
            time.sleep(stage_delay)
        initial = phase("initial")
        snapshot("02-initial-completed", 1, expected_owner=access["first_owner_id"])
        if stage_delay:
            time.sleep(stage_delay)
        updated = admin.put(
            f"issues/{ticket_id}.json",
            json={
                "issue": {
                    "assigned_to_id": access["second_owner_id"],
                    "notes": "External owner change while the agent process was stopped.",
                }
            },
        )
        updated.raise_for_status()
        publication_time = utc_now().isoformat()
        versions[0]["valid_to"] = publication_time
        versions[1]["valid_from"] = publication_time
        _json(state / "policies.json", versions)
        snapshot("03-external-changes", 2, expected_owner=access["second_owner_id"])
        if stage_delay:
            time.sleep(stage_delay)
        resumed = phase("resume")
        final_ticket = snapshot("04-resume-completed", 2)

    original_ids = {item["operation_id"] for item in initial["verified_receipts"]}
    new_receipts = [
        item for item in resumed["verified_receipts"] if item["operation_id"] not in original_ids
    ]
    new_journals = {item["journal_id"] for item in new_receipts}
    resumed_notes = [
        journal.get("notes", "")
        for journal in final_ticket.journals
        if journal["id"] in new_journals
    ]
    initial_trace = json.loads(Path(initial["trace_path"]).read_text())
    original_calls = {
        message["data"]["tool_call_id"]
        for message in initial_trace["messages"]
        if message["type"] == "tool"
    }
    checks = {
        "separate_process_resume": initial["process_id"] != resumed["process_id"],
        "same_native_checkpoint": initial["thread_id"] == resumed["thread_id"],
        "current_owner_preserved": final_ticket.assigned_to_id == access["second_owner_id"],
        "ticket_kept_open": not final_ticket.is_closed,
        "keep_open_constraint_preserved": resumed["contract"]["allow_close"] is False,
        "pending_action_preserved": "Obtain the gateway log sample." in resumed["pending_actions"],
        "two_verified_actions": len(resumed["verified_receipts"]) == 2,
        "current_policy_read_on_resume": _policy_observed(
            resumed["trace_path"],
            versions[1]["source_uri"],
            exclude_calls=original_calls,
        ),
        "current_policy_and_owner_in_new_note": any(
            versions[1]["source_uri"] in note
            and "Identity Operations" in note
            and final_ticket.assigned_to_name in note
            for note in resumed_notes
        ),
    }
    summary = {
        "kind": "live_model_and_real_redmine" if live else "scripted_framework_and_real_redmine",
        "model": model_settings.name if model_settings is not None else "scripted-fixture",
        "external_model_requests": (
            initial["usage"]["calls"] + resumed["usage"]["calls"] if live else 0
        ),
        "phase_usage": {"initial": initial["usage"], "resume": resumed["usage"]},
        "ticket_url": f"http://localhost:3080/issues/{ticket_id}",
        "current_owner": final_ticket.assigned_to_name,
        "current_status": final_ticket.status_name,
        "checks": checks,
        "passed": all(checks.values()),
        "initial_trace": initial["trace_path"],
        "resumed_trace": resumed["trace_path"],
        "results_path": str(output),
        "stages": stages,
    }
    _json(output / "summary.json", summary)
    if not summary["passed"]:
        raise AssertionError("The Redmine handoff verification failed; inspect summary.json.")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["initial", "resume"])
    parser.add_argument("--access", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--ticket", type=int, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    result = worker(args.access, args.state, args.ticket, args.phase, live=args.live)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
