//! Windows owns executable updates. Only the installed, approved source is used.
use crate::deployment::{
    DesktopRelease, EngineWindowsIdentity, InstalledArtifact, PackageIdentity,
};
use serde::Serialize;
use std::time::{Duration, Instant};

#[cfg(any(windows, test))]
#[path = "deployment_settlement.rs"]
mod deployment_settlement;

#[cfg(all(windows, feature = "qualification-tools", debug_assertions))]
#[path = "optional_acquisition.rs"]
pub(crate) mod optional_acquisition;

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

#[derive(Debug, Serialize)]
pub struct PreparedUpdate {
    pub package_full_name: String,
    pub version: String,
}

#[derive(Clone, Debug, PartialEq)]
enum EngineSelection {
    Absent,
    Present {
        full_name: String,
        version: String,
        path: String,
    },
}

fn selection_after_register(
    before: &EngineSelection,
    after: &EngineSelection,
    target: Option<&str>,
) -> bool {
    match (before, after) {
        (EngineSelection::Absent, EngineSelection::Absent) => true,
        (
            EngineSelection::Present { version: old, .. },
            EngineSelection::Present { version: now, .. },
        ) => now == old || target == Some(now.as_str()),
        _ => false,
    }
}

fn version_tuple(text: &str) -> Option<[u16; 4]> {
    let values: Vec<_> = text.split('.').collect();
    if values.len() != 4 {
        return None;
    }
    let mut result = [0; 4];
    for (value, slot) in values.iter().zip(&mut result) {
        *slot = value.parse().ok()?;
        if slot.to_string() != *value {
            return None;
        }
    }
    Some(result)
}

const STAGED_FILE_LIMIT: usize = 65536;
const FOUNDATION: &str = "http://schemas.microsoft.com/appx/manifest/foundation/windows10";
const BUNDLE: &str = "http://schemas.microsoft.com/appx/2016/bundle";
const UAP3: &str = "http://schemas.microsoft.com/appx/manifest/uap/windows10/3";
#[derive(Debug)]
struct ManifestNode {
    name: String,
    namespace: String,
    attributes: std::collections::BTreeMap<String, String>,
    children: Vec<ManifestNode>,
}
impl ManifestNode {
    fn one(&self, ns: &str, name: &str) -> Result<&Self, &'static str> {
        let mut found = self
            .children
            .iter()
            .filter(|n| n.namespace == ns && n.name == name);
        let result = found.next().ok_or("staged_identity_mismatch")?;
        if found.next().is_some() {
            return Err("staged_identity_mismatch");
        }
        Ok(result)
    }
    fn has(&self, key: &str, value: &str) -> bool {
        self.attributes.get(key).is_some_and(|x| x == value)
    }
}
fn manifest_xml(bytes: &[u8]) -> Result<ManifestNode, &'static str> {
    use quick_xml::{events::Event, Reader};
    use std::collections::BTreeMap;
    if bytes.is_empty() || bytes.len() > STAGED_FILE_LIMIT {
        return Err("staged_evidence_size_invalid");
    }
    let text = std::str::from_utf8(bytes).map_err(|_| "staged_manifest_invalid")?;
    let mut reader = Reader::from_str(text.trim_start_matches('\u{feff}'));
    let mut stack: Vec<(ManifestNode, BTreeMap<String, String>)> = Vec::new();
    let mut root = None;
    let mut count = 0;
    loop {
        let event = reader.read_event().map_err(|_| "staged_manifest_invalid")?;
        let empty = matches!(&event, Event::Empty(_));
        match event {
            Event::Start(e) | Event::Empty(e) => {
                count += 1;
                if count > 1024 || stack.len() > 64 || (stack.is_empty() && root.is_some()) {
                    return Err("staged_manifest_invalid");
                }
                let mut namespaces = stack.last().map(|x| x.1.clone()).unwrap_or_default();
                let mut attrs = BTreeMap::new();
                for a in e.attributes() {
                    let a = a.map_err(|_| "staged_manifest_invalid")?;
                    let key = a.key.as_ref().to_owned();
                    let value = a
                        .normalized_value(quick_xml::XmlVersion::Implicit1_0)
                        .map_err(|_| "staged_manifest_invalid")?
                        .into_owned();
                    if attrs.insert(key.clone(), value.clone()).is_some() {
                        return Err("staged_manifest_invalid");
                    }
                    if key == "xmlns" {
                        namespaces.insert(String::new(), value);
                    } else if let Some(prefix) = key.strip_prefix("xmlns:") {
                        namespaces.insert(prefix.into(), value);
                    }
                }
                let name = e.name().as_ref().to_owned();
                let (prefix, local) = name.split_once(':').unwrap_or(("", name.as_str()));
                let namespace = namespaces
                    .get(prefix)
                    .ok_or("staged_manifest_invalid")?
                    .clone();
                let node = ManifestNode {
                    name: local.into(),
                    namespace,
                    attributes: attrs,
                    children: Vec::new(),
                };
                if empty {
                    if let Some(parent) = stack.last_mut() {
                        parent.0.children.push(node)
                    } else {
                        root = Some(node)
                    }
                } else {
                    stack.push((node, namespaces));
                }
            }
            Event::End(_) => {
                let (node, _) = stack.pop().ok_or("staged_manifest_invalid")?;
                if let Some(parent) = stack.last_mut() {
                    parent.0.children.push(node)
                } else {
                    root = Some(node)
                }
            }
            Event::Eof => {
                if !stack.is_empty() {
                    return Err("staged_manifest_invalid");
                }
                return root.ok_or("staged_manifest_invalid");
            }
            Event::DocType(_) | Event::GeneralRef(_) => return Err("staged_manifest_invalid"),
            Event::Decl(d) => {
                if root.is_some()
                    || !stack.is_empty()
                    || d.version().map_err(|_| "staged_manifest_invalid")?.as_ref() != "1.0"
                    || d.encoding()
                        .transpose()
                        .map_err(|_| "staged_manifest_invalid")?
                        .is_some_and(|v| v.as_ref() != "utf-8" && v.as_ref() != "UTF-8")
                {
                    return Err("staged_manifest_invalid");
                }
            }
            Event::Text(t)
                if stack.is_empty() && !t.as_ref().bytes().all(|b| b.is_ascii_whitespace()) =>
            {
                return Err("staged_manifest_invalid")
            }
            _ => (),
        }
    }
}

fn application_manifest(
    bytes: &[u8],
    actual: &PackageIdentity,
    application: Option<&str>,
    main: Option<&str>,
) -> Result<(), &'static str> {
    let root = manifest_xml(bytes)?;
    if root.name != "Package" || root.namespace != FOUNDATION {
        return Err("staged_identity_mismatch");
    }
    let id = root.one(FOUNDATION, "Identity")?;
    if !id.has("Name", &actual.name)
        || !id.has("Publisher", &actual.publisher)
        || !id.has("Version", &actual.version)
        || !id.has("ProcessorArchitecture", "x64")
    {
        return Err("staged_identity_mismatch");
    }
    if let Some(application) = application {
        let apps = root.one(FOUNDATION, "Applications")?;
        if apps.children.len() != 1 || !apps.one(FOUNDATION, "Application")?.has("Id", application)
        {
            return Err("staged_identity_mismatch");
        }
    }
    if let Some(main) = main {
        let deps = root.one(FOUNDATION, "Dependencies")?;
        let relation = deps.one(UAP3, "MainPackageDependency")?;
        if relation.attributes.len() != 1 || !relation.has("Name", main) {
            return Err("staged_relation_mismatch");
        }
    }
    Ok(())
}

fn bundle_manifest(
    bytes: &[u8],
    main: &PackageIdentity,
    engine: &crate::servicing_feed::Coordinate,
    identity: &EngineWindowsIdentity,
) -> Result<(), &'static str> {
    let root = manifest_xml(bytes)?;
    if root.name != "Bundle"
        || root.namespace != BUNDLE
        || !root.has("SchemaVersion", "5.0")
        || root.children.len() != 3
    {
        return Err("staged_relation_mismatch");
    }
    let id = root.one(BUNDLE, "Identity")?;
    let optional = root.one(BUNDLE, "OptionalBundle")?;
    let main_file = format!("ConversationAnalytics_{}_x64.msix", main.version);
    let engine_file = format!("TextAnalysisEngine_{}_x64.msix", engine.version);
    let bundle_file = engine
        .package_uri
        .rsplit('/')
        .next()
        .ok_or("staged_relation_mismatch")?;
    if !id.has("Name", &main.name)
        || !id.has("Publisher", &main.publisher)
        || !id.has("Version", &main.version)
        || !optional.has("Name", &identity.name)
        || !optional.has("Publisher", &identity.publisher)
        || !optional.has("Version", &engine.version)
        || !optional.has("FileName", bundle_file)
    {
        return Err("staged_relation_mismatch");
    }
    let packages = root.one(BUNDLE, "Packages")?;
    if packages.children.len() != 1 || optional.children.len() != 1 {
        return Err("staged_relation_mismatch");
    }
    for (node, version, file) in [
        (
            packages.one(BUNDLE, "Package")?,
            main.version.as_str(),
            main_file.as_str(),
        ),
        (
            optional.one(BUNDLE, "Package")?,
            engine.version.as_str(),
            engine_file.as_str(),
        ),
    ] {
        if !node.has("Type", "application")
            || !node.has("Architecture", "x64")
            || !node.has("Version", version)
            || !node.has("FileName", file)
        {
            return Err("staged_relation_mismatch");
        }
    }
    Ok(())
}

fn target_descriptor(
    bytes: &[u8],
    current: &DesktopRelease,
    actual: &PackageIdentity,
    target: &crate::servicing_feed::Target,
) -> Result<DesktopRelease, &'static str> {
    if bytes.is_empty() || bytes.len() > STAGED_FILE_LIMIT {
        return Err("staged_evidence_size_invalid");
    }
    let target_release: DesktopRelease =
        serde_json::from_slice(bytes).map_err(|_| "desktop_descriptor_invalid")?;
    target_release.validate_target(actual)?;
    if !target_release.target_matches(current) {
        return Err("staged_identity_mismatch");
    }
    match (target, &target_release.servicing) {
        (
            crate::servicing_feed::Target::RelatedBundle {
                engine_identity,
                engine,
                ..
            },
            Some(s),
        ) => match &s.installed_artifact {
            InstalledArtifact::RelatedBundle {
                engine: declared, ..
            } if &declared.identity == engine_identity
                && declared.package_version == engine.version =>
            {
                ()
            }
            _ => return Err("staged_relation_mismatch"),
        },
        (crate::servicing_feed::Target::MainPackage { .. }, Some(s))
            if matches!(s.installed_artifact, InstalledArtifact::MainPackage { .. }) =>
        {
            ()
        }
        _ => return Err("staged_relation_mismatch"),
    }
    Ok(target_release)
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
enum StagedRole {
    MainManifest,
    MainDescriptor,
    MainBundle,
    EngineManifest,
    #[cfg(all(feature = "qualification-tools", debug_assertions))]
    CaseInput,
    #[cfg(all(feature = "qualification-tools", debug_assertions))]
    CurrentPackage,
}
#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
enum StagedOperation {
    OriginLookup,
    PathLookup,
    OpenAncestor,
    Attributes,
    Security,
    StablePath,
    OpenFile,
    BoundedRead,
    Validate,
    #[cfg(all(feature = "qualification-tools", debug_assertions))]
    CaseRead,
    #[cfg(all(feature = "qualification-tools", debug_assertions))]
    Identity,
    #[cfg(all(feature = "qualification-tools", debug_assertions))]
    Selection,
    #[cfg(all(feature = "qualification-tools", debug_assertions))]
    FeedParse,
}
#[derive(Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum StagedNativeError {
    Win32 {
        code: u32,
    },
    #[cfg(all(feature = "qualification-tools", debug_assertions))]
    Hresult {
        code: i32,
    },
}
#[derive(Debug, Serialize)]
struct StagedReadError {
    reason: &'static str,
    role: StagedRole,
    operation: StagedOperation,
    native_error: Option<StagedNativeError>,
    #[cfg(all(feature = "qualification-tools", debug_assertions))]
    files_read: Option<Vec<(StagedRole, usize)>>,
}
impl StagedReadError {
    fn new(reason: &'static str, role: StagedRole, operation: StagedOperation) -> Self {
        Self {
            reason,
            role,
            operation,
            native_error: None,
            #[cfg(all(feature = "qualification-tools", debug_assertions))]
            files_read: None,
        }
    }
    fn win32(
        reason: &'static str,
        role: StagedRole,
        operation: StagedOperation,
        code: u32,
    ) -> Self {
        Self {
            native_error: Some(StagedNativeError::Win32 { code }),
            ..Self::new(reason, role, operation)
        }
    }
    fn io(
        reason: &'static str,
        role: StagedRole,
        operation: StagedOperation,
        error: std::io::Error,
    ) -> Self {
        Self {
            native_error: error
                .raw_os_error()
                .map(|code| StagedNativeError::Win32 { code: code as u32 }),
            ..Self::new(reason, role, operation)
        }
    }
}

#[cfg(windows)]
struct StagedFiles {
    held: Vec<std::fs::File>,
    bytes: usize,
    #[cfg(all(feature = "qualification-tools", debug_assertions))]
    records: Vec<(StagedRole, usize)>,
}
#[cfg(windows)]
impl StagedFiles {
    fn read(
        &mut self,
        root: &std::path::Path,
        relative: &str,
        role: StagedRole,
    ) -> Result<Vec<u8>, StagedReadError> {
        use std::{
            fs::OpenOptions,
            io::Read,
            os::windows::{fs::OpenOptionsExt, io::AsRawHandle},
        };
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
            FILE_SHARE_READ,
        };
        let fail = |reason, operation| StagedReadError::new(reason, role, operation);
        if !root.is_absolute()
            || root.to_string_lossy().starts_with("\\\\")
            || root.components().any(|p| {
                matches!(
                    p,
                    std::path::Component::ParentDir | std::path::Component::CurDir
                )
            })
        {
            return Err(fail("staged_path_unprotected", StagedOperation::Attributes));
        }
        let path = root.join(relative);
        if !path.starts_with(root)
            || relative
                .split('/')
                .any(|s| s.is_empty() || matches!(s, "." | "..") || s.contains([':', '\\']))
        {
            return Err(fail("staged_path_unprotected", StagedOperation::Attributes));
        }
        for parent in path
            .parent()
            .ok_or_else(|| fail("staged_path_unprotected", StagedOperation::Attributes))?
            .ancestors()
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
        {
            let access = windows_sys::Win32::Storage::FileSystem::FILE_READ_ATTRIBUTES
                | if parent.starts_with(root) {
                    windows_sys::Win32::Storage::FileSystem::READ_CONTROL
                } else {
                    0
                };
            let file = OpenOptions::new()
                .access_mode(access)
                .share_mode(FILE_SHARE_READ)
                .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS)
                .open(parent)
                .map_err(|e| {
                    StagedReadError::io(
                        "staged_path_unprotected",
                        role,
                        StagedOperation::OpenAncestor,
                        e,
                    )
                })?;
            let metadata = file.metadata().map_err(|e| {
                StagedReadError::io(
                    "staged_path_unprotected",
                    role,
                    StagedOperation::Attributes,
                    e,
                )
            })?;
            use std::os::windows::fs::MetadataExt;
            if !metadata.is_dir() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            {
                return Err(fail("staged_path_unprotected", StagedOperation::Attributes));
            }
            if parent.starts_with(root) {
                protected_package_file(file.as_raw_handle(), role)?;
            }
            stable_file_path(&file, parent, role)?;
            self.held.push(file);
        }
        let mut file = OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&path)
            .map_err(|e| {
                StagedReadError::io(
                    "staged_package_unavailable",
                    role,
                    StagedOperation::OpenFile,
                    e,
                )
            })?;
        let metadata = file.metadata().map_err(|e| {
            StagedReadError::io(
                "staged_package_unavailable",
                role,
                StagedOperation::Attributes,
                e,
            )
        })?;
        use std::os::windows::fs::MetadataExt;
        if !metadata.is_file()
            || metadata.len() == 0
            || metadata.len() > STAGED_FILE_LIMIT as u64
            || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
        {
            return Err(fail(
                "staged_evidence_size_invalid",
                StagedOperation::BoundedRead,
            ));
        }
        protected_package_file(file.as_raw_handle(), role)?;
        stable_file_path(&file, &path, role)?;
        let mut bytes = Vec::new();
        (&mut file)
            .take(STAGED_FILE_LIMIT as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| {
                StagedReadError::io(
                    "staged_package_unavailable",
                    role,
                    StagedOperation::BoundedRead,
                    e,
                )
            })?;
        if bytes.is_empty()
            || bytes.len() > STAGED_FILE_LIMIT
            || bytes.len() as u64 != metadata.len()
        {
            return Err(fail(
                "staged_evidence_size_invalid",
                StagedOperation::BoundedRead,
            ));
        }
        self.bytes = self
            .bytes
            .checked_add(bytes.len())
            .ok_or_else(|| fail("staged_evidence_size_invalid", StagedOperation::BoundedRead))?;
        if self.bytes > 4 * STAGED_FILE_LIMIT {
            return Err(fail(
                "staged_evidence_size_invalid",
                StagedOperation::BoundedRead,
            ));
        }
        self.held.push(file);
        #[cfg(all(feature = "qualification-tools", debug_assertions))]
        self.records.push((role, bytes.len()));
        Ok(bytes)
    }
}

#[cfg(any(windows, test))]
fn staged_path_matches(observed: &str, expected: &std::path::Path) -> bool {
    // PathBuf can retain '/' in joined components; the handle API returns '\\'.
    observed.eq_ignore_ascii_case(&expected.to_string_lossy().replace('/', "\\"))
}

#[cfg(windows)]
fn stable_file_path(
    file: &std::fs::File,
    expected: &std::path::Path,
    role: StagedRole,
) -> Result<(), StagedReadError> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::GetFinalPathNameByHandleW;
    let mut value = vec![0u16; 32768];
    let size = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            value.as_mut_ptr(),
            value.len() as u32,
            0,
        )
    };
    let fail =
        || StagedReadError::new("staged_path_unprotected", role, StagedOperation::StablePath);
    if size == 0 {
        return Err(StagedReadError::win32(
            "staged_path_unprotected",
            role,
            StagedOperation::StablePath,
            unsafe { windows_sys::Win32::Foundation::GetLastError() },
        ));
    }
    if size as usize >= value.len() {
        return Err(fail());
    }
    let value = String::from_utf16(&value[..size as usize]).map_err(|_| fail())?;
    let value = value.strip_prefix("\\\\?\\").ok_or_else(fail)?;
    if !staged_path_matches(value, expected) {
        return Err(fail());
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn package_allow_ace(bytes: &[u8]) -> Option<(u32, &[u8], bool)> {
    // Basic and callback allow ACEs share the mask/SID prefix. Conditions can
    // restrict a grant, so their unevaluated mask is a conservative upper bound.
    if bytes.len() < 16
        || !matches!(bytes[0], 0 | 9)
        || usize::from(u16::from_le_bytes([bytes[2], bytes[3]])) != bytes.len()
        || bytes.len() % 4 != 0
        || bytes[8] != 1
        || bytes[9] > 15
    {
        return None;
    }
    let sid_end = 16 + usize::from(bytes[9]) * 4;
    let sid = bytes.get(8..sid_end)?;
    Some((
        u32::from_le_bytes(bytes[4..8].try_into().ok()?),
        sid,
        bytes[0] == 9,
    ))
}

#[cfg(any(windows, test))]
fn package_grant_protected(mask: u32, callback: bool, trusted: bool) -> bool {
    mask & 0x500d0156 == 0 || (!callback && trusted)
}

#[cfg(windows)]
fn protected_package_file(
    handle: std::os::windows::io::RawHandle,
    role: StagedRole,
) -> Result<(), StagedReadError> {
    use std::{
        mem::{size_of, zeroed},
        ptr::null_mut,
    };
    use windows_sys::Win32::{
        Foundation::LocalFree,
        Security::{
            Authorization::{ConvertSidToStringSidW, GetSecurityInfo, SE_FILE_OBJECT},
            *,
        },
    };
    let fail = || StagedReadError::new("staged_path_unprotected", role, StagedOperation::Security);
    let native = || {
        StagedReadError::win32(
            "staged_path_unprotected",
            role,
            StagedOperation::Security,
            unsafe { windows_sys::Win32::Foundation::GetLastError() },
        )
    };
    let (mut owner, mut acl, mut descriptor) = (null_mut(), null_mut(), null_mut());
    let code = unsafe {
        GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            &mut acl,
            null_mut(),
            &mut descriptor,
        )
    };
    if code != 0 {
        return Err(StagedReadError::win32(
            "staged_path_unprotected",
            role,
            StagedOperation::Security,
            code,
        ));
    }
    struct Allocation(*mut std::ffi::c_void);
    impl Drop for Allocation {
        fn drop(&mut self) {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
    let _descriptor = Allocation(descriptor);
    let sid_text = |sid: PSID| -> Result<String, StagedReadError> {
        let mut text = null_mut();
        if sid.is_null() || unsafe { IsValidSid(sid) } == 0 {
            return Err(fail());
        }
        if unsafe { ConvertSidToStringSidW(sid, &mut text) } == 0 {
            return Err(native());
        }
        let _text = Allocation(text.cast());
        let mut n = 0;
        unsafe {
            while n < 256 && *text.add(n) != 0 {
                n += 1;
            }
        }
        if n == 256 {
            return Err(fail());
        }
        String::from_utf16(unsafe { std::slice::from_raw_parts(text, n) }).map_err(|_| fail())
    };
    let trusted = |sid: &str| {
        matches!(
            sid,
            "S-1-5-18"
                | "S-1-5-32-544"
                | "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
        )
    };
    if !trusted(&sid_text(owner)?) || acl.is_null() || unsafe { IsValidAcl(acl) } == 0 {
        return Err(fail());
    }
    let mut information: ACL_SIZE_INFORMATION = unsafe { zeroed() };
    if unsafe {
        GetAclInformation(
            acl,
            &mut information as *mut _ as _,
            size_of::<ACL_SIZE_INFORMATION>() as u32,
            AclSizeInformation,
        )
    } == 0
    {
        return Err(native());
    }
    if information.AceCount > 256 {
        return Err(fail());
    }
    for index in 0..information.AceCount {
        let mut pointer = null_mut();
        if unsafe { GetAce(acl, index, &mut pointer) } == 0 {
            return Err(native());
        }
        let header = unsafe { &*pointer.cast::<ACE_HEADER>() };
        if header.AceType == 1 {
            continue;
        }
        let bytes = unsafe {
            std::slice::from_raw_parts(pointer.cast::<u8>(), usize::from(header.AceSize))
        };
        let (mask, sid, callback) = package_allow_ace(bytes).ok_or_else(fail)?;
        let sid = sid_text(sid.as_ptr() as _)?;
        // Content, deletion and ACL writes are reserved to Windows servicing.
        if !package_grant_protected(mask, callback, trusted(&sid) || sid == "S-1-3-4") {
            return Err(fail());
        }
    }
    Ok(())
}

#[cfg(windows)]
fn validate_staged_target(
    current: &DesktopRelease,
    actual: &PackageIdentity,
    target: &crate::servicing_feed::Target,
    selection: &EngineSelection,
) -> Result<(String, StagedFiles), StagedReadError> {
    let mut files = StagedFiles {
        held: Vec::new(),
        bytes: 0,
        #[cfg(all(feature = "qualification-tools", debug_assertions))]
        records: Vec::new(),
    };
    let result = (|| -> Result<String, StagedReadError> {
        use crate::servicing_feed::Target;
        let failure = |reason, role| StagedReadError::new(reason, role, StagedOperation::Validate);
        let family = actual
            .family
            .as_deref()
            .ok_or_else(|| failure("staged_identity_mismatch", StagedRole::MainManifest))?;
        let suffix = family
            .strip_prefix(&(actual.name.clone() + "_"))
            .ok_or_else(|| failure("staged_identity_mismatch", StagedRole::MainManifest))?;
        let core = target.core();
        let main = PackageIdentity {
            name: actual.name.clone(),
            publisher: actual.publisher.clone(),
            version: core.version.clone(),
            architecture: "x64".into(),
            family: actual.family.clone(),
        };
        let root = staged_root(
            &format!("{}_{}_x64__{}", actual.name, core.version, suffix),
            StagedRole::MainManifest,
        )?;
        let app = files.read(&root, "AppxManifest.xml", StagedRole::MainManifest)?;
        application_manifest(&app, &main, Some(&current.identity.application_id), None)
            .map_err(|r| failure(r, StagedRole::MainManifest))?;
        let descriptor = files.read(
            &root,
            "app/desktop-release.json",
            StagedRole::MainDescriptor,
        )?;
        target_descriptor(&descriptor, current, &main, target)
            .map_err(|r| failure(r, StagedRole::MainDescriptor))?;
        if let Target::RelatedBundle {
            engine_identity,
            engine,
            ..
        } = target
        {
            let bundle = staged_root(
                &format!("{}_{}_neutral_~_{}", actual.name, core.version, suffix),
                StagedRole::MainBundle,
            )?;
            bundle_manifest(
                &files.read(
                    &bundle,
                    "AppxMetadata/AppxBundleManifest.xml",
                    StagedRole::MainBundle,
                )?,
                &main,
                engine,
                engine_identity,
            )
            .map_err(|r| failure(r, StagedRole::MainBundle))?;
            if matches!(selection, EngineSelection::Present { .. }) {
                let engine_root = staged_root(
                    &format!(
                        "{}_{}_x64__{}",
                        engine_identity.name, engine.version, suffix
                    ),
                    StagedRole::EngineManifest,
                )?;
                application_manifest(
                    &files.read(&engine_root, "AppxManifest.xml", StagedRole::EngineManifest)?,
                    &PackageIdentity {
                        name: engine_identity.name.clone(),
                        publisher: engine_identity.publisher.clone(),
                        version: engine.version.clone(),
                        architecture: "x64".into(),
                        family: Some(engine_identity.family.clone()),
                    },
                    None,
                    Some(&actual.name),
                )
                .map_err(|r| failure(r, StagedRole::EngineManifest))?;
            }
            let maximum = if matches!(selection, EngineSelection::Absent) {
                3
            } else {
                4
            };
            if files.bytes > maximum * STAGED_FILE_LIMIT {
                return Err(failure(
                    "staged_evidence_size_invalid",
                    StagedRole::MainBundle,
                ));
            }
        }
        let uri = url::Url::from_file_path(root.join("AppxManifest.xml"))
            .map_err(|_| failure("staged_package_unavailable", StagedRole::MainManifest))?
            .into();
        Ok(uri)
    })();
    match result {
        Ok(uri) => Ok((uri, files)),
        Err(error) => {
            #[cfg(all(feature = "qualification-tools", debug_assertions))]
            let error = {
                let mut error = error;
                error.files_read = Some(files.records.clone());
                error
            };
            Err(error)
        }
    }
}

#[cfg(windows)]
fn selected_engine(
    manager: &windows_runtime::Management::Deployment::PackageManager,
    release: &DesktopRelease,
    main: &PackageIdentity,
) -> Result<EngineSelection, &'static str> {
    use windows_runtime::{core::HSTRING, Management::Deployment::PackageTypes};
    let Some(expected) = release.engine_identity() else {
        return Ok(EngineSelection::Absent);
    };
    expected.validate(main)?;
    let found = manager
        .FindPackagesByUserSecurityIdNamePublisherWithPackageTypes(
            &HSTRING::new(),
            &HSTRING::from(&expected.name),
            &HSTRING::from(&expected.publisher),
            PackageTypes::Optional,
        )
        .map_err(|_| "optional_selection_unavailable")?;
    let iterator = found
        .First()
        .map_err(|_| "optional_selection_unavailable")?;
    let mut result = None;
    while iterator
        .HasCurrent()
        .map_err(|_| "optional_selection_unavailable")?
    {
        if result.is_some() {
            return Err("optional_selection_ambiguous");
        }
        let p = iterator
            .Current()
            .map_err(|_| "optional_selection_unavailable")?;
        let id = p.Id().map_err(|_| "optional_selection_unavailable")?;
        let (_, observed) =
            observe_package(p.clone()).map_err(|_| "optional_selection_unavailable")?;
        if observed.identity.name != expected.name
            || observed.identity.publisher != expected.publisher
            || observed.identity.family.as_deref() != Some(expected.family.as_str())
            || observed.identity.architecture != "x64"
            || !crate::deployment::engine_version(&observed.identity.version)
            || observed.origin != Origin::SignedDirect
            || observed.signature != Signature::SignedDirect
            || !p
                .IsOptional()
                .map_err(|_| "optional_selection_unavailable")?
            || p.IsBundle().map_err(|_| "optional_selection_unavailable")?
            || !p
                .Status()
                .and_then(|s| s.VerifyIsOK())
                .map_err(|_| "optional_selection_unavailable")?
        {
            return Err("optional_identity_mismatch");
        }
        let deps = p
            .Dependencies()
            .map_err(|_| "optional_selection_unavailable")?;
        if deps.Size().map_err(|_| "optional_selection_unavailable")? > 64 {
            return Err("optional_relation_mismatch");
        }
        let mut relations = 0;
        for index in 0..deps.Size().map_err(|_| "optional_selection_unavailable")? {
            let dep = deps
                .GetAt(index)
                .and_then(|p| p.Id())
                .map_err(|_| "optional_selection_unavailable")?;
            if dep
                .FamilyName()
                .map_err(|_| "optional_selection_unavailable")?
                .to_string()
                == main.family.as_deref().unwrap_or("")
            {
                if dep
                    .Name()
                    .map_err(|_| "optional_selection_unavailable")?
                    .to_string()
                    != main.name
                    || dep
                        .Publisher()
                        .map_err(|_| "optional_selection_unavailable")?
                        .to_string()
                        != main.publisher
                {
                    return Err("optional_relation_mismatch");
                }
                relations += 1;
            }
        }
        if relations != 1 {
            return Err("optional_relation_mismatch");
        }
        result = Some(EngineSelection::Present {
            full_name: id
                .FullName()
                .map_err(|_| "optional_selection_unavailable")?
                .to_string(),
            version: observed.identity.version,
            path: p
                .InstalledPath()
                .map_err(|_| "optional_selection_unavailable")?
                .to_string(),
        });
        iterator
            .MoveNext()
            .map_err(|_| "optional_selection_unavailable")?;
    }
    Ok(result.unwrap_or(EngineSelection::Absent))
}

#[cfg(all(windows, feature = "qualification-tools", debug_assertions))]
#[path = "staged_readback_qualification.rs"]
pub(crate) mod staged_readback_qualification;

/// Windows downloads and validates the package. Only descriptor bytes cross
/// this process; a successful deferred registration is required before drain.
#[cfg(windows)]
pub fn prepare_direct(
    release: &DesktopRelease,
    cancelled: impl Fn() -> bool,
) -> Result<PreparedUpdate, &'static str> {
    use windows_runtime::{
        core::HSTRING,
        Management::Deployment::{PackageManager, RegisterPackageOptions, StagePackageOptions},
    };
    if cancelled() {
        return Err("deployment_cancelled");
    }
    let _apartment = Apartment::new().map_err(|_| "windows_source_unavailable")?;
    let (current, actual) = observe().map_err(|_| "windows_source_unavailable")?;
    if bind(release, &actual) != UpdateSource::DirectAppinstaller {
        return Err("direct_source_required");
    }
    automatic_policy(&current)?;
    // Availability is an informational Windows cache. The exact registered
    // descriptor supplies the forward target; Windows must still validate and
    // stage its signed same-family bytes before any runtime drain is allowed.
    let bytes = crate::servicing_feed::read_metadata(
        actual
            .app_installer_uri
            .as_deref()
            .ok_or("direct_source_required")?,
        &cancelled,
    )?;
    let target = crate::servicing_feed::parse(&bytes, release, &actual.identity)?;
    if cancelled() {
        return Err("deployment_cancelled");
    }
    let manager: PackageManager = activate_owned().map_err(|_| "windows_stage_failed")?;
    let selection = selected_engine(&manager, release, &actual.identity)?;
    if let (
        EngineSelection::Present { version, .. },
        crate::servicing_feed::Target::RelatedBundle { engine, .. },
    ) = (&selection, &target)
    {
        if version_tuple(&engine.version) < version_tuple(version) {
            return Err("optional_target_downgrade");
        }
    }
    let uri = owned_uri(&target.core().package_uri).map_err(|_| "windows_stage_failed")?;
    let options: StagePackageOptions = activate_owned().map_err(|_| "windows_stage_failed")?;
    if let (
        EngineSelection::Present { .. },
        crate::servicing_feed::Target::RelatedBundle { engine, .. },
    ) = (&selection, &target)
    {
        options
            .RelatedPackageUris()
            .and_then(|v| v.Append(&owned_uri(&engine.package_uri)?))
            .map_err(|_| "windows_stage_failed")?;
    }
    let related_count = u32::from(matches!(
        (&selection, &target),
        (
            EngineSelection::Present { .. },
            crate::servicing_feed::Target::RelatedBundle { .. }
        )
    ));
    if options
        .OptionalPackageFamilyNames()
        .and_then(|v| v.Size())
        .map_err(|_| "windows_stage_failed")?
        != 0
        || options
            .OptionalPackageUris()
            .and_then(|v| v.Size())
            .map_err(|_| "windows_stage_failed")?
            != 0
        || options
            .RelatedPackageUris()
            .and_then(|v| v.Size())
            .map_err(|_| "windows_stage_failed")?
            != related_count
    {
        return Err("windows_stage_options_invalid");
    }
    let staged = manager
        .StagePackageByUriAsync(&uri, &options)
        .map_err(|_| "windows_stage_failed")?;
    wait_deployment(&staged, &cancelled, Duration::from_secs(300))
        .map_err(|_| "windows_stage_failed")?;
    let full_name = HSTRING::from(format!(
        "{}_{}_x64__{}",
        actual.identity.name,
        target.core().version,
        current
            .Id()
            .and_then(|id| id.PublisherId())
            .map_err(|_| "staged_identity_mismatch")?
    ));
    let (manifest, _staged_evidence) = if release.servicing.is_some() {
        let (uri, files) = validate_staged_target(release, &actual.identity, &target, &selection)
            .map_err(|error| error.reason)?;
        (uri, Some(files))
    } else {
        (staged_manifest(&full_name.to_string())?, None)
    };
    // Recheck effective settings after the potentially long Windows download.
    let (again, actual_again) = observe().map_err(|_| "windows_source_unavailable")?;
    if bind(release, &actual_again) != UpdateSource::DirectAppinstaller {
        return Err("direct_source_required");
    }
    automatic_policy(&again)?;
    if actual_again.identity != actual.identity
        || selected_engine(&manager, release, &actual_again.identity)? != selection
    {
        return Err("update_selection_changed");
    }
    if cancelled() {
        return Err("deployment_cancelled");
    }
    // Register the verified main package's manifest, using its Windows-owned
    // installed location rather than an untrusted path from the descriptor.
    let manifest = owned_uri(&manifest).map_err(|_| "staged_package_unavailable")?;
    let options: RegisterPackageOptions =
        activate_owned().map_err(|_| "windows_registration_failed")?;
    options
        .SetDeferRegistrationWhenPackagesAreInUse(true)
        .map_err(|_| "windows_registration_failed")?;
    if matches!(selection, EngineSelection::Present { .. }) {
        let family = &release
            .engine_identity()
            .ok_or("optional_identity_mismatch")?
            .family;
        options
            .OptionalPackageFamilyNames()
            .and_then(|v| v.Append(&HSTRING::from(family)))
            .map_err(|_| "windows_registration_failed")?;
    }
    if !options
        .DeferRegistrationWhenPackagesAreInUse()
        .map_err(|_| "windows_registration_failed")?
        || options
            .OptionalPackageFamilyNames()
            .and_then(|v| v.Size())
            .map_err(|_| "windows_registration_failed")?
            != u32::from(matches!(selection, EngineSelection::Present { .. }))
    {
        return Err("windows_registration_options_invalid");
    }
    let registration = manager
        .RegisterPackageByUriAsync(&manifest, &options)
        .map_err(|_| "windows_registration_failed")?;
    let result = wait_deployment(&registration, &cancelled, Duration::from_secs(60))
        .map_err(|_| "windows_registration_failed")?;
    if result
        .IsRegistered()
        .map_err(|_| "windows_registration_failed")?
    {
        return Err("windows_registration_not_deferred");
    }
    let (after, actual_after) = observe().map_err(|_| "windows_source_unavailable")?;
    if actual_after.identity != actual.identity
        || bind(release, &actual_after) != UpdateSource::DirectAppinstaller
    {
        return Err("update_selection_changed");
    }
    automatic_policy(&after)?;
    let target_engine = match &target {
        crate::servicing_feed::Target::RelatedBundle { engine, .. } => {
            Some(engine.version.as_str())
        }
        _ => None,
    };
    if !selection_after_register(
        &selection,
        &selected_engine(&manager, release, &actual_after.identity)?,
        target_engine,
    ) {
        return Err("update_selection_changed");
    }
    Ok(PreparedUpdate {
        package_full_name: full_name.to_string(),
        version: target.core().version.clone(),
    })
}

#[cfg(windows)]
fn staged_manifest(full_name: &str) -> Result<String, &'static str> {
    url::Url::from_file_path(
        staged_root(full_name, StagedRole::MainManifest)
            .map_err(|error| error.reason)?
            .join("AppxManifest.xml"),
    )
    .map(|uri| uri.into())
    .map_err(|_| "staged_package_unavailable")
}

#[cfg(windows)]
fn staged_root(full_name: &str, role: StagedRole) -> Result<std::path::PathBuf, StagedReadError> {
    use std::{os::windows::ffi::OsStringExt, ptr::null_mut};
    use windows_sys::Win32::{
        Foundation::{ERROR_INSUFFICIENT_BUFFER, ERROR_SUCCESS},
        Storage::Packaging::Appx::{
            GetStagedPackageOrigin, GetStagedPackagePathByFullName,
            PackageOrigin_DeveloperSigned as SIGNED, PackageOrigin_LineOfBusiness as ENTERPRISE,
        },
    };
    let fail = |reason, operation| StagedReadError::new(reason, role, operation);
    let name: Vec<u16> = full_name.encode_utf16().chain(Some(0)).collect();
    let mut origin = 0;
    let mut length = 0;
    // These documented APIs work for staged packages without administrative
    // package enumeration. StagePackageByUriAsync above retains signature
    // validation; developer-unsigned and unknown staged origins are refused.
    unsafe {
        let code = GetStagedPackageOrigin(name.as_ptr(), &mut origin);
        if code != ERROR_SUCCESS {
            return Err(StagedReadError::win32(
                "staged_identity_mismatch",
                role,
                StagedOperation::OriginLookup,
                code,
            ));
        }
        if !matches!(origin, SIGNED | ENTERPRISE) {
            return Err(fail(
                "staged_identity_mismatch",
                StagedOperation::OriginLookup,
            ));
        }
        let code = GetStagedPackagePathByFullName(name.as_ptr(), &mut length, null_mut());
        if code != ERROR_INSUFFICIENT_BUFFER {
            return Err(StagedReadError::win32(
                "staged_package_unavailable",
                role,
                StagedOperation::PathLookup,
                code,
            ));
        }
        if !(2..=32768).contains(&length) {
            return Err(fail(
                "staged_package_unavailable",
                StagedOperation::PathLookup,
            ));
        }
        let mut path = vec![0u16; length as usize];
        let code = GetStagedPackagePathByFullName(name.as_ptr(), &mut length, path.as_mut_ptr());
        if code != ERROR_SUCCESS {
            return Err(StagedReadError::win32(
                "staged_package_unavailable",
                role,
                StagedOperation::PathLookup,
                code,
            ));
        }
        if length < 2 || length as usize > path.len() || path[length as usize - 1] != 0 {
            return Err(fail(
                "staged_package_unavailable",
                StagedOperation::PathLookup,
            ));
        }
        let path =
            std::path::PathBuf::from(std::ffi::OsString::from_wide(&path[..length as usize - 1]));
        if !path.is_absolute() {
            return Err(fail(
                "staged_package_unavailable",
                StagedOperation::PathLookup,
            ));
        }
        Ok(path)
    }
}

#[cfg(windows)]
fn automatic_policy(
    package: &windows_runtime::ApplicationModel::Package,
) -> Result<(), &'static str> {
    use windows_runtime::ApplicationModel::AppInstallerPolicySource;
    let info = registered_info(package)
        .map_err(|_| "update_policy_unavailable")?
        .ok_or("direct_source_required")?;
    let read = || -> windows_runtime::core::Result<bool> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| {
                windows_runtime::core::Error::from_hresult(windows_runtime::core::HRESULT(
                    0x80004005u32 as i32,
                ))
            })?;
        let ticks = now.as_nanos() / 100 + 116_444_736_000_000_000u128;
        Ok(info.PolicySource()? == AppInstallerPolicySource::Default
            && info.AutomaticBackgroundTask()?
            && info.OnLaunch()?
            && !info.ForceUpdateFromAnyVersion()?
            && !info.ShowPrompt()?
            && info.HoursBetweenUpdateChecks()? == 6
            && !info.UpdateBlocksActivation()?
            && paused_until(&info)?.is_none_or(|until| until as i128 <= ticks as i128)
            && info.UpdateUris()?.Size()? == 0
            && info.OptionalPackageUris()?.Size()? == 0
            && info.DependencyPackageUris()?.Size()? == 0)
    };
    if read().map_err(|_| "update_policy_unavailable")? {
        Ok(())
    } else {
        Err("update_policy_disabled_or_changed")
    }
}

#[cfg(windows)]
fn paused_until(
    info: &windows_runtime::ApplicationModel::AppInstallerInfo,
) -> windows_runtime::core::Result<Option<i64>> {
    use windows_runtime::{
        core::Interface,
        ApplicationModel::IAppInstallerInfo2,
        Foundation::{DateTime, IReference},
    };
    let interface: IAppInstallerInfo2 = info.cast()?;
    let mut pointer = std::ptr::null_mut();
    let code = unsafe { (interface.vtable().PausedUntil)(interface.as_raw(), &mut pointer) };
    let value = if pointer.is_null() {
        None
    } else {
        Some(unsafe { IReference::<DateTime>::from_raw(pointer) })
    };
    code.ok()?;
    value
        .map(|value| Ok(value.Value()?.UniversalTime))
        .transpose()
}

#[cfg(windows)]
fn wait_deployment(
    operation: &windows_future::IAsyncOperationWithProgress<
        windows_runtime::Management::Deployment::DeploymentResult,
        windows_runtime::Management::Deployment::DeploymentProgress,
    >,
    cancelled: &impl Fn() -> bool,
    budget: Duration,
) -> windows_runtime::core::Result<windows_runtime::Management::Deployment::DeploymentResult> {
    use deployment_settlement::{Operation, Status};
    struct Deployment<'a>(
        &'a windows_future::IAsyncOperationWithProgress<
            windows_runtime::Management::Deployment::DeploymentResult,
            windows_runtime::Management::Deployment::DeploymentProgress,
        >,
    );
    impl Operation for Deployment<'_> {
        type Output = windows_runtime::core::Result<
            windows_runtime::Management::Deployment::DeploymentResult,
        >;
        fn status(&mut self) -> Status {
            match self.0.Status() {
                Ok(windows_future::AsyncStatus::Started) => Status::Pending,
                Ok(windows_future::AsyncStatus::Completed)
                | Ok(windows_future::AsyncStatus::Canceled)
                | Ok(windows_future::AsyncStatus::Error) => Status::Terminal,
                _ => Status::Unobservable,
            }
        }
        fn cancel(&mut self) {
            // Cancel is a request; its return value does not establish settlement.
            let _ = self.0.Cancel();
        }
        fn results(&mut self) -> Self::Output {
            let result = self.0.GetResults()?;
            result.ExtendedErrorCode()?.ok()?;
            Ok(result)
        }
    }
    let started = Instant::now();
    deployment_settlement::wait(
        &mut Deployment(operation),
        || cancelled() || started.elapsed() >= budget,
        || std::thread::sleep(Duration::from_millis(100)),
    )
}

#[cfg(windows)]
fn owned_uri(value: &str) -> windows_runtime::core::Result<windows_runtime::Foundation::Uri> {
    use windows_runtime::{
        core::{Interface, HSTRING},
        Foundation::IUriRuntimeClassFactory,
    };
    let factory: IUriRuntimeClassFactory = unsafe {
        windows_runtime::Win32::System::WinRT::RoGetActivationFactory(&HSTRING::from(
            "Windows.Foundation.Uri",
        ))?
    };
    let text = HSTRING::from(value);
    let mut pointer = std::ptr::null_mut();
    let code = unsafe {
        (factory.vtable().CreateUri)(
            factory.as_raw(),
            std::mem::transmute_copy(&text),
            &mut pointer,
        )
    };
    owned_result(code, pointer)
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
    use windows_runtime::ApplicationModel::{IPackageStatics, Package};
    let factory: IPackageStatics = unsafe {
        windows_runtime::Win32::System::WinRT::RoGetActivationFactory(
            &windows_runtime::core::HSTRING::from("Windows.ApplicationModel.Package"),
        )?
    };
    use windows_runtime::core::Interface;
    let mut pointer = std::ptr::null_mut();
    let code = unsafe { (factory.vtable().Current)(factory.as_raw(), &mut pointer) };
    let package: Package = owned_result(code, pointer)?;
    observe_package(package)
}

#[cfg(windows)]
pub(crate) fn installed(full_name: &str) -> Result<(Observation, String), &'static str> {
    use windows_runtime::{core::HSTRING, Management::Deployment::PackageManager};
    let _apartment = Apartment::new().map_err(|_| "servicing_source_unavailable")?;
    let manager: PackageManager = activate_owned().map_err(|_| "servicing_source_unavailable")?;
    let package = manager
        .FindPackageByUserSecurityIdPackageFullName(&HSTRING::new(), &HSTRING::from(full_name))
        .map_err(|_| "servicing_source_unavailable")?;
    let location = package
        .InstalledPath()
        .map_err(|_| "servicing_source_unavailable")?
        .to_string();
    let (_, observed) = observe_package(package).map_err(|_| "servicing_source_unavailable")?;
    Ok((observed, location))
}

#[cfg(windows)]
fn observe_package(
    package: windows_runtime::ApplicationModel::Package,
) -> windows_runtime::core::Result<(windows_runtime::ApplicationModel::Package, Observation)> {
    use windows_runtime::{ApplicationModel::PackageSignatureKind, System::ProcessorArchitecture};
    use windows_sys::Win32::Storage::Packaging::Appx::{
        GetStagedPackageOrigin, PackageOrigin_DeveloperSigned as ORIGIN_DEVELOPER_SIGNED,
        PackageOrigin_LineOfBusiness as ORIGIN_LINE_OF_BUSINESS,
        PackageOrigin_Store as ORIGIN_STORE,
    };
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
                family: Some(id.FamilyName()?.to_string()),
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
    registered_info(package)?
        .map(|info| Ok(info.Uri()?.RawUri()?.to_string()))
        .transpose()
}

#[cfg(windows)]
fn registered_info(
    package: &windows_runtime::ApplicationModel::Package,
) -> windows_runtime::core::Result<Option<windows_runtime::ApplicationModel::AppInstallerInfo>> {
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
    Ok(info)
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
    #[test]
    #[cfg(windows)]
    fn staged_path_matches_joined_descriptor_separators_and_ascii_case() {
        let root = std::path::Path::new(r"C:\Program Files\WindowsApps\Example.Main");
        let expected = root.join("app/desktop-release.json");
        let observed = r"C:\Program Files\WindowsApps\Example.Main\app\desktop-release.json";
        assert!(!observed.eq_ignore_ascii_case(&expected.to_string_lossy()));
        assert!(staged_path_matches(observed, &expected));
        assert!(staged_path_matches(
            &observed.to_ascii_uppercase(),
            &expected
        ));
    }

    #[test]
    fn staged_path_comparison_preserves_drive_components_and_literal_dots() {
        let observed = r"C:\Program Files\WindowsApps\Example.Main\app\desktop-release.json";
        for expected in [
            r"D:\Program Files\WindowsApps\Example.Main\app/desktop-release.json",
            r"C:\Program Files\WindowsApps\Other.Main\app/desktop-release.json",
            r"C:\Program Files\WindowsApps\Example.Main\app/other.json",
            r"C:\Program Files\WindowsApps\Example.Main\app/./desktop-release.json",
            r"C:\Program Files\WindowsApps\Example.Main\app/sub/../desktop-release.json",
        ] {
            assert!(!staged_path_matches(
                observed,
                std::path::Path::new(expected)
            ));
        }
    }

    fn allow_ace(kind: u8, flags: u8, mask: u32) -> Vec<u8> {
        let mut bytes = vec![kind, flags, 0, 0];
        bytes.extend_from_slice(&mask.to_le_bytes());
        bytes.extend_from_slice(&[1, 2, 0, 0, 0, 0, 0, 5]);
        bytes.extend_from_slice(&32u32.to_le_bytes());
        bytes.extend_from_slice(&545u32.to_le_bytes());
        if kind == 9 {
            bytes.extend_from_slice(b"artx");
        }
        let size = (bytes.len() as u16).to_le_bytes();
        bytes[2..4].copy_from_slice(&size);
        bytes
    }

    #[test]
    fn package_callback_read_grant_uses_mask_upper_bound() {
        for flags in [3, 19, 16] {
            let bytes = allow_ace(9, flags, 0x1200a9);
            let (mask, sid, callback) = package_allow_ace(&bytes).unwrap();
            assert_eq!(sid.len(), 16);
            assert!(callback);
            assert!(package_grant_protected(mask, callback, false));
        }
    }

    #[test]
    fn package_callback_protected_writes_are_always_refused() {
        for mask in [
            2, 4, 0x10, 0x40, 0x100, 0x10000, 0x40000, 0x80000, 0x10000000, 0x40000000,
        ] {
            let bytes = allow_ace(9, 3, 0x1200a9 | mask);
            let (mask, _, callback) = package_allow_ace(&bytes).unwrap();
            for trusted in [false, true] {
                assert!(!package_grant_protected(mask, callback, trusted));
            }
        }
    }

    #[test]
    fn package_basic_grants_keep_existing_write_policy() {
        assert!(package_grant_protected(0x1200a9, false, false));
        assert!(!package_grant_protected(0x1f01ff, false, false));
        assert!(package_grant_protected(0x1f01ff, false, true));
        assert!(!package_grant_protected(0x1f01ff, true, true));
    }

    #[test]
    fn package_allow_ace_requires_bounded_supported_sid_layout() {
        let good = allow_ace(9, 3, 0x1200a9);
        for length in 0..good.len() {
            assert!(package_allow_ace(&good[..length]).is_none());
        }
        for (offset, value) in [(0, 11), (2, 0), (8, 2), (9, 16), (9, 15)] {
            let mut bad = good.clone();
            bad[offset] = value;
            assert!(package_allow_ace(&bad).is_none());
        }
        let mut basic = allow_ace(0, 3, 0x1200a9);
        assert!(!package_allow_ace(&basic).unwrap().2);
        basic.push(0);
        let size = (basic.len() as u16).to_le_bytes();
        basic[2..4].copy_from_slice(&size);
        assert!(package_allow_ace(&basic).is_none());
    }

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
                family: None,
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
    fn alias_movement_is_limited_to_own_registration_and_stable_choice() {
        let old = EngineSelection::Present {
            full_name: "Engine_old".into(),
            version: "42.0.0.0".into(),
            path: "C:/old".into(),
        };
        let new = EngineSelection::Present {
            full_name: "Engine_new".into(),
            version: "43.0.0.0".into(),
            path: "C:/new".into(),
        };
        assert!(selection_after_register(&old, &old, Some("43.0.0.0")));
        assert!(selection_after_register(&old, &new, Some("43.0.0.0")));
        assert!(!selection_after_register(&old, &new, None));
        assert!(!selection_after_register(
            &old,
            &EngineSelection::Absent,
            Some("43.0.0.0")
        ));
        assert!(!selection_after_register(
            &EngineSelection::Absent,
            &new,
            Some("43.0.0.0")
        ));
        assert!(selection_after_register(
            &EngineSelection::Absent,
            &EngineSelection::Absent,
            Some("43.0.0.0")
        ));
        assert_ne!(old, new);
    }

    #[test]
    fn staged_bundle_relation_is_required_even_without_an_engine_object() {
        let main = PackageIdentity {
            name: "Example.Main".into(),
            publisher: "CN=Example".into(),
            version: "1.0.11.0".into(),
            architecture: "x64".into(),
            family: Some("Example.Main_0123456789abc".into()),
        };
        let identity = EngineWindowsIdentity {
            name: "Example.Engine".into(),
            publisher: main.publisher.clone(),
            family: "Example.Engine_0123456789abc".into(),
            architecture: "x64".into(),
        };
        let engine = crate::servicing_feed::Coordinate {
            version: "42.0.2.3".into(),
            package_uri: "https://example.test/TextAnalysisEngine_42.0.2.3_x64.msixbundle".into(),
        };
        let bundle = format!(
            r#"<Bundle xmlns="{BUNDLE}" SchemaVersion="5.0"><Identity Name="Example.Main" Publisher="CN=Example" Version="1.0.11.0"/><Packages><Package Type="application" Architecture="x64" Version="1.0.11.0" FileName="ConversationAnalytics_1.0.11.0_x64.msix"/></Packages><OptionalBundle Name="Example.Engine" Publisher="CN=Example" Version="42.0.2.3" FileName="TextAnalysisEngine_42.0.2.3_x64.msixbundle"><Package Type="application" Architecture="x64" Version="42.0.2.3" FileName="TextAnalysisEngine_42.0.2.3_x64.msix"/></OptionalBundle></Bundle>"#
        );
        assert_eq!(
            bundle_manifest(bundle.as_bytes(), &main, &engine, &identity),
            Ok(())
        );
        for bad in [
            bundle.replace("42.0.2.3", "42.0.2.4"),
            bundle.replace("2016/bundle", "2013/bundle"),
            bundle.replace("Example.Engine", "Other.Engine"),
            bundle.replace("<Packages>", "<Packages><Package/>"),
            bundle.replace("x64.msix\"", "arm64.msix\""),
        ] {
            assert!(bundle_manifest(bad.as_bytes(), &main, &engine, &identity).is_err());
        }
    }

    #[test]
    fn signed_fixed_manifest_identity_relation_and_bounds_are_required() {
        let main = PackageIdentity {
            name: "Example.Main".into(),
            publisher: "CN=Example".into(),
            version: "1.0.11.0".into(),
            architecture: "x64".into(),
            family: Some("Example.Main_0123456789abc".into()),
        };
        let manifest = format!(
            r#"<Package xmlns="{FOUNDATION}" xmlns:uap3="{UAP3}"><Identity Name="Example.Main" Publisher="CN=Example" Version="1.0.11.0" ProcessorArchitecture="x64"/><Applications><Application Id="Bridge"/></Applications><Dependencies><uap3:MainPackageDependency Name="Example.Parent"/></Dependencies></Package>"#
        );
        assert_eq!(
            application_manifest(
                manifest.as_bytes(),
                &main,
                Some("Bridge"),
                Some("Example.Parent")
            ),
            Ok(())
        );
        assert!(application_manifest(
            manifest
                .replace("Example.Parent", "Wrong.Parent")
                .as_bytes(),
            &main,
            Some("Bridge"),
            Some("Example.Parent")
        )
        .is_err());
        assert!(application_manifest(
            manifest
                .replace(
                    "<Application Id=\"Bridge\"/>",
                    "<Application Id=\"Bridge\"/><Application Id=\"Other\"/>"
                )
                .as_bytes(),
            &main,
            Some("Bridge"),
            None
        )
        .is_err());
        assert!(manifest_xml(&vec![b' '; STAGED_FILE_LIMIT + 1]).is_err());
        assert!(manifest_xml(b"<!DOCTYPE Package><Package/>").is_err());
        assert!(manifest_xml(manifest.replace("xmlns:uap3=", "unbound=").as_bytes()).is_err());
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
    #[cfg(windows)]
    #[test]
    fn preparation_requires_an_installed_source_before_network_or_deployment() {
        assert!(matches!(
            prepare_direct(&release(false), || true),
            Err("deployment_cancelled")
        ));
        for _ in 0..3 {
            let result = std::thread::spawn(|| prepare_direct(&release(false), || false))
                .join()
                .unwrap();
            assert!(matches!(result, Err("windows_source_unavailable")));
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
