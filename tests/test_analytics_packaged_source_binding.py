"""A caller's source claim cannot relabel an older frozen runtime."""
from copy import deepcopy
import json
from pathlib import Path
import zipfile

import pytest

from tools import analytics_qualification as q
from tools import analytics_qualification_hardware as hardware
from tools import analytics_qualification_packaged as packaged

pytestmark = [pytest.mark.ci_tier("fast")]
REVISION = "a" * 40
PROFILE = "reference-windows-16g"
MANIFEST = q.read_json(Path(__file__).resolve().parents[1] / "docs/analytics/acceptance-manifest.json")


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    agent = runtime / "Agent"
    agent.mkdir(parents=True)
    (runtime / "Brain.exe").write_bytes(b"synthetic executable")
    (agent / "manifest.json").write_text("{}", encoding="utf-8")
    browser = tmp_path / "browser.exe"
    browser.write_bytes(b"synthetic browser")
    data, profile = tmp_path / "data", tmp_path / "browser-profile"
    data.mkdir()
    profile.mkdir()
    (data / "runtime.env").write_text("", encoding="utf-8")
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"synthetic installer")
    observed = dict(MANIFEST["profiles"][PROFILE], cpu_model="Synthetic CPU",
                    power_mode="Synthetic mode", instruction_requirements="AMD64",
                    virtualization="hyper-v-single-boot-disk", host_cpu_model="Synthetic host",
                    host_power_mode="Synthetic mode", storage_evidence={"schema": "analytics-hardware-evidence.v1"})
    monkeypatch.setattr(hardware, "observe", lambda: deepcopy(observed))
    monkeypatch.setattr(hardware, "observe_network_isolation",
                        lambda: {"active_adapters": 0, "default_routes": 0})
    monkeypatch.setattr(packaged.shutil, "which", lambda _: "synthetic-node")
    monkeypatch.setattr(packaged.subprocess, "check_output", lambda *a, **k: str(browser))

    def build(raw):
        manifest_path = runtime / "release-manifest.json"
        if raw is not None:
            manifest_path.write_bytes(raw)
        archive = tmp_path / "runtime.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for item in runtime.rglob("*"):
                if item.is_file():
                    output.write(item, item.relative_to(runtime).as_posix())
        inputs = {"schema": "analytics-package-inputs.v1", "source_revision": REVISION,
            "artifacts": {name: {"path": str(path), "sha256": q.file_digest(path)}
                          for name, path in (("installer", installer), ("runtime", archive))},
            "runtime_directory": str(runtime), "agent_directory": str(agent),
            "data_directory": str(data), "browser_profile": str(profile),
            "synthetic_account_id": "synthetic-continuous-owner"}
        source = {"revision": REVISION, "working_tree": "", "signature_valid": True}
        return inputs, source
    return build


def release_manifest(**changes):
    return dict(schema="ofca-release-manifest/v1", source_commit=REVISION,
                architecture="x64", version="0.1.0") | changes


@pytest.mark.parametrize("fault", ["older", "missing", "schema", "architecture", "malformed",
                                  "duplicate", "oversized"])
def test_preflight_rejects_unbound_archive_despite_matching_declared_hashes(candidate, fault):
    value = release_manifest()
    if fault == "older":
        value["source_commit"] = "b" * 40
    elif fault == "schema":
        value["schema"] = "unrecognized"
    elif fault == "architecture":
        value["architecture"] = "arm64"
    raw = json.dumps(value).encode()
    if fault == "missing":
        raw = None
    elif fault == "malformed":
        raw = b"["
    elif fault == "duplicate":
        raw = raw[:-1] + b', "source_commit": "' + REVISION.encode() + b'"}'
    elif fault == "oversized":
        raw += b" " * 65536
    inputs, source = candidate(raw)
    with pytest.raises(ValueError):
        packaged.prerequisites(inputs, MANIFEST, source, PROFILE)


@pytest.mark.parametrize("bom", [False, True])
def test_preflight_binds_the_packaged_manifest_bytes(candidate, bom):
    raw = json.dumps(release_manifest()).encode("utf-8-sig" if bom else "utf-8")
    inputs, source = candidate(raw)
    result = packaged.prerequisites(inputs, MANIFEST, source, PROFILE)
    assert result["runtime_source"] == packaged.runtime_source(inputs["artifacts"]["runtime"]["path"], REVISION)
    assert result["runtime_source"]["source_revision"] == REVISION
    assert result["runtime_source"]["manifest_sha256"] == result["runtime_files"]["release-manifest.json"]


@pytest.mark.parametrize("fault", ["older_archive", "missing_observation", "changed_observation"])
def test_evidence_verifier_rechecks_the_archived_source(candidate, tmp_path, fault):
    raw = json.dumps(release_manifest(source_commit="b" * 40 if fault == "older_archive" else REVISION)).encode()
    inputs, source = candidate(raw)
    context = {"source": source, "artifacts": packaged.artifact_context(inputs)}
    payload = {"subject_sha256": q.digest(source), "supervisor_instance": "synthetic", "hardware": {}}
    observed = {"hardware": {}}
    if fault != "missing_observation":
        observed["runtime_source"] = {"source_revision": REVISION, "manifest_sha256": "0" * 64}
    config = {"manifest": MANIFEST, "subject": source, "observed": observed}
    documents = {"worker-input.json": config, "payload.json": payload,
        "hardware.json": {}, "artifact-check.json": {}, "process.json": {},
        "network-isolation-before.json": {}, "network-isolation-after.json": {}}
    attempt = tmp_path / "attempt"
    references = []
    for name, value in documents.items():
        q.write_once(attempt / name, value)
        references.append({"name": name, "path": name})
    result = {"payload": payload, "process_instance": "synthetic", "attachments": references}
    assert packaged.check_evidence(attempt, result, MANIFEST, context) == [
        "packaged_source_binding_invalid" if fault == "older_archive" else "packaged_source_binding_mismatch"]
