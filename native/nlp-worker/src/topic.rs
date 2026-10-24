//! Profile-bound classification with independent multi-label decisions.
use crate::{context::Context, entity::Control, wire};
use anyhow::{Result, ensure};
use gliner2_rs::{
    boundary::preprocessing::{BoundaryPreprocessingPolicy, OriginalCoverage, WordSplitter},
    classification::build_classification_schema_tokens_with_descriptions,
    classifier::Classifier,
    encoder::Encoder,
    schema::{FormattedInput, Segment, format_input_with_mapping},
    scores::{Activation, ClassificationScores},
    tokenizer::RuntimeTokenizer,
};
use ndarray::{Array2, Array3, s};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub const ADAPTER: &str = "boundary_topics";
const FORMAT: &str = "onnx-boundary-classification.v1";
const CONFIG: &str = "boundary-topic-config.v1";
const MAX_LABELS: usize = 64;
const MAX_TOKENS: usize = 4096;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Graphs {
    encoder: String,
    classifier: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Mapping {
    label_id: String,
    model_label: String,
    topic_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Decoding {
    activation: String,
    multi_label: bool,
    comparison: String,
    allow_empty: bool,
    threshold: f32,
    temperature: f32,
    hidden_size: usize,
    max_encoded_tokens: usize,
    max_words: usize,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Group {
    group_id: String,
    task_name: String,
    instruction: Option<String>,
    label_ids: Vec<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    schema: String,
    graphs: Graphs,
    tokenizer: String,
    task_name: String,
    instruction: Option<String>,
    labels: Vec<Mapping>,
    policies: BTreeMap<String, Value>,
    decoding: Decoding,
    #[serde(default)]
    groups: Option<Vec<Group>>,
}

pub struct TopicAdapter {
    tokenizer: RuntimeTokenizer,
    encoder: Encoder,
    classifier: Classifier,
    config: Config,
    conditioning: Vec<(String, String)>,
    component: Value,
}

fn plain(value: &str, max: usize, nonempty: bool) -> Result<()> {
    ensure!(
        value.chars().count() <= max && (!nonempty || !value.trim().is_empty()),
        "binding_mismatch"
    );
    for marker in [
        "[P]",
        "[L]",
        "[E]",
        "[C]",
        "[R]",
        "[DESCRIPTION]",
        "[SEP_TEXT]",
        "[SEP_STRUCT]",
    ] {
        ensure!(!value.contains(marker), "binding_mismatch");
    }
    Ok(())
}

fn bind_labels(component: &Value, cfg: &Config) -> Result<Vec<(String, String)>> {
    let tasks = component["tasks"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
    ensure!(
        tasks.len() == 1
            && tasks[0]["task"] == "message_topics"
            && tasks[0]["task_contract"] == "message_topics.v1",
        "incompatible"
    );
    let score = &tasks[0]["score_policy"];
    ensure!(
        (score["kind"] == "probability" || score["kind"] == "none")
            && score["calibration"] == "not_calibrated"
            && score["calibration_sha256"].is_null(),
        "incompatible"
    );
    let executed = component["executed_labels"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
    ensure!(
        !executed.is_empty() && executed.len() <= MAX_LABELS && cfg.labels.len() == executed.len(),
        "binding_mismatch"
    );
    let mut ids = BTreeSet::new();
    let mut model_labels = BTreeSet::new();
    let mut conditioning = Vec::new();
    for (label, mapping) in executed.iter().zip(&cfg.labels) {
        wire::id(&json!(mapping.label_id))?;
        wire::id(&json!(mapping.topic_id))?;
        plain(&mapping.model_label, 256, true)?;
        ensure!(
            mapping.label_id == mapping.topic_id
                && label["task"] == "message_topics"
                && label["label_id"] == mapping.label_id
                && ids.insert(mapping.label_id.as_str())
                && model_labels.insert(mapping.model_label.as_str()),
            "binding_mismatch"
        );
        let description = label["description"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
        plain(description, 2048, false)?;
        conditioning.push((mapping.model_label.clone(), description.to_owned()));
    }
    let enabled = tasks[0]["enabled_ids"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
    let mut seen = BTreeSet::new();
    ensure!(
        enabled.len() <= MAX_LABELS
            && enabled.iter().all(|v| v
                .as_str()
                .is_some_and(|id| ids.contains(id) && seen.insert(id))),
        "binding_mismatch"
    );
    Ok(conditioning)
}

fn parse_config(component: &Value, bytes: &[u8]) -> Result<(Config, Vec<(String, String)>)> {
    ensure!(bytes.len() <= 65536, "resource_limit");
    let value = wire::parse(bytes)?;
    let mut keys = vec![
        "schema",
        "graphs",
        "tokenizer",
        "task_name",
        "instruction",
        "labels",
        "policies",
        "decoding",
    ];
    if value.get("groups").is_some() {
        ensure!(value["groups"].is_array(), "binding_mismatch");
        for group in value["groups"].as_array().unwrap() {
            wire::object(
                group,
                &["group_id", "task_name", "instruction", "label_ids"],
            )?;
        }
        keys.push("groups");
    }
    wire::object(&value, &keys)?;
    let cfg: Config = serde_json::from_value(value)?;
    let d = &cfg.decoding;
    ensure!(
        cfg.schema == CONFIG
            && d.activation == "sigmoid"
            && d.multi_label
            && d.comparison == "inclusive"
            && d.allow_empty
            && d.threshold.is_finite()
            && (0.0..=1.0).contains(&d.threshold)
            && d.temperature.is_finite()
            && (0.0001..=10000.0).contains(&d.temperature)
            && (1..=4096).contains(&d.hidden_size)
            && (1..=MAX_TOKENS).contains(&d.max_encoded_tokens)
            && (1..=MAX_TOKENS).contains(&d.max_words),
        "incompatible"
    );
    plain(&cfg.task_name, 256, true)?;
    if let Some(instruction) = &cfg.instruction {
        plain(instruction, 2048, false)?;
    }
    let policies = [
        ("preprocessing", "boundary-whitespace.v1"),
        ("windowing", "single-message-no-truncation.v1"),
        ("decoding", "multi-label-sigmoid.v1"),
        ("output_mapping", "ordered-topic-mapping.v1"),
        ("decision", "inclusive-threshold-empty.v1"),
    ];
    ensure!(
        cfg.policies.len() == policies.len() + 1
            && cfg.policies.get("runtime_requirements") == Some(&component["runtime_requirements"]),
        "binding_mismatch"
    );
    for (name, policy_id) in policies {
        ensure!(component[name]["policy_id"] == policy_id, "incompatible");
        ensure!(
            cfg.policies.get(name) == Some(&component[name]),
            "binding_mismatch"
        );
    }
    let conditioning = bind_labels(component, &cfg)?;
    classification_views(&cfg, &conditioning)?;
    Ok((cfg, conditioning))
}

fn bind_artifacts(
    artifacts: &BTreeMap<String, crate::context::Artifact>,
    component: &Value,
    cfg: &Config,
    config_id: &str,
) -> Result<()> {
    let expected = [
        cfg.graphs.encoder.as_str(),
        cfg.graphs.classifier.as_str(),
        cfg.tokenizer.as_str(),
        config_id,
    ];
    let names: BTreeSet<_> = expected.iter().copied().collect();
    let ids = component["artifact_ids"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
    let actual = ids
        .iter()
        .map(|v| {
            v.as_str()
                .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))
        })
        .collect::<Result<BTreeSet<_>>>()?;
    ensure!(
        names.len() == 4 && ids.len() == 4 && actual == names,
        "artifact_mismatch"
    );
    let roles = [
        (expected[0], "weights", FORMAT),
        (expected[1], "weights", FORMAT),
        (expected[2], "tokenizer", "huggingface-tokenizer-json.v1"),
        (config_id, "configuration", CONFIG),
    ];
    for (name, role, format) in roles {
        wire::id(&json!(name))?;
        let file = artifacts
            .get(name)
            .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
        ensure!(
            file.role == role
                && (file.format_id == format
                    || (name == cfg.graphs.encoder && file.format_id == "onnx-boundary-entity.v1")),
            "artifact_mismatch"
        );
    }
    Ok(())
}

fn schema(cfg: &Config, conditioning: &[(String, String)]) -> Vec<String> {
    let labels = conditioning.iter().map(|v| v.0.clone()).collect::<Vec<_>>();
    build_classification_schema_tokens_with_descriptions(
        &cfg.task_name,
        &labels,
        cfg.instruction.as_deref(),
        conditioning,
    )
}

struct ClassificationView {
    indices: Vec<usize>,
    schema: Vec<String>,
}

fn classification_views(
    cfg: &Config,
    conditioning: &[(String, String)],
) -> Result<Vec<ClassificationView>> {
    let Some(groups) = &cfg.groups else {
        return Ok(vec![ClassificationView {
            indices: (0..cfg.labels.len()).collect(),
            schema: schema(cfg, conditioning),
        }]);
    };
    ensure!(
        !groups.is_empty() && groups.len() <= cfg.labels.len(),
        "binding_mismatch"
    );
    let mut names = BTreeSet::new();
    let mut assigned = BTreeSet::new();
    let mut views = Vec::new();
    for group in groups {
        wire::id(&json!(group.group_id))?;
        ensure!(
            names.insert(&group.group_id) && !group.label_ids.is_empty(),
            "binding_mismatch"
        );
        plain(&group.task_name, 256, true)?;
        if let Some(instruction) = &group.instruction {
            plain(instruction, 2048, false)?;
        }
        let mut indices = Vec::new();
        for id in &group.label_ids {
            let index = cfg
                .labels
                .iter()
                .position(|label| &label.label_id == id)
                .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
            ensure!(assigned.insert(index), "binding_mismatch");
            indices.push(index);
        }
        ensure!(
            indices.windows(2).all(|pair| pair[0] < pair[1]),
            "binding_mismatch"
        );
        let selected = indices
            .iter()
            .map(|&i| conditioning[i].clone())
            .collect::<Vec<_>>();
        let labels = selected
            .iter()
            .map(|label| label.0.clone())
            .collect::<Vec<_>>();
        views.push(ClassificationView {
            indices,
            schema: build_classification_schema_tokens_with_descriptions(
                &group.task_name,
                &labels,
                group.instruction.as_deref(),
                &selected,
            ),
        });
    }
    ensure!(assigned.len() == cfg.labels.len(), "binding_mismatch");
    Ok(views)
}

fn assemble_logits<T>(
    views: &[(Vec<usize>, T)],
    count: usize,
    mut infer: impl FnMut(&T) -> Result<Vec<f32>>,
) -> Result<Vec<f32>> {
    let mut logits = vec![None; count];
    for (indices, input) in views {
        let values = infer(input)?;
        ensure!(
            values.len() == indices.len() && values.iter().all(|v| v.is_finite()),
            "classifier_output"
        );
        for (&index, value) in indices.iter().zip(values) {
            ensure!(
                index < count && logits[index].is_none(),
                "classifier_output"
            );
            logits[index] = Some(value);
        }
    }
    logits
        .into_iter()
        .map(|value| value.ok_or_else(|| anyhow::anyhow!("classifier_output")))
        .collect()
}

fn words(text: &str, limit: usize) -> Result<Vec<String>> {
    ensure!(text.len() <= 65536, "encoded_bound");
    let prepared =
        BoundaryPreprocessingPolicy::new(limit, WordSplitter::Whitespace)?.prepare(text, &[]);
    ensure!(prepared.truncated_words == 0, "word_bound");
    if prepared
        .original_offsets
        .iter()
        .all(|offset| offset.coverage == OriginalCoverage::SyntheticOnly)
    {
        return Ok(Vec::new());
    }
    Ok(prepared.text_tokens)
}

fn examine_words(
    text: &str,
    limit: usize,
    analyze: impl FnOnce(Vec<String>) -> Result<Vec<Value>>,
) -> Result<Vec<Value>> {
    let words = words(text, limit)?;
    if words.is_empty() {
        return Ok(Vec::new());
    }
    analyze(words)
}

fn label_routes(
    formatted: &FormattedInput,
    schema: &[String],
    labels: usize,
    max_tokens: usize,
) -> Result<Vec<usize>> {
    let length = formatted.input_ids.len();
    ensure!(
        (1..=max_tokens).contains(&length) && length <= MAX_TOKENS,
        "encoded_bound"
    );
    ensure!(
        formatted.attention_mask.len() == length
            && formatted.subwords.len() == length
            && formatted.mapped_indices.len() == length
            && formatted.input_ids.iter().all(|v| *v >= 0)
            && formatted.attention_mask.iter().all(|v| *v == 1),
        "query_route"
    );
    let markers = schema
        .iter()
        .enumerate()
        .filter(|(_, v)| v.as_str() == "[P]" || v.as_str() == "[L]")
        .map(|(i, v)| (i, v.as_str()))
        .collect::<Vec<_>>();
    ensure!(
        markers.len() == labels + 1
            && markers[0].1 == "[P]"
            && markers.iter().skip(1).all(|v| v.1 == "[L]"),
        "query_structure"
    );
    let actual_markers = formatted
        .subwords
        .iter()
        .zip(&formatted.mapped_indices)
        .filter(|(word, mapping)| {
            mapping.segment == Segment::Schema
                && ["[P]", "[L]", "[E]", "[C]", "[R]"].contains(&word.as_str())
        })
        .count();
    ensure!(actual_markers == labels + 1, "query_route");
    let mut routes = Vec::new();
    for (word, marker) in markers {
        let positions = formatted
            .mapped_indices
            .iter()
            .enumerate()
            .filter(|(_, m)| {
                m.segment == Segment::Schema && m.schema_idx == 0 && m.orig_idx == word
            })
            .map(|(i, _)| i)
            .collect::<Vec<_>>();
        ensure!(
            positions.len() == 1 && formatted.subwords[positions[0]] == marker,
            "query_route"
        );
        if marker == "[L]" {
            routes.push(positions[0]);
        }
    }
    Ok(routes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::context::Artifact;
    use gliner2_rs::schema::Mapping as Route;

    fn example() -> (Value, Value) {
        let mut policies = serde_json::Map::new();
        for (name, policy_id) in [
            ("runtime_requirements", "cpu.two"),
            ("preprocessing", "boundary-whitespace.v1"),
            ("windowing", "single-message-no-truncation.v1"),
            ("decoding", "multi-label-sigmoid.v1"),
            ("output_mapping", "ordered-topic-mapping.v1"),
            ("decision", "inclusive-threshold-empty.v1"),
        ] {
            policies.insert(
                name.into(),
                json!({"policy_id":policy_id,"revision":"1","sha256":"a".repeat(64)}),
            );
        }
        let mut component = json!({"tasks":[{"task":"message_topics","task_contract":"message_topics.v1",
            "enabled_ids":["topic.a","topic.b"],"score_policy":{"kind":"probability","calibration":"not_calibrated","calibration_sha256":null}}],
            "artifact_ids":["encoder","classifier","tokenizer","configuration"],
            "executed_labels":[{"task":"message_topics","label_id":"topic.a","description":"First definition"},
                {"task":"message_topics","label_id":"topic.b","description":"Second definition"}],"max_output_findings":64});
        for (name, value) in &policies {
            component[name] = value.clone();
        }
        let cfg = json!({"schema":CONFIG,"graphs":{"encoder":"encoder","classifier":"classifier"},
            "tokenizer":"tokenizer","task_name":"topics","instruction":null,
            "labels":[{"label_id":"topic.a","model_label":"First","topic_id":"topic.a"},
                {"label_id":"topic.b","model_label":"Second","topic_id":"topic.b"}],
            "policies":policies,"decoding":{"activation":"sigmoid","multi_label":true,"comparison":"inclusive","allow_empty":true,
                "threshold":0.5,"temperature":1.0,"hidden_size":2,"max_encoded_tokens":512,"max_words":512}});
        (component, cfg)
    }
    fn parsed(component: &Value, cfg: &Value) -> Result<(Config, Vec<(String, String)>)> {
        parse_config(component, &serde_json::to_vec(cfg)?)
    }
    fn grouped_example() -> (Value, Value) {
        let (component, mut config) = example();
        config["groups"] = json!([
            {"group_id":"second", "task_name":"details", "instruction":"Inspect details", "label_ids":["topic.b"]},
            {"group_id":"first", "task_name":"summary", "instruction":null, "label_ids":["topic.a"]}
        ]);
        (component, config)
    }
    #[test]
    fn grouped_schema_uses_each_explicit_prompt_and_bound_description() {
        let (component, config) = grouped_example();
        let (cfg, conditioning) = parsed(&component, &config).unwrap();
        let views = classification_views(&cfg, &conditioning).unwrap();
        assert_eq!(views.len(), 2);
        assert_eq!(views[0].indices, [1]);
        assert_eq!(views[1].indices, [0]);
        assert_eq!(
            views[0].schema,
            build_classification_schema_tokens_with_descriptions(
                "details",
                &["Second".into()],
                Some("Inspect details"),
                &[("Second".into(), "Second definition".into())]
            )
        );
        assert_eq!(
            views[1].schema,
            build_classification_schema_tokens_with_descriptions(
                "summary",
                &["First".into()],
                None,
                &[("First".into(), "First definition".into())]
            )
        );
        let (flat, values) = example();
        let (cfg, conditioning) = parsed(&flat, &values).unwrap();
        let views = classification_views(&cfg, &conditioning).unwrap();
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].schema, schema(&cfg, &conditioning));
        assert_eq!(views[0].indices, [0, 1]);
    }
    #[test]
    fn group_partition_and_exact_fields_are_required() {
        let (component, original) = grouped_example();
        for groups in [
            json!(null),
            json!([]),
            json!([{"group_id":"one","task_name":"topics","instruction":null,"label_ids":["topic.a"]}]),
            json!([{"group_id":"one","task_name":"topics","instruction":null,"label_ids":["topic.a","topic.a"]}]),
            json!([{"group_id":"one","task_name":"topics","instruction":null,"label_ids":["topic.a","missing"]}]),
            json!([{"group_id":"one","task_name":"topics","instruction":null,"label_ids":["topic.b","topic.a"]}]),
        ] {
            let mut config = original.clone();
            config["groups"] = groups;
            assert!(parsed(&component, &config).is_err());
        }
        for change in 0..7 {
            let mut config = original.clone();
            match change {
                0 => config["groups"][1]["group_id"] = json!("second"),
                1 => config["groups"][0]["label_ids"] = json!([]),
                2 => config["groups"][0]["task_name"] = json!("[L]"),
                3 => config["groups"][0]["instruction"] = json!("[P]"),
                4 => {
                    config["groups"][0]
                        .as_object_mut()
                        .unwrap()
                        .remove("instruction");
                }
                5 => config["groups"][0]["extra"] = json!(true),
                _ => config["unknown"] = json!(true),
            }
            assert!(parsed(&component, &config).is_err(), "case {change}");
        }
    }
    #[test]
    fn grouped_outputs_are_atomic_finite_and_in_global_order() {
        let views = vec![(vec![1], vec![0.0]), (vec![0], vec![-2.0])];
        let logits = assemble_logits(&views, 2, |v| Ok(v.clone())).unwrap();
        assert_eq!(logits, [-2.0, 0.0]);
        let (component, config) = example();
        let (cfg, _) = parsed(&component, &config).unwrap();
        let findings = decode(&component, &cfg, &logits).unwrap();
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0]["topic_id"], "topic.b");
        for bad in [vec![], vec![f32::NAN], vec![f32::INFINITY], vec![0.0, 1.0]] {
            let mut calls = 0;
            assert!(
                assemble_logits(&views, 2, |_| {
                    calls += 1;
                    Ok(if calls == 1 { vec![0.0] } else { bad.clone() })
                })
                .is_err()
            );
            assert_eq!(calls, 2);
        }
        let mut calls = 0;
        assert!(
            assemble_logits(&views, 2, |_| {
                calls += 1;
                if calls == 2 {
                    anyhow::bail!("cancelled")
                } else {
                    Ok(vec![1.0])
                }
            })
            .is_err()
        );
        assert!(assemble_logits(&views[..1], 2, |v| Ok(v.clone())).is_err());
        let duplicate = vec![(vec![0], vec![1.0]), (vec![0], vec![1.0])];
        assert!(assemble_logits(&duplicate, 2, |v| Ok(v.clone())).is_err());
        let unknown = vec![(vec![2], vec![1.0])];
        assert!(assemble_logits(&unknown, 2, |v| Ok(v.clone())).is_err());
    }
    #[test]
    fn every_group_view_requires_its_complete_actual_routes() {
        let (component, config) = grouped_example();
        let (cfg, conditioning) = parsed(&component, &config).unwrap();
        let views = classification_views(&cfg, &conditioning).unwrap();
        for view in views {
            let mut input = formatted(&view.schema);
            assert!(label_routes(&input, &view.schema, view.indices.len(), 512).is_ok());
            assert!(
                label_routes(
                    &input,
                    &view.schema,
                    view.indices.len(),
                    input.input_ids.len() - 1
                )
                .is_err()
            );
            input.attention_mask[0] = 0;
            assert!(label_routes(&input, &view.schema, view.indices.len(), 512).is_err());
        }
    }
    fn files() -> BTreeMap<String, Artifact> {
        [
            ("encoder", "weights", FORMAT),
            ("classifier", "weights", FORMAT),
            ("tokenizer", "tokenizer", "huggingface-tokenizer-json.v1"),
            ("configuration", "configuration", CONFIG),
        ]
        .into_iter()
        .map(|(id, role, format)| {
            (
                id.into(),
                Artifact {
                    artifact_id: id.into(),
                    sha256: "b".repeat(64),
                    size_bytes: 1,
                    role: role.into(),
                    format_id: format.into(),
                },
            )
        })
        .collect()
    }
    fn formatted(schema: &[String]) -> FormattedInput {
        // One invented piece per schema word; no tokenizer or graph is loaded.
        FormattedInput {
            subwords: schema.to_vec(),
            input_ids: vec![1; schema.len()],
            attention_mask: vec![1; schema.len()],
            mapped_indices: (0..schema.len())
                .map(|orig_idx| Route {
                    segment: Segment::Schema,
                    orig_idx,
                    schema_idx: 0,
                })
                .collect(),
        }
    }

    #[test]
    fn matched_capability_limits_and_score_kind_are_binding() {
        let (mut c, _) = example();
        c["adapter"] = json!({"adapter_id":ADAPTER,"adapter_version":"1","adapter_build_sha256":"a".repeat(64),
            "abi":"nlp-adapter.v1","architecture":"boundary-shared-v1","artifact_format":FORMAT,
            "tokenizer_format":"huggingface-tokenizer-json.v1"});
        c["inference_threads"] = json!(2);
        let mut cap = json!({"adapter":c["adapter"],"task_contracts":["message_topics.v1"],"score_kinds":["probability","none"],
            "runtime_requirements_sha256":"a".repeat(64)});
        for (field, n) in [
            ("max_source_codepoints", 4096),
            ("max_batch_items", 1),
            ("max_windows", 1),
            ("max_candidates", 64),
            ("max_output_findings", 64),
        ] {
            c[field] = json!(n);
            cap[field] = json!(n);
        }
        assert!(admission(&c, &cap).is_ok());
        for (kind, advertised) in [("probability", "none"), ("none", "probability")] {
            let (mut c, mut cap) = (c.clone(), cap.clone());
            c["tasks"][0]["score_policy"]["kind"] = json!(kind);
            cap["score_kinds"] = json!([advertised]);
            assert!(admission(&c, &cap).is_err());
        }
        for field in [
            "max_source_codepoints",
            "max_batch_items",
            "max_windows",
            "max_candidates",
            "max_output_findings",
        ] {
            let mut bad = c.clone();
            bad[field] = json!(cap[field].as_u64().unwrap() + 1);
            assert!(admission(&bad, &cap).is_err());
        }
        let mut bad = cap.clone();
        bad["task_contracts"] = json!(["concept_mentions.v1"]);
        assert!(admission(&c, &bad).is_err());
        let mut bad = c.clone();
        bad["runtime_requirements"]["sha256"] = json!("b".repeat(64));
        assert!(admission(&bad, &cap).is_err());
    }
    #[test]
    fn complete_ordered_conditioning_is_independent_from_return_selection() {
        let (mut c, v) = example();
        let (cfg, original) = parsed(&c, &v).unwrap();
        let prompt = schema(&cfg, &original);
        for enabled in [json!(["topic.a"]), json!([])] {
            c["tasks"][0]["enabled_ids"] = enabled;
            let (cfg, conditioning) = parsed(&c, &v).unwrap();
            assert_eq!(conditioning, original);
            assert_eq!(schema(&cfg, &conditioning), prompt);
            assert_eq!(
                decode(&c, &cfg, &[5., 5.]).unwrap().len(),
                c["tasks"][0]["enabled_ids"].as_array().unwrap().len()
            );
        }
        let rendered = prompt.join(" ");
        assert!(rendered.contains("First: First definition"));
        assert!(rendered.contains("Second: Second definition"));
    }
    #[test]
    fn mapping_order_identity_and_enabled_members_are_exact() {
        let (c, v) = example();
        for fault in 0..8 {
            let (mut c, mut v) = (c.clone(), v.clone());
            match fault {
                0 => v["labels"].as_array_mut().unwrap().swap(0, 1),
                1 => v["labels"][1]["model_label"] = json!("First"),
                2 => v["labels"][1]["topic_id"] = json!("topic.a"),
                3 => c["tasks"][0]["enabled_ids"] = json!(["topic.missing"]),
                4 => c["tasks"][0]["enabled_ids"] = json!(["topic.a", "topic.a"]),
                5 => c["executed_labels"][0]["task"] = json!("concept_mentions"),
                6 => c["tasks"][0]["task_contract"] = json!("concept_mentions.v1"),
                _ => v["labels"].as_array_mut().unwrap().clear(),
            }
            assert!(parsed(&c, &v).is_err(), "fault {fault}");
        }
    }
    #[test]
    fn strict_parser_rejects_duplicate_unknown_and_missing_fields() {
        let (c, v) = example();
        for field in ["instruction", "task_name", "decoding"] {
            let mut bad = v.clone();
            bad.as_object_mut().unwrap().remove(field);
            assert!(parsed(&c, &bad).is_err());
        }
        let mut bad = v.clone();
        bad["extra"] = json!(true);
        assert!(parsed(&c, &bad).is_err());
        let mut bad = v.clone();
        bad["decoding"]["extra"] = json!(true);
        assert!(parsed(&c, &bad).is_err());
        let bytes = serde_json::to_vec(&v).unwrap();
        let mut duplicate = bytes[..bytes.len() - 1].to_vec();
        duplicate.extend_from_slice(b",\"schema\":\"other\"}");
        assert!(parse_config(&c, &duplicate).is_err());
        assert!(parse_config(&c, &vec![b' '; 65537]).is_err());
    }
    #[test]
    fn unsupported_or_unbound_policies_are_refused() {
        let (c, v) = example();
        for field in [
            "preprocessing",
            "windowing",
            "decoding",
            "output_mapping",
            "decision",
        ] {
            let (mut c, mut v) = (c.clone(), v.clone());
            c[field]["policy_id"] = json!("unsupported");
            v["policies"][field] = c[field].clone();
            assert!(parsed(&c, &v).is_err());
        }
        let mut bad = v.clone();
        bad["policies"]["decoding"]["sha256"] = json!("c".repeat(64));
        assert!(parsed(&c, &bad).is_err());
        for (field, value) in [
            ("activation", json!("softmax")),
            ("multi_label", json!(false)),
            ("allow_empty", json!(false)),
            ("comparison", json!("exclusive")),
            ("threshold", json!(-0.01)),
            ("threshold", json!(1.01)),
            ("temperature", json!(0)),
            ("temperature", json!(10001)),
            ("temperature", json!(0.00001)),
            ("hidden_size", json!(0)),
            ("hidden_size", json!(4097)),
            ("max_encoded_tokens", json!(4097)),
            ("max_words", json!(0)),
        ] {
            let mut bad = v.clone();
            bad["decoding"][field] = value;
            assert!(parsed(&c, &bad).is_err(), "{field}");
        }
    }
    #[test]
    fn reserved_markers_in_every_conditioning_field_are_refused() {
        let (c, v) = example();
        for marker in [
            "[P]",
            "[L]",
            "[E]",
            "[C]",
            "[R]",
            "[DESCRIPTION]",
            "[SEP_TEXT]",
            "[SEP_STRUCT]",
        ] {
            for field in 0..4 {
                let (mut c, mut v) = (c.clone(), v.clone());
                match field {
                    0 => v["task_name"] = json!(marker),
                    1 => v["instruction"] = json!(marker),
                    2 => v["labels"][0]["model_label"] = json!(marker),
                    _ => c["executed_labels"][0]["description"] = json!(marker),
                }
                assert!(parsed(&c, &v).is_err());
            }
        }
    }
    #[test]
    fn all_logical_members_roles_and_formats_are_required() {
        let (c, v) = example();
        let (cfg, _) = parsed(&c, &v).unwrap();
        assert!(bind_artifacts(&files(), &c, &cfg, "configuration").is_ok());
        let mut shared = files();
        shared.get_mut("encoder").unwrap().format_id = "onnx-boundary-entity.v1".into();
        let entity_encoder = shared["encoder"].clone();
        assert!(bind_artifacts(&shared, &c, &cfg, "configuration").is_ok());
        assert_eq!(shared["encoder"], entity_encoder);
        shared.get_mut("classifier").unwrap().format_id = "onnx-boundary-entity.v1".into();
        assert!(bind_artifacts(&shared, &c, &cfg, "configuration").is_err());
        for id in ["encoder", "classifier", "tokenizer", "configuration"] {
            let mut missing = files();
            missing.remove(id);
            assert!(bind_artifacts(&missing, &c, &cfg, "configuration").is_err());
            let mut bad = files();
            bad.get_mut(id).unwrap().role = "runtime".into();
            assert!(bind_artifacts(&bad, &c, &cfg, "configuration").is_err());
            let mut bad = files();
            bad.get_mut(id).unwrap().format_id = "other".into();
            assert!(bind_artifacts(&bad, &c, &cfg, "configuration").is_err());
        }
        for ids in [
            json!(["encoder", "encoder", "tokenizer", "configuration"]),
            json!(["encoder", "classifier", "tokenizer"]),
            json!([
                "encoder",
                "classifier",
                "tokenizer",
                "configuration",
                "extra"
            ]),
        ] {
            let mut bad = c.clone();
            bad["artifact_ids"] = ids;
            assert!(bind_artifacts(&files(), &bad, &cfg, "configuration").is_err());
        }
    }
    #[test]
    fn full_prompt_bounds_and_marker_routes_precede_encoder_gather() {
        let (c, v) = example();
        let (cfg, conditioning) = parsed(&c, &v).unwrap();
        let tokens = schema(&cfg, &conditioning);
        let input = formatted(&tokens);
        let routes = label_routes(&input, &tokens, 2, 512).unwrap();
        assert_eq!(routes.len(), 2);
        assert!(routes[0] < routes[1]);
        let hidden = Array3::from_shape_fn((1, tokens.len(), 2), |(_, i, j)| (i * 2 + j) as f32);
        let states = gather(&hidden, tokens.len(), 2, &routes).unwrap();
        assert_eq!(states[[1, 1]], (routes[1] * 2 + 1) as f32);
        assert!(label_routes(&input, &tokens, 2, tokens.len() - 1).is_err());
        for fault in 0..6 {
            let mut bad = input.clone();
            match fault {
                0 => bad.subwords[routes[0]] = "[E]".into(),
                1 => bad.mapped_indices[routes[0]].schema_idx = 1,
                2 => bad.mapped_indices[routes[0]].segment = Segment::Text,
                3 => bad.attention_mask[0] = 0,
                4 => bad.input_ids[0] = -1,
                _ => bad.mapped_indices.pop().map(|_| ()).unwrap(),
            }
            assert!(label_routes(&bad, &tokens, 2, 512).is_err());
        }
    }
    #[test]
    fn empty_or_whitespace_source_never_enters_tokenization_or_inference() {
        for text in ["", " ", "\t\n\r", "\u{a0}\u{2003}", "\u{001c}\u{001f}"] {
            assert!(
                examine_words(text, 512, |_| panic!("no source words to classify"))
                    .unwrap()
                    .is_empty()
            );
        }
        let mut called = false;
        let result = examine_words(".", 512, |prepared| {
            called = true;
            assert!(!prepared.is_empty());
            Ok(vec![])
        })
        .unwrap();
        assert!(called && result.is_empty());
    }
    #[test]
    fn word_truncation_and_source_size_are_never_complete() {
        assert!(words("one two three", 1).is_err());
        assert!(words(&"x".repeat(65537), 4096).is_err());
        assert!(words("one two", 512).is_ok());
    }
    #[test]
    fn nonfinite_wrong_shape_and_wrong_rows_are_rejected() {
        let (c, v) = example();
        let (cfg, _) = parsed(&c, &v).unwrap();
        for logits in [
            vec![0.],
            vec![0., 0., 0.],
            vec![f32::NAN, 0.],
            vec![f32::INFINITY, 0.],
            vec![f32::NEG_INFINITY, 0.],
        ] {
            assert!(decode(&c, &cfg, &logits).is_err());
        }
        let mut tiny = v.clone();
        tiny["decoding"]["temperature"] = json!(0.0001);
        let (cfg, _) = parsed(&c, &tiny).unwrap();
        assert!(decode(&c, &cfg, &[f32::MAX, 0.]).is_err());
        assert!(gather(&Array3::zeros((1, 3, 2)), 4, 2, &[0, 1]).is_err());
        assert!(gather(&Array3::zeros((1, 3, 3)), 3, 2, &[0, 1]).is_err());
        assert!(gather(&Array3::zeros((1, 3, 2)), 3, 2, &[0, 3]).is_err());
        assert!(gather(&Array3::from_elem((1, 3, 2), f32::NAN), 3, 2, &[0, 1]).is_err());
    }
    #[test]
    fn inclusive_independent_decisions_allow_empty_and_apply_temperature_once() {
        let (c, v) = example();
        let (cfg, _) = parsed(&c, &v).unwrap();
        assert!(decode(&c, &cfg, &[-1., -2.]).unwrap().is_empty());
        let findings = decode(&c, &cfg, &[0., 0.]).unwrap();
        assert_eq!(findings.len(), 2);
        assert_eq!(findings[0]["topic_id"], "topic.a");
        assert_eq!(findings[0]["score"]["value"], 0.5);
        let mut hot = v.clone();
        hot["decoding"]["temperature"] = json!(2.);
        let (hot, _) = parsed(&c, &hot).unwrap();
        assert_eq!(
            decode(&c, &hot, &[2., -2.]).unwrap(),
            decode(&c, &cfg, &[1., -1.]).unwrap()
        );
        let mut bound = c.clone();
        bound["max_output_findings"] = json!(1);
        assert!(decode(&bound, &cfg, &[1., 1.]).is_err());
    }
    #[test]
    fn score_absence_is_preserved_and_other_score_kinds_are_refused() {
        let (mut c, v) = example();
        c["tasks"][0]["score_policy"]["kind"] = json!("none");
        let (cfg, _) = parsed(&c, &v).unwrap();
        assert!(decode(&c, &cfg, &[0., 0.]).unwrap()[0]["score"].is_null());
        for kind in ["logit", "margin", "other"] {
            c["tasks"][0]["score_policy"]["kind"] = json!(kind);
            assert!(parsed(&c, &v).is_err());
        }
        c["tasks"][0]["score_policy"]["kind"] = json!("probability");
        c["tasks"][0]["score_policy"]["calibration"] = json!("calibrated");
        assert!(parsed(&c, &v).is_err());
    }
}

fn gather(
    hidden: &Array3<f32>,
    length: usize,
    width: usize,
    routes: &[usize],
) -> Result<Array2<f32>> {
    ensure!(
        hidden.dim() == (1, length, width)
            && hidden.iter().all(|v| v.is_finite())
            && routes.iter().all(|i| *i < length),
        "encoder_output"
    );
    let mut states = Array2::zeros((routes.len(), width));
    for (row, index) in routes.iter().enumerate() {
        states.row_mut(row).assign(&hidden.slice(s![0, *index, ..]));
    }
    Ok(states)
}

fn decode(component: &Value, cfg: &Config, logits: &[f32]) -> Result<Vec<Value>> {
    let labels = cfg
        .labels
        .iter()
        .map(|v| v.model_label.clone())
        .collect::<Vec<_>>();
    let scores = ClassificationScores::new(
        cfg.task_name.clone(),
        labels,
        logits.to_vec(),
        cfg.decoding.temperature,
        Activation::Sigmoid,
    )
    .map_err(|_| anyhow::anyhow!("classifier_output"))?;
    ensure!(
        scores.scaled_logits().iter().all(|v| v.is_finite()),
        "classifier_output"
    );
    let enabled = component["tasks"][0]["enabled_ids"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
    let include_score = component["tasks"][0]["score_policy"]["kind"] == "probability";
    let mut findings = Vec::new();
    for (mapping, score) in cfg.labels.iter().zip(scores.probabilities()) {
        if *score >= cfg.decoding.threshold && enabled.iter().any(|v| v == &mapping.topic_id) {
            findings.push(json!({"topic_id":mapping.topic_id,"score":if include_score {json!({"kind":"probability","value":score})} else {Value::Null}}));
        }
    }
    ensure!(
        findings.len() <= component["max_output_findings"].as_u64().unwrap_or(0) as usize,
        "output_bound"
    );
    Ok(findings)
}

fn admission(component: &Value, cap: &Value) -> Result<()> {
    crate::context::supported_adapter(&component["adapter"])?;
    ensure!(
        component["adapter"]["adapter_id"] == ADAPTER && cap["adapter"] == component["adapter"],
        "incompatible"
    );
    crate::context::capability(cap)?;
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
            && component["inference_threads"] == 2
            && component["runtime_requirements"]["sha256"] == cap["runtime_requirements_sha256"],
        "incompatible"
    );
    crate::context::score_kinds(component, cap)?;
    Ok(())
}

impl TopicAdapter {
    pub fn load(context: &Context, component: Value, control: &Control) -> Result<Self> {
        control.check()?;
        let cap = context
            .adapters
            .iter()
            .find(|v| v["adapter"] == component["adapter"])
            .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
        admission(&component, cap)?;
        let ids = component["artifact_ids"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
        let configs = ids
            .iter()
            .filter_map(|v| context.files.get(v.as_str()?))
            .filter(|v| v.artifact.role == "configuration" && v.artifact.format_id == CONFIG)
            .collect::<Vec<_>>();
        ensure!(
            configs.len() == 1 && configs[0].artifact.size_bytes <= 65536,
            "artifact_mismatch"
        );
        let bytes = std::fs::read(&configs[0].path)?;
        let (config, conditioning) = parse_config(&component, &bytes)?;
        ensure!(
            conditioning.len() <= component["max_candidates"].as_u64().unwrap_or(0) as usize,
            "resource_limit"
        );
        let artifacts = context
            .files
            .iter()
            .map(|(id, file)| (id.clone(), file.artifact.clone()))
            .collect();
        bind_artifacts(
            &artifacts,
            &component,
            &config,
            &configs[0].artifact.artifact_id,
        )?;
        crate::tokenizer_metadata::check(&context.files[&config.tokenizer], control)?;
        let encoder_path = context.artifact_path(&config.graphs.encoder, "weights")?;
        let classifier_path = context.artifact_path(&config.graphs.classifier, "weights")?;
        crate::onnx::check(&encoder_path, control)?;
        crate::onnx::check(&classifier_path, control)?;
        let options = crate::runtime::options(context)?;
        control.check()?;
        let tokenizer =
            RuntimeTokenizer::from_file(context.artifact_path(&config.tokenizer, "tokenizer")?)?;
        let encoder = Encoder::new_with_options(encoder_path, options)?;
        control.check()?;
        let classifier = Classifier::new_with_options(classifier_path, options)?;
        control.check()?;
        Ok(Self {
            tokenizer,
            encoder,
            classifier,
            config,
            conditioning,
            component,
        })
    }

    pub fn extract(&self, text: &str, control: &Control) -> Result<Vec<Value>> {
        control.check()?;
        examine_words(text, self.config.decoding.max_words, |words| {
            // Admit every complete view before executing any encoder pass.
            let mut admitted = Vec::new();
            for view in classification_views(&self.config, &self.conditioning)? {
                control.check()?;
                let formatted = format_input_with_mapping(
                    &self.tokenizer,
                    std::slice::from_ref(&view.schema),
                    &words,
                )?;
                label_routes(
                    &formatted,
                    &view.schema,
                    view.indices.len(),
                    self.config.decoding.max_encoded_tokens,
                )?;
                admitted.push((view.indices, (view.schema, formatted)));
            }
            let logits = assemble_logits(
                &admitted,
                self.config.labels.len(),
                |(schema, formatted)| {
                    control.check()?;
                    let routes = label_routes(
                        formatted,
                        schema,
                        schema
                            .iter()
                            .filter(|token| token.as_str() == "[L]")
                            .count(),
                        self.config.decoding.max_encoded_tokens,
                    )?;
                    let length = formatted.input_ids.len();
                    let hidden = self.encoder.infer(
                        Array2::from_shape_vec((1, length), formatted.input_ids.clone())?,
                        Array2::from_shape_vec((1, length), formatted.attention_mask.clone())?,
                    )?;
                    control.check()?;
                    let states =
                        gather(&hidden, length, self.config.decoding.hidden_size, &routes)?;
                    let logits = self.classifier.infer(states)?;
                    control.check()?;
                    Ok(logits
                        .as_slice()
                        .ok_or_else(|| anyhow::anyhow!("classifier_output"))?
                        .to_vec())
                },
            )?;
            control.check()?;
            let findings = decode(&self.component, &self.config, &logits)?;
            control.check()?;
            Ok(findings)
        })
    }
}
