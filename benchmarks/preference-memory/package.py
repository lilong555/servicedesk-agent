"""Archive original runtime state and verify the complete preference-memory evidence set."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tarfile
from collections import Counter
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "results/runs"
PREFIX = "preference-memory-20261106-"
RUN_NAMES = ("pilot", "pilot-v2", "pilot-v3", "formal")
FREEZE_NAMES = ("freeze", "freeze-v2", "freeze-v3")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sqlite_facts(path):
    before = sha(path.read_bytes())
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        integrity = [row[0] for row in db.execute("PRAGMA integrity_check")]
        tables = {}
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
    assert integrity == ["ok"], path
    assert before == sha(path.read_bytes()), path
    return {"integrity_check": integrity, "table_rows": tables}


def package_state(directory, verify_only):
    archive_path = directory / "raw-state.tar.gz"
    manifest_path = directory / "raw-state-manifest.json"
    originals = sorted(
        path
        for state in directory.glob("[0-9][0-9]-*/state")
        for path in state.rglob("*")
        if path.is_file()
    )
    if not archive_path.exists():
        assert not verify_only, f"Missing archive: {archive_path}"
        assert originals, directory
        assert not any(path.name.endswith(("-wal", "-shm")) for path in originals)
        with tarfile.open(archive_path, "x:gz") as archive:
            for path in originals:
                assert not path.is_symlink(), path
                archive.add(path, arcname=path.relative_to(directory).as_posix(), recursive=False)
    members = []
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            assert member.isfile() and not relative.is_absolute() and ".." not in relative.parts
            assert relative.parts[1] == "state", member.name
            data = archive.extractfile(member).read()
            original = directory / relative
            item = {"path": member.name, "bytes": len(data), "sha256": sha(data)}
            if original.exists():
                assert item["sha256"] == sha(original.read_bytes()), original
            if relative.suffix == ".sqlite":
                # Validate archived bytes independently of the live filesystem.
                with TemporaryDirectory(prefix=".state-verify-", dir=directory) as temp:
                    restored = Path(temp) / relative.name
                    restored.write_bytes(data)
                    facts = sqlite_facts(restored)
                if original.exists():
                    assert sqlite_facts(original) == facts, original
                item["sqlite"] = facts
                item["sqlite"]["archived_bytes_integrity_check"] = facts["integrity_check"]
            members.append(item)
    assert len(members) == len({item["path"] for item in members})
    assert {p.relative_to(directory).as_posix() for p in originals} <= {
        item["path"] for item in members
    }
    manifest = {
        "archive": archive_path.name,
        "archive_sha256": sha(archive_path.read_bytes()),
        "members": members,
        "sqlite_files": sum("sqlite" in item for item in members),
        "original_files_unchanged": True,
    }
    if manifest_path.exists():
        assert read(manifest_path) == manifest, manifest_path
    else:
        assert not verify_only, manifest_path
        write(manifest_path, manifest)
    return {
        "directory": directory.relative_to(ROOT).as_posix(),
        "archive_sha256": manifest["archive_sha256"],
        "archived_files": len(members),
        "sqlite_files": manifest["sqlite_files"],
        "integrity_checks_passed": True,
    }


def verify_freeze(directory):
    record = read(directory / "freeze.json")
    canonical = json.dumps(
        record["protocol"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert sha(canonical) == record["freeze_id"]
    archive_path = directory / "source.tar.gz"
    assert sha(archive_path.read_bytes()) == record["source_archive_sha256"]
    with tarfile.open(archive_path) as archive:
        members = {m.name: m for m in archive.getmembers()}
        assert set(members) == set(record["protocol"]["source_files"])
        for name, expected in record["protocol"]["source_files"].items():
            assert sha(archive.extractfile(members[name]).read()) == expected, name
    return {
        "directory": directory.relative_to(ROOT).as_posix(),
        "freeze_id": record["freeze_id"],
        "source_files": len(members),
        "source_archive_sha256": record["source_archive_sha256"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    report = {"runs": [], "freezes": [], "usage_by_phase": {}}
    usage_keys = (
        "calls",
        "failed_calls",
        "unknown_usage_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
    )
    totals = Counter()
    for name in RUN_NAMES:
        directory = RUNS / (PREFIX + name)
        analysis = read(directory / "analysis.json")
        summary = read(directory / "summary.json")
        assert not analysis["checks"]["issues"], directory
        assert summary["completed"], directory
        assert summary.get("source_matches_finish") is not False, directory
        assert analysis["started"] == analysis["planned"], directory
        assert analysis["frozen_scenario_sha256_matches"], directory
        state = package_state(directory, args.verify_only)
        state["source_matches_finish"] = summary.get("source_matches_finish")
        assert state["sqlite_files"] == analysis["started"] * 2
        report["runs"].append(state)
        report["usage_by_phase"][name] = analysis["all_usage"]
        totals.update({key: analysis["all_usage"][key] for key in usage_keys})
    for name in FREEZE_NAMES:
        report["freezes"].append(verify_freeze(RUNS / (PREFIX + name)))
    formal = read(RUNS / (PREFIX + "formal") / "summary.json")
    pilot = read(RUNS / (PREFIX + "pilot-v3") / "summary.json")
    assert formal["freeze_id"] == pilot["freeze_id"] == report["freezes"][-1]["freeze_id"]
    assert pilot["pilot_gate_passed"] is True
    report["formal_gate"] = {
        "admission_pilot": PREFIX + "pilot-v3",
        "admission_pilot_passed": pilot["pilot_gate_passed"],
        "formal_phase_local_field": formal["pilot_gate_passed"],
        "phase_local_expression": "phase == 'pilot' and pilot_gate(episodes) "
        "and source_matches_finish",
        "same_freeze": True,
    }
    report["all_usage"] = dict(totals)
    report["formal_executions"] = 24
    report["pilot_executions"] = 12
    report["checks_passed"] = True
    output = RUNS / (PREFIX + "verification")
    if args.verify_only:
        assert read(output / "audit.json") == report
    else:
        output.mkdir(exist_ok=True)
        write(output / "audit.json", report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
