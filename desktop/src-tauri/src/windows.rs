//! Native handles never cross the renderer boundary.
use crate::policy::{Listener, PORT};
use std::{
    ffi::OsString,
    mem::{size_of, zeroed},
    net::{IpAddr, Ipv4Addr, Ipv6Addr},
    os::windows::ffi::{OsStrExt, OsStringExt},
    path::{Path, PathBuf},
    ptr::{null, null_mut},
};
use windows_sys::Win32::{
    Foundation::*,
    NetworkManagement::IpHelper::*,
    Networking::WinSock::{AF_INET, AF_INET6},
    Security::*,
    Storage::FileSystem::*,
    System::{JobObjects::*, Pipes::CreatePipe, Threading::*},
    UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowThreadProcessId},
};

type Result<T> = std::result::Result<T, &'static str>;

pub struct Handle(HANDLE);
// Kernel handles are process-wide. Ownership still has exactly one Drop.
unsafe impl Send for Handle {}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
impl Handle {
    fn checked(raw: HANDLE, error: &'static str) -> Result<Self> {
        if raw.is_null() || raw == INVALID_HANDLE_VALUE {
            Err(error)
        } else {
            Ok(Self(raw))
        }
    }
}

fn wide(value: &std::ffi::OsStr) -> Vec<u16> {
    value.encode_wide().chain(Some(0)).collect()
}

fn process_sid(process: HANDLE) -> Result<Vec<u8>> {
    unsafe {
        let mut raw = null_mut();
        if OpenProcessToken(process, TOKEN_QUERY, &mut raw) == 0 {
            return Err("owner_inspection_failed");
        }
        let token = Handle::checked(raw, "owner_inspection_failed")?;
        let mut size = 0;
        GetTokenInformation(token.0, TokenUser, null_mut(), 0, &mut size);
        if size == 0 || size > 65536 {
            return Err("owner_inspection_failed");
        }
        let mut buffer = vec![0usize; (size as usize).div_ceil(size_of::<usize>())];
        if GetTokenInformation(
            token.0,
            TokenUser,
            buffer.as_mut_ptr().cast(),
            size,
            &mut size,
        ) == 0
        {
            return Err("owner_inspection_failed");
        }
        let user = &*buffer.as_ptr().cast::<TOKEN_USER>();
        if IsValidSid(user.User.Sid) == 0 {
            return Err("owner_inspection_failed");
        }
        Ok(std::slice::from_raw_parts(
            user.User.Sid.cast::<u8>(),
            GetLengthSid(user.User.Sid) as usize,
        )
        .to_vec())
    }
}

pub fn current_sid() -> Result<Vec<u8>> {
    unsafe { process_sid(GetCurrentProcess()) }
}

fn process_image(process: HANDLE) -> Result<PathBuf> {
    let mut path = vec![0u16; 32768];
    let mut size = path.len() as u32;
    if unsafe { QueryFullProcessImageNameW(process, 0, path.as_mut_ptr(), &mut size) } == 0 {
        return Err("owner_inspection_failed");
    }
    PathBuf::from(OsString::from_wide(&path[..size as usize]))
        .canonicalize()
        .map_err(|_| "owner_inspection_failed")
}

/// Automatic browser return must not interrupt another foreground application.
pub fn foreground_matches(executable: &Path) -> bool {
    let inspect = || -> Result<bool> {
        let mut pid = 0;
        let foreground = unsafe { GetForegroundWindow() };
        if foreground.is_null() || unsafe { GetWindowThreadProcessId(foreground, &mut pid) } == 0 {
            return Ok(false);
        }
        let process = Handle::checked(
            unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) },
            "owner_inspection_failed",
        )?;
        Ok(process_image(process.0)?
            == executable
                .canonicalize()
                .map_err(|_| "owner_inspection_failed")?
            && process_sid(process.0)? == current_sid()?
            && unsafe { GetForegroundWindow() } == foreground)
    };
    inspect().unwrap_or(false)
}

fn listener(address: IpAddr, pid: u32) -> Result<Listener> {
    unsafe {
        let process = Handle::checked(
            OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid),
            "owner_inspection_failed",
        )?;
        let image = process_image(process.0)?;
        Ok(Listener {
            address,
            pid,
            image,
            sid: process_sid(process.0)?,
        })
    }
}

/// Both address families are inspected; wildcard and extra listeners fail admission.
pub fn listeners() -> Result<Vec<Listener>> {
    let mut result = Vec::new();
    for family in [AF_INET, AF_INET6] {
        unsafe {
            let mut size = 0;
            let initial = GetExtendedTcpTable(
                null_mut(),
                &mut size,
                0,
                family as u32,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            );
            if initial != ERROR_INSUFFICIENT_BUFFER || !(4..=16 * 1024 * 1024).contains(&size) {
                return Err("port_inspection_failed");
            }
            let mut table = vec![0u32; (size as usize).div_ceil(4)];
            let status = GetExtendedTcpTable(
                table.as_mut_ptr().cast(),
                &mut size,
                0,
                family as u32,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            );
            if status != NO_ERROR {
                return Err("port_inspection_failed");
            }
            let count = table[0] as usize;
            let row_size = if family == AF_INET {
                size_of::<MIB_TCPROW_OWNER_PID>()
            } else {
                size_of::<MIB_TCP6ROW_OWNER_PID>()
            };
            if count > (size as usize - 4) / row_size {
                return Err("port_inspection_failed");
            }
            for index in 0..count {
                let raw = table.as_ptr().cast::<u8>().add(4 + index * row_size);
                let (port, address, pid) = if family == AF_INET {
                    let row = std::ptr::read_unaligned(raw.cast::<MIB_TCPROW_OWNER_PID>());
                    (
                        row.dwLocalPort,
                        IpAddr::V4(Ipv4Addr::from(row.dwLocalAddr.to_ne_bytes())),
                        row.dwOwningPid,
                    )
                } else {
                    let row = std::ptr::read_unaligned(raw.cast::<MIB_TCP6ROW_OWNER_PID>());
                    (
                        row.dwLocalPort,
                        IpAddr::V6(Ipv6Addr::from(row.ucLocalAddr)),
                        row.dwOwningPid,
                    )
                };
                if u16::from_be(port as u16) == PORT {
                    result.push(listener(address, pid)?);
                }
            }
        }
    }
    Ok(result)
}

struct Attributes {
    storage: Vec<usize>,
    initialized: bool,
}
impl Attributes {
    fn new(handles: &mut [HANDLE]) -> Result<Self> {
        unsafe {
            let mut size = 0;
            InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut size);
            let mut this = Self {
                storage: vec![0; size.div_ceil(size_of::<usize>())],
                initialized: false,
            };
            if InitializeProcThreadAttributeList(this.raw(), 1, 0, &mut size) == 0 {
                return Err("child_start_failed");
            }
            this.initialized = true;
            if UpdateProcThreadAttribute(
                this.raw(),
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_ptr().cast(),
                std::mem::size_of_val(handles),
                null_mut(),
                null(),
            ) == 0
            {
                return Err("child_start_failed");
            }
            Ok(this)
        }
    }
    fn raw(&mut self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
        self.storage.as_mut_ptr().cast()
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

pub fn application_mutex() -> Result<Handle> {
    let name = wide(std::ffi::OsStr::new("OnlyFansConversationalAnalyticsBrain"));
    unsafe {
        Handle::checked(
            CreateMutexW(null(), 0, name.as_ptr()),
            "application_mutex_unavailable",
        )
    }
}

pub struct Brain {
    process: Handle,
    job: Handle,
    lifetime: Option<Handle>,
    pub pid: u32,
}

impl Brain {
    pub fn start(image: &Path, data: &Path, handoff: &str) -> Result<Self> {
        unsafe {
            let job = Handle::checked(CreateJobObjectW(null(), null()), "child_start_failed")?;
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of_val(&limits) as u32,
            ) == 0
            {
                return Err("child_start_failed");
            }
            let security = SECURITY_ATTRIBUTES {
                nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: null_mut(),
                bInheritHandle: 1,
            };
            let (mut read, mut write) = (null_mut(), null_mut());
            if CreatePipe(&mut read, &mut write, &security, 0) == 0 {
                return Err("child_start_failed");
            }
            let read = Handle::checked(read, "child_start_failed")?;
            let write = Handle::checked(write, "child_start_failed")?;
            if SetHandleInformation(write.0, HANDLE_FLAG_INHERIT, 0) == 0 {
                return Err("child_start_failed");
            }
            // A windowless Python process still needs valid standard handles.
            // Inherit only this null device and the separate lifetime pipe.
            let null_device = Handle::checked(
                CreateFileW(
                    wide(std::ffi::OsStr::new("NUL")).as_ptr(),
                    GENERIC_READ | GENERIC_WRITE,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    &security,
                    OPEN_EXISTING,
                    FILE_ATTRIBUTE_NORMAL,
                    null_mut(),
                ),
                "child_start_failed",
            )?;
            let mut inherited = [read.0, null_device.0];
            let mut attributes = Attributes::new(&mut inherited)?;
            let mut startup: STARTUPINFOEXW = zeroed();
            startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
            startup.StartupInfo.hStdInput = null_device.0;
            startup.StartupInfo.hStdOutput = null_device.0;
            startup.StartupInfo.hStdError = null_device.0;
            startup.lpAttributeList = attributes.raw();
            let mut environment: std::collections::BTreeMap<OsString, OsString> =
                std::env::vars_os()
                    .filter(|(key, _)| {
                        !matches!(
                            key.to_string_lossy().to_ascii_uppercase().as_str(),
                            "PYTHONPATH"
                                | "PYTHONHOME"
                                | "LOCAL_ANALYTICS_DATA_DIR"
                                | "BRAIN_PARENT_LIFETIME_HANDLE"
                                | "LOCAL_PROVISIONING_HANDOFF_TOKEN"
                                | "LOCAL_PROVISIONING_EXTENSION_ID"
                        )
                    })
                    .collect();
            environment.insert(
                "LOCAL_ANALYTICS_DATA_DIR".into(),
                data.as_os_str().to_owned(),
            );
            environment.insert(
                "BRAIN_PARENT_LIFETIME_HANDLE".into(),
                (read.0 as usize).to_string().into(),
            );
            environment.insert("LOCAL_PROVISIONING_HANDOFF_TOKEN".into(), handoff.into());
            environment.insert(
                "LOCAL_PROVISIONING_EXTENSION_ID".into(),
                env!("BRIDGE_EXTENSION_ID").into(),
            );
            let mut environment_wide = Vec::new();
            for (key, value) in environment {
                environment_wide.extend(key.encode_wide());
                environment_wide.push(b'=' as u16);
                environment_wide.extend(value.encode_wide());
                environment_wide.push(0);
            }
            environment_wide.push(0);
            // Keep canonical paths for owner checks, but launch with the equivalent
            // ordinary path when possible. Frozen Python's template loader joins
            // resource names with '/', which verbatim Windows paths prohibit.
            let launch_image = dunce::simplified(image);
            let application = wide(launch_image.as_os_str());
            let mut command =
                wide(OsString::from(format!("\"{}\" --brain", launch_image.display())).as_os_str());
            let directory = wide(
                launch_image
                    .parent()
                    .ok_or("invalid_installation")?
                    .as_os_str(),
            );
            let mut process: PROCESS_INFORMATION = zeroed();
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
                environment_wide.as_ptr().cast(),
                directory.as_ptr(),
                &startup.StartupInfo,
                &mut process,
            ) == 0
            {
                return Err("child_start_failed");
            }
            let process_handle = Handle::checked(process.hProcess, "child_start_failed")?;
            let thread = Handle::checked(process.hThread, "child_start_failed")?;
            // The child cannot execute until containment has succeeded.
            if AssignProcessToJobObject(job.0, process_handle.0) == 0
                || ResumeThread(thread.0) == u32::MAX
            {
                TerminateProcess(process_handle.0, 1);
                WaitForSingleObject(process_handle.0, 5000);
                return Err("child_start_failed");
            }
            Ok(Self {
                process: process_handle,
                job,
                lifetime: Some(write),
                pid: process.dwProcessId,
            })
        }
    }
    pub fn exit_code(&self) -> Result<Option<u32>> {
        unsafe {
            match WaitForSingleObject(self.process.0, 0) {
                WAIT_TIMEOUT => Ok(None),
                WAIT_OBJECT_0 => {
                    let mut code = 0;
                    if GetExitCodeProcess(self.process.0, &mut code) == 0 {
                        return Err("child_inspection_failed");
                    }
                    Ok(Some(code))
                }
                _ => Err("child_inspection_failed"),
            }
        }
    }
    /// Observe only this owned process; a missing listener never establishes exit.
    /// None is a deadline or Quit, and must never authorize a replacement.
    pub fn wait_for_exit(
        &self,
        timeout: std::time::Duration,
        quitting: &std::sync::atomic::AtomicBool,
    ) -> Result<Option<u32>> {
        use std::sync::atomic::Ordering;
        let deadline = std::time::Instant::now() + timeout;
        loop {
            if quitting.load(Ordering::SeqCst) {
                return Ok(None);
            }
            if let Some(code) = self.exit_code()? {
                return Ok(Some(code));
            }
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                return Ok(None);
            }
            std::thread::sleep(remaining.min(std::time::Duration::from_millis(100)));
        }
    }
    pub fn stop(&mut self) {
        self.lifetime.take();
        unsafe {
            if WaitForSingleObject(self.process.0, 15000) != WAIT_OBJECT_0 {
                TerminateJobObject(self.job.0, 1);
                WaitForSingleObject(self.process.0, 5000);
            }
        }
    }
}
impl Drop for Brain {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_owner_inspection_finds_real_ipv4_and_ipv6_listeners() {
        // A serial, fixed-port test exercises the OS table decoder and owner identity.
        let v4 = std::net::TcpListener::bind(("127.0.0.1", PORT))
            .expect("qualification port must be free");
        let v6 = std::net::TcpListener::bind(("::1", PORT)).expect("IPv6 must be available");
        let rows = listeners().unwrap();
        let image = std::env::current_exe().unwrap().canonicalize().unwrap();
        let sid = current_sid().unwrap();
        assert!(rows
            .iter()
            .any(|r| r.address == "127.0.0.1".parse::<IpAddr>().unwrap()
                && r.pid == std::process::id()
                && r.image == image
                && r.sid == sid));
        assert!(rows
            .iter()
            .any(|r| r.address == "::1".parse::<IpAddr>().unwrap() && r.pid == std::process::id()));
        drop((v4, v6));
    }
}
