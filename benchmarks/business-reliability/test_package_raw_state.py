"""Focused safety checks for the synthetic SQLite archive."""

import gzip
import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "package_raw_state", Path(__file__).with_name("package_raw_state.py")
)
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


@pytest.mark.parametrize(
    "name",
    [
        "/tmp/server.sqlite",
        "../server.sqlite",
        "main/../server.sqlite",
        "main\\server.sqlite",
        "./main/server.sqlite",
        "verification/server.sqlite",
    ],
)
def test_rejects_unsafe_member_paths(name):
    with pytest.raises(ValueError):
        packager.safe_path(name, database=True)


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_accepts_checkpoint_sidecars(suffix):
    name = "deterministic/late_commit/reconcile/runtime/checkpoint.sqlite" + suffix
    assert packager.safe_path(name, database=True).as_posix() == name


@pytest.mark.parametrize("malformation", ["symlink", "bad_hash", "duplicate", "none"])
def test_restore_checks_members_before_writing(tmp_path, malformation):
    data = b"synthetic test data"
    name = "main/note-000/reconcile/server.sqlite"
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            if malformation == "symlink":
                member.type, member.linkname, member.size = tarfile.SYMTYPE, "/tmp/outside", 0
            archive.addfile(member, io.BytesIO(data) if member.isfile() else None)
            if malformation == "duplicate":
                archive.addfile(member, io.BytesIO(data))
    path = tmp_path / "fixture.tar.gz"
    path.write_bytes(buffer.getvalue())
    manifest = {
        "archive_sha256": hashlib.sha256(buffer.getvalue()).hexdigest(),
        "files": [
            {
                "path": name,
                "size": len(data),
                "sha256": "0" * 64
                if malformation == "bad_hash"
                else hashlib.sha256(data).hexdigest(),
            }
        ],
    }
    destination = tmp_path / "restored"
    destination.mkdir()
    if malformation == "none":
        packager.restore(path, manifest, destination)
        assert (destination / name).read_bytes() == data
    else:
        with pytest.raises(ValueError):
            packager.restore(path, manifest, destination)
        assert list(destination.iterdir()) == []


def test_restore_rejects_corrupt_archive(tmp_path):
    archive = tmp_path / "corrupt.tar.gz"
    archive.write_bytes(b"changed payload")
    with pytest.raises(ValueError, match="Archive SHA-256"):
        packager.restore(archive, {"archive_sha256": "0" * 64}, tmp_path / "restore")
