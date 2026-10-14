#![cfg(all(windows, feature = "qualification-tools"))]
#![allow(dead_code)]
#[path = "../src/policy.rs"]
mod policy;
#[path = "../src/windows.rs"]
mod windows;
use std::{
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

struct Directory(PathBuf);
impl Directory {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "bridge-process-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Directory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
fn started(data: &Path) -> u32 {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Ok(value) = std::fs::read_to_string(data.join("started")) {
            if let Ok(pid) = value.parse() {
                return pid;
            }
        }
        assert!(Instant::now() < deadline, "contained child never started");
        std::thread::sleep(Duration::from_millis(20));
    }
}
#[test]
fn closing_the_owned_pipe_allows_graceful_shutdown() {
    let data = Directory::new();
    let image = Path::new(env!("CARGO_BIN_EXE_brain-fixture"))
        .canonicalize()
        .unwrap();
    assert!(image.as_os_str().to_string_lossy().starts_with(r"\\?\"));
    let mut child = windows::Brain::start(&image, &data.0, "test-lifetime").unwrap();
    assert_eq!(started(&data.0), child.pid);
    child.stop();
    assert_eq!(child.exit_code().unwrap(), Some(0));
    assert!(data.0.join("graceful").is_file());
}
#[test]
fn killing_the_supervisor_does_not_leave_its_child_running() {
    use std::os::windows::process::CommandExt;
    use windows_sys::Win32::{Foundation::*, System::Threading::*};
    let data = Directory::new();
    let mut parent = std::process::Command::new(env!("CARGO_BIN_EXE_brain-fixture"))
        .arg("--supervise")
        .env("LOCAL_ANALYTICS_DATA_DIR", &data.0)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .unwrap();
    let pid = started(&data.0);
    unsafe {
        let child = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
        assert!(!child.is_null());
        parent.kill().unwrap();
        parent.wait().unwrap();
        let outcome = WaitForSingleObject(child, 5000);
        CloseHandle(child);
        assert_eq!(
            outcome, WAIT_OBJECT_0,
            "Job Object left an orphan after host death"
        );
    }
}

#[test]
fn socket_first_shutdown_requires_actual_exit_and_refuses_timeout_or_quit() {
    use std::sync::atomic::AtomicBool;
    let image = Path::new(env!("CARGO_BIN_EXE_brain-fixture"))
        .canonicalize()
        .unwrap();
    for (code, timeout, quitting, expected) in [
        (75, Duration::from_secs(2), false, Some(75)),
        (1, Duration::from_secs(2), false, Some(1)),
        (75, Duration::from_millis(10), false, None),
        (75, Duration::from_secs(2), true, None),
    ] {
        let data = Directory::new();
        std::fs::write(data.0.join("delayed-exit"), code.to_string()).unwrap();
        let mut child = windows::Brain::start(&image, &data.0, "test-lifetime").unwrap();
        assert_eq!(started(&data.0), child.pid);
        assert!(windows::listeners()
            .unwrap()
            .iter()
            .any(|row| row.pid == child.pid));
        std::fs::write(data.0.join("close-listener"), b"close").unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while !data.0.join("listener-closed").is_file() {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(windows::listeners().unwrap().is_empty());
        assert_eq!(
            child.exit_code().unwrap(),
            None,
            "listener closes before process exit"
        );
        let result = child
            .wait_for_exit(timeout, &AtomicBool::new(quitting))
            .unwrap();
        assert_eq!(result, expected);
        let mut restart = policy::RestartPolicy::default();
        if quitting {
            restart.quit();
        }
        assert_eq!(
            result.is_some_and(|exit| restart.should_restart(exit, true)),
            expected == Some(75)
        );
        child.stop();
    }
}
