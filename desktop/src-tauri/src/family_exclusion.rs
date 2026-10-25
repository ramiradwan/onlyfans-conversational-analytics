//! One transferable file object excludes package-family mutations.
use sha2::{Digest, Sha256};
use std::{
    ffi::OsStr,
    mem::{size_of, zeroed},
    os::windows::ffi::OsStrExt,
    path::{Path, PathBuf},
    ptr::{null, null_mut},
};
use windows_sys::Win32::{
    Foundation::*,
    Security::{Authorization::*, *},
    Storage::FileSystem::*,
    System::{Threading::*, IO::OVERLAPPED},
};
type Result<T> = std::result::Result<T, &'static str>;
// winnt.h SE_GROUP_LOGON_ID (also exposed by the optional SystemServices module).
const SE_GROUP_LOGON_ID: u32 = 0xc0000000;
const MAGIC: &[u8] = b"native-servicing-lease.v2\n";
fn read_at(handle: HANDLE, offset: u32, bytes: &mut [u8]) -> Result<()> {
    let mut ov: OVERLAPPED = unsafe { zeroed() };
    ov.Anonymous.Anonymous.Offset = offset;
    let mut n = 0;
    if unsafe {
        ReadFile(
            handle,
            bytes.as_mut_ptr(),
            bytes.len() as u32,
            &mut n,
            &mut ov,
        )
    } == 0
        || n as usize != bytes.len()
    {
        return Err("servicing_exclusion_invalid");
    }
    Ok(())
}
fn write_at(handle: HANDLE, offset: u32, bytes: &[u8]) -> Result<()> {
    let mut ov: OVERLAPPED = unsafe { zeroed() };
    ov.Anonymous.Anonymous.Offset = offset;
    let mut n = 0;
    if unsafe { WriteFile(handle, bytes.as_ptr(), bytes.len() as u32, &mut n, &mut ov) } == 0
        || n as usize != bytes.len()
        || unsafe { FlushFileBuffers(handle) } == 0
    {
        return Err("servicing_exclusion_invalid");
    }
    Ok(())
}
pub(crate) fn wide(s: &OsStr) -> Vec<u16> {
    s.encode_wide().chain(Some(0)).collect()
}
#[derive(Debug)]
pub(crate) struct Handle(pub(crate) HANDLE);
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
impl Handle {
    pub(crate) fn new(h: HANDLE) -> Result<Self> {
        if h.is_null() || h == INVALID_HANDLE_VALUE {
            Err("servicing_exclusion_unavailable")
        } else {
            Ok(Self(h))
        }
    }
}
struct Local(*mut std::ffi::c_void);
impl Drop for Local {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.0);
        }
    }
}
fn token_info(process: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<Vec<usize>> {
    unsafe {
        let mut token = null_mut();
        if OpenProcessToken(process, TOKEN_QUERY, &mut token) == 0 {
            return Err("servicing_peer_invalid");
        }
        let token = Handle::new(token)?;
        let mut n = 0;
        GetTokenInformation(token.0, class, null_mut(), 0, &mut n);
        if n == 0 || n > 65536 {
            return Err("servicing_peer_invalid");
        }
        let mut buf = vec![0usize; (n as usize).div_ceil(size_of::<usize>())];
        if GetTokenInformation(token.0, class, buf.as_mut_ptr().cast(), n, &mut n) == 0 {
            return Err("servicing_peer_invalid");
        }
        Ok(buf)
    }
}
fn sid_text(sid: PSID) -> Result<String> {
    unsafe {
        if IsValidSid(sid) == 0 {
            return Err("servicing_peer_invalid");
        }
        let mut s = null_mut();
        if ConvertSidToStringSidW(sid, &mut s) == 0 {
            return Err("servicing_peer_invalid");
        }
        let _allocation = Local(s.cast());
        let mut n = 0;
        while n < 256 && *s.add(n) != 0 {
            n += 1;
        }
        if n == 256 {
            return Err("servicing_peer_invalid");
        }
        String::from_utf16(std::slice::from_raw_parts(s, n)).map_err(|_| "servicing_peer_invalid")
    }
}
pub(crate) fn user(process: HANDLE) -> Result<String> {
    let b = token_info(process, TokenUser)?;
    sid_text(unsafe { (*b.as_ptr().cast::<TOKEN_USER>()).User.Sid })
}
pub(crate) fn logon(process: HANDLE) -> Result<String> {
    let b = token_info(process, TokenLogonSid)?;
    let groups = unsafe { &*b.as_ptr().cast::<TOKEN_GROUPS>() };
    if groups.GroupCount != 1
        || groups.Groups[0].Attributes & SE_GROUP_LOGON_ID != SE_GROUP_LOGON_ID
    {
        return Err("servicing_peer_invalid");
    }
    sid_text(groups.Groups[0].Sid)
}
pub(crate) struct Security {
    descriptor: Local,
}
impl Security {
    pub(crate) fn new(pipe: bool) -> Result<Self> {
        let current = unsafe { GetCurrentProcess() };
        let owner = user(current)?;
        let principal = if pipe { logon(current)? } else { owner.clone() };
        let text = wide(OsStr::new(&format!("O:{owner}D:P(A;;GA;;;{principal})")));
        let mut p = null_mut();
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                text.as_ptr(),
                1,
                &mut p,
                null_mut(),
            )
        } == 0
        {
            return Err("servicing_exclusion_unavailable");
        }
        Ok(Self {
            descriptor: Local(p),
        })
    }
    pub(crate) fn attributes(&self, inherit: bool) -> SECURITY_ATTRIBUTES {
        SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: self.descriptor.0,
            bInheritHandle: i32::from(inherit),
        }
    }
}
fn no_alias(path: &Path) -> Result<()> {
    for part in path.ancestors() {
        if let Ok(m) = std::fs::symlink_metadata(part) {
            use std::os::windows::fs::MetadataExt;
            if m.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err("servicing_exclusion_invalid");
            }
        }
    }
    Ok(())
}
fn private(path: &Path) -> Result<()> {
    no_alias(path)?;
    let (mut owner, mut acl, mut desc) = (null_mut(), null_mut(), null_mut());
    if unsafe {
        GetNamedSecurityInfoW(
            wide(path.as_os_str()).as_ptr(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            &mut acl,
            null_mut(),
            &mut desc,
        )
    } != 0
    {
        return Err("servicing_exclusion_invalid");
    }
    let _allocation = Local(desc);
    let mut control = 0;
    let mut revision = 0;
    let mut info: ACL_SIZE_INFORMATION = unsafe { zeroed() };
    if owner.is_null()
        || acl.is_null()
        || sid_text(owner)? != user(unsafe { GetCurrentProcess() })?
        || unsafe { GetSecurityDescriptorControl(desc, &mut control, &mut revision) } == 0
        || control & SE_DACL_PROTECTED == 0
        || unsafe { IsValidAcl(acl) } == 0
        || unsafe {
            GetAclInformation(
                acl,
                (&mut info as *mut ACL_SIZE_INFORMATION).cast(),
                size_of::<ACL_SIZE_INFORMATION>() as u32,
                AclSizeInformation,
            )
        } == 0
        || info.AceCount != 1
    {
        return Err("servicing_exclusion_invalid");
    }
    let mut ace = null_mut();
    if unsafe { GetAce(acl, 0, &mut ace) } == 0 {
        return Err("servicing_exclusion_invalid");
    }
    let ace = unsafe { &*ace.cast::<ACCESS_ALLOWED_ACE>() };
    if ace.Header.AceType != 0
        || ace.Header.AceFlags != 0
        || ace.Mask != FILE_ALL_ACCESS
        || sid_text((&ace.SidStart as *const u32).cast_mut().cast())?
            != user(unsafe { GetCurrentProcess() })?
    {
        return Err("servicing_exclusion_invalid");
    }
    Ok(())
}
fn root() -> Result<PathBuf> {
    let p =
        PathBuf::from(std::env::var_os("LOCALAPPDATA").ok_or("servicing_exclusion_unavailable")?)
            .join("ConversationAnalytics.Servicing");
    no_alias(&p)?;
    if !p.exists() {
        let security = Security::new(false)?;
        if unsafe { CreateDirectoryW(wide(p.as_os_str()).as_ptr(), &security.attributes(false)) }
            == 0
            && unsafe { GetLastError() } != ERROR_ALREADY_EXISTS
        {
            return Err("servicing_exclusion_unavailable");
        }
    }
    private(&p)?;
    Ok(p)
}
pub(crate) fn key(family: &str) -> Result<String> {
    if family.is_empty()
        || family.len() > 128
        || !family
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'.' || b == b'-')
    {
        return Err("servicing_exclusion_invalid");
    }
    Ok(format!(
        "{:x}",
        Sha256::digest(format!("{}\n{family}", user(unsafe { GetCurrentProcess() })?).as_bytes())
    ))
}
#[derive(Debug)]
pub(crate) struct Lease {
    pub(crate) handle: Handle,
}
impl Lease {
    pub(crate) fn acquire(family: &str) -> Result<Self> {
        let path = root()?.join(format!("{}.lease", key(family)?));
        no_alias(&path)?;
        let security = Security::new(false)?;
        let raw = unsafe {
            CreateFileW(
                wide(path.as_os_str()).as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                &security.attributes(false),
                CREATE_NEW,
                FILE_ATTRIBUTE_NORMAL,
                null_mut(),
            )
        };
        let (handle, new) = if raw == INVALID_HANDLE_VALUE {
            if unsafe { GetLastError() } != ERROR_FILE_EXISTS {
                return Err("servicing_exclusion_busy");
            }
            private(&path)?;
            (
                Handle::new(unsafe {
                    CreateFileW(
                        wide(path.as_os_str()).as_ptr(),
                        GENERIC_READ | GENERIC_WRITE,
                        0,
                        null(),
                        OPEN_EXISTING,
                        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                        null_mut(),
                    )
                })?,
                false,
            )
        } else {
            (Handle::new(raw)?, true)
        };
        private(&path)?;
        if new {
            let mut initial = MAGIC.to_vec();
            initial.extend_from_slice(&[0, 1]);
            initial.extend_from_slice(&[b'0'; 64]);
            write_at(handle.0, 0, &initial)?;
        }
        let lease = Self { handle };
        lease.validate(family)?;
        if lease.active()? {
            return Err("servicing_previous_owner_unsettled");
        }
        Ok(lease)
    }
    pub(crate) fn validate(&self, family: &str) -> Result<()> {
        let expected = root()?.join(format!("{}.lease", key(family)?));
        private(&expected)?;
        let mut name = vec![0u16; 32768];
        let n = unsafe {
            GetFinalPathNameByHandleW(self.handle.0, name.as_mut_ptr(), name.len() as u32, 0)
        };
        if n == 0 || n as usize >= name.len() {
            return Err("servicing_exclusion_invalid");
        }
        let name = PathBuf::from(
            String::from_utf16(&name[..n as usize]).map_err(|_| "servicing_exclusion_invalid")?,
        );
        if name
            != expected
                .canonicalize()
                .map_err(|_| "servicing_exclusion_invalid")?
        {
            return Err("servicing_exclusion_invalid");
        }
        let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { zeroed() };
        if unsafe { GetFileInformationByHandle(self.handle.0, &mut info) } == 0
            || info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
                != 0
            || info.nNumberOfLinks != 1
            || info.nFileSizeHigh != 0
            || info.nFileSizeLow != (MAGIC.len() + 66) as u32
        {
            return Err("servicing_exclusion_invalid");
        }
        let mut bytes = vec![0u8; MAGIC.len()];
        read_at(self.handle.0, 0, &mut bytes)?;
        if bytes != MAGIC {
            return Err("servicing_exclusion_invalid");
        }
        Ok(())
    }
    fn record(&self) -> Result<(bool, u8, String)> {
        let mut b = vec![0u8; MAGIC.len() + 66];
        read_at(self.handle.0, 0, &mut b)?;
        decode_record(&b)
    }
    pub(crate) fn active(&self) -> Result<bool> {
        Ok(self.record()?.0)
    }
    pub(crate) fn bound(&self, binding: &str) -> Result<bool> {
        self.bound_kind(binding, 1)
    }
    pub(crate) fn bound_servicing(&self, binding: &str) -> Result<bool> {
        self.bound_kind(binding, 2)
    }
    fn bound_kind(&self, binding: &str, kind: u8) -> Result<bool> {
        let (active, current_kind, current) = self.record()?;
        Ok(active && current_kind == kind && current == binding)
    }
    pub(crate) fn mark(&self, active: bool, binding: &str) -> Result<()> {
        self.mark_kind(active, binding, 1)
    }
    pub(crate) fn mark_servicing(&self, active: bool, binding: &str) -> Result<()> {
        self.mark_kind(active, binding, 2)
    }
    fn mark_kind(&self, active: bool, binding: &str, kind: u8) -> Result<()> {
        let (was_active, current_kind, current) = self.record()?;
        if !hex(binding)
            || (active && was_active)
            || (!active && (!was_active || current != binding || current_kind != kind))
        {
            return Err("servicing_exclusion_invalid");
        }
        if active {
            let mut body = vec![kind];
            body.extend_from_slice(binding.as_bytes());
            write_at(self.handle.0, (MAGIC.len() + 1) as u32, &body)?;
        }
        write_at(self.handle.0, MAGIC.len() as u32, &[u8::from(active)])?;
        if self.record()? != (active, kind, binding.to_string()) {
            return Err("servicing_exclusion_invalid");
        }
        Ok(())
    }
    pub(crate) fn deferred(family: &str, binding: &str) -> Result<Self> {
        let path = root()?.join(format!("{}.lease", key(family)?));
        private(&path)?;
        let handle = Handle::new(unsafe {
            CreateFileW(
                wide(path.as_os_str()).as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                null(),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                null_mut(),
            )
        })?;
        let lease = Self { handle };
        lease.validate(family)?;
        if !lease.bound_servicing(binding)? {
            return Err("servicing_previous_owner_unsettled");
        }
        Ok(lease)
    }
    pub(crate) fn duplicate(&self) -> Result<Handle> {
        let mut h = null_mut();
        let me = unsafe { GetCurrentProcess() };
        if unsafe { DuplicateHandle(me, self.handle.0, me, &mut h, 0, 1, DUPLICATE_SAME_ACCESS) }
            == 0
        {
            return Err("servicing_exclusion_unavailable");
        }
        Handle::new(h)
    }
}

fn hex(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn decode_record(b: &[u8]) -> Result<(bool, u8, String)> {
    if b.len() != MAGIC.len() + 66
        || &b[..MAGIC.len()] != MAGIC
        || b[MAGIC.len()] > 1
        || ![1, 2].contains(&b[MAGIC.len() + 1])
    {
        return Err("servicing_exclusion_invalid");
    }
    let binding = String::from_utf8(b[MAGIC.len() + 2..].to_vec())
        .map_err(|_| "servicing_exclusion_invalid")?;
    if !hex(&binding) {
        return Err("servicing_exclusion_invalid");
    }
    Ok((b[MAGIC.len()] == 1, b[MAGIC.len() + 1], binding))
}

#[derive(Clone, Debug, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct DeferredReceipt {
    pub schema: String,
    pub phase: String,
    pub family: String,
    pub binding: String,
    pub source_package: String,
    pub source_version: String,
    pub source_uri: String,
    pub target_package: String,
    pub target_version: String,
    pub target_name: String,
    pub target_publisher: String,
    pub target_descriptor_sha256: String,
    pub engine_present: bool,
    pub engine_family: Option<String>,
    pub engine_version: Option<String>,
}
impl DeferredReceipt {
    fn validate(&self) -> Result<()> {
        if self.schema != "normal-servicing-completion.v1"
            || self.phase != "servicing_completed_deferred"
            || !hex(&self.binding)
            || !hex(&self.target_descriptor_sha256)
            || self.source_package == self.target_package
            || self.source_version == self.target_version
            || [
                &self.family,
                &self.source_package,
                &self.source_version,
                &self.source_uri,
                &self.target_package,
                &self.target_version,
                &self.target_name,
                &self.target_publisher,
            ]
            .iter()
            .any(|s| s.is_empty() || s.len() > 1024)
            || (self.engine_present != self.engine_family.is_some())
            || (self.engine_present != self.engine_version.is_some())
            || self
                .engine_family
                .iter()
                .chain(self.engine_version.iter())
                .any(|s| s.is_empty() || s.len() > 256)
        {
            return Err("servicing_completion_invalid");
        }
        Ok(())
    }
    pub(crate) fn matches(
        &self,
        family: &str,
        package: &str,
        name: &str,
        publisher: &str,
        version: &str,
        descriptor: &str,
        source: &str,
    ) -> bool {
        self.validate().is_ok()
            && self.family == family
            && self.target_package == package
            && self.target_name == name
            && self.target_publisher == publisher
            && self.target_version == version
            && self.target_descriptor_sha256 == descriptor
            && self.source_uri == source
    }
    pub(crate) fn save(&self) -> Result<()> {
        self.validate()?;
        let bytes = serde_json::to_vec(self).map_err(|_| "servicing_completion_invalid")?;
        if bytes.len() > 4096 {
            return Err("servicing_completion_invalid");
        }
        let path = root()?.join(format!("{}.deferred.json", key(&self.family)?));
        if path.exists() {
            private(&path)?;
        }
        let temp = path.with_extension("next");
        no_alias(&temp)?;
        let security = Security::new(false)?;
        let handle = Handle::new(unsafe {
            CreateFileW(
                wide(temp.as_os_str()).as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                &security.attributes(false),
                CREATE_NEW,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                null_mut(),
            )
        })?;
        private(&temp)?;
        write_at(handle.0, 0, &bytes)?;
        drop(handle);
        if unsafe {
            MoveFileExW(
                wide(temp.as_os_str()).as_ptr(),
                wide(path.as_os_str()).as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        } == 0
        {
            return Err("servicing_completion_invalid");
        }
        private(&path)?;
        Ok(())
    }
    pub(crate) fn load(family: &str) -> Result<Option<Self>> {
        let path = root()?.join(format!("{}.deferred.json", key(family)?));
        if !path.exists() {
            return Ok(None);
        }
        private(&path)?;
        let handle = Handle::new(unsafe {
            CreateFileW(
                wide(path.as_os_str()).as_ptr(),
                GENERIC_READ,
                0,
                null(),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                null_mut(),
            )
        })?;
        let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { zeroed() };
        if unsafe { GetFileInformationByHandle(handle.0, &mut info) } == 0
            || info.nNumberOfLinks != 1
            || info.nFileSizeHigh != 0
            || info.nFileSizeLow == 0
            || info.nFileSizeLow > 4096
            || info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
                != 0
        {
            return Err("servicing_completion_invalid");
        }
        let mut bytes = vec![0; info.nFileSizeLow as usize];
        read_at(handle.0, 0, &mut bytes)?;
        let receipt: Self =
            serde_json::from_slice(&bytes).map_err(|_| "servicing_completion_invalid")?;
        receipt.validate()?;
        if receipt.family != family {
            return Err("servicing_completion_invalid");
        }
        Ok(Some(receipt))
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lease_record_is_closed_and_distinguishes_owners() {
        for b in [vec![], MAGIC.to_vec(), vec![0; MAGIC.len() + 66]] {
            assert!(decode_record(&b).is_err());
        }
        let mut b = MAGIC.to_vec();
        b.extend_from_slice(&[1, 2]);
        b.extend_from_slice(&[b'a'; 64]);
        assert_eq!(decode_record(&b).unwrap(), (true, 2, "a".repeat(64)));
        b[MAGIC.len() + 1] = 1;
        assert_eq!(decode_record(&b).unwrap().1, 1);
        b[MAGIC.len() + 1] = 3;
        assert!(decode_record(&b).is_err());
    }
    fn receipt() -> DeferredReceipt {
        DeferredReceipt {
            schema: "normal-servicing-completion.v1".into(),
            phase: "servicing_completed_deferred".into(),
            family: "Core_pub".into(),
            binding: "a".repeat(64),
            source_package: "Core_1_x64__pub".into(),
            source_version: "1.0.0.0".into(),
            source_uri: "https://example.test/app.appinstaller".into(),
            target_package: "Core_2_x64__pub".into(),
            target_version: "2.0.0.0".into(),
            target_name: "Core".into(),
            target_publisher: "CN=Publisher".into(),
            target_descriptor_sha256: "b".repeat(64),
            engine_present: false,
            engine_family: None,
            engine_version: None,
        }
    }
    #[test]
    fn dropped_preparation_receipt_requires_exact_target_admission() {
        let r = receipt();
        let bytes = serde_json::to_vec(&r).unwrap();
        drop(r);
        let r: DeferredReceipt = serde_json::from_slice(&bytes).unwrap();
        let args = (
            "Core_pub",
            "Core_2_x64__pub",
            "Core",
            "CN=Publisher",
            "2.0.0.0",
            "b".repeat(64),
            "https://example.test/app.appinstaller",
        );
        assert!(r.matches(args.0, args.1, args.2, args.3, args.4, &args.5, args.6));
        for (package, version, sha) in [
            ("Core_1_x64__pub", "1.0.0.0", args.5.clone()),
            ("Other_2_x64__pub", "2.0.0.0", args.5.clone()),
            (args.1, args.4, "c".repeat(64)),
        ] {
            assert!(!r.matches(args.0, package, args.2, args.3, version, &sha, args.6));
        }
        let mut bad = r.clone();
        bad.phase = "acquisition_active".into();
        assert!(bad.validate().is_err());
        bad = r.clone();
        bad.engine_present = true;
        assert!(bad.validate().is_err());
        let mut value = serde_json::to_value(r).unwrap();
        value["terminal_from_absence"] = true.into();
        assert!(serde_json::from_value::<DeferredReceipt>(value).is_err());
    }
    #[test]
    fn duplicate_file_object_keeps_exclusion_and_fixed_offset_record() {
        let name = format!(
            "native-family-lease-fixture-{}-{}.tmp",
            unsafe { GetCurrentProcessId() },
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let path = std::env::temp_dir().join(name);
        let security = Security::new(false).unwrap();
        let original = Handle::new(unsafe {
            CreateFileW(
                wide(path.as_os_str()).as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                &security.attributes(false),
                CREATE_NEW,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                null_mut(),
            )
        })
        .unwrap();
        private(&path).unwrap();
        let mut b = MAGIC.to_vec();
        b.extend_from_slice(&[0, 1]);
        b.extend_from_slice(&[b'0'; 64]);
        write_at(original.0, 0, &b).unwrap();
        let lease = Lease { handle: original };
        let duplicate = Lease {
            handle: lease.duplicate().unwrap(),
        };
        assert_ne!(
            unsafe { SetFilePointerEx(lease.handle.0, 777, null_mut(), FILE_BEGIN) },
            0
        );
        lease.mark(true, &"a".repeat(64)).unwrap();
        assert!(duplicate.bound(&"a".repeat(64)).unwrap());
        assert!(!duplicate.bound_servicing(&"a".repeat(64)).unwrap());
        assert!(duplicate.mark_servicing(false, &"a".repeat(64)).is_err());
        assert_ne!(
            unsafe { SetFilePointerEx(duplicate.handle.0, 999, null_mut(), FILE_BEGIN) },
            0
        );
        duplicate.mark(false, &"a".repeat(64)).unwrap();
        lease.mark_servicing(true, &"b".repeat(64)).unwrap();
        assert!(duplicate.bound_servicing(&"b".repeat(64)).unwrap());
        drop(lease);
        let open = || unsafe {
            CreateFileW(
                wide(path.as_os_str()).as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                null(),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                null_mut(),
            )
        };
        assert_eq!(open(), INVALID_HANDLE_VALUE);
        assert_eq!(unsafe { GetLastError() }, ERROR_SHARING_VIOLATION);
        drop(duplicate);
        let reopened = Lease {
            handle: Handle::new(open()).unwrap(),
        };
        assert!(reopened.bound_servicing(&"b".repeat(64)).unwrap());
        reopened.mark_servicing(false, &"b".repeat(64)).unwrap();
        drop(reopened);
        std::fs::remove_file(path).unwrap();
    }
}
