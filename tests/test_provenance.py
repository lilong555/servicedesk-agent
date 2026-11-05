import hashlib
import tarfile

from servicedesk_agent.domain import fingerprint
from servicedesk_agent.provenance import capture_source


def test_source_archive_contains_reproducible_code_and_excludes_private_data(tmp_path):
    manifest = capture_source(tmp_path)
    assert manifest["implementation_sha256"] == fingerprint(manifest["files"])
    archive_path = tmp_path / manifest["source_archive"]
    assert (
        manifest["source_archive_sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    )
    assert archive_path.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()
        assert "uv.lock" in names
        assert "src/servicedesk_agent/agent.py" in names
        assert "src/servicedesk_agent/assets/itsm_subset.json" in names
        assert all(
            not name.startswith((".runtime/", ".venv/", "artifacts/", "vendor/")) and name != ".env"
            for name in names
        )
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(archive.extractfile(name).read()).hexdigest() == digest
    assert manifest["packages"]["deepagents"] == "0.7.32"
