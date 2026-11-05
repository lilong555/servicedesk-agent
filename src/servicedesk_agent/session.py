"""Process-safe ownership around the native SQLite checkpointer."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START

from servicedesk_agent.agent import build_desk_agent
from servicedesk_agent.components import ComponentBundle
from servicedesk_agent.domain import BusinessError, TaskContract
from servicedesk_agent.testing import ScriptedChatModel
from servicedesk_agent.usage import UsageRecorder


class _AtomicSqliteSaver(SqliteSaver):
    @contextmanager
    def cursor(self, transaction: bool = True):
        # The owning session commits all native checkpoint updates together.
        with super().cursor(transaction=False) as cursor:
            yield cursor


@contextmanager
def session_storage(root: Path, thread_id: str, *, atomic: bool = False):
    directory = root / "sessions" / thread_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "session.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BusinessError("session_busy", "Another process owns this session.") from exc
        try:
            saver_type = _AtomicSqliteSaver if atomic else SqliteSaver
            with saver_type.from_conn_string(str(directory / "checkpoint.sqlite")) as saver:
                if atomic:
                    saver.setup()
                    with saver.conn:
                        yield saver, directory
                else:
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


def _superseded_tool_results(
    messages,
    *,
    error: str = "superseded_by_user_input",
    message: str | None = None,
) -> list[ToolMessage]:
    answered = {item.tool_call_id for item in messages if isinstance(item, ToolMessage)}
    results = []
    for item in messages:
        if not isinstance(item, AIMessage):
            continue
        for call in (*item.tool_calls, *item.invalid_tool_calls):
            if call["id"] is None or call["id"] in answered:
                continue
            results.append(
                ToolMessage(
                    content=json.dumps(
                        {
                            "ok": False,
                            "error": error,
                            "retry_cancelled": True,
                            "execution_outcome": "unknown",
                            "message": message
                            or (
                                "A new user message superseded this pending tool call. "
                                "Its execution or retry has been cancelled. No result was "
                                "recorded; earlier execution may have produced side effects. "
                                "Reconcile possible writes through read-only evidence and "
                                "verified receipts; keep unconfirmed outcomes unresolved. "
                                "Plan subsequent actions using the latest user message."
                            ),
                        }
                    ),
                    tool_call_id=call["id"],
                    name=call["name"] or "unknown",
                    status="error",
                )
            )
            answered.add(call["id"])
    return results


def revoke_session_writes(root: Path, client, identity: TaskContract) -> dict[str, Any]:
    with session_storage(root, identity.thread_id, atomic=True) as (saver, _):
        client._check_contract(identity)
        config = {"configurable": {"thread_id": identity.thread_id}}
        graph = build_desk_agent(ScriptedChatModel(), client, identity, checkpointer=saver)
        existing = graph.get_state(config)
        if not existing.values:
            raise BusinessError("session_missing", "Start this session before revoking writes.")
        stored = TaskContract.model_validate(existing.values["contract"])
        if stored.thread_id != identity.thread_id:
            raise BusinessError("contract_changed", "The saved contract has a different scope.")
        client._check_contract(stored)
        if stored.variant == "B":
            # Native Skills restores B's nodes and private channels without loading its
            # policy catalog, memory tools, skill sources, or external configuration.
            graph = build_desk_agent(
                ScriptedChatModel(),
                client,
                stored,
                checkpointer=saver,
                components=ComponentBundle(agent_kwargs={"skills": []}),
            )
            existing = graph.get_state(config)
        readonly = stored.model_copy(
            update={"allow_notes": False, "allowed_status_ids": (), "allow_close": False}
        )
        messages = _superseded_tool_results(
            existing.values.get("messages", []),
            error="session_writes_revoked",
            message=(
                "Session write permissions have been revoked. This pending tool call's "
                "execution or retry has been cancelled. No result was recorded; earlier "
                "execution may have produced side effects. Reconcile possible writes "
                "through read-only evidence and verified receipts; keep unconfirmed "
                "outcomes unresolved. Plan subsequent actions using the read-only contract."
            ),
        )
        if existing.tasks:
            # Fold completed sibling outputs into the checkpoint before changing authority.
            graph.update_state(config, None, as_node=END)
        if stored != readonly or messages:
            graph.update_state(
                config,
                {"contract": readonly.model_dump(mode="json"), "messages": messages},
                as_node=START,
            )
        stopped = graph.get_state(config)
        # Completed outputs folded by END can schedule a following model/middleware node.
        while stopped.tasks:
            graph.update_state(config, None, as_node=END)
            stopped = graph.get_state(config)
        state = dict(stopped.values)
        return {
            "thread_id": readonly.thread_id,
            "contract": state["contract"],
            "pending_actions": state.get("pending_actions", []),
            "handoff_summary": state.get("handoff_summary", ""),
            "verified_receipts": list(state.get("receipts", {}).values()),
        }


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
            if existing.tasks:
                # END folds in completed sibling outputs and clears queued tasks without
                # executing them. The live snapshot already includes those saved outputs.
                graph.update_state(config, None, as_node=END)
            messages = _superseded_tool_results(existing.values.get("messages", []))
            graph.invoke({"messages": [*messages, HumanMessage(content=message)]}, config)
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
