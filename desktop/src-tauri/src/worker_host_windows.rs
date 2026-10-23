use super::*;
use crate::windows::{wide, Attributes, Handle, OUTER_JOB_QUERY};
use std::{
    ffi::OsStr,
    fs::File,
    io::{Read, Seek, SeekFrom, Write},
    mem::{size_of, zeroed},
    os::windows::{
        fs::{MetadataExt, OpenOptionsExt},
        io::{AsRawHandle, FromRawHandle},
    },
    path::{Path, PathBuf},
    ptr::{null, null_mut},
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::*,
    Security::SECURITY_ATTRIBUTES,
    Storage::{
        FileSystem::*,
        Packaging::Appx::{GetPackageFamilyName, GetPackageFullName},
    },
    System::{
        Diagnostics::ToolHelp::*,
        JobObjects::*,
        Pipes::{CreatePipe, PeekNamedPipe},
        Threading::*,
    },
};
#[link(name = "kernel32")]
unsafe extern "system" {
    fn ProcessIdToSessionId(pid: u32, session: *mut u32) -> i32;
}

#[repr(C)]
struct ObjectBasic {
    attributes: u32,
    access: u32,
    handles: u32,
    pointers: u32,
    reserved: [u32; 10],
}
#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtQueryObject(
        handle: HANDLE,
        class: u32,
        buffer: *mut std::ffi::c_void,
        length: u32,
        returned: *mut u32,
    ) -> i32;
}
fn access(handle: HANDLE) -> Result<u32> {
    unsafe {
        let mut info: ObjectBasic = zeroed();
        let mut n = 0;
        if NtQueryObject(
            handle,
            0,
            (&mut info as *mut ObjectBasic).cast(),
            size_of::<ObjectBasic>() as u32,
            &mut n,
        ) < 0
        {
            return Err("identity_mismatch");
        }
        Ok(info.access)
    }
}
fn inherited(handle: HANDLE) -> Result<()> {
    unsafe {
        let mut flags = 0;
        if GetHandleInformation(handle, &mut flags) == 0 || flags & HANDLE_FLAG_INHERIT == 0 {
            return Err("invalid_bootstrap");
        }
        Ok(())
    }
}
fn pipe(handle: HANDLE, read: bool) -> Result<()> {
    inherited(handle)?;
    let rights = access(handle)?;
    unsafe {
        if GetFileType(handle) != FILE_TYPE_PIPE
            || rights
                & (if read {
                    FILE_READ_DATA
                } else {
                    FILE_WRITE_DATA
                })
                == 0
            || rights
                & (if read {
                    FILE_WRITE_DATA
                } else {
                    FILE_READ_DATA
                })
                != 0
        {
            return Err("invalid_bootstrap");
        }
    }
    Ok(())
}
fn alive(parent: HANDLE, lifetime: HANDLE) -> Result<()> {
    unsafe {
        if WaitForSingleObject(parent, 0) != WAIT_TIMEOUT {
            return Err("parent_lost");
        }
        let mut n = 0;
        if PeekNamedPipe(lifetime, null_mut(), 0, null_mut(), &mut n, null_mut()) == 0 {
            return Err("parent_lost");
        }
    }
    Ok(())
}
fn read_exact(
    pipe: HANDLE,
    length: usize,
    parent: HANDLE,
    lifetime: HANDLE,
    end: Instant,
) -> Result<Vec<u8>> {
    let mut bytes = vec![0u8; length];
    let mut position = 0;
    while position < length {
        alive(parent, lifetime)?;
        if Instant::now() >= end {
            return Err("timeout");
        }
        unsafe {
            let mut available = 0;
            if PeekNamedPipe(pipe, null_mut(), 0, null_mut(), &mut available, null_mut()) == 0 {
                return Err("io_failure");
            }
            if available == 0 {
                std::thread::sleep(Duration::from_millis(5));
                continue;
            }
            let count = (length - position).min(available as usize);
            let mut read = 0;
            if ReadFile(
                pipe,
                bytes[position..].as_mut_ptr(),
                count as u32,
                &mut read,
                null_mut(),
            ) == 0
                || read == 0
            {
                return Err("io_failure");
            }
            position += read as usize;
        }
    }
    Ok(bytes)
}
fn frame(pipe: HANDLE, parent: HANDLE, lifetime: HANDLE, end: Instant) -> Result<Vec<u8>> {
    let prefix = read_exact(pipe, 4, parent, lifetime, end)?;
    let n = frame_length(prefix.try_into().unwrap())?;
    read_exact(pipe, n, parent, lifetime, end)
}
fn status(
    pipe: HANDLE,
    nonce: &str,
    state: &str,
    reason: &str,
    pid: Option<u32>,
    tree_drained: bool,
) -> Result<()> {
    status_with_stage(pipe, nonce, state, reason, pid, tree_drained, None)
}
fn status_with_stage(
    pipe: HANDLE,
    nonce: &str,
    state: &str,
    reason: &str,
    pid: Option<u32>,
    tree_drained: bool,
    stage: Option<FailureStage>,
) -> Result<()> {
    let bytes = serde_json::to_vec(&status_value_with_stage(
        nonce,
        state,
        reason,
        pid,
        tree_drained,
        stage,
    )?)
    .map_err(|_| "io_failure")?;
    let mut output = (bytes.len() as u32).to_le_bytes().to_vec();
    output.extend(bytes);
    unsafe {
        let mut written = 0;
        if WriteFile(
            pipe,
            output.as_ptr(),
            output.len() as u32,
            &mut written,
            null_mut(),
        ) == 0
            || written as usize != output.len()
        {
            return Err("io_failure");
        }
    }
    Ok(())
}
fn process_package(process: HANDLE, family: bool) -> Result<String> {
    unsafe {
        let mut length = 0;
        let first = if family {
            GetPackageFamilyName(process, &mut length, null_mut())
        } else {
            GetPackageFullName(process, &mut length, null_mut())
        };
        if first != ERROR_INSUFFICIENT_BUFFER || !(2..=4096).contains(&length) {
            return Err("identity_mismatch");
        }
        let mut text = vec![0u16; length as usize];
        let code = if family {
            GetPackageFamilyName(process, &mut length, text.as_mut_ptr())
        } else {
            GetPackageFullName(process, &mut length, text.as_mut_ptr())
        };
        if code != 0 || length < 2 || length as usize > text.len() || text[length as usize - 1] != 0
        {
            return Err("identity_mismatch");
        }
        String::from_utf16(&text[..length as usize - 1]).map_err(|_| "identity_mismatch")
    }
}
fn same_path(a: &Path, b: &Path) -> bool {
    dunce::simplified(a)
        .to_string_lossy()
        .replace('/', "\\")
        .eq_ignore_ascii_case(&dunce::simplified(b).to_string_lossy().replace('/', "\\"))
}
fn parent_pid() -> Result<u32> {
    unsafe {
        let snapshot = Handle::checked(
            CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0),
            "identity_mismatch",
        )?;
        let mut entry: PROCESSENTRY32W = zeroed();
        entry.dwSize = size_of::<PROCESSENTRY32W>() as u32;
        let mut valid = Process32FirstW(snapshot.0, &mut entry);
        while valid != 0 {
            if entry.th32ProcessID == GetCurrentProcessId() {
                return Ok(entry.th32ParentProcessID);
            }
            valid = Process32NextW(snapshot.0, &mut entry)
        }
        Err("identity_mismatch")
    }
}
fn parent_identity(
    parent: HANDLE,
    outer: HANDLE,
    packages: &crate::servicing::WorkerPackages,
    stage: &mut FailureStage,
) -> Result<()> {
    unsafe {
        *stage = FailureStage::ParentAccess;
        if access(parent)? != (PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE) {
            return Err("identity_mismatch");
        }
        *stage = FailureStage::OuterAccess;
        if access(outer)? != OUTER_JOB_QUERY {
            return Err("identity_mismatch");
        }
        *stage = FailureStage::ParentProcess;
        let pid = GetProcessId(parent);
        if pid == 0 || pid != parent_pid()? || WaitForSingleObject(parent, 0) != WAIT_TIMEOUT {
            return Err("identity_mismatch");
        }
        let current = GetCurrentProcess();
        *stage = FailureStage::ParentSid;
        if crate::windows::process_sid(parent)? != crate::windows::process_sid(current)? {
            return Err("identity_mismatch");
        }
        let (mut a, mut b) = (0, 0);
        *stage = FailureStage::ParentSession;
        if ProcessIdToSessionId(pid, &mut a) == 0
            || ProcessIdToSessionId(GetCurrentProcessId(), &mut b) == 0
            || a != b
        {
            return Err("identity_mismatch");
        }
        *stage = FailureStage::ParentPackage;
        if process_package(parent, false)? != process_package(current, false)? {
            return Err("identity_mismatch");
        }
        *stage = FailureStage::ParentFamily;
        if process_package(parent, true)?
            != packages
                .main
                .identity
                .family
                .as_deref()
                .ok_or("identity_mismatch")?
        {
            return Err("identity_mismatch");
        }
        *stage = FailureStage::CurrentPackageIdentity;
        if crate::windows::package_identity()?.as_ref() != Some(&packages.main.identity) {
            return Err("identity_mismatch");
        }
        *stage = FailureStage::ParentImage;
        let image = crate::windows::process_image(parent)?;
        *stage = FailureStage::HostImage;
        let own = std::env::current_exe().map_err(|_| "identity_mismatch")?;
        *stage = FailureStage::ParentImage;
        if !same_path(&image, &packages.main_root.join("app/Brain.exe")) {
            return Err("identity_mismatch");
        }
        *stage = FailureStage::HostImage;
        if !same_path(&own, &packages.main_root.join("app/Bridge.exe")) {
            return Err("identity_mismatch");
        }
        for p in [parent, current] {
            *stage = if p == parent {
                FailureStage::ParentJob
            } else {
                FailureStage::HostJob
            };
            let mut member = 0;
            if IsProcessInJob(p, outer, &mut member) == 0 || member == 0 {
                return Err("identity_mismatch");
            }
        }
        Ok(())
    }
}
fn hash_file(file: &mut File, artifact: &Artifact) -> Result<()> {
    let m = file.metadata().map_err(|_| "artifact_mismatch")?;
    if !m.is_file()
        || m.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || m.len() != artifact.size_bytes
    {
        return Err("artifact_mismatch");
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "artifact_mismatch")?;
    let mut hash = Sha256::new();
    let mut count = 0;
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf).map_err(|_| "artifact_mismatch")?;
        if n == 0 {
            break;
        }
        count += n as u64;
        if count > artifact.size_bytes {
            return Err("artifact_mismatch");
        }
        hash.update(&buf[..n]);
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "artifact_mismatch")?;
    if count != artifact.size_bytes || format!("{:x}", hash.finalize()) != artifact.sha256 {
        return Err("artifact_mismatch");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_process_image_matches_ordinary_local_path() {
        let image = crate::windows::process_image(unsafe { GetCurrentProcess() }).unwrap();
        let ordinary = dunce::simplified(&image);
        assert_ne!(image.as_os_str(), ordinary.as_os_str());
        assert!(same_path(&image, ordinary));
        assert!(same_path(ordinary, &image));
        assert!(same_path(&image, &std::env::current_exe().unwrap()));
    }

    #[test]
    fn local_path_representations_preserve_package_and_namespace_boundaries() {
        let image =
            Path::new(r"C:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Brain.exe");
        let equivalent =
            Path::new(r"\\?\C:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Brain.exe");
        assert!(same_path(image, equivalent));
        assert!(same_path(equivalent, image));
        assert!(same_path(
            image,
            Path::new("c:/program files/windowsapps/example.main_1.0.3.0_x64/app/brain.exe")
        ));
        for other in [
            r"\\?\D:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Brain.exe",
            r"\\?\C:\Program Files\WindowsApps\Example.Main_1.0.4.0_x64\app\Brain.exe",
            r"\\?\C:\Program Files\WindowsApps\Example.Engine_1.0.3.0_x64\app\Brain.exe",
            r"\\?\C:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Bridge.exe",
            r"\\server\C$\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Brain.exe",
            r"\\?\UNC\server\C$\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Brain.exe",
            r"\\.\C:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Brain.exe",
            r"\??\C:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Brain.exe",
            r"\\?\C:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\.\Brain.exe",
            r"\\?\C:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\other\..\Brain.exe",
            r"\\?\C:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Brain.exe.",
            r"\\?\C:\Program Files\WindowsApps\Example.Main_1.0.3.0_x64\app\Brain.exe ",
        ] {
            assert!(!same_path(image, Path::new(other)), "{other}");
        }
        for (verbatim, ordinary) in [
            (r"\\?\C:\app\NUL", r"C:\app\NUL"),
            (r"\\?\C:\app\Brain.exe.", r"C:\app\Brain.exe."),
            (r"\\?\C:\app\Brain.exe ", r"C:\app\Brain.exe "),
            (
                r"\\?\C:\app\other\..\Brain.exe",
                r"C:\app\other\..\Brain.exe",
            ),
        ] {
            assert!(!same_path(Path::new(verbatim), Path::new(ordinary)));
        }
    }
}
fn data_handle(binding: &Binding) -> Result<File> {
    unsafe {
        let raw = binding.handle as HANDLE;
        inherited(raw)?;
        let rights = access(raw)?;
        if GetFileType(raw) != FILE_TYPE_DISK
            || rights & FILE_READ_DATA == 0
            || rights != FILE_GENERIC_READ
        {
            return Err("artifact_mismatch");
        }
        // Reopen by the observed handle path, denying writers, then compare file IDs.
        let original = File::from_raw_handle(raw);
        let mut name = vec![0u16; 32768];
        let n = GetFinalPathNameByHandleW(raw, name.as_mut_ptr(), name.len() as u32, 0);
        if n == 0 || n as usize >= name.len() {
            return Err("artifact_mismatch");
        }
        use std::os::windows::ffi::OsStringExt;
        let path = PathBuf::from(std::ffi::OsString::from_wide(&name[..n as usize]));
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .map_err(|_| "artifact_mismatch")?;
        let (mut a, mut b): (BY_HANDLE_FILE_INFORMATION, BY_HANDLE_FILE_INFORMATION) =
            (zeroed(), zeroed());
        if GetFileInformationByHandle(raw, &mut a) == 0
            || GetFileInformationByHandle(file.as_raw_handle(), &mut b) == 0
            || a.dwVolumeSerialNumber != b.dwVolumeSerialNumber
            || a.nFileIndexHigh != b.nFileIndexHigh
            || a.nFileIndexLow != b.nFileIndexLow
            || b.nNumberOfLinks != 1
        {
            return Err("artifact_mismatch");
        }
        drop(original);
        hash_file(&mut file, &binding.artifact)?;
        Ok(file)
    }
}
fn metadata(
    packages: &mut crate::servicing::WorkerPackages,
) -> Result<(Metadata, Vec<(Artifact, File)>)> {
    let (bytes, _) = packages.read_engine(METADATA, FRAME_LIMIT)?;
    let meta: Metadata = decode(&bytes)?;
    let m = &packages.engine.identity;
    let main = &packages.main.identity;
    for (key, expected) in [
        ("package_name", m.name.as_str()),
        (
            "package_family",
            m.family.as_deref().ok_or("identity_mismatch")?,
        ),
        ("core_package_name", main.name.as_str()),
        ("core_package_version", main.version.as_str()),
        ("publisher", m.publisher.as_str()),
        ("package_version", m.version.as_str()),
        ("related_set_id", packages.related_set_id.as_str()),
    ] {
        if meta.engine[key] != expected {
            return Err("identity_mismatch");
        }
    }
    let mut files = Vec::new();
    for entry in &meta.artifacts {
        if entry.file != WORKER && !RUNTIME_FILES.contains(&entry.file.as_str()) {
            return Err("artifact_mismatch");
        }
        if entry.artifact.size_bytes > 128 * 1024 * 1024 {
            return Err("resource_limit");
        }
        let (bytes, mut file) =
            packages.read_engine(&format!("worker/{}", entry.file), 128 * 1024 * 1024)?;
        if bytes.len() as u64 != entry.artifact.size_bytes || sha(&bytes) != entry.artifact.sha256 {
            return Err("artifact_mismatch");
        }
        file.seek(SeekFrom::Start(0))
            .map_err(|_| "artifact_mismatch")?;
        files.push((entry.artifact.clone(), file));
    }
    for entry in
        std::fs::read_dir(packages.engine_root.join("worker")).map_err(|_| "artifact_mismatch")?
    {
        let entry = entry.map_err(|_| "artifact_mismatch")?;
        let name = entry.file_name();
        let name = name.to_str().ok_or("artifact_mismatch")?;
        if entry.file_type().map_err(|_| "artifact_mismatch")?.is_dir()
            || ((name.to_ascii_lowercase().ends_with(".exe")
                || name.to_ascii_lowercase().ends_with(".dll"))
                && !meta.artifacts.iter().any(|a| a.file == name))
        {
            return Err("artifact_mismatch");
        }
    }
    Ok((meta, files))
}
struct Worker {
    process: Handle,
    job: Handle,
    pid: u32,
    joined: bool,
}
impl Worker {
    fn empty(&self) -> Result<bool> {
        unsafe {
            let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = zeroed();
            if QueryInformationJobObject(
                self.job.0,
                JobObjectBasicAccountingInformation,
                (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                null_mut(),
            ) == 0
            {
                return Err("worker_failure");
            }
            Ok(info.ActiveProcesses == 0)
        }
    }
    fn drain(&mut self) -> Result<()> {
        unsafe {
            if WaitForSingleObject(self.process.0, 5000) != WAIT_OBJECT_0 {
                return Err("timeout");
            }
            let end = Instant::now() + Duration::from_secs(5);
            while !self.empty()? {
                if Instant::now() >= end {
                    return Err("timeout");
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            self.joined = true;
            let mut code = 0;
            if GetExitCodeProcess(self.process.0, &mut code) == 0 || code != 0 {
                return Err("worker_failure");
            }
            Ok(())
        }
    }
    fn terminate_and_drain(&mut self) -> bool {
        unsafe {
            TerminateJobObject(self.job.0, 1);
            TerminateProcess(self.process.0, 1);
            if WaitForSingleObject(self.process.0, 5000) != WAIT_OBJECT_0 {
                return false;
            }
            let end = Instant::now() + Duration::from_secs(5);
            loop {
                match self.empty() {
                    Ok(true) => {
                        self.joined = true;
                        return true;
                    }
                    Ok(false) => {}
                    Err(_) => return false,
                }
                if Instant::now() >= end {
                    return false;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
        }
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        if !self.joined {
            unsafe {
                TerminateJobObject(self.job.0, 1);
                TerminateProcess(self.process.0, 1);
                WaitForSingleObject(self.process.0, 5000);
            }
        }
    }
}
fn spawn(
    packages: &crate::servicing::WorkerPackages,
    input: Handle,
    output: Handle,
    context: &[u8],
    files: &[(Artifact, File)],
    memory: u64,
    parent: HANDLE,
    outer: HANDLE,
    lifetime: HANDLE,
    tree_drained: &mut bool,
) -> Result<Worker> {
    unsafe {
        let job = Handle::checked(CreateJobObjectW(null(), null()), "launch_failure")?;
        let mut limit: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
        limit.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_JOB_MEMORY;
        limit.BasicLimitInformation.ActiveProcessLimit = 1;
        limit.ProcessMemoryLimit = usize::try_from(memory).map_err(|_| "resource_limit")?;
        limit.JobMemoryLimit = limit.ProcessMemoryLimit;
        if memory == 0
            || SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                (&limit as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) == 0
        {
            return Err("resource_limit");
        }
        let security = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: null_mut(),
            bInheritHandle: 1,
        };
        let (mut r, mut w) = (null_mut(), null_mut());
        if CreatePipe(&mut r, &mut w, &security, 65536) == 0 {
            return Err("launch_failure");
        }
        let read = Handle::checked(r, "launch_failure")?;
        let write = Handle::checked(w, "launch_failure")?;
        if SetHandleInformation(write.0, HANDLE_FLAG_INHERIT, 0) == 0 {
            return Err("launch_failure");
        }
        let nul = Handle::checked(
            CreateFileW(
                wide(OsStr::new("NUL")).as_ptr(),
                GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                &security,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                null_mut(),
            ),
            "launch_failure",
        )?;
        let mut inherited = vec![input.0, output.0, read.0, nul.0];
        for (_, file) in files {
            inherited.push(file.as_raw_handle());
        }
        for h in &inherited {
            if SetHandleInformation(*h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) == 0 {
                return Err("launch_failure");
            }
        }
        let mut attrs = Attributes::new(&mut inherited).map_err(|_| "launch_failure")?;
        let mut startup: STARTUPINFOEXW = zeroed();
        startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = input.0;
        startup.StartupInfo.hStdOutput = output.0;
        startup.StartupInfo.hStdError = nul.0;
        startup.lpAttributeList = attrs.raw();
        let image = packages.engine_root.join("worker").join(WORKER);
        let application = wide(image.as_os_str());
        let mut command = wide(OsStr::new(&format!(
            "\"{}\" --owner-context-handle {}",
            image.display(),
            read.0 as usize
        )));
        let directory = wide(image.parent().ok_or("launch_failure")?.as_os_str());
        let root = std::env::var_os("SystemRoot").ok_or("launch_failure")?;
        let values = [
            (
                "PATH",
                std::ffi::OsString::from(format!(
                    "{};{}\\System32",
                    image.parent().unwrap().display(),
                    PathBuf::from(&root).display()
                )),
            ),
            ("SystemRoot", root.clone()),
            ("WINDIR", root),
        ];
        use std::os::windows::ffi::OsStrExt;
        let mut env = Vec::new();
        for (k, v) in values {
            env.extend(OsStr::new(k).encode_wide());
            env.push(61);
            env.extend(v.encode_wide());
            env.push(0);
        }
        env.push(0);
        let mut pi: PROCESS_INFORMATION = zeroed();
        if CreateProcessW(
            application.as_ptr(),
            command.as_mut_ptr(),
            null(),
            null(),
            1,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT,
            env.as_ptr().cast(),
            directory.as_ptr(),
            &startup.StartupInfo,
            &mut pi,
        ) == 0
        {
            return Err("launch_failure");
        }
        let process = Handle::checked(pi.hProcess, "launch_failure")?;
        let thread = Handle::checked(pi.hThread, "launch_failure")?;
        let mut worker = Worker {
            process,
            job,
            pid: pi.dwProcessId,
            joined: false,
        };
        *tree_drained = false;
        let result = (|| -> Result<()> {
            for h in &inherited {
                if SetHandleInformation(*h, HANDLE_FLAG_INHERIT, 0) == 0 {
                    return Err("launch_failure");
                }
            }
            drop(input);
            drop(output);
            if AssignProcessToJobObject(worker.job.0, worker.process.0) == 0 {
                return Err("launch_failure");
            }
            for job in [worker.job.0, outer] {
                let mut member = 0;
                if IsProcessInJob(worker.process.0, job, &mut member) == 0 || member == 0 {
                    return Err("identity_mismatch");
                }
            }
            let (mut child_session, mut host_session) = (0, 0);
            if ProcessIdToSessionId(worker.pid, &mut child_session) == 0
                || ProcessIdToSessionId(GetCurrentProcessId(), &mut host_session) == 0
                || child_session != host_session
                || process_package(worker.process.0, false)?
                    != process_package(GetCurrentProcess(), false)?
                || !same_path(&crate::windows::process_image(worker.process.0)?, &image)
                || crate::windows::process_sid(worker.process.0)? != crate::windows::current_sid()?
            {
                return Err("identity_mismatch");
            }
            alive(parent, lifetime)?;
            if ResumeThread(thread.0) == u32::MAX {
                return Err("launch_failure");
            }
            drop(read);
            drop(nul);
            let mut payload = (context.len() as u32).to_le_bytes().to_vec();
            payload.extend(context);
            let mut writer = File::from_raw_handle(write.0);
            std::mem::forget(write);
            let task = std::thread::spawn(move || writer.write_all(&payload));
            let end = Instant::now() + Duration::from_secs(20);
            while !task.is_finished() {
                if Instant::now() >= end || alive(parent, lifetime).is_err() {
                    TerminateJobObject(worker.job.0, 1);
                    WaitForSingleObject(worker.process.0, 5000);
                    break;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            // Only the exact child owns the context reader; its joined exit releases a blocked writer.
            if !task.is_finished() {
                use std::os::windows::io::AsRawHandle;
                windows_sys::Win32::System::IO::CancelSynchronousIo(task.as_raw_handle());
            }
            let cancel_end = Instant::now() + Duration::from_secs(5);
            while !task.is_finished() && Instant::now() < cancel_end {
                std::thread::sleep(Duration::from_millis(5));
            }
            if !task.is_finished() {
                // No successful status can outlive an unjoined context writer.
                worker.terminate_and_drain();
                std::process::abort();
            }
            task.join()
                .map_err(|_| "io_failure")?
                .map_err(|_| "io_failure")?;
            alive(parent, lifetime)?;
            if WaitForSingleObject(worker.process.0, 0) != WAIT_TIMEOUT {
                return Err("worker_failure");
            }
            Ok(())
        })();
        if let Err(reason) = result {
            *tree_drained = worker.terminate_and_drain();
            return Err(reason);
        }
        Ok(worker)
    }
}

pub fn run(args: &[String]) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(180);
    let (raw, nonce) = arguments(args)?;
    for h in raw {
        inherited(h as HANDLE)?;
    }
    let [parent, outer, command, status_pipe, input, output, lifetime] = raw
        .map(|h| Handle::checked(h as HANDLE, "invalid_bootstrap"))
        .map(|h| h.unwrap());
    for (h, read) in [
        (command.0, true),
        (status_pipe.0, false),
        (input.0, true),
        (output.0, false),
        (lifetime.0, true),
    ] {
        pipe(h, read)?;
    }
    for handle in raw {
        unsafe {
            if SetHandleInformation(handle as HANDLE, HANDLE_FLAG_INHERIT, 0) == 0 {
                return Err("invalid_bootstrap");
            }
        }
    }
    let mut worker_pid = None;
    let mut worker: Option<Worker> = None;
    let mut tree_drained = true;
    let mut held_packages: Option<crate::servicing::WorkerPackages> = None;
    let mut held_files = Vec::new();
    let mut stage = FailureStage::ParentAccess;
    let result = (|| {
        // Authenticate the direct parent before optional-package enumeration.
        unsafe {
            if access(parent.0)? != (PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE) {
                return Err("identity_mismatch");
            }
            stage = FailureStage::OuterAccess;
            if access(outer.0)? != OUTER_JOB_QUERY {
                return Err("identity_mismatch");
            }
            stage = FailureStage::ParentProcess;
            if GetProcessId(parent.0) != parent_pid()? {
                return Err("identity_mismatch");
            }
            alive(parent.0, lifetime.0)?;
            let current = GetCurrentProcess();
            stage = FailureStage::CurrentPackage;
            let full = process_package(current, false)?;
            stage = FailureStage::ParentPackage;
            if process_package(parent.0, false)? != full {
                return Err("identity_mismatch");
            }
            stage = FailureStage::ParentSid;
            if crate::windows::process_sid(parent.0)? != crate::windows::current_sid()? {
                return Err("identity_mismatch");
            }
            let (mut a, mut b) = (0, 0);
            stage = FailureStage::ParentSession;
            if ProcessIdToSessionId(GetProcessId(parent.0), &mut a) == 0
                || ProcessIdToSessionId(GetCurrentProcessId(), &mut b) == 0
                || a != b
            {
                return Err("identity_mismatch");
            }
            for process in [parent.0, current] {
                stage = if process == parent.0 {
                    FailureStage::ParentJob
                } else {
                    FailureStage::HostJob
                };
                let mut member = 0;
                if IsProcessInJob(process, outer.0, &mut member) == 0 || member == 0 {
                    return Err("identity_mismatch");
                }
            }
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
            stage = FailureStage::OuterLimits;
            if QueryInformationJobObject(
                outer.0,
                JobObjectExtendedLimitInformation,
                (&mut limits as *mut JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                null_mut(),
            ) == 0
                || limits.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0
                || limits.BasicLimitInformation.LimitFlags
                    & (JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)
                    != 0
            {
                return Err("identity_mismatch");
            }
            stage = FailureStage::MainObservation;
            let (observed, root) = crate::servicing::installed(&full)?;
            stage = FailureStage::MainTrust;
            if observed.origin != crate::servicing::Origin::SignedDirect
                || observed.signature != crate::servicing::Signature::SignedDirect
            {
                return Err("identity_mismatch");
            }
            stage = FailureStage::ParentImage;
            if !same_path(
                &crate::windows::process_image(parent.0)?,
                &Path::new(&root).join("app/Brain.exe"),
            ) {
                return Err("identity_mismatch");
            }
            stage = FailureStage::HostImage;
            if !same_path(
                &std::env::current_exe().map_err(|_| "identity_mismatch")?,
                &Path::new(&root).join("app/Bridge.exe"),
            ) {
                return Err("identity_mismatch");
            }
        }
        let mut packages = crate::servicing::worker_packages(&mut stage)?;
        parent_identity(parent.0, outer.0, &packages, &mut stage)?;
        stage = FailureStage::MainFiles;
        packages.read_main("app/Brain.exe", 128 * 1024 * 1024)?;
        packages.read_main("app/Bridge.exe", 128 * 1024 * 1024)?;
        stage = FailureStage::InitialCommand;
        let initial = frame(
            command.0,
            parent.0,
            lifetime.0,
            deadline.min(Instant::now() + Duration::from_secs(20)),
        )?;
        let tag: Value = decode(&initial)?;
        stage = FailureStage::WorkerMetadata;
        let (meta, mut retained) = metadata(&mut packages)?;
        if Instant::now() >= deadline {
            return Err("timeout");
        }
        alive(parent.0, lifetime.0)?;
        if tag["schema"] == "nlp-host-observe.v1" {
            stage = FailureStage::ObservationBinding;
            observe(&initial, &nonce, &meta.engine)?;
            let synthetic = serde_json::json!({"artifacts":meta.artifacts.iter().map(|a|serde_json::to_value(&a.artifact).unwrap()).collect::<Vec<_>>(),"components":[{"engine":meta.engine,"adapter":meta.adapters.first().ok_or("artifact_mismatch")?["adapter"]}]});
            stage = FailureStage::MetadataBinding;
            validate_metadata(&meta, &synthetic)?;
            drop(input);
            drop(output);
            status(status_pipe.0, &nonce, "ready", "none", None, true)?;
            stage = FailureStage::WorkerControl;
            control(
                &frame(
                    command.0,
                    parent.0,
                    lifetime.0,
                    deadline.min(Instant::now() + Duration::from_secs(20)),
                )?,
                &nonce,
                "close",
            )?;
            status(status_pipe.0, &nonce, "drained", "none", None, true)?;
            return Ok(());
        }
        stage = FailureStage::InitialCommand;
        let launch: Launch = decode(&initial)?;
        let profile = profile(&launch, &nonce, &raw)?;
        stage = FailureStage::MetadataBinding;
        validate_metadata(&meta, &profile)?;
        stage = FailureStage::ArtifactBindings;
        for binding in &launch.artifacts {
            retained.push((binding.artifact.clone(), data_handle(binding)?));
        }
        let mut bindings = Vec::new();
        for item in profile["artifacts"].as_array().ok_or("invalid_bootstrap")? {
            let a: Artifact =
                serde_json::from_value(item.clone()).map_err(|_| "invalid_bootstrap")?;
            let f = retained
                .iter()
                .find(|(v, _)| v == &a)
                .ok_or("artifact_mismatch")?;
            bindings.push(
                serde_json::json!({"artifact":a,"handle":f.1.as_raw_handle()as usize as u64}),
            );
        }
        let context=serde_json::to_vec(&serde_json::json!({"schema":"nlp-owner-context.v1","channel_nonce":nonce,"profile_json":launch.profile_json,"engine":meta.engine,"adapters":meta.adapters,"artifacts":bindings})).map_err(|_|"invalid_bootstrap")?;
        if context.len() > FRAME_LIMIT {
            return Err("resource_limit");
        }
        let memory = profile["resources"]["transition_peak_memory_bytes"]
            .as_u64()
            .filter(|v| *v > 0)
            .ok_or("resource_limit")?;
        held_packages = Some(packages);
        held_files = retained;
        if Instant::now() >= deadline {
            return Err("timeout");
        }
        alive(parent.0, lifetime.0)?;
        status(status_pipe.0, &nonce, "ready", "none", None, true)?;
        stage = FailureStage::WorkerControl;
        control(
            &frame(
                command.0,
                parent.0,
                lifetime.0,
                deadline.min(Instant::now() + Duration::from_secs(20)),
            )?,
            &nonce,
            "commit",
        )?;
        stage = FailureStage::WorkerLaunch;
        worker = Some(spawn(
            held_packages.as_ref().unwrap(),
            input,
            output,
            &context,
            &held_files,
            memory,
            parent.0,
            outer.0,
            lifetime.0,
            &mut tree_drained,
        )?);
        let child = worker.as_mut().unwrap();
        worker_pid = Some(child.pid);
        status(status_pipe.0, &nonce, "running", "none", worker_pid, false)?;
        stage = FailureStage::WorkerControl;
        control(
            &frame(command.0, parent.0, lifetime.0, deadline)?,
            &nonce,
            "close",
        )?;
        stage = FailureStage::WorkerDrain;
        let settled = child.drain();
        tree_drained = child.joined;
        settled?;
        status(status_pipe.0, &nonce, "drained", "none", worker_pid, true)?;
        Ok(())
    })();
    if let Some(child) = worker.as_mut() {
        if !tree_drained {
            tree_drained = child.terminate_and_drain();
        }
    }
    if let Err(reason) = result {
        let reason = match reason {
            "invalid_bootstrap" | "identity_mismatch" | "artifact_mismatch" | "resource_limit"
            | "launch_failure" | "io_failure" | "parent_lost" | "timeout" | "worker_failure" => {
                reason
            }
            _ => "identity_mismatch",
        };
        let _ = status_with_stage(
            status_pipe.0,
            &nonce,
            "failed",
            reason,
            worker_pid,
            tree_drained,
            Some(stage),
        );
        return Err(reason);
    }
    Ok(())
}
