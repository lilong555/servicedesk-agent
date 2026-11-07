"""Verify the frozen source, query population and retrieval scores without a model."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tarfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check(root: Path):
    protocol_bytes = (root / "protocol.json").read_bytes()
    protocol = json.loads(protocol_bytes)
    raw_bytes = (root / "retrieval.json").read_bytes()
    raw = json.loads(raw_bytes)
    summary = json.loads((root / "summary.json").read_text())
    issues = []
    if digest(protocol_bytes) != summary["protocol_sha256"]:
        issues.append("protocol_digest")
    if digest(raw_bytes) != summary["retrieval_sha256"]:
        issues.append("retrieval_digest")
    source = root / "source.tar.gz"
    if digest(source.read_bytes()) != protocol["source_archive_sha256"]:
        issues.append("source_archive_digest")
    cases = None
    with tarfile.open(source, "r:gz") as archive:
        members = archive.getmembers()
        if sorted(member.name for member in members) != sorted(protocol["source_members"]):
            issues.append("source_members")
        for member in members:
            if not member.isfile() or member.size > 20_000_000:
                issues.append("source_member_type_or_size")
                continue
            data = archive.extractfile(member).read()
            if digest(data) != protocol["source_members"].get(member.name):
                issues.append("source_member_hash:" + member.name)
            if member.name == "benchmarks/policy-applicability-20261107/cases.json":
                cases = json.loads(data)
    if cases is None:
        raise ValueError("Frozen query population is missing.")
    expected = {(query["id"], arm): query for query in cases["queries"] for arm in protocol["conditions"]}
    seen = set()
    totals = {arm: {key: 0 for key in ("queries", "answerable_queries", "hit_at_1", "hit_at_3", "returned_documents", "time_inapplicable_returned", "out_of_scope_returned", "empty_eligible_cases", "correct_empty_results", "unrelated_queries", "unrelated_empty")} for arm in protocol["conditions"]}
    for row in raw["rows"]:
        query, found = row["query"], row["results"]
        identity = (query["id"], row["arm"])
        if identity in seen or expected.get(identity) != query:
            issues.append("query_identity:" + str(identity))
        seen.add(identity)
        now = datetime.fromisoformat(query["time"].replace("Z", "+00:00"))
        gold = {tuple(item) for item in query["gold"]}
        pairs = [(item["policy_id"], item["version"]) for item in found]
        invalid, outside = 0, 0
        for item in found:
            evidence = item["evidence"]
            start = datetime.fromisoformat(evidence["valid_from"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(evidence["valid_to"].replace("Z", "+00:00")) if evidence["valid_to"] else None
            invalid += int(now < start or end is not None and now >= end)
            outside += int(evidence["entity_scope"] != query["scope"])
        values = {
            "queries": 1, "answerable_queries": int(bool(gold)),
            "hit_at_1": int(bool(gold and pairs and pairs[0] in gold)),
            "hit_at_3": int(bool(gold.intersection(pairs))),
            "returned_documents": len(found), "time_inapplicable_returned": invalid,
            "out_of_scope_returned": outside, "empty_eligible_cases": int(query["kind"] == "empty-eligible"),
            "correct_empty_results": int(query["kind"] == "empty-eligible" and not found),
            "unrelated_queries": int(query["kind"] == "unrelated"),
            "unrelated_empty": int(query["kind"] == "unrelated" and not found),
        }
        for key, value in values.items():
            totals[row["arm"]][key] += value
    if seen != set(expected):
        issues.append("population")
    for arm, values in totals.items():
        for key, value in values.items():
            if summary["conditions"][arm][key] != value:
                issues.append(f"summary:{arm}:{key}")
    return {"issues": issues, "verified_rows": len(seen), "source_members": len(protocol["source_members"]), "conditions": totals, "analyzer_sha256": digest(Path(__file__).read_bytes()), "model_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = check(args.run)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result["issues"]))
