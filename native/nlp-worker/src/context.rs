//! Owner-bound descriptors; wire messages cannot choose executable or data paths.
use crate::wire::{self, canonical, hash, id, object};
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    path::PathBuf,
};
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    pub artifact_id: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub role: String,
    pub format_id: String,
}
impl Artifact {
    pub fn validate(&self) -> Result<()> {
        id(&json!(self.artifact_id))?;
        id(&json!(self.format_id))?;
        ensure!(
            wire::hex(&self.sha256, 64)
                && self.size_bytes > 0
                && self.size_bytes <= 9_007_199_254_740_991
                && ["engine", "runtime", "weights", "tokenizer", "configuration"]
                    .contains(&self.role.as_str()),
            "artifact_mismatch"
        );
        Ok(())
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OwnerArtifact {
    pub artifact: Artifact,
    pub handle: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Owner {
    pub schema: String,
    pub channel_nonce: String,
    pub profile_json: String,
    pub engine: Value,
    pub adapters: Vec<Value>,
    pub artifacts: Vec<OwnerArtifact>,
}
pub struct BoundFile {
    pub artifact: Artifact,
    pub _file: File,
    pub path: PathBuf,
}
pub struct Context {
    pub nonce: String,
    pub profile_json: String,
    pub profile: Value,
    pub profile_sha: String,
    pub engine: Value,
    pub adapters: Vec<Value>,
    pub files: BTreeMap<String, BoundFile>,
}
pub const ADAPTER: &str = "boundary_entities";
pub fn supported_adapter(v: &Value) -> Result<()> {
    object(
        v,
        &[
            "adapter_id",
            "adapter_version",
            "adapter_build_sha256",
            "abi",
            "architecture",
            "artifact_format",
            "tokenizer_format",
        ],
    )?;
    ensure!(
        v["adapter_id"] == ADAPTER
            && v["adapter_version"] == "1"
            && v["abi"] == "nlp-adapter.v1"
            && v["architecture"] == "boundary-shared-v1"
            && v["artifact_format"] == "onnx-boundary-entity.v1"
            && v["tokenizer_format"] == "huggingface-tokenizer-json.v1"
            && v["adapter_build_sha256"]
                .as_str()
                .is_some_and(|s| wire::hex(s, 64)),
        "incompatible"
    );
    Ok(())
}
fn policy(v: &Value) -> Result<()> {
    object(v, &["policy_id", "revision", "sha256"])?;
    id(&v["policy_id"])?;
    id(&v["revision"])?;
    ensure!(
        v["sha256"].as_str().is_some_and(|s| wire::hex(s, 64)),
        "invalid_payload"
    );
    Ok(())
}
fn array(v: &Value) -> Result<&Vec<Value>> {
    v.as_array()
        .ok_or_else(|| anyhow::anyhow!("invalid_payload"))
}
pub fn integer(v: &Value, min: u64, max: u64) -> Result<u64> {
    let n = v
        .as_u64()
        .ok_or_else(|| anyhow::anyhow!("invalid_payload"))?;
    ensure!((min..=max).contains(&n), "resource_limit");
    Ok(n)
}
pub fn capability(v: &Value) -> Result<()> {
    object(
        v,
        &[
            "adapter",
            "task_contracts",
            "score_kinds",
            "max_source_codepoints",
            "max_batch_items",
            "max_windows",
            "max_candidates",
            "max_output_findings",
            "runtime_requirements_sha256",
        ],
    )?;
    supported_adapter(&v["adapter"])?;
    ensure!(
        v["task_contracts"] == json!(["concept_mentions.v1"]),
        "incompatible"
    );
    let scores = array(&v["score_kinds"])?;
    ensure!(
        !scores.is_empty()
            && scores.len() <= 2
            && scores.iter().all(|x| x == "none" || x == "probability")
            && scores
                .iter()
                .map(Value::as_str)
                .collect::<BTreeSet<_>>()
                .len()
                == scores.len(),
        "incompatible"
    );
    for (key, max) in [
        ("max_source_codepoints", 131072),
        ("max_batch_items", 16),
        ("max_windows", 1),
        ("max_candidates", 192),
        ("max_output_findings", 512),
    ] {
        integer(&v[key], 1, max)?;
    }
    ensure!(
        v["runtime_requirements_sha256"]
            .as_str()
            .is_some_and(|s| wire::hex(s, 64)),
        "incompatible"
    );
    Ok(())
}
pub fn validate_profile(v: &Value) -> Result<()> {
    object(
        v,
        &[
            "canonicalization",
            "schema_version",
            "profile_id",
            "revision",
            "components",
            "bindings",
            "artifacts",
            "aggregation",
            "presentation",
            "resources",
            "execution_mode",
        ],
    )?;
    ensure!(
        v["canonicalization"] == "nlp-canonical.v1"
            && ["nlp-profile.v2", "nlp-profile.v3"]
                .iter()
                .any(|s| v["schema_version"] == *s)
            && v["execution_mode"] == "serial_unload",
        "invalid_payload"
    );
    id(&v["profile_id"])?;
    id(&v["revision"])?;
    policy(&v["aggregation"])?;
    policy(&v["presentation"])?;
    object(
        &v["resources"],
        &[
            "download_bytes",
            "installed_bytes",
            "rollback_bytes",
            "temporary_bytes",
            "transition_peak_memory_bytes",
            "inference_threads",
        ],
    )?;
    for (key, value) in v["resources"].as_object().unwrap() {
        integer(
            value,
            if key == "rollback_bytes" || key == "temporary_bytes" {
                0
            } else {
                1
            },
            9_007_199_254_740_991,
        )?;
    }
    ensure!(v["resources"]["inference_threads"] == 2, "resource_limit");
    let components = array(&v["components"])?;
    ensure!(
        !components.is_empty() && components.len() <= 2,
        "invalid_payload"
    );
    let mut names = BTreeSet::new();
    let mut pairs = BTreeSet::new();
    for c in components {
        object(
            c,
            &[
                "canonicalization",
                "component_id",
                "engine",
                "adapter",
                "pack_id",
                "pack_revision",
                "pack_sha256",
                "model_revision",
                "artifact_ids",
                "artifact_closure_sha256",
                "tokenizer_sha256",
                "runtime_requirements",
                "tasks",
                "executed_labels",
                "preprocessing",
                "windowing",
                "decoding",
                "output_mapping",
                "decision",
                "language",
                "max_source_codepoints",
                "max_batch_items",
                "max_windows",
                "max_candidates",
                "max_output_findings",
                "peak_memory_bytes",
                "inference_threads",
            ],
        )?;
        ensure!(
            c["canonicalization"] == "nlp-canonical.v1"
                && c["language"] == "en"
                && c["inference_threads"] == 2,
            "incompatible"
        );
        ensure!(names.insert(id(&c["component_id"])?), "invalid_payload");
        for k in [
            "runtime_requirements",
            "preprocessing",
            "windowing",
            "decoding",
            "output_mapping",
            "decision",
        ] {
            policy(&c[k])?;
        }
        let tasks = array(&c["tasks"])?;
        ensure!(!tasks.is_empty() && tasks.len() <= 2, "invalid_payload");
        for task in tasks {
            object(
                task,
                &[
                    "task",
                    "task_contract",
                    "taxonomy",
                    "enabled_ids",
                    "score_policy",
                ],
            )?;
            policy(&task["taxonomy"])?;
            object(
                &task["score_policy"],
                &["kind", "semantics_id", "calibration", "calibration_sha256"],
            )?;
            ensure!(
                pairs.insert((id(&c["component_id"])?, id(&task["task"])?)),
                "invalid_payload"
            );
        }
        let labels = array(&c["executed_labels"])?;
        ensure!(labels.len() <= 128, "invalid_payload");
        for l in labels {
            object(l, &["task", "label_id", "description"])?;
            id(&l["task"])?;
            id(&l["label_id"])?;
            ensure!(
                l["description"]
                    .as_str()
                    .is_some_and(|x| x.chars().count() <= 2048),
                "invalid_payload"
            );
        }
    }
    let bindings = array(&v["bindings"])?;
    ensure!(
        !bindings.is_empty() && bindings.len() <= 2,
        "invalid_payload"
    );
    let mut bp = BTreeSet::new();
    for b in bindings {
        object(b, &["task", "component_id"])?;
        ensure!(
            bp.insert((id(&b["component_id"])?, id(&b["task"])?)),
            "invalid_payload"
        );
    }
    ensure!(pairs == bp, "binding_mismatch");
    if v["schema_version"] == "nlp-profile.v2" {
        ensure!(
            bindings.len() == 2
                && bindings.iter().any(|b| b["task"] == "message_topics")
                && bindings.iter().any(|b| b["task"] == "concept_mentions"),
            "binding_mismatch"
        );
    }
    Ok(())
}
impl Context {
    pub fn bind(owner: Owner, files: Vec<BoundFile>) -> Result<Self> {
        ensure!(
            owner.schema == "nlp-owner-context.v1" && wire::hex(&owner.channel_nonce, 32),
            "context_mismatch"
        );
        let profile = wire::parse(owner.profile_json.as_bytes())?;
        validate_profile(&profile)?;
        ensure!(
            canonical(&profile)? == owner.profile_json,
            "context_mismatch"
        );
        object(
            &owner.engine,
            &[
                "package_name",
                "package_family",
                "core_package_name",
                "core_package_version",
                "publisher",
                "package_version",
                "related_set_id",
                "engine_version",
                "platform",
                "worker_protocol",
                "engine_build_sha256",
            ],
        )?;
        ensure!(
            owner.engine["platform"] == "windows-x64"
                && owner.engine["worker_protocol"] == "nlp-worker.v2",
            "incompatible"
        );
        ensure!(
            !owner.adapters.is_empty() && owner.adapters.len() <= 16,
            "incompatible"
        );
        for cap in &owner.adapters {
            capability(cap)?;
            ensure!(
                cap["adapter"]["adapter_build_sha256"] == owner.engine["engine_build_sha256"],
                "context_mismatch"
            );
        }
        let artifacts: Vec<Artifact> = serde_json::from_value(profile["artifacts"].clone())?;
        ensure!(
            !artifacts.is_empty()
                && artifacts.len() <= 256
                && artifacts
                    == owner
                        .artifacts
                        .iter()
                        .map(|a| a.artifact.clone())
                        .collect::<Vec<_>>(),
            "artifact_mismatch"
        );
        let mut unique = std::collections::BTreeSet::new();
        let mut installed = 0u64;
        for artifact in &artifacts {
            artifact.validate()?;
            ensure!(unique.insert(&artifact.artifact_id), "artifact_mismatch");
            installed = installed
                .checked_add(artifact.size_bytes)
                .ok_or_else(|| anyhow::anyhow!("resource_limit"))?;
        }
        ensure!(
            installed
                <= integer(
                    &profile["resources"]["installed_bytes"],
                    1,
                    9_007_199_254_740_991
                )?,
            "resource_limit"
        );
        for c in array(&profile["components"])? {
            let ids = array(&c["artifact_ids"])?;
            ensure!(!ids.is_empty() && ids.len() <= 128, "artifact_mismatch");
            let mut unique_ids = BTreeSet::new();
            for name in ids {
                ensure!(
                    unique_ids.insert(id(name)?)
                        && artifacts.iter().any(|a| name == &a.artifact_id),
                    "artifact_mismatch"
                );
            }
            let mut closure = artifacts
                .iter()
                .filter(|a| {
                    a.role == "engine"
                        || a.role == "runtime"
                        || ids.iter().any(|name| name == &a.artifact_id)
                })
                .cloned()
                .collect::<Vec<_>>();
            closure.sort_by(|a, b| a.artifact_id.cmp(&b.artifact_id));
            ensure!(
                c["artifact_closure_sha256"]
                    == hash(canonical(&serde_json::to_value(&closure)?)?.as_bytes()),
                "artifact_mismatch"
            );
            let tokenizers = closure
                .iter()
                .filter(|a| a.role == "tokenizer")
                .collect::<Vec<_>>();
            ensure!(
                c["tokenizer_sha256"]
                    == hash(canonical(&serde_json::to_value(tokenizers)?)?.as_bytes()),
                "artifact_mismatch"
            );
        }
        let mut table = BTreeMap::new();
        for f in files {
            f.artifact.validate()?;
            ensure!(
                table.insert(f.artifact.artifact_id.clone(), f).is_none(),
                "artifact_mismatch"
            );
        }
        ensure!(
            table.len() == artifacts.len()
                && artifacts
                    .iter()
                    .all(|a| table.get(&a.artifact_id).is_some_and(|f| f.artifact == *a)),
            "artifact_mismatch"
        );
        for c in array(&profile["components"])? {
            ensure!(c["engine"] == owner.engine, "context_mismatch");
        }
        Ok(Self {
            nonce: owner.channel_nonce,
            profile_sha: hash(owner.profile_json.as_bytes()),
            profile_json: owner.profile_json,
            profile,
            engine: owner.engine,
            adapters: owner.adapters,
            files: table,
        })
    }
    pub fn component(&self, id_value: &Value) -> Result<Value> {
        let name = id(id_value)?;
        self.profile["components"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["component_id"] == name)
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))
    }
    pub fn bound(&self, p: &Value) -> Result<Value> {
        ensure!(
            p["protocol"] == "nlp-worker.v2" && p["profile_sha256"] == self.profile_sha,
            "binding_mismatch"
        );
        id(&p["request_id"])?;
        let c = self.component(&p["component_id"])?;
        ensure!(
            p["component_sha256"] == hash(canonical(&c)?.as_bytes()),
            "binding_mismatch"
        );
        Ok(c)
    }
    pub fn artifact_path(&self, name: &str, role: &str) -> Result<PathBuf> {
        let f = self
            .files
            .get(name)
            .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
        ensure!(f.artifact.role == role, "artifact_mismatch");
        Ok(f.path.clone())
    }
}
