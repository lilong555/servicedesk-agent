#!/usr/bin/env python3
"""Measure policy retrieval on a frozen synthetic corpus and temporal queries."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

os.environ.setdefault("HAYSTACK_TELEMETRY_ENABLED", "false")

from benchmark_policy_lifecycle import documents, retrieval_experiment

ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(cases: Path, output: Path) -> dict:
    data = json.loads(cases.read_text(encoding="utf-8"))
    ids = [query["id"] for query in data["queries"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Query IDs must be unique.")
    material = documents(data)
    known = {(item.scope, item.policy_id, item.version) for item in material}
    for query in data["queries"]:
        if any((query["scope"], *gold) not in known for gold in query["gold"]):
            raise ValueError(f"Unknown gold identity: {query['id']}")
    output.mkdir(parents=True, exist_ok=False)
    sources = sorted(ROOT.glob("src/servicedesk_agent/*.py")) + [
        ROOT / "scripts/benchmark_policy_lifecycle.py",
        Path(__file__).resolve(), ROOT / "pyproject.toml", ROOT / "uv.lock", cases,
    ]
    members = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sources}
    with tarfile.open(output / "source.tar.gz", "w:gz") as archive:
        for path in sources:
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    protocol = {
        "schema": "servicedesk.policy-applicability.v1",
        "dataset": data["dataset_id"], "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_members": members,
        "source_archive_sha256": hashlib.sha256((output / "source.tar.gz").read_bytes()).hexdigest(),
        "documents": len(material), "policy_identities": len(data["policies"]),
        "policy_families": len({item.policy_id for item in material}),
        "queries": len(data["queries"]), "query_kinds": dict(Counter(q["kind"] for q in data["queries"])),
        "conditions": ["unfiltered_time", "filtered_time"], "top_k": 3,
        "model_requests": 0, "backend": "production PolicyCatalog and Haystack BM25",
        "packages": {name: version(name) for name in ("haystack-ai", "pydantic", "langgraph", "langmem")},
        "primary_metrics": ["hit_at_1", "hit_at_3", "time_inapplicable_returned", "out_of_scope_returned"],
        "unit": "synthetic query; date variants and scope variants share policy families",
        "design": "Both conditions share corpus, scope, retrieval and top_k; time eligibility is varied.",
    }
    write_json(output / "protocol.json", protocol)
    started = time.perf_counter()
    result = retrieval_experiment(data)
    result["elapsed_s"] = time.perf_counter() - started
    write_json(output / "retrieval.json", result)
    rows = result["rows"]
    summary = dict(result["summary"])
    for arm, values in summary.items():
        selected = [row for row in rows if row["arm"] == arm]
        values["unrelated_queries"] = sum(row["query"]["kind"] == "unrelated" for row in selected)
        values["unrelated_empty"] = sum(row["query"]["kind"] == "unrelated" and row["metrics"]["empty_result"] for row in selected)
        values["temporal_cases"] = {}
        for label in sorted({row["query"]["temporal_case"] for row in selected}):
            subset = [row for row in selected if row["query"]["temporal_case"] == label]
            values["temporal_cases"][label] = {
                "queries": len(subset),
                "answerable": sum(row["metrics"]["answerable"] for row in subset),
                "hit_at_1": sum(bool(row["metrics"]["hit_at_1"]) for row in subset),
                "hit_at_3": sum(bool(row["metrics"]["hit_at_3"]) for row in subset),
                "empty": sum(row["metrics"]["empty_result"] for row in subset),
            }
    summary_document = {
        "dataset": data["dataset_id"], "documents": len(material), "queries_per_condition": len(ids),
        "model_requests": 0, "conditions": summary,
        "protocol_sha256": hashlib.sha256((output / "protocol.json").read_bytes()).hexdigest(),
        "retrieval_sha256": hashlib.sha256((output / "retrieval.json").read_bytes()).hexdigest(),
    }
    write_json(output / "summary.json", summary_document)
    return summary_document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "benchmarks/policy-applicability-20261107/cases.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.cases.resolve(), args.output.resolve()), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
