"""Create a labelled, build-only public-fixture overlay outside the checkout.

Production packaging has no fixture switches. This tool changes a copied source
tree before either artifact is built, records every changed public input, and
never copies the admission/publication producers into the frozen consumer.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ORIGIN = "https://updates.dipsy.fi"
FIXTURE_ORIGIN = "http://127.0.0.1:17873"
PRODUCTION_TRUST = "9a0965a3684afae9b4745d9c36845abb2f0a168ce3b1da3256567720542873c9"
SCOPE = "qualification-only-public-fixture-unchanged-background/v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def source_receipts(output):
    paths = [
        "app/packaged_entry.py", "app/security/signing-publication-trust.json",
        "app/core/signing-parameter-release.json", "app/security/signing_parameter_publications.py",
        "app/services/signing_parameters.py", "extension/build.mjs",
        "extension/runtime/companion-client.mjs", "extension/runtime/signing-publication-trust.mjs",
        "packaging/pyinstaller/brain.spec",
    ]
    result = {name: {"source_sha256": digest((ROOT / name).read_bytes()),
                     "overlay_sha256": digest((output / name).read_bytes())} for name in paths}
    for name in ("fixture_runtime.py", "fixture_installation.py"):
        result[name] = {"source_path": "tools/signing-parameter-qualification/" + name,
                        "source_sha256": digest((ROOT / "tools/signing-parameter-qualification" / name).read_bytes()),
                        "overlay_sha256": digest((output / name).read_bytes())}
    return result


def runtime_receipt(runtime_python):
    code = (
        "import hashlib,importlib.util,json,platform; from pathlib import Path; "
        "names=('ofca_native_snow.ofca_native_snow','sqlcipher3._sqlite3'); "
        "print(json.dumps({'python':platform.python_version(),'modules':"
        "{name:hashlib.sha256(Path(importlib.util.find_spec(name).origin).read_bytes()).hexdigest() for name in names}}))"
    )
    report = json.loads(subprocess.run([str(runtime_python), "-X", "utf8", "-c", code], check=True, capture_output=True).stdout)
    report["fixed_runtime_sources_sha256"] = digest((ROOT / "packaging/sqlcipher/fixed-runtime-sources.json").read_bytes())
    return report


def create_overlay(output: Path, publication_trust: Path, runtime_python: Path):
    output = output.resolve()
    if output == ROOT or ROOT in output.parents or output.exists():
        raise ValueError("overlay must be a new directory outside the checkout")
    trust = json.loads(publication_trust.read_text(encoding="utf-8"))
    raw_trust = canonical(trust)
    trust_hash = digest(raw_trust)
    if trust_hash == PRODUCTION_TRUST:
        raise ValueError("qualification overlay requires distinct public fixture trust")
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout.decode("utf-8").split("\0")
    output.mkdir(parents=True)
    for name in files:
        if not name or name.startswith("tools/signing-parameter-qualification/"):
            continue
        source, target = ROOT / name, output / name
        if not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    helper_hashes = {}
    for name in ("fixture_runtime.py", "fixture_installation.py"):
        shutil.copyfile(ROOT / "tools/signing-parameter-qualification" / name, output / name)
        helper_hashes[name] = digest((output / name).read_bytes())
    # The normal frozen dependency collection/spec is retained. Only its entry
    # selects the explicitly labelled fixture composition before normal app.main.
    (output / "app/packaged_entry.py").write_text(
        '"""QUALIFICATION ONLY: public synthetic admission; never distribute."""\n'
        'from fixture_runtime import main\nif __name__ == "__main__":\n    main()\n', encoding="utf-8")
    changed = {}

    def replace(name, old, new, *, minimum=1):
        target = output / name
        before = target.read_text(encoding="utf-8")
        count = before.count(old)
        if count < minimum:
            raise ValueError(f"qualification overlay source changed: {name}")
        target.write_text(before.replace(old, new), encoding="utf-8", newline="\n")
        changed[name] = {"source_sha256": digest((ROOT / name).read_bytes()), "overlay_sha256": digest(target.read_bytes())}

    (output / "app/security/signing-publication-trust.json").write_bytes(raw_trust + b"\n")
    release = {"schema": "ofca-signing-parameter-release/v1", "origin": FIXTURE_ORIGIN,
               "base_path": "/signing-parameters/v1/", "trust_sha256": trust_hash}
    (output / "app/core/signing-parameter-release.json").write_bytes(canonical(release) + b"\n")
    replace("app/security/signing_parameter_publications.py", PRODUCTION_TRUST, trust_hash)
    replace("app/services/signing_parameters.py", PRODUCTION_ORIGIN, FIXTURE_ORIGIN)
    replace("extension/build.mjs", PRODUCTION_ORIGIN, FIXTURE_ORIGIN)
    replace("extension/build.mjs", "../contracts/production/grant-profile-v1/trust-set.json",
            "../contracts/grant-profile-v1/keys/trust-set.json")
    replace("extension/runtime/companion-client.mjs",
            "loadGrantTrustSet(await (await fetch(chromeApi.runtime.getURL('companion-grant-trust.json'))).json())",
            "loadGrantTrustSet(await (await fetch(chromeApi.runtime.getURL('companion-grant-trust.json'))).json(), { allowNonProduction: true })")
    replace("packaging/pyinstaller/brain.spec", "console=False", "console=True")
    module = output / "extension/runtime/signing-publication-trust.mjs"
    module.write_text('// QUALIFICATION ONLY: public synthetic trust compiled before build.\n'
                      f"export const SIGNING_PUBLICATION_TRUST_B64 = '{base64.b64encode(raw_trust).decode('ascii')}';\n"
                      f"export const SIGNING_PUBLICATION_TRUST_SHA256 = '{trust_hash}';\n"
                      'export const signingPublicationTrust = Object.freeze(JSON.parse(atob(SIGNING_PUBLICATION_TRUST_B64)));\n', encoding="utf-8")
    # Assets are generated prerequisites, never credentials or runtime state.
    shutil.copytree(ROOT / "app/static/dist", output / "app/static/dist", dirs_exist_ok=True)
    shutil.copytree(ROOT / "extension/node_modules", output / "extension/node_modules")
    receipt = {"schema": "ofca-signing-parameter-qualification-overlay/v1", "scope": SCOPE,
               "production_usable": False, "origin": FIXTURE_ORIGIN, "publication_trust_sha256": trust_hash,
               "shipping_background_entry": "background-read-only.js", "patches": source_receipts(output),
               "qualification_helpers_sha256": helper_hashes,
               "runtime_provenance": runtime_receipt(runtime_python),
               "composition_substitutions": ["fixed compiled public synthetic publication trust and loopback origin",
                    "manifest-pinned public commercial fixture trust projection for six activation checks",
                    "public software installation-provider simulation; no TPM qualification",
                    "pre-admitted durable local operator; no WebAuthn ceremony qualification",
                    "qualification stdin entry with normal app.main lifecycle; console bootloader"],
               "producer_modules_in_consumer": False}
    (output / "qualification-overlay.json").write_bytes(canonical(receipt) + b"\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--publication-trust", required=True, type=Path)
    parser.add_argument("--runtime-python", required=True, type=Path)
    args = parser.parse_args()
    create_overlay(args.output, args.publication_trust, args.runtime_python)
