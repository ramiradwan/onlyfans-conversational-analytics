#![cfg(all(windows, feature = "qualification-tools"))]

use std::{
    path::PathBuf,
    process::Command,
    time::{Duration, Instant},
};
use windows_sys::Win32::{Foundation::*, System::Threading::*};

struct Directory(PathBuf);
impl Directory {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "optional-process-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        Self(root)
    }
    fn wait_pid(&self, name: &str) -> u32 {
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            if let Ok(value) = std::fs::read_to_string(self.0.join(name)) {
                if let Ok(pid) = value.parse() {
                    return pid;
                }
            }
            assert!(Instant::now() < deadline, "fixture never became ready");
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for Directory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn ended(pid: u32) {
    unsafe {
        let process = OpenProcess(
            PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
            0,
            pid,
        );
        if process.is_null() {
            assert_eq!(GetLastError(), ERROR_INVALID_PARAMETER);
            return;
        }
        let result = WaitForSingleObject(process, 5000);
        CloseHandle(process);
        assert_eq!(result, WAIT_OBJECT_0, "owned descendant remained");
    }
}

fn run(abrupt: bool, worker: bool) {
    let root = Directory::new();
    let image = PathBuf::from(env!("CARGO_BIN_EXE_optional-package-fixture"));
    if worker {
        std::fs::write(
            root.0.join("approved-worker-path"),
            image.as_os_str().to_string_lossy().as_bytes(),
        )
        .unwrap();
    }
    let mut supervisor = Command::new(&image)
        .env("LOCAL_ANALYTICS_DATA_DIR", &root.0)
        .arg("--supervise")
        .spawn()
        .unwrap();
    root.wait_pid("supervisor-ready");
    let brain = root.wait_pid("brain-started");
    let optional = worker.then(|| root.wait_pid("worker-started"));
    if abrupt {
        supervisor.kill().unwrap();
        supervisor.wait().unwrap();
    } else {
        std::fs::write(root.0.join("stop"), b"shutdown").unwrap();
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            if let Some(code) = supervisor.try_wait().unwrap() {
                assert!(code.success());
                break;
            }
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(root.0.join("servicing-ready").is_file());
        assert!(root.0.join("brain-drained").is_file());
        if worker {
            assert!(root.0.join("worker-stopped").is_file());
        }
    }
    ended(brain);
    if let Some(pid) = optional {
        ended(pid);
    }
}

#[test]
fn optional_worker_drains_before_native_servicing() {
    run(false, true);
}
#[test]
fn optional_worker_dies_with_native_supervisor() {
    run(true, true);
}
#[test]
fn native_supervisor_servicing_without_optional_worker() {
    run(false, false);
}
