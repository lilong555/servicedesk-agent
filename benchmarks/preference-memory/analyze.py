"""Recompute preference-memory metrics from retained request, HTTP and session records."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import types
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRIVER = "scripts/benchmark_preference_memory.py"
SCENARIOS = "benchmarks/preference-memory/scenarios.json"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def verify_sources(run_freeze, freeze_path, workspace=ROOT):
    issues, sources = [], {}
    archived_freeze = read(freeze_path)
    protocol = run_freeze["protocol"]
    if archived_freeze != run_freeze:
        issues.append("freeze-record-mismatch")
    if digest(protocol) != run_freeze["freeze_id"]:
        issues.append("freeze-protocol-sha256-mismatch")
    if digest(protocol["source_files"]) != protocol["implementation_sha256"]:
        issues.append("freeze-implementation-sha256-mismatch")
    archive_path = freeze_path.parent / "source.tar.gz"
    if not archive_path.exists():
        issues.append("source-archive-missing")
    else:
        if sha(archive_path.read_bytes()) != run_freeze["source_archive_sha256"]:
            issues.append("source-archive-sha256-mismatch")
        try:
            with tarfile.open(archive_path) as archive:
                members = archive.getmembers()
                if len(members) != len({m.name for m in members}):
                    issues.append("source-archive-duplicate-members")
                for member in members:
                    if not member.isfile():
                        issues.append(f"source-archive-nonfile:{member.name}")
                        continue
                    sources[member.name] = archive.extractfile(member).read()
        except tarfile.TarError:
            issues.append("source-archive-invalid")
    if set(sources) != set(protocol["source_files"]):
        issues.append("source-archive-member-set-mismatch")
    for name, expected in protocol["source_files"].items():
        if name in sources and sha(sources[name]) != expected:
            issues.append(f"source-member-sha256-mismatch:{name}")
    scenario_matches = (
        SCENARIOS in sources and sha(sources[SCENARIOS]) == protocol["scenario_sha256"]
    )
    if not scenario_matches:
        issues.append("frozen-scenario-sha256-mismatch")
    # The driver and scenarios come from the verified archive; its production imports
    # still resolve through the workspace and must match that same frozen source.
    imported_sources = {
        name: expected
        for name, expected in protocol["source_files"].items()
        if name.startswith("src/servicedesk_agent/") or name == "pyproject.toml"
    }
    for name, expected in imported_sources.items():
        path = workspace / name
        if not path.exists() or sha(path.read_bytes()) != expected:
            issues.append(f"workspace-import-sha256-mismatch:{name}")
    binding = {
        "source_mode": "verified_frozen_archive",
        "freeze_record": str(freeze_path),
        "source_archive_sha256": sha(archive_path.read_bytes()) if archive_path.exists() else None,
        "verified_source_files": len(sources),
        "workspace_import_files": len(imported_sources),
        "frozen_scenario_sha256_matches": scenario_matches,
    }
    return sources, binding, issues


def save_result(output, result):
    output = Path(output).resolve()
    parent = output.parent
    if (
        parent.parent != ROOT / "results/runs"
        or not parent.name.startswith("preference-memory-20261106-")
        or output.suffix != ".json"
    ):
        raise ValueError("Use a new JSON file in a preference-memory-20261106-* audit directory.")
    if (parent / "summary.json").exists() or (parent / "freeze.json").exists():
        raise ValueError("Write audit output outside original run and freeze directories.")
    parent.mkdir(exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "output": str(output.relative_to(ROOT)),
                "started": result.get("started"),
                "calls": result.get("calls"),
                "issues": result["checks"]["issues"],
            }
        )
    )
    return result


def ratio(a, b):
    return {
        "available": a,
        "empty_denominator": b,
        "empty_minus_available": b - a,
        "reduction_percent": (b - a) / b * 100 if b else None,
    }


def provider_input_digest(messages):
    value = json.loads(json.dumps(messages))
    for batch in value:
        for message in batch:
            message["data"].pop("id", None)
    return digest(value)


def analyze(directory, freeze_path, output):
    directory = Path(directory).resolve()
    freeze_path = Path(freeze_path).resolve()
    freeze = read(directory / "freeze.json")
    sources, binding, issues = verify_sources(freeze, freeze_path)
    provenance = {
        "analyzer": {
            "path": str(Path(__file__).resolve().relative_to(ROOT)),
            "sha256": sha(Path(__file__).read_bytes()),
        },
        "source_binding": binding,
    }
    if issues:
        return save_result(
            output,
            {
                "source": str(directory.relative_to(ROOT)),
                **provenance,
                "checks": {"issues": issues},
            },
        )
    bench = types.ModuleType("frozen_preference_memory_benchmark_analysis")
    bench.__file__ = str(ROOT / DRIVER)
    exec(
        compile(sources[DRIVER], f"{freeze_path.parent}/source.tar.gz:{DRIVER}", "exec"),
        bench.__dict__,
    )
    summary = read(directory / "summary.json")
    schedule = freeze["protocol"][f"{summary['phase']}_schedule"]
    scenarios = {s["id"]: s for s in json.loads(sources[SCENARIOS])["scenarios"]}
    rows, issues = [], []
    request_ids = []
    schema_hashes = []
    for position, scheduled in enumerate(schedule):
        path = directory / (
            f"{position + 1:02d}-{scheduled['scenario']}-"
            f"{scheduled['condition']}-r{scheduled['repeat']}"
        )
        if not (path / "result.json").exists():
            issues.append(f"unstarted-or-unfinalized:{path.name}")
            continue
        row = bench.read(path / "result.json")
        calls = bench.read(path / "model_calls.json")
        requests = bench.read(path / "model_requests.json")
        http = bench.read(path / "http.json")
        writes = bench.read(path / "writes.json")
        sessions = bench.read(path / "sessions.json")
        setup = bench.read(path / "setup.json")
        searches = bench.read(path / "memory_searches.json")
        turns = bench.read(path / "turns.json")
        scenario = scenarios[row["scenario"]]
        recorder = bench.UsageRecorder("agent")
        recorder.calls = calls
        recalculated = recorder.summary()
        for key in (
            "calls",
            "failed_calls",
            "unknown_usage_calls",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
        ):
            if recalculated[key] != row["usage"][key]:
                issues.append(f"usage:{path.name}:{key}")
        for call in calls:
            usage = call["usage"]
            if usage is not None and not {"input_tokens", "output_tokens"} <= usage.keys():
                issues.append(f"partial-usage:{path.name}:{call['run_id']}")
            if usage is not None and "cache_read" not in usage.get("input_token_details", {}):
                issues.append(f"missing-cache-usage:{path.name}:{call['run_id']}")
            if call["role"] != "agent":
                issues.append(f"unexpected-role:{path.name}:{call['run_id']}")
        ids = [r["run_id"] for r in calls]
        request_ids.extend(ids)
        if set(ids) != {r["run_id"] for r in requests}:
            issues.append(f"request-callback-ids:{path.name}")
        if len(writes) != sum(r["method"] == "PUT" for r in http):
            issues.append(f"http-writes:{path.name}")
        grading = bench.grade(
            scenario,
            bench.read(path / "before.json"),
            bench.read(path / "after.json"),
            writes,
            row["final_receipts"],
        )
        if grading != row["business_grade"]:
            issues.append(f"business-grade:{path.name}")
        if row["error_type"] is None and grading["success"] != row["success"]:
            issues.append(f"success:{path.name}")
        expected_confirmation = scenario["confirmed_preference"]
        if len(setup["memory"]) != 1:
            issues.append(f"setup-count:{path.name}")
        elif setup["memory"][0]["value"]["content"]["content"] != expected_confirmation:
            issues.append(f"setup-text:{path.name}")
        if setup["source_contract"]["session_id"] == setup["target_contract"]["session_id"]:
            issues.append(f"new-session:{path.name}")
        if setup["target_contract"]["user_constraints"]:
            issues.append(f"constraint-leak:{path.name}")
        first = json.dumps(requests[0]["messages"]) if requests else ""
        if any(marker in first for marker in scenario["private_markers"]):
            issues.append(f"first-request-leak:{path.name}")
        if row["condition"] == "empty" and any(s["returned_result"] for s in searches):
            issues.append(f"empty-result:{path.name}")
        schema_hashes.append(bench.digest(bench.read(path / "tool_schemas.json")))
        if requests and requests[0]["tools"]:
            schema_hashes.append(bench.digest(requests[0]["tools"]))
        preference_questions = sum(
            bench.clarification(scenario, turn["assistant"])[0] == "preference_clarification"
            and not sessions[index]["verified_receipts"]
            for index, turn in enumerate(turns)
        )
        user_followups = max(len(turns) - 1, 0)
        preference_followups = sum(
            turn.get("followup_kind") == "preference_clarification" for turn in turns[:-1]
        )
        if preference_followups != row["preference_clarifications"]:
            issues.append(f"preference-followup-count:{path.name}")
        for previous, current in zip(turns, turns[1:], strict=False):
            if current["user"] != bench.clarification(scenario, previous["assistant"])[1]:
                issues.append(f"fixed-followup-text:{path.name}:{current['turn']}")
        native_calls = []
        for trace in sorted((path / "state/sessions").glob("*/trace-*.json")):
            native_calls.extend(bench.read(trace)["model_calls"])
        if Counter(r["run_id"] for r in native_calls) != Counter(ids):
            issues.append(f"production-ledger:{path.name}")
        rows.append(
            {
                **row,
                "directory": path.name,
                "preference_questions": preference_questions,
                "user_followups": user_followups,
                "preference_followups": preference_followups,
                "http_write_attempts": len(writes),
                "journals": len(bench.read(path / "after.json")["journals"]),
                "unknown_latency_calls": sum(c["elapsed_seconds"] is None for c in calls),
                "model_seconds": sum(c["elapsed_seconds"] or 0 for c in calls),
                "first_request_tool_schema_sha256": (
                    bench.digest(requests[0]["tools"])
                    if requests and requests[0]["tools"]
                    else None
                ),
                "first_request_messages_sha256": (
                    bench.digest(requests[0]["messages"]) if requests else None
                ),
                "first_request_provider_messages_sha256": (
                    provider_input_digest(requests[0]["messages"]) if requests else None
                ),
                "component_schema_sha256": bench.digest(bench.read(path / "tool_schemas.json")),
            }
        )
    if len(request_ids) != len(set(request_ids)):
        issues.append("duplicate-request-ids")
    for field in ("component_schema_sha256", "first_request_tool_schema_sha256"):
        hashes = {r[field] for r in rows if r[field] is not None}
        if len(hashes) > 1:
            issues.append(f"schema-condition-drift:{field}")
    for scenario, repeat in {(r["scenario"], r["repeat"]) for r in rows}:
        pair = [r for r in rows if r["scenario"] == scenario and r["repeat"] == repeat]
        if len(pair) == 2 and len({r["first_request_provider_messages_sha256"] for r in pair}) != 1:
            issues.append(f"initial-input-condition-drift:{scenario}:{repeat}")
    aggregate = bench.summarize(rows)
    for key in ("groups", "pairs"):
        if aggregate[key] != summary[key]:
            issues.append(f"summary-drift:{key}")
    if len(rows) != summary["started_executions"]:
        issues.append("summary-started-count")
    for condition, group in aggregate["groups"].items():
        group["user_followups"] = sum(
            r["user_followups"] for r in rows if r["condition"] == condition
        )
        group["preference_followups"] = sum(
            r["preference_followups"] for r in rows if r["condition"] == condition
        )
    effects = {}
    for key in ("calls", "input_tokens", "output_tokens", "cache_read_tokens"):
        effects[key] = ratio(
            aggregate["groups"]["available"]["usage"][key],
            aggregate["groups"]["empty"]["usage"][key],
        )
    for kind in ("history_required", "current_override", "all"):
        selected = [r for r in rows if kind == "all" or r["kind"] == kind]
        effects[kind] = {
            metric: ratio(
                sum(r[metric] for r in selected if r["condition"] == "available"),
                sum(r[metric] for r in selected if r["condition"] == "empty"),
            )
            for metric in (
                "preference_clarifications",
                "preference_questions",
                "user_followups",
                "http_write_attempts",
            )
        }
        effects[kind]["executions_per_condition"] = dict(Counter(r["condition"] for r in selected))
    result = {
        **provenance,
        "source": str(directory.relative_to(ROOT)),
        "freeze_id": freeze["freeze_id"],
        "phase": summary["phase"],
        "planned": len(schedule),
        "started": len(rows),
        "independent_scenarios": len({r["scenario"] for r in rows}),
        "calls": len(request_ids),
        "all_usage": {
            key: sum(r["usage"][key] for r in rows)
            for key in (
                "calls",
                "failed_calls",
                "unknown_usage_calls",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
            )
        },
        "checks": {"issues": issues},
        "groups": aggregate["groups"],
        "pairs": aggregate["pairs"],
        "effects": effects,
        "executions": rows,
        "source_matches_finish": summary.get("source_matches_finish"),
        "frozen_scenario_sha256_matches": binding["frozen_scenario_sha256_matches"],
    }
    return save_result(output, result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.run, args.freeze, args.output)
    raise SystemExit(1 if result["checks"]["issues"] else 0)
