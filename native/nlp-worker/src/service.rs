//! One component residence and one active operation, with a responsive control path.
use crate::{
    context::{Context, integer},
    entity::{Control, EntityAdapter},
    wire::{Envelope, canonical, hash, id, object},
};
use anyhow::{Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
fn assessed_payload(text: &str, findings: Vec<Value>) -> Value {
    let count = text.chars().count();
    json!({"task":"concept_mentions","task_contract":"concept_mentions.v1","outcome":if findings.is_empty(){"assessed_no_match"}else{"assessed_findings"},"reason":"none","coverage":{"source_codepoints":count,"examined":if count==0{json!([])}else{json!([{"start":0,"end":count}])},"excluded":[],"limits_reached":[]},"findings":findings})
}
fn artifact_set(actual: &Value, expected: &[Value]) -> Result<()> {
    let list = actual
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
    ensure!(list.len() == expected.len(), "artifact_mismatch");
    let mut seen = BTreeSet::new();
    for value in list {
        let a: crate::context::Artifact = serde_json::from_value(value.clone())?;
        a.validate()?;
        ensure!(
            seen.insert(a.artifact_id) && expected.contains(value),
            "artifact_mismatch"
        );
    }
    Ok(())
}

fn error_payload(_component: &Value, text: &str, reason: &str, limit: Option<&str>) -> Value {
    let count = text.chars().count();
    let outcome = if reason == "unsupported_input" {
        "unsupported"
    } else if reason == "cancelled" {
        "unprocessed"
    } else {
        "failed"
    };
    json!({"task":"concept_mentions","task_contract":"concept_mentions.v1","outcome":outcome,"reason":reason,"coverage":{"source_codepoints":count,"examined":[],"excluded":if count==0{json!([])}else{json!([{"start":0,"end":count}])},"limits_reached":limit.map(|v|vec![v]).unwrap_or_default()},"findings":[]})
}
fn terminal_error(error: &anyhow::Error) -> (&'static str, Option<&'static str>) {
    let text = error.to_string();
    match text.as_str() {
        "cancelled" => ("cancelled", None),
        "deadline" => ("deadline", None),
        "encoded_bound" | "word_bound" => ("unsupported_input", Some("tokens")),
        "output_bound" => ("limit_reached", Some("output")),
        _ => ("worker_failure", None),
    }
}
fn validate_analyze(context: &Context, p: &Value, c: &Value) -> Result<()> {
    object(
        p,
        &[
            "protocol",
            "request_id",
            "profile_sha256",
            "component_id",
            "component_sha256",
            "tasks",
            "deadline_ms",
            "items",
        ],
    )?;
    context.bound(p)?;
    ensure!(
        p["tasks"] == json!(["concept_mentions"]),
        "binding_mismatch"
    );
    let items = p["items"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("invalid_payload"))?;
    ensure!(
        !items.is_empty()
            && items.len() <= c["max_batch_items"].as_u64().unwrap_or(0) as usize
            && items.len() <= 16,
        "resource_limit"
    );
    let mut seen = BTreeSet::new();
    for item in items {
        object(item, &["request_item_id", "text", "text_sha256"])?;
        ensure!(
            seen.insert(id(&item["request_item_id"])?.to_owned()),
            "invalid_payload"
        );
        let text = item["text"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("invalid_payload"))?;
        ensure!(
            item["text_sha256"] == hash(text.as_bytes())
                && text.chars().count()
                    <= c["max_source_codepoints"].as_u64().unwrap_or(0) as usize,
            "binding_mismatch"
        );
    }
    Ok(())
}

fn analyze(
    adapter: &EntityAdapter,
    context: &Context,
    p: &Value,
    c: &Value,
    control: &Control,
) -> Result<Value> {
    validate_analyze(context, p, c)?;
    let items = p["items"].as_array().unwrap();
    let mut results = Vec::new();
    for item in items {
        let text = item["text"].as_str().unwrap();
        let result = adapter.extract(text, control);
        let payload = match result {
            Ok(findings) => assessed_payload(text, findings),
            Err(error) => {
                let (reason, limit) = terminal_error(&error);
                error_payload(c, text, reason, limit)
            }
        };
        results.push(json!({"request_item_id":item["request_item_id"],"tasks":[{"component_id":p["component_id"],"component_sha256":p["component_sha256"],"text_sha256":item["text_sha256"],"payload":payload}]}));
    }
    Ok(
        json!({"protocol":"nlp-worker.v2","request_id":p["request_id"],"profile_sha256":p["profile_sha256"],"component_id":p["component_id"],"component_sha256":p["component_sha256"],"items":results}),
    )
}
struct Active {
    request: Value,
    operation: String,
    cancel: Arc<AtomicBool>,
    join: JoinHandle<(Option<EntityAdapter>, Value)>,
    cancel_reply: Option<Value>,
}
pub struct Service {
    context: Arc<Context>,
    bootstrapped: bool,
    closed: bool,
    seen: BTreeSet<String>,
    resident: Option<(Value, EntityAdapter)>,
    active: Option<Active>,
    component: Option<Value>,
    last_terminal: Option<(Value, String)>,
}
impl Service {
    pub fn new(context: Context) -> Self {
        Self {
            context: Arc::new(context),
            bootstrapped: false,
            closed: false,
            seen: BTreeSet::new(),
            resident: None,
            active: None,
            component: None,
            last_terminal: None,
        }
    }
    fn lifecycle(&self, p: &Value, operation: &str, outcome: &str, health: &str) -> Value {
        let mut out = json!({"protocol":"nlp-worker.v2","operation":operation,"request_id":p["request_id"],"profile_sha256":p["profile_sha256"],"component_sha256":p["component_sha256"],"target_request_id":null,"outcome":outcome,"health":health});
        if operation == "cancel" {
            out["target_request_id"] = p["target_request_id"].clone()
        }
        out
    }
    pub fn dispatch(&mut self, e: Envelope) -> Result<Vec<(String, Value)>> {
        ensure!(!self.closed, "unexpected_operation");
        let op = e.operation;
        let p = e.payload;
        if !self.bootstrapped {
            ensure!(op == "bootstrap", "unexpected_operation");
            object(&p, &["channel_nonce", "profile_json", "artifact_ids"])?;
            ensure!(
                p["channel_nonce"] == self.context.nonce
                    && p["profile_json"] == self.context.profile_json,
                "binding_mismatch"
            );
            let ids = self.context.profile["artifacts"]
                .as_array()
                .unwrap()
                .iter()
                .map(|a| a["artifact_id"].clone())
                .collect::<Vec<_>>();
            ensure!(p["artifact_ids"] == json!(ids), "binding_mismatch");
            self.bootstrapped = true;
            return Ok(vec![(
                op,
                json!({"channel_nonce":self.context.nonce,"profile_sha256":self.context.profile_sha,"outcome":"bound","reason":"none"}),
            )]);
        }
        ensure!(op != "bootstrap", "unexpected_operation");
        let rid = id(&p["request_id"])?;
        if self.seen.len() == 256 && op == "close" && self.active.is_none() {
            object(&p, &["request_id"])?;
            ensure!(!self.seen.contains(rid), "unexpected_operation");
        } else {
            ensure!(
                self.seen.len() < 256 && self.seen.insert(rid.to_owned()),
                "unexpected_operation"
            );
        }
        match op.as_str() {
            "handshake" => {
                object(&p, &["protocol", "request_id", "expected_engine"])?;
                ensure!(
                    p["protocol"] == "nlp-worker.v2" && p["expected_engine"] == self.context.engine,
                    "binding_mismatch"
                );
                Ok(vec![(
                    op,
                    json!({"protocol":"nlp-worker.v2","request_id":p["request_id"],"engine":self.context.engine,"adapters":self.context.adapters}),
                )])
            }
            "prepare" => {
                object(
                    &p,
                    &[
                        "protocol",
                        "request_id",
                        "profile_sha256",
                        "component_id",
                        "component_sha256",
                        "artifacts",
                        "max_memory_bytes",
                        "inference_threads",
                    ],
                )?;
                let c = self.context.bound(&p)?;
                ensure!(
                    self.active.is_none() && self.resident.is_none(),
                    "unexpected_operation"
                );
                ensure!(
                    p["max_memory_bytes"]
                        == self.context.profile["resources"]["transition_peak_memory_bytes"]
                        && p["inference_threads"] == 2,
                    "resource_limit"
                );
                ensure!(
                    c["peak_memory_bytes"]
                        .as_u64()
                        .is_some_and(|n| n <= p["max_memory_bytes"].as_u64().unwrap_or(0)),
                    "resource_limit"
                );
                let expected = self.context.profile["artifacts"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .filter(|a| {
                        a["role"] == "engine"
                            || a["role"] == "runtime"
                            || c["artifact_ids"]
                                .as_array()
                                .unwrap()
                                .contains(&a["artifact_id"])
                    })
                    .cloned()
                    .collect::<Vec<_>>();
                artifact_set(&p["artifacts"], &expected)?;
                let context = self.context.clone();
                let component = c.clone();
                let request = p.clone();
                let flag = Arc::new(AtomicBool::new(false));
                let control = Control {
                    cancelled: flag.clone(),
                    deadline: Instant::now() + Duration::from_secs(120),
                };
                let join = thread::spawn(move || {
                    let result = EntityAdapter::load(&context, component, &control);
                    let (model, outcome, reason) = match result {
                        Ok(a) => (Some(a), "ready", "none"),
                        Err(error) => {
                            let s = error.to_string();
                            (
                                None,
                                if s == "cancelled" {
                                    "cancelled"
                                } else {
                                    "refused"
                                },
                                if s == "cancelled" {
                                    "cancelled"
                                } else if [
                                    "artifact_mismatch",
                                    "external_initializer",
                                    "invalid_graph",
                                ]
                                .contains(&s.as_str())
                                {
                                    "artifact_mismatch"
                                } else if s == "resource_limit" {
                                    "resource_limit"
                                } else {
                                    "incompatible"
                                },
                            )
                        }
                    };
                    let response = json!({"protocol":"nlp-worker.v2","request_id":request["request_id"],"profile_sha256":request["profile_sha256"],"component_id":request["component_id"],"component_sha256":request["component_sha256"],"outcome":outcome,"reason":reason});
                    (model, response)
                });
                self.component = Some(c);
                self.active = Some(Active {
                    request: p,
                    operation: op,
                    cancel: flag,
                    join,
                    cancel_reply: None,
                });
                Ok(vec![])
            }
            "analyze" => {
                ensure!(self.active.is_none(), "unexpected_operation");
                let current = &self
                    .resident
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("unexpected_operation"))?
                    .0;
                validate_analyze(&self.context, &p, current)?;
                let (c, adapter) = self
                    .resident
                    .take()
                    .ok_or_else(|| anyhow::anyhow!("unexpected_operation"))?;
                ensure!(self.context.bound(&p)? == c, "binding_mismatch");
                let duration = integer(&p["deadline_ms"], 1, 120000)?;
                let flag = Arc::new(AtomicBool::new(false));
                let control = Control {
                    cancelled: flag.clone(),
                    deadline: Instant::now() + Duration::from_millis(duration),
                };
                let context = self.context.clone();
                let request = p.clone();
                let component = c.clone();
                let join = thread::spawn(move || {
                    let result = analyze(&adapter, &context, &request, &component, &control);
                    match result {
                        Ok(response) => (Some(adapter), response),
                        Err(_) => (None, json!({"reason":"invalid_payload"})),
                    }
                });
                self.component = Some(c);
                self.active = Some(Active {
                    request: p,
                    operation: op,
                    cancel: flag,
                    join,
                    cancel_reply: None,
                });
                Ok(vec![])
            }
            "cancel" => {
                object(
                    &p,
                    &[
                        "protocol",
                        "operation",
                        "request_id",
                        "target_request_id",
                        "profile_sha256",
                        "component_sha256",
                    ],
                )?;
                ensure!(
                    p["operation"] == "cancel"
                        && p["protocol"] == "nlp-worker.v2"
                        && p["profile_sha256"] == self.context.profile_sha,
                    "binding_mismatch"
                );
                id(&p["target_request_id"])?;
                if let Some(a) = &mut self.active {
                    ensure!(
                        a.request["request_id"] == p["target_request_id"]
                            && a.request["component_sha256"] == p["component_sha256"]
                            && a.cancel_reply.is_none(),
                        "binding_mismatch"
                    );
                    a.cancel.store(true, Ordering::SeqCst);
                    a.cancel_reply = Some(p);
                    Ok(vec![])
                } else {
                    ensure!(
                        self.last_terminal
                            .as_ref()
                            .is_some_and(|(target, operation)| (operation == "prepare"
                                || operation == "analyze")
                                && target["request_id"] == p["target_request_id"]
                                && target["component_sha256"] == p["component_sha256"]
                                && target["profile_sha256"] == p["profile_sha256"]),
                        "binding_mismatch"
                    );
                    Ok(vec![(
                        op,
                        self.lifecycle(
                            &p,
                            "cancel",
                            "completed",
                            if self.resident.is_some() {
                                "ready"
                            } else {
                                "unloaded"
                            },
                        ),
                    )])
                }
            }
            "unload" => {
                object(
                    &p,
                    &[
                        "protocol",
                        "operation",
                        "request_id",
                        "profile_sha256",
                        "component_sha256",
                    ],
                )?;
                ensure!(
                    p["protocol"] == "nlp-worker.v2"
                        && p["operation"] == "unload"
                        && p["profile_sha256"] == self.context.profile_sha
                        && self
                            .component
                            .as_ref()
                            .is_some_and(|c| p["component_sha256"]
                                == hash(canonical(c).unwrap_or_default().as_bytes())),
                    "binding_mismatch"
                );
                if self.active.is_some() {
                    return Ok(vec![(op, self.lifecycle(&p, "unload", "busy", "ready"))]);
                }
                self.resident.take();
                Ok(vec![(
                    op,
                    self.lifecycle(&p, "unload", "completed", "unloaded"),
                )])
            }
            "close" => {
                object(&p, &["request_id"])?;
                if self.active.is_some() {
                    return Ok(vec![(
                        op,
                        json!({"request_id":p["request_id"],"outcome":"failed","reason":"busy"}),
                    )]);
                }
                self.resident.take();
                self.closed = true;
                Ok(vec![(
                    op,
                    json!({"request_id":p["request_id"],"outcome":"drained","reason":"none"}),
                )])
            }
            _ => bail!("unexpected_operation"),
        }
    }
    pub fn poll(&mut self) -> Result<Vec<(String, Value)>> {
        if !self.active.as_ref().is_some_and(|a| a.join.is_finished()) {
            return Ok(vec![]);
        }
        let active = self.active.take().unwrap();
        let (model, response) = active
            .join
            .join()
            .map_err(|_| anyhow::anyhow!("host_failure"))?;
        let failed = response.get("protocol").is_none();
        if let Some(model) = model {
            self.resident = Some((self.component.clone().unwrap(), model));
        }
        ensure!(!failed, "invalid_payload");
        self.last_terminal = Some((active.request, active.operation.clone()));
        let mut replies = vec![(active.operation, response)];
        if let Some(cancel) = active.cancel_reply {
            replies.push((
                "cancel".into(),
                self.lifecycle(
                    &cancel,
                    "cancel",
                    "completed",
                    if self.resident.is_some() {
                        "ready"
                    } else {
                        "unloaded"
                    },
                ),
            ));
        }
        Ok(replies)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    fn service() -> Service {
        let component =
            json!({"component_id":"one","max_batch_items":2,"max_source_codepoints":32});
        let profile = json!({"components":[component],"artifacts":[]});
        let profile_json = canonical(&profile).unwrap();
        Service::new(Context {
            nonce: "a".repeat(32),
            profile_sha: hash(profile_json.as_bytes()),
            profile_json,
            profile,
            engine: json!({"worker_protocol":"nlp-worker.v2"}),
            adapters: vec![],
            files: BTreeMap::new(),
        })
    }
    fn command(operation: &str, payload: Value) -> Envelope {
        Envelope {
            channel: "nlp-child.v1".into(),
            operation: operation.into(),
            payload,
        }
    }
    fn bootstrap(s: &mut Service) {
        let p = json!({"channel_nonce":s.context.nonce,"profile_json":s.context.profile_json,"artifact_ids":[]});
        assert_eq!(
            s.dispatch(command("bootstrap", p)).unwrap()[0].1["outcome"],
            "bound"
        );
    }
    fn cancel(s: &Service, request: &str, target: &str) -> Value {
        json!({"protocol":"nlp-worker.v2","operation":"cancel","request_id":request,"target_request_id":target,"profile_sha256":s.context.profile_sha,"component_sha256":"b".repeat(64)})
    }
    #[test]
    fn bootstrap_is_first_once_and_exact() {
        let mut s = service();
        assert!(s.dispatch(command("handshake", json!({}))).is_err());
        bootstrap(&mut s);
        let p = json!({"channel_nonce":s.context.nonce,"profile_json":s.context.profile_json,"artifact_ids":[]});
        assert!(s.dispatch(command("bootstrap", p)).is_err());
    }
    fn handshakes(s: &mut Service, count: usize) {
        for i in 0..count {
            let p = json!({"protocol":"nlp-worker.v2","request_id":format!("handshake.{i}"),"expected_engine":s.context.engine});
            let replies = s.dispatch(command("handshake", p)).unwrap();
            assert_eq!(replies.len(), 1);
            assert_eq!(replies[0].0, "handshake");
            assert_eq!(replies[0].1["request_id"], format!("handshake.{i}"));
            assert_eq!(replies[0].1["engine"], s.context.engine);
        }
    }
    #[test]
    fn exhausted_request_history_reserves_one_terminal_close() {
        let mut s = service();
        bootstrap(&mut s);
        handshakes(&mut s, 256);
        assert_eq!(s.seen.len(), 256);
        let extra = json!({"protocol":"nlp-worker.v2","request_id":"handshake.extra","expected_engine":s.context.engine});
        assert!(s.dispatch(command("handshake", extra)).is_err());
        assert_eq!(s.seen.len(), 256);
        let close = json!({"request_id":"close.one"});
        let replies = s.dispatch(command("close", close.clone())).unwrap();
        assert_eq!(
            replies,
            vec![(
                "close".into(),
                json!({"request_id":"close.one","outcome":"drained","reason":"none"})
            )]
        );
        assert_eq!(s.seen.len(), 256);
        assert!(s.closed);
        assert!(s.dispatch(command("close", close)).is_err());
        assert!(
            s.dispatch(command("close", json!({"request_id":"close.two"})))
                .is_err()
        );
        let p = json!({"protocol":"nlp-worker.v2","request_id":"handshake.after","expected_engine":s.context.engine});
        assert!(s.dispatch(command("handshake", p)).is_err());
        assert_eq!(s.seen.len(), 256);
    }
    #[test]
    fn reserved_close_rejects_replays_and_malformed_payloads() {
        let mut s = service();
        bootstrap(&mut s);
        handshakes(&mut s, 256);
        for p in [
            json!({"request_id":"handshake.0"}),
            json!({"request_id":"close.extra","extra":true}),
            json!({}),
            json!({"request_id":""}),
            json!({"request_id":1}),
        ] {
            assert!(s.dispatch(command("close", p)).is_err());
            assert!(!s.closed);
            assert_eq!(s.seen.len(), 256);
        }
        assert_eq!(
            s.dispatch(command("close", json!({"request_id":"close.valid"})))
                .unwrap()[0]
                .1["outcome"],
            "drained"
        );
        assert_eq!(s.seen.len(), 256);
    }
    #[test]
    fn ordinary_terminal_close_also_rejects_further_dispatch() {
        let mut s = service();
        bootstrap(&mut s);
        assert_eq!(
            s.dispatch(command("close", json!({"request_id":"close.one"})))
                .unwrap()[0]
                .1["outcome"],
            "drained"
        );
        assert_eq!(s.seen.len(), 1);
        assert!(
            s.dispatch(command("close", json!({"request_id":"close.one"})))
                .is_err()
        );
        assert!(
            s.dispatch(command("close", json!({"request_id":"close.two"})))
                .is_err()
        );
        let p = json!({"channel_nonce":s.context.nonce,"profile_json":s.context.profile_json,"artifact_ids":[]});
        assert!(s.dispatch(command("bootstrap", p)).is_err());
        assert_eq!(s.seen.len(), 1);
    }
    #[test]
    fn busy_close_preserves_active_operation_and_history_limit() {
        let mut s = service();
        bootstrap(&mut s);
        handshakes(&mut s, 255);
        let (finish, wait) = std::sync::mpsc::sync_channel(0);
        let join = thread::spawn(move || {
            wait.recv_timeout(Duration::from_secs(2)).unwrap();
            (None, json!({"protocol":"nlp-worker.v2"}))
        });
        s.active = Some(Active {
            request: json!({"request_id":"prepare.one"}),
            operation: "prepare".into(),
            cancel: Arc::new(AtomicBool::new(false)),
            join,
            cancel_reply: None,
        });
        let replies = s
            .dispatch(command("close", json!({"request_id":"close.busy"})))
            .unwrap();
        assert_eq!(
            replies,
            vec![(
                "close".into(),
                json!({"request_id":"close.busy","outcome":"failed","reason":"busy"})
            )]
        );
        assert!(!s.closed);
        assert_eq!(s.seen.len(), 256);
        assert!(
            s.dispatch(command(
                "close",
                json!({"request_id":"close.exhausted.busy"})
            ))
            .is_err()
        );
        assert!(!s.closed);
        assert_eq!(s.seen.len(), 256);
        let active = s.active.take().unwrap();
        assert!(!active.cancel.load(Ordering::SeqCst));
        assert!(active.cancel_reply.is_none());
        finish.send(()).unwrap();
        active.join.join().unwrap();
        assert_eq!(
            s.dispatch(command("close", json!({"request_id":"close.idle"})))
                .unwrap()[0]
                .1["outcome"],
            "drained"
        );
        assert_eq!(s.seen.len(), 256);
    }
    #[test]
    fn idle_cancel_binds_only_the_latest_cancellable_terminal() {
        let mut s = service();
        bootstrap(&mut s);
        let target = json!({"request_id":"finished","profile_sha256":s.context.profile_sha,"component_sha256":"b".repeat(64)});
        s.last_terminal = Some((target.clone(), "analyze".into()));
        let p = cancel(&s, "cancel.good", "finished");
        assert_eq!(
            s.dispatch(command("cancel", p)).unwrap()[0].1["health"],
            "unloaded"
        );
        let p = cancel(&s, "cancel.foreign", "other");
        assert!(s.dispatch(command("cancel", p)).is_err());
        s.last_terminal = Some((target, "handshake".into()));
        let p = cancel(&s, "cancel.wrong_operation", "finished");
        assert!(s.dispatch(command("cancel", p)).is_err());
    }
    #[test]
    fn cancel_ack_waits_for_actual_operation_join() {
        let mut s = service();
        bootstrap(&mut s);
        let flag = Arc::new(AtomicBool::new(false));
        let flag_worker = flag.clone();
        let request = json!({"request_id":"prepare.one","profile_sha256":s.context.profile_sha,"component_sha256":"b".repeat(64)});
        let response =
            json!({"protocol":"nlp-worker.v2","request_id":"prepare.one","outcome":"cancelled"});
        let join = thread::spawn(move || {
            while !flag_worker.load(Ordering::SeqCst) {
                thread::sleep(Duration::from_millis(1));
            }
            (None, response)
        });
        s.active = Some(Active {
            request,
            operation: "prepare".into(),
            cancel: flag.clone(),
            join,
            cancel_reply: None,
        });
        let p = cancel(&s, "cancel.one", "prepare.one");
        assert!(s.dispatch(command("cancel", p)).unwrap().is_empty());
        assert!(flag.load(Ordering::SeqCst));
        let deadline = Instant::now() + Duration::from_secs(1);
        let replies = loop {
            let r = s.poll().unwrap();
            if !r.is_empty() {
                break r;
            }
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(1));
        };
        assert_eq!(replies[0].0, "prepare");
        assert_eq!(replies[1].0, "cancel");
        assert_eq!(replies[1].1["health"], "unloaded");
        assert!(s.active.is_none());
    }
    #[test]
    fn learned_null_and_failed_examination_are_distinct() {
        use gliner2_rs::boundary::decode::{QueryScores, group_scored_candidates};
        let scores = QueryScores {
            indices: vec![[0, 1]],
            valid_mask: vec![true],
            pair_logits: vec![2.],
            query_valid: true,
            threshold: 0.5,
            null_logit: Some(1.),
        };
        assert!(group_scored_candidates(&[scores], 1., 0.5).unwrap()[0].is_empty());
        assert_eq!(
            assessed_payload("echo", vec![])["outcome"],
            "assessed_no_match"
        );
        for (reason, limit) in [
            ("unsupported_input", Some("tokens")),
            ("limit_reached", Some("output")),
            ("worker_failure", None),
            ("cancelled", None),
        ] {
            let p = error_payload(&json!({}), "echo", reason, limit);
            assert_ne!(p["outcome"], "assessed_no_match");
            assert!(p["coverage"]["examined"].as_array().unwrap().is_empty());
            assert!(!p["coverage"]["excluded"].as_array().unwrap().is_empty());
        }
    }
    #[test]
    fn artifact_order_does_not_change_prepare_semantics() {
        let a = json!({"artifact_id":"one","sha256":"a".repeat(64),"size_bytes":1,"role":"weights","format_id":"format"});
        let b = json!({"artifact_id":"two","sha256":"b".repeat(64),"size_bytes":1,"role":"configuration","format_id":"format"});
        assert!(artifact_set(&json!([b.clone(), a.clone()]), &[a.clone(), b]).is_ok());
        assert!(artifact_set(&json!([a.clone(), a.clone()]), &[a.clone(), a]).is_err());
    }
    #[test]
    fn malformed_later_item_refuses_whole_request_before_execution() {
        let s = service();
        let c = s.context.profile["components"][0].clone();
        let sha = hash(canonical(&c).unwrap().as_bytes());
        let p = json!({"protocol":"nlp-worker.v2","request_id":"analyze.one","profile_sha256":s.context.profile_sha,"component_id":"one","component_sha256":sha,"tasks":["concept_mentions"],"deadline_ms":100,"items":[{"request_item_id":"first","text":"echo","text_sha256":hash(b"echo")},{"request_item_id":"second","text":"echo","text_sha256":"a".repeat(64)}]});
        assert!(validate_analyze(&s.context, &p, &c).is_err());
    }
}
