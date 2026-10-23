//! Closed private child admission. Task payloads use the existing worker ABI.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

pub const FLAG: &str = "--nlp-worker-host";
pub const FRAME_LIMIT: usize = 1_048_576;
pub const METADATA: &str = "worker/worker-release.json";
pub const WORKER: &str = "TextAnalysisWorker.exe";
pub const RUNTIME_FILES: [&str; 6] = [
    "onnxruntime.dll",
    "onnxruntime_providers_shared.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
];
pub type Result<T> = std::result::Result<T, &'static str>;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    pub artifact_id: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub role: String,
    pub format_id: String,
}
impl Artifact {
    fn valid(&self) -> bool {
        identifier(&self.artifact_id)
            && identifier(&self.format_id)
            && hex(&self.sha256, 64)
            && (1..=9_007_199_254_740_991).contains(&self.size_bytes)
            && matches!(
                self.role.as_str(),
                "engine" | "runtime" | "weights" | "tokenizer" | "configuration"
            )
    }
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub artifact: Artifact,
    pub handle: u64,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Launch {
    pub schema: String,
    pub channel_nonce: String,
    pub profile_json: String,
    pub artifacts: Vec<Binding>,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Control {
    pub schema: String,
    pub channel_nonce: String,
    pub operation: String,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Observe {
    pub schema: String,
    pub channel_nonce: String,
    pub engine: Value,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PackageArtifact {
    pub artifact: Artifact,
    pub file: String,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Metadata {
    pub schema: String,
    pub engine: Value,
    pub adapters: Vec<Value>,
    pub artifacts: Vec<PackageArtifact>,
}

pub fn requested(args: &[String]) -> bool {
    args.get(1).is_some_and(|x| x == FLAG)
}
pub fn hex(value: &str, n: usize) -> bool {
    value.len() == n
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn identifier(value: &str) -> bool {
    (1..=160).contains(&value.len())
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._:-".contains(&c))
}
pub fn arguments(args: &[String]) -> Result<([usize; 7], String)> {
    if args.len() != 10 || !requested(args) || !hex(&args[9], 32) {
        return Err("invalid_bootstrap");
    }
    let mut handles = [0; 7];
    for (i, value) in args[2..9].iter().enumerate() {
        let n: usize = value.parse().map_err(|_| "invalid_bootstrap")?;
        if n < 4 || n == usize::MAX || n.to_string() != *value || handles[..i].contains(&n) {
            return Err("invalid_bootstrap");
        }
        handles[i] = n;
    }
    Ok((handles, args[9].clone()))
}

// Value's default parser accepts duplicate members. Reject them at every level.
struct ClosedValue(Value);
impl<'de> Deserialize<'de> for ClosedValue {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct Visitor;
        impl<'de> serde::de::Visitor<'de> for Visitor {
            type Value = ClosedValue;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("finite JSON with unique members")
            }
            fn visit_bool<E: serde::de::Error>(
                self,
                v: bool,
            ) -> std::result::Result<Self::Value, E> {
                Ok(ClosedValue(v.into()))
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> std::result::Result<Self::Value, E> {
                Ok(ClosedValue(v.into()))
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> std::result::Result<Self::Value, E> {
                Ok(ClosedValue(v.into()))
            }
            fn visit_f64<E: serde::de::Error>(self, v: f64) -> std::result::Result<Self::Value, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| ClosedValue(Value::Number(n)))
                    .ok_or_else(|| E::custom("nonfinite"))
            }
            fn visit_str<E: serde::de::Error>(
                self,
                v: &str,
            ) -> std::result::Result<Self::Value, E> {
                Ok(ClosedValue(v.into()))
            }
            fn visit_string<E: serde::de::Error>(
                self,
                v: String,
            ) -> std::result::Result<Self::Value, E> {
                Ok(ClosedValue(v.into()))
            }
            fn visit_unit<E: serde::de::Error>(self) -> std::result::Result<Self::Value, E> {
                Ok(ClosedValue(Value::Null))
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut a: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                let mut out = Vec::new();
                while let Some(ClosedValue(v)) = a.next_element()? {
                    out.push(v);
                }
                Ok(ClosedValue(Value::Array(out)))
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut a: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                let mut out = serde_json::Map::new();
                while let Some((k, ClosedValue(v))) = a.next_entry::<String, ClosedValue>()? {
                    if out.insert(k, v).is_some() {
                        return Err(serde::de::Error::custom("duplicate"));
                    }
                }
                Ok(ClosedValue(Value::Object(out)))
            }
        }
        d.deserialize_any(Visitor)
    }
}
pub fn decode<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    if bytes.is_empty() || bytes.len() > FRAME_LIMIT {
        return Err("invalid_bootstrap");
    }
    let ClosedValue(v) = serde_json::from_slice(bytes).map_err(|_| "invalid_bootstrap")?;
    serde_json::from_value(v).map_err(|_| "invalid_bootstrap")
}
fn ascii_string(value: &str, out: &mut String) {
    out.push('"');
    for c in value.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if !(' '..='~').contains(&c) => {
                for u in c.encode_utf16(&mut [0; 2]) {
                    use std::fmt::Write;
                    write!(out, "\\u{u:04x}").unwrap();
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
fn canonical(value: &Value, out: &mut String) {
    match value {
        Value::Object(m) => {
            out.push('{');
            let mut keys = m.keys().collect::<Vec<_>>();
            keys.sort();
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',')
                }
                ascii_string(k, out);
                out.push(':');
                canonical(&m[*k], out)
            }
            out.push('}');
        }
        Value::Array(a) => {
            out.push('[');
            for (i, v) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',')
                }
                canonical(v, out)
            }
            out.push(']');
        }
        Value::String(s) => ascii_string(s, out),
        other => out.push_str(&other.to_string()),
    }
}
pub fn profile(launch: &Launch, nonce: &str, reserved: &[usize]) -> Result<Value> {
    if launch.schema != "nlp-host-launch.v1"
        || launch.channel_nonce != nonce
        || !launch.profile_json.is_ascii()
        || launch.artifacts.len() > 256
    {
        return Err("invalid_bootstrap");
    }
    let value: Value = decode(launch.profile_json.as_bytes())?;
    let mut encoded = String::new();
    canonical(&value, &mut encoded);
    if encoded != launch.profile_json
        || !matches!(
            value["schema_version"].as_str(),
            Some("nlp-profile.v2" | "nlp-profile.v3")
        )
        || value["canonicalization"] != "nlp-canonical.v1"
    {
        return Err("invalid_bootstrap");
    }
    let components = value["components"].as_array().ok_or("invalid_bootstrap")?;
    if !(1..=2).contains(&components.len()) {
        return Err("invalid_bootstrap");
    }
    let expected = value["artifacts"]
        .as_array()
        .ok_or("invalid_bootstrap")?
        .iter()
        .map(|v| serde_json::from_value::<Artifact>(v.clone()).map_err(|_| "invalid_bootstrap"))
        .collect::<Result<Vec<_>>>()?;
    let mut ids = BTreeSet::new();
    if expected.is_empty()
        || expected.len() > 256
        || expected
            .iter()
            .any(|a| !a.valid() || !ids.insert(a.artifact_id.clone()))
    {
        return Err("invalid_bootstrap");
    }
    let data = expected
        .iter()
        .filter(|a| !matches!(a.role.as_str(), "engine" | "runtime"))
        .collect::<Vec<_>>();
    if data.len() != launch.artifacts.len() {
        return Err("artifact_mismatch");
    }
    let mut handles = reserved.iter().copied().collect::<BTreeSet<_>>();
    let mut got = BTreeSet::new();
    for b in &launch.artifacts {
        if !data.contains(&&b.artifact)
            || !got.insert(&b.artifact.artifact_id)
            || b.handle < 4
            || b.handle >= usize::MAX as u64
            || !handles.insert(b.handle as usize)
        {
            return Err("artifact_mismatch");
        }
    }
    Ok(value)
}
pub fn validate_metadata(meta: &Metadata, profile: &Value) -> Result<()> {
    if meta.schema != "nlp-engine-release.v1"
        || meta.adapters.is_empty()
        || meta.adapters.len() > 16
        || !(2..=7).contains(&meta.artifacts.len())
    {
        return Err("artifact_mismatch");
    }
    let expected = profile["artifacts"].as_array().ok_or("invalid_bootstrap")?;
    let mut names = BTreeSet::new();
    let mut ids = BTreeSet::new();
    for member in &meta.artifacts {
        if !member.artifact.valid()
            || !matches!(member.artifact.role.as_str(), "engine" | "runtime")
            || !names.insert(member.file.as_str())
            || !ids.insert(&member.artifact.artifact_id)
            || !(member.file == WORKER || RUNTIME_FILES.contains(&member.file.as_str()))
            || (member.file != WORKER && member.artifact.role != "runtime")
            || !expected
                .contains(&serde_json::to_value(&member.artifact).map_err(|_| "artifact_mismatch")?)
        {
            return Err("artifact_mismatch");
        }
    }
    let executable = meta
        .artifacts
        .iter()
        .find(|a| a.file == WORKER)
        .ok_or("artifact_mismatch")?;
    if executable.artifact.role != "engine"
        || !names.contains("onnxruntime.dll")
        || meta.engine["engine_build_sha256"] != executable.artifact.sha256
        || meta.engine["worker_protocol"] != "nlp-worker.v2"
        || meta.engine["platform"] != "windows-x64"
    {
        return Err("artifact_mismatch");
    }
    if expected
        .iter()
        .filter(|a| matches!(a["role"].as_str(), Some("engine" | "runtime")))
        .count()
        != meta.artifacts.len()
    {
        return Err("artifact_mismatch");
    }
    exact_keys(
        &meta.engine,
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
    for key in [
        "package_name",
        "package_family",
        "core_package_name",
        "core_package_version",
        "package_version",
        "related_set_id",
        "engine_version",
    ] {
        if !meta.engine[key].as_str().is_some_and(identifier) {
            return Err("artifact_mismatch");
        }
    }
    if !meta.engine["publisher"]
        .as_str()
        .is_some_and(|s| !s.is_empty() && s.chars().count() <= 512)
    {
        return Err("artifact_mismatch");
    }
    let mut adapter_ids = BTreeSet::new();
    for cap in &meta.adapters {
        exact_keys(
            cap,
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
        let a = &cap["adapter"];
        exact_keys(
            a,
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
        if !adapter_ids.insert((
            a["adapter_id"].to_string(),
            a["adapter_version"].to_string(),
        )) {
            return Err("artifact_mismatch");
        }
        if a["adapter_build_sha256"] != executable.artifact.sha256
            || a["adapter_id"] != "boundary_entities"
            || a["adapter_version"] != "1"
            || a["abi"] != "nlp-adapter.v1"
            || a["architecture"] != "boundary-shared-v1"
            || a["artifact_format"] != "onnx-boundary-entity.v1"
            || a["tokenizer_format"] != "huggingface-tokenizer-json.v1"
        {
            return Err("artifact_mismatch");
        }
        for (name, bound) in [
            ("max_source_codepoints", 131072),
            ("max_batch_items", 16),
            ("max_windows", 256),
            ("max_candidates", 8192),
            ("max_output_findings", 576),
        ] {
            if !cap[name].as_u64().is_some_and(|n| n >= 1 && n <= bound) {
                return Err("artifact_mismatch");
            }
        }
        if !cap["runtime_requirements_sha256"]
            .as_str()
            .is_some_and(|s| hex(s, 64))
        {
            return Err("artifact_mismatch");
        }
        for (key, allowed) in [
            (
                "task_contracts",
                vec!["message_topics.v1", "concept_mentions.v1"],
            ),
            (
                "score_kinds",
                vec!["probability", "logit", "margin", "none"],
            ),
        ] {
            let a = cap[key].as_array().ok_or("artifact_mismatch")?;
            let mut seen = BTreeSet::new();
            if a.is_empty()
                || a.iter().any(|s| {
                    !s.as_str()
                        .is_some_and(|s| allowed.contains(&s) && seen.insert(s))
                })
            {
                return Err("artifact_mismatch");
            }
        }
    }
    for c in profile["components"]
        .as_array()
        .ok_or("invalid_bootstrap")?
    {
        if c["engine"] != meta.engine
            || meta
                .adapters
                .iter()
                .filter(|a| a["adapter"] == c["adapter"])
                .count()
                != 1
        {
            return Err("identity_mismatch");
        }
    }
    Ok(())
}
fn exact_keys(value: &Value, keys: &[&str]) -> Result<()> {
    let m = value.as_object().ok_or("artifact_mismatch")?;
    if m.len() != keys.len() || keys.iter().any(|k| !m.contains_key(*k)) {
        return Err("artifact_mismatch");
    }
    Ok(())
}
pub fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn control(bytes: &[u8], nonce: &str, operation: &str) -> Result<()> {
    let c: Control = decode(bytes)?;
    if c.schema != "nlp-host-control.v1" || c.channel_nonce != nonce || c.operation != operation {
        return Err("invalid_bootstrap");
    }
    Ok(())
}
pub fn observe(bytes: &[u8], nonce: &str, engine: &Value) -> Result<()> {
    let request: Observe = decode(bytes)?;
    if request.schema != "nlp-host-observe.v1"
        || request.channel_nonce != nonce
        || &request.engine != engine
    {
        return Err("identity_mismatch");
    }
    Ok(())
}
pub fn frame_length(bytes: [u8; 4]) -> Result<usize> {
    let n = u32::from_le_bytes(bytes) as usize;
    if n == 0 || n > FRAME_LIMIT {
        return Err("invalid_bootstrap");
    }
    Ok(n)
}
#[derive(Clone, Copy, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum FailureStage {
    ParentAccess,
    OuterAccess,
    ParentProcess,
    CurrentPackage,
    ParentPackage,
    ParentSid,
    ParentSession,
    ParentJob,
    HostJob,
    OuterLimits,
    MainObservation,
    MainTrust,
    ParentImage,
    HostImage,
    PackageObservation,
    PackageTrust,
    DescriptorRead,
    DescriptorParse,
    DescriptorValidation,
    DeclaredEngine,
    EngineSelection,
    InstalledEngine,
    MainManifest,
    EngineManifest,
    ParentFamily,
    CurrentPackageIdentity,
    MainFiles,
    InitialCommand,
    WorkerMetadata,
    ObservationBinding,
    MetadataBinding,
    ArtifactBindings,
    WorkerControl,
    WorkerLaunch,
    WorkerDrain,
}

pub(crate) fn status_value_with_stage(
    nonce: &str,
    state: &str,
    reason: &str,
    pid: Option<u32>,
    tree_drained: bool,
    stage: Option<FailureStage>,
) -> Result<Value> {
    let mut value = status_value(nonce, state, reason, pid, tree_drained)?;
    if let Some(stage) = stage {
        if state != "failed" {
            return Err("invalid_bootstrap");
        }
        value["failure_stage"] = serde_json::to_value(stage).map_err(|_| "invalid_bootstrap")?;
    }
    Ok(value)
}

pub fn status_value(
    nonce: &str,
    state: &str,
    reason: &str,
    pid: Option<u32>,
    tree_drained: bool,
) -> Result<Value> {
    let coherent = match state {
        "ready" => pid.is_none() && tree_drained && reason == "none",
        "running" => pid.is_some_and(|p| p > 0) && !tree_drained && reason == "none",
        "drained" => tree_drained && reason == "none",
        "failed" => [
            "invalid_bootstrap",
            "identity_mismatch",
            "artifact_mismatch",
            "resource_limit",
            "launch_failure",
            "io_failure",
            "parent_lost",
            "timeout",
            "worker_failure",
        ]
        .contains(&reason),
        _ => false,
    };
    if !coherent {
        return Err("invalid_bootstrap");
    }
    Ok(
        serde_json::json!({"schema":"nlp-host-status.v1","channel_nonce":nonce,"state":state,"reason":reason,"worker_pid":pid,"tree_drained":tree_drained}),
    )
}

#[cfg(windows)]
#[path = "worker_host_windows.rs"]
mod platform;
#[cfg(windows)]
pub fn run(args: &[String]) -> Result<()> {
    platform::run(args)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn failed_status_stage_is_finite_and_absent_on_success() {
        let value = status_value_with_stage(
            "n",
            "failed",
            "identity_mismatch",
            None,
            true,
            Some(FailureStage::DescriptorValidation),
        )
        .unwrap();
        assert_eq!(value["failure_stage"], "descriptor_validation");
        assert_eq!(value["reason"], "identity_mismatch");
        assert_eq!(value["tree_drained"], true);
        assert!(serde_json::from_str::<FailureStage>(r#""exception detail""#).is_err());
        assert!(status_value_with_stage(
            "n",
            "ready",
            "none",
            None,
            true,
            Some(FailureStage::ParentAccess)
        )
        .is_err());
        let ready = status_value_with_stage("n", "ready", "none", None, true, None).unwrap();
        assert!(ready.get("failure_stage").is_none());
        assert!(status_value("n", "failed", "identity_mismatch", None, true)
            .unwrap()
            .get("failure_stage")
            .is_none());
    }
    #[test]
    fn strict_private_arguments() {
        let args = [
            "Bridge",
            FLAG,
            "4",
            "8",
            "12",
            "16",
            "20",
            "24",
            "28",
            "0123456789abcdef0123456789abcdef",
        ]
        .map(str::to_string);
        assert!(arguments(&args).is_ok());
        for i in 2..9 {
            let mut bad = args.clone();
            bad[i] = "4".into();
            if i != 2 {
                assert!(arguments(&bad).is_err())
            }
        }
        let mut bad = args.clone();
        bad[2] = "04".into();
        assert!(arguments(&bad).is_err());
    }
    #[test]
    fn duplicate_json_never_dispatches() {
        for raw in [
            br#"{"a":1,"a":2}"#.as_slice(),
            br#"{"a":{"x":1,"x":2}}"#,
            br#"{"a":NaN}"#,
        ] {
            assert!(decode::<Value>(raw).is_err())
        }
    }
    #[test]
    fn ascii_descriptor_spelling() {
        let v: Value = serde_json::from_str(r#"{"z":"\u007fé😀","a":true}"#).unwrap();
        let mut s = String::new();
        canonical(&v, &mut s);
        assert_eq!(s, r#"{"a":true,"z":"\u007f\u00e9\ud83d\ude00"}"#);
    }
    #[test]
    fn identifier_matches_task_contract() {
        for s in [".a", ":a", "_a", "-a", &"x".repeat(160)] {
            assert!(identifier(s))
        }
        for s in ["", &"x".repeat(161), "é", "a/b"] {
            assert!(!identifier(s))
        }
    }
    #[test]
    fn control_is_nonce_and_operation_bound() {
        let raw = br#"{"schema":"nlp-host-control.v1","channel_nonce":"one","operation":"commit"}"#;
        assert!(control(raw, "one", "commit").is_ok());
        assert!(control(raw, "two", "commit").is_err());
        assert!(control(raw, "one", "close").is_err());
    }
    #[test]
    fn frame_bound_is_checked_before_allocation() {
        for n in [0, FRAME_LIMIT + 1, u32::MAX as usize] {
            assert!(frame_length((n as u32).to_le_bytes()).is_err())
        }
        assert_eq!(
            frame_length((FRAME_LIMIT as u32).to_le_bytes()).unwrap(),
            FRAME_LIMIT
        );
    }
    #[test]
    fn physical_drain_never_implies_successful_unload() {
        assert!(status_value("n", "failed", "worker_failure", Some(1), true).is_ok());
        assert!(status_value("n", "failed", "worker_failure", Some(1), false).is_ok());
        assert!(status_value("n", "drained", "none", Some(1), false).is_err());
        assert!(status_value("n", "ready", "none", None, false).is_err());
        assert!(status_value("n", "running", "none", Some(1), true).is_err());
        assert!(status_value("n", "failed", "exception_text", None, true).is_err());
    }
    #[test]
    fn observe_cannot_commit_or_supply_paths() {
        let engine = serde_json::json!({"engine_version":"1"});
        let value =
            serde_json::json!({"schema":"nlp-host-observe.v1","channel_nonce":"n","engine":engine});
        let bytes = serde_json::to_vec(&value).unwrap();
        assert!(observe(&bytes, "n", &engine).is_ok());
        assert!(observe(&bytes, "x", &engine).is_err());
        let mut bad = value;
        bad["path"] = "caller.exe".into();
        assert!(decode::<Observe>(&serde_json::to_vec(&bad).unwrap()).is_err());
    }
    fn fixture() -> (Metadata, Value) {
        let engine = serde_json::json!({"package_name":"Example.Engine","package_family":"Example.Engine_publisher","core_package_name":"Example.Main","core_package_version":"1.0.2.0","publisher":"CN=Example","package_version":"1.0.9.0","related_set_id":"set","engine_version":"1","platform":"windows-x64","worker_protocol":"nlp-worker.v2","engine_build_sha256":"a".repeat(64)});
        let adapter = serde_json::json!({"adapter_id":"boundary_entities","adapter_version":"1","adapter_build_sha256":"a".repeat(64),"abi":"nlp-adapter.v1","architecture":"boundary-shared-v1","artifact_format":"onnx-boundary-entity.v1","tokenizer_format":"huggingface-tokenizer-json.v1"});
        let capability = serde_json::json!({"adapter":adapter,"task_contracts":["concept_mentions.v1"],"score_kinds":["probability"],"max_source_codepoints":4096,"max_batch_items":1,"max_windows":1,"max_candidates":192,"max_output_findings":576,"runtime_requirements_sha256":"b".repeat(64)});
        let assets = vec![
            PackageArtifact {
                artifact: Artifact {
                    artifact_id: "worker".into(),
                    sha256: "a".repeat(64),
                    size_bytes: 5,
                    role: "engine".into(),
                    format_id: "pe".into(),
                },
                file: WORKER.into(),
            },
            PackageArtifact {
                artifact: Artifact {
                    artifact_id: "ort".into(),
                    sha256: "c".repeat(64),
                    size_bytes: 6,
                    role: "runtime".into(),
                    format_id: "dll".into(),
                },
                file: "onnxruntime.dll".into(),
            },
        ];
        let profile = serde_json::json!({"schema_version":"nlp-profile.v3","canonicalization":"nlp-canonical.v1","components":[{"engine":engine,"adapter":adapter}],"artifacts":assets.iter().map(|a|serde_json::to_value(&a.artifact).unwrap()).collect::<Vec<_>>()});
        (
            Metadata {
                schema: "nlp-engine-release.v1".into(),
                engine,
                adapters: vec![capability],
                artifacts: assets,
            },
            profile,
        )
    }
    #[test]
    fn signed_metadata_cannot_substitute_code_or_capability() {
        let (mut meta, profile) = fixture();
        assert!(validate_metadata(&meta, &profile).is_ok());
        meta.artifacts[0].artifact.sha256 = "d".repeat(64);
        assert!(validate_metadata(&meta, &profile).is_err());
        let (mut meta, profile) = fixture();
        meta.engine["unexpected"] = true.into();
        assert!(validate_metadata(&meta, &profile).is_err());
        let (mut meta, profile) = fixture();
        meta.adapters[0]["score_kinds"] = serde_json::json!(["probability", "probability"]);
        assert!(validate_metadata(&meta, &profile).is_err());
        let (mut meta, profile) = fixture();
        meta.artifacts[1].file = "../onnxruntime.dll".into();
        assert!(validate_metadata(&meta, &profile).is_err());
    }
    #[test]
    fn data_handles_are_exact_and_not_control_aliases() {
        let (_, mut value) = fixture();
        let asset = Artifact {
            artifact_id: "weights".into(),
            sha256: "e".repeat(64),
            size_bytes: 9,
            role: "weights".into(),
            format_id: "safetensors".into(),
        };
        value["artifacts"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::to_value(&asset).unwrap());
        let mut json = String::new();
        canonical(&value, &mut json);
        let mut launch = Launch {
            schema: "nlp-host-launch.v1".into(),
            channel_nonce: "n".into(),
            profile_json: json,
            artifacts: vec![Binding {
                artifact: asset,
                handle: 40,
            }],
        };
        assert!(profile(&launch, "n", &[4, 8]).is_ok());
        launch.artifacts[0].handle = 4;
        assert!(profile(&launch, "n", &[4, 8]).is_err());
        launch.artifacts.clear();
        assert!(profile(&launch, "n", &[4, 8]).is_err());
    }
}
