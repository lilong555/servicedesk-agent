"""Local CLI for service desk work, recovery and development evaluation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import ExitStack
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from servicedesk_agent.config import ModelSettings, RedmineSettings, state_dir
from servicedesk_agent.domain import BusinessError, TaskContract
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import execute_session, load_session


def _ticket_args(parser, *, session=False):
    parser.add_argument("--ticket", type=int, required=True)
    if session:
        parser.add_argument("--session", required=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="desk-agent", description=__doc__)
    root.add_argument("--env-file", type=Path, action="append", default=[])
    commands = root.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser(
        "doctor", help="Check dependencies, model configuration and Redmine."
    )
    doctor.add_argument("--ticket", type=int)
    ticket = commands.add_parser("ticket", help="Read the current ticket without a model call.")
    _ticket_args(ticket)
    start = commands.add_parser("start", help="Start a ticket session with explicit action grants.")
    _ticket_args(start, session=True)
    start.add_argument("--message", required=True)
    start.add_argument("--constraint", action="append", default=[])
    start.add_argument("--allow-notes", action="store_true")
    start.add_argument("--allow-status", type=int, action="append", default=[])
    start.add_argument("--allow-close", action="store_true")
    start.add_argument("--components", action="store_true")
    resume = commands.add_parser(
        "resume", help="Resume the stored task contract and unfinished work."
    )
    _ticket_args(resume, session=True)
    resume.add_argument(
        "--message", default="Continue the saved task while preserving its constraints."
    )
    status = commands.add_parser("status", help="Read the saved handoff without a model call.")
    _ticket_args(status, session=True)
    remember = commands.add_parser(
        "remember", help="Store a preference explicitly confirmed by you."
    )
    _ticket_args(remember)
    remember.add_argument("--text", required=True)
    demo = commands.add_parser("demo", help="Verify two-process handoff on real Redmine.")
    demo.add_argument("--access", type=Path, default=Path(".runtime/redmine-access.json"))
    demo.add_argument("--output", type=Path)
    demo.add_argument("--live", action="store_true")
    commands.add_parser("mcp-serve", help="Expose read-only Redmine tools over MCP stdio.")
    bench = commands.add_parser("bench", help="Fixed CRM development tasks.")
    bench_commands = bench.add_subparsers(dest="bench_command", required=True)
    for name in ("select", "smoke", "run"):
        sub = bench_commands.add_parser(name)
        sub.add_argument("--source", type=Path, default=Path("vendor/crm-agent-bench"))
        if name != "select":
            sub.add_argument("--output", type=Path, required=True)
            sub.add_argument("--groups", nargs="+", choices=["A", "B"], default=["A", "B"])
        if name == "run":
            sub.add_argument(
                "--split", choices=["calibration", "development"], default="calibration"
            )
            sub.add_argument("--max-calls", type=int, default=300)
            sub.add_argument("--max-turns", type=int, default=15)
    return root


def _identity(client, args) -> TaskContract:
    return TaskContract(
        service_url=client.settings.url,
        project_id=client.settings.project_id,
        principal_id=client.current_user()["id"],
        ticket_id=args.ticket,
        session_id=getattr(args, "session", "preferences"),
    )


def _bundle(stack, contract, root):
    if contract.variant == "A":
        return None
    from langgraph.store.sqlite import SqliteStore

    from servicedesk_agent.components import build_components
    from servicedesk_agent.knowledge import PolicyCatalog

    catalog = os.getenv("DESK_POLICY_CATALOG")
    scope = os.getenv("DESK_KNOWLEDGE_SCOPE")
    if not catalog or not scope:
        raise ValueError("Set DESK_POLICY_CATALOG and DESK_KNOWLEDGE_SCOPE for component group B.")
    store = stack.enter_context(SqliteStore.from_conn_string(str(root / "memory.sqlite")))
    store.setup()
    return build_components(contract.scope, store, PolicyCatalog(Path(catalog), scope))


def _receipt_summary(receipts):
    return [
        {
            "operation_id": item["operation_id"],
            "action": item["action"],
            "journal_id": item["journal_id"],
            "source_uri": item["after"]["evidence"]["source_uri"],
            "source_version": item["after"]["evidence"]["source_version"],
            "observed_at": item["observed_at"],
        }
        for item in receipts
    ]


def dispatch(args):
    if args.command == "mcp-serve":
        from servicedesk_agent.mcp_server import main

        main()
        return None
    if args.command == "demo":
        from servicedesk_agent.demo import run_demo

        output = args.output or Path("artifacts") / f"redmine-demo-{uuid4().hex[:8]}"
        return run_demo(args.access, output, live=args.live)
    if args.command == "bench":
        from servicedesk_agent.benchmark import load_fixed_tasks, subset_manifest
        from servicedesk_agent.evaluation import run

        if args.bench_command == "select":
            tasks = load_fixed_tasks(args.source, "all")
            return {
                **subset_manifest(),
                "task_count": len(tasks),
                "development_count": 9,
                "holdout_count": 12,
            }
        return run(
            args.source,
            args.output,
            live=args.bench_command == "run",
            groups=tuple(args.groups),
            split=getattr(args, "split", "calibration"),
            max_calls=getattr(args, "max_calls", 300),
            max_turns=getattr(args, "max_turns", 15),
        )
    model_settings = ModelSettings.from_env()
    redmine_settings = RedmineSettings.from_env()
    if args.command == "doctor":
        model_ready = True
        try:
            model_settings.require_live()
        except ValueError:
            model_ready = False
        result = {
            "python": sys.version.split()[0],
            "versions": {
                name: version(name)
                for name in ("deepagents", "langgraph", "langgraph-checkpoint-sqlite")
            },
            "live_model_configured": model_ready,
            "model_name": model_settings.name or None,
            "redmine_configured": bool(redmine_settings.api_key.get_secret_value()),
        }
        if result["redmine_configured"]:
            try:
                with RedmineClient(redmine_settings) as client:
                    result["redmine_principal_id"] = client.current_user()["id"]
                    if args.ticket:
                        ticket = client.get_ticket(args.ticket)
                        result["ticket"] = {"id": ticket.id, "status": ticket.status_name}
            except BusinessError as exc:
                result["redmine_error"] = exc.as_dict()
        return result
    if not redmine_settings.api_key.get_secret_value():
        raise ValueError("Set DESK_REDMINE_API_KEY or select the local Redmine environment file.")
    with ExitStack() as stack:
        client = stack.enter_context(RedmineClient(redmine_settings))
        if args.command == "ticket":
            return client.get_ticket(args.ticket).model_dump(mode="json")
        root = state_dir()
        identity = _identity(client, args)
        if args.command == "remember":
            from langgraph.store.sqlite import SqliteStore

            from servicedesk_agent.components import remember_confirmed_preference

            client.get_ticket(args.ticket)
            store = stack.enter_context(SqliteStore.from_conn_string(str(root / "memory.sqlite")))
            store.setup()
            return {"result": remember_confirmed_preference(identity.scope, args.text, store)}
        if args.command in {"resume", "status"}:
            state = load_session(root, client, identity)
            if not state:
                raise BusinessError("session_missing", "No saved session matches this identity.")
            contract = TaskContract.model_validate(state["contract"])
        else:
            contract = TaskContract.model_validate(
                {
                    **identity.model_dump(mode="json"),
                    "variant": "B" if args.components else "A",
                    "allow_notes": args.allow_notes,
                    "allowed_status_ids": args.allow_status,
                    "allow_close": args.allow_close,
                    "user_constraints": args.constraint,
                }
            )
        if args.command == "status":
            return {
                "contract": state["contract"],
                "pending_actions": state.get("pending_actions", []),
                "handoff_summary": state.get("handoff_summary", ""),
                "verified_receipts": _receipt_summary(list(state.get("receipts", {}).values())),
            }
        model = model_settings.build()
        stack.callback(model.root_client.close)
        bundle = _bundle(stack, contract, root)
        result = execute_session(
            root,
            client,
            contract,
            model,
            args.message,
            resume=args.command == "resume",
            components=bundle,
            model_settings=model_settings,
        )
        result["verified_receipts"] = _receipt_summary(result["verified_receipts"])
        return result


def main() -> None:
    args = parser().parse_args()
    load_dotenv(".env", override=False)
    for env_file in args.env_file:
        if not env_file.is_file():
            raise SystemExit(f"Environment file not found: {env_file}")
        load_dotenv(env_file, override=True)
    try:
        result = dispatch(args)
    except BusinessError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
