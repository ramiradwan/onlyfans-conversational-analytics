//! Windows owns executable updates. Only the installed, approved source is used.
use crate::deployment::{DesktopRelease, PackageIdentity};
use serde::Serialize;
use std::time::{Duration, Instant};

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum UpdateSource {
    LegacyInno,
    DirectAppinstaller,
    MicrosoftStore,
    Unconfigured { reason: &'static str },
}

#[derive(Debug, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum Availability {
    NotChecked,
    NoUpdates,
    Available,
    Required,
    Unknown { reason: &'static str },
}

#[derive(Debug, Serialize)]
pub struct UpdateStatus {
    pub source: UpdateSource,
    pub availability: Availability,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Origin {
    Store,
    SignedDirect,
    Other,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Signature {
    Store,
    SignedDirect,
    Other,
}

#[derive(Clone, Debug)]
pub struct Observation {
    pub identity: PackageIdentity,
    pub origin: Origin,
    pub signature: Signature,
    pub app_installer_uri: Option<String>,
}

pub fn bind(release: &DesktopRelease, actual: &Observation) -> UpdateSource {
    let unconfigured = |reason| UpdateSource::Unconfigured { reason };
    if release.validate(&actual.identity).is_err() {
        return unconfigured("package_identity_mismatch");
    }
    match release.update_owner.as_str() {
        "direct-appinstaller" => {
            if actual.origin != Origin::SignedDirect || actual.signature != Signature::SignedDirect
            {
                return unconfigured("installation_owner_mismatch");
            }
            match actual.app_installer_uri.as_deref() {
                None => unconfigured("appinstaller_source_missing"),
                Some(uri) if Some(uri) != release.app_installer_uri.as_deref() => {
                    unconfigured("appinstaller_source_mismatch")
                }
                Some(_) => UpdateSource::DirectAppinstaller,
            }
        }
        "microsoft-store" => {
            if actual.origin == Origin::Store
                && actual.signature == Signature::Store
                && actual.app_installer_uri.is_none()
            {
                UpdateSource::MicrosoftStore
            } else {
                unconfigured("installation_owner_mismatch")
            }
        }
        _ => unconfigured("update_owner_unknown"),
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Wait {
    Continue,
    Cancelled,
    TimedOut,
}

fn wait_decision(started: Instant, now: Instant, cancelled: bool) -> Wait {
    if cancelled {
        Wait::Cancelled
    } else if now.duration_since(started) >= Duration::from_secs(20) {
        Wait::TimedOut
    } else {
        Wait::Continue
    }
}

/// A deployment result does not prove that a deferred registration has crossed
/// its process-exit boundary. Keep that distinction explicit for the coordinator.
#[cfg(feature = "qualification-tools")]
#[allow(dead_code)] // Not exposed by the shipping host until exit coordination is qualified.
#[derive(Debug, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum DeploymentStatus {
    Completed {
        registered: bool,
    },
    Refused {
        reason: &'static str,
    },
    Unknown {
        reason: &'static str,
        hresult: Option<i32>,
    },
}

#[cfg(all(windows, feature = "qualification-tools"))]
#[allow(dead_code)] // Qualification fixture only; no shipping command starts deployment.
pub fn probe_appinstaller_deferral(
    release: &DesktopRelease,
    cancelled: impl Fn() -> bool,
) -> DeploymentStatus {
    let refused = |reason| DeploymentStatus::Refused { reason };
    if release.purpose != "qualification" {
        return refused("qualification_required");
    }
    if cancelled() {
        return refused("deployment_cancelled");
    }
    let Ok(_apartment) = Apartment::new() else {
        return refused("windows_source_unavailable");
    };
    let Ok((_, actual)) = observe() else {
        return refused("windows_source_unavailable");
    };
    if bind(release, &actual) != UpdateSource::DirectAppinstaller {
        return refused("direct_source_required");
    }
    let result = deploy_registered(&actual, &cancelled);
    result.unwrap_or_else(|error| DeploymentStatus::Unknown {
        reason: "windows_deployment_failed",
        hresult: Some(error.code().0),
    })
}

#[cfg(all(windows, feature = "qualification-tools"))]
#[allow(dead_code)]
fn deploy_registered(
    actual: &Observation,
    cancelled: &impl Fn() -> bool,
) -> windows_runtime::core::Result<DeploymentStatus> {
    use windows_future::AsyncStatus;
    use windows_runtime::{
        core::{Interface, HSTRING},
        Foundation::{IUriRuntimeClassFactory, Uri},
        Management::Deployment::{AddPackageOptions, PackageManager},
    };
    if cancelled() {
        return Ok(DeploymentStatus::Refused {
            reason: "deployment_cancelled",
        });
    }
    // No caller-supplied URI, force shutdown, downgrade or replacement options.
    let Some(registered_uri) = actual.app_installer_uri.as_deref() else {
        return Ok(DeploymentStatus::Refused {
            reason: "direct_source_required",
        });
    };
    let factory: IUriRuntimeClassFactory = unsafe {
        windows_runtime::Win32::System::WinRT::RoGetActivationFactory(&HSTRING::from(
            "Windows.Foundation.Uri",
        ))?
    };
    let raw_uri = HSTRING::from(registered_uri);
    let mut pointer = std::ptr::null_mut();
    let code = unsafe {
        (factory.vtable().CreateUri)(
            factory.as_raw(),
            std::mem::transmute_copy(&raw_uri),
            &mut pointer,
        )
    };
    let uri: Uri = owned_result(code, pointer)?;
    // Probe the documented URI/options combination. Installed qualification
    // returned E_INVALIDARG: it is deliberately unavailable to the shipping host.
    let options: AddPackageOptions = activate_owned()?;
    options.SetDeferRegistrationWhenPackagesAreInUse(true)?;
    let manager: PackageManager = activate_owned()?;
    let operation = manager.AddPackageByUriAsync(&uri, &options)?;
    struct CancelOnDrop<F: Fn()>(F);
    impl<F: Fn()> Drop for CancelOnDrop<F> {
        fn drop(&mut self) {
            (self.0)();
        }
    }
    let _cancel = CancelOnDrop(|| {
        let _ = operation.Cancel();
    });
    let started = Instant::now();
    loop {
        let cancelled_now = cancelled();
        if cancelled_now || started.elapsed() >= Duration::from_secs(300) {
            let _ = operation.Cancel();
            return Ok(DeploymentStatus::Unknown {
                hresult: None,
                reason: if cancelled_now {
                    "deployment_cancelled"
                } else {
                    "deployment_timed_out"
                },
            });
        }
        match operation.Status()? {
            AsyncStatus::Started => std::thread::sleep(Duration::from_millis(100)),
            AsyncStatus::Completed => {
                let result = operation.GetResults()?;
                result.ExtendedErrorCode()?.ok()?;
                return Ok(DeploymentStatus::Completed {
                    registered: result.IsRegistered()?,
                });
            }
            AsyncStatus::Error => {
                operation.GetResults()?;
                return Ok(DeploymentStatus::Unknown {
                    reason: "windows_deployment_failed",
                    hresult: None,
                });
            }
            _ => {
                return Ok(DeploymentStatus::Unknown {
                    reason: "windows_deployment_failed",
                    hresult: None,
                })
            }
        }
    }
}

#[cfg(windows)]
pub fn inspect(release: Option<&DesktopRelease>, cancelled: impl Fn() -> bool) -> UpdateStatus {
    let Some(release) = release else {
        return UpdateStatus {
            source: UpdateSource::LegacyInno,
            availability: Availability::NotChecked,
        };
    };
    let unavailable = || UpdateStatus {
        source: UpdateSource::Unconfigured {
            reason: "windows_source_unavailable",
        },
        availability: Availability::NotChecked,
    };
    if cancelled() {
        return unavailable();
    }
    let Ok(_apartment) = Apartment::new() else {
        return unavailable();
    };
    let Ok((package, actual)) = observe() else {
        return unavailable();
    };
    let source = bind(release, &actual);
    let availability = if source == UpdateSource::DirectAppinstaller {
        check_availability(&package, &cancelled).unwrap_or(Availability::Unknown {
            reason: "windows_check_failed",
        })
    } else {
        // Store servicing belongs to Store. Never query/register the direct feed.
        Availability::NotChecked
    };
    UpdateStatus {
        source,
        availability,
    }
}

#[cfg(windows)]
struct Apartment;
// Avoid the generated process-static activation-factory cache: after the last
// worker calls RoUninitialize, a later check must obtain fresh COM interfaces.
#[cfg(windows)]
fn activate_owned<T: windows_runtime::core::Interface + windows_runtime::core::RuntimeName>(
) -> windows_runtime::core::Result<T> {
    use windows_runtime::core::{Interface, HSTRING};
    unsafe { windows_runtime::Win32::System::WinRT::RoActivateInstance(&HSTRING::from(T::NAME))? }
        .cast()
}
#[cfg(windows)]
fn owned_result<T: windows_runtime::core::Interface>(
    code: windows_runtime::core::HRESULT,
    pointer: *mut std::ffi::c_void,
) -> windows_runtime::core::Result<T> {
    let object = if pointer.is_null() {
        None
    } else {
        Some(unsafe { T::from_raw(pointer) })
    };
    code.ok()?;
    object.ok_or_else(|| {
        windows_runtime::core::Error::from_hresult(windows_runtime::core::HRESULT(
            0x80004003u32 as i32,
        ))
    })
}
#[cfg(windows)]
impl Apartment {
    fn new() -> windows_runtime::core::Result<Self> {
        unsafe {
            windows_runtime::Win32::System::WinRT::RoInitialize(
                windows_runtime::Win32::System::WinRT::RO_INIT_MULTITHREADED,
            )?;
        }
        Ok(Self)
    }
}
#[cfg(windows)]
impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { windows_runtime::Win32::System::WinRT::RoUninitialize() }
    }
}

#[cfg(windows)]
fn observe(
) -> windows_runtime::core::Result<(windows_runtime::ApplicationModel::Package, Observation)> {
    use windows_runtime::{
        ApplicationModel::{IPackageStatics, Package, PackageSignatureKind},
        System::ProcessorArchitecture,
    };
    use windows_sys::Win32::Storage::Packaging::Appx::{
        GetStagedPackageOrigin, PackageOrigin_DeveloperSigned as ORIGIN_DEVELOPER_SIGNED,
        PackageOrigin_LineOfBusiness as ORIGIN_LINE_OF_BUSINESS,
        PackageOrigin_Store as ORIGIN_STORE,
    };
    let factory: IPackageStatics = unsafe {
        windows_runtime::Win32::System::WinRT::RoGetActivationFactory(
            &windows_runtime::core::HSTRING::from("Windows.ApplicationModel.Package"),
        )?
    };
    use windows_runtime::core::Interface;
    let mut pointer = std::ptr::null_mut();
    let code = unsafe { (factory.vtable().Current)(factory.as_raw(), &mut pointer) };
    let package: Package = owned_result(code, pointer)?;
    let id = package.Id()?;
    let version = id.Version()?;
    let full_name: Vec<u16> = id.FullName()?.iter().copied().chain(Some(0)).collect();
    let mut origin = 0;
    let result = unsafe { GetStagedPackageOrigin(full_name.as_ptr(), &mut origin) };
    if result != 0 {
        return Err(windows_runtime::core::Error::from_hresult(
            windows_runtime::core::HRESULT::from_win32(result),
        ));
    }
    let signature = match package.SignatureKind()? {
        PackageSignatureKind::Store => Signature::Store,
        PackageSignatureKind::Developer | PackageSignatureKind::Enterprise => {
            Signature::SignedDirect
        }
        _ => Signature::Other,
    };
    let app_installer_uri = registered_uri(&package)?;
    Ok((
        package,
        Observation {
            identity: PackageIdentity {
                name: id.Name()?.to_string(),
                publisher: id.Publisher()?.to_string(),
                version: format!(
                    "{}.{}.{}.{}",
                    version.Major, version.Minor, version.Build, version.Revision
                ),
                architecture: if id.Architecture()? == ProcessorArchitecture::X64 {
                    "x64"
                } else {
                    "unsupported"
                }
                .into(),
            },
            origin: match origin {
                ORIGIN_STORE => Origin::Store,
                ORIGIN_DEVELOPER_SIGNED | ORIGIN_LINE_OF_BUSINESS => Origin::SignedDirect,
                _ => Origin::Other,
            },
            signature,
            app_installer_uri,
        },
    ))
}

#[cfg(windows)]
fn registered_uri(
    package: &windows_runtime::ApplicationModel::Package,
) -> windows_runtime::core::Result<Option<String>> {
    use windows_runtime::{
        core::Interface,
        ApplicationModel::{AppInstallerInfo, IPackage6},
    };
    let interface: IPackage6 = package.cast()?;
    // GetAppInstallerInfo permits S_OK + null. The generated nonnullable Rust
    // projection turns null into an error and loses that distinction. Preserve
    // the HRESULT and transfer only a nonnull, owned interface to its RAII wrapper.
    let mut pointer = std::ptr::null_mut();
    let result =
        unsafe { (interface.vtable().GetAppInstallerInfo)(interface.as_raw(), &mut pointer) };
    let info = if pointer.is_null() {
        None
    } else {
        Some(unsafe { AppInstallerInfo::from_raw(pointer) })
    };
    result.ok()?;
    info.map(|info| Ok(info.Uri()?.RawUri()?.to_string()))
        .transpose()
}

#[cfg(windows)]
fn check_availability(
    current: &windows_runtime::ApplicationModel::Package,
    cancelled: &impl Fn() -> bool,
) -> windows_runtime::core::Result<Availability> {
    use windows_future::AsyncStatus;
    use windows_runtime::{
        core::HSTRING, ApplicationModel::PackageUpdateAvailability,
        Management::Deployment::PackageManager,
    };
    // Microsoft documents an access-denied issue when checking Package.Current
    // directly. Resolve only this user's current full name through PackageManager.
    let manager: PackageManager = activate_owned()?;
    let package = manager
        .FindPackageByUserSecurityIdPackageFullName(&HSTRING::new(), &current.Id()?.FullName()?)?;
    if cancelled() {
        return Ok(Availability::Unknown {
            reason: "check_cancelled",
        });
    }
    let operation = package.CheckUpdateAvailabilityAsync()?;
    struct CancelOnDrop<F: Fn()>(F);
    impl<F: Fn()> Drop for CancelOnDrop<F> {
        fn drop(&mut self) {
            (self.0)();
        }
    }
    let _cancel = CancelOnDrop(|| {
        let _ = operation.Cancel();
    });
    let started = Instant::now();
    loop {
        let wait = wait_decision(started, Instant::now(), cancelled());
        if wait != Wait::Continue {
            let _ = operation.Cancel();
            return Ok(Availability::Unknown {
                reason: if wait == Wait::Cancelled {
                    "check_cancelled"
                } else {
                    "check_timed_out"
                },
            });
        }
        match operation.Status()? {
            AsyncStatus::Started => std::thread::sleep(Duration::from_millis(100)),
            AsyncStatus::Completed => {
                let result = operation.GetResults()?;
                if result.ExtendedError()?.is_err() {
                    return Ok(Availability::Unknown {
                        reason: "windows_check_failed",
                    });
                }
                return Ok(match result.Availability()? {
                    PackageUpdateAvailability::NoUpdates => Availability::NoUpdates,
                    PackageUpdateAvailability::Available => Availability::Available,
                    PackageUpdateAvailability::Required => Availability::Required,
                    _ => Availability::Unknown {
                        reason: "windows_availability_unknown",
                    },
                });
            }
            _ => {
                return Ok(Availability::Unknown {
                    reason: "windows_check_failed",
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn release(store: bool) -> DesktopRelease {
        serde_json::from_str(if store {
            include_str!("../../../shared/desktop-release/qualification-store.json")
        } else {
            include_str!("../../../shared/desktop-release/qualification-direct.json")
        })
        .unwrap()
    }
    fn observation(store: bool) -> Observation {
        let value = release(store);
        Observation {
            identity: PackageIdentity {
                name: value.identity.name,
                publisher: value.identity.publisher,
                version: "1.0.1.0".into(),
                architecture: "x64".into(),
            },
            origin: if store {
                Origin::Store
            } else {
                Origin::SignedDirect
            },
            signature: if store {
                Signature::Store
            } else {
                Signature::SignedDirect
            },
            app_installer_uri: value.app_installer_uri,
        }
    }
    #[test]
    fn direct_binding_requires_exact_registered_source_and_signed_origin() {
        let release = release(false);
        let actual = observation(false);
        assert_eq!(bind(&release, &actual), UpdateSource::DirectAppinstaller);
        let mut raw = actual.clone();
        raw.app_installer_uri = None;
        assert_eq!(
            bind(&release, &raw),
            UpdateSource::Unconfigured {
                reason: "appinstaller_source_missing"
            }
        );
        for uri in [
            "https://other.example.test/feed.appinstaller",
            &(release.app_installer_uri.clone().unwrap() + "/"),
            &release
                .app_installer_uri
                .clone()
                .unwrap()
                .replace("https:", "HTTPS:"),
        ] {
            raw.app_installer_uri = Some(uri.into());
            assert_eq!(
                bind(&release, &raw),
                UpdateSource::Unconfigured {
                    reason: "appinstaller_source_mismatch"
                }
            );
        }
        for origin in [Origin::Store, Origin::Other] {
            let mut raw = actual.clone();
            raw.origin = origin;
            assert!(matches!(
                bind(&release, &raw),
                UpdateSource::Unconfigured { .. }
            ));
        }
        for signature in [Signature::Store, Signature::Other] {
            let mut raw = actual.clone();
            raw.signature = signature;
            assert!(matches!(
                bind(&release, &raw),
                UpdateSource::Unconfigured { .. }
            ));
        }
    }
    #[test]
    fn store_binding_requires_store_installation_without_a_direct_feed() {
        let release = release(true);
        let actual = observation(true);
        assert_eq!(bind(&release, &actual), UpdateSource::MicrosoftStore);
        let mut raw = actual.clone();
        raw.app_installer_uri = Some("https://other.example.test/feed.appinstaller".into());
        assert!(matches!(
            bind(&release, &raw),
            UpdateSource::Unconfigured { .. }
        ));
        let mut raw = actual.clone();
        raw.origin = Origin::SignedDirect;
        assert!(matches!(
            bind(&release, &raw),
            UpdateSource::Unconfigured { .. }
        ));
        let mut raw = actual.clone();
        raw.signature = Signature::SignedDirect;
        assert!(matches!(
            bind(&release, &raw),
            UpdateSource::Unconfigured { .. }
        ));
    }
    #[test]
    fn descriptor_and_os_identity_must_agree_before_update_observation() {
        let release = release(false);
        for field in ["name", "publisher", "version", "architecture"] {
            let mut raw = observation(false);
            match field {
                "name" => raw.identity.name = "Other".into(),
                "publisher" => raw.identity.publisher = "CN=Other".into(),
                "version" => raw.identity.version = "1.0.2.0".into(),
                _ => raw.identity.architecture = "arm64".into(),
            }
            assert_eq!(
                bind(&release, &raw),
                UpdateSource::Unconfigured {
                    reason: "package_identity_mismatch"
                }
            );
        }
    }
    #[test]
    fn async_wait_has_a_fixed_budget_and_observes_cancellation_first() {
        let start = Instant::now();
        assert_eq!(
            wait_decision(start, start + Duration::from_secs(19), false),
            Wait::Continue
        );
        assert_eq!(
            wait_decision(start, start + Duration::from_secs(20), false),
            Wait::TimedOut
        );
        assert_eq!(
            wait_decision(start, start + Duration::from_secs(40), false),
            Wait::TimedOut
        );
        assert_eq!(
            wait_decision(start, start + Duration::from_secs(40), true),
            Wait::Cancelled
        );
    }
    #[cfg(windows)]
    #[test]
    fn native_nonpackaged_process_cannot_report_managed_updates() {
        assert_eq!(inspect(None, || false).source, UpdateSource::LegacyInno);
        // Calls release their COM apartments. Fresh workers must not reuse a
        // stale process-static factory after the previous MTA is torn down.
        for _ in 0..4 {
            let status = std::thread::spawn(|| inspect(Some(&release(false)), || false))
                .join()
                .unwrap();
            assert!(matches!(status.source, UpdateSource::Unconfigured { .. }));
            assert_eq!(status.availability, Availability::NotChecked);
        }
    }
    #[cfg(all(windows, feature = "qualification-tools"))]
    #[test]
    fn deployment_refuses_cancelled_or_nonpackaged_callers_before_starting() {
        assert_eq!(
            probe_appinstaller_deferral(&release(false), || true),
            DeploymentStatus::Refused {
                reason: "deployment_cancelled",
            }
        );
        assert_eq!(
            probe_appinstaller_deferral(&release(false), || false),
            DeploymentStatus::Refused {
                reason: "windows_source_unavailable",
            }
        );
        let mut production = release(false);
        production.purpose = "production".into();
        assert_eq!(
            probe_appinstaller_deferral(&production, || false),
            DeploymentStatus::Refused {
                reason: "qualification_required",
            }
        );
    }
}
