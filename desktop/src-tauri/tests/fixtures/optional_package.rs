//! Synthetic optional-process fixture; excluded from release packaging.
#![allow(dead_code)]
#[path = "../../src/bootstrap.rs"]
mod bootstrap;
#[path = "../../src/deployment.rs"]
mod deployment;
#[path = "../../src/policy.rs"]
mod policy;
#[cfg(debug_assertions)]
#[path = "../../src/servicing.rs"]
mod servicing;
#[cfg(debug_assertions)]
#[path = "../../src/servicing_feed.rs"]
mod servicing_feed;
#[path = "../../src/windows.rs"]
mod windows;

use std::{
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::AtomicBool,
    time::{Duration, Instant},
};

fn root() -> PathBuf {
    std::env::var_os("LOCAL_ANALYTICS_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("LOCALAPPDATA").expect("local app data"))
                .join("TextAnalysisPackageQualification")
        })
}

fn observe(root: &Path, name: &str, value: impl AsRef<[u8]>) {
    std::fs::write(root.join(name), value).expect("synthetic observation");
}

fn wait_file(root: &Path, name: &str) {
    let deadline = Instant::now() + Duration::from_secs(20);
    while !root.join(name).is_file() {
        assert!(Instant::now() < deadline, "synthetic observation timed out");
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn status(handle: usize, frame: &[u8]) {
    unsafe {
        let mut written = 0;
        assert_ne!(
            windows_sys::Win32::Storage::FileSystem::WriteFile(
                handle as _,
                frame.as_ptr(),
                frame.len() as u32,
                &mut written,
                std::ptr::null_mut(),
            ),
            0
        );
        assert_eq!(written as usize, frame.len());
    }
}

fn worker(root: &Path) {
    observe(
        root,
        "worker-variant",
        option_env!("OPTIONAL_PACKAGE_FIXTURE_VARIANT").unwrap_or("A"),
    );
    observe(root, "worker-started", std::process::id().to_string());
    let mut input = [0u8; 1];
    // Only pipe EOF is meaningful; this fixture receives no text payload.
    assert_eq!(std::io::stdin().read(&mut input).unwrap(), 0);
    observe(root, "worker-stopped", b"pipe-eof");
}

fn brain(root: &Path) {
    let status_handle: usize = std::env::var("BRAIN_BOOTSTRAP_STATUS_HANDLE")
        .unwrap()
        .parse()
        .unwrap();
    let lifetime: usize = std::env::var("BRAIN_PARENT_LIFETIME_HANDLE")
        .unwrap()
        .parse()
        .unwrap();
    unsafe {
        use windows_sys::Win32::Foundation::{SetHandleInformation, HANDLE_FLAG_INHERIT};
        assert_ne!(
            SetHandleInformation(status_handle as _, HANDLE_FLAG_INHERIT, 0),
            0
        );
        assert_ne!(
            SetHandleInformation(lifetime as _, HANDLE_FLAG_INHERIT, 0),
            0
        );
    }
    status(status_handle, b"{\"schema\":\"ofca-bootstrap-status/v1\",\"seq\":1,\"phase\":\"starting\",\"reason\":null}\n{\"schema\":\"ofca-bootstrap-status/v1\",\"seq\":2,\"phase\":\"checking\",\"reason\":null}\n");
    let worker_image = std::fs::read_to_string(root.join("approved-worker-path"))
        .ok()
        .map(PathBuf::from);
    let mut child = worker_image.map(|image| {
        assert!(image.is_absolute() && image.is_file());
        Command::new(image)
            .arg("--worker")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("installed synthetic worker")
    });
    if child.is_some() {
        wait_file(root, "worker-started");
    }
    observe(root, "brain-started", std::process::id().to_string());
    status(status_handle, b"{\"schema\":\"ofca-bootstrap-status/v1\",\"seq\":3,\"phase\":\"ready-to-check-listener\",\"reason\":null}\n");
    unsafe {
        let mut input = [0u8; 1];
        let mut read = 0;
        let result = windows_sys::Win32::Storage::FileSystem::ReadFile(
            lifetime as _,
            input.as_mut_ptr(),
            1,
            &mut read,
            std::ptr::null_mut(),
        );
        assert!(
            result != 0
                || windows_sys::Win32::Foundation::GetLastError()
                    == windows_sys::Win32::Foundation::ERROR_BROKEN_PIPE
        );
        assert_eq!(read, 0);
    }
    if let Some(child) = child.as_mut() {
        child.stdin.take();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(code) = child.try_wait().unwrap() {
                assert!(code.success());
                break;
            }
            assert!(Instant::now() < deadline, "worker did not drain");
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    status(
        status_handle,
        b"{\"schema\":\"ofca-runtime-shutdown/v1\",\"after_seq\":3,\"drained\":true}\n",
    );
    observe(root, "brain-drained", b"worker-exited");
}

fn supervise(root: &Path) {
    let image = std::env::current_exe().unwrap().canonicalize().unwrap();
    let mut child = windows::Brain::start(&image, root, "synthetic-optional-process").unwrap();
    let mut startup = bootstrap::Supervisor::new(Instant::now());
    let deadline = Instant::now() + Duration::from_secs(20);
    while !startup.ready() {
        let (bytes, _) = child.bootstrap_bytes().unwrap();
        startup.receive(&bytes, Instant::now()).unwrap();
        assert!(child.exit_code().unwrap().is_none());
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    observe(root, "supervisor-ready", std::process::id().to_string());
    wait_file(root, "stop");
    let mut receipt = startup.shutdown_receipt().unwrap();
    let stopped = child
        .stop_for_servicing(
            &mut receipt,
            Duration::from_secs(10),
            &AtomicBool::new(false),
        )
        .unwrap();
    assert!(stopped);
    observe(
        root,
        "servicing-ready",
        b"drained-exit-zero-empty-owned-job",
    );
}

fn main() {
    let root = root();
    #[cfg(debug_assertions)]
    {
        use servicing::optional_acquisition::{self, Action};
        let arguments: Vec<_> = std::env::args().collect();
        if arguments.get(1).map(String::as_str) == Some("--inspect-staged-target") {
            if arguments.len() != 2 {
                std::process::exit(1);
            }
            let passed =
                std::thread::spawn(move || servicing::staged_readback_qualification::run(&root))
                    .join()
                    .unwrap_or(false);
            std::process::exit(if passed { 0 } else { 1 });
        }
        let action = match arguments.get(1).map(String::as_str) {
            Some("--bootstrap-related-source") => Some(Action::Bootstrap),
            Some("--inspect-optional-source") => Some(Action::Inspect),
            Some("--acquire-optional") => Some(Action::Acquire),
            Some("--cancel-optional") => Some(Action::CancelAcquire),
            _ => None,
        };
        if let Some(action) = action {
            if arguments.len() != 2 {
                std::process::exit(1);
            }
            let passed = std::thread::spawn(move || optional_acquisition::run(&root, action))
                .join()
                .unwrap_or(false);
            std::process::exit(if passed { 0 } else { 1 });
        }
    }
    std::fs::create_dir_all(&root).unwrap();
    match std::env::args().nth(1).as_deref() {
        Some("--worker") => worker(&root),
        Some("--brain") => brain(&root),
        None | Some("--supervise") => supervise(&root),
        _ => panic!("closed synthetic fixture mode"),
    }
}
