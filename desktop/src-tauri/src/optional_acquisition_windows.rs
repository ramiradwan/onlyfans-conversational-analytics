use super::*;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    mem::size_of,
    path::PathBuf,
    ptr::null_mut,
    time::Instant,
};
use windows_future::AsyncStatus;
use windows_runtime::{
    core::{Interface, HSTRING},
    ApplicationModel::{IPackageCatalogStatics, Package, PackageCatalog},
    Management::Deployment::{
        AddPackageByAppInstallerOptions, PackageManager, PackageTypes, PackageVolume,
    },
};
use windows_sys::Win32::{
    Foundation::*, Security::*, Storage::Packaging::Appx::GetCurrentPackageFullName,
    System::Threading::*,
};

struct Handle(HANDLE);
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Owner {
    nonce: String,
    core_full_name: String,
    operation: String,
}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
struct Trace {
    file: File,
    records: usize,
    bytes: usize,
    started: Instant,
}
impl Trace {
    fn stage(&mut self, stage: &'static str, value: Value) -> Result<()> {
        let mut bytes = serde_json::to_vec(
            &json!({"stage":stage,"elapsed_ms":self.started.elapsed().as_millis(),"value":value}),
        )
        .map_err(|_| refused("evidence_encoding_failed"))?;
        bytes.push(b'\n');
        require(
            self.records < 64 && bytes.len() <= 16_384 && self.bytes + bytes.len() <= 262_144,
            "evidence_bound_exceeded",
        )?;
        self.file
            .write_all(&bytes)
            .and_then(|_| self.file.flush())
            .and_then(|_| self.file.sync_data())
            .map_err(|_| refused("evidence_write_failed"))?;
        self.records += 1;
        self.bytes += bytes.len();
        Ok(())
    }
}
fn bounded_file(path: &Path, limit: u64) -> Result<Vec<u8>> {
    use std::os::windows::fs::MetadataExt;
    let metadata =
        std::fs::symlink_metadata(path).map_err(|_| refused("owned_input_unavailable"))?;
    require(
        metadata.is_file() && metadata.file_attributes() & 0x400 == 0 && metadata.len() <= limit,
        "regular_bounded_input_required",
    )?;
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| file.take(limit + 1).read_to_end(&mut bytes))
        .map_err(|_| refused("owned_input_unavailable"))?;
    require(
        bytes.len() as u64 <= limit,
        "regular_bounded_input_required",
    )?;
    Ok(bytes)
}
fn token_integer(token: HANDLE, kind: TOKEN_INFORMATION_CLASS) -> Result<u32> {
    unsafe {
        let mut value = 0u32;
        let mut length = 0u32;
        require(
            GetTokenInformation(token, kind, (&mut value as *mut u32).cast(), 4, &mut length) != 0
                && length == 4,
            "token_observation_failed",
        )?;
        Ok(value)
    }
}
fn caller() -> Result<Caller> {
    unsafe {
        let mut raw = null_mut();
        require(
            OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw) != 0,
            "token_observation_failed",
        )?;
        let token = Handle(raw);
        let mut length = 0;
        GetTokenInformation(token.0, TokenIntegrityLevel, null_mut(), 0, &mut length);
        require(
            (size_of::<TOKEN_MANDATORY_LABEL>() as u32..=4096).contains(&length),
            "integrity_label_invalid",
        )?;
        let mut data = vec![0usize; (length as usize).div_ceil(size_of::<usize>())];
        require(
            GetTokenInformation(
                token.0,
                TokenIntegrityLevel,
                data.as_mut_ptr().cast(),
                length,
                &mut length,
            ) != 0,
            "token_observation_failed",
        )?;
        let label = &*data.as_ptr().cast::<TOKEN_MANDATORY_LABEL>();
        let first = data.as_ptr() as usize;
        let end = first + data.len() * size_of::<usize>();
        let sid = label.Label.Sid as usize;
        require(
            sid >= first && sid.checked_add(8).is_some_and(|last| last <= end),
            "integrity_label_invalid",
        )?;
        let count = *GetSidSubAuthorityCount(label.Label.Sid);
        require(
            (1..=15).contains(&count)
                && sid
                    .checked_add(8 + 4 * count as usize)
                    .is_some_and(|last| last <= end)
                && IsValidSid(label.Label.Sid) != 0,
            "integrity_label_invalid",
        )?;
        let integrity_rid = *GetSidSubAuthority(label.Label.Sid, count as u32 - 1);
        let mut package_length = 0;
        let code = GetCurrentPackageFullName(&mut package_length, null_mut());
        let package = if code == APPMODEL_ERROR_NO_PACKAGE {
            None
        } else {
            require(
                code == ERROR_INSUFFICIENT_BUFFER && (2..=256).contains(&package_length),
                "caller_package_unavailable",
            )?;
            let mut name = vec![0u16; package_length as usize];
            require(
                GetCurrentPackageFullName(&mut package_length, name.as_mut_ptr()) == ERROR_SUCCESS
                    && package_length >= 2
                    && package_length as usize <= name.len()
                    && name[package_length as usize - 1] == 0,
                "caller_package_unavailable",
            )?;
            Some(
                String::from_utf16(&name[..package_length as usize - 1])
                    .map_err(|_| refused("caller_package_unavailable"))?,
            )
        };
        let session_id = token_integer(token.0, TokenSessionId)?;
        Ok(Caller {
            elevated: token_integer(token.0, TokenElevation)? != 0,
            elevation_type: token_integer(token.0, TokenElevationType)?,
            app_container: token_integer(token.0, TokenIsAppContainer)? != 0,
            integrity_rid,
            package,
            session_id,
        })
    }
}
fn full_name(profile: &Profile, engine: bool) -> String {
    let suffix = profile.core_family.rsplit_once('_').unwrap().1;
    if engine {
        format!(
            "{}_{}_x64__{}",
            profile.engine_name, profile.engine_version, suffix
        )
    } else {
        format!(
            "{}_{}_x64__{}",
            profile.core_name, profile.core_version, suffix
        )
    }
}
fn packages(manager: &PackageManager, profile: &Profile, engine: bool) -> Result<Vec<Package>> {
    let iterable = manager.FindPackagesByUserSecurityIdNamePublisherWithPackageTypes(
        &HSTRING::new(),
        &HSTRING::from(if engine {
            &profile.engine_name
        } else {
            &profile.core_name
        }),
        &HSTRING::from(&profile.publisher),
        if engine {
            PackageTypes::Optional
        } else {
            PackageTypes::Main
        },
    )?;
    let mut found = Vec::new();
    let iterator = iterable.First()?;
    while iterator.HasCurrent()? {
        require(found.len() < 2, "duplicate_package_registration")?;
        found.push(iterator.Current()?);
        iterator.MoveNext()?;
    }
    Ok(found)
}
fn package_record(package: &Package) -> Result<Value> {
    let (_, observed) = super::super::observe_package(package.clone())?;
    let id = package.Id()?;
    Ok(
        json!({"name":observed.identity.name,"publisher":observed.identity.publisher,"version":observed.identity.version,
        "architecture":observed.identity.architecture,"family":id.FamilyName()?.to_string(),"full_name":id.FullName()?.to_string(),
        "source":observed.app_installer_uri,"installed_path":package.InstalledPath()?.to_string(),
        "optional":package.IsOptional()?,"bundle":package.IsBundle()?,"status_ok":package.Status()?.VerifyIsOK()?,
        "signed_direct":observed.origin == super::super::Origin::SignedDirect && observed.signature == super::super::Signature::SignedDirect}),
    )
}
fn verify_package(
    package: &Package,
    profile: &Profile,
    engine: bool,
    trace: &mut Trace,
    stage: &'static str,
) -> Result<()> {
    let record = package_record(package)?;
    trace.stage(stage, record.clone())?;
    require(
        record["full_name"].as_str() == Some(full_name(profile, engine).as_str())
            && record["publisher"].as_str() == Some(profile.publisher.as_str())
            && record["family"].as_str()
                == Some(if engine {
                    profile.engine_family.as_str()
                } else {
                    profile.core_family.as_str()
                })
            && record["architecture"] == "x64"
            && record["optional"] == engine
            && record["bundle"] == false
            && record["status_ok"] == true
            && record["signed_direct"] == true,
        "installed_identity_mismatch",
    )
}
fn verify_source(
    package: &Package,
    profile: &Profile,
    trace: &mut Trace,
    stage: &'static str,
) -> Result<()> {
    let info = super::super::registered_info(package)?
        .ok_or_else(|| refused("registered_source_missing"))?;
    let source = info.Uri()?.RawUri()?.to_string();
    macro_rules! uri_list {
        ($expression:expr) => {{
            let view = $expression?;
            let count = view.Size()?;
            require(count <= 8, "registered_uri_list_bound_exceeded")?;
            let mut values = Vec::new();
            for index in 0..count {
                let value = view.GetAt(index)?.RawUri()?.to_string();
                require(value.len() <= 2048, "registered_uri_bound_exceeded")?;
                values.push(value);
            }
            values
        }};
    }
    let update_uris = uri_list!(info.UpdateUris());
    let optional_uris = uri_list!(info.OptionalPackageUris());
    let dependency_uris = uri_list!(info.DependencyPackageUris());
    let pause = match super::super::paused_until(&info) {
        Ok(value) => json!({"available":true,"universal_time":value}),
        Err(error) => json!({"available":false,"hresult":error.code().0}),
    };
    trace.stage(stage, json!({"uri":source,"on_launch":info.OnLaunch()?,"hours":info.HoursBetweenUpdateChecks()?,
        "show_prompt":info.ShowPrompt()?,"blocks_activation":info.UpdateBlocksActivation()?,"background":info.AutomaticBackgroundTask()?,
        "force_any_version":info.ForceUpdateFromAnyVersion()?,"policy_source":info.PolicySource()?.0,
        "pause":pause,"update_uris":update_uris,"optional_uris":optional_uris,"dependency_uris":dependency_uris}))?;
    require(source == profile.source_uri, "registered_source_mismatch")?;
    super::super::automatic_policy(package).map_err(refused)
}
fn open_catalog() -> Result<PackageCatalog> {
    let factory: IPackageCatalogStatics = unsafe {
        windows_runtime::Win32::System::WinRT::RoGetActivationFactory(&HSTRING::from(
            "Windows.ApplicationModel.PackageCatalog",
        ))?
    };
    let mut pointer = null_mut();
    let result =
        unsafe { (factory.vtable().OpenForCurrentPackage)(factory.as_raw(), &mut pointer) };
    Ok(super::super::owned_result(result, pointer)?)
}
fn acquire(
    profile: &Profile,
    root: &Path,
    trace: &mut Trace,
    cancel_probe: bool,
) -> Result<Option<Package>> {
    trace.stage("before_catalog_open", json!({}))?;
    let catalog = open_catalog()?;
    trace.stage("after_catalog_open", json!({}))?;
    trace.stage(
        "before_optional_operation",
        json!({"family":profile.engine_family}),
    )?;
    let operation = catalog.AddOptionalPackageAsync(&HSTRING::from(&profile.engine_family))?;
    // Once created, only an observed terminal status permits releasing this operation.
    let mut failure = trace
        .stage(
            "after_optional_operation",
            json!({"budget_seconds":OPERATION_BUDGET.as_secs()}),
        )
        .err();
    let started = Instant::now();
    let mut wait = cancellation_policy::Wait::default();
    let mut status_failure_recorded = false;
    loop {
        use cancellation_policy::{Status, Step};
        let status = match operation.Status() {
            Ok(AsyncStatus::Started) => Status::Started,
            Ok(AsyncStatus::Completed) => Status::Completed,
            Ok(AsyncStatus::Canceled) => Status::Canceled,
            Ok(AsyncStatus::Error) => Status::Error,
            other => {
                if !status_failure_recorded {
                    let error = match other {
                        Err(error) => Failure::from(error),
                        _ => refused("unknown_async_status"),
                    };
                    let _ = trace.stage(
                        "optional_status_unobservable",
                        json!({"failure":error,"ownership_retained":true}),
                    );
                    if failure.is_none() {
                        failure = Some(error);
                    }
                    status_failure_recorded = true;
                }
                Status::Unobservable
            }
        };
        match wait.observe(status, started.elapsed(), cancel_probe || failure.is_some() || timed_out(started.elapsed(), root.join("cancel").exists())) {
            Step::Cancel => {
                let result = operation.Cancel();
                let hresult = result.as_ref().err().map_or(0, |error| error.code().0);
                if let Err(error) = trace.stage("optional_cancel_requested", json!({"after_observed_started":true,"hresult":hresult,"terminal_observed":false})) {
                    if failure.is_none() { failure = Some(error); }
                }
            }
            Step::Unresolved => {
                if let Err(error) = trace.stage("optional_settlement_unresolved", json!({"ownership_retained":true,"pid":std::process::id(),"thread_id":unsafe{GetCurrentThreadId()}})) {
                    if failure.is_none() { failure = Some(error); }
                }
            }
            Step::Terminal(terminal) => {
                // Terminal API status and deployment result are independent observations.
                let code = operation.ErrorCode();
                let result = operation.GetResults();
                let event = trace.stage("optional_terminal", json!({"status":format!("{terminal:?}"),"cancel_requested":wait.cancel_at.is_some(),"error_code":code.as_ref().ok().map(|code|code.0),"error_code_query_hresult":code.as_ref().err().map(|error|error.code().0),"get_results_hresult":result.as_ref().err().map_or(0,|error|error.code().0)}));
                if failure.is_none() { failure = event.err(); }
                if terminal == Status::Completed {
                    let result = result?;
                    let extended = result.ExtendedError()?;
                    trace.stage("optional_completed_result", json!({"extended_error":extended.0,"deployment_succeeded":extended.is_ok()}))?;
                    extended.ok()?;
                    let package = result.Package()?;
                    if let Some(error) = failure { return Err(error); }
                    return Ok(Some(package));
                }
                if let Some(error) = failure { return Err(error); }
                if terminal == Status::Canceled && cancel_probe && wait.cancel_at.is_some() {
                    return Ok(None);
                }
                return Err(match result { Err(error) => error.into(), Ok(_) => Failure {reason:"optional_terminal_not_completed",hresult:code.ok().map(|code|code.0)} });
            }
            Step::Poll => {}
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}
fn execute(
    profile: &Profile,
    inventory: &Inventory,
    action: Action,
    root: &Path,
    trace: &mut Trace,
) -> Result<()> {
    let caller = caller()?;
    trace.stage(
        "caller",
        serde_json::to_value(&caller).map_err(|_| refused("evidence_encoding_failed"))?,
    )?;
    admit_caller(&caller, action, &full_name(profile, false))?;
    trace.stage("before_mta", json!({}))?;
    let _apartment = super::super::Apartment::new()?;
    trace.stage(
        "mta_ready",
        json!({"pid":std::process::id(),"thread_id":unsafe{GetCurrentThreadId()}}),
    )?;
    let manager: PackageManager = super::super::activate_owned()?;
    if action == Action::Bootstrap {
        let before = packages(&manager, profile, false)?;
        require(before.len() == 1, "exact_previous_core_required")?;
        let record = package_record(&before[0])?;
        trace.stage("bootstrap_previous_core", record.clone())?;
        require(
            record["name"] == profile.core_name
                && record["family"] == profile.core_family
                && record["publisher"] == profile.publisher
                && record["version"] == profile.previous_core_version
                && record["architecture"] == "x64"
                && record["optional"] == false
                && record["bundle"] == false
                && record["full_name"]
                    == full_name(profile, false).replacen(
                        &format!("_{}_", profile.core_version),
                        &format!("_{}_", profile.previous_core_version),
                        1,
                    )
                && record["status_ok"] == true
                && record["signed_direct"] == true,
            "exact_previous_core_required",
        )?;
    } else {
        let (current, _) = super::super::observe()?;
        verify_package(&current, profile, false, trace, "core_before")?;
        verify_source(&current, profile, trace, "source_before")?;
        let image = dunce::canonicalize(
            std::env::current_exe().map_err(|_| refused("installed_image_unavailable"))?,
        )
        .map_err(|_| refused("installed_image_unavailable"))?;
        let expected =
            dunce::canonicalize(PathBuf::from(current.InstalledPath()?.to_string()).join("app"))
                .map_err(|_| refused("installed_image_unavailable"))?;
        require(
            image.parent() == Some(expected.as_path())
                && image
                    .file_name()
                    .is_some_and(|name| name == "optional-package-fixture.exe"),
            "installed_fixture_path_mismatch",
        )?;
    }
    require(
        packages(&manager, profile, true)?.is_empty(),
        "engine_must_be_absent",
    )?;
    trace.stage("engine_absent_before", json!({}))?;
    trace.stage(
        "before_descriptor_read",
        json!({"source":profile.source_uri}),
    )?;
    let bytes =
        crate::servicing_feed::read_metadata(&profile.source_uri, &|| root.join("cancel").exists())
            .map_err(refused)?;
    require(
        format!("{:x}", Sha256::digest(&bytes)) == inventory.descriptor_sha256,
        "descriptor_inventory_digest_mismatch",
    )?;
    parse_descriptor(&bytes, profile, inventory)?;
    trace.stage(
        "descriptor_verified",
        json!({"bytes":bytes.len(),"sha256":format!("{:x}",Sha256::digest(&bytes))}),
    )?;
    if action == Action::Bootstrap {
        let uri = super::super::owned_uri(&profile.source_uri)?;
        trace.stage(
            "before_bootstrap_operation",
            json!({"api":"AddPackageByAppInstallerFileAsync"}),
        )?;
        let operation = manager.AddPackageByAppInstallerFileAsync(
            &uri,
            AddPackageByAppInstallerOptions::None,
            None::<&PackageVolume>,
        )?;
        trace.stage(
            "after_bootstrap_operation",
            json!({"budget_seconds":OPERATION_BUDGET.as_secs()}),
        )?;
        let result = super::super::wait_deployment(
            &operation,
            &|| root.join("cancel").exists(),
            OPERATION_BUDGET,
        )?;
        require(
            result.IsRegistered()?,
            "bootstrap_registration_not_observed",
        )?;
    } else if action == Action::Acquire || action == Action::CancelAcquire {
        if let Some(package) = acquire(profile, root, trace, action == Action::CancelAcquire)? {
            verify_package(&package, profile, true, trace, "optional_result")?;
            let worker = PathBuf::from(package.InstalledPath()?.to_string())
                .join("worker")
                .join("TextAnalysisWorker.exe");
            let bytes = bounded_file(&worker, 32 * 1024 * 1024)?;
            require(
                format!("{:x}", Sha256::digest(&bytes)) == profile.worker_sha256,
                "installed_worker_digest_mismatch",
            )?;
            trace.stage(
                "worker_verified",
                json!({"path":worker,"sha256":profile.worker_sha256}),
            )?;
        }
    }
    let core = packages(&manager, profile, false)?;
    require(core.len() == 1, "exact_core_registration_required")?;
    verify_package(&core[0], profile, false, trace, "core_after")?;
    verify_source(&core[0], profile, trace, "source_after")?;
    let optional = packages(&manager, profile, true)?;
    if action == Action::Acquire {
        require(optional.len() == 1, "exact_optional_registration_required")?;
        verify_package(&optional[0], profile, true, trace, "optional_after")?;
    } else if action == Action::CancelAcquire {
        require(optional.len() <= 1, "exact_optional_registration_required")?;
        if let Some(package) = optional.first() {
            verify_package(package, profile, true, trace, "optional_after")?;
        }
    } else {
        require(optional.is_empty(), "engine_must_remain_absent")?;
    }
    trace.stage("complete", json!({"operation":format!("{action:?}"),"engine_present":!optional.is_empty(),"acquisition_proven":false}))?;
    Ok(())
}
pub(crate) fn run(root: &Path, action: Action) -> bool {
    let result = (|| -> Result<bool> {
        use std::os::windows::fs::MetadataExt;
        let directory =
            std::fs::symlink_metadata(root).map_err(|_| refused("owned_root_unavailable"))?;
        require(
            directory.is_dir() && directory.file_attributes() & 0x400 == 0,
            "regular_owned_root_required",
        )?;
        let image = std::env::current_exe().map_err(|_| refused("fixture_image_unavailable"))?;
        let profile_bytes = bounded_file(
            &image
                .parent()
                .ok_or_else(|| refused("fixture_image_unavailable"))?
                .join("optional-acquisition-profile.json"),
            16_384,
        )?;
        let profile: Profile =
            serde_json::from_slice(&profile_bytes).map_err(|_| refused("profile_invalid"))?;
        profile.validate()?;
        let owner: Owner =
            serde_json::from_slice(&bounded_file(&root.join("acquisition-owner.json"), 2048)?)
                .map_err(|_| refused("owner_marker_invalid"))?;
        require(
            owner.nonce == profile.nonce
                && owner.core_full_name == full_name(&profile, false)
                && owner.operation == format!("{action:?}"),
            "owner_marker_invalid",
        )?;
        let inventory_bytes = bounded_file(&root.join("acquisition-inventory.json"), 4096)?;
        let inventory: Inventory =
            serde_json::from_slice(&inventory_bytes).map_err(|_| refused("inventory_invalid"))?;
        inventory.validate(&profile)?;
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(root.join("native-acquisition.ndjson"))
            .map_err(|_| refused("fresh_evidence_file_required"))?;
        let mut trace = Trace {
            file,
            records: 0,
            bytes: 0,
            started: Instant::now(),
        };
        trace.stage("profile", json!({"nonce":profile.nonce,"descriptor_kind":profile.descriptor_kind,"sha256":format!("{:x}",Sha256::digest(&profile_bytes))}))?;
        trace.stage("external_inventory", json!({"sha256":format!("{:x}",Sha256::digest(&inventory_bytes)),
            "main_uri":inventory.main_bundle_uri,"main_sha256":inventory.main_bundle_sha256,
            "main_size":inventory.main_bundle_size,"descriptor_sha256":inventory.descriptor_sha256}))?;
        let result = execute(&profile, &inventory, action, root, &mut trace);
        if let Err(error) = result {
            trace.stage(
                "refused",
                serde_json::to_value(error).map_err(|_| refused("evidence_encoding_failed"))?,
            )?;
            return Ok(false);
        }
        Ok(true)
    })();
    match result {
        Ok(value) => value,
        Err(error) => {
            eprintln!("{}", serde_json::to_string(&error).unwrap());
            false
        }
    }
}
