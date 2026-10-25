//! Same-image reconnect protocol. A disconnected client does not cancel work.
use crate::engine_acquisition::{Owner, Phase, Selection, Status};
use serde::{Deserialize, Serialize};
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};
type Result<T> = std::result::Result<T, &'static str>;
pub(crate) const FLAG: &str = "--engine-acquisition-broker";
pub(crate) const MAX_FRAME: usize = 4096;
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Peer {
    pub pid: u32,
    pub birth: u64,
    pub image: PathBuf,
    pub package: String,
    pub logon: String,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Journal {
    pub schema: String,
    pub nonce: String,
    pub binding: String,
    pub broker: Peer,
    pub launcher: Peer,
    pub committed: bool,
    pub selection: Selection,
    pub terminal: bool,
    pub status: Status,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quote_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub review_digest: Option<String>,
}
impl Journal {
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        if bytes.is_empty() || bytes.len() > MAX_FRAME {
            return Err("optional_reconnect_invalid");
        }
        let raw: serde_json::Value = crate::worker_host::decode(bytes)?;
        let j: Self =
            serde_json::from_value(raw.clone()).map_err(|_| "optional_reconnect_invalid")?;
        let review_valid = match j.schema.as_str() {
            "optional-engine-reconnect.v1" => !raw
                .as_object()
                .is_some_and(|v| v.contains_key("quote_sha256") || v.contains_key("review_digest")),
            "optional-engine-reconnect.v2" => j.review().is_ok(),
            _ => false,
        };
        if !review_valid
            || !crate::worker_host::hex(&j.nonce, 32)
            || !crate::worker_host::hex(&j.binding, 64)
            || j.broker.pid == 0
            || j.broker.birth == 0
            || !j.broker.image.is_absolute()
            || j.broker.package.is_empty()
            || j.broker.package.len() > 256
            || j.broker.logon.is_empty()
            || j.broker.logon.len() > 256
            || j.launcher.pid == 0
            || j.launcher.birth == 0
            || j.launcher.pid == j.broker.pid
            || j.launcher.image != j.broker.image
            || j.launcher.package != j.broker.package
            || j.launcher.logon != j.broker.logon
            || (!j.committed && j.status.installed)
            || j.status.operation_id.as_deref() != Some(&j.nonce)
            || j.status.progress_percent.is_some()
            || j.status.reason.as_ref().is_some_and(|s| {
                s.len() > 64 || !s.bytes().all(|b| b.is_ascii_lowercase() || b == b'_')
            })
            || j.terminal == j.status.in_flight
            || j.status.installed != (j.status.state == Phase::Installed)
            || (!j.terminal && j.status.installed)
            || (j.terminal
                && !matches!(
                    j.status.state,
                    Phase::Installed | Phase::Failed | Phase::RecoveryPending
                ))
        {
            return Err("optional_reconnect_invalid");
        }
        Ok(j)
    }
    pub(crate) fn review(&self) -> Result<crate::engine_acquisition_quote::Binding> {
        let binding = crate::engine_acquisition_quote::Binding {
            quote_sha256: self.quote_sha256.clone().ok_or("optional_quote_stale")?,
            review_digest: self.review_digest.clone().ok_or("optional_quote_stale")?,
        };
        if !binding.valid() {
            return Err("optional_quote_stale");
        }
        Ok(binding)
    }
    pub fn save(&self, data: &Path) -> Result<()> {
        let raw = serde_json::to_vec(self).map_err(|_| "optional_reconnect_invalid")?;
        Self::decode(&raw)?;
        crate::servicing_handoff::save_engine_reconnect(data, &raw)
    }
}
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Action {
    Status {},
    Cancel { operation_id: String },
    Commit {},
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    pub nonce: String,
    pub peer: Peer,
    pub action: Action,
}
pub(crate) fn same_peer(claim: &Peer, actual: &Peer, local: &Peer) -> bool {
    claim == actual
        && actual.pid != 0
        && actual.birth != 0
        && actual.image == local.image
        && actual.package == local.package
        && actual.logon == local.logon
}
pub(crate) fn current_journal(j: &Journal, binding: &str, peer: &Peer) -> Result<bool> {
    let same = j.binding == binding
        && j.broker.image == peer.image
        && j.broker.package == peer.package
        && j.broker.logon == peer.logon;
    if same {
        return Ok(true);
    }
    if j.terminal && matches!(j.status.state, Phase::Installed | Phase::Failed) {
        return Ok(false);
    }
    Err("optional_broker_release_changed")
}
pub(crate) fn commit_authorized(journal: &Journal, request: &Request) -> bool {
    matches!(request.action, Action::Commit {})
        && request.nonce == journal.nonce
        && request.peer == journal.launcher
}
pub(crate) fn requested(args: &[String]) -> bool {
    args.iter().skip(1).any(|s| s == FLAG)
}
pub(crate) fn arguments(args: &[String]) -> Result<(&str, usize)> {
    if args.len() != 4
        || args.get(1).is_none_or(|s| s != FLAG)
        || !crate::worker_host::hex(&args[2], 32)
    {
        return Err("optional_broker_arguments");
    }
    let h: usize = args[3].parse().map_err(|_| "optional_broker_arguments")?;
    if h < 4 || h == usize::MAX || h.to_string() != args[3] {
        return Err("optional_broker_arguments");
    }
    Ok((&args[2], h))
}
pub(crate) fn unknown(id: Option<String>, reason: &str) -> Status {
    Status {
        operation_id: id,
        state: Phase::RecoveryPending,
        reason: Some(reason.into()),
        ..Status::default()
    }
}
pub(crate) fn dispatch(owner: &Arc<Owner>, journal: &Journal, request: Request) -> Result<Status> {
    if request.nonce != journal.nonce {
        return Err("optional_broker_peer");
    }
    match request.action {
        Action::Status {} => Ok(owner.status()),
        Action::Commit {} if journal.committed && request.peer == journal.launcher => {
            Ok(owner.status())
        }
        Action::Commit {} => Err("optional_broker_not_started"),
        Action::Cancel { operation_id } => owner.cancel(&operation_id),
    }
}
#[path = "engine_acquisition_broker_windows.rs"]
mod windows;
pub(crate) fn run(args: &[String]) -> Result<()> {
    windows::run(args)
}
pub(crate) fn status(data: &Path) -> Status {
    windows::status(data).unwrap_or_else(|_| unknown(None, "optional_reconnect_unavailable"))
}
pub(crate) fn start(
    data: &Path,
    selection: Selection,
    review_digest: &str,
    cancelled: &impl Fn() -> bool,
) -> Result<Status> {
    windows::start(data, selection, review_digest, cancelled)
}
// Called only after normal Core startup admission, never by the broker role.
pub(crate) fn admitted_servicing() {
    // The old activation helper can briefly retain the same exclusive file.
    // A pending or invalid receipt never blocks base application usability.
    let _ = std::thread::spawn(|| {
        for _ in 0..20 {
            match windows::admitted_servicing() {
                Ok(true) | Err(_) => break,
                Ok(false) => std::thread::sleep(std::time::Duration::from_millis(100)),
            }
        }
    })
    .join();
}
pub(crate) fn cancel(data: &Path, id: &str) -> Result<Status> {
    windows::cancel(data, id)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn headless_route_is_closed() {
        let ok = vec![
            "Bridge.exe".into(),
            FLAG.into(),
            "a".repeat(32),
            "123".into(),
        ];
        assert_eq!(arguments(&ok).unwrap().1, 123);
        for bad in [
            vec!["Bridge.exe".into(), FLAG.into()],
            vec![
                "Bridge.exe".into(),
                FLAG.into(),
                "https://example.test".into(),
                "123".into(),
            ],
        ] {
            assert!(arguments(&bad).is_err());
        }
        let mut bad = ok.clone();
        bad.push("module".into());
        assert!(arguments(&bad).is_err());
        let mut bad = ok;
        bad[3] = "0123".into();
        assert!(arguments(&bad).is_err());
    }
    #[test]
    fn request_has_no_acquisition_or_resource_arguments() {
        for raw in [
            r#"{"action":"start","selection":"module"}"#,
            r#"{"action":"status","uri":"https://example.test"}"#,
            r#"{"action":"commit","uri":"https://example.test"}"#,
            r#"{"action":"commit","selection":"module"}"#,
        ] {
            assert!(serde_json::from_str::<Action>(raw).is_err());
        }
        assert_eq!(
            serde_json::from_str::<Action>(r#"{"action":"status"}"#).unwrap(),
            Action::Status {}
        );
    }
    #[test]
    fn unknown_is_not_live_or_ready() {
        let s = unknown(Some("a".repeat(32)), "optional_recovery_pending");
        assert!(!s.installed && !s.in_flight);
        assert_eq!(s.state, Phase::RecoveryPending);
    }
    fn journal() -> Journal {
        Journal {
            quote_sha256: None,
            review_digest: None,
            schema: "optional-engine-reconnect.v1".into(),
            nonce: "a".repeat(32),
            binding: "b".repeat(64),
            broker: Peer {
                pid: 12,
                birth: 34,
                image: PathBuf::from("C:/installed/app/Bridge.exe"),
                package: "Main_1.0.0.0_x64__publisher".into(),
                logon: "S-1-5-5-1-2".into(),
            },
            launcher: Peer {
                pid: 56,
                birth: 78,
                image: PathBuf::from("C:/installed/app/Bridge.exe"),
                package: "Main_1.0.0.0_x64__publisher".into(),
                logon: "S-1-5-5-1-2".into(),
            },
            committed: false,
            selection: Selection::Module,
            terminal: false,
            status: Status {
                operation_id: Some("a".repeat(32)),
                state: Phase::Checking,
                in_flight: true,
                ..Status::default()
            },
        }
    }
    #[test]
    fn journal_never_turns_partial_or_unknown_into_ready() {
        let good = journal();
        let bytes = serde_json::to_vec(&good).unwrap();
        assert!(Journal::decode(&bytes).is_ok());
        for mutate in [0, 1, 2, 3, 4] {
            let mut bad = good.clone();
            match mutate {
                0 => bad.status.installed = true,
                1 => bad.terminal = true,
                2 => bad.status.operation_id = None,
                3 => bad.schema = "optional-engine-acquisition.v1".into(),
                _ => bad.status.progress_percent = Some(10),
            }
            assert!(Journal::decode(&serde_json::to_vec(&bad).unwrap()).is_err());
        }
        assert!(Journal::decode(&vec![b' '; MAX_FRAME + 1]).is_err());
        let mut done = good;
        done.terminal = true;
        done.status.in_flight = false;
        done.status.state = Phase::Failed;
        assert!(Journal::decode(&serde_json::to_vec(&done).unwrap()).is_ok());
    }
    #[test]
    fn pid_reuse_release_image_and_logon_mismatch_refused() {
        let peer = journal().broker;
        assert!(same_peer(&peer, &peer, &peer));
        for which in 0..5 {
            let mut bad = peer.clone();
            match which {
                0 => bad.pid += 1,
                1 => bad.birth += 1,
                2 => bad.image = PathBuf::from("C:/other/Bridge.exe"),
                3 => bad.package.push('x'),
                _ => bad.logon.push('x'),
            }
            assert!(!same_peer(&peer, &bad, &peer));
            assert!(!same_peer(&bad, &bad, &peer) || which < 2);
        }
    }
    #[test]
    fn status_wire_rejects_unknown_authority_fields() {
        let mut value = serde_json::to_value(Status::default()).unwrap();
        value["uri"] = serde_json::json!("https://example.test");
        assert!(serde_json::from_value::<Status>(value).is_err());
        let mut value = serde_json::to_value(journal()).unwrap();
        value["startup_resume"] = serde_json::json!(true);
        assert!(Journal::decode(&serde_json::to_vec(&value).unwrap()).is_err());
    }
    #[test]
    fn only_original_launcher_can_commit_and_passive_routes_cannot_start() {
        let j = journal();
        let mut r = Request {
            nonce: j.nonce.clone(),
            peer: j.launcher.clone(),
            action: Action::Commit {},
        };
        assert!(commit_authorized(&j, &r));
        r.action = Action::Status {};
        assert!(!commit_authorized(&j, &r));
        r.action = Action::Cancel {
            operation_id: j.nonce.clone(),
        };
        assert!(!commit_authorized(&j, &r));
        r.action = Action::Commit {};
        r.peer.pid += 1;
        assert!(!commit_authorized(&j, &r));
        r.peer = j.launcher.clone();
        r.peer.birth += 1;
        assert!(!commit_authorized(&j, &r));
        r.peer = j.launcher.clone();
        r.nonce = "c".repeat(32);
        assert!(!commit_authorized(&j, &r));
        let bad = vec!["Bridge.exe".into(), "extra".into(), FLAG.into()];
        assert!(requested(&bad));
        assert!(arguments(&bad).is_err());
    }
    #[test]
    fn completed_history_cannot_become_current_readiness_or_block_new_generation() {
        let mut j = journal();
        let mut peer = j.broker.clone();
        peer.logon.push('x');
        assert!(current_journal(&j, &j.binding, &peer).is_err());
        j.terminal = true;
        j.status.in_flight = false;
        j.status.state = Phase::RecoveryPending;
        assert!(current_journal(&j, &j.binding, &peer).is_err());
        for state in [Phase::Installed, Phase::Failed] {
            j.status.state = state;
            assert!(!current_journal(&j, &j.binding, &peer).unwrap());
            assert!(current_journal(&j, &j.binding, &j.broker).unwrap());
            assert!(!current_journal(&j, &"f".repeat(64), &j.broker).unwrap());
            let mut newer = j.broker.clone();
            newer.image.push("new");
            newer.package.push('x');
            assert!(!current_journal(&j, &j.binding, &newer).unwrap());
        }
    }
    #[test]
    fn versioned_review_journal_is_closed_and_legacy_preview_stays_observable() {
        let mut old = journal();
        old.selection = Selection::Preview;
        let raw = serde_json::to_vec(&old).unwrap();
        let parsed = Journal::decode(&raw).unwrap();
        assert_eq!(parsed.selection, Selection::Preview);
        assert!(parsed.review().is_err());
        let mut value = serde_json::to_value(&old).unwrap();
        value["quote_sha256"] = serde_json::Value::Null;
        assert!(Journal::decode(&serde_json::to_vec(&value).unwrap()).is_err());
        let mut current = old.clone();
        current.schema = "optional-engine-reconnect.v2".into();
        current.quote_sha256 = Some("b".repeat(64));
        current.review_digest = Some("c".repeat(64));
        assert!(Journal::decode(&serde_json::to_vec(&current).unwrap())
            .unwrap()
            .review()
            .is_ok());
        for key in ["quote_sha256", "review_digest"] {
            let mut value = serde_json::to_value(&current).unwrap();
            value.as_object_mut().unwrap().remove(key);
            assert!(Journal::decode(&serde_json::to_vec(&value).unwrap()).is_err());
            let mut value = serde_json::to_value(&current).unwrap();
            value[key] = serde_json::Value::Null;
            assert!(Journal::decode(&serde_json::to_vec(&value).unwrap()).is_err());
        }
        current.review_digest = Some("uri".into());
        assert!(Journal::decode(&serde_json::to_vec(&current).unwrap()).is_err());
    }
}
