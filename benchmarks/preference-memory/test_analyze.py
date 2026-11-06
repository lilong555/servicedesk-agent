from __future__ import annotations

import copy
import importlib.util
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "memory_analysis", Path(__file__).with_name("analyze.py")
)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)
FREEZE = ROOT / "results/runs/preference-memory-20261106-freeze-v3/freeze.json"


@pytest.mark.parametrize("suffix", ["freeze", "freeze-v2", "freeze-v3"])
def test_archived_source_and_scenario_bindings(suffix):
    path = ROOT / f"results/runs/preference-memory-20261106-{suffix}/freeze.json"
    sources, binding, issues = analysis.verify_sources(analysis.read(path), path)
    assert issues == []
    assert binding["frozen_scenario_sha256_matches"]
    assert analysis.DRIVER in sources
    assert len(json.loads(sources[analysis.SCENARIOS])["scenarios"]) == 6


def fixture_freeze(tmp_path, record):
    path = tmp_path / "freeze.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    shutil.copyfile(FREEZE.parent / "source.tar.gz", tmp_path / "source.tar.gz")
    return path


def test_scenario_hash_mismatch_is_an_issue(tmp_path):
    record = copy.deepcopy(analysis.read(FREEZE))
    record["protocol"]["scenario_sha256"] = "0" * 64
    record["freeze_id"] = analysis.digest(record["protocol"])
    path = fixture_freeze(tmp_path, record)
    _, binding, issues = analysis.verify_sources(record, path)
    assert "frozen-scenario-sha256-mismatch" in issues
    assert not binding["frozen_scenario_sha256_matches"]


def test_source_member_hash_mismatch_is_an_issue(tmp_path):
    record = copy.deepcopy(analysis.read(FREEZE))
    record["protocol"]["source_files"][analysis.DRIVER] = "0" * 64
    record["protocol"]["implementation_sha256"] = analysis.digest(
        record["protocol"]["source_files"]
    )
    record["freeze_id"] = analysis.digest(record["protocol"])
    path = fixture_freeze(tmp_path, record)
    _, _, issues = analysis.verify_sources(record, path)
    assert f"source-member-sha256-mismatch:{analysis.DRIVER}" in issues


def test_archive_and_workspace_hash_mismatches_are_issues(tmp_path):
    record = copy.deepcopy(analysis.read(FREEZE))
    record["source_archive_sha256"] = "0" * 64
    path = fixture_freeze(tmp_path, record)
    _, _, issues = analysis.verify_sources(record, path, workspace=tmp_path)
    assert "source-archive-sha256-mismatch" in issues
    assert any(issue.startswith("workspace-import-sha256-mismatch:") for issue in issues)


def test_original_run_is_rejected_as_output():
    path = ROOT / "results/runs/preference-memory-20261106-formal/analysis.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="outside original"):
        analysis.save_result(path, {"checks": {"issues": []}})
    assert path.read_bytes() == before


@pytest.fixture
def archived_state(tmp_path):
    original = ROOT / "results/runs/preference-memory-20261106-formal"
    for name in ("raw-state.tar.gz", "raw-state-manifest.json"):
        shutil.copyfile(original / name, tmp_path / name)
    return tmp_path


def test_public_archive_reconstructs_all_native_ledgers(archived_state):
    traces, binding, issues = analysis.archived_traces(archived_state)
    assert issues == []
    assert len(traces) == 24
    assert sum(len(trace["model_calls"]) for items in traces.values() for trace in items) == 221
    assert binding["verified_files"] == 129
    assert not any(archived_state.glob("*/state"))


@pytest.mark.parametrize("field", ["archive", "member"])
def test_state_archive_hash_mismatch_is_rejected(archived_state, field):
    path = archived_state / "raw-state-manifest.json"
    manifest = analysis.read(path)
    if field == "archive":
        manifest["archive_sha256"] = "0" * 64
    else:
        manifest["members"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(manifest))
    _, _, issues = analysis.archived_traces(archived_state)
    assert any("mismatch" in issue for issue in issues)


@pytest.mark.parametrize("kind", ["traversal", "symlink"])
def test_state_archive_unsafe_members_are_rejected(tmp_path, kind):
    path = tmp_path / "raw-state.tar.gz"
    name = "../outside" if kind == "traversal" else "01-case/state/link"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
        archive.addfile(member, io.BytesIO(b""))
    (tmp_path / "raw-state-manifest.json").write_text(
        json.dumps(
            {
                "archive_sha256": analysis.sha(path.read_bytes()),
                "members": [{"path": name, "bytes": 0, "sha256": analysis.sha(b"")}],
            }
        )
    )
    _, _, issues = analysis.archived_traces(tmp_path)
    assert any(issue.startswith("state-archive-invalid-member") for issue in issues)


def test_missing_state_archive_is_reported(tmp_path):
    _, _, issues = analysis.archived_traces(tmp_path)
    assert issues == ["state-archive-or-manifest-missing"]
