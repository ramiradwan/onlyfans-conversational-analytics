"""Focused regression tests for Product 2.0.3 release qualification."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools import engineering_attestation as producer

pytestmark = [pytest.mark.ci_tier('fast')]


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_KEY = json.loads((ROOT / "extension" / "manifest.json").read_text())["key"]
SOURCE_COMMIT = "b" * 40
BASELINE_COMMIT = "a" * 40
RELEASE_TAG = "v2.0.3"
RELEASE_VERSION = "2.0.3"
BROWSER_CI_POLICIES = ["sharded-v3", "sharded-v3-pr-cutover", "sharded-v4-pr-cutover", "sharded-v5-pr-cutover", "sharded-v6-pr-cutover"]
PRODUCT_CI_POLICIES = [None, "sharded-v1", "sharded-v2", "sharded-v2-pr-cutover", *BROWSER_CI_POLICIES]


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
        "web_accessible_resources": [
            {"resources": ["setup.html"], "matches": ["http://bridge.localhost/*"]}
        ],
        "content_security_policy": {
            "extension_pages": "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'; connect-src 'self' ws://127.0.0.1:17871; frame-ancestors 'none';"
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
            "sharded-v2-pr-cutover": producer.REQUIRED_SHARDED_V2_PRODUCT_CI_JOB_NAMES,
            "sharded-v3": producer.REQUIRED_SHARDED_V3_PRODUCT_CI_JOB_NAMES,
            "sharded-v3-pr-cutover": producer.REQUIRED_SHARDED_V3_PRODUCT_CI_JOB_NAMES,
            "sharded-v4-pr-cutover": producer.REQUIRED_SHARDED_V4_PRODUCT_CI_JOB_NAMES,
            "sharded-v5-pr-cutover": producer.REQUIRED_SHARDED_V4_PRODUCT_CI_JOB_NAMES,
            "sharded-v6-pr-cutover": producer.REQUIRED_SHARDED_V4_PRODUCT_CI_JOB_NAMES,
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
        if ci_policy in BROWSER_CI_POLICIES:
            next(job for job in self.product_ci_jobs if job["name"] == "browser-e2e-serial-control")["conclusion"] = "skipped"
        if ci_policy in {"sharded-v2", "sharded-v2-pr-cutover", "sharded-v3", "sharded-v3-pr-cutover", "sharded-v4-pr-cutover", "sharded-v5-pr-cutover"}:
            self.product_ci_source = (ROOT / f"tests/fixtures/product-ci-{ci_policy}.yml").read_bytes()
        elif ci_policy == "sharded-v6-pr-cutover":
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
        # Model the Git blob served by the API, not checkout line endings.
        # Mutation cases must target the same literal source on Windows/Linux.
        self.product_ci_source = self.product_ci_source.replace(b"\r\n", b"\n")
        self.product_ci_policy = (ROOT / (
            f"tests/fixtures/product-ci-{ci_policy}-policy.json" if ci_policy in {"sharded-v3", "sharded-v3-pr-cutover", "sharded-v4-pr-cutover", "sharded-v5-pr-cutover"}
            else producer.PRODUCT_CI_POLICY_PATH
        )).read_bytes()
        self.artifacts = [
            _artifact(f"windows-package-{RELEASE_TAG}", identifier=91),
            _artifact(f"windows-package-unsigned-{RELEASE_TAG}", identifier=90),
            _artifact(f"windows-package-sqlcipher-{SOURCE_COMMIT}", identifier=89),
            _artifact(f"windows-package-companion-{SOURCE_COMMIT}", identifier=88),
            _artifact(f"windows-package-measurements-{SOURCE_COMMIT}", identifier=85),
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
        if path.endswith(f"/git/trees/{'1' * 40}"):
            return {"tree": [
                {"path": ".github", "type": "tree", "mode": "040000", "sha": "2" * 40},
                {"path": "ci", "type": "tree", "mode": "040000", "sha": "5" * 40},
            ]}
        if path.endswith(f"/git/trees/{'5' * 40}"):
            return {"tree": [{"path": "product-ci-policy.json", "type": "blob", "mode": "100644", "sha": "6" * 40}]}
        if path.endswith(f"/git/blobs/{'6' * 40}"):
            return {"encoding": "base64", "size": len(self.product_ci_policy),
                    "content": base64.b64encode(self.product_ci_policy).decode()}
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


@pytest.mark.parametrize("name", ["browser-reporting-safety", "browser-e2e-core", "browser-e2e-catchup", "windows-browser-e2e"])
@pytest.mark.parametrize("failure", ["missing", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("ci_policy", BROWSER_CI_POLICIES)
def test_v3_full_release_qualification_requires_every_browser_boundary(name, failure, ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    assert _qualify_source(api).product_ci_run_id == 43
    if failure == "missing":
        api.product_ci_jobs = [job for job in api.product_ci_jobs if job["name"] != name]
    else:
        next(job for job in api.product_ci_jobs if job["name"] == name)["conclusion"] = failure
    with pytest.raises(producer.ContractError):
        _qualify_source(api)


@pytest.mark.parametrize("failure", ["missing", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("ci_policy", ["sharded-v4-pr-cutover", "sharded-v5-pr-cutover", "sharded-v6-pr-cutover"])
def test_v4_release_requires_successful_native_desktop_execution(failure, ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    assert _qualify_source(api).product_ci_run_id == 43
    if failure == "missing":
        api.product_ci_jobs = [job for job in api.product_ci_jobs if job["name"] != "desktop-native-contracts"]
    else:
        next(job for job in api.product_ci_jobs if job["name"] == "desktop-native-contracts")["conclusion"] = failure
    with pytest.raises(producer.ContractError):
        _qualify_source(api)


@pytest.mark.parametrize("old,new", [
    ("  desktop-native-contracts:\n", "  omitted-native-contracts:\n"),
    ("  desktop-native-contracts:\n", "  desktop-native-contracts:\n    if: false\n"),
    ("  desktop-native-contracts:\n", "  desktop-native-contracts:\n    continue-on-error: true\n"),
    ("  desktop-native-contracts:\n", "  desktop-native-contracts:\n    name: other-name\n"),
    ("    runs-on: windows-latest\n", "    runs-on: ubuntu-latest\n"),
    ("    timeout-minutes: 20\n", "    timeout-minutes: 60\n"),
    ("      - desktop-native-contracts\n", ""),
])
@pytest.mark.parametrize("ci_policy", ["sharded-v4-pr-cutover", "sharded-v5-pr-cutover", "sharded-v6-pr-cutover"])
def test_v4_release_refuses_optional_or_unbounded_native_contracts(old, new, ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    assert old.encode() in api.product_ci_source
    api.product_ci_source = api.product_ci_source.replace(old.encode(), new.encode(), 1)
    with pytest.raises(producer.ContractError):
        _qualify_source(api)


@pytest.mark.parametrize("ci_policy,minutes", [("sharded-v4-pr-cutover", "60"), ("sharded-v5-pr-cutover", "90"), ("sharded-v6-pr-cutover", "90")])
@pytest.mark.parametrize("replacement", ["59", "60", "89", "90", "91", "120", "${{ inputs.timeout }}", None])
def test_full_windows_timeout_is_exact_for_each_source_policy(ci_policy, minutes, replacement):
    api = QualificationApi(ci_policy=ci_policy)
    assert _qualify_source(api).source_commit == SOURCE_COMMIT
    before, body = api.product_ci_source.decode().split("  windows-full-shards:\n", 1)
    matrix, after = body.split("  windows-full-regression:\n", 1)
    declaration = f"    timeout-minutes: {minutes}\n"
    assert declaration in matrix
    changed = "" if replacement is None else f"    timeout-minutes: {replacement}\n"
    api.product_ci_source = (before + "  windows-full-shards:\n"
                             + matrix.replace(declaration, changed, 1)
                             + "  windows-full-regression:\n" + after).encode()
    if replacement == minutes:
        assert _qualify_source(api).source_commit == SOURCE_COMMIT
    else:
        with pytest.raises(producer.ContractError, match="matrix execution declaration"):
            _qualify_source(api)


@pytest.mark.parametrize("ci_policy,other_policy", [
    ("sharded-v4-pr-cutover", "sharded-v5-pr-cutover"),
    ("sharded-v5-pr-cutover", "sharded-v4-pr-cutover"),
    ("sharded-v5-pr-cutover", "sharded-v6-pr-cutover"),
    ("sharded-v6-pr-cutover", "sharded-v5-pr-cutover"),
])
def test_full_windows_budget_policy_never_accepts_another_versions_document(ci_policy, other_policy):
    api = QualificationApi(ci_policy=ci_policy)
    api.product_ci_policy = QualificationApi(ci_policy=other_policy).product_ci_policy
    with pytest.raises(producer.ContractError, match="source policy"):
        _qualify_source(api)


@pytest.mark.parametrize("ci_policy", BROWSER_CI_POLICIES)
def test_v3_full_release_qualification_retains_older_dependencies_but_newer_failure_wins(ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    api.product_ci_run["run_attempt"] = 2
    original = next(job for job in api.product_ci_jobs if job["name"] == "browser-e2e-catchup")
    newest = dict(original, id=99999, run_attempt=2)
    api.product_ci_jobs.append(newest)
    assert _qualify_source(api).source_commit == SOURCE_COMMIT
    newest["conclusion"] = "failure"
    with pytest.raises(producer.ContractError, match="required Product CI job"):
        _qualify_source(api)


@pytest.mark.parametrize("mutation", [
    lambda policy: policy["required_job_names"].remove("browser-e2e-catchup"),
    lambda policy: policy["job_ids"].remove("browser-reporting-safety"),
    lambda policy: policy.update(allowed_skipped_jobs=["browser-e2e-core"]),
    lambda policy: policy.update(policy_version="sharded-v2"),
    lambda policy: policy.update(schema_version=True),
])
@pytest.mark.parametrize("ci_policy", BROWSER_CI_POLICIES)
def test_v3_full_release_qualification_reads_and_refuses_weakened_source_policy(mutation, ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    policy = json.loads(api.product_ci_policy)
    mutation(policy)
    api.product_ci_policy = json.dumps(policy).encode()
    with pytest.raises(producer.ContractError, match="source policy"):
        _qualify_source(api)


@pytest.mark.parametrize("old,new", [
    ("    name: browser-e2e-${{ matrix.lane }}", "    name: browser-e2e-core"),
    ("      max-parallel: 2", "      max-parallel: 3"),
    ("          - catchup", "          - core"),
    ("    runs-on: windows-latest", "    runs-on: ubuntu-latest"),
    ("    timeout-minutes: 45", "    timeout-minutes: 45\n    continue-on-error: true"),
    ("    strategy:", "    if: false\n    strategy:"),
])
@pytest.mark.parametrize("ci_policy", BROWSER_CI_POLICIES)
def test_v3_full_release_qualification_refuses_weakened_browser_topology(old, new, ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    before, body = api.product_ci_source.decode().split("  browser-e2e-execution:\n", 1)
    matrix, after = body.split("  windows-browser-e2e:\n", 1)
    assert old in matrix
    api.product_ci_source = (before + "  browser-e2e-execution:\n" + matrix.replace(old, new, 1)
                             + "  windows-browser-e2e:\n" + after).encode()
    with pytest.raises(producer.ContractError):
        _qualify_source(api)


@pytest.mark.parametrize("ci_policy", BROWSER_CI_POLICIES)
def test_v3_policy_never_falls_back_when_source_document_is_missing(ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    api.product_ci_policy = b"{}"
    with pytest.raises(producer.ContractError, match="source policy"):
        _qualify_source(api)


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled", None])
@pytest.mark.parametrize("ci_policy", BROWSER_CI_POLICIES)
def test_v3_main_push_requires_only_the_explicit_serial_control_skip(conclusion, ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    assert _qualify_source(api).source_commit == SOURCE_COMMIT
    next(job for job in api.product_ci_jobs if job["name"] == "browser-e2e-serial-control")["conclusion"] = conclusion
    with pytest.raises(producer.ContractError):
        _qualify_source(api)


@pytest.mark.parametrize("replacement", [
    "${{ always() }}", "${{ inputs.browser_serial_control }}", "${{ github.event_name == 'workflow_dispatch' }}", "false",
])
@pytest.mark.parametrize("ci_policy", BROWSER_CI_POLICIES)
def test_v3_release_requires_the_exact_opt_in_control_condition(replacement, ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    before, control = api.product_ci_source.decode().split("  browser-e2e-serial-control:\n", 1)
    assert producer.BROWSER_SERIAL_CONTROL_IF in control
    api.product_ci_source = (before + "  browser-e2e-serial-control:\n"
        + control.replace(producer.BROWSER_SERIAL_CONTROL_IF, replacement, 1)).encode()
    with pytest.raises(producer.ContractError, match="serial control"):
        _qualify_source(api)


@pytest.mark.parametrize("ci_policy", BROWSER_CI_POLICIES)
def test_v3_release_cannot_omit_the_optional_job_record_or_aggregate_dependency(ci_policy):
    api = QualificationApi(ci_policy=ci_policy)
    api.product_ci_jobs = [job for job in api.product_ci_jobs if job["name"] != "browser-e2e-serial-control"]
    with pytest.raises(producer.ContractError, match="exact required job set"):
        _qualify_source(api)
    api = QualificationApi(ci_policy=ci_policy)
    before, aggregate = api.product_ci_source.decode().split("  windows-browser-e2e:\n", 1)
    api.product_ci_source = (before + "  windows-browser-e2e:\n"
        + aggregate.replace("      - browser-e2e-serial-control\n", "", 1)).encode()
    with pytest.raises(producer.ContractError, match="browser gate"):
        _qualify_source(api)


@pytest.mark.parametrize("ci_policy", ["sharded-v2-pr-cutover", *BROWSER_CI_POLICIES])
@pytest.mark.parametrize("name", ["windows-full-regression-1", "windows-full-regression-2", "windows-full-regression"])
@pytest.mark.parametrize("failure", ["missing", "failure", "skipped", "cancelled"])
def test_pr_cutover_release_keeps_every_main_windows_execution_required(ci_policy, name, failure):
    api = QualificationApi(ci_policy=ci_policy)
    assert _qualify_source(api).product_ci_run_id == 43
    if failure == "missing":
        api.product_ci_jobs = [job for job in api.product_ci_jobs if job["name"] != name]
    else:
        next(job for job in api.product_ci_jobs if job["name"] == name)["conclusion"] = failure
    with pytest.raises(producer.ContractError):
        _qualify_source(api)


@pytest.mark.parametrize("old,new", [
    ("github.event_name == 'push' || github.event_name == 'workflow_dispatch'", "github.event_name != 'pull_request'"),
    ("github.event_name == 'push' || github.event_name == 'workflow_dispatch'", "false"),
    ("pull_request) test '${{ needs.windows-full-shards.result }}' = 'skipped'", "pull_request) true"),
    ("push|workflow_dispatch) test '${{ needs.windows-full-shards.result }}' = 'success'", "push|workflow_dispatch) true"),
    ("*) echo \"Unsupported Product CI event\"; exit 1", "*) true"),
    ("  workflow_dispatch:\n", "  schedule:\n    - cron: 17 3 * * *\n  workflow_dispatch:\n"),
    ("  windows-platform-contract:\n", "  windows-platform-contract:\n    if: false\n"),
    ("  analytics-windows-contract:\n", "  analytics-windows-contract:\n    continue-on-error: true\n"),
    ("      - main\n", "      - other\n"),
    ("      - main\n", "      - main\n    paths:\n      - frontend/**\n"),
    ("      - reopened\n", "      - reopened\n      - edited\n"),
    ("        type: boolean", "        type: string"),
    ("        default: false", "        default: true"),
    ("      browser_qualification:\n", "      unknown_input:\n"),
    ("    inputs:\n", "    inputs:\n      extra_input:\n        type: boolean\n        default: false\n"),
    ("  workflow_dispatch:\n", "  workflow_dispatch: {}\n"),
])
def test_v3_cutover_release_refuses_weakened_events_windows_or_manual_inputs(old, new):
    api = QualificationApi(ci_policy="sharded-v3-pr-cutover")
    source = api.product_ci_source.decode().replace("\r\n", "\n")
    assert old in source
    api.product_ci_source = source.replace(old, new, 1).encode()
    with pytest.raises(producer.ContractError):
        _qualify_source(api)


def test_cutover_policy_cannot_downgrade_to_historical_v3_or_omit_policy_fetch():
    api = QualificationApi(ci_policy="sharded-v3-pr-cutover")
    assert _qualify_source(api).product_ci_run_id == 43
    api.product_ci_policy = b"{}"
    with pytest.raises(producer.ContractError, match="source policy"):
        _qualify_source(api)
    api = QualificationApi(ci_policy="sharded-v3-pr-cutover")
    api.product_ci_source = api.product_ci_source.replace(b"sharded-v3-pr-cutover", b"sharded-v3")
    policy = json.loads(api.product_ci_policy)
    policy["policy_version"] = "sharded-v3"
    api.product_ci_policy = json.dumps(policy).encode()
    with pytest.raises(producer.ContractError, match="matrix execution declaration"):
        _qualify_source(api)


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


def _msix_actions_artifact(target, mutation=None):
    """Container/linkage evidence only; synthetic MSIX bytes prove no Windows trust."""
    from tools.msix_packaging import render_appinstaller
    profile = json.loads((ROOT / "packaging/msix/profiles" / ("production-direct.json" if target == "msix-direct" else "production-store.json")).read_text())
    sequence = profile["release_sequence"]
    version = f"1.{sequence // 65536}.{sequence % 65536}.0"
    package_name = f"ConversationAnalytics_{version}_x64.msix"
    agent_name, agent = _chrome_zip()
    package = b"synthetic package: not signature qualification"
    digest = lambda data: hashlib.sha256(data).hexdigest()
    encode = lambda obj: (json.dumps(obj, sort_keys=True) + "\n").encode()
    distribution, mode, signature = (("direct-appinstaller", "DirectSigned", "trusted-timestamped")
        if target == "msix-direct" else ("microsoft-store", "StoreSubmission", "unsigned-store-submission"))
    family = ("Dipsy.OnlyFansConversationalAnalytics_bvmm2ppd090g2" if target == "msix-direct"
        else "Dipsy.ConversationAnalytics_nhwt8as2n81zj")
    outer = {package_name:package, agent_name:agent, "desktop.profile.json":encode(profile)}
    inventory = {"schema":"ofca-msix-inventory/v1", "identity":profile["identity"], "profile_sha256":digest(outer["desktop.profile.json"]),
        "package_version":version, "application_version":profile["application_version"], "runtime_release":{"source_commit":SOURCE_COMMIT}}
    build = {"producer_source":{"base_commit":SOURCE_COMMIT,"dirty":False}, "verification":{"package_sha256":"0" * 64}}
    if mutation == "runtime-source":
        inventory["runtime_release"]["source_commit"] = "c" * 40
    outer["desktop.inventory.json"] = encode(inventory)
    outer["desktop.build.json"] = encode(build)
    verification = {"schema":"ofca-msix-release-verification/v1", "purpose":"production", "distribution":distribution,
        "mode":mode, "signature":signature, "package_sha256":digest(package), "profile_sha256":digest(outer["desktop.profile.json"]),
        "inventory_sha256":digest(outer["desktop.inventory.json"]), "identity":{"Name":profile["identity"]["name"],
            "Publisher":profile["identity"]["publisher"], "FamilyName":family, "Version":version, "Architecture":"x64"}}
    if mutation == "wrong-publisher":
        verification["identity"]["Publisher"] = "CN=Other"
    outer["desktop.verification.json"] = encode(verification)
    candidate = {"schema":"ofca-msix-release-inputs/v1", "target":target, "source_commit":SOURCE_COMMIT, "source_tree":"1" * 40,
        "package_file":package_name, "package_version":version, "application_version":profile["application_version"],
        "profile_sha256":digest(outer["desktop.profile.json"]), "inventory_sha256":digest(outer["desktop.inventory.json"]),
        "build_sha256":digest(outer["desktop.build.json"]), "unsigned_package_sha256":"0" * 64, "agent_file":agent_name,
        "agent_sha256":digest(agent), "expected_family":family, "direct_delivery_sha256":None, "appinstaller_template_sha256":None}
    final = {key:value for key,value in candidate.items() if key not in {"unsigned_package_sha256", "direct_delivery_sha256", "appinstaller_template_sha256"}}
    final.update(schema="ofca-desktop-artifact-release/v1", package_sha256=digest(package), verification_sha256=digest(outer["desktop.verification.json"]),
        signature_scope=signature, resolved_direct_inputs_sha256=None, descriptor_sha256=None)
    if target == "msix-direct":
        prefix = "https://updates.dipsy.fi/desktop/v1/packages/"
        template = render_appinstaller(profile, prefix + "0" * 64 + "/" + package_name)
        descriptor = render_appinstaller(profile, prefix + digest(package) + "/" + package_name)
        if mutation == "descriptor-origin":
            descriptor = descriptor.replace(b"updates.dipsy.fi/desktop/v1/packages", b"other.example.test/desktop/v1/packages")
        if mutation == "descriptor-policy":
            descriptor = descriptor.replace(b'ShowPrompt="false"', b'ShowPrompt="true"')
        outer.update({"desktop.appinstaller-template.xml":template, "ConversationAnalytics.appinstaller":descriptor,
            "desktop.resolved-direct-inputs.json":encode({"schema":"ofca-direct-release-environment/v1"})})
        candidate["appinstaller_template_sha256"] = digest(template)
        candidate["direct_delivery_sha256"] = "d" * 64
        final["descriptor_sha256"] = digest(descriptor)
        final["resolved_direct_inputs_sha256"] = digest(outer["desktop.resolved-direct-inputs.json"])
    if mutation == "source-tree":
        candidate["source_tree"] = final["source_tree"] = "2" * 40
    if mutation == "agent-link":
        candidate["agent_sha256"] = final["agent_sha256"] = "0" * 64
    if mutation == "channel-scope":
        final["signature_scope"] = "unqualified"
    if mutation == "store-feed-authority":
        candidate["direct_delivery_sha256"] = "d" * 64
    outer["desktop.release-inputs.json"] = encode(candidate)
    outer["desktop.artifact-release.json"] = encode(final)
    if mutation == "package-bytes":
        outer[package_name] = b"different final package bytes"
    if mutation == "extra-file":
        outer["unapproved.exe"] = b"unexpected"
    outer["sha256sums.txt"] = "".join(f"{digest(data)} *{name}\n" for name,data in sorted(outer.items())).encode()
    archive = _zip_bytes(outer)
    return archive, "sha256:" + digest(archive), profile


@pytest.mark.parametrize("target", ["msix-direct", "msix-store"])
def test_msix_container_extracts_the_same_agent_without_attesting_desktop_trust(target):
    archive, digest, profile = _msix_actions_artifact(target)
    source = _qualify_source(QualificationApi())
    qualified = producer.qualify_downloaded_artifact(archive, expected_server_digest=digest, release_tag=RELEASE_TAG,
        source=source, profile_reader=lambda route: profile if route == target else {})
    assert qualified.filename == _chrome_zip()[0]
    assert qualified.sha256 == hashlib.sha256(_chrome_zip()[1]).hexdigest()
    with pytest.raises(producer.ContractError, match="resolved source"):
        producer.qualify_downloaded_artifact(archive, expected_server_digest=digest, release_tag=RELEASE_TAG)


@pytest.mark.parametrize("target", ["msix-direct", "msix-store"])
@pytest.mark.parametrize("profile_matches", [True, False])
def test_protected_msix_signer_resolves_source_profile_before_key_use(
    monkeypatch, target, profile_matches
):
    """Drive the protected command through the real container/profile gate."""
    archive, digest, profile = _msix_actions_artifact(target)

    class Api(QualificationApi):
        downloads = 0

        def get(self, path):
            if path.endswith("/git/ref/heads/main"):
                return {"object": {"type": "commit", "sha": SOURCE_COMMIT}}
            return super().get(path)

        def download(self, path, destination):
            assert path.endswith("/actions/artifacts/91/zip")
            self.downloads += 1
            destination.write_bytes(archive)

    api = Api()
    api.artifacts[0]["digest"] = digest
    profile_reads, qualified_agents, sensitive_calls = [], [], []
    real_fetch = producer.fetch_git_blob
    profile_path = "packaging/msix/profiles/" + (
        "production-direct.json" if target == "msix-direct" else "production-store.json"
    )

    def fetch_profile(client, repository, path, commit):
        if path != profile_path:
            return real_fetch(client, repository, path, commit)
        assert client is api and repository == producer.PRODUCT_REPOSITORY
        assert commit == SOURCE_COMMIT
        profile_reads.append(path)
        selected = json.loads(json.dumps(profile))
        if not profile_matches:
            selected["identity"]["publisher"] = "CN=Unapproved"
        return "7" * 40, json.dumps(selected).encode()

    def refuse_downstream(chrome_zip, coordinates, **kwargs):
        qualified_agents.append(chrome_zip.sha256)
        raise producer.ContractError("synthetic downstream qualification refusal")

    def forbid_sensitive_use(*args, **kwargs):
        sensitive_calls.append(True)
        pytest.fail("container qualification must precede key, signing and handoff use")

    for name, value in {
        "GITHUB_TOKEN": "synthetic-product-token",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SOURCE_COMMIT,
        "PRODUCER_WORKFLOW_SHA": SOURCE_COMMIT,
        "PRODUCER_CONTROL_BASELINE_SHA": BASELINE_COMMIT,
        "REVIEW_REPOSITORY": "review-owner/evidence-control",
        "REVIEW_DEFAULT_BRANCH": "main",
        "REVIEW_PROJECTION_PATH": "projection/review-state.json",
        "REVIEW_PROJECTION_DIGEST_PATH": "projection/review-state.sha256",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(producer, "GitHubApi", lambda *args, **kwargs: api)
    monkeypatch.setattr(producer, "fetch_git_blob", fetch_profile)
    monkeypatch.setattr(producer, "verify_conformance_vector", lambda *args, **kwargs: None)
    monkeypatch.setattr(producer, "qualify_store_release", refuse_downstream)
    for name in ("_decode_secret_to_file", "sign_ed25519", "acquire_handoff_client", "create_evidence_pr"):
        monkeypatch.setattr(producer, name, forbid_sensitive_use)
    args = argparse.Namespace(
        windows_package_run_id=42, release_tag=RELEASE_TAG,
        legal_repository_revision="c" * 40, legal_bindings_repository_revision="d" * 40,
        legal_bindings_path=f"compliance/cws/releases/{RELEASE_VERSION}/legal-release-bindings.json",
        legal_bindings_digest="e" * 64,
    )
    expected = "synthetic downstream qualification refusal" if profile_matches else "source-approved"
    with pytest.raises(producer.ContractError, match=expected):
        producer.command_sign_and_handoff(args)
    assert api.downloads == 1 and profile_reads == [profile_path]
    assert qualified_agents == ([hashlib.sha256(_chrome_zip()[1]).hexdigest()] if profile_matches else [])
    assert sensitive_calls == []


@pytest.mark.parametrize("mutation", ["runtime-source", "source-tree", "wrong-publisher", "agent-link", "channel-scope", "package-bytes", "extra-file", "descriptor-origin", "descriptor-policy"])
def test_msix_agent_container_refuses_rehashed_but_inconsistent_metadata(mutation):
    archive, digest, profile = _msix_actions_artifact("msix-direct", mutation)
    with pytest.raises(producer.ContractError):
        producer.qualify_downloaded_artifact(archive, expected_server_digest=digest, release_tag=RELEASE_TAG,
            source=_qualify_source(QualificationApi()), profile_reader=lambda route: profile)


def test_msix_agent_profile_must_come_from_the_resolved_source():
    archive, digest, profile = _msix_actions_artifact("msix-direct")
    profile["purpose"] = "qualification"
    with pytest.raises(producer.ContractError, match="source-approved"):
        producer.qualify_downloaded_artifact(archive, expected_server_digest=digest, release_tag=RELEASE_TAG,
            source=_qualify_source(QualificationApi()), profile_reader=lambda route: profile)


def test_store_agent_container_cannot_carry_direct_delivery_authority():
    archive, digest, profile = _msix_actions_artifact("msix-store", "store-feed-authority")
    with pytest.raises(producer.ContractError, match="direct delivery"):
        producer.qualify_downloaded_artifact(archive, expected_server_digest=digest, release_tag=RELEASE_TAG,
            source=_qualify_source(QualificationApi()), profile_reader=lambda route: profile)


def test_current_publisher_artifacts_are_scoped_to_the_exact_run_and_attempt():
    api = QualificationApi()
    api.artifacts.append(_artifact(f"desktop-publisher-runtime-{SOURCE_COMMIT}-42-2", identifier=93))
    api.artifacts.append(_artifact(f"desktop-publication-{SOURCE_COMMIT}-42-2", identifier=94))
    assert _qualify_source(api).source_tree == "1" * 40
    api.artifacts[-1]["name"] = f"desktop-publication-{SOURCE_COMMIT}-42-1"
    with pytest.raises(producer.ContractError, match="artifact identity/count"):
        _qualify_source(api)


def test_measurement_artifact_matches_the_workflow_without_replacing_the_package():
    workflow = yaml.safe_load((ROOT / producer.WINDOWS_PACKAGE_WORKFLOW).read_text())
    measurements = next(
        step for step in workflow["jobs"]["build"]["steps"]
        if step.get("name") == "Retain package size measurements"
    )
    assert measurements["uses"].startswith("actions/upload-artifact@")
    assert "if" not in measurements
    assert measurements["with"]["if-no-files-found"] == "error"
    artifact_name = measurements["with"]["name"].replace(
        "${{ inputs.product_revision }}", SOURCE_COMMIT
    )
    api = QualificationApi()
    assert api.artifacts[-1]["name"] == artifact_name
    result = _qualify_source(api)
    assert result.artifact_name == f"windows-package-{RELEASE_TAG}"
    assert result.artifact_id == 91
    assert result.artifact_server_digest == api.artifacts[0]["digest"]


def test_historical_package_runs_may_omit_measurement_evidence():
    api = QualificationApi()
    api.artifacts = [
        artifact for artifact in api.artifacts
        if artifact["name"] != f"windows-package-measurements-{SOURCE_COMMIT}"
    ]
    assert _qualify_source(api).artifact_id == 91


def test_measurement_evidence_uses_package_source_when_the_producer_is_newer():
    api = QualificationApi()
    workflow_sha = "c" * 40

    def qualify():
        return producer.qualify_windows_package_source(
            api, run_id=42, release_tag=RELEASE_TAG,
            baseline_sha=BASELINE_COMMIT, workflow_sha=workflow_sha,
        )

    assert qualify().source_commit == SOURCE_COMMIT
    api.artifacts[-1]["name"] = f"windows-package-measurements-{workflow_sha}"
    with pytest.raises(producer.ContractError, match="artifact identity/count"):
        qualify()


@pytest.mark.parametrize("name", [
    "windows-package-measurements-" + "e" * 40,
    f"windows-package-measurements-{RELEASE_TAG}",
    f"windows-package-measurements-{SOURCE_COMMIT}-42-2",
])
def test_measurement_evidence_rejects_any_other_source_or_name(name):
    api = QualificationApi()
    api.artifacts[-1]["name"] = name
    with pytest.raises(producer.ContractError, match="artifact identity/count"):
        _qualify_source(api)


def test_duplicate_measurement_evidence_is_rejected():
    api = QualificationApi()
    api.artifacts.append(_artifact(
        f"windows-package-measurements-{SOURCE_COMMIT}", identifier=86
    ))
    with pytest.raises(producer.ContractError, match="artifact identity/count"):
        _qualify_source(api)


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


@pytest.mark.parametrize("ci_policy", PRODUCT_CI_POLICIES, ids=["legacy-source" if policy is None else policy for policy in PRODUCT_CI_POLICIES])
def test_required_package_artifacts_accept_current_evidence_artifacts(ci_policy) -> None:
    result = _qualify_source(QualificationApi(ci_policy=ci_policy))
    assert result.artifact_name == f"windows-package-{RELEASE_TAG}"
    assert result.artifact_id == 91


@pytest.mark.parametrize("ci_policy", PRODUCT_CI_POLICIES, ids=["legacy-source" if policy is None else policy for policy in PRODUCT_CI_POLICIES])
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


@pytest.mark.parametrize("ci_policy", PRODUCT_CI_POLICIES, ids=["legacy-source" if policy is None else policy for policy in PRODUCT_CI_POLICIES])
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


_SPARSE_AUTHORITY_RUNNER = r"""
import base64, json, runpy, sys
from pathlib import Path
assert sys.flags.isolated and sys.flags.no_site
assert not any('site-packages' in entry for entry in sys.path)
module = runpy.run_path(sys.argv[1], run_name='sparse_protected_producer')
payload = json.loads(Path(sys.argv[2]).read_bytes())
try:
    if payload['mode'] == 'container':
        qualified = module['qualify_downloaded_artifact'](
            base64.b64decode(payload['archive']),
            expected_server_digest=payload['digest'], release_tag=payload['tag'])
        result = {'version': qualified.version}
    else:
        qualified = module['QualifiedChromeZip'](
            filename='synthetic-public-authority.zip', version='2.0.4',
            sha256='0' * 64, size_bytes=1, bytes=b'x',
            actions_archive_sha256='0' * 64,
            entries={name: base64.b64decode(value) for name, value in payload['entries'].items()},
            metadata=payload['metadata'])
        result = vars(module['qualify_packaged_signing_parameters'](qualified))
except module['QualificationError'] as error:
    print(json.dumps({'outcome': 'refused', 'step': error.step}))
    raise SystemExit(error.exit_code)
except module['ContractError']:
    print(json.dumps({'outcome': 'refused', 'step': 'container'}))
    raise SystemExit(2)
assert not any(name == 'app' or name.startswith('app.')
               or name == 'cryptography' or name.startswith('cryptography.')
               for name in sys.modules)
print(json.dumps({'outcome': 'accepted', 'result': result,
                  'isolated': bool(sys.flags.isolated), 'no_site': bool(sys.flags.no_site)}))
"""


def _run_sparse_authority_probe(tmp_path, payload, *, configuration="present"):
    workflow = yaml.safe_load((ROOT / producer.PRODUCER_WORKFLOW).read_text())
    checkout = next(step for step in workflow["jobs"]["sign-and-handoff"]["steps"]
                    if step.get("uses", "").startswith("actions/checkout@"))
    declared_files = checkout["with"]["sparse-checkout"].split()
    assert "tools/engineering_attestation.py" in declared_files
    assert "app/core/customer-release.json" in declared_files
    sparse = tmp_path / "declared-sparse-checkout"
    for relative in declared_files:
        original, copied = ROOT / relative, sparse / relative
        assert original.is_file()
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, copied)
    assert {path.relative_to(sparse).as_posix() for path in sparse.rglob("*.py")} == {
        "tools/engineering_attestation.py"
    }
    config = sparse / "app/core/customer-release.json"
    if configuration == "missing":
        config.unlink()
    elif configuration == "mismatched":
        value = json.loads(config.read_bytes())
        value["hosted_api_origin"] = "https://different.example.test"
        config.write_text(json.dumps(value), encoding="utf-8")
    request = tmp_path / "public-resource-probe.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    unrelated = tmp_path / "unrelated-working-directory"
    unrelated.mkdir()
    environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "ComSpec")
                   if name in os.environ}
    environment.update(TEMP=str(unrelated), TMP=str(unrelated))
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", _SPARSE_AUTHORITY_RUNNER,
         str(sparse / "tools/engineering_attestation.py"), str(request)],
        cwd=unrelated, env=environment, capture_output=True, text=True, timeout=20,
    )


@pytest.mark.parametrize("variant", [
    "canonical", "reordered", "whitespace", "escaped-ascii", "invalid-schema",
    "extra-member", "key-replacement", "wrong-curve", "off-curve", "empty-roots",
    "duplicate-key", "nan", "missing-runtime", "rebound-runtime",
])
def test_protected_signing_authority_qualifies_without_app_or_site_packages(tmp_path, variant):
    trust = json.loads((ROOT / "app/security/signing-publication-trust.json").read_bytes())
    canonical = json.dumps(trust, sort_keys=True, separators=(",", ":")).encode()
    trust_hash = hashlib.sha256(canonical).hexdigest()
    assert trust_hash == "9a0965a3684afae9b4745d9c36845abb2f0a168ce3b1da3256567720542873c9"
    encoded = base64.b64encode(canonical)
    background = b"const publicTrust = '" + encoded + b"';"
    if variant == "reordered":
        trust = {"roots": trust["roots"], "schema": trust["schema"]}
    elif variant == "invalid-schema":
        trust["schema"] = "signing-publication-trust/v2"
    elif variant == "extra-member":
        trust["unreviewed"] = True
    elif variant == "key-replacement":
        # Public P-256 generator point: a different valid key must still be refused.
        trust["roots"][0]["jwk"].update(
            x=base64.urlsafe_b64encode(bytes.fromhex(
                "6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296"
            )).rstrip(b"=").decode(),
            y=base64.urlsafe_b64encode(bytes.fromhex(
                "4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5"
            )).rstrip(b"=").decode(),
        )
    elif variant == "wrong-curve":
        trust["roots"][0]["jwk"]["crv"] = "P-384"
    elif variant == "off-curve":
        trust["roots"][0]["jwk"].update(x="A" * 43, y="A" * 43)
    elif variant == "empty-roots":
        trust["roots"] = []
    trust_bytes = json.dumps(trust, indent=2 if variant == "whitespace" else None).encode()
    if variant == "escaped-ascii":
        trust_bytes = trust_bytes.replace(b'"schema"', b'"\\u0073chema"')
    elif variant == "duplicate-key":
        trust_bytes = trust_bytes.replace(b'"schema":', b'"schema":"ignored","schema":', 1)
    elif variant == "nan":
        trust_bytes = b'{"schema":"signing-publication-trust/v1","roots":[NaN]}'
    metadata = {"signing_parameters": {
        "schema": "ofca-signing-parameter-consumer/v1", "mode": "verified",
        "engine": "onlyfans-sha1-checksum/v1", "parameter_schema": "local-packaged-signing-rule/v1",
        "trust_sha256": trust_hash, "distribution_origin": "https://updates.dipsy.fi",
        "distribution_base_path": "/signing-parameters/v1/",
    }}
    entries = {
        "signing-publication-trust.json": trust_bytes,
        "signing-parameter-release.json": json.dumps({
            "schema": "ofca-signing-parameter-release/v1", "origin": "https://updates.dipsy.fi",
            "base_path": "/signing-parameters/v1/", "trust_sha256": trust_hash,
        }).encode(),
        "background.js": background,
    }
    if variant == "missing-runtime":
        del entries["background.js"]
    elif variant == "rebound-runtime":
        entries["background.js"] = background + background
    result = _run_sparse_authority_probe(tmp_path, {
        "mode": "authority", "metadata": metadata,
        "entries": {name: base64.b64encode(value).decode() for name, value in entries.items()},
    })
    valid = variant in {"canonical", "reordered", "whitespace", "escaped-ascii"}
    assert result.returncode == (0 if valid else 16), result.stdout + result.stderr
    observation = json.loads(result.stdout)
    if valid:
        assert observation == {"outcome": "accepted", "result": metadata["signing_parameters"],
                               "isolated": True, "no_site": True}
    else:
        assert observation == {"outcome": "refused", "step": "signing-parameters"}


@pytest.mark.parametrize("configuration", ["present", "missing", "mismatched"])
def test_protected_sparse_policy_uses_only_declared_public_configuration(tmp_path, configuration):
    _, chrome = _chrome_zip()
    with zipfile.ZipFile(io.BytesIO(chrome)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    release = json.loads((ROOT / "app/core/customer-release.json").read_bytes())
    origin = release["hosted_api_origin"]
    manifest = json.loads(entries["manifest.json"])
    manifest["host_permissions"] = ["http://bridge.localhost/*", origin + "/*"]
    manifest["externally_connectable"] = {"matches": [
        "http://bridge.localhost:17871/*", release["setup_transfer_receiver_url"],
        origin + "/public/onboarding/installation-continuation",
    ]}
    entries["manifest.json"] = json.dumps(manifest).encode()
    metadata = json.loads(entries["build-meta.json"])
    metadata["outputs"]["manifest.json"] = "sha256:" + hashlib.sha256(entries["manifest.json"]).hexdigest()
    entries["build-meta.json"] = json.dumps(metadata).encode()
    agent = _zip_bytes(entries)
    agent_name = f"OnlyFans-Conversational-Analytics-Agent-{RELEASE_VERSION}-chrome.zip"
    installer_name = "OnlyFans-Conversational-Analytics-Setup-0.7.5-x64.exe"
    outer = {agent_name: agent, installer_name: b"synthetic installer, no Windows trust claim"}
    outer["sha256sums.txt"] = "".join(
        f"{hashlib.sha256(data).hexdigest()} *{name}\n" for name, data in sorted(outer.items())
    ).encode()
    archive = _zip_bytes(outer)
    result = _run_sparse_authority_probe(tmp_path, {
        "mode": "container", "archive": base64.b64encode(archive).decode(),
        "digest": "sha256:" + hashlib.sha256(archive).hexdigest(), "tag": RELEASE_TAG,
    }, configuration=configuration)
    assert result.returncode == (0 if configuration == "present" else 2), result.stdout + result.stderr
    observation = json.loads(result.stdout)
    assert observation["outcome"] == ("accepted" if configuration == "present" else "refused")
    if configuration == "present":
        assert observation["result"] == {"version": RELEASE_VERSION}


@pytest.mark.parametrize("ci_policy,minutes", [
    ("sharded-v2", "20"), ("sharded-v2-pr-cutover", "20"),
    ("sharded-v3", "20"), ("sharded-v3-pr-cutover", "20"),
    ("sharded-v4-pr-cutover", "20"), ("sharded-v5-pr-cutover", "20"),
    ("sharded-v6-pr-cutover", "30"),
])
@pytest.mark.parametrize("replacement", ["19", "20", "29", "30", "31", "${{ inputs.timeout }}", None])
def test_analytics_timeout_is_exact_for_each_source_policy(ci_policy, minutes, replacement):
    api = QualificationApi(ci_policy=ci_policy)
    assert _qualify_source(api).source_commit == SOURCE_COMMIT
    before, body = api.product_ci_source.decode().split("  analytics-integration:\n", 1)
    matrix, after = body.split("  windows-full-shards:\n", 1)
    declaration = f"    timeout-minutes: {minutes}\n"
    assert declaration in matrix
    changed = "" if replacement is None else f"    timeout-minutes: {replacement}\n"
    api.product_ci_source = (before + "  analytics-integration:\n"
                             + matrix.replace(declaration, changed, 1)
                             + "  windows-full-shards:\n" + after).encode()
    if replacement == minutes:
        assert _qualify_source(api).source_commit == SOURCE_COMMIT
    else:
        with pytest.raises(producer.ContractError, match="matrix execution declaration"):
            _qualify_source(api)


@pytest.mark.parametrize("ci_policy,other_policy", [
    ("sharded-v5-pr-cutover", "sharded-v6-pr-cutover"),
    ("sharded-v6-pr-cutover", "sharded-v5-pr-cutover"),
])
def test_analytics_budget_source_never_accepts_another_versions_marker(ci_policy, other_policy):
    api = QualificationApi(ci_policy=ci_policy)
    api.product_ci_source = api.product_ci_source.replace(ci_policy.encode(), other_policy.encode())
    with pytest.raises(producer.ContractError, match="source policy"):
        _qualify_source(api)


def test_v5_source_policy_is_frozen_independently_of_current_workflow():
    api = QualificationApi(ci_policy="sharded-v5-pr-cutover")
    assert hashlib.sha256(api.product_ci_source).hexdigest() == "f6512935244ed31457412bcdcaf0bf92048f7130c894b73346c7a2a6059aea62"
    assert hashlib.sha256(api.product_ci_policy.replace(b"\r\n", b"\n")).hexdigest() == "edc170ec6cc828bce29fa2c68b822fef5266ed5d7ead7fca533849cc56fd1af9"
    assert api.product_ci_source != (ROOT / producer.PRODUCT_CI_WORKFLOW).read_bytes()
    assert _qualify_source(api).source_commit == SOURCE_COMMIT
