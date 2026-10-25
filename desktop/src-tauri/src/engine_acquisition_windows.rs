use super::*;
use crate::{
    deployment::{EngineRelease, InstalledArtifact},
    servicing,
};
use sha2::{Digest, Sha256};
use std::time::{Duration, Instant};
use windows_future::{AsyncStatus, IAsyncOperation};
use windows_runtime::{
    core::{Interface, HSTRING},
    ApplicationModel::{
        IPackageCatalogStatics, PackageCatalog, PackageCatalogAddOptionalPackageResult,
    },
    Management::Deployment::PackageManager,
};

const BUDGET: Duration = Duration::from_secs(300);
pub(crate) struct Failure {
    pub reason: &'static str,
    pub hresult: Option<i32>,
    pub recovery: bool,
}
impl From<&'static str> for Failure {
    fn from(reason: &'static str) -> Self {
        Self {
            reason,
            hresult: None,
            recovery: false,
        }
    }
}
fn windows_error(error: windows_runtime::core::Error) -> Failure {
    Failure {
        reason: "optional_windows_failed",
        hresult: Some(error.code().0),
        recovery: false,
    }
}
fn recovery(reason: &'static str) -> Failure {
    Failure {
        reason,
        hresult: None,
        recovery: true,
    }
}

fn reconcile_active(owner: &Owner, journal: &Journal, verify: impl FnOnce() -> bool) -> Failure {
    // Installed code is an observation, not proof that the previous operation settled.
    let installed = verify();
    owner.update(|s| {
        s.operation_id = Some(journal.operation_id.clone());
        s.state = Phase::RecoveryPending;
        s.installed = installed;
    });
    recovery("optional_recovery_pending")
}

pub(crate) fn declared(release: Option<&DesktopRelease>) -> Result<&EngineRelease, Failure> {
    let release = release.ok_or("optional_direct_release_required")?;
    if release.update_owner != "direct-appinstaller" {
        return Err("optional_direct_release_required".into());
    }
    match release.servicing.as_ref().map(|s| &s.installed_artifact) {
        Some(InstalledArtifact::RelatedBundle { engine, .. }) => Ok(engine),
        _ => Err("optional_release_required".into()),
    }
}

fn binding(
    release: &DesktopRelease,
    actual: &crate::deployment::PackageIdentity,
    engine: &EngineRelease,
) -> Result<String, Failure> {
    let bytes = serde_json::to_vec(&(
        &release.identity.name,
        &release.identity.publisher,
        &actual.version,
        &actual.family,
        &release.app_installer_uri,
        &engine.identity.name,
        &engine.identity.family,
        &engine.package_version,
    ))
    .map_err(|_| "optional_binding_invalid")?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

pub(crate) fn installed(
    release: &DesktopRelease,
    expected: &EngineRelease,
) -> Result<bool, Failure> {
    let (package, actual) = servicing::observe().map_err(windows_error)?;
    if servicing::bind(release, &actual) != servicing::UpdateSource::DirectAppinstaller {
        return Err("optional_direct_release_required".into());
    }
    servicing::automatic_policy(&package)?;
    let manager: PackageManager = servicing::activate_owned().map_err(windows_error)?;
    match servicing::selected_engine(&manager, release, &actual.identity)? {
        servicing::EngineSelection::Absent => Ok(false),
        servicing::EngineSelection::Present { version, .. } => {
            if version != expected.package_version {
                return Err("optional_identity_mismatch".into());
            }
            crate::worker_host::inspect_installed_engine()?;
            Ok(true)
        }
    }
}

fn catalog() -> Result<PackageCatalog, Failure> {
    let factory: IPackageCatalogStatics = unsafe {
        windows_runtime::Win32::System::WinRT::RoGetActivationFactory(&HSTRING::from(
            "Windows.ApplicationModel.PackageCatalog",
        ))
        .map_err(windows_error)?
    };
    let mut pointer = std::ptr::null_mut();
    let code = unsafe { (factory.vtable().OpenForCurrentPackage)(factory.as_raw(), &mut pointer) };
    servicing::owned_result(code, pointer).map_err(windows_error)
}

struct Optional<'a> {
    operation: &'a IAsyncOperation<PackageCatalogAddOptionalPackageResult>,
    owner: &'a Owner,
    terminal: Option<AsyncStatus>,
}
impl servicing::deployment_settlement::Operation for Optional<'_> {
    type Output = Result<(), Failure>;
    fn status(&mut self) -> servicing::deployment_settlement::Status {
        use servicing::deployment_settlement::Status;
        match self.operation.Status() {
            Ok(AsyncStatus::Started) => Status::Pending,
            Ok(status @ (AsyncStatus::Completed | AsyncStatus::Canceled | AsyncStatus::Error)) => {
                self.terminal = Some(status);
                Status::Terminal
            }
            other => {
                self.owner.update(|s| {
                    s.reason = Some("optional_status_unobservable".into());
                    if let Err(error) = other {
                        s.hresult = Some(error.code().0);
                    }
                });
                Status::Unobservable
            }
        }
    }
    fn cancel(&mut self) {
        self.owner.update(|s| {
            s.cancel_requested = true;
            s.state = Phase::Cancelling;
        });
        if let Err(error) = self.operation.Cancel() {
            self.owner.update(|s| {
                s.reason = Some("optional_cancel_request_failed".into());
                s.hresult = Some(error.code().0);
            });
        }
    }
    fn results(&mut self) -> Self::Output {
        let result = self.operation.GetResults().map_err(windows_error)?;
        result
            .ExtendedError()
            .map_err(windows_error)?
            .ok()
            .map_err(windows_error)?;
        if self.terminal != Some(AsyncStatus::Completed) {
            let hresult = self.operation.ErrorCode().ok().map(|code| code.0);
            return Err(Failure {
                reason: "optional_terminal_not_completed",
                hresult,
                recovery: false,
            });
        }
        Ok(())
    }
}

pub(super) fn execute(
    owner: &Owner,
    _selection: Selection,
    release: Option<&DesktopRelease>,
    id: &str,
    cancelled: &impl Fn() -> bool,
) -> Result<bool, Failure> {
    let previous = crate::servicing_handoff::load_engine_acquisition(&owner.data)
        .and_then(|b| b.map(|b| Journal::decode(&b)).transpose())
        .map_err(|_| recovery("optional_intent_invalid"))?;
    if let Some(journal) = previous.as_ref().filter(|j| j.active) {
        return Err(reconcile_active(owner, journal, || {
            if let (Ok(_apartment), Some(release), Ok(engine)) =
                (servicing::Apartment::new(), release, declared(release))
            {
                return matches!(installed(release, engine), Ok(true));
            }
            false
        }));
    }
    let _apartment = servicing::Apartment::new().map_err(windows_error)?;
    let expected = declared(release)?;
    let release = release.ok_or("optional_direct_release_required")?;
    if installed(release, expected)? {
        return Ok(true);
    }
    if cancelled() {
        return Err("optional_cancelled".into());
    }
    let (package, actual) = servicing::observe().map_err(windows_error)?;
    if servicing::bind(release, &actual) != servicing::UpdateSource::DirectAppinstaller {
        return Err("optional_direct_release_required".into());
    }
    servicing::automatic_policy(&package)?;
    let bytes = crate::servicing_feed::read_metadata(
        actual
            .app_installer_uri
            .as_deref()
            .ok_or("optional_direct_release_required")?,
        cancelled,
    )?;
    match crate::servicing_feed::parse_current_related(&bytes, release, &actual.identity)? {
        crate::servicing_feed::Target::RelatedBundle {
            engine_identity,
            engine,
            ..
        } if engine_identity == expected.identity && engine.version == expected.package_version => {
        }
        _ => return Err("optional_identity_mismatch".into()),
    }
    if cancelled() {
        return Err("optional_cancelled".into());
    }
    let catalog = catalog()?;
    let mut journal = Journal {
        schema: "optional-engine-acquisition.v1".into(),
        operation_id: id.into(),
        binding_sha256: binding(release, &actual.identity, expected)?,
        active: true,
    };
    journal.save(&owner.data)?; // Durable ownership precedes the Windows mutation.
    let operation = match catalog.AddOptionalPackageAsync(&HSTRING::from(&expected.identity.family))
    {
        Ok(operation) => operation,
        Err(error) => {
            journal.active = false;
            journal
                .save(&owner.data)
                .map_err(|_| recovery("optional_intent_unsettled"))?;
            return Err(windows_error(error));
        }
    };
    owner.update(|s| s.state = Phase::Acquiring);
    let started = Instant::now();
    let result = servicing::deployment_settlement::wait(
        &mut Optional {
            operation: &operation,
            owner,
            terminal: None,
        },
        || {
            let expired = started.elapsed() >= BUDGET;
            if expired {
                owner.update(|s| s.deadline_reached = true);
            }
            cancelled() || expired
        },
        || std::thread::sleep(Duration::from_millis(100)),
    );
    // Only wait's observed terminal path reaches this durable settlement.
    journal.active = false;
    journal
        .save(&owner.data)
        .map_err(|_| recovery("optional_intent_unsettled"))?;
    result?;
    if !installed(release, expected)? {
        return Err("optional_not_registered".into());
    }
    owner.update(|s| {
        s.reason = None;
        s.hresult = None;
    });
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn active_intent_cannot_be_settled_by_package_presence_or_absence() {
        for installed in [false, true] {
            let owner = Arc::new(Owner {
                data: PathBuf::from("unused"),
                inner: Mutex::new(Inner {
                    status: Status::default(),
                    thread: None,
                }),
                cancel: AtomicBool::new(false),
                stopping: AtomicBool::new(false),
                remote: false,
            });
            let journal = Journal {
                schema: "optional-engine-acquisition.v1".into(),
                operation_id: "a".repeat(32),
                binding_sha256: "b".repeat(64),
                active: true,
            };
            let before = serde_json::to_vec(&journal).unwrap();
            let failure = reconcile_active(&owner, &journal, || installed);
            assert!(failure.recovery);
            assert_eq!(failure.reason, "optional_recovery_pending");
            assert_eq!(before, serde_json::to_vec(&journal).unwrap());
            assert!(journal.active && owner.busy());
            assert_eq!(owner.status().installed, installed);
            assert!(!owner.status().in_flight);
            assert!(owner.drain());
            assert_eq!(owner.status().state, Phase::RecoveryPending);
            assert!(owner.busy());
        }
    }
    #[test]
    fn only_declared_direct_related_engine_is_admissible() {
        assert!(declared(None).is_err());
        let main: DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct.json"
        ))
        .unwrap();
        assert!(declared(Some(&main)).is_err());
        let mut related: DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct-v2-related.json"
        ))
        .unwrap();
        assert!(declared(Some(&related)).is_ok());
        related.update_owner = "microsoft-store".into();
        assert!(declared(Some(&related)).is_err());
        related.update_owner = "unconfigured".into();
        assert!(declared(Some(&related)).is_err());
    }
}
