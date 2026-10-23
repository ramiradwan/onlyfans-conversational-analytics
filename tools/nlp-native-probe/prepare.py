"""Prepare a pinned, local-only native text-analysis probe outside application builds."""
from pathlib import Path
import argparse
import shutil
import subprocess

REVISION = "1bcf40e80c114ceb707d172b3efd28b577121b39"

def prepare(source: Path, target: Path):
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != REVISION:
        raise ValueError("unexpected source revision")
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip():
        raise ValueError("source checkout must be clean")
    if target.exists():
        raise ValueError("target must not exist")
    upstream = target / "upstream"
    upstream.mkdir(parents=True)
    shutil.copytree(source / "src", upstream / "src")
    for name in ("Cargo.toml", "Cargo.lock", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md"):
        shutil.copy2(source / name, upstream / name)
    manifest = (upstream / "Cargo.toml").read_text(encoding="utf-8")
    manifest = "\n".join(line for line in manifest.splitlines() if not line.startswith("hf-hub =")) + "\n"
    manifest = manifest.replace('"download-binaries", "tls-rustls", "copy-dylibs", ', '"load-dynamic", ')
    (upstream / "Cargo.toml").write_text(manifest, encoding="utf-8")
    path = upstream / "src/boundary/pipeline.rs"
    text = path.read_text(encoding="utf-8")
    for field, typ, graph in (("explicit", "ExplicitModel", "boundary_explicit_scorer.onnx"),
                              ("records", "RecordModel", "boundary_records.onnx"),
                              ("relations", "RelationModel", "boundary_relations.onnx")):
        text = text.replace(f"    {field}: {typ},", f"    {field}: Option<{typ}>,", 1)
        line = f'        let {field} = required_file(bundle, "{graph}")?;\n'
        if text.count(line) != 1:
            raise ValueError("unexpected optional graph constructor")
        text = text.replace(line, "", 1)
        start = text.index(f"            {field}: {typ}::new_with_options(")
        end = text.index("            })?,", start) + len("            })?,")
        text = text[:start] + f"            {field}: None," + text[end:]
        text = text.replace(f"self.{field}.infer(", f'self.{field}.as_ref().context("head unavailable")?.infer(')
    method = """
    pub fn probe_input_tokens(&self, text: &str, schema: &SchemaSpec) -> Result<usize> {
        ensure!(schema.structures.is_empty() && schema.relations.is_empty(), "unsupported schema");
        let labels: Vec<_> = schema.entities.iter().map(|x| x.name.clone()).collect();
        let descriptions: Vec<_> = schema.entities.iter().filter_map(|x| x.description.as_ref().map(|d| (x.name.clone(), d.clone()))).collect();
        let mut tokens = vec![build_entities_schema_tokens_with_descriptions(&labels, None, &descriptions)];
        for classification in &schema.classifications {
            tokens.push(build_classification_schema_tokens_with_descriptions(&classification.task, &classification.labels,
                classification.prompt.as_deref(), &classification.label_descriptions));
        }
        let prepared = self.preprocessing.prepare(text, &[]);
        ensure!(prepared.truncated_words == 0, "source truncated");
        Ok(format_input_with_mapping(&self.tokenizer, &tokens, &prepared.text_tokens)?.input_ids.len())
    }

"""
    text = text.replace("    fn encode_schema(", method + "    fn encode_schema(", 1)
    path.write_text(text, encoding="utf-8")
    shutil.copy2(Path(__file__).with_name("Cargo.lock"), target / "Cargo.lock")
    runtime = upstream / "src/runtime.rs"
    code = runtime.read_text(encoding="utf-8")
    code = code.replace("        // Only the CPU provider passes validation.",
                        "        builder = builder.with_execution_providers([ort::ep::CPU::default().with_arena_allocator(false).build().error_on_failure()]).map_err(|error| anyhow!(error.to_string()))?;\n        // Only the CPU provider passes validation.", 1)
    code = code.replace("        // Only the CPU provider passes validation. ORT registers CPU implicitly,\n        // so no provider registration call is needed and none can fall back.",
                        "        // Explicit CPU registration has no fallback provider.")
    runtime.write_text(code, encoding="utf-8")
    (target / "src").mkdir()
    shutil.copy2(Path(__file__).with_name("main.rs"), target / "src/main.rs")
    (target / "Cargo.toml").write_text('''[package]
name = "local-text-probe"
version = "0.1.0"
edition = "2024"

[dependencies]
anyhow = "1"
gliner2-rs = { path = "upstream" }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
ort = { version = "=2.0.0-rc.13", default-features = false, features = ["std", "load-dynamic", "api-27"] }

[profile.release]
lto = "thin"
codegen-units = 1
strip = true
''')

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.source, args.target)
