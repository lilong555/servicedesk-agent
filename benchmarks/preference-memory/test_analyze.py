from __future__ import annotations

import copy
import importlib.util
import json
import shutil
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
