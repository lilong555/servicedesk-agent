"""Process-safe ownership around the native SQLite checkpointer."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage, messages_to_dict
from langgraph.checkpoint.sqlite import SqliteSaver

from servicedesk_agent.agent import build_desk_agent
from servicedesk_agent.domain import BusinessError, TaskContract
from servicedesk_agent.testing import ScriptedChatModel
from servicedesk_agent.usage import UsageRecorder


@contextmanager
def session_storage(root: Path, thread_id: str):
    directory = root / "sessions" / thread_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "session.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BusinessError("session_busy", "Another process owns this session.") from exc
        try:
            with SqliteSaver.from_conn_string(str(directory / "checkpoint.sqlite")) as saver:
                yield saver, directory
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_session(root: Path, client, identity: TaskContract) -> dict[str, Any]:
    with session_storage(root, identity.thread_id) as (saver, _):
        graph = build_desk_agent(ScriptedChatModel(), client, identity, checkpointer=saver)
        state = dict(graph.get_state({"configurable": {"thread_id": identity.thread_id}}).values)
        if state:
            stored = TaskContract.model_validate(state["contract"])
            if stored.thread_id != identity.thread_id:
                raise BusinessError("contract_changed", "The saved contract has a different scope.")
        return state


def execute_session(
    root: Path,
    client,
    contract: TaskContract,
    model,
    message: str,
    *,
    resume: bool = False,
    components=None,
    model_settings=None,
    recursion_limit: int = 60,
) -> dict[str, Any]:
    if not message.strip():
        raise ValueError("Provide a nonempty task message.")
    usage = UsageRecorder()
    with session_storage(root, contract.thread_id) as (saver, directory):
        config = {
            "configurable": {"thread_id": contract.thread_id},
            "recursion_limit": recursion_limit,
            "callbacks": [usage],
        }
        graph = build_desk_agent(model, client, contract, checkpointer=saver, components=components)
        existing = graph.get_state(config)
        if existing.values and not resume:
            raise BusinessError("session_exists", "Use resume to continue this existing session.")
        if not existing.values and resume:
            raise BusinessError("session_missing", "Start this session before resuming it.")
        if existing.values:
            stored = TaskContract.model_validate(existing.values["contract"])
            if stored != contract:
                raise BusinessError("contract_changed", "Resume with the persisted task contract.")
        client._check_contract(contract)
        if not existing.values:
            initial_state = components.initial_state if components is not None else {}
            if initial_state.keys() & {
                "contract",
                "receipts",
                "observations",
                "pending_actions",
                "handoff_summary",
                "messages",
            }:
                raise BusinessError(
                    "invalid_component_state", "Component state includes reserved session records."
                )
            graph.update_state(
                config,
                {
                    **initial_state,
                    "contract": contract.model_dump(mode="json"),
                    "pending_actions": [],
                    "handoff_summary": "",
                    "receipts": {},
                    "observations": {},
                },
            )
        run_id = uuid4().hex
        failure = None
        try:
            if existing.next:
                # Complete an interrupted native super-step before accepting a new user turn.
                graph.invoke(None, config)
            graph.invoke({"messages": [HumanMessage(content=message)]}, config)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                state = dict(graph.get_state(config).values)
                trace = {
                    "run_id": run_id,
                    "model_mode": "scripted" if isinstance(model, ScriptedChatModel) else "live",
                    "thread_id": contract.thread_id,
                    "error_type": type(failure).__name__ if failure is not None else None,
                    "usage": usage.summary(model_settings),
                    "model_calls": usage.calls,
                    "messages": messages_to_dict(state.get("messages", [])),
                    "receipt_ids": list(state.get("receipts", {})),
                }
                path = directory / f"trace-{run_id}.json"
                path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
                path.chmod(0o600)
            except Exception as exc:
                if failure is None:
                    raise
                failure.add_note(f"Session trace finalization failed ({type(exc).__name__}).")
        last = next(
            (item for item in reversed(state.get("messages", [])) if item.type == "ai"), None
        )
        return {
            "thread_id": contract.thread_id,
            "message": last.text if last is not None else "",
            "contract": state["contract"],
            "pending_actions": state.get("pending_actions", []),
            "handoff_summary": state.get("handoff_summary", ""),
            "verified_receipts": list(state.get("receipts", {}).values()),
            "usage": usage.summary(model_settings),
            "trace_path": str(path),
        }
