"""Prepare the pinned optional worker in a fresh external build directory."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess

REVISION = "1bcf40e80c114ceb707d172b3efd28b577121b39"
ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native/nlp-worker"

def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("upstream source differs")
    return text.replace(old, new, 1)

def prepare(source: Path, target: Path):
    source, target = source.resolve(), target.absolute()
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != REVISION or subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip():
        raise ValueError("clean pinned source required")
    if target.exists() or target.is_relative_to(ROOT) or target.is_relative_to(source):
        raise ValueError("fresh external target required")
    for ancestor in (source, *source.parents, target.parent, *target.parent.parents):
        if ancestor.is_symlink() or getattr(ancestor.stat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("aliased source or target")
    upstream = target / "upstream"
    upstream.mkdir(parents=True)
    shutil.copytree(source / "src", upstream / "src")
    for name in ("Cargo.toml", "Cargo.lock", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md"):
        shutil.copyfile(source / name, upstream / name)
    manifest = (upstream / "Cargo.toml").read_text(encoding="utf-8")
    manifest = "\n".join(line for line in manifest.splitlines() if not line.startswith("hf-hub =")) + "\n"
    manifest = replace_once(manifest, '"download-binaries", "tls-rustls", "copy-dylibs", ', '"load-dynamic", ')
    (upstream / "Cargo.toml").write_text(manifest, encoding="utf-8", newline="\n")
    tokenizer = upstream / "src/tokenizer.rs"
    code = tokenizer.read_text(encoding="utf-8")
    code = replace_once(code, '        let inner = Tokenizer::from_file(tokenizer_path).map_err(anyhow::Error::msg)?;',
                        '        Self::from_file(tokenizer_path)\n    }\n\n    pub fn from_file(path: impl AsRef<Path>) -> Result<Self> {\n        let inner = Tokenizer::from_file(path).map_err(anyhow::Error::msg)?;')
    tokenizer.write_text(code, encoding="utf-8", newline="\n")
    runtime = upstream / "src/runtime.rs"
    code = runtime.read_text(encoding="utf-8")
    old = "        // Only the CPU provider passes validation. ORT registers CPU implicitly,\n        // so no provider registration call is needed and none can fall back."
    new = '        builder = builder.with_execution_providers([ort::ep::CPU::default().with_arena_allocator(false).build().error_on_failure()]).map_err(|error| anyhow!(error.to_string()))?;\n        // Explicit CPU registration has no fallback provider.'
    runtime.write_text(replace_once(code, old, new), encoding="utf-8", newline="\n")
    shutil.copytree(SOURCE / "src", target / "src")
    for name in ("Cargo.toml", "Cargo.lock"):
        shutil.copyfile(SOURCE / name, target / name)
    def pin(path):
        data = path.read_bytes()
        return {"path": path.relative_to(target).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    files = [pin(p) for p in sorted(target.rglob("*")) if p.is_file()]
    (target / "preparation.json").write_text(json.dumps({"schema": "nlp-worker-build.v1", "upstream_revision": REVISION, "files": files}, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.source, args.target)
