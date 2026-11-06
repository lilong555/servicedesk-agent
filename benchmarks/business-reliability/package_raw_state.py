#!/usr/bin/env python3
"""Package the completed synthetic run and verify restoration without changing its source."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
RUN = REPO / "results/runs/business-reliability-20261106-001"
VERIFICATION = REPO / "results/runs/business-reliability-20261106-verification"
ARCHIVE = "raw-state.tar.gz"
MANIFEST = "raw-state.manifest.json"
DATABASES = {"server.sqlite", "memory.sqlite", "checkpoint.sqlite"}
STATE_NAMES = DATABASES | {
    name + suffix for name in DATABASES for suffix in ("-wal", "-shm", "-journal")
}
ROOTS = {"main", "deterministic", "revocation"}
SECRETS = {
    "provider_key": rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}",
    "github_token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "aws_access_key": rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "private_key": rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "bearer_token": rb"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/-]{12,}",
    "credential_field": rb"(?i)(?:api[_-]?key|password|access[_-]?token)"
    rb"""["']?\s*[:=]\s*["'][^"']{8,}["']""",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=True, indent=2) + "\n")


def safe_path(name, *, database=False):
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or path.as_posix() != name
    ):
        raise ValueError(f"Unsafe archive path: {name!r}")
    if database and (path.parts[0] not in ROOTS or path.name not in STATE_NAMES):
        raise ValueError(f"Unexpected state member: {name!r}")
    return path


def is_public_input(path):
    return (
        path.is_file()
        and not path.is_symlink()
        and ".sqlite" not in path.name
        and not path.name.endswith(".lock")
        and not any(part.endswith(".redmine-locks") for part in path.parts)
        and not path.name.startswith(("raw-state", "publication"))
    )


def inspect_synthetic(root, states):
    fixture = read_json(root / "redmine.json")
    case_paths = [
        *sorted((root / "main").glob("*/*/case.json")),
        *sorted((root / "deterministic").glob("*/*/case.json")),
        root / "revocation/case.json",
    ]
    if len(case_paths) != 205:
        raise ValueError("Expected 205 synthetic case fixtures.")
    for path in case_paths:
        case = read_json(path)
        contract = case["contract"]
        if (
            case["fixture"] != fixture
            or contract["service_url"] != "https://business-reliability.invalid"
            or (contract["project_id"], contract["principal_id"], contract["ticket_id"])
            != (1, 1, 42)
            or case["note"] != f"Investigation completed for {case['operation_id']}."
        ):
            raise ValueError(f"Unexpected business input: {path.relative_to(root)}")
    findings, urls = [], set()
    for path in states:
        data = path.read_bytes()
        for label, pattern in SECRETS.items():
            if re.search(pattern, data):
                findings.append({"path": path.relative_to(root).as_posix(), "pattern": label})
        urls.update(
            value.decode("ascii") for value in re.findall(rb"https?://[A-Za-z0-9._:-]+", data)
        )
        if path.name not in DATABASES:
            if path.name.endswith(("-wal", "-journal")) and data:
                raise ValueError("Package a completed run with an empty WAL/rollback journal.")
            continue
        if data[:16] != b"SQLite format 3\x00":
            raise ValueError(f"Invalid SQLite header: {path}")
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError(f"SQLite integrity failure: {path}")
        finally:
            connection.close()
    if findings or urls != {"https://business-reliability.invalid"}:
        raise ValueError(json.dumps({"secret_findings": findings, "service_urls": sorted(urls)}))
    return {
        "case_fixtures_checked": len(case_paths),
        "sqlite_integrity_checks": sum(path.name in DATABASES for path in states),
        "sqlite_sidecar_files": sum(path.name not in DATABASES for path in states),
        "credential_patterns": list(SECRETS),
        "credential_findings": findings,
        "service_urls": sorted(urls),
        "scope": "Synthetic business state plus local paths, process IDs and timestamps.",
        "credentials": (
            "Transport headers are omitted; no real credentials found in database bytes."
        ),
    }


def package(root):
    states = sorted(path for path in root.rglob("*") if ".sqlite" in path.name)
    if not states or any(not path.is_file() or path.is_symlink() for path in states):
        raise ValueError("Expected regular, quiescent SQLite files.")
    for path in states:
        safe_path(path.relative_to(root).as_posix(), database=True)
    checks = inspect_synthetic(root, states)
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": digest(path.read_bytes()),
        }
        for path in states
    ]
    public_inputs = [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": digest(path.read_bytes()),
        }
        for path in sorted(root.rglob("*"))
        if is_public_input(path)
    ]
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for entry in files:
                data = (root / entry["path"]).read_bytes()
                if digest(data) != entry["sha256"]:
                    raise ValueError("SQLite changed during packaging.")
                info = tarfile.TarInfo(entry["path"])
                info.size, info.mode, info.mtime = len(data), 0o600, 0
                archive.addfile(info, io.BytesIO(data))
    payload = buffer.getvalue()
    manifest = {
        "format": 1,
        "archive": ARCHIVE,
        "archive_sha256": digest(payload),
        "archive_bytes": len(payload),
        "file_count": len(files),
        "uncompressed_bytes": sum(entry["size"] for entry in files),
        "files": files,
        "public_inputs": public_inputs,
        "synthetic_content_audit": checks,
        "packager_sha256": digest(Path(__file__).read_bytes()),
        "summary_sha256": digest((root / "summary.json").read_bytes()),
        "operations_csv_sha256": digest((root / "operations.csv").read_bytes()),
        "frozen_source_archive_sha256": digest((root / "source.zip").read_bytes()),
    }
    with (root / ARCHIVE).open("xb") as handle:
        handle.write(payload)
    save_json(root / MANIFEST, manifest)
    with (root / "raw-state.sha256").open("x", encoding="ascii") as handle:
        handle.write(f"{digest(payload)}  {ARCHIVE}\n")
    return manifest


def restore(archive_path, manifest, destination):
    payload = archive_path.read_bytes()
    if digest(payload) != manifest["archive_sha256"]:
        raise ValueError("Archive SHA-256 mismatch.")
    expected = {entry["path"]: entry for entry in manifest["files"]}
    if len(expected) != len(manifest["files"]):
        raise ValueError("Duplicate manifest path.")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(set(names)) != len(names) or set(names) != set(expected):
            raise ValueError("Archive member list mismatch.")
        validated = []
        for member in members:
            safe_path(member.name, database=True)
            if not member.isfile() or member.size != expected[member.name]["size"]:
                raise ValueError("Expected a regular file of the declared size.")
            data = archive.extractfile(member).read()
            if digest(data) != expected[member.name]["sha256"]:
                raise ValueError(f"State SHA-256 mismatch: {member.name}")
            target = destination / member.name
            if any(path.is_symlink() for path in [target, *target.parents]):
                raise ValueError("Symlink in restoration destination.")
            if target.exists():
                raise ValueError(f"Restore into a fresh directory: {target}")
            validated.append((target, data))
        for target, data in validated:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
            target.chmod(0o600)


def verify(root, destination):
    if destination.parent != REPO / "results/runs" or not destination.name.startswith(
        "business-reliability-20261106"
    ):
        raise ValueError("Use a fresh results/runs/business-reliability-20261106* directory.")
    manifest = read_json(root / MANIFEST)
    destination.mkdir(exist_ok=False)
    originals = {
        path.relative_to(root).as_posix(): digest(path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    for entry in manifest["public_inputs"]:
        relative = safe_path(entry["path"])
        source = root / relative
        if source.is_symlink() or digest(source.read_bytes()) != entry["sha256"]:
            raise ValueError(f"Public input mismatch: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    if list(destination.rglob("*.sqlite*")):
        raise ValueError("The public-input copy already contained SQLite.")
    restore(root / ARCHIVE, manifest, destination)
    command = [
        sys.executable,
        str(REPO / "scripts/benchmark_business_reliability.py"),
        "summarize",
        str(destination),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    checks = {
        "summarize_exit_zero": completed.returncode == 0,
        "frozen_script_matches": digest(
            (REPO / "scripts/benchmark_business_reliability.py").read_bytes()
        )
        == read_json(root / "freeze.json")["source_hashes"][
            "scripts/benchmark_business_reliability.py"
        ],
        "summary_identical": digest((destination / "summary.json").read_bytes())
        == manifest["summary_sha256"],
        "operations_csv_identical": digest((destination / "operations.csv").read_bytes())
        == manifest["operations_csv_sha256"],
        "restored_state_hashes": all(
            digest((destination / entry["path"]).read_bytes()) == entry["sha256"]
            for entry in manifest["files"]
        ),
        "original_run_files_unchanged": all(
            digest((root / path).read_bytes()) == value for path, value in originals.items()
        ),
    }
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "archive_sha256": manifest["archive_sha256"],
        "sqlite_files_restored": manifest["file_count"],
        "public_input_files_copied": len(manifest["public_inputs"]),
        "destination": str(destination.relative_to(REPO)),
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    save_json(destination / "raw-state.restore-verification.json", result)
    return result


def public_paths(root):
    files = [
        REPO / "scripts/benchmark_business_reliability.py",
        REPO / "tests/test_business_reliability_benchmark.py",
        *sorted((REPO / "benchmarks/business-reliability").glob("*")),
        *sorted(root.rglob("*")),
        *sorted(VERIFICATION.glob("*")),
    ]
    selected = []
    for path in files:
        if (
            path.is_file()
            and not path.is_symlink()
            and ".sqlite" not in path.name
            and not path.name.endswith(".lock")
            and not any(part.endswith(".redmine-locks") for part in path.parts)
        ):
            selected.append(path.relative_to(REPO).as_posix())
    own_path = root / "publication-files.txt"
    selected.append(own_path.relative_to(REPO).as_posix())
    with own_path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(sorted(set(selected))) + "\n")
    return {"public_files": len(set(selected)), "pathspec": str(own_path.relative_to(REPO))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("package", "verify", "public-paths"))
    parser.add_argument("--run", type=Path, default=RUN)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    root = args.run.resolve()
    if root != RUN:
        raise ValueError(
            "This packager targets the completed business-reliability-20261106-001 run."
        )
    if args.action == "package":
        manifest = package(root)
        result = {
            key: manifest[key]
            for key in (
                "archive_sha256",
                "archive_bytes",
                "file_count",
                "uncompressed_bytes",
            )
        }
    elif args.action == "verify":
        if args.destination is None:
            parser.error("verify requires --destination")
        result = verify(root, args.destination.resolve())
    else:
        result = public_paths(root)
    print(json.dumps(result, indent=2))
    return int(result.get("passed") is False)


if __name__ == "__main__":
    raise SystemExit(main())
