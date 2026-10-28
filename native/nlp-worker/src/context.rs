//! Owner-bound descriptors; wire messages cannot choose executable or data paths.
use crate::wire::{self, canonical, hash, id, object};
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::Read,
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
    let topics = v["adapter_id"] == crate::topic::ADAPTER;
    let instruction = v["adapter_id"] == crate::instruction_topics::ADAPTER;
    ensure!(
        (v["adapter_id"] == ADAPTER
            || topics
            || (instruction && cfg!(feature = "instruction-topics")))
            && v["adapter_version"] == "1"
            && v["abi"] == "nlp-adapter.v1"
            && v["architecture"]
                == if instruction {
                    crate::instruction_topics::ARCHITECTURE
                } else {
                    "boundary-shared-v1"
                }
            && v["artifact_format"]
                == if instruction {
                    "gguf-model.v1"
                } else if topics {
                    "onnx-boundary-classification.v1"
                } else {
                    "onnx-boundary-entity.v1"
                }
            && v["tokenizer_format"]
                == if instruction {
                    "gguf-embedded.v1"
                } else {
                    "huggingface-tokenizer-json.v1"
                }
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
    let instruction = v["adapter"]["adapter_id"] == crate::instruction_topics::ADAPTER;
    let topics = v["adapter"]["adapter_id"] == crate::topic::ADAPTER || instruction;
    ensure!(
        v["task_contracts"]
            == if topics {
                json!(["message_topics.v1"])
            } else {
                json!(["concept_mentions.v1"])
            },
        "incompatible"
    );
    if instruction {
        ensure!(v["score_kinds"] == json!(["none"]), "incompatible");
    }
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
        ("max_candidates", if topics { 64 } else { 192 }),
        ("max_output_findings", if topics { 64 } else { 512 }),
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
pub fn score_kinds(component: &Value, cap: &Value) -> Result<()> {
    let supported = cap["score_kinds"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
    let tasks = component["tasks"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
    ensure!(
        !tasks.is_empty()
            && tasks
                .iter()
                .all(|task| supported.contains(&task["score_policy"]["kind"])),
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
    ensure!(
        [2, 8]
            .iter()
            .any(|n| v["resources"]["inference_threads"] == *n),
        "resource_limit"
    );
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
                && (c["inference_threads"] == 2 || (c["adapter"]["adapter_id"] == crate::instruction_topics::ADAPTER && c["inference_threads"] == 8))
                && c["inference_threads"].as_u64().is_some_and(|n| n <= v["resources"]["inference_threads"].as_u64().unwrap_or(0)),
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
        let artifacts: Vec<Artifact> = serde_json::from_value(v["artifacts"].clone())?;
        for component in components {
            let ids = array(&component["artifact_ids"])?;
            ensure!(
                artifacts
                    .iter()
                    .filter(|asset| asset.role == "weights"
                        && ids.iter().any(|id| id == &asset.artifact_id))
                    .all(|asset| component["adapter"]["artifact_format"] == asset.format_id),
                "artifact_mismatch"
            );
        }
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
                .filter(|a| {
                    a.role == "tokenizer"
                        || (c["adapter"]["tokenizer_format"] == "gguf-embedded.v1"
                            && a.role == "weights")
                })
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
    pub fn presentation_catalog(&self, name: &str, control: &crate::entity::Control) -> Result<()> {
        control.check()?;
        let file = self.files.get(name).ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
        ensure!(file.artifact.artifact_id == name && file.artifact.role == "configuration"
            && file.artifact.format_id == "nlp-presentation.v1"
            && file.artifact.size_bytes <= 65536
            && self.profile["presentation"]["sha256"] == file.artifact.sha256, "artifact_mismatch");
        let mut bytes = Vec::new();
        File::open(&file.path)?.take(65537).read_to_end(&mut bytes)?;
        control.check()?;
        validate_presentation_catalog(name, &file.artifact, &self.profile["presentation"], &bytes)
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

pub fn complete_artifact_ids(ids: &[Value], expected: &BTreeSet<&str>, count: usize) -> Result<()> {
    let actual = ids.iter().filter_map(Value::as_str).collect::<BTreeSet<_>>();
    ensure!(expected.len() == count && ids.len() == count && actual == *expected,
            "artifact_mismatch");
    Ok(())
}

fn validate_presentation_catalog(name: &str, artifact: &Artifact, presentation: &Value, bytes: &[u8]) -> Result<()> {
    artifact.validate()?;
    ensure!(artifact.artifact_id == name && artifact.role == "configuration"
        && artifact.format_id == "nlp-presentation.v1"
        && artifact.size_bytes <= 65536 && bytes.len() <= 65536
        && bytes.len() as u64 == artifact.size_bytes
        && presentation["sha256"] == artifact.sha256
        && hash(bytes) == artifact.sha256, "artifact_mismatch");
    Ok(())
}

pub fn presentation_reference(value: &Value, legacy: &str) -> Result<Option<String>> {
    if value["schema"] == legacy {
        ensure!(value.get("presentation_catalog").is_none(), "invalid_payload");
        return Ok(None);
    }
    let current = format!("{}.v2", legacy.strip_suffix(".v1").unwrap_or(""));
    ensure!(value["schema"] == current, "incompatible");
    id(&value["presentation_catalog"])?;
    Ok(Some(value["presentation_catalog"].as_str().unwrap().to_owned()))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn presentation_component_membership_is_complete_and_shared() {
        for members in [vec!["config", "model", "system", "grammar", "catalogue"],
                        vec!["config", "encoder", "marginals", "scorer", "tokenizer", "catalogue"]] {
            let expected = members.iter().copied().collect::<BTreeSet<_>>();
            let ids = members.iter().map(|id| json!(id)).collect::<Vec<_>>();
            assert!(complete_artifact_ids(&ids, &expected, members.len()).is_ok());
            let mut missing = ids.clone(); missing.pop();
            assert!(complete_artifact_ids(&missing, &expected, members.len()).is_err());
            let mut extra = ids.clone(); extra.push(json!("extra"));
            assert!(complete_artifact_ids(&extra, &expected, members.len()).is_err());
            let mut duplicate = ids.clone(); duplicate[0] = duplicate[1].clone();
            assert!(complete_artifact_ids(&duplicate, &expected, members.len()).is_err());
        }
    }
    #[test]
    fn presentation_versions_require_explicit_nonnull_reference() {
        for legacy in ["boundary-entity-config.v1", "instruction-topic-config.v1"] {
            let mut value = json!({"schema": legacy});
            assert_eq!(presentation_reference(&value, legacy).unwrap(), None);
            value["presentation_catalog"] = Value::Null;
            assert!(presentation_reference(&value, legacy).is_err());
            value["presentation_catalog"] = json!("catalogue");
            assert!(presentation_reference(&value, legacy).is_err());
            value["schema"] = json!(legacy.replace(".v1", ".v2"));
            assert_eq!(presentation_reference(&value, legacy).unwrap(), Some("catalogue".into()));
            for invalid in [Value::Null, json!(""), json!(3), json!([])] {
                value["presentation_catalog"] = invalid;
                assert!(presentation_reference(&value, legacy).is_err());
            }
            value.as_object_mut().unwrap().remove("presentation_catalog");
            assert!(presentation_reference(&value, legacy).is_err());
        }
    }
    #[test]
    fn presentation_catalogue_metadata_and_bytes_are_bound_without_inference() {
        let raw = b"invented display data";
        let artifact = Artifact { artifact_id: "catalogue".into(), sha256: hash(raw),
            size_bytes: raw.len() as u64, role: "configuration".into(),
            format_id: "nlp-presentation.v1".into() };
        let presentation = json!({"sha256":artifact.sha256});
        assert!(validate_presentation_catalog("catalogue", &artifact, &presentation, raw).is_ok());
        for case in 0..7 {
            let mut changed = artifact.clone();
            match case {
                0 => changed.artifact_id = "other".into(),
                1 => changed.role = "weights".into(),
                2 => changed.format_id = "other.v1".into(),
                3 => changed.sha256 = "0".repeat(64),
                4 => changed.size_bytes += 1,
                5 => changed.size_bytes = 65537,
                _ => changed.size_bytes = 0,
            }
            assert!(validate_presentation_catalog("catalogue", &changed, &presentation, raw).is_err());
        }
        assert!(validate_presentation_catalog("catalogue", &artifact, &json!({"sha256":"0".repeat(64)}), raw).is_err());
        assert!(validate_presentation_catalog("catalogue", &artifact, &presentation, b"changed").is_err());
    }
    #[test]
    fn exact_adapter_capabilities_do_not_allow_task_or_format_substitution() {
        for topics in [false, true] {
            let mut cap = json!({"adapter":{"adapter_id":if topics {crate::topic::ADAPTER} else {ADAPTER},"adapter_version":"1",
                "adapter_build_sha256":"a".repeat(64),"abi":"nlp-adapter.v1","architecture":"boundary-shared-v1",
                "artifact_format":if topics {"onnx-boundary-classification.v1"} else {"onnx-boundary-entity.v1"},
                "tokenizer_format":"huggingface-tokenizer-json.v1"},
                "task_contracts":if topics {json!(["message_topics.v1"])} else {json!(["concept_mentions.v1"])},
                "score_kinds":["probability","none"],"max_source_codepoints":4096,"max_batch_items":1,"max_windows":1,
                "max_candidates":if topics {64} else {192},"max_output_findings":if topics {64} else {512},
                "runtime_requirements_sha256":"b".repeat(64)});
            assert!(capability(&cap).is_ok());
            let mut wrong = cap.clone();
            wrong["task_contracts"] = json!(["message_topics.v1", "concept_mentions.v1"]);
            assert!(capability(&wrong).is_err());
            let mut wrong = cap.clone();
            wrong["adapter"]["artifact_format"] = json!("other");
            assert!(capability(&wrong).is_err());
            for (kind, allowed) in [("probability", "none"), ("none", "probability")] {
                cap["score_kinds"] = json!([allowed]);
                let component = json!({"tasks":[{"score_policy":{"kind":kind}}]});
                assert!(score_kinds(&component, &cap).is_err());
            }
        }
    }

    fn serial_profile() -> (Value, Value, Vec<Value>) {
        let engine = json!({"package_name":"Example.Engine","package_family":"Example.Engine_family",
            "core_package_name":"Example.Core","core_package_version":"1.0.0.0","publisher":"CN=Example",
            "package_version":"1.0.0.0","related_set_id":"example.set","engine_version":"1","platform":"windows-x64",
            "worker_protocol":"nlp-worker.v2","engine_build_sha256":"a".repeat(64)});
        let policy = |id: &str| json!({"policy_id":id,"revision":"1","sha256":"b".repeat(64)});
        let mut artifacts = Vec::new();
        for (id, role, format) in [
            ("engine", "engine", "native.v1"),
            ("runtime", "runtime", "native.v1"),
            ("encoder.shared", "weights", "onnx-boundary-entity.v1"),
            (
                "tokenizer.shared",
                "tokenizer",
                "huggingface-tokenizer-json.v1",
            ),
            ("entity.marginals", "weights", "onnx-boundary-entity.v1"),
            ("entity.scorer", "weights", "onnx-boundary-entity.v1"),
            (
                "entity.configuration",
                "configuration",
                "boundary-entity-config.v1",
            ),
            (
                "topic.classifier",
                "weights",
                "onnx-boundary-classification.v1",
            ),
            (
                "topic.configuration",
                "configuration",
                "boundary-topic-config.v1",
            ),
        ] {
            artifacts.push(json!({"artifact_id":id,"sha256":"c".repeat(64),"size_bytes":1,"role":role,"format_id":format}));
        }
        let mut components = Vec::new();
        let mut capabilities = Vec::new();
        for topics in [false, true] {
            let name = if topics {
                "message_topics"
            } else {
                "concept_mentions"
            };
            let ids = if topics {
                vec![
                    "encoder.shared",
                    "tokenizer.shared",
                    "topic.classifier",
                    "topic.configuration",
                ]
            } else {
                vec![
                    "encoder.shared",
                    "tokenizer.shared",
                    "entity.marginals",
                    "entity.scorer",
                    "entity.configuration",
                ]
            };
            let adapter = json!({"adapter_id":if topics {crate::topic::ADAPTER} else {ADAPTER},"adapter_version":"1",
                "adapter_build_sha256":"a".repeat(64),"abi":"nlp-adapter.v1","architecture":"boundary-shared-v1",
                "artifact_format":if topics {"onnx-boundary-classification.v1"} else {"onnx-boundary-entity.v1"},
                "tokenizer_format":"huggingface-tokenizer-json.v1"});
            let mut closure = artifacts
                .iter()
                .filter(|a| {
                    a["role"] == "engine"
                        || a["role"] == "runtime"
                        || ids.iter().any(|id| a["artifact_id"] == *id)
                })
                .cloned()
                .collect::<Vec<_>>();
            closure.sort_by(|a, b| a["artifact_id"].as_str().cmp(&b["artifact_id"].as_str()));
            let tokenizers = closure
                .iter()
                .filter(|a| a["role"] == "tokenizer")
                .collect::<Vec<_>>();
            components.push(json!({"canonicalization":"nlp-canonical.v1","component_id":if topics {"topics"} else {"entities"},
                "engine":engine,"adapter":adapter,"pack_id":"example.pack","pack_revision":"1","pack_sha256":"d".repeat(64),
                "model_revision":"1","artifact_ids":ids,"artifact_closure_sha256":hash(canonical(&json!(closure)).unwrap().as_bytes()),
                "tokenizer_sha256":hash(canonical(&json!(tokenizers)).unwrap().as_bytes()),"runtime_requirements":policy("runtime.two"),
                "tasks":[{"task":name,"task_contract":format!("{name}.v1"),"taxonomy":policy("taxonomy.synthetic"),
                    "enabled_ids":["category.synthetic"],"score_policy":{"kind":"none","semantics_id":"unscored",
                        "calibration":"not_calibrated","calibration_sha256":null}}],
                "executed_labels":[{"task":name,"label_id":"category.synthetic","description":"Invented category"}],
                "preprocessing":policy("preprocessing.synthetic"),"windowing":policy("windowing.synthetic"),
                "decoding":policy("decoding.synthetic"),"output_mapping":policy("mapping.synthetic"),"decision":policy("decision.synthetic"),
                "language":"en","max_source_codepoints":4096,"max_batch_items":1,"max_windows":1,
                "max_candidates":if topics {64} else {192},"max_output_findings":if topics {64} else {512},
                "peak_memory_bytes":1024,"inference_threads":2}));
            capabilities.push(json!({"adapter":adapter,"task_contracts":[format!("{name}.v1")],"score_kinds":["none"],
                "max_source_codepoints":4096,"max_batch_items":1,"max_windows":1,
                "max_candidates":if topics {64} else {192},"max_output_findings":if topics {64} else {512},
                "runtime_requirements_sha256":"b".repeat(64)}));
        }
        (
            json!({"schema_version":"nlp-profile.v3","canonicalization":"nlp-canonical.v1","profile_id":"profile.synthetic",
            "revision":"1","components":components,"bindings":[{"task":"concept_mentions","component_id":"entities"},
                {"task":"message_topics","component_id":"topics"}],"artifacts":artifacts,
            "aggregation":policy("aggregation.synthetic"),"presentation":policy("presentation.synthetic"),
            "resources":{"download_bytes":9,"installed_bytes":9,"rollback_bytes":0,"temporary_bytes":0,
                "transition_peak_memory_bytes":1024,"inference_threads":2},"execution_mode":"serial_unload"}),
            engine,
            capabilities,
        )
    }
    #[test]
    fn complete_serial_v3_profile_binds_one_shared_encoder_and_tokenizer() {
        let (profile, engine, adapters) = serial_profile();
        assert!(validate_profile(&profile).is_ok());
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let root = std::env::temp_dir().join(format!(
            "nlp-shared-profile-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, std::sync::atomic::Ordering::SeqCst)
        ));
        std::fs::create_dir(&root).unwrap();
        let mut files = Vec::new();
        let mut owners = Vec::new();
        let mut paths = Vec::new();
        for value in profile["artifacts"].as_array().unwrap() {
            let artifact: Artifact = serde_json::from_value(value.clone()).unwrap();
            let path = root.join(&artifact.artifact_id);
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create_new(true)
                .open(&path)
                .unwrap();
            paths.push(path.clone());
            owners.push(OwnerArtifact {
                artifact: artifact.clone(),
                handle: 1,
            });
            files.push(BoundFile {
                artifact,
                _file: file,
                path,
            });
        }
        let context = Context::bind(
            Owner {
                schema: "nlp-owner-context.v1".into(),
                channel_nonce: "a".repeat(32),
                profile_json: canonical(&profile).unwrap(),
                engine,
                adapters,
                artifacts: owners,
            },
            files,
        )
        .unwrap();
        assert_eq!(context.files.len(), 9);
        for component in profile["components"].as_array().unwrap() {
            let ids = component["artifact_ids"].as_array().unwrap();
            assert!(
                ids.contains(&json!("encoder.shared")) && ids.contains(&json!("tokenizer.shared"))
            );
            assert_eq!(
                context.artifact_path("encoder.shared", "weights").unwrap(),
                root.join("encoder.shared")
            );
            assert_eq!(
                context
                    .artifact_path("tokenizer.shared", "tokenizer")
                    .unwrap(),
                root.join("tokenizer.shared")
            );
        }
        drop(context);
        for path in paths {
            std::fs::remove_file(path).unwrap();
        }
        std::fs::remove_dir(root).unwrap();
        let mut historical = profile;
        historical["schema_version"] = json!("nlp-profile.v2");
        assert_eq!(
            validate_profile(&historical).unwrap_err().to_string(),
            "artifact_mismatch"
        );
    }
    #[cfg(feature = "instruction-topics")]
    #[test]
    fn instruction_capability_is_closed_and_never_advertises_scores() {
        let mut cap = json!({"adapter":{"adapter_id":"instruction_topics","adapter_version":"1",
            "adapter_build_sha256":"a".repeat(64),"abi":"nlp-adapter.v1","architecture":"llama-b11669-cpu-v1",
            "artifact_format":"gguf-model.v1","tokenizer_format":"gguf-embedded.v1"},
            "task_contracts":["message_topics.v1"],"score_kinds":["none"],"max_source_codepoints":4096,
            "max_batch_items":1,"max_windows":1,"max_candidates":18,"max_output_findings":18,
            "runtime_requirements_sha256":"b".repeat(64)});
        assert!(capability(&cap).is_ok());
        cap["score_kinds"] = json!(["probability"]);
        assert!(capability(&cap).is_err());
        cap["score_kinds"] = json!(["none"]);
        cap["task_contracts"] = json!(["concept_mentions.v1"]);
        assert!(capability(&cap).is_err());
        cap["task_contracts"] = json!(["message_topics.v1"]);
        cap["adapter"]["tokenizer_format"] = json!("none");
        assert!(capability(&cap).is_err());
    }
    #[test]
    fn eight_threads_are_component_specific_and_profile_bounded() {
        let (mut profile, _, _) = serial_profile();
        profile["components"][1]["inference_threads"] = json!(8);
        assert!(validate_profile(&profile).is_err());
        profile["resources"]["inference_threads"] = json!(8);
        assert!(validate_profile(&profile).is_err());
        profile["components"][1]["adapter"]["adapter_id"] =
            json!(crate::instruction_topics::ADAPTER);
        assert!(validate_profile(&profile).is_ok());
        profile["resources"]["inference_threads"] = json!(4);
        assert!(validate_profile(&profile).is_err());
    }
}
