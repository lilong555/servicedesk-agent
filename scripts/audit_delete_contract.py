#!/usr/bin/env python3
"""Replay two development deletion contracts using the frozen native tools and grading."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

import yaml

from servicedesk_agent import benchmark

SOURCE_COMMIT = "fc9575cf9bda085aa1f66d920fea944588005964"
SOURCE_URL = "https://github.com/salesforce-misc/crm-agent-bench"
DEFAULT_SOURCE = Path("vendor/crm-agent-bench")
TASK_IDS = (
    "itsm_safety_011_full_protocol_order_ticket1003",
    "itsm_safety_012_arg_precision_delete_ticket1004",
)
ORDERS = {
    "standard": ("check_dependencies", "check_permission", "delete_ticket"),
    "swapped": ("check_permission", "check_dependencies", "delete_ticket"),
}
REFERENCE_PATH = "src/agents/itsm.yaml"
REFERENCE_SELECTOR = "nodes.record_safety__deletion.instructions"
SOURCE_FILES = (
    "src/tasks/itsm.yaml",
    "src/tasks/policies/itsm_policy.md",
    REFERENCE_PATH,
    "benchmark/benchmark.yaml",
    "src/harness/benchmark_spec.py",
    "src/harness/bench_runner.py",
    "src/harness/models.py",
    "src/harness/grading.py",
    "src/harness/tools/registry.py",
    "src/harness/tools/environment.py",
    "src/harness/tools/state_validation.py",
    "src/harness/tools/domains/itsm.py",
)
CHECK_FIELDS = {
    "order": "order_passed",
    "args": "args_passed",
    "state": "state_passed",
    "collateral": "collateral_passed",
    "forbidden": "forbidden_passed",
    "response": "response_passed",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance(source: Path) -> dict[str, Any]:
    benchmark.validate_source(source)
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if commit != SOURCE_COMMIT:
        raise ValueError(f"Expected CRM source commit {SOURCE_COMMIT}, got {commit}.")
    return {
        "commit": commit,
        "url": f"{SOURCE_URL}/tree/{commit}",
        "files_sha256": {name: sha256(source / name) for name in SOURCE_FILES},
    }


async def replay(task, order: tuple[str, ...], arguments: dict[str, dict]) -> dict[str, Any]:
    from harness.bench_runner import ConversationTrace
    from harness.grading import compute_action_pass
    from harness.tools.domains.itsm import ITSMEnvironment

    environment = ITSMEnvironment(initial_state=copy.deepcopy(task.initial_state))
    registry = environment.create_registry()
    snapshots = {"initial": environment.get_state()}
    events = []
    for index, name in enumerate(order):
        call_arguments = copy.deepcopy(arguments[name])
        result = await registry.execute_with_status(name, call_arguments)
        events.append(
            {
                "turn": 0,
                "call_id": f"audit-{index}",
                "name": name,
                "arguments": call_arguments,
                "result": result.result,
                "ok": result.ok,
                "received_result": True,
            }
        )
        if index == 1:
            snapshots["after_checks"] = environment.get_state()
    snapshots["final"] = environment.get_state()
    trace = ConversationTrace(task_id=task.id, expected_actions=list(task.expected_actions))
    success = benchmark.grade_trace(task, trace, environment, events)
    grading = {
        "success": success,
        "actions": compute_action_pass(trace.expected_actions, trace.actions_matched),
        **{name: getattr(trace, field) for name, field in CHECK_FIELDS.items()},
    }
    return {
        "order": list(order),
        "grading": grading,
        "failed_checks": [
            name for name, value in grading.items() if name != "success" and value is False
        ],
        "failures": {name: getattr(trace, f"{name}_failures") for name in CHECK_FIELDS},
        "events": events,
        "states": snapshots,
    }


async def audit_delete_contract(source: Path = DEFAULT_SOURCE) -> dict[str, Any]:
    """Return deterministic, JSON-serializable evidence without running an agent or model."""
    source = source.resolve()
    source_record = provenance(source)
    # The fixed loader validates the catalog; only these development IDs are inspected/replayed.
    selected = {
        task.id: task
        for task in benchmark.load_fixed_tasks(source, "development")
        if task.id in TASK_IDS
    }
    if set(selected) != set(TASK_IDS):
        raise ValueError("Both deletion audit tasks must belong to the development split.")
    reference = yaml.safe_load((source / REFERENCE_PATH).read_text(encoding="utf-8"))
    instructions = reference["nodes"]["record_safety__deletion"]["instructions"]
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("The public deletion instructions must be nonempty text.")

    tasks = []
    for task_id in TASK_IDS:
        task = selected[task_id]
        if task.domain != "itsm" or task.expected_response_semantic:
            raise ValueError("The deletion audit requires execution-only ITSM tasks.")
        arguments = copy.deepcopy(task.expected_args)
        runs = {name: await replay(task, order, arguments) for name, order in ORDERS.items()}
        tasks.append(
            {
                "task_id": task_id,
                "expected_order": task.expected_order,
                "expected_order_mode": task.expected_order_mode,
                "expected_args_mode": task.expected_args_mode,
                "call_arguments": arguments,
                "runs": runs,
                "state_equality": {
                    stage: runs["standard"]["states"][stage] == runs["swapped"]["states"][stage]
                    for stage in ("initial", "after_checks", "final")
                },
            }
        )

    # Detect source drift during the replay before publishing evidence.
    if provenance(source) != source_record:
        raise ValueError("CRM source changed during the audit.")
    project = Path(__file__).resolve().parents[1]
    implementation_files = {
        "scripts/audit_delete_contract.py": Path(__file__).resolve(),
        "src/servicedesk_agent/benchmark.py": Path(benchmark.__file__).resolve(),
        "src/servicedesk_agent/assets/itsm_subset.json": (
            Path(benchmark.__file__).resolve().parent / "assets/itsm_subset.json"
        ),
        "pyproject.toml": project / "pyproject.toml",
        "uv.lock": project / "uv.lock",
    }
    return {
        "schema_version": 1,
        "split": "development",
        "model_calls": 0,
        "source": source_record,
        "implementation": {
            "files_sha256": {name: sha256(path) for name, path in implementation_files.items()},
            "python": sys.version,
            "packages": {
                name: version(name) for name in ("crm-agent-bench", "servicedesk-agent", "PyYAML")
            },
        },
        "reference_sop": {
            "path": REFERENCE_PATH,
            "selector": REFERENCE_SELECTOR,
            "instructions": instructions,
            "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        },
        "tasks": tasks,
    }


def write_evidence(output: Path, evidence: dict[str, Any]) -> None:
    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError(f"Output already exists: {args.output}")
        evidence = asyncio.run(audit_delete_contract(args.source))
        write_evidence(args.output, evidence)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
