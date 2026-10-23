//! Closed CPU boundary entity adapter, with authenticated logical file bindings.
use crate::{
    context::{Context, supported_adapter},
    wire,
};
use anyhow::{Result, ensure};
use gliner2_rs::{
    boundary::{
        decode::{
            OverlapPolicy, QueryScores, WordOffset, group_scored_candidates, map_half_open_utf8,
            resolve_overlaps,
        },
        marginals::{MarginalInput, MarginalModel},
        pool::{PoolConfig, build_shared_candidate_pool},
        preprocessing::{BoundaryPreprocessingPolicy, WordSplitter},
        scorer::{ScorerInput, ScorerModel},
    },
    encoder::Encoder,
    entities::build_entities_schema_tokens_with_descriptions,
    schema::{Segment, format_input_with_mapping},
    tokenizer::RuntimeTokenizer,
};
use ndarray::{Array2, Axis, s};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Instant,
};
#[derive(Clone)]
pub struct Control {
    pub cancelled: Arc<AtomicBool>,
    pub deadline: Instant,
}
impl Control {
    pub fn check(&self) -> Result<()> {
        ensure!(!self.cancelled.load(Ordering::SeqCst), "cancelled");
        ensure!(Instant::now() < self.deadline, "deadline");
        Ok(())
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Graphs {
    encoder: String,
    marginals: String,
    scorer: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Mapping {
    label_id: String,
    model_label: String,
    concept_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Decoding {
    threshold: f32,
    pair_temperature: f32,
    abstention_threshold: f32,
    max_encoded_tokens: usize,
    max_words: usize,
    overlap: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    schema: String,
    graphs: Graphs,
    tokenizer: String,
    labels: Vec<Mapping>,
    policies: BTreeMap<String, Value>,
    decoding: Decoding,
}
pub struct EntityAdapter {
    tokenizer: RuntimeTokenizer,
    encoder: Encoder,
    marginal: MarginalModel,
    scorer: ScorerModel,
    config: Config,
    conditioning: Vec<(String, String)>,
    component: Value,
}
fn bind_labels(component: &Value, mappings: &[Mapping]) -> Result<Vec<(String, String)>> {
    let executed = component["executed_labels"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
    let tasks = component["tasks"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
    ensure!(
        tasks.len() == 1
            && tasks[0]["task"] == "concept_mentions"
            && tasks[0]["task_contract"] == "concept_mentions.v1",
        "incompatible"
    );
    let enabled = tasks[0]["enabled_ids"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
    ensure!(
        !executed.is_empty() && executed.len() <= 16 && mappings.len() == executed.len(),
        "incompatible"
    );
    let bounded = |s: &str| !s.trim().is_empty() && s.chars().count() <= 256;
    let mut ids = std::collections::BTreeSet::new();
    let mut model_labels = std::collections::BTreeSet::new();
    let mut conditioning = Vec::new();
    for (label, mapping) in executed.iter().zip(mappings) {
        // This adapter binds each stable ID to the same output category.
        ensure!(
            bounded(&mapping.label_id)
                && bounded(&mapping.concept_id)
                && bounded(&mapping.model_label)
                && mapping.label_id == mapping.concept_id
                && label["task"] == "concept_mentions"
                && label["label_id"] == mapping.label_id
                && ids.insert(mapping.label_id.as_str())
                && model_labels.insert(mapping.model_label.as_str()),
            "binding_mismatch"
        );
        let description = label["description"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
        ensure!(description.chars().count() <= 2048, "incompatible");
        conditioning.push((mapping.model_label.clone(), description.to_owned()));
    }
    let mut enabled_seen = std::collections::BTreeSet::new();
    ensure!(
        enabled.iter().all(|id| id
            .as_str()
            .is_some_and(|id| ids.contains(id) && enabled_seen.insert(id))),
        "binding_mismatch"
    );
    Ok(conditioning)
}
fn normalized_words(text: &str) -> Result<(String, Vec<String>, Vec<WordOffset>)> {
    ensure!(text.len() <= 65536, "source_bound");
    let policy = BoundaryPreprocessingPolicy::new(512, WordSplitter::Whitespace)?;
    let first = policy.prepare(text, &[]);
    ensure!(first.truncated_words == 0, "word_bound");
    // Already normalized input prevents the library's original-length clamping.
    let full = policy.prepare(&first.normalized_text, &[]);
    ensure!(
        full.truncated_words == 0 && first.text_tokens == full.text_tokens,
        "normalized_route"
    );
    let offsets = full
        .original_offsets
        .iter()
        .map(|v| WordOffset {
            start: v.start,
            end: v.end,
        })
        .collect();
    Ok((first.normalized_text, first.text_tokens, offsets))
}
fn source_coordinates(original: &str, start: usize, end: usize) -> Result<(usize, usize)> {
    ensure!(
        start < end
            && end <= original.len()
            && original.is_char_boundary(start)
            && original.is_char_boundary(end),
        "span_not_original"
    );
    Ok((
        original[..start].chars().count(),
        original[..end].chars().count(),
    ))
}
// Preserve raw first-piece routes in diagnostics; only the hidden-state gather
// clamps a terminal zero-piece placeholder, as in the publisher's gather_routed.
fn gather_text_indices(indices: &[usize], length: usize) -> Result<Vec<usize>> {
    ensure!(
        length > 0 && indices.iter().all(|&i| i <= length),
        "invalid_text_route"
    );
    Ok(indices.iter().map(|&i| i.min(length - 1)).collect())
}
struct PreparedInput {
    normalized: String,
    tokens: Vec<String>,
    offsets: Vec<WordOffset>,
    formatted: gliner2_rs::schema::FormattedInput,
    text_indices: Vec<usize>,
    query_indices: Vec<usize>,
}
fn prepare_input(
    tokenizer: &RuntimeTokenizer,
    text: &str,
    conditioning: &[(String, String)],
) -> Result<PreparedInput> {
    let (normalized, tokens, offsets) = normalized_words(text)?;
    let labels = conditioning.iter().map(|x| x.0.clone()).collect::<Vec<_>>();
    let descriptions = conditioning.to_vec();
    let schema = build_entities_schema_tokens_with_descriptions(&labels, None, &descriptions);
    let marker_words = schema
        .iter()
        .enumerate()
        .filter(|(_, v)| v.as_str() == "[E]")
        .map(|(i, _)| i)
        .collect::<Vec<_>>();
    ensure!(marker_words.len() == conditioning.len(), "query_structure");
    let text_base = schema.len() + 1;
    let formatted = format_input_with_mapping(tokenizer, &[schema], &tokens)?;
    let length = formatted.input_ids.len();
    ensure!((1..=512).contains(&length), "encoded_bound");

    let mut text_indices = Vec::new();
    for word in 0..tokens.len() {
        text_indices.push(
            formatted
                .mapped_indices
                .iter()
                .position(|m| m.segment == Segment::Text && m.orig_idx >= text_base + word)
                .unwrap_or(length),
        );
    }
    let mut query_indices = Vec::new();
    for word in marker_words {
        let indices = formatted
            .mapped_indices
            .iter()
            .enumerate()
            .filter(|(_, m)| m.segment == Segment::Schema && m.orig_idx == word)
            .map(|(i, _)| i)
            .collect::<Vec<_>>();
        ensure!(
            indices.len() == 1 && formatted.subwords[indices[0]] == "[E]",
            "query_route"
        );
        query_indices.push(indices[0]);
    }

    Ok(PreparedInput {
        normalized,
        tokens,
        offsets,
        formatted,
        text_indices,
        query_indices,
    })
}

impl EntityAdapter {
    pub fn load(context: &Context, component: Value, control: &Control) -> Result<Self> {
        control.check()?;
        supported_adapter(&component["adapter"])?;
        let cap = context
            .adapters
            .iter()
            .find(|cap| cap["adapter"] == component["adapter"])
            .ok_or_else(|| anyhow::anyhow!("incompatible"))?;
        ensure!(
            component["tasks"].as_array().is_some_and(|a| a.len() == 1
                && a[0]["task"] == "concept_mentions"
                && a[0]["task_contract"] == "concept_mentions.v1"),
            "incompatible"
        );
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
                    .is_some_and(|n| n <= cap[field].as_u64().unwrap_or(0)),
                "resource_limit"
            );
        }
        ensure!(
            component["runtime_requirements"]["sha256"] == cap["runtime_requirements_sha256"]
                && component["inference_threads"] == 2
                && component["max_candidates"]
                    .as_u64()
                    .is_some_and(|n| n >= 192),
            "incompatible"
        );
        let ids = component["artifact_ids"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
        let configs = ids
            .iter()
            .filter_map(|id| context.files.get(id.as_str()?))
            .filter(|f| {
                f.artifact.role == "configuration"
                    && f.artifact.format_id == "boundary-entity-config.v1"
            })
            .collect::<Vec<_>>();
        ensure!(configs.len() == 1, "artifact_mismatch");
        ensure!(configs[0].artifact.size_bytes <= 65536, "resource_limit");
        let bytes = std::fs::read(&configs[0].path)?;
        ensure!(bytes.len() <= 65536, "resource_limit");
        let cfg: Config = serde_json::from_value(wire::parse(&bytes)?)?;
        ensure!(
            cfg.schema == "boundary-entity-config.v1"
                && cfg.decoding.overlap == "flat"
                && (0.0..=1.).contains(&cfg.decoding.threshold)
                && (0.0..=1.).contains(&cfg.decoding.abstention_threshold)
                && cfg.decoding.pair_temperature.is_finite()
                && cfg.decoding.pair_temperature > 0.
                && (1..=512).contains(&cfg.decoding.max_encoded_tokens)
                && (1..=512).contains(&cfg.decoding.max_words),
            "incompatible"
        );
        let policies = [
            "runtime_requirements",
            "preprocessing",
            "windowing",
            "decoding",
            "output_mapping",
            "decision",
        ];
        ensure!(
            component["decision"]["policy_id"] == "retained-predictions.v1",
            "incompatible"
        );
        ensure!(
            cfg.policies.len() == policies.len()
                && policies
                    .iter()
                    .all(|k| cfg.policies.get(*k) == Some(&component[*k])),
            "binding_mismatch"
        );
        let conditioning = bind_labels(&component, &cfg.labels)?;
        let score = &component["tasks"][0]["score_policy"];
        ensure!(
            (score["kind"] == "probability" || score["kind"] == "none")
                && score["calibration"] == "not_calibrated"
                && score["calibration_sha256"].is_null(),
            "incompatible"
        );
        let expected = [
            cfg.graphs.encoder.as_str(),
            cfg.graphs.marginals.as_str(),
            cfg.graphs.scorer.as_str(),
            cfg.tokenizer.as_str(),
            configs[0].artifact.artifact_id.as_str(),
        ]
        .into_iter()
        .collect::<std::collections::BTreeSet<_>>();
        ensure!(
            expected.len() == 5
                && ids.len() == 5
                && ids
                    .iter()
                    .all(|id| id.as_str().is_some_and(|id| expected.contains(id))),
            "artifact_mismatch"
        );
        let graph = |name: &str| -> Result<std::path::PathBuf> {
            let path = context.artifact_path(name, "weights")?;
            ensure!(
                context.files[name].artifact.format_id == "onnx-boundary-entity.v1",
                "incompatible"
            );
            crate::onnx::check(&path, control)?;
            Ok(path)
        };
        let encoder_path = graph(&cfg.graphs.encoder)?;
        control.check()?;
        let marginal_path = graph(&cfg.graphs.marginals)?;
        let scorer_path = graph(&cfg.graphs.scorer)?;
        let tokenizer_path = context.artifact_path(&cfg.tokenizer, "tokenizer")?;
        ensure!(
            context.files[&cfg.tokenizer].artifact.format_id == "huggingface-tokenizer-json.v1",
            "incompatible"
        );
        crate::tokenizer_metadata::check(&context.files[&cfg.tokenizer], control)?;
        let options = crate::runtime::options(context)?;
        control.check()?;
        let tokenizer = RuntimeTokenizer::from_file(&tokenizer_path)?;
        let encoder = Encoder::new_with_options(encoder_path, options)?;
        control.check()?;
        let marginal = MarginalModel::new_with_options(marginal_path, options)?;
        control.check()?;
        let scorer = ScorerModel::new_with_options(scorer_path, options)?;
        control.check()?;
        Ok(Self {
            tokenizer,
            encoder,
            marginal,
            scorer,
            config: cfg,
            conditioning,
            component,
        })
    }
    pub fn extract(&self, text: &str, control: &Control) -> Result<Vec<Value>> {
        control.check()?;
        let PreparedInput {
            normalized,
            tokens,
            offsets,
            formatted,
            text_indices,
            query_indices,
            ..
        } = prepare_input(&self.tokenizer, text, &self.conditioning)?;
        let length = formatted.input_ids.len();
        ensure!(
            length <= self.config.decoding.max_encoded_tokens
                && tokens.len() <= self.config.decoding.max_words,
            "encoded_bound"
        );
        let gathered = gather_text_indices(&text_indices, length)?;
        let hidden = self.encoder.infer(
            Array2::from_shape_vec((1, length), formatted.input_ids)?,
            Array2::from_elem((1, length), 1i64),
        )?;
        ensure!(
            hidden.dim() == (1, length, 768) && hidden.iter().all(|v| v.is_finite()),
            "encoder_output"
        );
        control.check()?;
        let queries = self.conditioning.len();
        let text_states = hidden.select(Axis(1), &gathered);
        let query_states = hidden.select(Axis(1), &query_indices);
        let text_mask = Array2::from_elem((1, tokens.len()), true);
        let query_mask = Array2::from_elem((1, queries), true);
        let m = self.marginal.infer(MarginalInput {
            text_states: text_states.clone(),
            text_mask: text_mask.clone(),
            query_states: query_states.clone(),
            query_mask: query_mask.clone(),
        })?;
        control.check()?;
        let pool = build_shared_candidate_pool(
            m.boundary_mask.index_axis(Axis(0), 0),
            query_mask.index_axis(Axis(0), 0),
            m.start_logits.index_axis(Axis(0), 0),
            m.end_logits.index_axis(Axis(0), 0),
            m.start_all.index_axis(Axis(0), 0),
            m.end_all.index_axis(Axis(0), 0),
            PoolConfig::default(),
        )?;
        control.check()?;
        let scored = self.scorer.infer(ScorerInput {
            boundary_states: m.boundary_states,
            text_states,
            text_mask,
            query_states,
            query_mask,
            start_logits: m.start_logits,
            end_logits: m.end_logits,
            inside_prefix: m.inside_prefix,
            inside_prefix_mean: m.inside_prefix_mean,
            candidate_indices: pool.indices.clone().insert_axis(Axis(0)),
            candidate_mask: pool.mask.clone().insert_axis(Axis(0)),
            candidate_compat: pool.compat_logits.clone().insert_axis(Axis(0)),
        })?;
        control.check()?;
        let indices = pool
            .indices
            .rows()
            .into_iter()
            .map(|r| Ok([usize::try_from(r[0])?, usize::try_from(r[1])?]))
            .collect::<Result<Vec<_>>>()?;
        let rows = (0..queries)
            .map(|q| QueryScores {
                indices: indices.clone(),
                valid_mask: pool.mask.to_vec(),
                pair_logits: scored.pair_logits.slice(s![0, q, ..]).to_vec(),
                query_valid: true,
                threshold: self.config.decoding.threshold,
                null_logit: Some(scored.null_logits[[0, q]]),
            })
            .collect::<Vec<_>>();
        let groups = group_scored_candidates(
            &rows,
            self.config.decoding.pair_temperature,
            self.config.decoding.abstention_threshold,
        )?;
        let mut findings = Vec::new();
        let mut seen = std::collections::BTreeSet::new();
        for (q, group) in groups.iter().enumerate() {
            for span in resolve_overlaps(group, OverlapPolicy::Disallow)? {
                control.check()?;
                let mapped = map_half_open_utf8(&normalized, &offsets, span.start, span.end)?;
                let (start, end) = source_coordinates(text, mapped.start, mapped.end)?;
                ensure!(
                    text[mapped.start..mapped.end] == mapped.text,
                    "source_surface"
                );
                let concept = &self.config.labels[q].concept_id;
                if !enabled_concept(&self.component, concept) {
                    continue;
                }
                if seen.insert((concept.clone(), start, end)) {
                    findings.push(json!({"concept_id":concept,"span":{"start":start,"end":end},"score":if self.component["tasks"][0]["score_policy"]["kind"]=="none"{Value::Null}else{json!({"kind":"probability","value":span.confidence})}}));
                }
                ensure!(
                    findings.len()
                        <= self.component["max_output_findings"].as_u64().unwrap_or(0) as usize
                        && findings.len() <= 512,
                    "output_bound"
                );
            }
        }
        Ok(findings)
    }
}

fn enabled_concept(component: &Value, concept: &str) -> bool {
    component["tasks"][0]["enabled_ids"]
        .as_array()
        .is_some_and(|ids| ids.iter().any(|id| id == concept))
}
#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (Value, Vec<Mapping>) {
        let component = json!({"executed_labels":[
            {"task":"concept_mentions","label_id":"opaque.7","description":"First"},
            {"task":"concept_mentions","label_id":"opaque.9","description":"Second"}],
            "tasks":[{"task":"concept_mentions","task_contract":"concept_mentions.v1",
                "enabled_ids":["opaque.9"]}]});
        let mappings = serde_json::from_value(json!([
            {"label_id":"opaque.7","concept_id":"opaque.7","model_label":"one"},
            {"label_id":"opaque.9","concept_id":"opaque.9","model_label":"two"}]))
        .unwrap();
        (component, mappings)
    }
    #[test]
    fn binding_preserves_full_model_conditioning_for_output_subsets() {
        let (mut component, mut mappings) = fixture();
        let expected = vec![
            ("one".to_owned(), "First".to_owned()),
            ("two".to_owned(), "Second".to_owned()),
        ];
        for enabled in [
            json!([]),
            json!(["opaque.9"]),
            json!(["opaque.7", "opaque.9"]),
        ] {
            component["tasks"][0]["enabled_ids"] = enabled;
            assert_eq!(bind_labels(&component, &mappings).unwrap(), expected);
        }
        component["executed_labels"][0]["label_id"] = json!("other.1");
        mappings[0].label_id = "other.1".into();
        mappings[0].concept_id = "other.1".into();
        component["tasks"][0]["enabled_ids"] = json!([]);
        assert_eq!(bind_labels(&component, &mappings).unwrap(), expected);
        assert!(!enabled_concept(&component, "other.1"));
    }
    #[test]
    fn invalid_bindings_fail_without_model_construction() {
        for case in 0..12 {
            let (mut c, mut m) = fixture();
            match case {
                0 => m[1].model_label = m[0].model_label.clone(),
                1 => m[1].label_id = m[0].label_id.clone(),
                2 => m[0].concept_id = "opaque.9".into(),
                3 => m[0].model_label = " ".into(),
                4 => m[0].model_label = "x".repeat(257),
                5 => {
                    m.pop();
                }
                6 => c["executed_labels"][0]["task"] = json!("other"),
                7 => c["tasks"][0]["enabled_ids"] = json!(["missing"]),
                8 => c["tasks"][0]["enabled_ids"] = json!(["opaque.9", "opaque.9"]),
                9 => m.swap(0, 1),
                10 => c["tasks"][0]["task"] = json!("other"),
                _ => m[0].label_id.clear(),
            }
            assert!(bind_labels(&c, &m).is_err(), "case {case}");
        }
        assert!(
            serde_json::from_value::<Mapping>(json!({
            "label_id":"opaque.7", "concept_id":"opaque.7"}))
            .is_err()
        );
        assert!(
            serde_json::from_value::<Mapping>(json!({
            "label_id":"opaque.7", "concept_id":"opaque.7", "model_label":"one",
            "extra":true}))
            .is_err()
        );
    }
    #[test]
    fn terminal_gather_preserves_raw_routes() {
        let raw = vec![0, 2, 3];
        assert_eq!(gather_text_indices(&raw, 3).unwrap(), vec![0, 2, 2]);
        assert_eq!(raw, vec![0, 2, 3]);
    }
    #[test]
    fn original_coordinates_do_not_clip_synthetic_suffix() {
        assert_eq!(source_coordinates("\u{1f600} echo", 5, 9).unwrap(), (2, 6));
        assert!(source_coordinates("echo", 0, 5).is_err());
    }
}
