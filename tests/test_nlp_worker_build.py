"""Pinned external worker preparation uses invented source trees only."""
from pathlib import Path
import importlib.util
import json

import pytest

pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nlp_worker_prepare", ROOT / "tools/nlp-worker/prepare.py")
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)

@pytest.fixture
def source(tmp_path, monkeypatch):
    folder = tmp_path / "upstream-source"
    (folder / "src").mkdir(parents=True)
    (folder / "Cargo.toml").write_text('hf-hub = "1"\nort = { features = ["std", "download-binaries", "tls-rustls", "copy-dylibs", "api-27"] }\n')
    (folder / "src/tokenizer.rs").write_text('        let inner = Tokenizer::from_file(tokenizer_path).map_err(anyhow::Error::msg)?;\n')
    (folder / "src/runtime.rs").write_text('        // Only the CPU provider passes validation. ORT registers CPU implicitly,\n        // so no provider registration call is needed and none can fall back.\n')
    for name in ("Cargo.lock", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md"):
        (folder / name).write_text("invented fixture\n")
    monkeypatch.setattr(PREPARE.subprocess, "check_output", lambda argv, **kwargs: PREPARE.REVISION + "\n" if argv[-1] == "HEAD" else "")
    return folder

def test_external_preparation_retains_sources_and_disables_downloads(tmp_path, source):
    target = tmp_path / "prepared"
    PREPARE.prepare(source, target)
    manifest = (target / "upstream/Cargo.toml").read_text()
    assert "load-dynamic" in manifest
    assert all(text not in manifest for text in ("hf-hub", "download-binaries", "copy-dylibs", "tls-rustls"))
    assert "pub fn from_file" in (target / "upstream/src/tokenizer.rs").read_text()
    assert "error_on_failure" in (target / "upstream/src/runtime.rs").read_text()
    receipt = json.loads((target / "preparation.json").read_text())
    assert receipt["upstream_revision"] == PREPARE.REVISION
    assert any(item["path"] == "upstream/LICENSE" for item in receipt["files"])
    assert any(item["path"] == "src/service.rs" for item in receipt["files"])
    assert (source / "Cargo.toml").read_text().startswith("hf-hub")


def test_preparation_preserves_utf8_source_bytes(tmp_path, source):
    path = source / 'src/tokenizer.rs'
    comment = '// caf\u00e9 \U0001f601\n'
    path.write_text(comment + path.read_text(encoding='utf-8'), encoding='utf-8')
    target = tmp_path / 'utf8-prepared'
    PREPARE.prepare(source, target)
    assert (target / 'upstream/src/tokenizer.rs').read_bytes().startswith(comment.encode('utf-8'))

@pytest.mark.parametrize("status", ["wrong_revision", "dirty"])
def test_wrong_or_dirty_source_refused_before_target_creation(tmp_path, source, monkeypatch, status):
    def output(argv, **kwargs):
        return ("0" * 40 if status == "wrong_revision" else PREPARE.REVISION) if argv[-1] == "HEAD" else " M src/lib.rs\n"
    monkeypatch.setattr(PREPARE.subprocess, "check_output", output)
    target = tmp_path / "not-created"
    with pytest.raises(ValueError, match="clean pinned"):
        PREPARE.prepare(source, target)
    assert not target.exists()

def test_existing_target_and_source_subdirectory_are_refused(tmp_path, source):
    with pytest.raises(ValueError, match="fresh external"):
        PREPARE.prepare(source, tmp_path)
    with pytest.raises(ValueError, match="fresh external"):
        PREPARE.prepare(source, source / "build")

def test_changed_patch_seam_fails_closed(tmp_path, source):
    (source / "src/tokenizer.rs").write_text("different source\n")
    with pytest.raises(ValueError, match="upstream source differs"):
        PREPARE.prepare(source, tmp_path / "rejected")

def test_public_worker_has_no_runtime_download_feature():
    manifest = (ROOT / "native/nlp-worker/Cargo.toml").read_text()
    assert 'path = "upstream"' in manifest
    assert '"load-dynamic"' in manifest
    assert 'download-binaries' not in manifest
    assert 'default-features = false' in manifest
