//! Profile-bound full-message encoder classification on the shared CPU runtime.
use crate::{
    context::{Artifact, Context},
    entity::Control,
    wire,
};
use anyhow::{Result, ensure};
use ort::{
    session::{Session, builder::GraphOptimizationLevel},
    value::{Tensor, TensorElementType, ValueType},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Mutex,
};
use tokenizers::{Encoding, Tokenizer};

pub const ADAPTER: &str = "encoder_topics";
pub const ARCHITECTURE: &str = "encoder-cls-v1";
pub const FORMAT: &str = "onnx-encoder-classification.v1";
const CONFIG: &str = "encoder-topic-config.v1";
const INPUTS: [&str; 3] = ["input_ids", "attention_mask", "token_type_ids"];
const OUTPUTS: [&str; 2] = ["raw_cls", "logits"];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Label {
    label_id: String,
    topic_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Decoding {
    hidden_size: usize,
    max_encoded_tokens: usize,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    schema: String,
    graph: String,
    tokenizer: String,
    labels: Vec<Label>,
    policies: BTreeMap<String, Value>,
    decoding: Decoding,
}

fn config(component: &Value, bytes: &[u8]) -> Result<Config> {
    ensure!(bytes.len() <= 65536, "resource_limit");
    let cfg: Config = serde_json::from_value(wire::parse(bytes)?)?;
    ensure!(
        cfg.schema == CONFIG
            && (1..=64).contains(&cfg.labels.len())
            && (1..=4096).contains(&cfg.decoding.hidden_size)
            && (2..=512).contains(&cfg.decoding.max_encoded_tokens),
        "incompatible"
    );
    let tasks = component["tasks"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
    ensure!(
        tasks.len() == 1
            && tasks[0]["task"] == "message_topics"
            && tasks[0]["task_contract"] == "message_topics.v1",
        "incompatible"
    );
    let score = &tasks[0]["score_policy"];
    ensure!(
        (score["kind"] == "none" || score["kind"] == "probability")
            && score["calibration"] == "not_calibrated"
            && score["calibration_sha256"].is_null(),
        "incompatible"
    );
    let executed = component["executed_labels"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
    ensure!(executed.len() == cfg.labels.len(), "binding_mismatch");
    let mut ids = BTreeSet::new();
    for (mapping, label) in cfg.labels.iter().zip(executed) {
        wire::id(&json!(mapping.label_id))?;
        ensure!(
            mapping.label_id == mapping.topic_id
                && label["task"] == "message_topics"
                && label["label_id"] == mapping.label_id
                && ids.insert(mapping.label_id.as_str()),
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
            .is_some_and(|id| ids.contains(id) && seen.insert(id))),
        "binding_mismatch"
    );
    let policies = [
        ("preprocessing", "full-message-tokenizer.v1"),
        ("windowing", "single-message-no-truncation.v1"),
        ("decoding", "linear-logits-uncertainty.v1"),
        ("output_mapping", "ordered-topic-mapping.v1"),
        ("decision", "inclusive-zero-uncertainty.v1"),
    ];
    ensure!(
        cfg.policies.len() == 6
            && cfg.policies.get("runtime_requirements") == Some(&component["runtime_requirements"]),
        "binding_mismatch"
    );
    for (name, id) in policies {
        ensure!(
            component[name]["policy_id"] == id && cfg.policies.get(name) == Some(&component[name]),
            "binding_mismatch"
        );
    }
    ensure!(
        cfg.labels.len() <= component["max_candidates"].as_u64().unwrap_or(0) as usize,
        "resource_limit"
    );
    Ok(cfg)
}

fn artifacts(
    component: &Value,
    cfg: &Config,
    config_id: &str,
    files: &BTreeMap<String, Artifact>,
) -> Result<()> {
    let required = [
        (cfg.graph.as_str(), "weights", FORMAT),
        (
            cfg.tokenizer.as_str(),
            "tokenizer",
            "huggingface-tokenizer-json.v1",
        ),
        (config_id, "configuration", CONFIG),
    ];
    let actual = component["artifact_ids"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
    let expected: BTreeSet<_> = required.iter().map(|(id, _, _)| *id).collect();
    ensure!(
        expected.len() == 3
            && actual.len() == 3
            && actual
                .iter()
                .filter_map(Value::as_str)
                .collect::<BTreeSet<_>>()
                == expected,
        "artifact_mismatch"
    );
    for (id, role, format) in required {
        wire::id(&json!(id))?;
        let file = files
            .get(id)
            .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
        ensure!(
            file.role == role && file.format_id == format,
            "artifact_mismatch"
        );
    }
    Ok(())
}

fn tensor_type(value: &ValueType, ty: TensorElementType, expected: &[i64]) -> Result<()> {
    match value {
        ValueType::Tensor {
            ty: actual, shape, ..
        } => ensure!(*actual == ty && &shape[..] == expected, "graph_signature"),
        _ => anyhow::bail!("graph_signature"),
    }
    Ok(())
}

fn signature(session: &Session, cfg: &Config) -> Result<()> {
    let inputs = session
        .inputs()
        .iter()
        .map(|v| (v.name(), v.dtype()))
        .collect::<Vec<_>>();
    let outputs = session
        .outputs()
        .iter()
        .map(|v| (v.name(), v.dtype()))
        .collect::<Vec<_>>();
    signature_metadata(&inputs, &outputs, cfg)
}

fn signature_metadata(
    inputs: &[(&str, &ValueType)],
    outputs: &[(&str, &ValueType)],
    cfg: &Config,
) -> Result<()> {
    ensure!(
        inputs.iter().map(|v| v.0).collect::<Vec<_>>() == INPUTS
            && outputs.iter().map(|v| v.0).collect::<Vec<_>>() == OUTPUTS,
        "graph_signature"
    );
    for input in inputs {
        tensor_type(input.1, TensorElementType::Int64, &[1, -1])?;
    }
    tensor_type(
        outputs[0].1,
        TensorElementType::Float32,
        &[1, cfg.decoding.hidden_size as i64],
    )?;
    tensor_type(
        outputs[1].1,
        TensorElementType::Float32,
        &[1, cfg.labels.len() as i64 + 1],
    )
}

fn encoding(e: &Encoding, max: usize) -> Result<()> {
    let n = e.get_ids().len();
    ensure!(
        (2..=max).contains(&n) && e.get_overflowing().is_empty(),
        "encoded_bound"
    );
    ensure!(
        e.get_attention_mask().len() == n
            && e.get_type_ids().len() == n
            && e.get_attention_mask().iter().all(|v| *v == 1),
        "tokenizer_output"
    );
    Ok(())
}

fn tokenizer_policy(tokenizer: &Tokenizer) -> Result<()> {
    ensure!(
        tokenizer.get_truncation().is_none() && tokenizer.get_padding().is_none(),
        "incompatible"
    );
    Ok(())
}

fn examine(
    text: &str,
    control: &Control,
    infer: impl FnOnce() -> Result<Vec<Value>>,
) -> Result<Vec<Value>> {
    control.check()?;
    if text.trim().is_empty() {
        return Ok(Vec::new());
    }
    let result = infer();
    control.check()?;
    result
}

fn outputs(
    cfg: &Config,
    raw_shape: &[i64],
    raw: &[f32],
    log_shape: &[i64],
    logits: &[f32],
) -> Result<()> {
    ensure!(
        raw_shape == [1, cfg.decoding.hidden_size as i64]
            && raw.len() == cfg.decoding.hidden_size
            && log_shape == [1, cfg.labels.len() as i64 + 1]
            && logits.len() == cfg.labels.len() + 1
            && raw.iter().chain(logits).all(|v| v.is_finite()),
        "classifier_output"
    );
    Ok(())
}

fn decode(component: &Value, cfg: &Config, logits: &[f32]) -> Result<Vec<Value>> {
    ensure!(
        logits.len() == cfg.labels.len() + 1 && logits.iter().all(|v| v.is_finite()),
        "classifier_output"
    );
    ensure!(logits[cfg.labels.len()] < 0., "semantic_uncertainty");
    let enabled = component["tasks"][0]["enabled_ids"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("binding_mismatch"))?;
    let scores = component["tasks"][0]["score_policy"]["kind"] == "probability";
    let mut findings = Vec::new();
    for (mapping, &logit) in cfg.labels.iter().zip(logits) {
        if logit >= 0. && enabled.iter().any(|v| v == &mapping.topic_id) {
            let score = if logit >= 0. {
                1. / (1. + (-logit).exp())
            } else {
                logit.exp() / (1. + logit.exp())
            };
            findings.push(json!({"topic_id":mapping.topic_id,"score":if scores {json!({"kind":"probability","value":score})} else {Value::Null}}));
        }
    }
    ensure!(
        findings.len() <= component["max_output_findings"].as_u64().unwrap_or(0) as usize,
        "output_bound"
    );
    Ok(findings)
}

pub struct EncoderTopicAdapter {
    session: Mutex<Session>,
    tokenizer: Tokenizer,
    config: Config,
    component: Value,
}
impl EncoderTopicAdapter {
    pub fn load(context: &Context, component: Value, control: &Control) -> Result<Self> {
        control.check()?;
        crate::context::supported_adapter(&component["adapter"])?;
        ensure!(
            component["adapter"]["adapter_id"] == ADAPTER,
            "incompatible"
        );
        let cap = context
            .adapters
            .iter()
            .find(|v| v["adapter"] == component["adapter"])
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
                    .is_some_and(|v| v > 0 && v <= cap[field].as_u64().unwrap_or(0)),
                "resource_limit"
            );
        }
        ensure!(
            component["max_windows"] == 1
                && component["inference_threads"] == 2
                && component["runtime_requirements"]["sha256"]
                    == cap["runtime_requirements_sha256"],
            "incompatible"
        );
        let ids = component["artifact_ids"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
        let configs = ids
            .iter()
            .filter_map(|id| context.files.get(id.as_str()?))
            .filter(|v| v.artifact.role == "configuration" && v.artifact.format_id == CONFIG)
            .collect::<Vec<_>>();
        ensure!(
            configs.len() == 1 && configs[0].artifact.size_bytes <= 65536,
            "artifact_mismatch"
        );
        let cfg = config(&component, &std::fs::read(&configs[0].path)?)?;
        artifacts(
            &component,
            &cfg,
            &configs[0].artifact.artifact_id,
            &context
                .files
                .iter()
                .map(|(id, f)| (id.clone(), f.artifact.clone()))
                .collect(),
        )?;
        crate::tokenizer_metadata::check(&context.files[&cfg.tokenizer], control)?;
        let tokenizer = Tokenizer::from_file(context.artifact_path(&cfg.tokenizer, "tokenizer")?)
            .map_err(|_| anyhow::anyhow!("tokenizer_output"))?;
        tokenizer_policy(&tokenizer)?;
        let graph = context.artifact_path(&cfg.graph, "weights")?;
        crate::onnx::check(&graph, control)?;
        crate::runtime::options(context)?;
        control.check()?;
        let session = Session::builder()?
            .with_no_environment_execution_providers()?
            .with_parallel_execution(false)?
            .with_optimization_level(GraphOptimizationLevel::Level3)?
            .commit_from_file(graph)?;
        control.check()?;
        signature(&session, &cfg)?;
        Ok(Self {
            session: Mutex::new(session),
            tokenizer,
            config: cfg,
            component,
        })
    }
    pub fn extract(&self, text: &str, control: &Control) -> Result<Vec<Value>> {
        examine(text, control, || {
            let e = self
                .tokenizer
                .encode(text, true)
                .map_err(|_| anyhow::anyhow!("tokenizer_output"))?;
            control.check()?;
            encoding(&e, self.config.decoding.max_encoded_tokens)?;
            let n = e.get_ids().len();
            let tensor = |values: &[u32]| -> Result<_> {
                Ok(Tensor::from_array((
                    [1, n],
                    values.iter().map(|v| i64::from(*v)).collect::<Vec<_>>(),
                ))?)
            };
            let inputs = ort::inputs! {"input_ids"=>tensor(e.get_ids())?,"attention_mask"=>tensor(e.get_attention_mask())?,"token_type_ids"=>tensor(e.get_type_ids())?};
            let mut session = self
                .session
                .lock()
                .map_err(|_| anyhow::anyhow!("worker_failure"))?;
            control.check()?;
            let result = session.run(inputs)?;
            control.check()?;
            let (raw_shape, raw) = result["raw_cls"].try_extract_tensor::<f32>()?;
            let (log_shape, logits) = result["logits"].try_extract_tensor::<f32>()?;
            outputs(&self.config, raw_shape, raw, log_shape, logits)?;
            decode(&self.component, &self.config, logits)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::{
            Arc,
            atomic::{AtomicBool, Ordering},
        },
        time::{Duration, Instant},
    };

    fn example() -> (Value, Value) {
        let mut c = json!({"tasks":[{"task":"message_topics","task_contract":"message_topics.v1","enabled_ids":["topic.a","topic.b"],
            "score_policy":{"kind":"probability","calibration":"not_calibrated","calibration_sha256":null}}],
            "executed_labels":[{"task":"message_topics","label_id":"topic.a","description":"A"},{"task":"message_topics","label_id":"topic.b","description":"B"}],
            "max_candidates":64,"max_output_findings":64,"artifact_ids":["graph","tokenizer","config"]});
        for (name, id) in [
            ("runtime_requirements", "cpu.v1"),
            ("preprocessing", "full-message-tokenizer.v1"),
            ("windowing", "single-message-no-truncation.v1"),
            ("decoding", "linear-logits-uncertainty.v1"),
            ("output_mapping", "ordered-topic-mapping.v1"),
            ("decision", "inclusive-zero-uncertainty.v1"),
        ] {
            c[name] = json!({"policy_id":id,"revision":"1","sha256":"a".repeat(64)});
        }
        let policies: [(String, Value); 6] = [
            "runtime_requirements",
            "preprocessing",
            "windowing",
            "decoding",
            "output_mapping",
            "decision",
        ]
        .map(|k| (k.to_owned(), c[k].clone()));
        let cfg = json!({"schema":CONFIG,"graph":"graph","tokenizer":"tokenizer",
            "labels":[{"label_id":"topic.a","topic_id":"topic.a"},{"label_id":"topic.b","topic_id":"topic.b"}],
            "policies":policies.into_iter().collect::<serde_json::Map<_,_>>(),"decoding":{"hidden_size":4,"max_encoded_tokens":512}});
        (c, cfg)
    }
    fn parsed(c: &Value, v: &Value) -> Result<Config> {
        config(c, &serde_json::to_vec(v)?)
    }
    fn control() -> Control {
        Control {
            cancelled: Arc::new(AtomicBool::new(false)),
            deadline: Instant::now() + Duration::from_secs(1),
        }
    }

    #[test]
    fn capability_binds_encoder_architecture_and_topic_task_only() {
        let adapter = json!({"adapter_id":ADAPTER,"adapter_version":"1","adapter_build_sha256":"a".repeat(64),
            "abi":"nlp-adapter.v1","architecture":ARCHITECTURE,"artifact_format":FORMAT,"tokenizer_format":"huggingface-tokenizer-json.v1"});
        let cap = json!({"adapter":adapter,"task_contracts":["message_topics.v1"],"score_kinds":["none","probability"],
            "max_source_codepoints":65536,"max_batch_items":1,"max_windows":1,"max_candidates":64,"max_output_findings":64,"runtime_requirements_sha256":"a".repeat(64)});
        assert!(crate::context::capability(&cap).is_ok());
        let mut wrong = cap.clone();
        wrong["adapter"]["architecture"] = json!("boundary-shared-v1");
        assert!(crate::context::capability(&wrong).is_err());
        let mut wrong = cap;
        wrong["task_contracts"] = json!(["concept_mentions.v1"]);
        assert!(crate::context::capability(&wrong).is_err());
    }

    #[test]
    fn config_rejects_mismatched_order_policy_dimensions_and_enabled_ids() {
        let (c, v) = example();
        assert!(parsed(&c, &v).is_ok());
        for fault in 0..9 {
            let mut c = c.clone();
            let mut v = v.clone();
            match fault {
                0 => v["labels"].as_array_mut().unwrap().swap(0, 1),
                1 => v["policies"]["preprocessing"]["sha256"] = json!("b".repeat(64)),
                2 => v["decoding"]["hidden_size"] = json!(0),
                3 => v["decoding"]["max_encoded_tokens"] = json!(513),
                4 => c["tasks"][0]["enabled_ids"] = json!(["missing"]),
                5 => c["tasks"][0]["enabled_ids"] = json!(["topic.a", "topic.a"]),
                6 => c["tasks"][0]["score_policy"]["calibration"] = json!("calibrated"),
                7 => v["decoding"]["threshold"] = json!(0.1),
                _ => v["labels"][1]["topic_id"] = json!("topic.a"),
            }
            assert!(parsed(&c, &v).is_err(), "fault {fault}");
        }
    }

    #[test]
    fn artifact_roles_and_exact_component_membership_are_bound() {
        let (c, v) = example();
        let cfg = parsed(&c, &v).unwrap();
        let mut files = BTreeMap::new();
        for (id, role, format) in [
            ("graph", "weights", FORMAT),
            ("tokenizer", "tokenizer", "huggingface-tokenizer-json.v1"),
            ("config", "configuration", CONFIG),
        ] {
            files.insert(
                id.into(),
                Artifact {
                    artifact_id: id.into(),
                    sha256: "a".repeat(64),
                    size_bytes: 1,
                    role: role.into(),
                    format_id: format.into(),
                },
            );
        }
        assert!(artifacts(&c, &cfg, "config", &files).is_ok());
        files.get_mut("graph").unwrap().format_id = "onnx-boundary-classification.v1".into();
        assert!(artifacts(&c, &cfg, "config", &files).is_err());
        let mut extra = c;
        extra["artifact_ids"] = json!(["graph", "tokenizer", "config", "other"]);
        assert!(artifacts(&extra, &cfg, "config", &files).is_err());
    }

    #[test]
    fn raw_zero_decisions_precede_probability_rounding_and_enabled_mask() {
        let (mut c, v) = example();
        let cfg = parsed(&c, &v).unwrap();
        assert!(
            decode(&c, &cfg, &[-f32::MIN_POSITIVE, -1., -1.])
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            decode(&c, &cfg, &[0., -1., -1.]).unwrap()[0]["topic_id"],
            "topic.a"
        );
        c["tasks"][0]["enabled_ids"] = json!(["topic.b"]);
        let found = decode(&c, &cfg, &[1000., 0., -1.]).unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0]["topic_id"], "topic.b");
        assert_eq!(found[0]["score"]["value"], 0.5);
        c["tasks"][0]["score_policy"]["kind"] = json!("none");
        assert!(decode(&c, &cfg, &[1., 1., -1.]).unwrap()[0]["score"].is_null());
        assert_eq!(
            decode(&c, &cfg, &[1., 1., 0.]).unwrap_err().to_string(),
            "semantic_uncertainty"
        );
        c["tasks"][0]["enabled_ids"] = json!([]);
        assert_eq!(
            decode(&c, &cfg, &[-1., -1., 1.]).unwrap_err().to_string(),
            "semantic_uncertainty"
        );
    }

    #[test]
    fn wrong_shapes_and_nonfinite_features_or_logits_fail() {
        let (c, v) = example();
        let cfg = parsed(&c, &v).unwrap();
        assert!(outputs(&cfg, &[1, 4], &[0.; 4], &[1, 3], &[0.; 3]).is_ok());
        assert!(outputs(&cfg, &[2, 2], &[0.; 4], &[1, 3], &[0.; 3]).is_err());
        assert!(outputs(&cfg, &[1, 4], &[f32::NAN; 4], &[1, 3], &[0.; 3]).is_err());
        assert!(outputs(&cfg, &[1, 4], &[0.; 4], &[1, 3], &[f32::INFINITY; 3]).is_err());
        assert!(decode(&c, &cfg, &[0.; 2]).is_err());
        assert!(decode(&c, &cfg, &[0., f32::NAN, -1.]).is_err());
        assert!(
            tensor_type(
                &ValueType::Tensor {
                    ty: TensorElementType::Float32,
                    shape: vec![2, 4].into(),
                    dimension_symbols: Default::default()
                },
                TensorElementType::Float32,
                &[1, 4]
            )
            .is_err()
        );
    }

    fn tokenizer() -> Tokenizer {
        use tokenizers::{
            models::wordlevel::WordLevel, pre_tokenizers::whitespace::Whitespace,
            processors::bert::BertProcessing,
        };
        let vocab = [
            ("[UNK]", 0),
            ("[CLS]", 1),
            ("[SEP]", 2),
            ("hello", 3),
            ("café", 4),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect();
        let mut t = Tokenizer::new(
            WordLevel::builder()
                .vocab(vocab)
                .unk_token("[UNK]".into())
                .build()
                .unwrap(),
        );
        t.with_pre_tokenizer(Some(Whitespace::default()));
        t.with_post_processor(Some(BertProcessing::new(
            ("[SEP]".into(), 2),
            ("[CLS]".into(), 1),
        )));
        t
    }

    #[test]
    fn full_message_encoding_retains_specials_and_rejects_padding_truncation_or_overflow() {
        let mut t = tokenizer();
        let e = t.encode("hello\n café", true).unwrap();
        assert_eq!(e.get_ids(), &[1, 3, 4, 2]);
        assert!(encoding(&e, 4).is_ok());
        assert!(encoding(&e, 3).is_err());
        assert!(tokenizer_policy(&t).is_ok());
        t.with_padding(Some(Default::default()));
        assert!(tokenizer_policy(&t).is_err());
        t.with_padding(None);
        t.with_truncation(Some(tokenizers::TruncationParams {
            max_length: 3,
            ..Default::default()
        }))
        .unwrap();
        assert!(tokenizer_policy(&t).is_err());
        let truncated = t.encode("hello café hello", true).unwrap();
        assert!(encoding(&truncated, 512).is_err());
    }

    #[test]
    fn bound_template_nonzero_type_ids_are_retained() {
        use tokenizers::processors::template::TemplateProcessing;
        let mut t = tokenizer();
        t.with_post_processor(Some(
            TemplateProcessing::builder()
                .try_single("[CLS]:0 $A:1 [SEP]:1")
                .unwrap()
                .special_tokens(vec![("[CLS]", 1), ("[SEP]", 2)])
                .build()
                .unwrap(),
        ));
        let e = t.encode("hello café", true).unwrap();
        assert!(tokenizer_policy(&t).is_ok());
        assert!(encoding(&e, 512).is_ok());
        assert_eq!(e.get_ids(), &[1, 3, 4, 2]);
        assert_eq!(e.get_type_ids(), &[0, 1, 1, 1]);
        assert_eq!(e.get_attention_mask(), &[1, 1, 1, 1]);
    }

    #[test]
    fn graph_metadata_rejects_wrong_names_order_types_and_dimensions() {
        let (c, v) = example();
        let cfg = parsed(&c, &v).unwrap();
        let tensor = |ty, shape: Vec<i64>| ValueType::Tensor {
            ty,
            shape: shape.into(),
            dimension_symbols: Default::default(),
        };
        let input = tensor(TensorElementType::Int64, vec![1, -1]);
        let raw = tensor(TensorElementType::Float32, vec![1, 4]);
        let logits = tensor(TensorElementType::Float32, vec![1, 3]);
        let inputs = [
            ("input_ids", &input),
            ("attention_mask", &input),
            ("token_type_ids", &input),
        ];
        let outputs = [("raw_cls", &raw), ("logits", &logits)];
        assert!(signature_metadata(&inputs, &outputs, &cfg).is_ok());
        let mut wrong = inputs;
        wrong[0].0 = "ids";
        assert!(signature_metadata(&wrong, &outputs, &cfg).is_err());
        let mut wrong = inputs;
        wrong.swap(0, 2);
        assert!(signature_metadata(&wrong, &outputs, &cfg).is_err());
        let mut wrong = outputs;
        wrong.swap(0, 1);
        assert!(signature_metadata(&inputs, &wrong, &cfg).is_err());
        assert!(signature_metadata(&inputs[..2], &outputs, &cfg).is_err());
        for wrong_type in [
            tensor(TensorElementType::Float32, vec![1, -1]),
            tensor(TensorElementType::Int64, vec![-1, -1]),
            tensor(TensorElementType::Int64, vec![2, -1]),
            tensor(TensorElementType::Int64, vec![1, 512]),
            tensor(TensorElementType::Int64, vec![1, -1, 1]),
        ] {
            let mut wrong = inputs;
            wrong[1].1 = &wrong_type;
            assert!(signature_metadata(&wrong, &outputs, &cfg).is_err());
        }
        for wrong_type in [
            tensor(TensorElementType::Float64, vec![1, 4]),
            tensor(TensorElementType::Float32, vec![-1, 4]),
            tensor(TensorElementType::Float32, vec![1, 3]),
        ] {
            let wrong = [("raw_cls", &wrong_type), ("logits", &logits)];
            assert!(signature_metadata(&inputs, &wrong, &cfg).is_err());
        }
        let wrong_width = tensor(TensorElementType::Float32, vec![1, 2]);
        assert!(
            signature_metadata(
                &inputs,
                &[("raw_cls", &raw), ("logits", &wrong_width)],
                &cfg
            )
            .is_err()
        );
    }

    #[test]
    fn blank_input_skips_execution_and_control_failure_wins_on_return() {
        let c = control();
        for text in ["", " \n\t", "\u{a0}"] {
            assert!(
                examine(text, &c, || panic!("blank inference"))
                    .unwrap()
                    .is_empty()
            );
        }
        c.cancelled.store(true, Ordering::SeqCst);
        assert_eq!(
            examine("", &c, || Ok(vec![])).unwrap_err().to_string(),
            "cancelled"
        );
        let c = control();
        assert_eq!(
            examine("text", &c, || {
                c.cancelled.store(true, Ordering::SeqCst);
                Ok(vec![])
            })
            .unwrap_err()
            .to_string(),
            "cancelled"
        );
        let c = Control {
            deadline: Instant::now() - Duration::from_secs(1),
            ..control()
        };
        assert_eq!(
            examine("text", &c, || panic!("expired inference"))
                .unwrap_err()
                .to_string(),
            "deadline"
        );
    }
}
