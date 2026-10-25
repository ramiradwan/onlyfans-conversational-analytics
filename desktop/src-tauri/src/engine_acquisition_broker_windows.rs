use super::*;
use crate::{deployment, policy, servicing, windows as native};
use servicing::family_exclusion::{self as exclusion, Handle, Lease, Security};
use sha2::Digest;
use std::{
    ffi::OsStr,
    mem::{size_of, size_of_val, zeroed},
    ptr::{null, null_mut},
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::*,
    Storage::{FileSystem::*, Packaging::Appx::*},
    System::{JobObjects::*, Pipes::*, Threading::*},
};
struct Context {
    peer: Peer,
    family: String,
    binding: String,
    data: PathBuf,
    release: deployment::DesktopRelease,
}
fn package(process: HANDLE) -> Result<String> {
    let mut n = 0;
    if unsafe { GetPackageFullName(process, &mut n, null_mut()) } != ERROR_INSUFFICIENT_BUFFER
        || !(2..=256).contains(&n)
    {
        return Err("optional_broker_package");
    }
    let mut b = vec![0u16; n as usize];
    if unsafe { GetPackageFullName(process, &mut n, b.as_mut_ptr()) } != 0
        || n < 2
        || n as usize > b.len()
        || b[n as usize - 1] != 0
    {
        return Err("optional_broker_package");
    }
    String::from_utf16(&b[..n as usize - 1]).map_err(|_| "optional_broker_package")
}
fn birth(process: HANDLE) -> Result<u64> {
    let (mut c, mut e, mut k, mut u) = unsafe { (zeroed(), zeroed(), zeroed(), zeroed()) };
    if unsafe { GetProcessTimes(process, &mut c, &mut e, &mut k, &mut u) } == 0
        || unsafe { WaitForSingleObject(process, 0) } != WAIT_TIMEOUT
    {
        return Err("optional_broker_peer");
    }
    Ok(((c.dwHighDateTime as u64) << 32) | c.dwLowDateTime as u64)
}
fn observe_peer(process: HANDLE, pid: u32) -> Result<Peer> {
    Ok(Peer {
        pid,
        birth: birth(process)?,
        image: native::process_image(process)?,
        package: package(process)?,
        logon: exclusion::logon(process)?,
    })
}
fn context() -> Result<Context> {
    let me = unsafe { GetCurrentProcess() };
    let peer = observe_peer(me, unsafe { GetCurrentProcessId() })?;
    let mut n = 0;
    if unsafe { GetCurrentPackagePath(&mut n, null_mut()) } != ERROR_INSUFFICIENT_BUFFER
        || !(2..=32768).contains(&n)
    {
        return Err("optional_broker_package");
    }
    let mut b = vec![0u16; n as usize];
    if unsafe { GetCurrentPackagePath(&mut n, b.as_mut_ptr()) } != 0
        || n < 2
        || n as usize > b.len()
    {
        return Err("optional_broker_package");
    }
    let root = PathBuf::from(
        String::from_utf16(&b[..n as usize - 1]).map_err(|_| "optional_broker_package")?,
    );
    if peer.image
        != root
            .join("app/Bridge.exe")
            .canonicalize()
            .map_err(|_| "optional_broker_package")?
    {
        return Err("optional_broker_package");
    }
    let identity = native::package_identity()?.ok_or("optional_broker_package")?;
    let release = deployment::load(
        &peer
            .image
            .parent()
            .ok_or("optional_broker_package")?
            .join("desktop-release.json"),
        Some(&identity),
    )?
    .ok_or("optional_direct_release_required")?;
    if release.update_owner != "direct-appinstaller"
        || !matches!(
            release.servicing.as_ref().map(|s| &s.installed_artifact),
            Some(deployment::InstalledArtifact::RelatedBundle { .. })
        )
    {
        return Err("optional_direct_release_required");
    }
    let data = policy::data_directory(&peer.image.parent().unwrap().join("Brain.exe"))?;
    let engine = match &release.servicing.as_ref().unwrap().installed_artifact {
        deployment::InstalledArtifact::RelatedBundle { engine, .. } => engine,
        _ => return Err("optional_direct_release_required"),
    };
    let binding_bytes = serde_json::to_vec(&(
        &peer.package,
        &peer.image,
        &data,
        &release.purpose,
        &release.identity.name,
        &release.identity.publisher,
        &release.app_installer_uri,
        &engine.identity.name,
        &engine.identity.publisher,
        &engine.identity.family,
        &engine.package_version,
    ))
    .map_err(|_| "optional_broker_package")?;
    let binding = format!("{:x}", sha2::Sha256::digest(binding_bytes));
    Ok(Context {
        peer,
        family: identity.family.ok_or("optional_broker_package")?,
        binding,
        data,
        release,
    })
}
fn validate_peer(pid: u32, claim: &Peer, ctx: &Context) -> Result<Handle> {
    let h = Handle::new(unsafe {
        OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid)
    })?;
    let actual = observe_peer(h.0, pid)?;
    if !same_peer(claim, &actual, &ctx.peer)
        || exclusion::user(h.0)? != exclusion::user(unsafe { GetCurrentProcess() })?
    {
        return Err("optional_broker_peer");
    }
    Ok(h)
}
fn load(ctx: &Context) -> Result<Option<Journal>> {
    let j = crate::servicing_handoff::load_engine_reconnect(&ctx.data)?
        .map(|b| Journal::decode(&b))
        .transpose()?;
    if let Some(j) = j.as_ref() {
        if !current_journal(j, &ctx.binding, &ctx.peer)? {
            return Ok(None);
        }
    }
    Ok(j)
}
fn pipe_name(ctx: &Context, j: &Journal) -> Result<Vec<u16>> {
    Ok(exclusion::wide(OsStr::new(&format!(
        r"\\.\pipe\ConversationAnalytics.{}.{}",
        exclusion::key(&ctx.family)?,
        j.nonce
    ))))
}
fn read_frame(h: HANDLE, end: Instant) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    let mut wanted = 4;
    while bytes.len() < wanted {
        if Instant::now() >= end {
            return Err("optional_broker_timeout");
        }
        let mut available = 0;
        if unsafe { PeekNamedPipe(h, null_mut(), 0, null_mut(), &mut available, null_mut()) } == 0 {
            return Err("optional_broker_pipe");
        }
        if available == 0 {
            std::thread::sleep(Duration::from_millis(5));
            continue;
        }
        let mut b = vec![0u8; (wanted - bytes.len()).min(available as usize)];
        let mut n = 0;
        if unsafe { ReadFile(h, b.as_mut_ptr(), b.len() as u32, &mut n, null_mut()) } == 0 || n == 0
        {
            return Err("optional_broker_pipe");
        }
        bytes.extend_from_slice(&b[..n as usize]);
        if bytes.len() == 4 && wanted == 4 {
            let size = u32::from_le_bytes(bytes[..4].try_into().unwrap()) as usize;
            if size == 0 || size > MAX_FRAME {
                return Err("optional_broker_frame");
            }
            wanted = 4 + size;
        }
    }
    Ok(bytes.split_off(4))
}
fn write_frame(h: HANDLE, raw: &[u8]) -> Result<()> {
    if raw.is_empty() || raw.len() > MAX_FRAME {
        return Err("optional_broker_frame");
    }
    let mut b = (raw.len() as u32).to_le_bytes().to_vec();
    b.extend_from_slice(raw);
    let mut n = 0;
    if unsafe { WriteFile(h, b.as_ptr(), b.len() as u32, &mut n, null_mut()) } == 0
        || n as usize != b.len()
    {
        return Err("optional_broker_pipe");
    }
    Ok(())
}
fn request(
    ctx: &Context,
    j: &Journal,
    action: Action,
    commit_allowed: Option<&dyn Fn() -> bool>,
) -> Result<Status> {
    let h = Handle::new(unsafe {
        CreateFileW(
            pipe_name(ctx, j)?.as_ptr(),
            GENERIC_READ | GENERIC_WRITE,
            0,
            null(),
            OPEN_EXISTING,
            0,
            null_mut(),
        )
    })?;
    let mut mode = PIPE_READMODE_BYTE | PIPE_NOWAIT;
    if unsafe { SetNamedPipeHandleState(h.0, &mut mode, null_mut(), null_mut()) } == 0 {
        return Err("optional_broker_pipe");
    }
    let mut pid = 0;
    if unsafe { GetNamedPipeServerProcessId(h.0, &mut pid) } == 0 || pid != j.broker.pid {
        return Err("optional_broker_peer");
    }
    let _peer = validate_peer(pid, &j.broker, ctx)?;
    let committing = matches!(action, Action::Commit {});
    let raw = serde_json::to_vec(&Request {
        nonce: j.nonce.clone(),
        peer: ctx.peer.clone(),
        action,
    })
    .map_err(|_| "optional_broker_frame")?;
    if committing && !commit_allowed.is_some_and(|check| check()) {
        return Err("optional_cancelled");
    }
    write_frame(h.0, &raw)?;
    let answer = Journal::decode(&read_frame(h.0, Instant::now() + Duration::from_secs(3))?)?;
    if answer.nonce != j.nonce
        || answer.binding != j.binding
        || answer.broker != j.broker
        || (committing && !answer.committed)
    {
        return Err("optional_broker_peer");
    }
    Ok(answer.status)
}
fn installed_status(ctx: Context, mut status: Status) -> Status {
    if status.installed {
        let verified = std::thread::spawn(move || {
            let _a = servicing::Apartment::new().map_err(|_| "optional_not_registered")?;
            let expected = crate::engine_acquisition::platform::declared(Some(&ctx.release))
                .map_err(|_| "optional_not_registered")?;
            crate::engine_acquisition::platform::installed(&ctx.release, expected)
                .map_err(|_| "optional_not_registered")
        })
        .join();
        if !matches!(verified, Ok(Ok(true))) {
            status.installed = false;
            status.state = Phase::RecoveryPending;
            status.reason = Some("optional_not_registered".into());
        }
    }
    status
}
pub(super) fn status(data: &Path) -> Result<Status> {
    // Passive startup/status never creates a broker, lease or network operation.
    let ctx = match context() {
        Ok(ctx) => ctx,
        Err(error) => {
            if crate::servicing_handoff::load_engine_reconnect(data)?.is_none() {
                return Ok(Owner::new_local(data.to_owned()).status());
            }
            return Err(error);
        }
    };
    if ctx.data != data {
        return Err("optional_broker_root");
    }
    let Some(j) = load(&ctx)? else {
        return Ok(Owner::new_local(data.to_owned()).status());
    };
    if j.terminal {
        return Ok(installed_status(ctx, j.status));
    }
    match request(&ctx, &j, Action::Status {}, None) {
        Ok(status) => Ok(installed_status(ctx, status)),
        Err(_) => Ok(unknown(Some(j.nonce), "optional_recovery_pending")),
    }
}
pub(super) fn cancel(data: &Path, id: &str) -> Result<Status> {
    let ctx = context()?;
    if ctx.data != data {
        return Err("optional_broker_root");
    }
    let j = load(&ctx)?.ok_or("optional_operation_unknown")?;
    if id != j.nonce {
        return Err("optional_operation_unknown");
    }
    if j.terminal {
        return Ok(installed_status(ctx, j.status));
    }
    request(
        &ctx,
        &j,
        Action::Cancel {
            operation_id: id.into(),
        },
        None,
    )
}
struct BootstrapChild {
    process: Handle,
    _thread: Handle,
    commit_may_have_been_sent: bool,
}
impl Drop for BootstrapChild {
    fn drop(&mut self) {
        if !self.commit_may_have_been_sent {
            unsafe {
                TerminateProcess(self.process.0, 1);
                WaitForSingleObject(self.process.0, INFINITE);
            }
        }
    }
}
pub(super) fn start(
    data: &Path,
    selection: Selection,
    cancelled: &impl Fn() -> bool,
) -> Result<Status> {
    let ctx = context()?;
    if ctx.data != data {
        return Err("optional_broker_root");
    }
    let lease = Lease::acquire(&ctx.family)?;
    crate::servicing_handoff::admitted(data)?;
    if let Some(j) = load(&ctx)? {
        if !j.terminal || j.status.state == Phase::RecoveryPending {
            return Err("optional_recovery_pending");
        }
    }
    if Owner::new_local(data.to_owned()).busy() {
        return Err("optional_recovery_pending");
    }
    let mut random = [0u8; 16];
    getrandom::fill(&mut random).map_err(|_| "optional_random_unavailable")?;
    let nonce = random
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    let inherited = lease.duplicate()?;
    // Only this exact lease handle may be inherited. No parent-lifetime handle.
    let mut handles = [inherited.0];
    let mut bytes = 0;
    unsafe {
        InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut bytes);
    }
    if bytes == 0 || bytes > 65536 {
        return Err("optional_broker_launch");
    }
    let mut storage = vec![0usize; bytes.div_ceil(size_of::<usize>())];
    let attrs = storage.as_mut_ptr().cast();
    if unsafe { InitializeProcThreadAttributeList(attrs, 1, 0, &mut bytes) } == 0 {
        return Err("optional_broker_launch");
    }
    struct Attr(*mut std::ffi::c_void);
    impl Drop for Attr {
        fn drop(&mut self) {
            unsafe {
                DeleteProcThreadAttributeList(self.0);
            }
        }
    }
    let _attr = Attr(attrs);
    if unsafe {
        UpdateProcThreadAttribute(
            attrs,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
            handles.as_mut_ptr().cast(),
            size_of_val(&handles),
            null_mut(),
            null(),
        )
    } == 0
    {
        return Err("optional_broker_launch");
    }
    let mut in_job = 0;
    if unsafe { IsProcessInJob(GetCurrentProcess(), null_mut(), &mut in_job) } == 0 {
        return Err("optional_broker_launch");
    }
    let flags = CREATE_NO_WINDOW
        | EXTENDED_STARTUPINFO_PRESENT
        | if in_job != 0 {
            CREATE_BREAKAWAY_FROM_JOB
        } else {
            0
        };
    let mut startup: STARTUPINFOEXW = unsafe { zeroed() };
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.lpAttributeList = attrs;
    let mut process: PROCESS_INFORMATION = unsafe { zeroed() };
    let mut command = exclusion::wide(OsStr::new(&format!(
        "\"{}\" {FLAG} {nonce} {}",
        ctx.peer.image.display(),
        inherited.0 as usize
    )));
    if unsafe {
        CreateProcessW(
            exclusion::wide(ctx.peer.image.as_os_str()).as_ptr(),
            command.as_mut_ptr(),
            null(),
            null(),
            1,
            flags,
            null(),
            exclusion::wide(ctx.peer.image.parent().unwrap().as_os_str()).as_ptr(),
            &startup.StartupInfo,
            &mut process,
        )
    } == 0
    {
        return Err("optional_broker_launch");
    }
    let mut child = BootstrapChild {
        process: Handle::new(process.hProcess)?,
        _thread: Handle::new(process.hThread)?,
        commit_may_have_been_sent: false,
    };
    let peer = observe_peer(child.process.0, process.dwProcessId)?;
    let mut member = 0;
    if unsafe { IsProcessInJob(child.process.0, null_mut(), &mut member) } == 0
        || member != 0
        || peer.image != ctx.peer.image
        || peer.package != ctx.peer.package
        || peer.logon != ctx.peer.logon
    {
        return Err("optional_broker_launch");
    }
    let j = Journal {
        schema: "optional-engine-reconnect.v1".into(),
        nonce: nonce.clone(),
        binding: ctx.binding.clone(),
        broker: peer,
        launcher: ctx.peer.clone(),
        committed: false,
        selection,
        terminal: false,
        status: Status {
            operation_id: Some(nonce),
            state: Phase::Checking,
            in_flight: true,
            ..Status::default()
        },
    };
    let mut j = j;
    // Publish the bootstrap journal only after the lease marker is durable.
    // Until publication the inert child cannot inspect/settle this operation.
    lease.mark(true, &ctx.binding)?;
    if let Err(error) = j.save(data) {
        lease.mark(false, &ctx.binding)?;
        return Err(error);
    }
    drop(inherited);
    let end = Instant::now() + Duration::from_secs(5);
    loop {
        if cancelled() {
            if child.commit_may_have_been_sent {
                return Ok(unknown(Some(j.nonce), "optional_recovery_pending"));
            }
            drop(child);
            j.status = unknown(Some(j.nonce.clone()), "optional_broker_not_started");
            j.status.state = Phase::Failed;
            j.terminal = true;
            j.save(data)?;
            lease.mark(false, &ctx.binding)?;
            return Err("optional_cancelled");
        }
        // After granting Commit, never terminate the broker on a lost response.
        child.commit_may_have_been_sent = true;
        if let Ok(s) = request(&ctx, &j, Action::Commit {}, Some(&|| !cancelled())) {
            return Ok(s);
        }
        if let Some(done) = load(&ctx)? {
            if done.terminal {
                return Ok(installed_status(ctx, done.status));
            }
        }
        if Instant::now() >= end {
            return Ok(unknown(Some(j.nonce), "optional_recovery_pending"));
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
pub(super) fn run(args: &[String]) -> Result<()> {
    let bootstrap_end = Instant::now() + Duration::from_secs(5);
    let (nonce, raw) = arguments(args)?;
    let ctx = context()?;
    let mut in_job = 0;
    if unsafe { IsProcessInJob(GetCurrentProcess(), null_mut(), &mut in_job) } == 0 || in_job != 0 {
        return Err("optional_broker_launch");
    }
    let lease = Lease {
        handle: Handle::new(raw as HANDLE)?,
    };
    let mut flags = 0;
    if unsafe { GetHandleInformation(lease.handle.0, &mut flags) } == 0
        || flags & HANDLE_FLAG_INHERIT == 0
    {
        return Err("optional_broker_lease");
    }
    lease.validate(&ctx.family)?;
    let mut j = loop {
        if let Some(j) = load(&ctx)? {
            if j.nonce == nonce && j.broker == ctx.peer && !j.terminal && !j.committed {
                break j;
            }
        }
        if Instant::now() >= bootstrap_end {
            return Err("optional_broker_not_started");
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    crate::servicing_handoff::admitted(&ctx.data)?;
    let owner = Owner::new_local(ctx.data.clone());
    if owner.busy() {
        return Err("optional_recovery_pending");
    }
    let security = Security::new(true)?;
    let pipe = Handle::new(unsafe {
        CreateNamedPipeW(
            pipe_name(&ctx, &j)?.as_ptr(),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            8192,
            8192,
            0,
            &security.attributes(false),
        )
    })?;
    loop {
        if j.terminal {
            return Ok(());
        }
        if !j.committed && Instant::now() >= bootstrap_end {
            j.status = Status {
                operation_id: Some(j.nonce.clone()),
                state: Phase::Failed,
                reason: Some("optional_broker_not_started".into()),
                ..Status::default()
            };
            j.terminal = true;
            j.save(&ctx.data)?;
            if lease.bound(&ctx.binding)? {
                lease.mark(false, &ctx.binding)?;
            }
            return Ok(());
        }
        if j.committed && (!owner.status().in_flight || owner.local_finished()) {
            owner.drain();
            j.status = owner.status();
            j.terminal = true;
            j.save(&ctx.data)?;
            if j.status.state != Phase::RecoveryPending {
                lease.mark(false, &ctx.binding)?;
            }
            return Ok(());
        }
        let connected = unsafe { ConnectNamedPipe(pipe.0, null_mut()) } != 0
            || unsafe { GetLastError() } == ERROR_PIPE_CONNECTED;
        if connected {
            let mut serve = || -> Result<()> {
                let mut pid = 0;
                if unsafe { GetNamedPipeClientProcessId(pipe.0, &mut pid) } == 0 {
                    return Err("optional_broker_peer");
                }
                let request: Request = crate::worker_host::decode(&read_frame(
                    pipe.0,
                    Instant::now() + Duration::from_secs(2),
                )?)?;
                let _peer = validate_peer(pid, &request.peer, &ctx)?;
                if request.nonce != j.nonce {
                    return Err("optional_broker_peer");
                }
                if matches!(request.action, Action::Commit {}) {
                    if !commit_authorized(&j, &request) {
                        return Err("optional_broker_peer");
                    }
                    if !j.committed {
                        if !lease.bound(&ctx.binding)? {
                            return Err("optional_broker_lease");
                        }
                        // An authenticated original Core explicitly commits before mutation.
                        let mut committed = j.clone();
                        committed.committed = true;
                        committed.save(&ctx.data)?;
                        j = committed;
                        if owner
                            .start_local(j.selection, ctx.release.clone(), j.nonce.clone())
                            .is_err()
                        {
                            j.status = Status {
                                operation_id: Some(j.nonce.clone()),
                                state: Phase::Failed,
                                reason: Some("optional_broker_not_started".into()),
                                ..Status::default()
                            };
                            j.terminal = true;
                            j.save(&ctx.data)?;
                            lease.mark(false, &ctx.binding)?;
                            return Err("optional_broker_not_started");
                        }
                    }
                }
                if j.committed {
                    j.status = dispatch(&owner, &j, request)?;
                } else if !matches!(request.action, Action::Status {}) {
                    return Err("optional_broker_not_started");
                }
                write_frame(
                    pipe.0,
                    &serde_json::to_vec(&j).map_err(|_| "optional_broker_frame")?,
                )
            };
            let _ = serve();
            unsafe {
                DisconnectNamedPipe(pipe.0);
            }
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

// This entry is reachable only after the normal application's full admission.
pub(super) fn admitted_servicing() -> Result<bool> {
    let identity = native::package_identity()?.ok_or("servicing_completion_invalid")?;
    let family = identity
        .family
        .as_deref()
        .ok_or("servicing_completion_invalid")?;
    let Some(receipt) = exclusion::DeferredReceipt::load(family)? else {
        return Ok(true);
    };
    let peer = observe_peer(unsafe { GetCurrentProcess() }, unsafe {
        GetCurrentProcessId()
    })?;
    let (actual, root) = servicing::installed(&peer.package)?;
    if actual.identity != identity
        || peer.image
            != PathBuf::from(root)
                .join("app/Bridge.exe")
                .canonicalize()
                .map_err(|_| "servicing_completion_invalid")?
    {
        return Err("servicing_completion_invalid");
    }
    let descriptor = std::fs::read(
        peer.image
            .parent()
            .ok_or("servicing_completion_invalid")?
            .join("desktop-release.json"),
    )
    .map_err(|_| "servicing_completion_invalid")?;
    if descriptor.is_empty() || descriptor.len() > 1024 * 1024 {
        return Err("servicing_completion_invalid");
    }
    let release = deployment::load(
        &peer.image.parent().unwrap().join("desktop-release.json"),
        Some(&identity),
    )?
    .ok_or("servicing_completion_invalid")?;
    if release.update_owner != "direct-appinstaller"
        || !receipt.matches(
            family,
            &peer.package,
            &identity.name,
            &identity.publisher,
            &identity.version,
            &crate::worker_host::sha(&descriptor),
            release.app_installer_uri.as_deref().unwrap_or(""),
        )
    {
        // An old or unrelated admitted Core must preserve the known pending update.
        return Ok(true);
    }
    let _apartment = servicing::Apartment::new().map_err(|_| "servicing_completion_invalid")?;
    let (_, observed) = servicing::observe().map_err(|_| "servicing_completion_invalid")?;
    if observed.identity != identity
        || servicing::bind(&release, &observed) != servicing::UpdateSource::DirectAppinstaller
    {
        return Err("servicing_completion_invalid");
    }
    let manager: windows_runtime::Management::Deployment::PackageManager =
        servicing::activate_owned().map_err(|_| "servicing_completion_invalid")?;
    match servicing::selected_engine(&manager, &release, &identity)? {
        servicing::EngineSelection::Absent if !receipt.engine_present => (),
        servicing::EngineSelection::Present { version, .. } if receipt.engine_present => {
            let engine = release
                .engine_identity()
                .ok_or("servicing_completion_invalid")?;
            if receipt.engine_family.as_deref() != Some(engine.family.as_str())
                || receipt.engine_version.as_deref() != Some(version.as_str())
            {
                return Err("servicing_completion_invalid");
            }
            crate::worker_host::inspect_installed_engine()?;
        }
        _ => return Err("servicing_completion_invalid"),
    }
    // The marker's distinct kind prevents this receipt from settling an unknown
    // optional acquisition. No package/process absence is terminal evidence.
    let lease = match Lease::deferred(family, &receipt.binding) {
        Ok(lease) => lease,
        Err(_) => return Ok(false),
    };
    lease.mark_servicing(false, &receipt.binding)?;
    Ok(true)
}
