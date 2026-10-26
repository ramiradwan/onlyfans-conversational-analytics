//! Explicit optional-package ownership. Installation grants no analysis authority.
use crate::deployment::DesktopRelease;
use serde::{Deserialize, Serialize};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::JoinHandle,
};

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Selection {
    Module,
    Preview,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Phase {
    NotRequested,
    Checking,
    Acquiring,
    Cancelling,
    Installed,
    Failed,
    RecoveryPending,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Status {
    pub operation_id: Option<String>,
    pub state: Phase,
    pub installed: bool,
    pub in_flight: bool,
    pub cancel_requested: bool,
    pub deadline_reached: bool,
    // AddOptionalPackageAsync does not expose transfer byte counts.
    pub progress_percent: Option<u8>,
    pub reason: Option<String>,
    pub hresult: Option<i32>,
}
impl Default for Status {
    fn default() -> Self {
        Self {
            operation_id: None,
            state: Phase::NotRequested,
            installed: false,
            in_flight: false,
            cancel_requested: false,
            deadline_reached: false,
            progress_percent: None,
            reason: None,
            hresult: None,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Journal {
    schema: String,
    operation_id: String,
    binding_sha256: String,
    active: bool,
}
impl Journal {
    fn decode(bytes: &[u8]) -> Result<Self, &'static str> {
        let value: Self = crate::worker_host::decode(bytes)?;
        if value.schema != "optional-engine-acquisition.v1"
            || !crate::worker_host::hex(&value.operation_id, 32)
            || !crate::worker_host::hex(&value.binding_sha256, 64)
        {
            return Err("optional_intent_invalid");
        }
        Ok(value)
    }
    fn save(&self, data: &std::path::Path) -> Result<(), &'static str> {
        let bytes = serde_json::to_vec(self).map_err(|_| "optional_intent_invalid")?;
        crate::servicing_handoff::save_engine_acquisition(data, &bytes)
    }
}

struct Inner {
    status: Status,
    thread: Option<JoinHandle<()>>,
}
pub(crate) struct Owner {
    data: PathBuf,
    inner: Mutex<Inner>,
    cancel: AtomicBool,
    stopping: AtomicBool,
    remote: bool,
}
impl Owner {
    pub fn new(data: PathBuf) -> Arc<Self> {
        let mut owner = Self::new_local(data);
        Arc::get_mut(&mut owner).unwrap().remote = true;
        owner
    }
    pub(crate) fn new_local(data: PathBuf) -> Arc<Self> {
        let mut status = Status::default();
        match crate::servicing_handoff::load_engine_acquisition(&data)
            .and_then(|b| b.map(|b| Journal::decode(&b)).transpose())
        {
            Ok(Some(journal)) if journal.active => {
                status.operation_id = Some(journal.operation_id);
                status.state = Phase::RecoveryPending;
                status.reason = Some("optional_recovery_pending".into());
            }
            Err(_) => {
                status.state = Phase::RecoveryPending;
                status.reason = Some("optional_intent_invalid".into());
            }
            _ => {}
        }
        Arc::new(Self {
            data,
            inner: Mutex::new(Inner {
                status,
                thread: None,
            }),
            cancel: AtomicBool::new(false),
            stopping: AtomicBool::new(false),
            remote: false,
        })
    }
    pub fn status(&self) -> Status {
        if self.remote {
            return crate::engine_acquisition_broker::status(&self.data);
        }
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .status
            .clone()
    }
    pub fn busy(&self) -> bool {
        if self.remote {
            let s = self.status();
            return s.in_flight || s.state == Phase::RecoveryPending;
        }
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.status.in_flight
            || inner.status.state == Phase::RecoveryPending
            || inner.thread.as_ref().is_some_and(|t| !t.is_finished())
    }
    pub fn cancel(&self, operation: &str) -> Result<Status, &'static str> {
        if self.remote {
            return crate::engine_acquisition_broker::cancel(&self.data, operation);
        }
        if !crate::worker_host::hex(operation, 32) {
            return Err("optional_operation_unknown");
        }
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if inner.status.operation_id.as_deref() != Some(operation) {
            return Err("optional_operation_unknown");
        }
        if inner.status.in_flight {
            self.cancel.store(true, Ordering::SeqCst);
            inner.status.cancel_requested = true;
            inner.status.state = Phase::Cancelling;
        }
        Ok(inner.status.clone())
    }
    pub(crate) fn local_finished(&self) -> bool {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .thread
            .as_ref()
            .is_some_and(|t| t.is_finished())
    }
    pub fn request_stop(&self) {
        if self.remote {
            return;
        }
        self.cancel.store(true, Ordering::SeqCst);
    }
    pub fn drain(&self) -> bool {
        if self.remote {
            return true;
        }
        self.stopping.store(true, Ordering::SeqCst);
        self.request_stop();
        let thread = self
            .inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .thread
            .take();
        if let Some(thread) = thread {
            if thread.join().is_err() {
                self.update(|s| {
                    s.in_flight = false;
                    s.state = Phase::RecoveryPending;
                    s.reason = Some("optional_owner_failed".into());
                });
                return false;
            }
        }
        true
    }
    fn update(&self, action: impl FnOnce(&mut Status)) {
        action(&mut self.inner.lock().unwrap_or_else(|e| e.into_inner()).status);
    }
    pub fn start(
        self: &Arc<Self>,
        selection: Selection,
        release: Option<DesktopRelease>,
        review_digest: String,
        cancelled: impl Fn() -> bool + Send + 'static,
    ) -> Result<Status, &'static str> {
        if !crate::worker_host::hex(&review_digest, 64) {
            return Err("optional_quote_stale");
        }
        if self.remote {
            if cancelled() {
                return Err("optional_cancelled");
            }
            return crate::engine_acquisition_broker::start(
                &self.data,
                selection,
                &review_digest,
                &cancelled,
            );
        }
        let _ = release;
        Err("optional_broker_required")
    }
    pub(crate) fn start_local(
        self: &Arc<Self>,
        selection: Selection,
        release: DesktopRelease,
        id: String,
        review: crate::engine_acquisition_quote::Binding,
    ) -> Result<Status, &'static str> {
        if !review.valid_for(selection) {
            return Err("optional_quote_stale");
        }
        self.spawn_work_id(Some(id), move |owner, id| {
            platform::execute(owner, selection, Some(&release), id, &review, &|| {
                owner.cancel.load(Ordering::SeqCst)
            })
        })
    }
    #[cfg(test)]
    fn spawn_work(
        self: &Arc<Self>,
        work: impl FnOnce(&Owner, &str) -> Result<bool, platform::Failure> + Send + 'static,
    ) -> Result<Status, &'static str> {
        self.spawn_work_id(None, work)
    }
    fn spawn_work_id(
        self: &Arc<Self>,
        id: Option<String>,
        work: impl FnOnce(&Owner, &str) -> Result<bool, platform::Failure> + Send + 'static,
    ) -> Result<Status, &'static str> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if self.stopping.load(Ordering::SeqCst) {
            return Err("optional_owner_stopping");
        }
        if inner.status.in_flight || inner.thread.as_ref().is_some_and(|t| !t.is_finished()) {
            return Err("optional_acquisition_busy");
        }
        if let Some(thread) = inner.thread.take() {
            let _ = thread.join();
        }
        let mut nonce = [0u8; 16];
        getrandom::fill(&mut nonce).map_err(|_| "optional_random_unavailable")?;
        let id = id.unwrap_or_else(|| nonce.iter().map(|b| format!("{b:02x}")).collect::<String>());
        self.cancel.store(false, Ordering::SeqCst);
        inner.status = Status {
            operation_id: Some(id.clone()),
            state: Phase::Checking,
            in_flight: true,
            ..Status::default()
        };
        let owner = self.clone();
        let thread = std::thread::Builder::new()
            .name("optional-engine-acquisition".into())
            .spawn(move || {
                let result = work(&owner, &id);
                owner.update(|status| {
                    status.in_flight = false;
                    match result {
                        Ok(installed) => {
                            status.installed = installed;
                            status.state = if installed {
                                Phase::Installed
                            } else {
                                Phase::Failed
                            };
                        }
                        Err(error) => {
                            status.state = if error.recovery {
                                Phase::RecoveryPending
                            } else {
                                Phase::Failed
                            };
                            status.reason = Some(error.reason.into());
                            status.hresult = error.hresult;
                        }
                    }
                });
            })
            .map_err(|_| "optional_owner_unavailable");
        match thread {
            Ok(thread) => inner.thread = Some(thread),
            Err(error) => {
                inner.status.in_flight = false;
                inner.status.state = Phase::Failed;
                inner.status.reason = Some(error.into());
                return Err(error);
            }
        }
        Ok(inner.status.clone())
    }
}

#[path = "engine_acquisition_windows.rs"]
pub(crate) mod platform;

#[cfg(test)]
mod tests {
    use super::*;
    use std::{sync::mpsc, time::Duration};
    fn owner() -> Arc<Owner> {
        Arc::new(Owner {
            data: PathBuf::from("unused"),
            inner: Mutex::new(Inner {
                status: Status::default(),
                thread: None,
            }),
            cancel: AtomicBool::new(false),
            stopping: AtomicBool::new(false),
            remote: false,
        })
    }
    #[test]
    fn explicit_selection_is_closed() {
        for value in ["module", "preview"] {
            assert!(serde_json::from_str::<Selection>(&format!("\"{value}\"")).is_ok());
        }
        for value in [
            "startup",
            "install",
            "update",
            "analysis-run",
            "https://example.invalid/",
        ] {
            assert!(serde_json::from_str::<Selection>(&format!("\"{value}\"")).is_err());
        }
    }
    #[test]
    fn fresh_start_requires_review_and_never_uses_a_local_fallback() {
        let owner = owner();
        for selection in [Selection::Module, Selection::Preview] {
            assert_eq!(
                owner
                    .start(selection, None, String::new(), || false)
                    .unwrap_err(),
                "optional_quote_stale"
            );
            assert_eq!(
                owner
                    .start(selection, None, "a".repeat(64), || false)
                    .unwrap_err(),
                "optional_broker_required"
            );
        }
        assert!(owner.inner.lock().unwrap().thread.is_none());
        assert_eq!(owner.status().state, Phase::NotRequested);
    }
    #[test]
    fn no_owner_or_ready_state_is_created_by_observation() {
        let owner = owner();
        assert_eq!(owner.status().state, Phase::NotRequested);
        assert!(!owner.status().installed && !owner.busy());
        assert!(owner.inner.lock().unwrap().thread.is_none());
        assert!(owner.status().progress_percent.is_none());
    }
    #[test]
    fn one_owner_and_exact_cancellation_retain_work_until_joined() {
        let owner = owner();
        let (begun_rx, begun_tx) = (mpsc::channel::<()>(), mpsc::channel::<()>());
        let (started, observed) = begun_rx;
        let (release, wait) = begun_tx;
        let status = owner
            .spawn_work(move |_, _| {
                started.send(()).unwrap();
                wait.recv().unwrap();
                Ok(true)
            })
            .unwrap();
        observed.recv().unwrap();
        assert!(owner.busy());
        assert!(owner.spawn_work(|_, _| panic!("second owner")).is_err());
        assert!(owner.cancel(&"0".repeat(32)).is_err());
        assert!(!owner.cancel.load(Ordering::SeqCst));
        let cancelled = owner
            .cancel(status.operation_id.as_deref().unwrap())
            .unwrap();
        assert!(cancelled.in_flight && cancelled.cancel_requested && !cancelled.installed);
        let joined = Arc::new(AtomicBool::new(false));
        let drained = joined.clone();
        let retained = owner.clone();
        let drain = std::thread::spawn(move || {
            assert!(retained.drain());
            drained.store(true, Ordering::SeqCst);
        });
        std::thread::sleep(Duration::from_millis(20));
        assert!(!joined.load(Ordering::SeqCst));
        release.send(()).unwrap();
        drain.join().unwrap();
        assert_eq!(owner.status().state, Phase::Installed);
        assert!(owner.status().cancel_requested);
        assert!(!owner.busy());
        assert_eq!(
            owner.spawn_work(|_, _| panic!("closed owner")).unwrap_err(),
            "optional_owner_stopping"
        );
    }
    #[test]
    fn terminal_failure_never_becomes_installed() {
        let owner = owner();
        owner
            .spawn_work(|_, _| {
                Err(platform::Failure {
                    reason: "optional_terminal_not_completed",
                    hresult: Some(-12345),
                    recovery: false,
                })
            })
            .unwrap();
        assert!(owner.drain());
        let status = owner.status();
        assert_eq!(status.state, Phase::Failed);
        assert!(!status.installed && !status.in_flight);
        assert_eq!(status.hresult, Some(-12345));
    }
    #[test]
    fn unresolved_intent_remains_busy_without_claiming_a_live_process() {
        let owner = owner();
        owner
            .spawn_work(|_, _| {
                Err(platform::Failure {
                    reason: "optional_recovery_pending",
                    hresult: None,
                    recovery: true,
                })
            })
            .unwrap();
        assert!(owner.drain());
        let status = owner.status();
        assert_eq!(status.state, Phase::RecoveryPending);
        assert!(!status.in_flight && owner.busy());
        assert!(!status.installed);
    }
    #[test]
    fn historical_pending_without_owned_thread_can_quit_without_clearing_intent() {
        let owner = owner();
        owner.update(|s| {
            s.operation_id = Some("a".repeat(32));
            s.state = Phase::RecoveryPending;
            s.reason = Some("optional_recovery_pending".into());
        });
        assert!(owner.inner.lock().unwrap().thread.is_none());
        assert!(owner.drain());
        let status = owner.status();
        assert_eq!(
            status.operation_id.as_deref(),
            Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        );
        assert_eq!(status.state, Phase::RecoveryPending);
        assert_eq!(status.reason.as_deref(), Some("optional_recovery_pending"));
        assert!(!status.in_flight && !status.installed && owner.busy());
        assert!(owner.inner.lock().unwrap().thread.is_none());
    }
    #[test]
    fn journal_is_closed_and_binds_exact_owner_and_release() {
        let good = serde_json::json!({"schema":"optional-engine-acquisition.v1",
            "operation_id":"a".repeat(32),"binding_sha256":"b".repeat(64),"active":true});
        assert!(
            Journal::decode(&serde_json::to_vec(&good).unwrap())
                .unwrap()
                .active
        );
        for key in ["operation_id", "binding_sha256", "schema"] {
            let mut bad = good.clone();
            bad[key] = serde_json::json!("bad");
            assert!(Journal::decode(&serde_json::to_vec(&bad).unwrap()).is_err());
        }
        let mut bad = good;
        bad["uri"] = serde_json::json!("https://example.invalid/");
        assert!(Journal::decode(&serde_json::to_vec(&bad).unwrap()).is_err());
    }
}
