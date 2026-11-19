"""Exact-input, attested CI-only Windows SQLCipher wheel reuse.

A cache is only a transport. A restored manifest must have a GitHub build
provenance signature from the protected main CI workflow before any cached
wheel bytes enter the current run's wheelhouse. Release packaging builds fresh.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPO = "ramiradwan/onlyfans-conversational-analytics"
SIGNER = REPO + "/.github/workflows/sqlcipher-cache-publish.yml"
MANIFEST = "wheel-cache-manifest.json"
SOURCE_FILE = "packaging/sqlcipher/fixed-runtime-sources.json"
INPUT_FILES = (
    SOURCE_FILE,
    "packaging/sqlcipher/build-fixed-wheel.py",
    "packaging/sqlcipher/build-fixed-wheel.ps1",
    "THIRD_PARTY_NOTICES.md",
    "tools/ci_sqlcipher_cache.py",
)
LICENSES = ("binding/LICENSE", "openssl/LICENSE.txt", "sqlcipher/LICENSE.md",
            "sqlcipher/LICENSE.txt", "sqlcipher/SQLITE_LICENSE.md")


class CacheError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise CacheError(message)


def hex_digest(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{%d}" % length, value) is not None


def canonical(document):
    return (json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def read_json(path):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    require(path.is_file() and path.stat().st_size <= 1024 * 1024, "missing or oversized JSON")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def safe_entry(path):
    metadata = path.lstat()
    require(not path.is_symlink() and not getattr(metadata, "st_file_attributes", 0) & 0x400,
            "linked cache entry")
    return metadata


def digest(path):
    metadata = safe_entry(path)
    require(stat.S_ISREG(metadata.st_mode), "non-regular cache file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def capture_inputs(root=ROOT):
    require(os.name == "nt" and sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 11),
            "unsupported wheel cache build interpreter")
    environment = {name: os.environ.get(name, "") for name in ("ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH")}
    require(environment["RUNNER_OS"] == "Windows" and environment["RUNNER_ARCH"] == "X64"
            and all(environment.values()), "missing hosted image identity")
    spec = importlib.util.spec_from_file_location("fixed_wheel_builder", root / INPUT_FILES[1])
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return {"schema": 1, "files": {name: digest(root / name) for name in INPUT_FILES},
            "runner": environment, "python_version": sys.version.strip(), "python_tag": "cp311",
            "msvc_compiler_version": builder._msvc_compiler_version(builder._vcvars64())}


def fingerprint(inputs):
    return hashlib.sha256(canonical(inputs)).hexdigest()


def payload_names(sources, inputs):
    version = sources["binding"]["local_version"]
    tag = inputs["python_tag"]
    require(re.fullmatch(r"[0-9.]+\+ofca\.[0-9]+", version) is not None and tag == "cp311", "unsupported wheel identity")
    base = "sqlcipher3-" + version
    return {f"{base}-{tag}-{tag}-win_amd64.whl", f"{base}.provenance.json",
            *(f"{base}.licenses/{name}" for name in LICENSES)}


def inventory(directory):
    require(stat.S_ISDIR(safe_entry(directory).st_mode), "missing wheel cache directory")
    files = set()
    for path in directory.rglob("*"):
        metadata = safe_entry(path)
        if stat.S_ISREG(metadata.st_mode):
            files.add(path.relative_to(directory).as_posix())
        else:
            require(stat.S_ISDIR(metadata.st_mode), "unsupported cache entry")
    return files


def validate_payload(directory, inputs, sources, *, sealed=False):
    names = payload_names(sources, inputs)
    require(inventory(directory) == names | ({MANIFEST} if sealed else set()), "wheelhouse file inventory differs")
    hashes = {name: digest(directory / name) for name in sorted(names)}
    provenance_name = next(name for name in names if name.endswith(".provenance.json"))
    wheel = next(name for name in names if name.endswith(".whl"))
    provenance = read_json(directory / provenance_name)
    require(provenance.get("schema_version") == 1 and provenance.get("wheel") == {"filename": wheel, "sha256": hashes[wheel]},
            "wheel digest differs from build provenance")
    source_proof = provenance.get("sources", {})
    require(set(source_proof) == set(sources) | {"generated_amalgamation_sha256"}
            and all(source_proof[name] == value for name, value in sources.items())
            and hex_digest(source_proof.get("generated_amalgamation_sha256")), "wheel sources differ")
    build = provenance.get("build", {})
    require(build.get("target_python_version") == build.get("builder_python_version") == inputs["python_version"]
            and build.get("msvc_compiler_version") == inputs["msvc_compiler_version"]
            and build.get("platform") == "nt"
            and build.get("openssl_linkage") == "static /MD; dumpbin found no libcrypto/libssl DLL dependency"
            and build.get("vcpkg_openssl", {}).get("triplet") == sources["vcpkg"]["triplet"]
            and build.get("vcpkg_openssl", {}).get("port") == sources["vcpkg"]["port"], "wheel build identity differs")
    require(provenance.get("licenses_retained") == {name: hashes[name] for name in names if ".licenses/" in name},
            "retained license digests differ")
    return hashes


def seal(directory, inputs, sources, producer):
    require(set(producer) == {"source_commit", "workflow_run_id", "workflow_run_attempt"}
            and hex_digest(producer["source_commit"], 40)
            and all(type(producer[name]) is int and producer[name] > 0 for name in ("workflow_run_id", "workflow_run_attempt")),
            "invalid build producer")
    manifest = {"schema": 1, "inputs": inputs, "input_sha256": fingerprint(inputs), "producer": producer,
                "files": validate_payload(directory, inputs, sources)}
    (directory / MANIFEST).write_bytes(canonical(manifest))
    return manifest


def verify_attestation(path, source):
    # The cache file is signed by the protected main CI workflow, not the PR.
    # Bind the claimed build commit to both source and signing identity.
    result = subprocess.run(["gh", "attestation", "verify", str(path), "--repo", REPO,
        "--signer-workflow", SIGNER, "--source-ref", "refs/heads/main", "--source-digest", source,
        "--signer-digest", source, "--deny-self-hosted-runners"], capture_output=True, timeout=90)
    require(result.returncode == 0, "trusted main attestation unavailable or invalid")


def restore(cache, destination, inputs, sources, *, verifier=verify_attestation):
    require(not destination.exists(), "wheelhouse already exists")
    require(MANIFEST in inventory(cache), "cache manifest missing")
    manifest = read_json(cache / MANIFEST)
    require(set(manifest) == {"schema", "inputs", "input_sha256", "producer", "files"}
            and type(manifest["schema"]) is int and manifest["schema"] == 1
            and canonical(manifest["inputs"]) == canonical(inputs)
            and manifest["input_sha256"] == fingerprint(inputs), "cache inputs differ")
    producer = manifest["producer"]
    require(isinstance(producer, dict) and set(producer) == {"source_commit", "workflow_run_id", "workflow_run_attempt"}
            and hex_digest(producer["source_commit"], 40)
            and all(type(producer[name]) is int and producer[name] > 0 for name in ("workflow_run_id", "workflow_run_attempt")),
            "invalid cached producer")
    verifier(cache / MANIFEST, producer["source_commit"])
    require(manifest["files"] == validate_payload(cache, inputs, sources, sealed=True), "cached bytes differ")
    # Never copy unchecked paths. Recheck the copied bytes before atomic publish.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ofca-wheel-restore-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "wheelhouse"
        for name in sorted(set(manifest["files"]) | {MANIFEST}):
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cache / name, target)
        require((stage / MANIFEST).read_bytes() == canonical(manifest)
                and manifest["files"] == validate_payload(stage, inputs, sources, sealed=True), "copied bytes differ")
        stage.rename(destination)
    return manifest


def stage_publication(artifact, destination, sources, expected, *, root=ROOT):
    """Stage only a fresh, exact-source wheel from a qualified main run.

    A reused wheel has its original producer in the sealed manifest and is
    deliberately not eligible for publication again. The cache transport
    excludes the per-run envelope; the signed manifest binds all payload bytes.
    """
    require(not destination.exists(), "publication destination already exists")
    require(read_json(artifact / "ci-source.json") == {"schema_version": 1, **expected}, "artifact producer differs")
    manifest = read_json(artifact / MANIFEST)
    require(set(manifest) == {"schema", "inputs", "input_sha256", "producer", "files"}
            and type(manifest["schema"]) is int and manifest["schema"] == 1
            and manifest["producer"] == expected, "not a fresh exact-source wheel")
    inputs = manifest["inputs"]
    require(set(inputs) == {"schema", "files", "runner", "python_version", "python_tag", "msvc_compiler_version"}
            and type(inputs["schema"]) is int and inputs["schema"] == 1
            and inputs["files"] == {name: digest(root / name) for name in INPUT_FILES}
            and set(inputs["runner"]) == {"ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH"}
            and inputs["runner"]["RUNNER_OS"] == "Windows" and inputs["runner"]["RUNNER_ARCH"] == "X64"
            and all(isinstance(v, str) and v for v in inputs["runner"].values())
            and all(isinstance(inputs[k], str) and inputs[k] for k in ("python_version", "msvc_compiler_version"))
            and manifest["input_sha256"] == fingerprint(inputs), "publication inputs differ")
    names = payload_names(sources, inputs)
    require(inventory(artifact) == names | {MANIFEST, "ci-source.json"}, "publication artifact inventory differs")
    require((artifact / MANIFEST).read_bytes() == canonical(manifest), "publication manifest encoding differs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ofca-wheel-publish-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "wheelhouse"
        for name in sorted(names | {MANIFEST}):
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact / name, target)
        require((stage / MANIFEST).read_bytes() == canonical(manifest)
                and manifest["files"] == validate_payload(stage, inputs, sources, sealed=True), "publication bytes differ")
        stage.rename(destination)
    return manifest["input_sha256"]


def publish_from_qualified_ci(artifact, destination):
    # This entry point is used only by the protected-main workflow_run job.
    # Re-query the complete source-versioned CI contract and latest actual
    # producer instead of trusting event payloads or artifact names alone.
    sys.path.insert(0, str(ROOT))
    from tools.engineering_attestation import GitHubApi, qualify_product_ci_source, latest_ci_jobs, ContractError
    try:
        source = os.environ["GITHUB_SHA"]
        run_id = int(os.environ["UPSTREAM_RUN_ID"])
        attempt = int(os.environ["UPSTREAM_RUN_ATTEMPT"])
        require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_run" and os.environ.get("GITHUB_REF") == "refs/heads/main"
                and os.environ.get("GITHUB_REF_PROTECTED") == "true"
                and os.environ.get("GITHUB_REPOSITORY") == REPO and source == os.environ["UPSTREAM_SOURCE"], "publication requires exact protected main source")
        api = GitHubApi(os.environ["GH_TOKEN"])
        require(qualify_product_ci_source(api, source_commit=source) == run_id, "publication requires qualified main CI")
        run = api.get(f"/repos/{REPO}/actions/runs/{run_id}")
        require(run.get("run_attempt") == attempt and run.get("head_sha") == source, "upstream run changed")
        producer = latest_ci_jobs(api, run_id=run_id, run_attempt=attempt, source_commit=source)["fixed-sqlcipher-wheel"]
        require(producer.get("conclusion") == "success", "wheel producer did not pass")
        expected = {"source_commit": source, "workflow_run_id": run_id, "workflow_run_attempt": producer["run_attempt"]}
        return stage_publication(artifact, destination, read_json(ROOT / SOURCE_FILE), expected)
    except ContractError as error:
        raise CacheError("upstream CI qualification refused") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inputs", "seal", "restore", "publish-stage"))
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "publish-stage":
            require(args.artifact is not None and args.cache is not None, "publication paths required")
            print(publish_from_qualified_ci(args.artifact, args.cache))
            return 0
        require(args.inputs is not None, "inputs path required")
        if args.action == "inputs":
            inputs = capture_inputs()
            args.inputs.write_bytes(canonical(inputs))
            print(fingerprint(inputs))
        else:
            require(args.wheelhouse is not None, "wheelhouse required")
            inputs, sources = read_json(args.inputs), read_json(ROOT / SOURCE_FILE)
            require(canonical(inputs) == canonical(capture_inputs()), "current build inputs changed")
            if args.action == "seal":
                producer = {"source_commit": os.environ["PRODUCT_SHA"], "workflow_run_id": int(os.environ["GITHUB_RUN_ID"]),
                            "workflow_run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])}
                seal(args.wheelhouse, inputs, sources, producer)
            else:
                require(args.cache is not None, "cache directory required")
                restore(args.cache, args.wheelhouse, inputs, sources)
        return 0
    except (CacheError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("fixed_sqlcipher_cache_refused", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
