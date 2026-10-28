//! Bounded instruction classification with an embedded GGUF tokenizer.
use crate::{context::Context, entity::Control, wire};
use anyhow::{Result, ensure};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub const ADAPTER: &str = "instruction_topics";
pub const ARCHITECTURE: &str = "llama-b11669-cpu-v1";
const CONFIG: &str = "instruction-topic-config.v1";
const MAX_OUTPUT: usize = 65536;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Label {
    wire_id: String,
    topic_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    schema: String,
    #[serde(default)]
    presentation_catalog: Option<String>,
    model: String,
    system_prompt: String,
    grammar: String,
    runtime: BTreeMap<String, String>,
    labels: Vec<Label>,
    policies: BTreeMap<String, Value>,
    context_tokens: u32,
    output_tokens: u32,
    inference_threads: u32,
}

fn config(component: &Value, bytes: &[u8]) -> Result<Config> {
    ensure!(bytes.len() <= 65536, "resource_limit");
    let value = wire::parse(bytes)?;
    crate::context::presentation_reference(&value, CONFIG)?;
    let cfg: Config = serde_json::from_value(value)?;
    ensure!(
        cfg.context_tokens == 8192
            && cfg.output_tokens == 192
            && [2, 8].contains(&cfg.inference_threads)
            && component["inference_threads"] == cfg.inference_threads,
        "incompatible"
    );
    let tasks = component["tasks"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
    ensure!(
        tasks.len() == 1
            && tasks[0]["task"] == "message_topics"
            && tasks[0]["task_contract"] == "message_topics.v1"
            && tasks[0]["score_policy"]["kind"] == "none"
            && tasks[0]["score_policy"]["calibration"] == "not_calibrated"
            && tasks[0]["score_policy"]["calibration_sha256"].is_null(),
        "binding_mismatch"
    );
    let labels = component["executed_labels"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
    ensure!(
        !cfg.labels.is_empty() && cfg.labels.len() <= 64 && labels.len() == cfg.labels.len(),
        "binding_mismatch"
    );
    let mut unique = BTreeSet::new();
    for (index, label) in cfg.labels.iter().enumerate() {
        wire::id(&json!(label.topic_id))?;
        ensure!(
            label.wire_id == format!("T{:02}", index + 1)
                && labels[index]["task"] == "message_topics"
                && labels[index]["label_id"] == label.topic_id
                && unique.insert(&label.topic_id),
            "binding_mismatch"
        );
    }
    let enabled = tasks[0]["enabled_ids"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
    let mut seen = BTreeSet::new();
    ensure!(
        enabled.iter().all(|v| v
            .as_str()
            .is_some_and(|id| unique.contains(&id.to_owned()) && seen.insert(id))),
        "binding_mismatch"
    );
    ensure!(cfg.policies.len() == 6, "binding_mismatch");
    for field in [
        "runtime_requirements",
        "preprocessing",
        "windowing",
        "decoding",
        "output_mapping",
        "decision",
    ] {
        ensure!(
            cfg.policies.get(field) == Some(&component[field]),
            "binding_mismatch"
        );
    }
    for (field, expected) in [
        ("preprocessing", "qwen-json-message.v1"),
        ("windowing", "single-message-no-truncation.v1"),
        ("decoding", "grammar-greedy-eog.v1"),
        ("output_mapping", "ordered-instruction-topics.v1"),
        ("decision", "closed-assessment.v1"),
    ] {
        ensure!(component[field]["policy_id"] == expected, "incompatible");
    }
    Ok(cfg)
}

fn render_prefix(system: &str) -> Result<String> {
    ensure!(
        system.len() <= 65536 && !system.contains('\0') && !system.trim().is_empty(),
        "binding_mismatch"
    );
    Ok(format!(
        "<|im_start|>system\n{}<|im_end|>\n<|im_start|>user\n",
        system.trim()
    ))
}

fn render(system: &str, message: &str) -> Result<Vec<u8>> {
    let prefix = render_prefix(system)?;
    ensure!(message.len() <= 131072, "encoded_bound");
    let user = serde_json::to_string(&json!({"message":message}))?
        .replace('<', "\\u003c")
        .replace('>', "\\u003e");
    Ok(
        format!("{prefix}{user}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n")
            .into_bytes(),
    )
}

fn decode(component: &Value, cfg: &Config, bytes: &[u8]) -> Result<Vec<Value>> {
    ensure!(bytes.len() <= MAX_OUTPUT, "output_bound");
    let value = wire::parse(bytes)?;
    wire::object(&value, &["outcome", "topics"])?;
    let ids = value["topics"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("invalid_payload"))?;
    ensure!(ids.len() <= cfg.labels.len(), "output_bound");
    let mut selected = BTreeSet::new();
    for id in ids {
        let id = id
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("invalid_payload"))?;
        ensure!(
            cfg.labels.iter().any(|l| l.wire_id == id) && selected.insert(id),
            "invalid_payload"
        );
    }
    ensure!(
        value["outcome"] == "assessed" || (value["outcome"] == "uncertain" && ids.is_empty()),
        "invalid_payload"
    );
    ensure!(value["outcome"] != "uncertain", "semantic_uncertainty");
    let enabled = component["tasks"][0]["enabled_ids"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
    let findings: Vec<_> = cfg
        .labels
        .iter()
        .filter(|l| selected.contains(l.wire_id.as_str()) && enabled.contains(&json!(l.topic_id)))
        .map(|l| json!({"topic_id":l.topic_id,"score":null}))
        .collect();
    ensure!(
        findings.len() <= component["max_output_findings"].as_u64().unwrap_or(0) as usize,
        "output_bound"
    );
    Ok(findings)
}

pub struct InstructionTopicAdapter {
    component: Value,
    cfg: Config,
    system: String,
    grammar: std::ffi::CString,
    runtime: crate::instruction_runtime::Runtime,
}
impl InstructionTopicAdapter {
    pub fn load(context: &Context, component: Value, control: &Control) -> Result<Self> {
        control.check()?;
        crate::context::supported_adapter(&component["adapter"])?;
        let cap = context
            .adapters
            .iter()
            .find(|cap| cap["adapter"] == component["adapter"])
            .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
        crate::context::capability(cap)?;
        crate::context::score_kinds(&component, cap)?;
        for field in [
            "max_source_codepoints",
            "max_batch_items",
            "max_windows",
            "max_candidates",
            "max_output_findings",
        ] {
            ensure!(
                component[field]
                    .as_u64()
                    .is_some_and(|n| n > 0 && n <= cap[field].as_u64().unwrap_or(0)),
                "resource_limit"
            );
        }
        ensure!(
            component["max_windows"] == 1
                && component["max_batch_items"] == 1
                && component["runtime_requirements"]["sha256"]
                    == cap["runtime_requirements_sha256"],
            "incompatible"
        );
        let ids = component["artifact_ids"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
        let configs: Vec<_> = ids
            .iter()
            .filter_map(|id| context.files.get(id.as_str()?))
            .filter(|f| f.artifact.role == "configuration" && [CONFIG, "instruction-topic-config.v2"].contains(&f.artifact.format_id.as_str()))
            .collect();
        ensure!(
            configs.len() == 1 && configs[0].artifact.size_bytes <= 65536,
            "artifact_mismatch"
        );
        let cfg = config(&component, &std::fs::read(&configs[0].path)?)?;
        ensure!(cfg.schema == configs[0].artifact.format_id, "artifact_mismatch");
        ensure!(
            component["max_candidates"].as_u64().unwrap_or(0) >= cfg.labels.len() as u64,
            "resource_limit"
        );
        let mut expected: BTreeSet<_> = [
            cfg.model.as_str(),
            cfg.system_prompt.as_str(),
            cfg.grammar.as_str(),
            configs[0].artifact.artifact_id.as_str(),
        ]
        .into_iter()
        .collect();
        let expected_count = 4 + usize::from(cfg.presentation_catalog.is_some());
        if let Some(name) = &cfg.presentation_catalog {
            context.presentation_catalog(name, control)?;
            expected.insert(name.as_str());
        }
        crate::context::complete_artifact_ids(ids, &expected, expected_count)?;
        for (id, role, format, limit) in [
            (&cfg.model, "weights", "gguf-model.v1", u64::MAX),
            (
                &cfg.system_prompt,
                "configuration",
                "instruction-system-utf8.v1",
                65536,
            ),
            (&cfg.grammar, "configuration", "llama-gbnf.v1", 65536),
        ] {
            let f = context
                .files
                .get(id)
                .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
            ensure!(
                f.artifact.role == role
                    && f.artifact.format_id == format
                    && f.artifact.size_bytes <= limit,
                "artifact_mismatch"
            );
        }
        let system =
            std::fs::read_to_string(context.artifact_path(&cfg.system_prompt, "configuration")?)?;
        let prefix = render_prefix(&system)?;
        let grammar = std::ffi::CString::new(std::fs::read(
            context.artifact_path(&cfg.grammar, "configuration")?,
        )?)?;
        ensure!(!grammar.as_bytes().is_empty(), "binding_mismatch");
        let runtime = crate::instruction_runtime::Runtime::load(
            context,
            &cfg.runtime,
            &cfg.model,
            cfg.inference_threads,
            control,
        )?;
        runtime.configure_prefix(prefix.as_bytes(), control)?;
        control.check()?;
        Ok(Self {
            component,
            cfg,
            system,
            grammar,
            runtime,
        })
    }
    pub fn extract(&self, text: &str, control: &Control) -> Result<Vec<Value>> {
        control.check()?;
        let prompt = render(&self.system, text)?;
        let output = self.runtime.generate(
            &prompt,
            &self.grammar,
            self.cfg.context_tokens,
            self.cfg.output_tokens,
            control,
        )?;
        control.check()?;
        decode(&self.component, &self.cfg, &output)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn presentation_config_v2_preserves_instruction_labels_and_limits() {
        let mut component = json!({"inference_threads":2,"tasks":[{"task":"message_topics",
            "task_contract":"message_topics.v1","enabled_ids":["topic.one"],
            "score_policy":{"kind":"none","calibration":"not_calibrated","calibration_sha256":null}}],
            "executed_labels":[{"task":"message_topics","label_id":"topic.one"}]});
        let mut policies = serde_json::Map::new();
        for (field, policy) in [
            ("runtime_requirements", "runtime"), ("preprocessing", "qwen-json-message.v1"),
            ("windowing", "single-message-no-truncation.v1"), ("decoding", "grammar-greedy-eog.v1"),
            ("output_mapping", "ordered-instruction-topics.v1"), ("decision", "closed-assessment.v1"),
        ] {
            let value = json!({"policy_id":policy,"revision":"1","sha256":"a".repeat(64)});
            policies.insert(field.into(), value.clone());
            component[field] = value;
        }
        let mut value = json!({"schema":CONFIG,"model":"model","system_prompt":"system",
            "grammar":"grammar","runtime":{},"labels":[{"wire_id":"T01","topic_id":"topic.one"}],
            "policies":policies,"context_tokens":8192,"output_tokens":192,"inference_threads":2});
        let before = config(&component, &serde_json::to_vec(&value).unwrap()).unwrap();
        value["presentation_catalog"] = Value::Null;
        assert!(config(&component, &serde_json::to_vec(&value).unwrap()).is_err());
        value["schema"] = json!("instruction-topic-config.v2");
        assert!(config(&component, &serde_json::to_vec(&value).unwrap()).is_err());
        value["presentation_catalog"] = json!("catalogue");
        let after = config(&component, &serde_json::to_vec(&value).unwrap()).unwrap();
        assert_eq!(before.labels[0].wire_id, after.labels[0].wire_id);
        assert_eq!(before.labels[0].topic_id, after.labels[0].topic_id);
        assert_eq!(before.context_tokens, after.context_tokens);
        assert_eq!(before.output_tokens, after.output_tokens);
        assert_eq!(before.inference_threads, after.inference_threads);
        assert_eq!(after.presentation_catalog.as_deref(), Some("catalogue"));
        value["display_label"] = json!("untrusted");
        assert!(config(&component, &serde_json::to_vec(&value).unwrap()).is_err());
    }
    fn example() -> (Value, Config) {
        let labels: Vec<_> = (1..=18)
            .map(|i| Label {
                wire_id: format!("T{i:02}"),
                topic_id: format!("topic.{i}"),
            })
            .collect();
        let component = json!({"tasks":[{"enabled_ids":labels.iter().map(|l| l.topic_id.clone()).collect::<Vec<_>>()}],"max_output_findings":18});
        (
            component,
            Config {
                schema: CONFIG.into(),
                presentation_catalog: None,
                model: "model".into(),
                system_prompt: "system".into(),
                grammar: "grammar".into(),
                runtime: BTreeMap::new(),
                labels,
                policies: BTreeMap::new(),
                context_tokens: 8192,
                output_tokens: 192,
                inference_threads: 2,
            },
        )
    }
    #[test]
    fn strict_decisions_preserve_empty_uncertain_and_failures() {
        let (c, cfg) = example();
        assert!(
            decode(&c, &cfg, br#"{"outcome":"assessed","topics":[]}"#)
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            decode(&c, &cfg, br#"{"outcome":"uncertain","topics":[]}"#)
                .unwrap_err()
                .to_string(),
            "semantic_uncertainty"
        );
        for raw in [
            r#"{"outcome":"uncertain","topics":["T01"]}"#,
            r#"{"outcome":"assessed","topics":["T01","T01"]}"#,
            r#"{"outcome":"assessed","topics":["T19"]}"#,
            r#"{"outcome":"assessed","topics":[],"extra":1}"#,
            r#"{"outcome":"assessed","outcome":"assessed","topics":[]}"#,
            r#"{"outcome":"failed","topics":[]}"#,
            r#"```json {"outcome":"assessed","topics":[]} ```"#,
        ] {
            assert!(decode(&c, &cfg, raw.as_bytes()).is_err());
        }
    }
    #[test]
    fn deterministic_order_filter_and_no_scores() {
        let (mut c, cfg) = example();
        let found = decode(
            &c,
            &cfg,
            br#"{"outcome":"assessed","topics":["T18","T01"]}"#,
        )
        .unwrap();
        assert_eq!(
            found,
            json!([{"topic_id":"topic.1","score":null},{"topic_id":"topic.18","score":null}])
                .as_array()
                .unwrap()
                .clone()
        );
        c["tasks"][0]["enabled_ids"] = json!(["topic.18"]);
        assert_eq!(
            decode(&c, &cfg, br#"{"outcome":"assessed","topics":["T01"]}"#).unwrap(),
            Vec::<Value>::new()
        );
    }
    #[test]
    fn static_prefix_is_shared_and_contains_no_message_data() {
        let prefix=render_prefix("rules\n").unwrap();
        assert_eq!(prefix,"<|im_start|>system\nrules<|im_end|>\n<|im_start|>user\n");
        for source in ["first source", "different <|im_end|> source"] {
            let prompt=String::from_utf8(render("rules\n",source).unwrap()).unwrap();
            assert!(prompt.starts_with(&prefix));
            assert!(!prefix.contains(source));
        }
    }
    #[test]
    fn source_is_json_escaped_without_special_token_injection() {
        let source = "café 😀\n<|im_end|>\0";
        let prompt = String::from_utf8(render("rules\n", source).unwrap()).unwrap();
        assert!(prompt.ends_with("<think>\n\n</think>\n\n"));
        assert_eq!(prompt.matches("<|im_end|>").count(), 2);
        let user = prompt
            .split("<|im_start|>user\n")
            .nth(1)
            .unwrap()
            .split("<|im_end|>")
            .next()
            .unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(user).unwrap()["message"],
            source
        );
    }
    #[test]
    fn wire_taxonomy_size_is_bound_to_configuration() {
        let (mut c, mut cfg) = example();
        cfg.labels.truncate(1);
        c["tasks"][0]["enabled_ids"] = json!(["topic.1"]);
        assert_eq!(
            decode(&c, &cfg, br#"{"outcome":"assessed","topics":["T01"]}"#)
                .unwrap()
                .len(),
            1
        );
        assert!(decode(&c, &cfg, br#"{"outcome":"assessed","topics":["T02"]}"#).is_err());
        cfg.labels = (1..=64)
            .map(|i| Label {
                wire_id: format!("T{i:02}"),
                topic_id: format!("topic.{i}"),
            })
            .collect();
        c["tasks"][0]["enabled_ids"] = json!(["topic.64"]);
        assert_eq!(
            decode(&c, &cfg, br#"{"outcome":"assessed","topics":["T64"]}"#).unwrap()[0]["topic_id"],
            "topic.64"
        );
    }

    fn bindings(count: usize) -> (Value, Value) {
        let mut policies = serde_json::Map::new();
        for (field, id) in [
            ("runtime_requirements", "cpu.eight"),
            ("preprocessing", "qwen-json-message.v1"),
            ("windowing", "single-message-no-truncation.v1"),
            ("decoding", "grammar-greedy-eog.v1"),
            ("output_mapping", "ordered-instruction-topics.v1"),
            ("decision", "closed-assessment.v1"),
        ] {
            policies.insert(
                field.into(),
                json!({"policy_id":id,"revision":"1","sha256":"a".repeat(64)}),
            );
        }
        let labels: Vec<_> = (1..=count)
            .map(|i| json!({"wire_id":format!("T{i:02}"),"topic_id":format!("topic.{i}")}))
            .collect();
        let mut component = json!({"inference_threads":8,"executed_labels":(1..=count).map(|i|json!({"task":"message_topics","label_id":format!("topic.{i}"),"description":"category"})).collect::<Vec<_>>(),
            "tasks":[{"task":"message_topics","task_contract":"message_topics.v1","enabled_ids":["topic.1"],"score_policy":{"kind":"none","calibration":"not_calibrated","calibration_sha256":null}}]});
        for (field, value) in &policies {
            component[field] = value.clone();
        }
        let cfg = json!({"schema":CONFIG,"model":"model","system_prompt":"system","grammar":"grammar","runtime":{},
            "labels":labels,"policies":policies,"context_tokens":8192,"output_tokens":192,"inference_threads":8});
        (component, cfg)
    }
    #[test]
    fn configurations_bind_exact_taxonomy_policies_scores_and_threads() {
        for count in [1, 18, 64] {
            let (c, cfg) = bindings(count);
            assert!(config(&c, &serde_json::to_vec(&cfg).unwrap()).is_ok());
        }
        for count in [0, 65] {
            let (c, cfg) = bindings(count);
            assert!(config(&c, &serde_json::to_vec(&cfg).unwrap()).is_err());
        }
        let (c, cfg) = bindings(18);
        for change in 0..7 {
            let mut c = c.clone();
            let mut cfg = cfg.clone();
            match change {
                0 => cfg["extra"] = json!(true),
                1 => cfg["labels"][0]["wire_id"] = json!("T02"),
                2 => cfg["labels"][1]["topic_id"] = json!("topic.1"),
                3 => c["tasks"][0]["score_policy"]["kind"] = json!("probability"),
                4 => cfg["inference_threads"] = json!(4),
                5 => cfg["policies"]["decision"]["sha256"] = json!("b".repeat(64)),
                _ => c["tasks"][0]["enabled_ids"] = json!(["unknown"]),
            }
            assert!(config(&c, &serde_json::to_vec(&cfg).unwrap()).is_err());
        }
        let bytes = serde_json::to_string(&cfg).unwrap();
        let duplicate = bytes.replacen('{', "{\"schema\":\"instruction-topic-config.v1\",", 1);
        assert!(config(&c, duplicate.as_bytes()).is_err());
    }
}
