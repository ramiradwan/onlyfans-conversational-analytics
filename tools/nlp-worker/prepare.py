"""Prepare immutable worker sources in a fresh external build directory."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import re
import subprocess

REVISION = "1bcf40e80c114ceb707d172b3efd28b577121b39"
ROOT = Path(__file__).resolve().parents[2]
PREPARER_BYTES = Path(__file__).read_bytes()
PRODUCT_PREFIX = "native/nlp-worker/"
PRODUCT_REQUIRED = {"Cargo.toml", "Cargo.lock", "build.rs", "src/main.rs",
                    "tests/instruction_runtime_lifecycle.cpp"}
UPSTREAM_REQUIRED = {"Cargo.toml", "Cargo.lock", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md",
                     "src/tokenizer.rs", "src/runtime.rs"}


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("upstream source differs")
    return text.replace(old, new, 1)


def git(repository, *arguments):
    try:
        return subprocess.check_output(["git", "--no-replace-objects", "-C", str(repository), *arguments],
                                       stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        raise ValueError("Git source unavailable") from error


def snapshot(repository, revision, prefix, required, directories):
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("full immutable commit required")
    if git(repository, "cat-file", "-t", revision).strip() != b"commit":
        raise ValueError("full immutable commit required")
    records, blobs = [], {}
    for entry in git(repository, "ls-tree", "-rz", revision, "--", prefix or ".").split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        relative = path[len(prefix):] if prefix else path
        selected = relative in required or any(relative.startswith(folder + "/") for folder in directories)
        if not selected:
            continue
        parts = PurePosixPath(relative).parts
        if (not relative or "\\" in relative or PurePosixPath(relative).is_absolute()
                or any(part in (".", "..") or ":" in part for part in parts)
                or mode != "100644" or kind != "blob"):
            raise ValueError("normal source file required")
        if relative.casefold() in {name.casefold() for name in blobs}:
            raise ValueError("aliased source member")
        data = git(repository, "cat-file", "blob", oid)
        blobs[relative] = data
        records.append(dict(path=path, git_blob=oid, mode=mode, bytes=len(data),
                            sha256=hashlib.sha256(data).hexdigest()))
    if not required <= blobs.keys() or any(not any(name.startswith(folder + "/") for name in blobs)
                                           for folder in directories):
        raise ValueError("required source member missing")
    return blobs, sorted(records, key=lambda record: record["path"])


def refuse_alias(path):
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or (ancestor.exists() and getattr(ancestor.stat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("aliased source or target")


def prepare(source: Path, target: Path, product_revision: str):
    source, target = source.absolute(), target.absolute()
    refuse_alias(source)
    refuse_alias(target)
    if target.exists() or target.is_relative_to(ROOT) or target.is_relative_to(source):
        raise ValueError("fresh external target required")
    actual = git(source, "rev-parse", "HEAD").decode().strip()
    if actual != REVISION or git(source, "status", "--porcelain").strip():
        raise ValueError("clean pinned source required")
    product, product_inputs = snapshot(ROOT, product_revision, PRODUCT_PREFIX, PRODUCT_REQUIRED, ("src", "tests"))
    upstream, upstream_inputs = snapshot(source, REVISION, "", UPSTREAM_REQUIRED, ("src",))
    # Patch immutable input bytes before publishing any prepared tree.
    manifest = upstream["Cargo.toml"].decode("utf-8")
    manifest = "\n".join(line for line in manifest.splitlines() if not line.startswith("hf-hub =")) + "\n"
    upstream["Cargo.toml"] = replace_once(manifest, '"download-binaries", "tls-rustls", "copy-dylibs", ', '"load-dynamic", ').encode("utf-8")
    code = upstream["src/tokenizer.rs"].decode("utf-8")
    upstream["src/tokenizer.rs"] = replace_once(code, '        let inner = Tokenizer::from_file(tokenizer_path).map_err(anyhow::Error::msg)?;',
        '        Self::from_file(tokenizer_path)\n    }\n\n    pub fn from_file(path: impl AsRef<Path>) -> Result<Self> {\n        let inner = Tokenizer::from_file(path).map_err(anyhow::Error::msg)?;').encode("utf-8")
    code = upstream["src/runtime.rs"].decode("utf-8")
    old = "        // Only the CPU provider passes validation. ORT registers CPU implicitly,\n        // so no provider registration call is needed and none can fall back."
    new = '        builder = builder.with_execution_providers([ort::ep::CPU::default().with_arena_allocator(false).build().error_on_failure()]).map_err(|error| anyhow!(error.to_string()))?;\n        // Explicit CPU registration has no fallback provider.'
    upstream["src/runtime.rs"] = replace_once(code, old, new).encode("utf-8")
    preparer = PREPARER_BYTES
    if Path(__file__).read_bytes() != preparer:
        raise ValueError("preparer source changed")
    receipt = dict(schema="nlp-worker-build.v2", product_revision=product_revision, upstream_revision=REVISION,
                   inputs=dict(product=product_inputs, upstream=upstream_inputs),
                   preparer=dict(path="tools/nlp-worker/prepare.py", bytes=len(preparer),
                                 sha256=hashlib.sha256(preparer).hexdigest()))
    target.mkdir(parents=True, exist_ok=False)
    for prefix, members in (("", product), ("upstream/", upstream)):
        for name, data in sorted(members.items()):
            destination = target / (prefix + name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                output.write(data)
    files = []
    for path in sorted(target.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            files.append(dict(path=path.relative_to(target).as_posix(), bytes=len(data),
                              sha256=hashlib.sha256(data).hexdigest()))
    receipt["files"] = files
    with (target / "preparation.json").open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--product-revision", required=True)
    args = parser.parse_args()
    prepare(args.source, args.target, args.product_revision)
