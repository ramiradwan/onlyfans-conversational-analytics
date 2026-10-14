#![cfg(all(windows, feature = "qualification-tools"))]
#![allow(dead_code)]
#[path = "../src/bootstrap.rs"]
mod bootstrap;
#[path = "../src/deployment.rs"]
mod deployment;
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
    assert!(
        child.stop(),
        "ordinary pipe closure required forced Job cleanup"
    );
    assert_eq!(child.exit_code().unwrap(), Some(0));
    assert!(data.0.join("graceful").is_file());
}

#[test]
fn child_status_is_separate_from_lifetime_and_never_admits_a_listener() {
    let data = Directory::new();
    let image = Path::new(env!("CARGO_BIN_EXE_brain-fixture"))
        .canonicalize()
        .unwrap();
    let mut child = windows::Brain::start(&image, &data.0, "test-lifetime").unwrap();
    assert_eq!(started(&data.0), child.pid);
    let mut observation = bootstrap::Supervisor::new(Instant::now());
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let (bytes, closed) = child.bootstrap_bytes().unwrap();
        assert!(!closed);
        observation.receive(&bytes, Instant::now()).unwrap();
        if !bytes.is_empty() {
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(!observation.ready());
    assert!(!windows::listeners()
        .unwrap()
        .iter()
        .any(|row| row.pid == child.pid));
    assert!(
        child.stop(),
        "ordinary maintenance shutdown required forced Job cleanup"
    );
    assert_eq!(child.exit_code().unwrap(), Some(0));
}

#[test]
#[ignore = "requires a newly built frozen Brain supplied by BRAIN_QUALIFICATION_IMAGE"]
fn frozen_brain_uses_native_status_installer_signal_and_graceful_parent_shutdown() {
    let image = PathBuf::from(
        std::env::var_os("BRAIN_QUALIFICATION_IMAGE").expect("explicit frozen artifact required"),
    )
    .canonicalize()
    .unwrap();
    let data = Directory::new();
    let _installer_signal = windows::application_mutex().unwrap();
    let sid = windows::current_sid().unwrap();
    let mut original_key = None;
    for _ in 0..2 {
        assert!(
            windows::listeners().unwrap().is_empty(),
            "qualification port must be free"
        );
        let mut child = windows::Brain::start(
            &image,
            &data.0,
            "qualification-native-handoff-00000000000000000000000001",
        )
        .unwrap();
        let mut observation = bootstrap::Supervisor::new(Instant::now());
        loop {
            assert_eq!(
                child.exit_code().unwrap(),
                None,
                "frozen child refused real native startup"
            );
            let (bytes, closed) = child.bootstrap_bytes().unwrap();
            assert!(!closed);
            observation.receive(&bytes, Instant::now()).unwrap();
            if policy::verify_listener(&windows::listeners().unwrap(), child.pid, &image, &sid)
                .unwrap()
            {
                assert!(
                    observation.ready(),
                    "listener appeared before validated startup"
                );
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        let key = std::fs::read(data.0.join(".ofca-master-key.dpapi")).unwrap();
        if let Some(ref previous) = original_key {
            assert_eq!(previous, &key);
        } else {
            original_key = Some(key);
        }
        assert!(
            child.stop(),
            "healthy frozen maintenance used forced Job cleanup"
        );
        assert_eq!(child.exit_code().unwrap(), Some(0));
        assert!(windows::listeners().unwrap().is_empty());
    }
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
