//! Native-only servicing handoff. No renderer chooses a path, image or URL.
use crate::{
    deployment::DesktopRelease,
    handoff_contract::{Intent, Phase, EXIT_FOR_SERVICING, MAX_FRAME},
    policy, servicing, windows,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    ffi::OsStr,
    fs::File,
    io::{Read, Write},
    mem::{size_of, zeroed},
    os::windows::{ffi::OsStrExt, fs::OpenOptionsExt, io::FromRawHandle},
    path::{Path, PathBuf},
    ptr::{null, null_mut},
    sync::Arc,
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::*,
    Security::{Authorization::*, *},
    Storage::{FileSystem::*, Packaging::Appx::*},
    System::{Diagnostics::ToolHelp::*, Pipes::*, Threading::*},
};

type Result<T> = std::result::Result<T, &'static str>;
const HELPER_FLAG: &str = "--msix-servicing-helper";
pub const ACTIVATION_FLAG: &str = "--msix-servicing-activation";
const HELPER_BUDGET: Duration = Duration::from_secs(120);

struct Handle(HANDLE);
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
impl Handle {
    fn checked(value: HANDLE) -> Result<Self> {
        if value.is_null() || value == INVALID_HANDLE_VALUE {
            Err("servicing_handle_invalid")
        } else {
            Ok(Self(value))
        }
    }
}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value.as_ref().encode_wide().chain(Some(0)).collect()
}

struct Local(*mut std::ffi::c_void);
impl Drop for Local {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.0);
        }
    }
}

struct PrivateSecurity {
    descriptor: Local,
}
impl PrivateSecurity {
    fn new() -> Result<Self> {
        let sid = windows::current_sid()?;
        let mut text = null_mut();
        if unsafe { ConvertSidToStringSidW(sid.as_ptr() as _, &mut text) } == 0 {
            return Err("servicing_storage_invalid");
        }
        let allocation = Local(text.cast());
        let mut length = 0;
        unsafe {
            while *text.add(length) != 0 && length < 256 {
                length += 1;
            }
        }
        if length >= 256 {
            return Err("servicing_storage_invalid");
        }
        let sid_text = String::from_utf16(unsafe { std::slice::from_raw_parts(text, length) })
            .map_err(|_| "servicing_storage_invalid")?;
        drop(allocation);
        let mut descriptor = null_mut();
        let sddl = wide(format!("O:{sid_text}D:P(A;;FA;;;{sid_text})"));
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                SDDL_REVISION_1,
                &mut descriptor,
                null_mut(),
            )
        } == 0
        {
            return Err("servicing_storage_invalid");
        }
        Ok(Self {
            descriptor: Local(descriptor),
        })
    }
    fn attributes(&self, inherit: bool) -> SECURITY_ATTRIBUTES {
        SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: self.descriptor.0,
            bInheritHandle: inherit as i32,
        }
    }
}

fn private_path(path: &Path) -> Result<()> {
    policy::safe_data_path(path)?;
    let name = wide(path);
    let (mut owner, mut acl, mut descriptor) = (null_mut(), null_mut(), null_mut());
    let code = unsafe {
        GetNamedSecurityInfoW(
            name.as_ptr(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            &mut acl,
            null_mut(),
            &mut descriptor,
        )
    };
    if code != ERROR_SUCCESS {
        return Err("servicing_storage_invalid");
    }
    let _allocation = Local(descriptor);
    let sid = windows::current_sid()?;
    let mut control = 0;
    let mut revision = 0;
    let mut size: ACL_SIZE_INFORMATION = unsafe { zeroed() };
    if owner.is_null()
        || acl.is_null()
        || unsafe { IsValidSid(owner) } == 0
        || unsafe { IsValidAcl(acl) } == 0
        || unsafe { EqualSid(owner, sid.as_ptr() as _) } == 0
        || unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) } == 0
        || control & SE_DACL_PROTECTED == 0
        || unsafe {
            GetAclInformation(
                acl,
                &mut size as *mut _ as _,
                size_of::<ACL_SIZE_INFORMATION>() as u32,
                AclSizeInformation,
            )
        } == 0
        || size.AceCount != 1
    {
        return Err("servicing_storage_invalid");
    }
    let mut pointer = null_mut();
    if unsafe { GetAce(acl, 0, &mut pointer) } == 0 {
        return Err("servicing_storage_invalid");
    }
    let header = unsafe { &*pointer.cast::<ACE_HEADER>() };
    if header.AceSize < size_of::<ACCESS_ALLOWED_ACE>() as u16 {
        return Err("servicing_storage_invalid");
    }
    let ace = unsafe { &*pointer.cast::<ACCESS_ALLOWED_ACE>() };
    if ace.Header.AceType != 0
        || ace.Header.AceFlags != 0
        || ace.Mask != FILE_ALL_ACCESS
        || unsafe { IsValidSid((&ace.SidStart as *const u32) as _) } == 0
        || unsafe { EqualSid((&ace.SidStart as *const u32) as _, sid.as_ptr() as _) } == 0
    {
        return Err("servicing_storage_invalid");
    }
    Ok(())
}

fn create_directory(path: &Path) -> Result<()> {
    policy::safe_data_path(path)?;
    if !path.exists() {
        let security = PrivateSecurity::new()?;
        if unsafe { CreateDirectoryW(wide(path).as_ptr(), &security.attributes(false)) } == 0 {
            return Err("servicing_storage_invalid");
        }
    }
    private_path(path)
}

fn cache_root(data: &Path, create: bool) -> Result<PathBuf> {
    let temp = policy::safe_data_path(&data.join("temp"))?;
    let root = temp.join("native-servicing");
    if create {
        if !temp.exists() {
            create_directory(&temp)?;
        }
        create_directory(&root)?;
    } else {
        private_path(&root)?;
    }
    Ok(root)
}

fn create_file(path: &Path) -> Result<File> {
    let security = PrivateSecurity::new()?;
    let raw = unsafe {
        CreateFileW(
            wide(path).as_ptr(),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ,
            &security.attributes(false),
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            null_mut(),
        )
    };
    let handle = Handle::checked(raw)?;
    let raw = handle.0;
    std::mem::forget(handle);
    Ok(unsafe { File::from_raw_handle(raw) })
}

pub fn load_intent(data: &Path) -> Result<Option<Intent>> {
    let path = data.join("temp/native-servicing/intent.json");
    policy::safe_data_path(&path)?;
    if !path.exists() {
        return Ok(None);
    }
    private_path(&path)?;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .open(&path)
        .map_err(|_| "servicing_storage_invalid")?;
    let mut bytes = Vec::new();
    file.take(MAX_FRAME as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "servicing_storage_invalid")?;
    Intent::decode(&bytes).map(Some)
}

fn save_intent(data: &Path, intent: &Intent) -> Result<()> {
    intent.validate()?;
    let root = cache_root(data, true)?;
    let target = root.join("intent.json");
    if target.exists() {
        private_path(&target)?;
    }
    let bytes = serde_json::to_vec(intent).map_err(|_| "servicing_intent_invalid")?;
    if bytes.len() > MAX_FRAME {
        return Err("servicing_intent_invalid");
    }
    // The root lease serializes native writers. Only intent.json is durable
    // authority; discard an exact private scratch object left before a rename.
    let temporary = root.join("intent.next");
    if temporary.exists() {
        private_path(&temporary)?;
        std::fs::remove_file(&temporary).map_err(|_| "servicing_storage_invalid")?;
    }
    let mut file = create_file(&temporary)?;
    file.write_all(&bytes)
        .and_then(|_| file.sync_all())
        .map_err(|_| "servicing_storage_invalid")?;
    drop(file);
    if unsafe {
        MoveFileExW(
            wide(&temporary).as_ptr(),
            wide(&target).as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        let _ = std::fs::remove_file(&temporary);
        return Err("servicing_storage_invalid");
    }
    Ok(())
}

fn nonce() -> Result<String> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| "servicing_random_unavailable")?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn digest(path: &Path) -> Result<String> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .open(path)
        .map_err(|_| "servicing_image_invalid")?;
    if file
        .metadata()
        .map_err(|_| "servicing_image_invalid")?
        .len()
        > 64 * 1024 * 1024
    {
        return Err("servicing_image_invalid");
    }
    let mut reader = file.take(64 * 1024 * 1024 + 1);
    let mut hash = Sha256::new();
    let mut bytes = [0u8; 65536];
    loop {
        let count = reader
            .read(&mut bytes)
            .map_err(|_| "servicing_image_invalid")?;
        if count == 0 {
            break;
        }
        hash.update(&bytes[..count]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

#[derive(Clone)]
pub struct Cancellation(Arc<Handle>);
impl Cancellation {
    pub fn new() -> Result<Self> {
        let security = PrivateSecurity::new()?;
        Ok(Self(Arc::new(Handle::checked(unsafe {
            CreateEventW(&security.attributes(false), 1, 0, null())
        })?)))
    }
    pub fn signal(&self) {
        unsafe {
            SetEvent(self.0 .0);
        }
    }
    fn cancelled(&self) -> bool {
        unsafe { WaitForSingleObject(self.0 .0, 0) != WAIT_TIMEOUT }
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Frame {
    intent: Intent,
    root: PathBuf,
}

pub struct Pending {
    intent: Intent,
    data: PathBuf,
    writer: Option<Handle>,
    helper: Handle,
    cancel: Cancellation,
    committed: bool,
    _locked_copy: File,
}

// A failure before the helper owns its cancellation handle must still leave
// durable evidence that old code may resume. Never modify application state.
struct Preparation<'a> {
    data: &'a Path,
    intent: Intent,
    armed: bool,
}
impl Drop for Preparation<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.intent.phase = Phase::Cancelled;
            let _ = save_intent(self.data, &self.intent);
        }
    }
}

fn retire_copy(data: &Path, intent: &Intent) -> Result<()> {
    intent.validate()?;
    let session = cache_root(data, false)?.join(&intent.session);
    if !session.exists() {
        return Ok(());
    }
    private_path(&session)?;
    let image = session.join("Bridge.exe");
    if image.exists() {
        private_path(&image)?;
        // This is the exact private cache object named by our durable intent.
        // Refuse any additional objects; never remove a directory recursively.
        std::fs::remove_file(&image).map_err(|_| "servicing_cache_busy")?;
    }
    std::fs::remove_dir(&session).map_err(|_| "servicing_cache_busy")
}
impl Pending {
    pub fn commit(&mut self) -> Result<()> {
        if self.cancel.cancelled() {
            return Err("servicing_cancelled");
        }
        self.intent.phase = Phase::Committed;
        save_intent(&self.data, &self.intent)?;
        write_pipe(
            self.writer.as_ref().ok_or("servicing_handle_invalid")?.0,
            b"G",
        )?;
        self.writer.take();
        self.committed = true;
        Ok(())
    }
}
impl Drop for Pending {
    fn drop(&mut self) {
        if !self.committed {
            self.cancel.signal();
            self.writer.take();
            self.intent.phase = Phase::Cancelled;
            let _ = save_intent(&self.data, &self.intent);
            unsafe {
                WaitForSingleObject(self.helper.0, 2000);
            }
        }
    }
}

fn pipe() -> Result<(Handle, Handle)> {
    let security = PrivateSecurity::new()?;
    let (mut read, mut write) = (null_mut(), null_mut());
    if unsafe {
        CreatePipe(
            &mut read,
            &mut write,
            &security.attributes(true),
            (MAX_FRAME * 2) as u32,
        )
    } == 0
    {
        return Err("servicing_handle_invalid");
    }
    Ok((Handle::checked(read)?, Handle::checked(write)?))
}
fn duplicate(handle: HANDLE, access: u32) -> Result<Handle> {
    let mut target = null_mut();
    if unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            handle,
            GetCurrentProcess(),
            &mut target,
            access,
            1,
            0,
        )
    } == 0
    {
        return Err("servicing_handle_invalid");
    }
    Handle::checked(target)
}
fn write_pipe(pipe: HANDLE, bytes: &[u8]) -> Result<()> {
    let mut written = 0;
    if unsafe {
        WriteFile(
            pipe,
            bytes.as_ptr(),
            bytes.len() as u32,
            &mut written,
            null_mut(),
        )
    } == 0
        || written as usize != bytes.len()
    {
        return Err("servicing_pipe_failed");
    }
    Ok(())
}
fn read_pipe(pipe: HANDLE, count: usize, deadline: Instant, cancel: HANDLE) -> Result<Vec<u8>> {
    let mut result = Vec::with_capacity(count);
    while result.len() < count {
        if Instant::now() >= deadline || unsafe { WaitForSingleObject(cancel, 0) } != WAIT_TIMEOUT {
            return Err("servicing_cancelled");
        }
        let mut available = 0;
        if unsafe { PeekNamedPipe(pipe, null_mut(), 0, null_mut(), &mut available, null_mut()) }
            == 0
        {
            return Err("servicing_pipe_failed");
        }
        if available as usize > MAX_FRAME + 8 {
            return Err("servicing_frame_invalid");
        }
        if available == 0 {
            std::thread::sleep(Duration::from_millis(10));
            continue;
        }
        let length = (available as usize).min(count - result.len());
        let mut bytes = vec![0; length];
        let mut received = 0;
        if unsafe {
            ReadFile(
                pipe,
                bytes.as_mut_ptr(),
                length as u32,
                &mut received,
                null_mut(),
            )
        } == 0
            || received == 0
        {
            return Err("servicing_pipe_failed");
        }
        result.extend_from_slice(&bytes[..received as usize]);
    }
    Ok(result)
}

fn pipe_finished(pipe: HANDLE, deadline: Instant, cancel: HANDLE) -> Result<()> {
    loop {
        if Instant::now() >= deadline || unsafe { WaitForSingleObject(cancel, 0) } != WAIT_TIMEOUT {
            return Err("servicing_cancelled");
        }
        let mut available = 0;
        if unsafe { PeekNamedPipe(pipe, null_mut(), 0, null_mut(), &mut available, null_mut()) }
            == 0
        {
            return if unsafe { GetLastError() } == ERROR_BROKEN_PIPE {
                Ok(())
            } else {
                Err("servicing_pipe_failed")
            };
        }
        if available != 0 {
            return Err("servicing_frame_invalid");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn process_package(process: HANDLE, family: bool) -> Result<Option<String>> {
    unsafe {
        let mut size = 0;
        let code = if family {
            GetPackageFamilyName(process, &mut size, null_mut())
        } else {
            GetPackageFullName(process, &mut size, null_mut())
        };
        if code == APPMODEL_ERROR_NO_PACKAGE {
            return Ok(None);
        }
        if code != ERROR_INSUFFICIENT_BUFFER || !(2..=4096).contains(&size) {
            return Err("servicing_parent_invalid");
        }
        let mut bytes = vec![0u16; size as usize];
        let code = if family {
            GetPackageFamilyName(process, &mut size, bytes.as_mut_ptr())
        } else {
            GetPackageFullName(process, &mut size, bytes.as_mut_ptr())
        };
        if code != ERROR_SUCCESS
            || size < 2
            || size as usize > bytes.len()
            || bytes[size as usize - 1] != 0
        {
            return Err("servicing_parent_invalid");
        }
        String::from_utf16(&bytes[..size as usize - 1])
            .map(Some)
            .map_err(|_| "servicing_parent_invalid")
    }
}

struct Attributes {
    bytes: Vec<usize>,
    initialized: bool,
}
impl Attributes {
    fn new(handles: &mut [HANDLE], policy: &mut u32) -> Result<Self> {
        unsafe {
            let mut size = 0;
            InitializeProcThreadAttributeList(null_mut(), 2, 0, &mut size);
            let mut value = Self {
                bytes: vec![0; size.div_ceil(size_of::<usize>())],
                initialized: false,
            };
            if InitializeProcThreadAttributeList(value.raw(), 2, 0, &mut size) == 0 {
                return Err("servicing_helper_failed");
            }
            value.initialized = true;
            if UpdateProcThreadAttribute(
                value.raw(),
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_ptr() as _,
                std::mem::size_of_val(handles),
                null_mut(),
                null(),
            ) == 0
                || UpdateProcThreadAttribute(
                    value.raw(),
                    0,
                    PROC_THREAD_ATTRIBUTE_DESKTOP_APP_POLICY as usize,
                    policy as *mut _ as _,
                    4,
                    null_mut(),
                    null(),
                ) == 0
            {
                return Err("servicing_helper_failed");
            }
            Ok(value)
        }
    }
    fn raw(&mut self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
        self.bytes.as_mut_ptr().cast()
    }
}
impl Drop for Attributes {
    fn drop(&mut self) {
        if self.initialized {
            unsafe {
                DeleteProcThreadAttributeList(self.raw());
            }
        }
    }
}

pub fn start(
    data: &Path,
    release: &DesktopRelease,
    update: &servicing::PreparedUpdate,
    cancel: Cancellation,
) -> Result<Pending> {
    if cancel.cancelled() {
        return Err("servicing_cancelled");
    }
    if let Some(previous) = load_intent(data)? {
        if !matches!(previous.phase, Phase::Cancelled | Phase::Completed) {
            return Err("servicing_transition_pending");
        }
        retire_copy(data, &previous)?;
    }
    let source = std::env::current_exe().map_err(|_| "servicing_image_invalid")?;
    let family =
        process_package(unsafe { GetCurrentProcess() }, true)?.ok_or("servicing_parent_invalid")?;
    let full_name = process_package(unsafe { GetCurrentProcess() }, false)?
        .ok_or("servicing_parent_invalid")?;
    let (observed, _) = servicing::installed(&full_name)?;
    if servicing::bind(release, &observed) != servicing::UpdateSource::DirectAppinstaller {
        return Err("servicing_source_unavailable");
    }
    let intent = Intent {
        schema: "ofca-native-servicing/v1".into(),
        session: nonce()?,
        name: observed.identity.name,
        publisher: observed.identity.publisher,
        family,
        source_version: observed.identity.version,
        target_version: update.version.clone(),
        target_full_name: update.package_full_name.clone(),
        app_installer_uri: release
            .app_installer_uri
            .clone()
            .ok_or("servicing_source_unavailable")?,
        source_sha256: digest(&source)?,
        phase: Phase::Prepared,
    };
    intent.validate()?;
    save_intent(data, &intent)?;
    let mut preparation = Preparation {
        data,
        intent: intent.clone(),
        armed: true,
    };
    let root = cache_root(data, true)?;
    let session = root.join(&intent.session);
    create_directory(&session)?;
    let image = session.join("Bridge.exe");
    let mut copy = create_file(&image)?;
    let mut original = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .open(&source)
        .map_err(|_| "servicing_image_invalid")?;
    std::io::copy(&mut original, &mut copy)
        .and_then(|_| copy.sync_all())
        .map_err(|_| "servicing_image_invalid")?;
    drop(copy);
    if digest(&image)? != intent.source_sha256 {
        return Err("servicing_image_invalid");
    }
    let copy = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .open(&image)
        .map_err(|_| "servicing_image_invalid")?;
    drop(original);
    let (input, output) = pipe()?;
    let (status, response) = pipe()?;
    unsafe {
        if SetHandleInformation(output.0, HANDLE_FLAG_INHERIT, 0) == 0
            || SetHandleInformation(status.0, HANDLE_FLAG_INHERIT, 0) == 0
        {
            return Err("servicing_handle_invalid");
        }
    }
    let parent = duplicate(
        unsafe { GetCurrentProcess() },
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
    )?;
    let cancellation = duplicate(cancel.0 .0, SYNCHRONIZE)?;
    let mut inherited = [parent.0, input.0, response.0, cancellation.0];
    let mut breakaway = 1u32;
    let mut attributes = Attributes::new(&mut inherited, &mut breakaway)?;
    let mut startup: STARTUPINFOEXW = unsafe { zeroed() };
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.lpAttributeList = attributes.raw();
    let mut process: PROCESS_INFORMATION = unsafe { zeroed() };
    let mut command = wide(format!(
        "\"{}\" {HELPER_FLAG} {} {} {} {}",
        image.display(),
        parent.0 as usize,
        input.0 as usize,
        response.0 as usize,
        cancellation.0 as usize
    ));
    if unsafe {
        CreateProcessW(
            wide(&image).as_ptr(),
            command.as_mut_ptr(),
            null(),
            null(),
            1,
            CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
            null(),
            wide(&session).as_ptr(),
            &startup.StartupInfo,
            &mut process,
        )
    } == 0
    {
        return Err("servicing_helper_failed");
    }
    let helper = Handle::checked(process.hProcess)?;
    let _thread = Handle::checked(process.hThread)?;
    drop(input);
    drop(response);
    drop(parent);
    drop(cancellation);
    let mut pending = Pending {
        intent: intent.clone(),
        data: data.to_owned(),
        writer: Some(output),
        helper,
        cancel,
        committed: false,
        _locked_copy: copy,
    };
    preparation.armed = false;
    let bytes = serde_json::to_vec(&Frame {
        intent,
        root: data.to_owned(),
    })
    .map_err(|_| "servicing_frame_invalid")?;
    if bytes.len() > MAX_FRAME {
        return Err("servicing_frame_invalid");
    }
    let mut frame = (bytes.len() as u32).to_le_bytes().to_vec();
    frame.extend_from_slice(&bytes);
    write_pipe(pending.writer.as_ref().unwrap().0, &frame)?;
    if read_pipe(
        status.0,
        1,
        Instant::now() + Duration::from_secs(5),
        pending.cancel.0 .0,
    )? != b"R"
    {
        return Err("servicing_helper_failed");
    }
    if pending.cancel.cancelled() {
        return Err("servicing_cancelled");
    }
    // The read/share lock remains held throughout preparation and commit.
    pending.committed = false;
    Ok(pending)
}

pub fn helper_requested(args: &[String]) -> bool {
    args.get(1).is_some_and(|value| value == HELPER_FLAG)
}
fn argument_handle(value: &str) -> Result<Handle> {
    let number: usize = value.parse().map_err(|_| "servicing_handle_invalid")?;
    if number.to_string() != value || number < 4 || number == usize::MAX {
        return Err("servicing_handle_invalid");
    }
    Handle::checked(number as HANDLE)
}

pub fn helper_main(args: &[String]) -> Result<()> {
    if args.len() != 6 || !helper_requested(args) || windows::package_identity()?.is_some() {
        return Err("servicing_helper_invalid");
    }
    let values = [&args[2], &args[3], &args[4], &args[5]];
    for (index, value) in values.iter().enumerate() {
        if values[..index].contains(value) {
            return Err("servicing_handle_invalid");
        }
    }
    let parent = argument_handle(&args[2])?;
    let input = argument_handle(&args[3])?;
    let status = argument_handle(&args[4])?;
    let cancel = argument_handle(&args[5])?;
    if unsafe { GetProcessId(parent.0) } == 0
        || unsafe { GetProcessId(parent.0) } == unsafe { GetCurrentProcessId() }
        || windows::process_sid(parent.0)? != windows::current_sid()?
    {
        return Err("servicing_parent_invalid");
    }
    let deadline = Instant::now() + HELPER_BUDGET;
    let length = read_pipe(input.0, 4, deadline, cancel.0)?;
    let length =
        u32::from_le_bytes(length.try_into().map_err(|_| "servicing_frame_invalid")?) as usize;
    if length == 0 || length > MAX_FRAME {
        return Err("servicing_frame_invalid");
    }
    let frame: Frame = serde_json::from_slice(&read_pipe(input.0, length, deadline, cancel.0)?)
        .map_err(|_| "servicing_frame_invalid")?;
    frame.intent.validate()?;
    let full_name = process_package(parent.0, false)?.ok_or("servicing_parent_invalid")?;
    let family = process_package(parent.0, true)?.ok_or("servicing_parent_invalid")?;
    let parent_image = windows::process_image(parent.0)?;
    let (observed, installed) = servicing::installed(&full_name)?;
    let release = crate::deployment::load(
        &parent_image
            .parent()
            .ok_or("servicing_parent_invalid")?
            .join("desktop-release.json"),
        Some(&observed.identity),
    )?
    .ok_or("servicing_parent_invalid")?;
    let expected_parent = Path::new(&installed)
        .join("app/Bridge.exe")
        .canonicalize()
        .map_err(|_| "servicing_parent_invalid")?;
    if parent_image != expected_parent
        || family != frame.intent.family
        || !frame.intent.matches_release(&release)
        || observed.identity.version != frame.intent.source_version
        || servicing::bind(&release, &observed) != servicing::UpdateSource::DirectAppinstaller
    {
        return Err("servicing_parent_invalid");
    }
    let root = policy::safe_data_path(&frame.root)?;
    let product = policy::safe_data_path(
        &PathBuf::from(std::env::var_os("LOCALAPPDATA").ok_or("servicing_storage_invalid")?)
            .join("OnlyFans Conversational Analytics"),
    )?;
    release.validate_override(&root, &product)?;
    let image = std::env::current_exe().map_err(|_| "servicing_image_invalid")?;
    let expected_image = cache_root(&root, false)?
        .join(&frame.intent.session)
        .join("Bridge.exe");
    if policy::safe_data_path(&image)? != expected_image
        || digest(&image)? != frame.intent.source_sha256
        || digest(&parent_image)? != frame.intent.source_sha256
    {
        return Err("servicing_image_invalid");
    }
    private_path(&image)?;
    if load_intent(&root)?.as_ref() != Some(&frame.intent) {
        return Err("servicing_intent_invalid");
    }
    write_pipe(status.0, b"R")?;
    drop(status);
    if read_pipe(input.0, 1, deadline, cancel.0)? != b"G" {
        return Err("servicing_cancelled");
    }
    pipe_finished(input.0, deadline, cancel.0)?;
    drop(input);
    let mut committed = frame.intent;
    committed.phase = Phase::Committed;
    if load_intent(&root)?.as_ref() != Some(&committed) {
        return Err("servicing_intent_invalid");
    }
    loop {
        if Instant::now() >= deadline || unsafe { WaitForSingleObject(cancel.0, 0) } != WAIT_TIMEOUT
        {
            return Err("servicing_cancelled");
        }
        match unsafe { WaitForSingleObject(parent.0, 50) } {
            WAIT_OBJECT_0 => break,
            WAIT_TIMEOUT => (),
            _ => return Err("servicing_parent_invalid"),
        }
    }
    let mut exit = 0;
    if unsafe { GetExitCodeProcess(parent.0, &mut exit) } == 0 || exit != EXIT_FOR_SERVICING {
        return Err("servicing_parent_invalid");
    }
    drop(parent);
    while family_running(&family)? {
        if Instant::now() >= deadline
            || unsafe { WaitForSingleObject(cancel.0, 50) } != WAIT_TIMEOUT
        {
            return Err("servicing_cancelled");
        }
    }
    if unsafe { WaitForSingleObject(cancel.0, 0) } != WAIT_TIMEOUT {
        return Err("servicing_cancelled");
    }
    // Windows may briefly return the old registration while deferred deployment
    // settles. Old code refuses this private activation before WebView or Brain.
    // Observe its exit, then retry the stable AUMID within the same total budget.
    loop {
        if Instant::now() >= deadline || unsafe { WaitForSingleObject(cancel.0, 0) } != WAIT_TIMEOUT
        {
            return Err("servicing_activation_failed");
        }
        let target = match activate(&family, &committed.session) {
            Ok(value) => value,
            Err(_) => {
                std::thread::sleep(Duration::from_millis(200));
                continue;
            }
        };
        if let Ok(process) = Handle::checked(unsafe {
            OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, target)
        }) {
            if process_package(process.0, true)?.as_deref() != Some(&family) {
                return Err("servicing_activation_failed");
            }
            if let Some(full_name) = process_package(process.0, false)? {
                let (observed, _) = servicing::installed(&full_name)?;
                if observed.identity.name != committed.name
                    || observed.identity.publisher != committed.publisher
                {
                    return Err("servicing_activation_failed");
                }
                if crate::handoff_contract::version(&observed.identity.version)?
                    >= crate::handoff_contract::version(&committed.target_version)?
                {
                    return Ok(());
                }
            }
        }
        while family_running(&family)? {
            if Instant::now() >= deadline
                || unsafe { WaitForSingleObject(cancel.0, 50) } != WAIT_TIMEOUT
            {
                return Err("servicing_activation_failed");
            }
        }
        std::thread::sleep(Duration::from_millis(200));
    }
}

fn family_running(family: &str) -> Result<bool> {
    let snapshot = Handle::checked(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) })?;
    let mut entry: PROCESSENTRY32W = unsafe { zeroed() };
    entry.dwSize = size_of::<PROCESSENTRY32W>() as u32;
    let sid = windows::current_sid()?;
    let mut exists = unsafe { Process32FirstW(snapshot.0, &mut entry) } != 0;
    while exists {
        if entry.th32ProcessID != unsafe { GetCurrentProcessId() } {
            if let Ok(process) = Handle::checked(unsafe {
                OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, entry.th32ProcessID)
            }) {
                if windows::process_sid(process.0).is_ok_and(|value| value == sid)
                    && process_package(process.0, true)?.as_deref() == Some(family)
                {
                    return Ok(true);
                }
            }
        }
        exists = unsafe { Process32NextW(snapshot.0, &mut entry) } != 0;
    }
    Ok(false)
}

fn activate(family: &str, session: &str) -> Result<u32> {
    use windows_runtime::{
        core::PCWSTR,
        Win32::{
            System::Com::{
                CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_LOCAL_SERVER,
                COINIT_APARTMENTTHREADED,
            },
            UI::Shell::{
                ApplicationActivationManager, IApplicationActivationManager, AO_NOERRORUI,
            },
        },
    };
    unsafe {
        CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            .ok()
            .map_err(|_| "servicing_activation_failed")?;
        struct Apartment;
        impl Drop for Apartment {
            fn drop(&mut self) {
                unsafe {
                    CoUninitialize();
                }
            }
        }
        let _apartment = Apartment;
        let manager: IApplicationActivationManager =
            CoCreateInstance(&ApplicationActivationManager, None, CLSCTX_LOCAL_SERVER)
                .map_err(|_| "servicing_activation_failed")?;
        manager
            .ActivateApplication(
                PCWSTR(wide(format!("{family}!Bridge")).as_ptr()),
                PCWSTR(wide(format!("{ACTIVATION_FLAG} {session}")).as_ptr()),
                AO_NOERRORUI,
            )
            .map_err(|_| "servicing_activation_failed")
    }
}

pub fn startup(
    data: &Path,
    release: Option<&DesktopRelease>,
    actual_version: Option<&str>,
    session: Option<&str>,
) -> Result<()> {
    let Some(mut intent) = load_intent(data)? else {
        return if session.is_some() {
            Err("servicing_intent_invalid")
        } else {
            Ok(())
        };
    };
    if !release.is_some_and(|value| intent.matches_release(value))
        || session.is_some_and(|value| value != intent.session)
    {
        return Err("servicing_intent_invalid");
    }
    // The primary runtime-root lease is already held. A prepared transition has
    // never authorized relaunch; an interrupted preparation can be abandoned.
    if intent.phase == Phase::Prepared && session.is_none() {
        intent.phase = Phase::Cancelled;
        save_intent(data, &intent)?;
    }
    if !intent.admits(actual_version.ok_or("servicing_parent_invalid")?)? {
        return Err("servicing_pending");
    }
    Ok(())
}

pub fn admitted(data: &Path) -> Result<()> {
    if let Some(mut intent) = load_intent(data)? {
        if intent.phase == Phase::Committed {
            intent.phase = Phase::Completed;
            save_intent(data, &intent)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Root(PathBuf);
    impl Root {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("ofca-native-handoff-{}", nonce().unwrap()));
            std::fs::create_dir(&path).unwrap();
            Self(policy::safe_data_path(&path).unwrap())
        }
    }
    impl Drop for Root {
        fn drop(&mut self) {
            let root = self.0.join("temp/native-servicing");
            if let Ok(Some(intent)) = load_intent(&self.0) {
                let _ = retire_copy(&self.0, &intent);
            }
            let _ = std::fs::remove_file(root.join("intent.json"));
            let _ = std::fs::remove_dir(root);
            let _ = std::fs::remove_dir(self.0.join("temp"));
            std::fs::remove_dir(&self.0).expect("only exact test-owned objects remain");
        }
    }
    fn intent() -> Intent {
        crate::handoff_contract::tests::intent()
    }
    fn release() -> DesktopRelease {
        serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct.json"
        ))
        .unwrap()
    }

    #[test]
    fn durable_gate_recovers_uncommitted_preparation_but_never_admits_committed_old_code() {
        let root = Root::new();
        assert_eq!(
            startup(&root.0, Some(&release()), Some("1.0.1.0"), None),
            Ok(())
        );
        assert!(startup(
            &root.0,
            Some(&release()),
            Some("1.0.1.0"),
            Some(&"a".repeat(32))
        )
        .is_err());
        let mut value = intent();
        value.phase = Phase::Prepared;
        save_intent(&root.0, &value).unwrap();
        // Automatic activation cannot cancel a committed/prepared transition.
        assert_eq!(
            startup(
                &root.0,
                Some(&release()),
                Some("1.0.1.0"),
                Some(&value.session)
            ),
            Err("servicing_pending")
        );
        assert_eq!(
            startup(&root.0, Some(&release()), Some("1.0.1.0"), None),
            Ok(())
        );
        assert_eq!(
            load_intent(&root.0).unwrap().unwrap().phase,
            Phase::Cancelled
        );
        value.phase = Phase::Committed;
        save_intent(&root.0, &value).unwrap();
        assert_eq!(
            startup(&root.0, Some(&release()), Some("1.0.1.0"), None),
            Err("servicing_pending")
        );
        let mut wrong_release = release();
        wrong_release.identity.publisher.push(' ');
        assert_eq!(
            startup(&root.0, Some(&wrong_release), Some("1.0.2.0"), None),
            Err("servicing_intent_invalid")
        );
        assert_eq!(
            startup(
                &root.0,
                Some(&release()),
                Some("1.0.2.0"),
                Some(&value.session)
            ),
            Ok(())
        );
        admitted(&root.0).unwrap();
        assert_eq!(
            load_intent(&root.0).unwrap().unwrap().phase,
            Phase::Completed
        );
    }

    #[test]
    fn private_files_require_effective_user_ownership_and_corruption_is_not_first_run() {
        let root = Root::new();
        let value = intent();
        save_intent(&root.0, &value).unwrap();
        let cache = cache_root(&root.0, false).unwrap();
        private_path(&cache).unwrap();
        let path = cache.join("intent.json");
        private_path(&path).unwrap();
        std::fs::write(&path, b"{}").unwrap();
        assert_eq!(
            startup(&root.0, Some(&release()), Some("1.0.2.0"), None),
            Err("servicing_intent_invalid")
        );
        // A normal inherited ACL cannot impersonate the private cache contract.
        assert_eq!(private_path(&root.0), Err("servicing_storage_invalid"));
        let cancellation = Cancellation::new().unwrap();
        assert!(!cancellation.cancelled());
        cancellation.signal();
        assert!(cancellation.cancelled());
    }

    #[test]
    fn failed_preparation_is_cancelled_and_cache_retirement_never_deletes_extra_objects() {
        let root = Root::new();
        let mut value = intent();
        value.phase = Phase::Prepared;
        save_intent(&root.0, &value).unwrap();
        {
            let _guard = Preparation {
                data: &root.0,
                intent: value.clone(),
                armed: true,
            };
        }
        assert_eq!(
            load_intent(&root.0).unwrap().unwrap().phase,
            Phase::Cancelled
        );
        let session = cache_root(&root.0, false).unwrap().join(&value.session);
        create_directory(&session).unwrap();
        create_file(&session.join("Bridge.exe"))
            .unwrap()
            .write_all(b"synthetic cache")
            .unwrap();
        let extra = session.join("unexpected");
        std::fs::write(&extra, b"preserve").unwrap();
        assert_eq!(retire_copy(&root.0, &value), Err("servicing_cache_busy"));
        assert_eq!(std::fs::read(&extra).unwrap(), b"preserve");
        std::fs::remove_file(extra).unwrap();
        retire_copy(&root.0, &value).unwrap();
        assert!(!session.exists());
        assert!(helper_main(&["Bridge.exe".into(), HELPER_FLAG.into()]).is_err());
        for value in ["0", "-1", "00", "abc", "18446744073709551615"] {
            assert!(argument_handle(value).is_err());
        }
    }

    #[test]
    fn commit_frame_requires_exact_tail_and_owned_eof_before_relaunch() {
        let cancel = Cancellation::new().unwrap();
        let (input, output) = pipe().unwrap();
        write_pipe(output.0, b"Gextra").unwrap();
        assert_eq!(
            read_pipe(
                input.0,
                1,
                Instant::now() + Duration::from_secs(1),
                cancel.0 .0
            ),
            Ok(b"G".to_vec())
        );
        assert_eq!(
            pipe_finished(
                input.0,
                Instant::now() + Duration::from_secs(1),
                cancel.0 .0
            ),
            Err("servicing_frame_invalid")
        );
        let (input, output) = pipe().unwrap();
        write_pipe(output.0, b"G").unwrap();
        drop(output);
        assert_eq!(
            read_pipe(
                input.0,
                1,
                Instant::now() + Duration::from_secs(1),
                cancel.0 .0
            ),
            Ok(b"G".to_vec())
        );
        assert_eq!(
            pipe_finished(
                input.0,
                Instant::now() + Duration::from_secs(1),
                cancel.0 .0
            ),
            Ok(())
        );
    }
}
