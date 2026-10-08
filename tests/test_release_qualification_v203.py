"""Focused regression tests for Product 2.0.3 release qualification."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tools import engineering_attestation as producer

pytestmark = [pytest.mark.ci_tier('fast')]


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_KEY = json.loads((ROOT / "extension" / "manifest.json").read_text())["key"]
SOURCE_COMMIT = "b" * 40
BASELINE_COMMIT = "a" * 40
RELEASE_TAG = "v2.0.3"
RELEASE_VERSION = "2.0.3"


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=producer.FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 1
            archive.writestr(info, data)
    payload = bytearray(output.getvalue())
    cursor = 0
    while True:
        cursor = payload.find(b"PK\x01\x02", cursor)
        if cursor < 0:
            break
        struct.pack_into("<I", payload, cursor + 38, 0)
        name_length, extra_length, comment_length = struct.unpack_from(
            "<HHH", payload, cursor + 28
        )
        cursor += 46 + name_length + extra_length + comment_length
    return bytes(payload)


def _chrome_zip(version: str = RELEASE_VERSION) -> tuple[str, bytes]:
    manifest = {
        "key": MANIFEST_KEY,
        "manifest_version": 3,
        "version": version,
        "minimum_chrome_version": "132",
        "optional_host_permissions": ["https://onlyfans.com/*"],
        "externally_connectable": {"matches": ["http://bridge.localhost:17871/*"]},
        "content_security_policy": {
            "extension_pages": "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'; connect-src 'self' ws://127.0.0.1:17871;"
        },
    }
    outputs = {
        "background.js": b"export const ready = true;\n",
        "manifest.json": (json.dumps(manifest, sort_keys=True) + "\n").encode(),
    }
    metadata = {
        "schema": producer.EXTENSION_BUILD_SCHEMA,
        "extension_version": version,
        "extension_id": producer.EXPECTED_EXTENSION_ID,
        "determinism_verified": True,
        "outputs": {
            name: f"sha256:{hashlib.sha256(data).hexdigest()}"
            for name, data in outputs.items()
        },
        "target": "chrome132",
    }
    entries = outputs | {
        "build-meta.json": (json.dumps(metadata, sort_keys=True) + "\n").encode()
    }
    return (
        f"OnlyFans-Conversational-Analytics-Agent-{version}-chrome.zip",
        _zip_bytes(entries),
    )


def _actions_artifact(version: str = RELEASE_VERSION) -> tuple[bytes, str]:
    name, chrome_zip = _chrome_zip(version)
    installer_name = "OnlyFans-Conversational-Analytics-Setup-0.7.5-x64.exe"
    installer = b"signed-installer"
    sums = (
        f"{hashlib.sha256(chrome_zip).hexdigest()} *{name}\n"
        f"{hashlib.sha256(installer).hexdigest()} *{installer_name}\n"
    ).encode("ascii")
    archive = _zip_bytes(
        {name: chrome_zip, installer_name: installer, "sha256sums.txt": sums}
    )
    return archive, f"sha256:{hashlib.sha256(archive).hexdigest()}"


def _artifact(name: str, *, identifier: int) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": name,
        "expired": False,
        "digest": "sha256:" + f"{identifier:064x}"[-64:],
        "archive_download_url": f"https://api.github.com/artifacts/{identifier}/zip",
    }


class QualificationApi:
    def __init__(self, *, ci_policy: str | None = None) -> None:
        self.run = {
            "workflow_id": 77,
            "path": producer.WINDOWS_PACKAGE_WORKFLOW,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": RELEASE_TAG,
            "head_repository": {"full_name": producer.PRODUCT_REPOSITORY},
            "head_sha": SOURCE_COMMIT,
            "run_attempt": 2,
        }
        self.workflow = {"id": 77, "path": producer.WINDOWS_PACKAGE_WORKFLOW}
        self.windows_jobs = [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": 42,
                "head_sha": SOURCE_COMMIT,
            }
            for name in sorted(producer.REQUIRED_WINDOWS_JOB_NAMES)
        ]
        self.product_ci_workflow = {
            "id": 88,
            "path": producer.PRODUCT_CI_WORKFLOW,
            "state": "active",
        }
        self.product_ci_run = {
            "id": 43,
            "workflow_id": 88,
            "path": producer.PRODUCT_CI_WORKFLOW,
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_branch": producer.PRODUCT_DEFAULT_BRANCH,
            "head_sha": SOURCE_COMMIT,
            "run_attempt": 1,
            "repository": {"full_name": producer.PRODUCT_REPOSITORY},
            "head_repository": {"full_name": producer.PRODUCT_REPOSITORY},
        }
        policy_names = {
            None: producer.REQUIRED_PRODUCT_CI_JOB_NAMES,
            "sharded-v1": producer.REQUIRED_SHARDED_PRODUCT_CI_JOB_NAMES,
            "sharded-v2": producer.REQUIRED_SHARDED_V2_PRODUCT_CI_JOB_NAMES,
        }
        self.product_ci_jobs = [
            {
                "id": 4300 + index,
                "name": name,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
                "run_id": 43,
                "head_sha": SOURCE_COMMIT,
            }
            for index, name in enumerate(sorted(policy_names[ci_policy]))
        ]
        if ci_policy == "sharded-v2":
            self.product_ci_source = (ROOT / producer.PRODUCT_CI_WORKFLOW).read_bytes()
        else:
            source_jobs = (producer.SHARDED_PRODUCT_CI_JOB_IDS if ci_policy
                           else producer.REQUIRED_PRODUCT_CI_JOB_NAMES)
            self.product_ci_source = (
                "name: CI\n"
                + ("env:\n  CI_POLICY_VERSION: sharded-v1\n" if ci_policy else "")
                + "jobs:\n"
                + "".join(f"  {name}:\n    runs-on: ubuntu-latest\n" for name in sorted(source_jobs))
            ).encode()
        self.artifacts = [
            _artifact(f"windows-package-{RELEASE_TAG}", identifier=91),
            _artifact(f"windows-package-unsigned-{RELEASE_TAG}", identifier=90),
            _artifact(f"windows-package-sqlcipher-{SOURCE_COMMIT}", identifier=89),
            _artifact(f"windows-package-companion-{SOURCE_COMMIT}", identifier=88),
        ]

    def get(self, path: str) -> Any:
        if path.endswith("/actions/runs/42"):
            return self.run
        if path.endswith("/actions/workflows/77"):
            return self.workflow
        if path.endswith("/actions/workflows/ci.yml"):
            return self.product_ci_workflow
        if "/actions/workflows/88/runs?" in path:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            assert query == {
                "branch": [producer.PRODUCT_DEFAULT_BRANCH],
                "event": ["push"],
                "head_sha": [SOURCE_COMMIT],
                "status": ["success"],
                "per_page": ["100"],
            }
            return {"total_count": 1, "workflow_runs": [self.product_ci_run]}
        if path.endswith("/actions/runs/43/jobs?filter=all&per_page=100&page=1"):
            return {
                "total_count": len(self.product_ci_jobs),
                "jobs": self.product_ci_jobs,
            }
        if path.endswith(f"/git/commits/{SOURCE_COMMIT}"):
            return {"tree": {"sha": "1" * 40}}
        for tree_sha, name, kind, mode, sha in (
            ("1", ".github", "tree", "040000", "2"),
            ("2", "workflows", "tree", "040000", "3"),
            ("3", "ci.yml", "blob", "100644", "4"),
        ):
            if path.endswith(f"/git/trees/{tree_sha * 40}"):
                return {"tree": [{"path": name, "type": kind, "mode": mode, "sha": sha * 40}]}
        if path.endswith(f"/git/blobs/{'4' * 40}"):
            return {"encoding": "base64", "size": len(self.product_ci_source),
                    "content": base64.b64encode(self.product_ci_source).decode()}
        if path.endswith("/actions/runs/42/attempts/2/jobs?per_page=100"):
            return {"total_count": len(self.windows_jobs), "jobs": self.windows_jobs}
        if path.endswith("/actions/runs/42/artifacts?per_page=100"):
            return {"total_count": len(self.artifacts), "artifacts": self.artifacts}
        if "/git/ref/tags/" in path:
            return {"object": {"type": "commit", "sha": SOURCE_COMMIT}}
        if "/compare/" in path:
            return {"status": "ahead"}
        raise AssertionError(path)


def _qualify_source(api: QualificationApi) -> producer.QualifiedSource:
    return producer.qualify_windows_package_source(
        api,
        run_id=42,
        release_tag=RELEASE_TAG,
        baseline_sha=BASELINE_COMMIT,
        workflow_sha=SOURCE_COMMIT,
    )


def test_extension_input_qualification_uses_manifest_package_version() -> None:
    workflow = (ROOT / ".github" / "workflows" / "verify-extension-release-inputs.yml").read_text(
        encoding="utf-8"
    )
    assert "Get-Content -LiteralPath extension/manifest.json -Raw | ConvertFrom-Json" in workflow
    assert '("conversation-analytics-{0}.zip" -f $manifest.version)' in workflow
    assert "conversation-analytics-2.0.1.zip" not in workflow


def test_v203_release_tag_accepts_v203_packaged_agent() -> None:
    archive, digest = _actions_artifact()
    qualified = producer.qualify_downloaded_artifact(
        archive,
        expected_server_digest=digest,
        release_tag=RELEASE_TAG,
    )
    assert qualified.version == RELEASE_VERSION
    assert qualified.filename == (
        "OnlyFans-Conversational-Analytics-Agent-2.0.3-chrome.zip"
    )
    assert qualified.metadata["extension_version"] == RELEASE_VERSION


def test_release_tag_and_packaged_agent_version_mismatch_is_rejected() -> None:
    archive, digest = _actions_artifact()
    with pytest.raises(producer.ContractError, match="Agent version differ"):
        producer.qualify_downloaded_artifact(
            archive,
            expected_server_digest=digest,
            release_tag="v2.0.2",
        )


def test_historical_product_ci_requires_exact_legacy_four_job_set() -> None:
    assert producer.REQUIRED_PRODUCT_CI_JOB_NAMES == {
        "build-and-test",
        "fixed-sqlcipher-wheel",
        "windows-browser-e2e",
        "windows-tests",
    }
    result = _qualify_source(QualificationApi())
    assert result.product_ci_run_id == 43


def test_current_product_ci_requires_versioned_shards_and_gate() -> None:
    api = QualificationApi(ci_policy="sharded-v2")
    assert {"Required CI", "analytics-scale-qualification", "windows-full-regression",
            "windows-full-regression-1", "windows-full-regression-2",
            *(f"analytics-integration-{number}" for number in range(1, 5))} <= {
        job["name"] for job in api.product_ci_jobs
    }
    assert _qualify_source(api).product_ci_run_id == 43
    api.product_ci_jobs = QualificationApi().product_ci_jobs
    with pytest.raises(producer.ContractError, match="exact required job set"):
        _qualify_source(api)


@pytest.mark.parametrize("ci_policy", [None, "sharded-v1", "sharded-v2"], ids=["legacy-source", "sharded-v1", "sharded-v2"])
def test_required_package_artifacts_accept_current_evidence_artifacts(ci_policy) -> None:
    result = _qualify_source(QualificationApi(ci_policy=ci_policy))
    assert result.artifact_name == f"windows-package-{RELEASE_TAG}"
    assert result.artifact_id == 91


@pytest.mark.parametrize("ci_policy", [None, "sharded-v1", "sharded-v2"], ids=["legacy-source", "sharded-v1", "sharded-v2"])
def test_missing_or_failed_required_ci_jobs_are_rejected(ci_policy) -> None:
    missing = QualificationApi(ci_policy=ci_policy)
    missing.product_ci_jobs = [
        job
        for job in missing.product_ci_jobs
        if job["name"] != "fixed-sqlcipher-wheel"
    ]
    with pytest.raises(producer.ContractError, match="exact required job set"):
        _qualify_source(missing)

    failed = QualificationApi(ci_policy=ci_policy)
    fixed = next(
        job
        for job in failed.product_ci_jobs
        if job["name"] == "fixed-sqlcipher-wheel"
    )
    fixed["conclusion"] = "failure"
    with pytest.raises(producer.ContractError, match="required Product CI job"):
        _qualify_source(failed)


@pytest.mark.parametrize("ci_policy", [None, "sharded-v1", "sharded-v2"], ids=["legacy-source", "sharded-v1", "sharded-v2"])
def test_missing_required_or_unknown_package_artifacts_are_rejected(ci_policy) -> None:
    missing = QualificationApi(ci_policy=ci_policy)
    missing.artifacts = [
        artifact
        for artifact in missing.artifacts
        if artifact["name"] != f"windows-package-unsigned-{RELEASE_TAG}"
    ]
    with pytest.raises(producer.ContractError, match="artifact identity/count"):
        _qualify_source(missing)

    unknown = QualificationApi(ci_policy=ci_policy)
    unknown.artifacts.append(_artifact("unexpected-evidence", identifier=87))
    with pytest.raises(producer.ContractError, match="artifact identity/count"):
        _qualify_source(unknown)
