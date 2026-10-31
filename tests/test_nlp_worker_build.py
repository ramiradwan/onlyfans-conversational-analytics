"""Worker preparation uses invented local Git repositories, never model builds."""
from pathlib import Path
import hashlib
import importlib.util
import json
import subprocess

import pytest

pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nlp_worker_prepare", ROOT / "tools/nlp-worker/prepare.py")
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


def git(folder, *args, data=None):
    return subprocess.check_output(["git", "-C", str(folder), *args], input=data, stderr=subprocess.PIPE).decode().strip()


def commit(folder):
    git(folder, "add", ".")
    git(folder, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    return git(folder, "rev-parse", "HEAD")


def repository(path, files):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "core.autocrlf", "false")
    hooks = path.parent / (path.name + "-empty-hooks")
    hooks.mkdir()
    attributes = path.parent / (path.name + "-empty-attributes")
    attributes.write_text("")
    git(path, "config", "core.hooksPath", str(hooks))
    git(path, "config", "core.attributesFile", str(attributes))
    for name, data in files.items():
        leaf = path / name
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text(data, encoding="utf-8", newline="\n")
    return commit(path)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    source = tmp_path / "upstream-source"
    upstream = repository(source, {
        "Cargo.toml": 'hf-hub = "1"\nort = { features = ["std", "download-binaries", "tls-rustls", "copy-dylibs", "api-27"] }\n',
        "src/tokenizer.rs": '        let inner = Tokenizer::from_file(tokenizer_path).map_err(anyhow::Error::msg)?;\n',
        "src/runtime.rs": '        // Only the CPU provider passes validation. ORT registers CPU implicitly,\n        // so no provider registration call is needed and none can fall back.\n',
        **{name: "invented fixture\n" for name in ("Cargo.lock", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md")},
        ".gitignore": "src/ignored.rs\n",
    })
    product = tmp_path / "product"
    selected = repository(product, {"native/nlp-worker/" + name: "selected " + name + "\n"
                                    for name in PREPARE.PRODUCT_REQUIRED})
    monkeypatch.setattr(PREPARE, "REVISION", upstream)
    monkeypatch.setattr(PREPARE, "ROOT", product)
    return source, product, selected


def test_selected_product_commit_excludes_head_edits_and_untracked_files(tmp_path, sources):
    source, product, selected = sources
    main = product / "native/nlp-worker/src/main.rs"
    main.write_text("new committed source\n")
    assert commit(product) != selected
    main.write_text("dirty tracked source\n")
    (main.parent / "untracked.rs").write_text("untracked\n")
    (product / ".gitignore").write_text("native/nlp-worker/src/ignored.rs\n")
    (main.parent / "ignored.rs").write_text("ignored\n")
    (source / "src/ignored.rs").write_text("ignored upstream bytes\n")
    target = tmp_path / "prepared"
    PREPARE.prepare(source, target, selected)
    assert (target / "src/main.rs").read_text() == "selected src/main.rs\n"
    assert not any(target.rglob("untracked.rs"))
    assert not any(target.rglob("ignored.rs"))
    receipt = json.loads((target / "preparation.json").read_text())
    assert receipt["schema"] == "nlp-worker-build.v2"
    assert receipt["product_revision"] == selected
    assert receipt["upstream_revision"] == PREPARE.REVISION
    preparer = (ROOT / "tools/nlp-worker/prepare.py").read_bytes()
    assert receipt["preparer"] == dict(path="tools/nlp-worker/prepare.py", bytes=len(preparer),
                                      sha256=hashlib.sha256(preparer).hexdigest())
    for origin, repo, revision in (("product", product, selected), ("upstream", source, PREPARE.REVISION)):
        for record in receipt["inputs"][origin]:
            raw = subprocess.check_output(["git", "-C", str(repo), "show", revision + ":" + record["path"]])
            assert record["mode"] == "100644"
            assert record["git_blob"] == git(repo, "rev-parse", revision + ":" + record["path"])
            assert (record["bytes"], record["sha256"]) == (len(raw), hashlib.sha256(raw).hexdigest())
    actual = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} - {"preparation.json"}
    assert actual == {r["path"] for r in receipt["files"]}
    for record in receipt["files"]:
        raw = (target / record["path"]).read_bytes()
        assert (record["bytes"], record["sha256"]) == (len(raw), hashlib.sha256(raw).hexdigest())
    manifest = (target / "upstream/Cargo.toml").read_text()
    assert "load-dynamic" in manifest
    assert all(text not in manifest for text in ("hf-hub", "download-binaries", "copy-dylibs", "tls-rustls"))
    assert "pub fn from_file" in (target / "upstream/src/tokenizer.rs").read_text()
    assert "error_on_failure" in (target / "upstream/src/runtime.rs").read_text()
    assert (target / "upstream/LICENSE").read_text() == "invented fixture\n"
    assert (source / "Cargo.toml").read_text().startswith("hf-hub")


def test_preparation_preserves_utf8_git_blob_bytes(tmp_path, sources, monkeypatch):
    source, _, selected = sources
    path = source / "src/tokenizer.rs"
    comment = "// caf\u00e9 \U0001f601\n"
    path.write_text(comment + path.read_text(), encoding="utf-8", newline="\n")
    monkeypatch.setattr(PREPARE, "REVISION", commit(source))
    target = tmp_path / "utf8-prepared"
    PREPARE.prepare(source, target, selected)
    assert (target / "upstream/src/tokenizer.rs").read_bytes().startswith(comment.encode("utf-8"))


@pytest.mark.parametrize("revision", ["HEAD", "main", "a" * 39, "A" * 40, "0" * 40])
def test_noncommit_or_symbolic_product_selection_refused(tmp_path, sources, revision):
    source, _, _ = sources
    target = tmp_path / "rejected"
    with pytest.raises(ValueError):
        PREPARE.prepare(source, target, revision)
    assert not target.exists()


@pytest.mark.parametrize("change", ["missing", "executable", "symlink", "submodule"])
def test_invalid_selected_product_members_refused_before_publication(tmp_path, sources, change):
    source, product, _ = sources
    name = "native/nlp-worker/src/main.rs"
    if change == "missing":
        git(product, "rm", "-q", name)
    else:
        oid = git(product, "rev-parse", "HEAD:" + name)
        mode = {"executable": "100755", "symlink": "120000", "submodule": "160000"}[change]
        if change == "submodule":
            oid = git(product, "rev-parse", "HEAD")
        git(product, "update-index", "--cacheinfo", mode, oid, name)
    git(product, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "invalid member")
    target = tmp_path / "rejected"
    with pytest.raises(ValueError, match="required source|normal source"):
        PREPARE.prepare(source, target, git(product, "rev-parse", "HEAD"))
    assert not target.exists()


@pytest.mark.parametrize("change", ["wrong_revision", "dirty", "untracked"])
def test_wrong_or_dirty_upstream_refused(tmp_path, sources, monkeypatch, change):
    source, _, selected = sources
    if change == "wrong_revision":
        monkeypatch.setattr(PREPARE, "REVISION", "0" * 40)
    else:
        (source / ("Cargo.lock" if change == "dirty" else "untracked.txt")).write_text("changed\n")
    target = tmp_path / "rejected"
    with pytest.raises(ValueError, match="clean pinned"):
        PREPARE.prepare(source, target, selected)
    assert not target.exists()


def test_existing_target_and_source_subdirectory_are_refused(tmp_path, sources):
    source, _, selected = sources
    for target in (tmp_path, source / "build"):
        with pytest.raises(ValueError, match="fresh external"):
            PREPARE.prepare(source, target, selected)


def test_changed_patch_seam_refused_before_target_creation(tmp_path, sources, monkeypatch):
    source, _, selected = sources
    (source / "src/tokenizer.rs").write_text("different source\n")
    monkeypatch.setattr(PREPARE, "REVISION", commit(source))
    target = tmp_path / "rejected"
    with pytest.raises(ValueError, match="upstream source differs"):
        PREPARE.prepare(source, target, selected)
    assert not target.exists()


def test_product_replace_refs_cannot_substitute_selected_objects(tmp_path, sources):
    source, product, selected = sources
    member = "native/nlp-worker/src/main.rs"
    old_blob = git(product, "rev-parse", selected + ":" + member)
    (product / member).write_text("replacement bytes\n", newline="\n")
    newer = commit(product)
    new_blob = git(product, "rev-parse", newer + ":" + member)
    git(product, "replace", selected, newer)
    git(product, "replace", old_blob, new_blob)
    assert git(product, "show", selected + ":" + member) == "replacement bytes"
    target = tmp_path / "prepared"
    PREPARE.prepare(source, target, selected)
    assert (target / "src/main.rs").read_bytes() == b"selected src/main.rs\n"
    receipt = json.loads((target / "preparation.json").read_text())
    record = next(r for r in receipt["inputs"]["product"] if r["path"] == member)
    assert record["git_blob"] == old_blob
    assert record["sha256"] == hashlib.sha256(b"selected src/main.rs\n").hexdigest()


def test_full_blob_id_is_not_a_product_commit(tmp_path, sources):
    source, product, selected = sources
    blob = git(product, "rev-parse", selected + ":native/nlp-worker/src/main.rs")
    target = tmp_path / "rejected"
    with pytest.raises(ValueError, match="full immutable commit"):
        PREPARE.prepare(source, target, blob)
    assert not target.exists()


def test_public_worker_has_no_runtime_download_feature():
    manifest = (ROOT / "native/nlp-worker/Cargo.toml").read_text()
    assert 'path = "upstream"' in manifest
    assert '"load-dynamic"' in manifest
    assert 'download-binaries' not in manifest
    assert 'default-features = false' in manifest
