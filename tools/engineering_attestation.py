#!/usr/bin/env python3
"""Produce and privately hand off a signed engineering attestation.

The protected command intentionally uses only the Python standard library,
OpenSSL supplied by the pinned runner image, and the dependency-free Legal
release bindings gate. It never builds product code and never trusts
qualification conclusions emitted by the unprivileged jobs.

Attestation is an independent qualification boundary. It resolves the Legal
binding coordinates, retrieves the document itself, recomputes the
contract-defined digest, requires the exact Store ZIP to have passed the
controlled package audit, and recomputes the ZIP digest, before any key
material is decoded. Each precondition refuses with its own exit code so a
refusal names the step that refused.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


PRODUCT_REPOSITORY = "ramiradwan/onlyfans-conversational-analytics"
PRODUCT_CI_WORKFLOW = ".github/workflows/ci.yml"
WINDOWS_PACKAGE_WORKFLOW = ".github/workflows/windows-package.yml"
PRODUCER_WORKFLOW = ".github/workflows/engineering-attestation.yml"
TARGET = "chrome-extension"
SIGNER_ID = "product-engineering-attestation-ed25519-v1"
ALGORITHM = "ed25519"

# Every member the v1 attestation schema defines. The consumer sets
# additionalProperties to false, so a document carrying anything outside this
# set is rejected at evidence intake rather than at signing time.
ATTESTATION_V1_MEMBERS = frozenset(
    {
        "schema_version",
        "attestation_id",
        "created_at",
        "repository",
        "commit_hash",
        "workflow",
        "target",
        "artifact",
        "engineering_facts",
        "legal_projection",
        "provenance",
    }
)

PRODUCT_DEFAULT_BRANCH = "main"
EXPECTED_EXTENSION_ID = "mldllkjpnnjhdccpofhebhlhigpefcba"
EXTENSION_BUILD_SCHEMA = "ofca-extension-build/v4"
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
PUBLIC_KEY_RELATIVE_PATH = (
    "attestation/signers/"
    "product-engineering-attestation-ed25519-v1.pem"
)
CONFORMANCE_VECTOR_RELATIVE_PATH = (
    "tests/fixtures/engineering-attestation-v1-ed25519.json"
)
CONFORMANCE_VECTOR_SHA256 = (
    "e75b40221667d0f4a384db4be784e42850bad0826348decc3eec6bb70c5b5f9d"
)
REQUIRED_WINDOWS_JOB_NAMES = {
    "Build the unsigned package",
    "Sign and publish the package",
}
REQUIRED_PRODUCT_CI_JOB_NAMES = {
    "build-and-test",
    "windows-browser-e2e",
    "windows-tests",
}

PRODUCER_ROOT = Path(__file__).resolve().parent.parent
LEGAL_BINDINGS_GATE = PRODUCER_ROOT / "tools" / "legal-release-bindings" / "verify.mjs"
EXTENSION_ROOT = PRODUCER_ROOT / "extension"
EXTENSION_BUILD_SCRIPT = EXTENSION_ROOT / "build.mjs"

LEGAL_BINDINGS_SCHEMA = "ofca-legal-instrument-bindings/v1"
LEGAL_BINDINGS_KEYS = ("legal_bindings_digest", "schema", "source_revision")
SIGNING_RULE_KEYS = ("schema", "sha256", "source_revision")
SIGNING_RULE_FILE = "packaged-signing-rule.json"
EXTENSION_CONFIG_FILE = "extension-config.json"
BACKGROUND_FILE = "background.js"
BUILD_METADATA_FILE = "build-meta.json"
PRIVACY_POLICY_INSTRUMENT = "privacy_policy"

RESOLVER_JOB_NAME = "Resolve unprivileged source metadata"
QUALIFICATION_JOB_NAME = "Qualify the exact Store package"
SIGNING_JOB_NAME = "Sign and privately hand off evidence"
PRODUCER_JOB_NAMES = {
    RESOLVER_JOB_NAME,
    QUALIFICATION_JOB_NAME,
    SIGNING_JOB_NAME,
}

# Each precondition refuses with its own exit code so a refusal at one step is
# distinguishable in the output from a refusal at any other.
STEP_LEGAL_COORDINATES = "legal-coordinates"
STEP_LEGAL_RETRIEVAL = "legal-retrieval"
STEP_LEGAL_DIGEST = "legal-digest"
STEP_SIGNING_RULE = "signing-rule"
STEP_PRIVACY_POLICY = "privacy-policy"
STEP_PACKAGE_AUDIT = "package-audit"
STEP_ARTIFACT_DIGEST = "artifact-digest"
STEP_PACKAGE_QUALIFICATION = "package-qualification"
QUALIFICATION_EXIT_CODES = {
    STEP_LEGAL_COORDINATES: 10,
    STEP_LEGAL_RETRIEVAL: 11,
    STEP_LEGAL_DIGEST: 12,
    STEP_PACKAGE_AUDIT: 13,
    STEP_ARTIFACT_DIGEST: 14,
    STEP_PACKAGE_QUALIFICATION: 15,
    STEP_SIGNING_RULE: 16,
    STEP_PRIVACY_POLICY: 17,
}

# tools/legal-release-bindings/verify.mjs refusal codes, mapped onto the step
# that owns them. Anything unlisted is treated as a retrieval failure.
GATE_EXIT_STEPS = {
    2: STEP_LEGAL_COORDINATES,
    3: STEP_LEGAL_RETRIEVAL,
    4: STEP_LEGAL_RETRIEVAL,
    5: STEP_LEGAL_DIGEST,
    6: STEP_LEGAL_DIGEST,
    7: STEP_LEGAL_DIGEST,
    8: STEP_LEGAL_RETRIEVAL,
    9: STEP_PRIVACY_POLICY,
}

HEX_40 = re.compile(r"^[a-f0-9]{40}$")
HEX_64 = re.compile(r"^[a-f0-9]{64}$")
SPKI_FINGERPRINT = re.compile(r"^sha256:[a-f0-9]{64}$")
SHA256_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
TAG = re.compile(r"^v[0-9A-Za-z][0-9A-Za-z._-]*$")
AGENT_ZIP = re.compile(
    r"^OnlyFans-Conversational-Analytics-Agent-"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-chrome\.zip$"
)
INSTALLER = re.compile(
    r"^OnlyFans-Conversational-Analytics-Setup-"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-x64\.exe$"
)
EPHEMERAL_PROJECTION_KEYS = {
    "run_id",
    "run_attempt",
    "builder_id",
    "runner_name",
    "ephemeral_timestamp",
    "build_duration_seconds",
    "ci_job_id",
    "temp_dir",
}


class ContractError(RuntimeError):
    """A fail-closed producer contract violation."""


class ReviewBaseAdvancedError(ContractError):
    """The protected review branch advanced during PR creation."""


class QualificationError(ContractError):
    """A qualification precondition refused, naming the step that refused."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step

    @property
    def exit_code(self) -> int:
        return QUALIFICATION_EXIT_CODES[self.step]


def refuse(step: str, message: str) -> None:
    raise QualificationError(step, message)


class DuplicateJsonKeyError(ValueError):
    """A JSON object contained two members with the same name."""


class InvalidJsonConstantError(ValueError):
    """JSON contained a non-standard numeric constant."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise InvalidJsonConstantError(
        f"non-standard JSON numeric constant is not allowed: {value}"
    )


def load_json_strict(data: bytes, *, label: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"{label} is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        json.JSONDecodeError,
        DuplicateJsonKeyError,
        InvalidJsonConstantError,
    ) as exc:
        raise ContractError(f"{label} is not strict JSON: {exc}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: Any, *, label: str) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} cannot be serialized as canonical JSON: {exc}") from exc


def _canonicalize_projection(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonicalize_projection(value[key])
            for key in sorted(value)
            if key not in EPHEMERAL_PROJECTION_KEYS
        }
    if isinstance(value, list):
        return [_canonicalize_projection(item) for item in value]
    return value


def canonical_projection_bytes(value: Any) -> bytes:
    return _canonical_json_bytes(
        _canonicalize_projection(value), label="review projection"
    )


def canonical_projection_sha256(value: Any) -> str:
    return sha256_bytes(canonical_projection_bytes(value))


def attestation_signing_payload(attestation: Mapping[str, Any]) -> bytes:
    provenance = attestation.get("provenance")
    if not isinstance(provenance, dict):
        raise ContractError("attestation provenance must be an object")
    unsigned = copy.deepcopy(dict(attestation))
    unsigned_provenance = dict(provenance)
    unsigned_provenance.pop("signature", None)
    unsigned["provenance"] = unsigned_provenance
    return _canonical_json_bytes(unsigned, label="attestation signing payload")


def serialize_final_attestation(attestation: Mapping[str, Any]) -> bytes:
    provenance = attestation.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("signature"):
        raise ContractError("final attestation has no provenance signature")
    return _canonical_json_bytes(dict(attestation), label="final attestation") + b"\n"


def _require_hex(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise ContractError(f"{label} has an invalid format")
    return value


def _require_spki_fingerprint(value: str, label: str) -> str:
    if not SPKI_FINGERPRINT.fullmatch(value):
        raise ContractError(f"{label} must use sha256:<64 lowercase hex>")
    return value


def _run_openssl(
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    openssl: str = "openssl",
) -> bytes:
    try:
        result = subprocess.run(
            [openssl, *arguments],
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ContractError(f"OpenSSL could not be executed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(f"OpenSSL command failed: {detail}")
    return result.stdout


def derive_public_key(private_key_path: Path, *, openssl: str = "openssl") -> bytes:
    public_pem = _run_openssl(
        ["pkey", "-in", str(private_key_path), "-pubout"], openssl=openssl
    )
    details = _run_openssl(
        ["pkey", "-pubin", "-text", "-noout"],
        input_bytes=public_pem,
        openssl=openssl,
    )
    if b"ED25519" not in details.upper():
        raise ContractError("attestation private key is not Ed25519")
    return public_pem


def spki_fingerprint(public_pem: bytes, *, openssl: str = "openssl") -> str:
    der = _run_openssl(
        ["pkey", "-pubin", "-outform", "DER"],
        input_bytes=public_pem,
        openssl=openssl,
    )
    return "sha256:" + sha256_bytes(der)


def validate_signer_material(
    private_key_path: Path,
    committed_public_key_path: Path,
    *,
    expected_signer_id: str,
    expected_fingerprint: str,
    openssl: str = "openssl",
) -> str:
    if not private_key_path.is_file() or private_key_path.is_symlink():
        raise ContractError("protected attestation private key is missing or invalid")
    if not committed_public_key_path.is_file() or committed_public_key_path.is_symlink():
        raise ContractError("committed public verification key is missing or invalid")
    if expected_signer_id != SIGNER_ID:
        raise ContractError("protected signer ID does not match the producer contract")
    _require_spki_fingerprint(
        expected_fingerprint, "expected signer SPKI fingerprint"
    )

    committed_public_pem = committed_public_key_path.read_bytes()
    derived_public_pem = derive_public_key(private_key_path, openssl=openssl)
    committed_fingerprint = spki_fingerprint(committed_public_pem, openssl=openssl)
    fingerprint = spki_fingerprint(derived_public_pem, openssl=openssl)
    if fingerprint != committed_fingerprint:
        raise ContractError("protected private key does not match committed public PEM")
    if fingerprint != expected_fingerprint:
        raise ContractError("protected signer SPKI fingerprint mismatch")
    return fingerprint


def sign_ed25519(
    payload: bytes, private_key_path: Path, *, openssl: str = "openssl"
) -> bytes:
    with tempfile.NamedTemporaryFile(delete=False) as payload_file:
        payload_file.write(payload)
        payload_path = Path(payload_file.name)
    try:
        signature = _run_openssl(
            [
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(private_key_path),
                "-in",
                str(payload_path),
            ],
            openssl=openssl,
        )
    finally:
        payload_path.unlink(missing_ok=True)
    if len(signature) != 64:
        raise ContractError(
            f"Ed25519 produced an unexpected {len(signature)}-byte signature"
        )
    return signature


def verify_ed25519(
    payload: bytes,
    signature: bytes,
    public_key_path: Path,
    *,
    openssl: str = "openssl",
) -> None:
    with tempfile.NamedTemporaryFile(delete=False) as payload_file:
        payload_file.write(payload)
        payload_path = Path(payload_file.name)
    with tempfile.NamedTemporaryFile(delete=False) as signature_file:
        signature_file.write(signature)
        signature_path = Path(signature_file.name)
    try:
        _run_openssl(
            [
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_key_path),
                "-sigfile",
                str(signature_path),
                "-rawin",
                "-in",
                str(payload_path),
            ],
            openssl=openssl,
        )
    finally:
        payload_path.unlink(missing_ok=True)
        signature_path.unlink(missing_ok=True)


def verify_conformance_vector(
    vector_path: Path,
    temporary_root: Path,
    *,
    openssl: str = "openssl",
) -> None:
    if not vector_path.is_file() or vector_path.is_symlink():
        raise ContractError(f"conformance vector is missing or invalid: {vector_path}")
    vector_bytes = vector_path.read_bytes()
    if sha256_bytes(vector_bytes) != CONFORMANCE_VECTOR_SHA256:
        raise ContractError("conformance vector bytes do not match the consumer contract")
    vector = load_json_strict(vector_bytes, label="engineering attestation vector")
    try:
        if not isinstance(vector, dict) or vector["schema_version"] != "1.0":
            raise ContractError("conformance vector schema version mismatch")
        projection = vector["projection"]
        ed25519_vector = vector["ed25519"]
        negative = vector["negative_vectors"]
        if not all(isinstance(item, dict) for item in (projection, ed25519_vector, negative)):
            raise ContractError("conformance vector sections must be objects")

        projection_object = projection["object"]
        projection_bytes = canonical_projection_bytes(projection_object)
        if (
            projection_bytes.decode("utf-8") != projection["canonical_json_utf8"]
            or sha256_bytes(projection_bytes) != projection["sha256"]
        ):
            raise ContractError("conformance projection canonicalization mismatch")

        unsigned_attestation = ed25519_vector["unsigned_attestation"]
        if not isinstance(unsigned_attestation, dict):
            raise ContractError("conformance unsigned attestation must be an object")
        payload = attestation_signing_payload(unsigned_attestation)
        if (
            payload.decode("utf-8") != ed25519_vector["signing_payload_utf8"]
            or sha256_bytes(payload) != ed25519_vector["signing_payload_sha256"]
        ):
            raise ContractError("conformance signing payload mismatch")

        signature_text = ed25519_vector["signature_base64"]
        if not isinstance(signature_text, str):
            raise ContractError("conformance signature must be Base64 text")
        try:
            signature = base64.b64decode(signature_text, validate=True)
        except ValueError as exc:
            raise ContractError("conformance signature is not strict Base64") from exc
        if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != signature_text:
            raise ContractError("conformance signature is not canonical Ed25519 Base64")

        public_pem_text = ed25519_vector["public_key_pem"]
        expected_fingerprint = ed25519_vector["public_key_spki_fingerprint"]
        if not isinstance(public_pem_text, str) or not isinstance(
            expected_fingerprint, str
        ):
            raise ContractError("conformance public-key values must be strings")
        public_pem = public_pem_text.encode("ascii")
        _require_spki_fingerprint(expected_fingerprint, "conformance SPKI fingerprint")
        if spki_fingerprint(public_pem, openssl=openssl) != expected_fingerprint:
            raise ContractError("conformance public-key fingerprint mismatch")

        public_path = temporary_root / "conformance-public-key.pem"
        public_path.write_bytes(public_pem)
        try:
            verify_ed25519(
                payload,
                signature,
                public_path,
                openssl=openssl,
            )
        finally:
            public_path.unlink(missing_ok=True)

        final_attestation = copy.deepcopy(unsigned_attestation)
        provenance = final_attestation.get("provenance")
        if not isinstance(provenance, dict):
            raise ContractError("conformance attestation provenance must be an object")
        provenance["signature"] = signature_text
        if sha256_bytes(serialize_final_attestation(final_attestation)) != ed25519_vector[
            "final_attestation_sha256"
        ]:
            raise ContractError("conformance final attestation mismatch")

        for key in ("duplicate_key_json", "nonfinite_json"):
            malformed = negative[key]
            if not isinstance(malformed, str):
                raise ContractError(f"conformance negative vector {key!r} must be text")
            try:
                load_json_strict(malformed.encode("utf-8"), label=key)
            except ContractError:
                pass
            else:
                raise ContractError(
                    f"conformance negative vector {key!r} was unexpectedly accepted"
                )
    except (KeyError, TypeError, UnicodeEncodeError) as exc:
        raise ContractError(f"conformance vector structure is invalid: {exc}") from exc


class GitHubApi:
    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
    ) -> None:
        if not token:
            raise ContractError("GitHub API token is empty")
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        url = path if path.startswith("https://") else f"{self.api_url}{path}"
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "engineering-attestation/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        # GitHub's artifact endpoint redirects to a signed object-storage URL.
        # An unredirected header authenticates the API request without copying
        # the bearer token onto the redirected request.
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise ContractError(
                f"GitHub API {method} {urllib.parse.urlsplit(url).path} "
                f"failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ContractError(
                f"GitHub API {method} {urllib.parse.urlsplit(url).path} failed: {exc}"
            ) from exc
        if raw:
            return body
        if not body:
            return None
        parsed = load_json_strict(body, label="GitHub API response")
        return parsed

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self.request("POST", path, payload=payload)

    def patch(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self.request("PATCH", path, payload=payload)

    def delete(self, path: str) -> None:
        self.request("DELETE", path)

    def download(self, path: str, destination: Path) -> None:
        body = self.request("GET", path, raw=True)
        destination.write_bytes(body)


def _repo_path(repository: str) -> str:
    if repository.count("/") != 1:
        raise ContractError("repository must use owner/name form")
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
        or part in {".", ".."}
        for part in repository.split("/")
    ):
        raise ContractError("repository contains an invalid owner or name component")
    return "/repos/" + "/".join(
        urllib.parse.quote(part, safe="") for part in repository.split("/")
    )


def _require_review_git_path(value: str, label: str) -> str:
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ContractError(f"{label} must be a normalized repository-relative Git path")
    return value


@dataclass(frozen=True)
class ReviewConfiguration:
    repository: str
    default_branch: str
    projection_path: str
    projection_digest_path: str


def load_review_configuration() -> ReviewConfiguration:
    repository = _required_environment("REVIEW_REPOSITORY")
    _repo_path(repository)
    default_branch = _required_environment("REVIEW_DEFAULT_BRANCH")
    if (
        default_branch.startswith("refs/")
        or default_branch in {".", ".."}
        or any(ord(character) < 0x20 for character in default_branch)
    ):
        raise ContractError("REVIEW_DEFAULT_BRANCH is not a normalized branch name")
    return ReviewConfiguration(
        repository=repository,
        default_branch=default_branch,
        projection_path=_require_review_git_path(
            _required_environment("REVIEW_PROJECTION_PATH"),
            "REVIEW_PROJECTION_PATH",
        ),
        projection_digest_path=_require_review_git_path(
            _required_environment("REVIEW_PROJECTION_DIGEST_PATH"),
            "REVIEW_PROJECTION_DIGEST_PATH",
        ),
    )


def resolve_tag_commit(client: GitHubApi, repository: str, tag: str) -> str:
    if not TAG.fullmatch(tag):
        raise ContractError("release tag must be an exact v* tag without a ref prefix")
    encoded = urllib.parse.quote(tag, safe="")
    ref = client.get(f"{_repo_path(repository)}/git/ref/tags/{encoded}")
    obj = ref.get("object") if isinstance(ref, dict) else None
    for _ in range(8):
        if not isinstance(obj, dict):
            break
        object_type = obj.get("type")
        sha = obj.get("sha")
        if not isinstance(sha, str) or not HEX_40.fullmatch(sha):
            break
        if object_type == "commit":
            return sha
        if object_type != "tag":
            break
        tag_object = client.get(f"{_repo_path(repository)}/git/tags/{sha}")
        obj = tag_object.get("object") if isinstance(tag_object, dict) else None
    raise ContractError(f"release tag {tag!r} does not peel to a commit")


def require_ancestor(
    client: GitHubApi,
    repository: str,
    ancestor: str,
    descendant: str,
    *,
    label: str,
    allow_identical: bool = True,
) -> None:
    _require_hex(ancestor, HEX_40, f"{label} ancestor")
    _require_hex(descendant, HEX_40, f"{label} descendant")
    if not allow_identical and ancestor == descendant:
        raise ContractError(f"{label} must be a strict descendant of its baseline")
    comparison = client.get(
        f"{_repo_path(repository)}/compare/{ancestor}...{descendant}"
    )
    status_value = comparison.get("status") if isinstance(comparison, dict) else None
    allowed_statuses = {"ahead", "identical"} if allow_identical else {"ahead"}
    if status_value not in allowed_statuses:
        raise ContractError(
            f"{label} ancestor check failed: comparison status is {status_value!r}"
        )


@dataclass(frozen=True)
class QualifiedSource:
    run_id: int
    run_attempt: int
    product_ci_run_id: int
    source_commit: str
    artifact_id: int
    artifact_name: str
    artifact_server_digest: str
    archive_download_url: str


def qualify_product_ci_source(client: GitHubApi, *, source_commit: str) -> int:
    """Require the exact source commit to have a successful main-push CI run."""

    _require_hex(source_commit, HEX_40, "Product CI source commit")
    workflow_name = PRODUCT_CI_WORKFLOW.rsplit("/", 1)[-1]
    encoded_workflow = urllib.parse.quote(workflow_name, safe="")
    workflow = client.get(
        f"{_repo_path(PRODUCT_REPOSITORY)}/actions/workflows/{encoded_workflow}"
    )
    workflow_id = workflow.get("id") if isinstance(workflow, dict) else None
    if (
        not isinstance(workflow_id, int)
        or workflow_id <= 0
        or workflow.get("path") != PRODUCT_CI_WORKFLOW
        or workflow.get("state") != "active"
    ):
        raise ContractError("Product CI workflow identity, path, or state mismatch")

    query = urllib.parse.urlencode(
        {
            "branch": PRODUCT_DEFAULT_BRANCH,
            "event": "push",
            "head_sha": source_commit,
            "status": "success",
            "per_page": 100,
        }
    )
    runs_document = client.get(
        f"{_repo_path(PRODUCT_REPOSITORY)}/actions/workflows/{workflow_id}/runs?{query}"
    )
    runs = (
        runs_document.get("workflow_runs", [])
        if isinstance(runs_document, dict)
        else []
    )
    if not isinstance(runs_document, dict) or not isinstance(runs, list):
        raise ContractError("Product CI runs response is not an object")
    if runs_document.get("total_count") != len(runs):
        raise ContractError("Product CI run query returned more than 100 runs")
    if len(runs) != 1 or not isinstance(runs[0], dict):
        raise ContractError(
            "expected exactly one successful Product CI push run for the source commit"
        )
    run = runs[0]
    run_id = run.get("id")
    run_attempt = run.get("run_attempt")
    run_path = str(run.get("path", "")).split("@", 1)[0]
    expected = {
        "workflow_id": workflow_id,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_branch": PRODUCT_DEFAULT_BRANCH,
        "head_sha": source_commit,
    }
    for field, value in expected.items():
        if run.get(field) != value:
            raise ContractError(
                f"Product CI run {field} is {run.get(field)!r}, expected {value!r}"
            )
    if run_path != PRODUCT_CI_WORKFLOW:
        raise ContractError(
            f"Product CI run path is {run_path!r}, expected {PRODUCT_CI_WORKFLOW!r}"
        )
    if not isinstance(run_id, int) or run_id <= 0:
        raise ContractError("Product CI run has no positive numeric ID")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ContractError("Product CI run has no positive run attempt")
    for repository_field in ("repository", "head_repository"):
        repository = run.get(repository_field)
        if not isinstance(repository, dict) or (
            repository.get("full_name") != PRODUCT_REPOSITORY
        ):
            raise ContractError(
                f"Product CI run {repository_field} is not the Product repository"
            )

    jobs_document = client.get(
        f"{_repo_path(PRODUCT_REPOSITORY)}/actions/runs/{run_id}/attempts/"
        f"{run_attempt}/jobs?per_page=100"
    )
    jobs = jobs_document.get("jobs", []) if isinstance(jobs_document, dict) else []
    if not isinstance(jobs_document, dict) or not isinstance(jobs, list):
        raise ContractError("Product CI jobs response is not an object")
    if jobs_document.get("total_count") != len(jobs):
        raise ContractError("Product CI run has more than 100 jobs")
    observed_names = [job.get("name") for job in jobs if isinstance(job, dict)]
    if (
        len(jobs) != len(REQUIRED_PRODUCT_CI_JOB_NAMES)
        or set(observed_names) != REQUIRED_PRODUCT_CI_JOB_NAMES
    ):
        raise ContractError("Product CI run does not have the exact required job set")
    for job in jobs:
        if not isinstance(job, dict):
            raise ContractError("Product CI jobs response contains a non-object")
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or job.get("run_id") != run_id
            or job.get("head_sha") != source_commit
        ):
            raise ContractError(
                f"required Product CI job {job.get('name')!r} did not succeed "
                "for the qualified source commit"
            )
    return run_id


def qualify_windows_package_source(
    client: GitHubApi,
    *,
    run_id: int,
    release_tag: str,
    baseline_sha: str,
    workflow_sha: str,
) -> QualifiedSource:
    if run_id <= 0:
        raise ContractError("windows_package_run_id must be positive")
    _require_hex(baseline_sha, HEX_40, "PRODUCER_CONTROL_BASELINE_SHA")
    _require_hex(workflow_sha, HEX_40, "producer workflow SHA")

    run = client.get(f"{_repo_path(PRODUCT_REPOSITORY)}/actions/runs/{run_id}")
    if not isinstance(run, dict):
        raise ContractError("Windows package run response is not an object")
    workflow_id = run.get("workflow_id")
    if not isinstance(workflow_id, int) or workflow_id <= 0:
        raise ContractError("Windows package run has no positive workflow ID")
    workflow_path = str(run.get("path", "")).split("@", 1)[0]
    # A Store candidate is dispatched against an immutable release tag, not
    # produced by repository movement, so the qualifying event is the dispatch.
    # What binds the run to that tag is not the event but the three checks
    # below it: the run sits on the release tag, in this repository, and the
    # tag peels to the commit the run was built from.
    expected = {
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
    }
    for field, value in expected.items():
        if run.get(field) != value:
            raise ContractError(
                f"Windows package run {field} is {run.get(field)!r}, expected {value!r}"
            )
    if workflow_path != WINDOWS_PACKAGE_WORKFLOW:
        raise ContractError(
            f"Windows package run path is {workflow_path!r}, expected "
            f"{WINDOWS_PACKAGE_WORKFLOW!r}"
        )
    if run.get("head_branch") != release_tag:
        raise ContractError(
            "Windows package run head branch does not equal the immutable release tag"
        )
    head_repository = run.get("head_repository")
    if not isinstance(head_repository, dict) or (
        head_repository.get("full_name") != PRODUCT_REPOSITORY
    ):
        raise ContractError("Windows package run came from the wrong repository")
    source_commit = str(run.get("head_sha", ""))
    _require_hex(source_commit, HEX_40, "Windows package source commit")
    run_attempt = run.get("run_attempt")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ContractError("Windows package run has no positive run attempt")

    workflow = client.get(
        f"{_repo_path(PRODUCT_REPOSITORY)}/actions/workflows/{workflow_id}"
    )
    if (
        not isinstance(workflow, dict)
        or workflow.get("id") != workflow_id
        or workflow.get("path") != WINDOWS_PACKAGE_WORKFLOW
    ):
        raise ContractError("Windows package workflow numeric identity or path mismatch")
    if resolve_tag_commit(client, PRODUCT_REPOSITORY, release_tag) != source_commit:
        raise ContractError("release tag does not point to the Windows package source commit")

    require_ancestor(
        client,
        PRODUCT_REPOSITORY,
        baseline_sha,
        workflow_sha,
        label="producer workflow",
        allow_identical=False,
    )
    require_ancestor(
        client,
        PRODUCT_REPOSITORY,
        baseline_sha,
        source_commit,
        label="attested product commit",
        allow_identical=False,
    )
    product_ci_run_id = qualify_product_ci_source(
        client, source_commit=source_commit
    )

    jobs_document = client.get(
        f"{_repo_path(PRODUCT_REPOSITORY)}/actions/runs/{run_id}/attempts/"
        f"{run_attempt}/jobs?per_page=100"
    )
    jobs = jobs_document.get("jobs", []) if isinstance(jobs_document, dict) else []
    if not isinstance(jobs_document, dict) or not isinstance(jobs, list):
        raise ContractError("Windows package jobs response is not an object")
    if jobs_document.get("total_count") != len(jobs):
        raise ContractError("Windows package run has more than 100 jobs")
    for required_name in sorted(REQUIRED_WINDOWS_JOB_NAMES):
        matches = [
            job
            for job in jobs
            if isinstance(job, dict) and job.get("name") == required_name
        ]
        if len(matches) != 1:
            raise ContractError(
                f"expected exactly one required Windows package job {required_name!r}"
            )
        job = matches[0]
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or job.get("run_id") != run_id
            or job.get("head_sha") != source_commit
        ):
            raise ContractError(
                f"required Windows package job {required_name!r} did not succeed "
                "for the qualified run and commit"
            )

    artifacts_document = client.get(
        f"{_repo_path(PRODUCT_REPOSITORY)}/actions/runs/{run_id}/artifacts?per_page=100"
    )
    artifacts = (
        artifacts_document.get("artifacts", [])
        if isinstance(artifacts_document, dict)
        else []
    )
    if not isinstance(artifacts_document, dict):
        raise ContractError("Windows package artifacts response is not an object")
    if artifacts_document.get("total_count") != len(artifacts):
        raise ContractError("Windows package run has more than 100 artifacts")
    expected_name = f"windows-package-{release_tag}"
    expected_artifact_names = {
        expected_name,
        f"windows-package-unsigned-{release_tag}",
    }
    observed_artifact_names = [
        item.get("name") for item in artifacts if isinstance(item, dict)
    ]
    if (
        len(artifacts) != len(expected_artifact_names)
        or set(observed_artifact_names) != expected_artifact_names
    ):
        raise ContractError(
            "Windows package run artifact identity/count does not match the workflow contract"
        )
    candidates = [
        item
        for item in artifacts
        if isinstance(item, dict) and item.get("name") == expected_name
    ]
    if len(candidates) != 1:
        raise ContractError(
            f"expected exactly one {expected_name!r} Actions artifact, found "
            f"{len(candidates)}"
        )
    candidate = candidates[0]
    if candidate.get("expired") is not False:
        raise ContractError("qualified Windows package artifact is expired")
    artifact_id = candidate.get("id")
    if not isinstance(artifact_id, int) or artifact_id <= 0:
        raise ContractError("qualified Windows package artifact has no numeric ID")
    digest = candidate.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ContractError("qualified Actions artifact has no SHA-256 server digest")
    archive_download_url = candidate.get("archive_download_url")
    if not isinstance(archive_download_url, str) or not archive_download_url.startswith(
        "https://api.github.com/"
    ):
        raise ContractError("qualified Actions artifact download URL is not GitHub API")
    return QualifiedSource(
        run_id=run_id,
        run_attempt=run_attempt,
        product_ci_run_id=product_ci_run_id,
        source_commit=source_commit,
        artifact_id=artifact_id,
        artifact_name=expected_name,
        artifact_server_digest=digest,
        archive_download_url=archive_download_url,
    )


def _safe_zip_name(name: str, *, label: str) -> None:
    parts = name.split("/")
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or name.endswith("/")
        or ":" in name
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ContractError(f"{label} contains unsafe archive name {name!r}")


def _read_exact_zip_entries(data: bytes, *, label: str) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ContractError(f"{label} is not a valid ZIP: {exc}") from exc
    entries: dict[str, bytes] = {}
    casefolded: set[str] = set()
    try:
        for info in archive.infolist():
            archive_name = info.orig_filename
            if archive_name != info.filename:
                raise ContractError(
                    f"{label} contains a normalized or truncated archive name"
                )
            _safe_zip_name(archive_name, label=label)
            folded = archive_name.casefold()
            if archive_name in entries or folded in casefolded:
                raise ContractError(
                    f"{label} contains duplicate or case-colliding entry "
                    f"{archive_name!r}"
                )
            if info.flag_bits & 0x1:
                raise ContractError(f"{label} contains encrypted entry {archive_name!r}")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                raise ContractError(f"{label} contains symlink entry {archive_name!r}")
            if info.is_dir():
                raise ContractError(f"{label} contains directory entry {archive_name!r}")
            try:
                entries[archive_name] = archive.read(info)
            except (NotImplementedError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ContractError(
                    f"{label} entry {archive_name!r} cannot be read safely: {exc}"
                ) from exc
            casefolded.add(folded)
    finally:
        archive.close()
    if not entries:
        raise ContractError(f"{label} contains no files")
    return entries


def _parse_sha256sums(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ContractError("sha256sums.txt is not ASCII") from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([a-f0-9]{64}) \*([^/\\][^\\]*)", line)
        if not match:
            raise ContractError(f"invalid sha256sums.txt line: {line!r}")
        digest, filename = match.groups()
        _safe_zip_name(filename, label="sha256sums.txt")
        if filename in result:
            raise ContractError(f"duplicate sha256sums.txt filename: {filename!r}")
        result[filename] = digest
    return result


def extension_id_from_manifest_key(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError("Chrome manifest has no extension public key")
    try:
        public_key = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ContractError("Chrome manifest extension public key is not strict Base64") from exc
    if not public_key or base64.b64encode(public_key).decode("ascii") != value:
        raise ContractError("Chrome manifest extension public key is not canonical Base64")
    alphabet = "abcdefghijklmnop"
    return "".join(
        alphabet[nibble]
        for byte in hashlib.sha256(public_key).digest()[:16]
        for nibble in (byte >> 4, byte & 0x0F)
    )


@dataclass(frozen=True)
class QualifiedChromeZip:
    filename: str
    version: str
    sha256: str
    size_bytes: int
    bytes: bytes
    actions_archive_sha256: str
    entries: Mapping[str, bytes] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


def qualify_downloaded_artifact(
    actions_archive: bytes,
    *,
    expected_server_digest: str,
    release_tag: str,
) -> QualifiedChromeZip:
    if not TAG.fullmatch(release_tag):
        raise ContractError("release tag has an invalid format")
    archive_sha256 = sha256_bytes(actions_archive)
    if expected_server_digest != f"sha256:{archive_sha256}":
        raise ContractError("downloaded Actions artifact does not match its server digest")
    outer = _read_exact_zip_entries(actions_archive, label="Actions artifact")
    agent_names = [name for name in outer if AGENT_ZIP.fullmatch(name)]
    installer_names = [name for name in outer if INSTALLER.fullmatch(name)]
    expected_names = set(agent_names + installer_names + ["sha256sums.txt"])
    if len(agent_names) != 1 or len(installer_names) != 1 or set(outer) != expected_names:
        raise ContractError(
            "Actions artifact must contain exactly one installer, one Agent ZIP, "
            "and sha256sums.txt"
        )
    agent_match = AGENT_ZIP.fullmatch(agent_names[0])
    assert agent_match is not None
    if release_tag != f"v{agent_match.group('version')}":
        raise ContractError("release tag and packaged Agent version differ")
    sums = _parse_sha256sums(outer["sha256sums.txt"])
    expected_sum_names = set(agent_names + installer_names)
    if set(sums) != expected_sum_names:
        raise ContractError("sha256sums.txt does not describe the exact published files")
    for name in expected_sum_names:
        if sha256_bytes(outer[name]) != sums[name]:
            raise ContractError(f"sha256sums.txt digest mismatch for {name}")

    filename = agent_names[0]
    version = agent_match.group("version")
    chrome_zip = outer[filename]
    inner = _read_exact_zip_entries(chrome_zip, label="Chrome ZIP")
    if "manifest.json" not in inner or "build-meta.json" not in inner:
        raise ContractError("Chrome ZIP is missing manifest.json or build-meta.json")
    manifest = load_json_strict(inner["manifest.json"], label="Chrome manifest")
    metadata = load_json_strict(inner["build-meta.json"], label="extension build metadata")
    if not isinstance(manifest, dict) or not isinstance(metadata, dict):
        raise ContractError("Chrome manifest and build metadata must be objects")
    if metadata.get("schema") != EXTENSION_BUILD_SCHEMA:
        raise ContractError(
            f"extension build metadata schema is not {EXTENSION_BUILD_SCHEMA}"
        )
    if metadata.get("determinism_verified") is not True:
        raise ContractError("extension build metadata does not assert determinism")
    derived_extension_id = extension_id_from_manifest_key(manifest.get("key"))
    if (
        derived_extension_id != EXPECTED_EXTENSION_ID
        or metadata.get("extension_id") != derived_extension_id
    ):
        raise ContractError("Chrome ZIP carries the wrong extension identity")
    if manifest.get("manifest_version") != 3 or metadata.get("target") != "chrome116":
        raise ContractError("Chrome ZIP is not the qualified Manifest V3 Chrome target")
    if metadata.get("extension_version") != version or manifest.get("version") != version:
        raise ContractError("Chrome ZIP filename, manifest, and metadata versions differ")
    outputs = metadata.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        raise ContractError("extension build metadata has no outputs map")
    if set(inner) != set(outputs) | {"build-meta.json"}:
        raise ContractError("Chrome ZIP entries do not match the build metadata outputs")
    for name, declared in outputs.items():
        if not isinstance(name, str) or not isinstance(declared, str):
            raise ContractError("extension output digest entry is malformed")
        if not SHA256_DIGEST.fullmatch(declared):
            raise ContractError(
                f"extension output digest has invalid format for {name}"
            )
        expected = f"sha256:{sha256_bytes(inner[name])}"
        if declared != expected:
            raise ContractError(f"extension output digest mismatch for {name}")

    with zipfile.ZipFile(io.BytesIO(chrome_zip)) as archive:
        infos = archive.infolist()
        if [info.filename for info in infos] != sorted(
            info.filename for info in infos
        ):
            raise ContractError("Chrome ZIP entries are not in deterministic order")
        if archive.comment:
            raise ContractError("Chrome ZIP has an unexpected archive comment")
        for info in infos:
            if info.date_time != FIXED_ZIP_TIMESTAMP:
                raise ContractError(
                    f"Chrome ZIP entry {info.filename!r} has non-deterministic timestamp"
                )
            if (
                info.external_attr != 0
                or info.extra
                or info.comment
                or info.compress_type != zipfile.ZIP_DEFLATED
            ):
                raise ContractError(
                    f"Chrome ZIP entry {info.filename!r} has non-deterministic metadata"
                )
    return QualifiedChromeZip(
        filename=filename,
        version=version,
        sha256=sha256_bytes(chrome_zip),
        size_bytes=len(chrome_zip),
        bytes=chrome_zip,
        actions_archive_sha256=archive_sha256,
        entries=inner,
        metadata=metadata,
    )


@dataclass(frozen=True)
class LegalBindingsCoordinates:
    """Where the Legal binding document is, and what it must digest to.

    ``source_revision`` is revision A, the approval revision the document
    declares. ``fetch_revision`` is revision B, the revision the document is
    read at. The Legal contract forbids requiring A == B, so they stay two
    values here and in the signed payload.
    """

    source_revision: str
    fetch_revision: str
    document_path: str
    expected_digest: str


def resolve_legal_bindings_coordinates(
    *,
    source_revision: str,
    fetch_revision: str,
    document_path: str,
    expected_digest: str,
) -> LegalBindingsCoordinates:
    """Validate the declared coordinates before anything is retrieved.

    The two revisions are independent inputs. The Legal contract forbids
    requiring them to agree, so each is checked on its own and they are never
    compared with each other or collapsed into one value.
    """
    if not HEX_40.fullmatch(source_revision):
        refuse(
            STEP_LEGAL_COORDINATES,
            "the Legal repository revision is not a 40-character lowercase commit",
        )
    if not HEX_40.fullmatch(fetch_revision):
        refuse(
            STEP_LEGAL_COORDINATES,
            "the Legal bindings repository revision is not a 40-character lowercase commit",
        )
    if not HEX_64.fullmatch(expected_digest):
        refuse(
            STEP_LEGAL_COORDINATES,
            "the Legal bindings digest must be bare lowercase 64-hex",
        )
    segments = document_path.split("/")
    if (
        not document_path
        or document_path != document_path.strip()
        or document_path.startswith("/")
        or "\\" in document_path
        or not document_path.endswith(".json")
        or any(segment in ("", ".", "..") for segment in segments)
    ):
        refuse(
            STEP_LEGAL_COORDINATES,
            "the Legal bindings path is not a repository-relative JSON document",
        )
    return LegalBindingsCoordinates(
        source_revision=source_revision,
        fetch_revision=fetch_revision,
        document_path=document_path,
        expected_digest=expected_digest,
    )


@dataclass(frozen=True)
class EmbeddedLegalBindings:
    """The Legal binding state the artifact declares about itself."""

    schema: str
    source_revision: str
    digest: str


def read_embedded_legal_bindings(
    metadata: Mapping[str, Any],
) -> EmbeddedLegalBindings:
    """Read the artifact's own declaration, which is compared, never trusted.

    A release artifact records exactly three members. The development path
    records ``null`` here, so a development-mode artifact refuses before any
    retrieval and can never qualify.
    """
    if "legal_bindings" not in metadata:
        refuse(
            STEP_LEGAL_COORDINATES,
            "extension build metadata declares no legal_bindings member",
        )
    block = metadata["legal_bindings"]
    if block is None:
        refuse(
            STEP_LEGAL_COORDINATES,
            "the artifact records no Legal bindings, so it is not a release artifact",
        )
    if not isinstance(block, dict):
        refuse(STEP_LEGAL_COORDINATES, "recorded Legal bindings are not an object")
    if tuple(sorted(block)) != LEGAL_BINDINGS_KEYS:
        refuse(
            STEP_LEGAL_COORDINATES,
            f"recorded Legal bindings carry {sorted(block)} "
            f"rather than {list(LEGAL_BINDINGS_KEYS)}",
        )
    schema = block["schema"]
    source_revision = block["source_revision"]
    digest = block["legal_bindings_digest"]
    if schema != LEGAL_BINDINGS_SCHEMA:
        refuse(
            STEP_LEGAL_COORDINATES,
            f"recorded Legal bindings schema is not {LEGAL_BINDINGS_SCHEMA}",
        )
    if not isinstance(source_revision, str) or not HEX_40.fullmatch(source_revision):
        refuse(
            STEP_LEGAL_COORDINATES,
            "the recorded Legal source revision is not a 40-character commit",
        )
    if not isinstance(digest, str) or not HEX_64.fullmatch(digest):
        refuse(
            STEP_LEGAL_COORDINATES,
            "the recorded Legal bindings digest is not bare lowercase 64-hex",
        )
    return EmbeddedLegalBindings(
        schema=schema,
        source_revision=source_revision,
        digest=digest,
    )


@dataclass(frozen=True)
class PackagedSigningRule:
    """The packaged signing rule identity recomputed from artifact bytes."""

    schema: str
    source_revision: str
    sha256: str


def qualify_packaged_signing_rule(
    chrome_zip: QualifiedChromeZip,
) -> PackagedSigningRule:
    """Recompute the packaged signing rule identity from the ZIP itself."""
    declared = chrome_zip.metadata.get("signing_rule")
    if declared is None:
        refuse(
            STEP_SIGNING_RULE,
            "the artifact records no packaged signing rule, "
            "so it is not a release artifact",
        )
    if not isinstance(declared, dict) or tuple(sorted(declared)) != SIGNING_RULE_KEYS:
        refuse(
            STEP_SIGNING_RULE,
            "the recorded packaged signing rule has unexpected members",
        )
    rule_bytes = chrome_zip.entries.get(SIGNING_RULE_FILE)
    if rule_bytes is None:
        refuse(
            STEP_SIGNING_RULE,
            f"the Chrome ZIP does not carry {SIGNING_RULE_FILE}",
        )
    document = load_json_strict(rule_bytes, label="packaged signing rule")
    if not isinstance(document, dict):
        refuse(STEP_SIGNING_RULE, "the packaged signing rule is not an object")
    digest = f"sha256:{sha256_bytes(rule_bytes)}"
    if declared["sha256"] != digest:
        refuse(
            STEP_SIGNING_RULE,
            "the recorded packaged signing rule digest is not the digest of the "
            "packaged rule",
        )
    if declared["schema"] != document.get("schema") or declared[
        "source_revision"
    ] != document.get("source_revision"):
        refuse(
            STEP_SIGNING_RULE,
            "the recorded packaged signing rule identity is not the packaged "
            "rule's own identity",
        )
    background = chrome_zip.entries.get(BACKGROUND_FILE)
    if background is None:
        refuse(STEP_SIGNING_RULE, f"the Chrome ZIP does not carry {BACKGROUND_FILE}")
    source = background.decode("utf-8", errors="replace")
    rule_base64 = base64.b64encode(rule_bytes).decode("ascii")
    if source.count(rule_base64) != 1 or source.count(digest) != 1:
        refuse(
            STEP_SIGNING_RULE,
            "the packaged signing rule is not bound exactly once into "
            f"{BACKGROUND_FILE}",
        )
    return PackagedSigningRule(
        schema=declared["schema"],
        source_revision=declared["source_revision"],
        sha256=digest,
    )


@dataclass(frozen=True)
class ReleasePrivacyPolicy:
    """The privacy policy coordinate the shipped artifact resolves to."""

    url: str
    version: str
    rendered_sha256: str
    locale: str


def qualify_release_privacy_policy(
    chrome_zip: QualifiedChromeZip,
    document: Mapping[str, Any],
) -> ReleasePrivacyPolicy:
    """Bind the packaged privacy policy URL to the verified Legal instrument."""
    if chrome_zip.metadata.get("privacy_policy_configured") is not True:
        refuse(
            STEP_PRIVACY_POLICY,
            "the artifact declares no configured release privacy policy",
        )
    config_bytes = chrome_zip.entries.get(EXTENSION_CONFIG_FILE)
    if config_bytes is None:
        refuse(
            STEP_PRIVACY_POLICY,
            f"the Chrome ZIP does not carry {EXTENSION_CONFIG_FILE}",
        )
    config = load_json_strict(config_bytes, label="extension configuration")
    if not isinstance(config, dict):
        refuse(STEP_PRIVACY_POLICY, "the extension configuration is not an object")
    packaged_url = config.get("privacy_policy_url")
    if not isinstance(packaged_url, str) or not packaged_url:
        refuse(
            STEP_PRIVACY_POLICY,
            "the packaged extension configures no privacy policy URL",
        )
    origin = document.get("public_origin")
    instruments = document.get("instruments")
    instrument = (
        instruments.get(PRIVACY_POLICY_INSTRUMENT)
        if isinstance(instruments, dict)
        else None
    )
    if not isinstance(origin, str) or not isinstance(instrument, dict):
        refuse(
            STEP_PRIVACY_POLICY,
            "the verified Legal bindings carry no privacy policy instrument",
        )
    route = instrument.get("public_url")
    version = instrument.get("version")
    rendered = instrument.get("rendered_sha256")
    locale = instrument.get("locale")
    coordinates = (route, version, rendered, locale)
    if not all(isinstance(value, str) and value for value in coordinates):
        refuse(
            STEP_PRIVACY_POLICY,
            "the privacy policy instrument coordinates are malformed",
        )
    if not route.startswith("/"):
        refuse(
            STEP_PRIVACY_POLICY,
            "the privacy policy route is not an absolute public route",
        )
    expected_url = f"{origin.rstrip('/')}{route}"
    if packaged_url != expected_url:
        refuse(
            STEP_PRIVACY_POLICY,
            "the packaged privacy policy URL is not the verified Legal instrument's "
            "public route",
        )
    return ReleasePrivacyPolicy(
        url=packaged_url,
        version=version,
        rendered_sha256=rendered,
        locale=locale,
    )


def stage_legal_bindings_document(
    coordinates: LegalBindingsCoordinates,
    *,
    output: Path,
    runner_temp: Path,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    """Retrieve the Legal binding document through the release gate.

    The gate at ``tools/legal-release-bindings/verify.mjs`` is the one
    authenticated retrieval path; attestation reuses it rather than opening a
    second one. Its refusal codes are mapped onto the step that owns them so a
    retrieval failure stays distinguishable from a digest failure.
    """
    if not LEGAL_BINDINGS_GATE.is_file():
        refuse(
            STEP_LEGAL_RETRIEVAL,
            f"the Legal release bindings gate is missing at {LEGAL_BINDINGS_GATE}",
        )
    values = dict(os.environ if environment is None else environment)
    values.update(
        {
            "LEGAL_REPOSITORY_REVISION": coordinates.source_revision,
            "LEGAL_BINDINGS_REPOSITORY_REVISION": coordinates.fetch_revision,
            "LEGAL_BINDINGS_PATH": coordinates.document_path,
            "LEGAL_BINDINGS_DIGEST": coordinates.expected_digest,
            "RUNNER_TEMP": str(runner_temp),
        }
    )
    completed = subprocess.run(
        ["node", str(LEGAL_BINDINGS_GATE), f"--output={output}"],
        cwd=str(PRODUCER_ROOT),
        env=values,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        step = GATE_EXIT_STEPS.get(completed.returncode, STEP_LEGAL_RETRIEVAL)
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        refuse(
            step,
            "the Legal release bindings gate refused with exit "
            f"{completed.returncode}: {detail[-1] if detail else 'no detail'}",
        )
    if not output.is_file():
        refuse(
            STEP_LEGAL_RETRIEVAL,
            "the Legal release bindings gate staged no document",
        )
    return output.read_bytes()


@dataclass(frozen=True)
class QualifiedLegalBindings:
    """A Legal binding document retrieved and digested by attestation itself."""

    schema: str
    source_revision: str
    fetch_revision: str
    document_path: str
    digest: str
    document: Mapping[str, Any]
    path: Path


def qualify_legal_bindings(
    chrome_zip: QualifiedChromeZip,
    coordinates: LegalBindingsCoordinates,
    *,
    output: Path,
    runner_temp: Path,
    environment: Mapping[str, str] | None = None,
) -> QualifiedLegalBindings:
    """Retrieve, recompute and reconcile the Legal bindings for this ZIP.

    The gate already compares the fetched bytes against the canonical
    serialization, which is what rejects a duplicated member name; the digest
    is recomputed here as well, over the bytes that were actually staged, and
    reconciled with both the declared coordinate and the artifact's own
    embedded block. Disagreement is a provenance failure, not a warning.
    """
    embedded = read_embedded_legal_bindings(chrome_zip.metadata)
    if embedded.source_revision != coordinates.source_revision:
        refuse(
            STEP_LEGAL_COORDINATES,
            "the artifact was packaged against Legal source revision "
            f"{embedded.source_revision}, not {coordinates.source_revision}",
        )
    staged = stage_legal_bindings_document(
        coordinates,
        output=output,
        runner_temp=runner_temp,
        environment=environment,
    )
    digest = hashlib.sha256(staged).hexdigest()
    if digest != coordinates.expected_digest:
        refuse(
            STEP_LEGAL_DIGEST,
            "the retrieved Legal bindings digest to "
            f"{digest}, not the declared {coordinates.expected_digest}",
        )
    if digest != embedded.digest:
        refuse(
            STEP_LEGAL_DIGEST,
            "the artifact was packaged against Legal bindings digest "
            f"{embedded.digest}, not the {digest} recomputed here",
        )
    document = load_json_strict(staged, label="Legal release bindings")
    if not isinstance(document, dict):
        refuse(STEP_LEGAL_DIGEST, "the Legal release bindings are not an object")
    if document.get("schema") != LEGAL_BINDINGS_SCHEMA:
        refuse(
            STEP_LEGAL_DIGEST,
            f"the Legal release bindings schema is not {LEGAL_BINDINGS_SCHEMA}",
        )
    # Revision A is read out of the retrieved document rather than taken from
    # the dispatch input, so the signed value is the document's own approval
    # revision and the input only has to agree with it.
    approval_revision = document.get("legal_repository_revision")
    if approval_revision != coordinates.source_revision:
        refuse(
            STEP_LEGAL_DIGEST,
            "the retrieved Legal bindings declare a different source revision",
        )
    return QualifiedLegalBindings(
        schema=LEGAL_BINDINGS_SCHEMA,
        source_revision=approval_revision,
        fetch_revision=coordinates.fetch_revision,
        document_path=coordinates.document_path,
        digest=digest,
        document=document,
        path=output,
    )


def run_package_audit(
    *,
    artifact: Path,
    signing_rule: Path,
    legal_bindings: Path,
) -> str:
    """Require the exact Store ZIP to pass the exact-binding package audit."""
    if not EXTENSION_BUILD_SCRIPT.is_file():
        refuse(
            STEP_PACKAGE_AUDIT,
            f"the extension build script is missing at {EXTENSION_BUILD_SCRIPT}",
        )
    completed = subprocess.run(
        [
            "node",
            str(EXTENSION_BUILD_SCRIPT),
            "--audit-package",
            f"--artifact={artifact}",
            f"--packaged-signing-rule={signing_rule}",
            f"--legal-release-bindings={legal_bindings}",
        ],
        cwd=str(EXTENSION_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    output = f"{completed.stdout}{completed.stderr}".strip()
    if completed.returncode != 0:
        detail = output.splitlines()
        refuse(
            STEP_PACKAGE_AUDIT,
            f"the package audit refused the Store ZIP with exit "
            f"{completed.returncode}: {detail[-1] if detail else 'no detail'}",
        )
    if "Chrome archive audit passed" not in output:
        refuse(
            STEP_PACKAGE_AUDIT,
            "the package audit exited zero without reporting a passing audit",
        )
    return output


def recompute_store_zip_digest(path: Path, *, expected: str) -> str:
    """Recompute the Store ZIP digest from the bytes the audit ran against."""
    digester = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digester.update(block)
    digest = digester.hexdigest()
    if digest != expected:
        refuse(
            STEP_ARTIFACT_DIGEST,
            f"the audited Store ZIP digests to {digest}, not {expected}",
        )
    return digest


@dataclass(frozen=True)
class PackageQualification:
    """The controlled package audit this producer run performed."""

    workflow: str
    job: str
    job_id: int
    run_id: int
    run_attempt: int
    conclusion: str


def require_package_qualification(client: GitHubApi) -> PackageQualification:
    """Require this run's own qualification job to have concluded successfully.

    The conclusion is read from the Actions API for this run rather than from
    an output the unprivileged job wrote, and the producer workflow definition
    that decides what that job does is already pinned by
    ``require_current_producer_ref``.
    """
    run_id = _positive_integer(
        _required_environment("GITHUB_RUN_ID"), "GITHUB_RUN_ID"
    )
    run_attempt = _positive_integer(
        _required_environment("GITHUB_RUN_ATTEMPT"), "GITHUB_RUN_ATTEMPT"
    )
    payload = client.get(
        f"/repos/{PRODUCT_REPOSITORY}/actions/runs/{run_id}"
        f"/attempts/{run_attempt}/jobs?per_page=100"
    )
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        refuse(
            STEP_PACKAGE_QUALIFICATION,
            "the producer run reports no jobs",
        )
    names = {job.get("name") for job in jobs}
    if names != PRODUCER_JOB_NAMES:
        refuse(
            STEP_PACKAGE_QUALIFICATION,
            f"the producer run runs {sorted(str(name) for name in names)} "
            f"rather than {sorted(PRODUCER_JOB_NAMES)}",
        )
    matched = [job for job in jobs if job.get("name") == QUALIFICATION_JOB_NAME]
    if len(matched) != 1:
        refuse(
            STEP_PACKAGE_QUALIFICATION,
            f"the producer run has {len(matched)} {QUALIFICATION_JOB_NAME!r} jobs",
        )
    qualification = matched[0]
    if (
        qualification.get("status") != "completed"
        or qualification.get("conclusion") != "success"
    ):
        refuse(
            STEP_PACKAGE_QUALIFICATION,
            "the controlled package qualification did not conclude successfully",
        )
    if qualification.get("run_id") != run_id or (
        qualification.get("run_attempt") != run_attempt
    ):
        refuse(
            STEP_PACKAGE_QUALIFICATION,
            "the qualification job belongs to a different run or attempt",
        )
    if qualification.get("head_sha") != _required_environment("GITHUB_SHA"):
        refuse(
            STEP_PACKAGE_QUALIFICATION,
            "the qualification job ran from a different producer revision",
        )
    job_id = qualification.get("id")
    if not isinstance(job_id, int) or job_id <= 0:
        refuse(
            STEP_PACKAGE_QUALIFICATION,
            "the qualification job carries no job identifier",
        )
    return PackageQualification(
        workflow=PRODUCER_WORKFLOW,
        job=QUALIFICATION_JOB_NAME,
        job_id=job_id,
        run_id=run_id,
        run_attempt=run_attempt,
        conclusion="success",
    )


@dataclass(frozen=True)
class QualifiedStoreRelease:
    """Everything the precondition chain established about one Store ZIP."""

    chrome_zip: QualifiedChromeZip
    legal_bindings: QualifiedLegalBindings
    signing_rule: PackagedSigningRule
    privacy_policy: ReleasePrivacyPolicy
    artifact_path: Path
    artifact_sha256: str


def qualify_store_release(
    chrome_zip: QualifiedChromeZip,
    coordinates: LegalBindingsCoordinates,
    *,
    temporary_root: Path,
    audit_package: bool,
    environment: Mapping[str, str] | None = None,
) -> QualifiedStoreRelease:
    """Run the precondition chain against the exact downloaded Store ZIP.

    The chain is ordered, not a set of independent checks: the coordinates
    decide before anything is retrieved, retrieval and the recomputed digest
    decide before the artifact is read for its own bindings, and the ZIP digest
    is recomputed last, over the bytes the audit ran against. Any refusal
    raises out of this function, so a caller that signs only after it returns
    cannot sign past a failed precondition.
    """
    staged_document = temporary_root / "legal-release-bindings.json"
    legal_bindings = qualify_legal_bindings(
        chrome_zip,
        coordinates,
        output=staged_document,
        runner_temp=temporary_root,
        environment=environment,
    )
    signing_rule = qualify_packaged_signing_rule(chrome_zip)
    privacy_policy = qualify_release_privacy_policy(
        chrome_zip, legal_bindings.document
    )
    artifact_path = temporary_root / chrome_zip.filename
    artifact_path.write_bytes(chrome_zip.bytes)
    if audit_package:
        rule_path = temporary_root / SIGNING_RULE_FILE
        rule_path.write_bytes(chrome_zip.entries[SIGNING_RULE_FILE])
        run_package_audit(
            artifact=artifact_path,
            signing_rule=rule_path,
            legal_bindings=staged_document,
        )
    artifact_sha256 = recompute_store_zip_digest(
        artifact_path, expected=chrome_zip.sha256
    )
    return QualifiedStoreRelease(
        chrome_zip=chrome_zip,
        legal_bindings=legal_bindings,
        signing_rule=signing_rule,
        privacy_policy=privacy_policy,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
    )


def fetch_git_blob(
    client: GitHubApi,
    repository: str,
    path: str,
    commit: str,
) -> tuple[str, bytes]:
    _require_hex(commit, HEX_40, "Git blob commit")
    normalized_path = _require_review_git_path(path, "Git blob path")
    commit_document = client.get(
        f"{_repo_path(repository)}/git/commits/{commit}"
    )
    commit_tree = (
        commit_document.get("tree") if isinstance(commit_document, dict) else None
    )
    tree_sha = commit_tree.get("sha") if isinstance(commit_tree, dict) else None
    if not isinstance(tree_sha, str) or not re.fullmatch(
        r"[a-f0-9]{40,64}", tree_sha
    ):
        raise ContractError(f"Git commit {commit!r} has no tree object")

    parts = normalized_path.split("/")
    blob_sha: str | None = None
    for index, part in enumerate(parts):
        tree_document = client.get(
            f"{_repo_path(repository)}/git/trees/{tree_sha}"
        )
        if not isinstance(tree_document, dict) or tree_document.get("truncated") is True:
            raise ContractError(f"Git tree lookup for {path!r} was invalid or truncated")
        entries = tree_document.get("tree")
        if not isinstance(entries, list):
            raise ContractError(f"Git tree lookup for {path!r} returned no entries")
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("path") == part
        ]
        if len(matches) != 1:
            raise ContractError(f"Git path {path!r} was not uniquely present")
        entry = matches[0]
        entry_sha = entry.get("sha")
        if not isinstance(entry_sha, str) or not re.fullmatch(
            r"[a-f0-9]{40,64}", entry_sha
        ):
            raise ContractError(f"Git path {path!r} has an invalid object ID")
        if index < len(parts) - 1:
            if entry.get("type") != "tree" or entry.get("mode") != "040000":
                raise ContractError(f"Git path {path!r} crosses a non-tree object")
            tree_sha = entry_sha
            continue
        if entry.get("type") != "blob" or entry.get("mode") != "100644":
            raise ContractError(f"Git path {path!r} is not a regular non-executable blob")
        blob_sha = entry_sha
    if blob_sha is None:
        raise ContractError(f"Git path {path!r} did not resolve to a blob")
    blob = client.get(f"{_repo_path(repository)}/git/blobs/{blob_sha}")
    if not isinstance(blob, dict) or blob.get("encoding") != "base64":
        raise ContractError(f"Git blob for {path!r} is not Base64 encoded")
    encoded_content = "".join(str(blob.get("content", "")).split())
    try:
        data = base64.b64decode(encoded_content, validate=True)
    except ValueError as exc:
        raise ContractError(f"Git blob for {path!r} has invalid Base64") from exc
    if blob.get("size") != len(data):
        raise ContractError(f"Git blob size mismatch for {path!r}")
    return blob_sha, data


def resolve_branch_head(client: GitHubApi, repository: str, branch: str) -> str:
    encoded = urllib.parse.quote(branch, safe="")
    ref = client.get(f"{_repo_path(repository)}/git/ref/heads/{encoded}")
    obj = ref.get("object") if isinstance(ref, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    return _require_hex(str(sha or ""), HEX_40, f"{repository} {branch} head")


@dataclass(frozen=True)
class ReviewProjection:
    source_commit: str
    current_commit: str
    digest: str
    value: dict[str, Any]


def _declared_projection_digest(data: bytes, *, label: str) -> str:
    if len(data) != 65 or data[-1:] != b"\n":
        raise ContractError(f"{label} must contain 64 lowercase hex characters and one LF")
    try:
        value = data[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ContractError(f"{label} is not ASCII") from exc
    return _require_hex(value, HEX_64, label)


def _validate_projection_snapshot(
    snapshot_bytes: bytes,
    digest_bytes: bytes,
    *,
    label: str,
    expected_digest: str,
) -> dict[str, Any]:
    value = load_json_strict(snapshot_bytes, label=f"{label} review projection")
    if not isinstance(value, dict):
        raise ContractError("review projection snapshots must be JSON objects")
    canonical_bytes = canonical_projection_bytes(value)
    declared_digest = _declared_projection_digest(
        digest_bytes, label=f"{label} review projection digest"
    )
    if snapshot_bytes != canonical_bytes:
        raise ContractError(f"{label} review projection blob is not canonical JSON")
    if (
        sha256_bytes(snapshot_bytes) != expected_digest
        or canonical_projection_sha256(value) != expected_digest
        or declared_digest != expected_digest
    ):
        raise ContractError(
            "review projection digest changed or does not match the supplied "
            "canonical digest"
        )
    return value


def validate_review_projection(
    client: GitHubApi,
    *,
    configuration: ReviewConfiguration,
    source_commit: str,
    current_commit: str,
    expected_digest: str,
) -> ReviewProjection:
    _require_hex(source_commit, HEX_40, "legal_projection_source_commit")
    _require_hex(expected_digest, HEX_64, "legal_projection_canonical_sha256")
    require_ancestor(
        client,
        configuration.repository,
        source_commit,
        current_commit,
        label="review projection source",
    )
    _, source_bytes = fetch_git_blob(
        client,
        configuration.repository,
        configuration.projection_path,
        source_commit,
    )
    _, current_bytes = fetch_git_blob(
        client,
        configuration.repository,
        configuration.projection_path,
        current_commit,
    )
    _, source_digest_bytes = fetch_git_blob(
        client,
        configuration.repository,
        configuration.projection_digest_path,
        source_commit,
    )
    _, current_digest_bytes = fetch_git_blob(
        client,
        configuration.repository,
        configuration.projection_digest_path,
        current_commit,
    )
    source_value = _validate_projection_snapshot(
        source_bytes,
        source_digest_bytes,
        label="source",
        expected_digest=expected_digest,
    )
    _validate_projection_snapshot(
        current_bytes,
        current_digest_bytes,
        label="current",
        expected_digest=expected_digest,
    )
    return ReviewProjection(
        source_commit=source_commit,
        current_commit=current_commit,
        digest=expected_digest,
        value=source_value,
    )


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mask_workflow_value(value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ContractError("derived workflow secret contains a newline")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value.replace('%', '%25')}", flush=True)


def create_app_jwt(
    app_id: int,
    private_key_path: Path,
    *,
    now: int | None = None,
    openssl: str = "openssl",
) -> str:
    if app_id <= 0:
        raise ContractError("review App ID must be positive")
    issued = int(time.time() if now is None else now)
    header = _base64url(b'{"alg":"RS256","typ":"JWT"}')
    payload = _base64url(
        json.dumps(
            {"iat": issued - 60, "exp": issued + 540, "iss": str(app_id)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _run_openssl(
        ["dgst", "-sha256", "-sign", str(private_key_path)],
        input_bytes=signing_input,
        openssl=openssl,
    )
    return f"{header}.{payload}.{_base64url(signature)}"


@dataclass(frozen=True)
class HandoffIdentity:
    app_id: int
    installation_id: int
    bot_user_id: int
    repository_id: int
    app_slug: str


def acquire_handoff_client(
    *,
    configuration: ReviewConfiguration,
    app_id: int,
    installation_id: int,
    expected_bot_user_id: int,
    expected_repository_id: int,
    app_private_key_path: Path,
    api_url: str,
    openssl: str = "openssl",
    client_factory: Callable[..., GitHubApi] = GitHubApi,
) -> tuple[GitHubApi, HandoffIdentity]:
    jwt = create_app_jwt(app_id, app_private_key_path, openssl=openssl)
    _mask_workflow_value(jwt)
    app_client = client_factory(jwt, api_url=api_url)
    app = app_client.get("/app")
    if not isinstance(app, dict) or app.get("id") != app_id:
        raise ContractError("GitHub App JWT resolved to the wrong numeric App ID")
    slug = app.get("slug")
    if not isinstance(slug, str) or not slug:
        raise ContractError("GitHub App has no slug")
    installation = app_client.get(f"/app/installations/{installation_id}")
    if not isinstance(installation, dict):
        raise ContractError("GitHub App installation response is not an object")
    if installation.get("id") != installation_id or installation.get("app_id") != app_id:
        raise ContractError("GitHub App installation identity mismatch")
    if installation.get("suspended_at") is not None:
        raise ContractError("GitHub App installation is suspended")
    if installation.get("repository_selection") != "selected":
        raise ContractError("GitHub App installation must use selected repositories")
    required_permissions = {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
    }
    if installation.get("permissions") != required_permissions:
        raise ContractError(
            "review App installation permissions exceed or differ from the contract"
        )

    token_document = app_client.post(
        f"/app/installations/{installation_id}/access_tokens",
        {
            "repository_ids": [expected_repository_id],
            "permissions": {"contents": "write", "pull_requests": "write"},
        },
    )
    token = token_document.get("token") if isinstance(token_document, dict) else None
    if not isinstance(token, str) or not token:
        raise ContractError("GitHub App installation token response has no token")
    _mask_workflow_value(token)
    if token_document.get("permissions") != required_permissions:
        raise ContractError("review installation token permissions mismatch")
    installation_client = client_factory(token, api_url=api_url)
    resolved_repository = installation_client.get(
        _repo_path(configuration.repository)
    )
    if (
        not isinstance(resolved_repository, dict)
        or resolved_repository.get("id") != expected_repository_id
        or resolved_repository.get("full_name") != configuration.repository
        or resolved_repository.get("default_branch") != configuration.default_branch
    ):
        raise ContractError(
            "resolved review repository address, identity, or default branch changed"
        )
    repositories_document = installation_client.get(
        "/installation/repositories?per_page=100"
    )
    if not isinstance(repositories_document, dict):
        raise ContractError("review installation repositories response is not an object")
    repositories = (
        repositories_document.get("repositories", [])
    )
    if repositories_document.get("total_count") != 1 or len(repositories) != 1:
        raise ContractError("review installation token is not limited to one repository")
    repository = repositories[0]
    if (
        not isinstance(repository, dict)
        or repository.get("id") != expected_repository_id
        or repository.get("full_name") != configuration.repository
    ):
        raise ContractError("review installation token resolved to the wrong repository")
    encoded_bot = urllib.parse.quote(f"{slug}[bot]", safe="")
    bot = installation_client.get(f"/users/{encoded_bot}")
    if not isinstance(bot, dict) or bot.get("id") != expected_bot_user_id:
        raise ContractError("GitHub App bot numeric identity mismatch")
    return installation_client, HandoffIdentity(
        app_id=app_id,
        installation_id=installation_id,
        bot_user_id=expected_bot_user_id,
        repository_id=expected_repository_id,
        app_slug=slug,
    )


def _create_git_blob(client: GitHubApi, repository: str, data: bytes) -> str:
    document = client.post(
        f"{_repo_path(repository)}/git/blobs",
        {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
    )
    sha = document.get("sha") if isinstance(document, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40,64}", sha):
        raise ContractError("GitHub did not return a blob object ID")
    return sha


def retire_stale_evidence_prs(
    client: GitHubApi,
    *,
    configuration: ReviewConfiguration,
    identity: HandoffIdentity,
    current_base_commit: str,
) -> list[int]:
    _require_hex(current_base_commit, HEX_40, "current review base commit")
    pulls: list[Any] = []
    for page in range(1, 11):
        query = urllib.parse.urlencode(
            {
                "state": "open",
                "base": configuration.default_branch,
                "per_page": 100,
                "page": page,
            }
        )
        page_value = client.get(
            f"{_repo_path(configuration.repository)}/pulls?{query}"
        )
        if not isinstance(page_value, list):
            raise ContractError("open review pull-request response is not a list")
        pulls.extend(page_value)
        if len(page_value) < 100:
            break
    else:
        raise ContractError("more than 1,000 open review pull requests require inspection")

    retired: list[int] = []
    for pull in pulls:
        if not isinstance(pull, dict):
            raise ContractError("open review pull-request entry is not an object")
        user = pull.get("user")
        head = pull.get("head")
        base_ref = pull.get("base")
        head_repository = head.get("repo") if isinstance(head, dict) else None
        base_repository = base_ref.get("repo") if isinstance(base_ref, dict) else None
        if (
            not isinstance(user, dict)
            or user.get("id") != identity.bot_user_id
            or not isinstance(head, dict)
            or not isinstance(head_repository, dict)
            or head_repository.get("id") != identity.repository_id
            or not str(head.get("ref", "")).startswith("engineering-attestation/")
            or not isinstance(base_ref, dict)
            or base_ref.get("ref") != configuration.default_branch
            or not isinstance(base_repository, dict)
            or base_repository.get("id") != identity.repository_id
        ):
            continue
        number = pull.get("number")
        head_ref = head.get("ref")
        head_sha = head.get("sha")
        base_sha = base_ref.get("sha")
        if (
            not isinstance(number, int)
            or number <= 0
            or not isinstance(head_sha, str)
            or not HEX_40.fullmatch(head_sha)
            or not isinstance(base_sha, str)
            or not HEX_40.fullmatch(base_sha)
            or not isinstance(head_ref, str)
        ):
            raise ContractError("stale review App pull request has invalid identity metadata")
        head_commit = client.get(
            f"{_repo_path(configuration.repository)}/git/commits/{head_sha}"
        )
        parents = head_commit.get("parents") if isinstance(head_commit, dict) else None
        if not isinstance(parents, list) or len(parents) != 1:
            raise ContractError(
                "open review App pull request does not have one evidence-commit parent"
            )
        parent = parents[0]
        parent_sha = parent.get("sha") if isinstance(parent, dict) else None
        if not isinstance(parent_sha, str) or not HEX_40.fullmatch(parent_sha):
            raise ContractError(
                "open review App pull request has an invalid evidence-commit parent"
            )
        if parent_sha == current_base_commit:
            continue
        client.patch(
            f"{_repo_path(configuration.repository)}/pulls/{number}",
            {"state": "closed"},
        )
        encoded_ref = urllib.parse.quote(head_ref, safe="")
        client.delete(
            f"{_repo_path(configuration.repository)}/git/refs/heads/{encoded_ref}"
        )
        retired.append(number)
    return retired


def create_evidence_pr(
    client: GitHubApi,
    *,
    configuration: ReviewConfiguration,
    identity: HandoffIdentity,
    review_base_commit: str,
    attestation_bytes: bytes,
    artifact_bytes: bytes,
    pr_body: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
) -> tuple[int, str]:
    _require_hex(review_base_commit, HEX_40, "review base commit")
    attestation_sha256 = sha256_bytes(attestation_bytes)
    _require_hex(attestation_sha256, HEX_64, "attestation SHA-256")
    commit = client.get(
        f"{_repo_path(configuration.repository)}/git/commits/{review_base_commit}"
    )
    base_tree = commit.get("tree") if isinstance(commit, dict) else None
    base_tree_sha = base_tree.get("sha") if isinstance(base_tree, dict) else None
    if not isinstance(base_tree_sha, str) or not re.fullmatch(
        r"[a-f0-9]{40,64}", base_tree_sha
    ):
        raise ContractError("review base commit has no tree")

    archive_prefix = (
        "compliance/engineering/releases/attestations/" + attestation_sha256
    )
    attestation_blob = _create_git_blob(
        client, configuration.repository, attestation_bytes
    )
    artifact_blob = _create_git_blob(client, configuration.repository, artifact_bytes)
    paths = {
        "compliance/engineering/releases/current/attestation.json": attestation_blob,
        "compliance/engineering/releases/current/artifact.bin": artifact_blob,
        f"{archive_prefix}/attestation.json": attestation_blob,
        f"{archive_prefix}/artifact.bin": artifact_blob,
    }
    tree = client.post(
        f"{_repo_path(configuration.repository)}/git/trees",
        {
            "base_tree": base_tree_sha,
            "tree": [
                {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
                for path, blob_sha in sorted(paths.items())
            ],
        },
    )
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str) or not re.fullmatch(
        r"[a-f0-9]{40,64}", tree_sha
    ):
        raise ContractError("GitHub did not return an evidence tree ID")
    evidence_commit = client.post(
        f"{_repo_path(configuration.repository)}/git/commits",
        {
            "message": f"Ingest engineering attestation {attestation_sha256}",
            "tree": tree_sha,
            "parents": [review_base_commit],
        },
    )
    evidence_commit_sha = (
        evidence_commit.get("sha") if isinstance(evidence_commit, dict) else None
    )
    if not isinstance(evidence_commit_sha, str) or not HEX_40.fullmatch(
        evidence_commit_sha
    ):
        raise ContractError("GitHub did not return an evidence commit SHA")

    branch = (
        f"engineering-attestation/{attestation_sha256}-"
        f"{workflow_run_id}-{workflow_run_attempt}"
    )
    encoded_branch = urllib.parse.quote(branch, safe="")
    pr_number: int | None = None
    ref_created = False
    try:
        client.post(
            f"{_repo_path(configuration.repository)}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": evidence_commit_sha},
        )
        ref_created = True
        pull = client.post(
            f"{_repo_path(configuration.repository)}/pulls",
            {
                "title": f"Engineering evidence {attestation_sha256[:12]}",
                "head": branch,
                "base": configuration.default_branch,
                "body": pr_body,
                "maintainer_can_modify": False,
            },
        )
        pr_number = pull.get("number") if isinstance(pull, dict) else None
        html_url = pull.get("html_url") if isinstance(pull, dict) else None
        user = pull.get("user") if isinstance(pull, dict) else None
        head = pull.get("head") if isinstance(pull, dict) else None
        base_ref = pull.get("base") if isinstance(pull, dict) else None
        head_repo = head.get("repo") if isinstance(head, dict) else None
        base_repo = base_ref.get("repo") if isinstance(base_ref, dict) else None
        if (
            not isinstance(pr_number, int)
            or pr_number <= 0
            or not isinstance(html_url, str)
            or not isinstance(user, dict)
            or user.get("id") != identity.bot_user_id
            or not isinstance(head, dict)
            or head.get("ref") != branch
            or head.get("sha") != evidence_commit_sha
            or not isinstance(head_repo, dict)
            or head_repo.get("id") != identity.repository_id
            or not isinstance(base_ref, dict)
            or base_ref.get("ref") != configuration.default_branch
            or base_ref.get("sha") != review_base_commit
            or not isinstance(base_repo, dict)
            or base_repo.get("id") != identity.repository_id
        ):
            raise ContractError(
                "created review PR failed numeric App/repository identity checks"
            )

        rest_commit = client.get(
            f"{_repo_path(configuration.repository)}/commits/{evidence_commit_sha}"
        )
        author = rest_commit.get("author") if isinstance(rest_commit, dict) else None
        parents = rest_commit.get("parents") if isinstance(rest_commit, dict) else None
        commit_metadata = (
            rest_commit.get("commit") if isinstance(rest_commit, dict) else None
        )
        verification = (
            commit_metadata.get("verification")
            if isinstance(commit_metadata, dict)
            else None
        )

        if (
            not isinstance(author, dict)
            or author.get("id") != identity.bot_user_id
            or not isinstance(verification, dict)
            or verification.get("verified") is not True
            or verification.get("reason") != "valid"
            or not isinstance(parents, list)
            or [parent.get("sha") for parent in parents if isinstance(parent, dict)]
            != [review_base_commit]
        ):
            raise ContractError(
                "created evidence commit failed App author, signature, or parent checks"
            )
        if (
            resolve_branch_head(
                client,
                configuration.repository,
                configuration.default_branch,
            )
            != review_base_commit
        ):
            raise ReviewBaseAdvancedError(
                "review default branch advanced while the evidence PR was created"
            )
        return pr_number, html_url
    except Exception:
        if pr_number is not None:
            try:
                client.patch(
                    f"{_repo_path(configuration.repository)}/pulls/{pr_number}",
                    {"state": "closed"},
                )
            except Exception:
                pass
        if ref_created:
            try:
                client.delete(
                    f"{_repo_path(configuration.repository)}/git/refs/heads/{encoded_branch}"
                )
            except Exception:
                pass
        raise


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ContractError(f"required environment variable {name} is empty")
    return value


def load_producer_control_baseline() -> str:
    return _require_hex(
        _required_environment("PRODUCER_CONTROL_BASELINE_SHA"),
        HEX_40,
        "PRODUCER_CONTROL_BASELINE_SHA",
    )


def _positive_integer(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ContractError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise ContractError(f"{label} must be positive")
    return parsed


def _write_github_outputs(values: Mapping[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise ContractError(f"GitHub output {name} contains a newline")
            output.write(f"{name}={value}\n")


def _decode_secret_to_file(environment_name: str, destination: Path) -> None:
    encoded = _required_environment(environment_name)
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ContractError(f"{environment_name} is not strict Base64") from exc
    if not data:
        raise ContractError(f"{environment_name} decodes to no bytes")
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "wb") as secret_file:
            descriptor = None
            secret_file.write(data)
        destination.chmod(0o600)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            destination.unlink(missing_ok=True)
        raise ContractError(f"unable to restrict temporary key permissions: {exc}") from exc


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def require_current_producer_ref(client: GitHubApi) -> None:
    expected_ref = f"refs/heads/{PRODUCT_DEFAULT_BRANCH}"
    if _required_environment("GITHUB_REF") != expected_ref:
        raise ContractError(
            f"engineering attestation must be dispatched from {expected_ref}"
        )
    workflow_sha = _required_environment("PRODUCER_WORKFLOW_SHA")
    run_sha = _required_environment("GITHUB_SHA")
    _require_hex(workflow_sha, HEX_40, "producer workflow SHA")
    _require_hex(run_sha, HEX_40, "workflow-dispatch SHA")
    current_main = resolve_branch_head(
        client, PRODUCT_REPOSITORY, PRODUCT_DEFAULT_BRANCH
    )
    if workflow_sha != run_sha or workflow_sha != current_main:
        raise ContractError(
            "protected producer is not the current Product main commit"
        )


def _pr_body(
    *,
    source: QualifiedSource,
    release_tag: str,
    chrome_zip: QualifiedChromeZip,
    projection: ReviewProjection,
    attestation_sha256: str,
    signer_fingerprint: str,
    identity: HandoffIdentity,
) -> str:
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    workflow_run_id = _required_environment("GITHUB_RUN_ID")
    run_url = f"{server_url}/{PRODUCT_REPOSITORY}/actions/runs/{workflow_run_id}"
    return f"""## Engineering evidence intake

This pull request contains the exact four evidence paths authorized by the v1 intake contract.

- Product workflow run: `{workflow_run_id}`
- Product source run: `{source.run_id}` (attempt `{source.run_attempt}`)
- Product CI run: `{source.product_ci_run_id}`
- Actions artifact ID: `{source.artifact_id}`
- Actions artifact server digest: `{source.artifact_server_digest}`
- Release tag: `{release_tag}`
- Product commit: `{source.source_commit}`
- Chrome ZIP SHA-256: `{chrome_zip.sha256}`
- Chrome ZIP size: `{chrome_zip.size_bytes}`
- Attestation SHA-256: `{attestation_sha256}`
- Review projection source commit: `{projection.source_commit}`
- Review branch head checked before handoff: `{projection.current_commit}`
- Review projection canonical SHA-256: `{projection.digest}`
- Signer ID: `{SIGNER_ID}`
- Signer SPKI SHA-256: `{signer_fingerprint}`
- GitHub App ID: `{identity.app_id}`
- GitHub App installation ID: `{identity.installation_id}`
- [Producer run]({run_url})

The body is audit context, not cryptographic evidence. A `MATCH` result and an accepted evidence record establish producer identity and exact artifact provenance. They do not authorize publication or release.
"""


def command_resolve(args: argparse.Namespace) -> int:
    client = GitHubApi(
        _required_environment("GITHUB_TOKEN"),
        api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    require_current_producer_ref(client)
    source = qualify_windows_package_source(
        client,
        run_id=args.windows_package_run_id,
        release_tag=args.release_tag,
        baseline_sha=load_producer_control_baseline(),
        workflow_sha=_required_environment("PRODUCER_WORKFLOW_SHA"),
    )
    _write_github_outputs(
        {
            "source_commit": source.source_commit,
            "product_ci_run_id": str(source.product_ci_run_id),
            "artifact_id": str(source.artifact_id),
            "artifact_name": source.artifact_name,
            "artifact_server_digest": source.artifact_server_digest,
        }
    )
    print(
        "Resolved qualified Windows package metadata: "
        f"run={source.run_id} artifact_id={source.artifact_id} "
        f"source={source.source_commit}"
    )
    return 0


def build_attestation_document(
    *,
    source: QualifiedSource,
    release: QualifiedStoreRelease,
    projection: ReviewProjection,
    attestation_id: str,
    signed_at: str,
) -> dict[str, Any]:
    """Assemble the attestation from values this run verified for itself.

    The v1 contract defines no member for the bindings, signing rule, privacy
    policy and qualification job this run also verifies, and closes the document
    to members it does not define. Those checks gate the signature rather than
    appear in it, so each one refuses before this is reached.
    """
    chrome_zip = release.chrome_zip
    return {
        "schema_version": "1.0",
        "attestation_id": attestation_id,
        "created_at": signed_at,
        "repository": PRODUCT_REPOSITORY,
        "commit_hash": source.source_commit,
        "workflow": PRODUCER_WORKFLOW,
        "target": TARGET,
        "artifact": {
            "filename": chrome_zip.filename,
            "sha256": release.artifact_sha256,
            "size_bytes": chrome_zip.size_bytes,
            "version": chrome_zip.version,
            "media_type": "application/zip",
        },
        "legal_projection": projection.value,
        "provenance": {
            "algorithm": ALGORITHM,
            "signed_at": signed_at,
            "signer_id": SIGNER_ID,
        },
    }


def _legal_coordinates(args: argparse.Namespace) -> LegalBindingsCoordinates:
    return resolve_legal_bindings_coordinates(
        source_revision=args.legal_repository_revision,
        fetch_revision=args.legal_bindings_repository_revision,
        document_path=args.legal_bindings_path,
        expected_digest=args.legal_bindings_digest,
    )


def command_qualify_package(args: argparse.Namespace) -> int:
    """Audit the exact Store ZIP under the controlled package qualification."""
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    client = GitHubApi(_required_environment("GITHUB_TOKEN"), api_url=api_url)
    require_current_producer_ref(client)
    source = qualify_windows_package_source(
        client,
        run_id=args.windows_package_run_id,
        release_tag=args.release_tag,
        baseline_sha=load_producer_control_baseline(),
        workflow_sha=_required_environment("PRODUCER_WORKFLOW_SHA"),
    )
    coordinates = _legal_coordinates(args)
    with tempfile.TemporaryDirectory(prefix="attestation-qualification-") as temporary:
        temporary_root = Path(temporary)
        actions_archive_path = temporary_root / "windows-package.zip"
        client.download(
            f"{_repo_path(PRODUCT_REPOSITORY)}/actions/artifacts/"
            f"{source.artifact_id}/zip",
            actions_archive_path,
        )
        chrome_zip = qualify_downloaded_artifact(
            actions_archive_path.read_bytes(),
            expected_server_digest=source.artifact_server_digest,
            release_tag=args.release_tag,
        )
        release = qualify_store_release(
            chrome_zip,
            coordinates,
            temporary_root=temporary_root,
            audit_package=True,
        )
        print(
            "Store package qualified: "
            f"artifact={release.chrome_zip.filename} "
            f"artifact_sha256={release.artifact_sha256} "
            f"legal_bindings_digest={release.legal_bindings.digest}"
        )
    return 0


def command_sign_and_handoff(args: argparse.Namespace) -> int:
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    product_client = GitHubApi(_required_environment("GITHUB_TOKEN"), api_url=api_url)
    require_current_producer_ref(product_client)
    baseline_sha = load_producer_control_baseline()
    workflow_sha = _required_environment("PRODUCER_WORKFLOW_SHA")
    source = qualify_windows_package_source(
        product_client,
        run_id=args.windows_package_run_id,
        release_tag=args.release_tag,
        baseline_sha=baseline_sha,
        workflow_sha=workflow_sha,
    )
    review_configuration = load_review_configuration()

    with tempfile.TemporaryDirectory(prefix="engineering-attestation-") as temporary:
        temporary_root = Path(temporary)
        verify_conformance_vector(
            Path(CONFORMANCE_VECTOR_RELATIVE_PATH),
            temporary_root,
        )
        actions_archive_path = temporary_root / "windows-package.zip"
        product_client.download(
            f"{_repo_path(PRODUCT_REPOSITORY)}/actions/artifacts/"
            f"{source.artifact_id}/zip",
            actions_archive_path,
        )
        chrome_zip = qualify_downloaded_artifact(
            actions_archive_path.read_bytes(),
            expected_server_digest=source.artifact_server_digest,
            release_tag=args.release_tag,
        )
        # Every precondition runs before any key material is decoded, so a
        # refusal cannot reach a signature.
        release = qualify_store_release(
            chrome_zip,
            _legal_coordinates(args),
            temporary_root=temporary_root,
            audit_package=False,
        )
        # Called for its refusal. The v1 document records no qualification job,
        # so this gates the signature without appearing in what is signed.
        require_package_qualification(product_client)

        private_key_path = temporary_root / "attestation-private-key.pem"
        app_private_key_path = temporary_root / "review-app-private-key.pem"
        _decode_secret_to_file(
            "ENGINEERING_ATTESTATION_PRIVATE_KEY_B64", private_key_path
        )
        _decode_secret_to_file("REVIEW_APP_PRIVATE_KEY_B64", app_private_key_path)

        public_key_path = Path(PUBLIC_KEY_RELATIVE_PATH)
        fingerprint = validate_signer_material(
            private_key_path,
            public_key_path,
            expected_signer_id=_required_environment(
                "ENGINEERING_ATTESTATION_SIGNER_ID"
            ),
            expected_fingerprint=_required_environment(
                "ENGINEERING_ATTESTATION_SPKI_SHA256"
            ),
        )

        handoff_client, identity = acquire_handoff_client(
            configuration=review_configuration,
            app_id=_positive_integer(
                _required_environment("REVIEW_APP_ID"), "review App ID"
            ),
            installation_id=_positive_integer(
                _required_environment("REVIEW_INSTALLATION_ID"),
                "review App installation ID",
            ),
            expected_bot_user_id=_positive_integer(
                _required_environment("REVIEW_BOT_USER_ID"),
                "review App bot user ID",
            ),
            expected_repository_id=_positive_integer(
                _required_environment("REVIEW_REPOSITORY_ID"),
                "review repository ID",
            ),
            app_private_key_path=app_private_key_path,
            api_url=api_url,
        )

        review_head_before_signing = resolve_branch_head(
            handoff_client,
            review_configuration.repository,
            review_configuration.default_branch,
        )
        projection = validate_review_projection(
            handoff_client,
            configuration=review_configuration,
            source_commit=args.legal_projection_source_commit,
            current_commit=review_head_before_signing,
            expected_digest=args.legal_projection_canonical_sha256,
        )

        require_current_producer_ref(product_client)
        signed_at = _utc_timestamp()
        attestation: dict[str, Any] = build_attestation_document(
            source=source,
            release=release,
            projection=projection,
            attestation_id=(
                f"chrome-extension-{_required_environment('GITHUB_RUN_ID')}-"
                f"{_required_environment('GITHUB_RUN_ATTEMPT')}"
            ),
            signed_at=signed_at,
        )
        payload = attestation_signing_payload(attestation)
        signature = sign_ed25519(payload, private_key_path)
        attestation["provenance"]["signature"] = base64.b64encode(signature).decode(
            "ascii"
        )
        verify_ed25519(payload, signature, public_key_path)
        attestation_bytes = serialize_final_attestation(attestation)
        round_trip = load_json_strict(attestation_bytes, label="final attestation")
        if round_trip != attestation:
            raise ContractError("final attestation serialization did not round-trip")
        attestation_sha256 = sha256_bytes(attestation_bytes)

        pr_number: int | None = None
        for handoff_attempt in range(1, 4):
            review_head_before_pr = resolve_branch_head(
                handoff_client,
                review_configuration.repository,
                review_configuration.default_branch,
            )
            projection = validate_review_projection(
                handoff_client,
                configuration=review_configuration,
                source_commit=args.legal_projection_source_commit,
                current_commit=review_head_before_pr,
                expected_digest=args.legal_projection_canonical_sha256,
            )
            require_current_producer_ref(product_client)

            retire_stale_evidence_prs(
                handoff_client,
                configuration=review_configuration,
                identity=identity,
                current_base_commit=review_head_before_pr,
            )
            try:
                pr_number, _ = create_evidence_pr(
                    handoff_client,
                    configuration=review_configuration,
                    identity=identity,
                    review_base_commit=review_head_before_pr,
                    attestation_bytes=attestation_bytes,
                    artifact_bytes=chrome_zip.bytes,
                    pr_body=_pr_body(
                        source=source,
                        release_tag=args.release_tag,
                        chrome_zip=chrome_zip,
                        projection=projection,
                        attestation_sha256=attestation_sha256,
                        signer_fingerprint=fingerprint,
                        identity=identity,
                    ),
                    workflow_run_id=_positive_integer(
                        _required_environment("GITHUB_RUN_ID"),
                        "GitHub workflow run ID",
                    ),
                    workflow_run_attempt=_positive_integer(
                        _required_environment("GITHUB_RUN_ATTEMPT"),
                        "GitHub workflow run attempt",
                    ),
                )
                break
            except ReviewBaseAdvancedError as exc:
                if handoff_attempt == 3:
                    raise ContractError(
                        "review default branch advanced repeatedly during evidence "
                        "PR creation"
                    ) from exc
                print("Review branch advanced; recreating evidence from its new head")
        if pr_number is None:
            raise ContractError("evidence PR creation produced no pull-request number")
        _write_github_outputs(
            {
                "attestation_sha256": attestation_sha256,
                "artifact_sha256": chrome_zip.sha256,
                "review_pull_request_number": str(pr_number),
            }
        )
        print(
            "Engineering attestation submitted for review: "
            f"attestation_sha256={attestation_sha256} "
            f"artifact_sha256={chrome_zip.sha256} pr_number={pr_number}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in (
        ("resolve-source", command_resolve),
        ("qualify-package", command_qualify_package),
        ("sign-and-handoff", command_sign_and_handoff),
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--windows-package-run-id", required=True, type=int)
        command.add_argument("--release-tag", required=True)
        if name != "resolve-source":
            command.add_argument("--legal-repository-revision", required=True)
            command.add_argument(
                "--legal-bindings-repository-revision", required=True
            )
            command.add_argument("--legal-bindings-path", required=True)
            command.add_argument("--legal-bindings-digest", required=True)
        if name == "sign-and-handoff":
            command.add_argument("--legal-projection-source-commit", required=True)
            command.add_argument(
                "--legal-projection-canonical-sha256", required=True
            )
        command.set_defaults(handler=handler)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except QualificationError as exc:
        print(f"ERROR [{exc.step}]: {exc}", file=sys.stderr)
        return exc.exit_code
    except ContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
