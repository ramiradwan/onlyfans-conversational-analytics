//! Bounded packaged-fixture readback of the current staged target validator.
use super::*;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    fs::OpenOptions,
    io::{Read, Write},
    path::Path,
};
use windows_runtime::{
    core::HSTRING,
    Management::Deployment::{PackageManager, PackageTypes},
};
const CASE_FILE: &str = "staged-target-case.json";
const RESULT_FILE: &str = "staged-target-readback.json";
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum ExpectedEngine {
    Absent,
    Present,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExpectedMain {
    name: String,
    publisher: String,
    family: String,
    version: String,
    architecture: String,
    full_name: String,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Case {
    schema: String,
    nonce: String,
    current_main: ExpectedMain,
    current_descriptor_sha256: String,
    expected_engine: ExpectedEngine,
    target_feed: String,
}
fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn fail(reason: &'static str, operation: StagedOperation) -> StagedReadError {
    StagedReadError::new(reason, StagedRole::CurrentPackage, operation)
}
fn native(operation: StagedOperation, error: windows_runtime::core::Error) -> StagedReadError {
    StagedReadError {
        native_error: Some(StagedNativeError::Hresult {
            code: error.code().0,
        }),
        ..fail("qualification_windows_failure", operation)
    }
}
fn parse_case(
    bytes: &[u8],
    absent: Option<&str>,
    present: Option<&str>,
) -> Result<Case, StagedReadError> {
    let valid = absent
        .zip(present)
        .filter(|(a, p)| hex(a, 64) && hex(p, 64) && a != p)
        .ok_or_else(|| {
            fail(
                "qualification_case_pins_required",
                StagedOperation::CaseRead,
            )
        })?;
    if bytes.is_empty() || bytes.len() > 65536 {
        return Err(fail("qualification_case_bound", StagedOperation::CaseRead));
    }
    let case: Case = serde_json::from_slice(bytes)
        .map_err(|_| fail("qualification_case_invalid", StagedOperation::CaseRead))?;
    let expected = match case.expected_engine {
        ExpectedEngine::Absent => valid.0,
        ExpectedEngine::Present => valid.1,
    };
    if case.schema != "desktop-staged-readback-case/v1"
        || !hex(&case.nonce, 32)
        || !hex(&case.current_descriptor_sha256, 64)
        || case.target_feed.is_empty()
        || case.target_feed.len() > crate::servicing_feed::MAX_DESCRIPTOR
        || digest(bytes) != expected
    {
        return Err(fail(
            "qualification_case_binding",
            StagedOperation::CaseRead,
        ));
    }
    Ok(case)
}
fn selection_matches(expected: &ExpectedEngine, selection: &EngineSelection) -> bool {
    matches!(
        (expected, selection),
        (ExpectedEngine::Absent, EngineSelection::Absent)
            | (ExpectedEngine::Present, EngineSelection::Present { .. })
    )
}
fn fixed_case(root: &Path) -> Result<Vec<u8>, StagedReadError> {
    use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
    let path = root.join(CASE_FILE);
    for parent in path.ancestors() {
        let m = std::fs::symlink_metadata(parent).map_err(|e| {
            StagedReadError::io(
                "qualification_case_unavailable",
                StagedRole::CaseInput,
                StagedOperation::CaseRead,
                e,
            )
        })?;
        if m.file_attributes() & 0x400 != 0 {
            return Err(fail(
                "qualification_case_reparse",
                StagedOperation::CaseRead,
            ));
        }
    }
    let mut file = OpenOptions::new()
        .read(true)
        .share_mode(windows_sys::Win32::Storage::FileSystem::FILE_SHARE_READ)
        .custom_flags(windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT)
        .open(&path)
        .map_err(|e| {
            StagedReadError::io(
                "qualification_case_unavailable",
                StagedRole::CaseInput,
                StagedOperation::CaseRead,
                e,
            )
        })?;
    let m = file.metadata().map_err(|e| {
        StagedReadError::io(
            "qualification_case_unavailable",
            StagedRole::CaseInput,
            StagedOperation::CaseRead,
            e,
        )
    })?;
    if !m.is_file() || m.len() == 0 || m.len() > 65536 {
        return Err(fail("qualification_case_bound", StagedOperation::CaseRead));
    }
    let mut bytes = Vec::new();
    (&mut file)
        .take(65537)
        .read_to_end(&mut bytes)
        .map_err(|e| {
            StagedReadError::io(
                "qualification_case_unavailable",
                StagedRole::CaseInput,
                StagedOperation::CaseRead,
                e,
            )
        })?;
    if bytes.len() as u64 != m.len() {
        return Err(fail("qualification_case_bound", StagedOperation::CaseRead));
    }
    Ok(bytes)
}
// This is a current-user registration observation, not staged artifact lookup.
fn require_target_unregistered(
    manager: &PackageManager,
    actual: &PackageIdentity,
    target: &crate::servicing_feed::Target,
) -> Result<(), StagedReadError> {
    let suffix = crate::deployment::actual_publisher_id(actual)
        .map_err(|r| fail(r, StagedOperation::Identity))?;
    let x64 = format!("{}_{}_x64__{}", actual.name, target.core().version, suffix);
    let bundle = format!(
        "{}_{}_neutral_~_{}",
        actual.name,
        target.core().version,
        suffix
    );
    let values = manager
        .FindPackagesByUserSecurityIdNamePublisherWithPackageTypes(
            &HSTRING::new(),
            &HSTRING::from(&actual.name),
            &HSTRING::from(&actual.publisher),
            PackageTypes::Main | PackageTypes::Bundle,
        )
        .map_err(|e| native(StagedOperation::Identity, e))?;
    let iterator = values
        .First()
        .map_err(|e| native(StagedOperation::Identity, e))?;
    let mut count = 0;
    while iterator
        .HasCurrent()
        .map_err(|e| native(StagedOperation::Identity, e))?
    {
        count += 1;
        if count > 16 {
            return Err(fail(
                "qualification_registration_bound",
                StagedOperation::Identity,
            ));
        }
        let name = iterator
            .Current()
            .and_then(|p| p.Id())
            .and_then(|id| id.FullName())
            .map_err(|e| native(StagedOperation::Identity, e))?
            .to_string();
        if name == x64 || name == bundle {
            return Err(fail(
                "qualification_target_already_registered",
                StagedOperation::Identity,
            ));
        }
        iterator
            .MoveNext()
            .map_err(|e| native(StagedOperation::Identity, e))?;
    }
    Ok(())
}
#[derive(Serialize)]
struct Receipt {
    schema: &'static str,
    case_sha256: Option<String>,
    nonce: Option<String>,
    status: &'static str,
    expected_engine: Option<ExpectedEngine>,
    observed_engine: Option<&'static str>,
    validator_calls: u8,
    target_registration_observed_absent: Option<bool>,
    files_read: Option<Vec<(StagedRole, usize)>>,
    role_count: Option<usize>,
    current_descriptor_bytes: Option<usize>,
    target_bytes: Option<usize>,
    held_handle_count: Option<usize>,
    failure: Option<StagedReadError>,
}
fn require_current_release(
    release: &DesktopRelease,
    actual: &PackageIdentity,
) -> Result<(), StagedReadError> {
    if release.purpose != "qualification"
        || release.servicing.is_none()
        || release.update_owner != "direct-appinstaller"
    {
        return Err(fail("qualification_v2_required", StagedOperation::Validate));
    }
    release
        .validate(actual)
        .map_err(|r| fail(r, StagedOperation::Validate))
}
pub(crate) fn run(root: &Path) -> bool {
    let mut receipt = Receipt {
        schema: "desktop-staged-readback-result/v1",
        case_sha256: None,
        nonce: None,
        status: "FAIL",
        expected_engine: None,
        observed_engine: None,
        validator_calls: 0,
        target_registration_observed_absent: None,
        files_read: None,
        role_count: None,
        current_descriptor_bytes: None,
        target_bytes: None,
        held_handle_count: None,
        failure: None,
    };
    let mut retained = None;
    let outcome = (|| -> Result<(), StagedReadError> {
        let bytes = fixed_case(root)?;
        let case = parse_case(
            &bytes,
            option_env!("STAGED_READBACK_ABSENT_CASE_SHA256"),
            option_env!("STAGED_READBACK_PRESENT_CASE_SHA256"),
        )?;
        receipt.case_sha256 = Some(digest(&bytes));
        receipt.nonce = Some(case.nonce.clone());
        receipt.expected_engine = Some(case.expected_engine);
        let _apartment = Apartment::new().map_err(|e| native(StagedOperation::Identity, e))?;
        let (package, actual) = observe().map_err(|e| native(StagedOperation::Identity, e))?;
        let id = package
            .Id()
            .map_err(|e| native(StagedOperation::Identity, e))?;
        let current = &case.current_main;
        if actual.origin != Origin::SignedDirect
            || actual.signature != Signature::SignedDirect
            || actual.identity.name != current.name
            || actual.identity.publisher != current.publisher
            || actual.identity.family.as_deref() != Some(current.family.as_str())
            || actual.identity.version != current.version
            || actual.identity.architecture != current.architecture
            || id
                .FullName()
                .map_err(|e| native(StagedOperation::Identity, e))?
                .to_string()
                != current.full_name
            || package
                .IsOptional()
                .map_err(|e| native(StagedOperation::Identity, e))?
            || package
                .IsBundle()
                .map_err(|e| native(StagedOperation::Identity, e))?
        {
            return Err(fail(
                "qualification_identity_mismatch",
                StagedOperation::Identity,
            ));
        }
        let current_root = std::path::PathBuf::from(
            package
                .InstalledPath()
                .map_err(|e| native(StagedOperation::Identity, e))?
                .to_string(),
        );
        let mut current_files = StagedFiles::metadata();
        let descriptor = current_files.read(
            &current_root,
            "app/desktop-release.json",
            StagedRole::CurrentPackage,
        )?;
        receipt.current_descriptor_bytes = Some(descriptor.len());
        if digest(&descriptor) != case.current_descriptor_sha256 {
            return Err(fail(
                "qualification_descriptor_pin",
                StagedOperation::Validate,
            ));
        }
        let release: DesktopRelease = serde_json::from_slice(&descriptor)
            .map_err(|_| fail("desktop_descriptor_invalid", StagedOperation::Validate))?;
        require_current_release(&release, &actual.identity)?;
        let target =
            crate::servicing_feed::parse(case.target_feed.as_bytes(), &release, &actual.identity)
                .map_err(|r| fail(r, StagedOperation::FeedParse))?;
        if !matches!(target, crate::servicing_feed::Target::RelatedBundle { .. }) {
            return Err(fail(
                "qualification_related_target_required",
                StagedOperation::FeedParse,
            ));
        }
        let manager: PackageManager =
            activate_owned().map_err(|e| native(StagedOperation::Selection, e))?;
        let selected = selected_engine(&manager, &release, &actual.identity)
            .map_err(|r| fail(r, StagedOperation::Selection))?;
        receipt.observed_engine = Some(if matches!(selected, EngineSelection::Absent) {
            "absent"
        } else {
            "present"
        });
        if !selection_matches(&case.expected_engine, &selected) {
            return Err(fail(
                "qualification_engine_state_mismatch",
                StagedOperation::Selection,
            ));
        }
        require_target_unregistered(&manager, &actual.identity, &target)?;
        receipt.target_registration_observed_absent = Some(true);
        receipt.validator_calls += 1;
        let (_, files) = validate_staged_target(&release, &actual.identity, &target, &selected)?;
        receipt.files_read = Some(files.records.clone());
        receipt.target_bytes = Some(files.bytes);
        let (_, after) = observe().map_err(|e| native(StagedOperation::Identity, e))?;
        if after.identity != actual.identity
            || selected_engine(&manager, &release, &after.identity)
                .map_err(|r| fail(r, StagedOperation::Selection))?
                != selected
        {
            return Err(fail(
                "qualification_selection_changed",
                StagedOperation::Selection,
            ));
        }
        require_target_unregistered(&manager, &actual.identity, &target)?;
        receipt.held_handle_count = Some(files.held.len());
        retained = Some((current_files, files));
        Ok(())
    })();
    match outcome {
        Ok(()) => receipt.status = "PASS",
        Err(error) => {
            if error.files_read.is_some() {
                receipt.files_read = error.files_read.clone();
            }
            receipt.failure = Some(error);
        }
    }
    receipt.role_count = receipt.files_read.as_ref().map(Vec::len);
    let saved = (|| -> std::io::Result<()> {
        let body = serde_json::to_vec(&receipt)?;
        if body.len() > 16384 {
            return Err(std::io::ErrorKind::InvalidData.into());
        }
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(root.join(RESULT_FILE))?;
        file.write_all(&body)?;
        file.sync_all()
    })();
    drop(retained);
    saved.is_ok() && receipt.status == "PASS"
}
#[cfg(test)]
mod tests {
    use super::*;
    fn body(state: &str) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({"schema":"desktop-staged-readback-case/v1","nonce":"a".repeat(32),"current_main":{"name":"OFCA.Qualification.Example","publisher":"CN=OFCA MSIX Qualification Example","family":"OFCA.Qualification.Example_0123456789abc","version":"1.0.2.0","architecture":"x64","full_name":"OFCA.Qualification.Example_1.0.2.0_x64__0123456789abc"},"current_descriptor_sha256":"b".repeat(64),"expected_engine":state,"target_feed":"invented feed; shipping parser remains the next gate"})).unwrap()
    }
    #[test]
    fn cases_require_both_distinct_pins_and_exact_state_binding() {
        let a = body("absent");
        let p = body("present");
        let ah = digest(&a);
        let ph = digest(&p);
        assert!(parse_case(&a, Some(&ah), Some(&ph)).is_ok());
        assert!(parse_case(&p, Some(&ah), Some(&ph)).is_ok());
        for (x, y) in [
            (None, Some(ph.as_str())),
            (Some(ah.as_str()), None),
            (Some(ah.as_str()), Some(ah.as_str())),
        ] {
            assert!(parse_case(&a, x, y).is_err());
        }
        assert!(parse_case(&a, Some(&ph), Some(&ah)).is_err());
    }
    #[test]
    fn cases_are_closed_and_bounded() {
        let a = body("absent");
        let p = body("present");
        let ph = digest(&p);
        let mut v: serde_json::Value = serde_json::from_slice(&a).unwrap();
        v["uri_override"] = serde_json::json!("file:///unapproved");
        let b = serde_json::to_vec(&v).unwrap();
        assert!(parse_case(&b, Some(&digest(&b)), Some(&ph)).is_err());
        let b = vec![b' '; 65537];
        assert!(parse_case(&b, Some(&digest(&b)), Some(&ph)).is_err());
    }
    #[test]
    fn observed_selection_cannot_be_fabricated_by_expected_state() {
        assert!(selection_matches(
            &ExpectedEngine::Absent,
            &EngineSelection::Absent
        ));
        assert!(!selection_matches(
            &ExpectedEngine::Present,
            &EngineSelection::Absent
        ));
        let actual = EngineSelection::Present {
            full_name: "fixed".into(),
            version: "1.0.41.0".into(),
            path: "fixed".into(),
        };
        assert!(selection_matches(&ExpectedEngine::Present, &actual));
        assert!(!selection_matches(&ExpectedEngine::Absent, &actual));
    }
    #[test]
    fn current_release_gate_never_accepts_production_or_v1() {
        let mut release: DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct-v2-bridge.json"
        ))
        .unwrap();
        let actual = PackageIdentity {
            name: release.identity.name.clone(),
            publisher: release.identity.publisher.clone(),
            version: "1.0.10.0".into(),
            architecture: "x64".into(),
            family: Some(format!("{}_0123456789abc", release.identity.name)),
        };
        assert!(require_current_release(&release, &actual).is_ok());
        release.purpose = "production".into();
        assert_eq!(
            require_current_release(&release, &actual)
                .unwrap_err()
                .reason,
            "qualification_v2_required"
        );
        let legacy: DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct.json"
        ))
        .unwrap();
        assert_eq!(
            require_current_release(&legacy, &actual)
                .unwrap_err()
                .reason,
            "qualification_v2_required"
        );
    }
    #[test]
    fn diagnostic_keeps_native_code_and_shipping_reason_without_dynamic_text() {
        let e = StagedReadError::io(
            "staged_package_unavailable",
            StagedRole::MainDescriptor,
            StagedOperation::OpenFile,
            std::io::Error::from_raw_os_error(5),
        );
        let v = serde_json::to_value(&e).unwrap();
        assert_eq!(e.reason, "staged_package_unavailable");
        assert_eq!(
            v["native_error"],
            serde_json::json!({"kind":"win32","code":5})
        );
        assert_eq!(v["role"], "main_descriptor");
        assert_eq!(v["operation"], "open_file");
    }
}
