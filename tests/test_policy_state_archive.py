from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks/policy-lifecycle/audit_state.py"


@pytest.fixture
def state_archive(tmp_path):
    database = tmp_path / "template.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY)")
    data = database.read_bytes()
    path = tmp_path / "raw-state.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for index in range(16):
            member = tarfile.TarInfo(f"private-state/run-{index}/checkpoint.sqlite")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def run_audit(directory):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(directory), "--output", str(directory / "audit.json")],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return result


def test_archive_only_checkout_is_auditable(state_archive):
    result = run_audit(state_archive.parent)
    assert result.returncode == 0, result.stderr
    audit = json.loads((state_archive.parent / "audit.json").read_text())
    assert audit["verified"] is True
    assert audit["database_count"] == 16
    assert audit["original_files_compared"] == 0


def test_original_content_mismatch_fails(state_archive):
    original = state_archive.parent / "private-state/run-0/checkpoint.sqlite"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"changed")
    result = run_audit(state_archive.parent)
    assert result.returncode == 1
    audit = json.loads((state_archive.parent / "audit.json").read_text())
    assert audit["issues"] == ["content:private-state/run-0/checkpoint.sqlite"]


@pytest.mark.parametrize("kind", ["traversal", "symlink", "duplicate"])
def test_unsafe_members_are_rejected(tmp_path, kind):
    with tarfile.open(tmp_path / "raw-state.tar.gz", "w:gz") as archive:
        name = "../outside" if kind == "traversal" else "private-state/item"
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
        archive.addfile(member, io.BytesIO(b""))
        if kind == "duplicate":
            archive.addfile(member, io.BytesIO(b""))
    result = run_audit(tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "audit.json").exists()
