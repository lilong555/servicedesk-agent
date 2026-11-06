"""Verify the local synthetic policy state archive without modifying source state."""

import argparse
import hashlib
import json
import sqlite3
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
ROOT = args.directory.resolve()
archive = ROOT / "raw-state.tar.gz"
rows = []
issues = []
with tarfile.open(archive, "r:gz") as source, tempfile.TemporaryDirectory() as temporary:
    seen = set()
    for member in source.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.name in seen:
            raise ValueError("Invalid archive path")
        seen.add(member.name)
        if member.isdir():
            continue
        if not member.isfile() or path.parts[0] != "private-state":
            raise ValueError("Unexpected archive member")
        data = source.extractfile(member).read()
        sha = hashlib.sha256(data).hexdigest()
        original = ROOT / member.name
        item = {
            "path": member.name,
            "bytes": len(data),
            "sha256": sha,
            "matches_original": (
                hashlib.sha256(original.read_bytes()).hexdigest() == sha
                if original.exists()
                else None
            ),
        }
        if item["matches_original"] is False:
            issues.append(f"content:{member.name}")
        if member.name.endswith(".sqlite"):
            restored = Path(temporary) / f"{len(rows)}.sqlite"
            restored.write_bytes(data)
            with sqlite3.connect(
                f"{restored.as_uri()}?mode=ro&immutable=1", uri=True
            ) as connection:
                item["sqlite_integrity"] = [
                    row[0] for row in connection.execute("PRAGMA integrity_check")
                ]
            if item["sqlite_integrity"] != ["ok"]:
                issues.append(f"sqlite-integrity:{member.name}")
        rows.append(item)
databases = sum("sqlite_integrity" in row for row in rows)
if databases != 16:
    issues.append("expected-sixteen-databases")
result = {
    "archive": archive.name,
    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    "audit_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "database_count": databases,
    "original_files_compared": sum(row["matches_original"] is not None for row in rows),
    "member_count": len(rows),
    "members": rows,
    "issues": issues,
    "verified": not issues,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
with args.output.open("x") as output:
    json.dump(result, output, indent=2)
    output.write("\n")
print(json.dumps({key: result[key] for key in ("verified", "issues", "database_count")}))
raise SystemExit(bool(issues))
