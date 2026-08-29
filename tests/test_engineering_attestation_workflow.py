"""Security invariants for the protected engineering-attestation workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "engineering-attestation.yml"
WINDOWS_PACKAGE_WORKFLOW = ROOT / ".github" / "workflows" / "windows-package.yml"
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
TEMP_PARENT_EXPRESSION = (
    "${{ runner.temp }}/engineering-attestation-"
    "${{ github.run_id }}-${{ github.run_attempt }}"
)


def _document() -> dict[str, Any]:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _jobs() -> dict[str, Any]:
    jobs = _document().get("jobs")
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def test_manual_interface_and_workflow_permissions_are_closed() -> None:
    document = _document()
    trigger = document.get(True) or document.get("on")
    assert trigger == {
        "workflow_dispatch": {
            "inputs": {
                "windows_package_run_id": {
                    "description": (
                        "Successful tag-triggered Windows package workflow run ID"
                    ),
                    "required": True,
                    "type": "string",
                },
                "release_tag": {
                    "description": (
                        "Immutable v* tag that produced the Windows package"
                    ),
                    "required": True,
                    "type": "string",
                },
                "legal_projection_source_commit": {
                    "description": (
                        "Review commit containing the projection snapshot"
                    ),
                    "required": True,
                    "type": "string",
                },
                "legal_projection_canonical_sha256": {
                    "description": (
                        "Canonical SHA-256 of the reviewed projection"
                    ),
                    "required": True,
                    "type": "string",
                },
            }
        }
    }
    assert document.get("permissions") == {}
    assert document.get("concurrency") == {
        "group": "engineering-attestation-production",
        "cancel-in-progress": False,
    }


def test_resolver_is_unprivileged_and_emits_metadata_only() -> None:
    resolver = _jobs()["resolve-source"]
    assert "environment" not in resolver
    assert resolver["runs-on"] == "ubuntu-24.04"
    assert resolver["permissions"] == {"actions": "read", "contents": "read"}
    assert set(resolver["outputs"]) == {
        "source_commit",
        "product_ci_run_id",
        "artifact_id",
        "artifact_name",
        "artifact_server_digest",
    }
    serialized = yaml.safe_dump(resolver)
    assert "secrets." not in serialized
    assert "download-artifact" not in serialized
    assert "sign-and-handoff" not in serialized
    resolve_step = next(step for step in _steps(resolver) if step.get("id") == "resolve")
    assert "resolve-source" in resolve_step["run"]
    assert "${{ inputs." not in resolve_step["run"]


def test_signer_repeats_qualification_inside_the_protected_environment() -> None:
    signer = _jobs()["sign-and-handoff"]
    assert signer["needs"] == "resolve-source"
    assert signer["environment"] == "engineering-attestation-production"
    assert signer["runs-on"] == "ubuntu-24.04"
    assert signer["permissions"] == {"actions": "read", "contents": "read"}
    assert "env" not in signer
    serialized = yaml.safe_dump(signer)
    assert "needs.resolve-source.outputs" not in serialized
    assert "actions/download-artifact" not in serialized
    assert "actions/upload-artifact" not in serialized
    assert "setup-python" not in serialized
    assert "setup-node" not in serialized
    assert "pip install" not in serialized
    assert "npm " not in serialized

    handoff = next(step for step in _steps(signer) if step.get("id") == "handoff")
    assert handoff["env"]["ENGINEERING_ATTESTATION_TEMP_PARENT"] == (
        TEMP_PARENT_EXPRESSION
    )
    assert handoff["env"]["TMPDIR"] == TEMP_PARENT_EXPRESSION
    assert "sign-and-handoff" in handoff["run"]
    assert "set +x" in handoff["run"]
    assert "ulimit -c 0" in handoff["run"]
    assert "::add-mask::" in handoff["run"]
    assert "${value//%/%25}" in handoff["run"]
    assert "forbidden line break" in handoff["run"]
    assert "${{ inputs." not in handoff["run"]
    assert {
        "ENGINEERING_ATTESTATION_PRIVATE_KEY_B64",
        "REVIEW_APP_PRIVATE_KEY_B64",
    } <= set(handoff["env"])
    review_variables = {
        "REVIEW_REPOSITORY",
        "REVIEW_DEFAULT_BRANCH",
        "REVIEW_PROJECTION_PATH",
        "REVIEW_PROJECTION_DIGEST_PATH",
        "REVIEW_APP_ID",
        "REVIEW_INSTALLATION_ID",
        "REVIEW_BOT_USER_ID",
        "REVIEW_REPOSITORY_ID",
    }
    assert review_variables <= set(handoff["env"])
    for name in review_variables:
        assert handoff["env"][name] == "${{ vars." + name + " }}"

    checkout = next(
        step for step in _steps(signer) if step.get("name", "").startswith("Check out")
    )
    assert "attestation/signers/" in checkout["with"]["sparse-checkout"]
    assert "engineering-attestation-v1-ed25519.json" in checkout["with"][
        "sparse-checkout"
    ]
    cleanup = next(
        step
        for step in _steps(signer)
        if step.get("name") == "Remove temporary signer material"
    )
    assert cleanup["if"] == "always()"
    assert cleanup["env"]["ENGINEERING_ATTESTATION_TEMP_PARENT"] == (
        TEMP_PARENT_EXPRESSION
    )
    assert "Refusing to clean an unexpected temporary path" in cleanup["run"]


def test_signed_package_publication_stays_an_actions_artifact() -> None:
    document = yaml.safe_load(WINDOWS_PACKAGE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    signer = jobs.get("sign")
    assert isinstance(signer, dict)
    assert signer["permissions"] == {"contents": "read", "id-token": "write"}
    publish_steps = [
        step
        for step in _steps(signer)
        if step.get("name") == "Publish the signed package"
    ]
    assert len(publish_steps) == 1
    publish = publish_steps[0]
    action, ref = publish["uses"].rsplit("@", 1)
    assert action == "actions/upload-artifact"
    assert COMMIT_SHA.fullmatch(ref)
    assert publish["if"] == "steps.verify-signatures.outcome == 'success'"
    assert publish["with"] == {
        "name": "windows-package-${{ github.ref_name }}",
        "path": "${{ github.workspace }}\\release\\",
    }
    assert "run" not in publish


def test_every_action_is_commit_pinned_and_checkout_is_credentialless() -> None:
    for job in _jobs().values():
        for step in _steps(job):
            action = step.get("uses")
            if action is None:
                continue
            owner_name, ref = action.rsplit("@", 1)
            assert "/" in owner_name
            assert COMMIT_SHA.fullmatch(ref)
            if owner_name == "actions/checkout":
                assert step.get("with", {}).get("persist-credentials") is False
                assert step.get("with", {}).get("ref") == "${{ github.workflow_sha }}"
