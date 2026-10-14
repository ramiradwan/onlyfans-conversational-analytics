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
    qualify_frozen_startup(None);
}

#[test]
#[ignore = "requires independently built A/B artifacts with auth catalogs 32 and 33"]
fn frozen_a_to_b_migrates_before_admission_and_reopens_original_protected_key() {
    let older = PathBuf::from(
        std::env::var_os("BRAIN_QUALIFICATION_A_IMAGE").expect("explicit A artifact required"),
    )
    .canonicalize()
    .unwrap();
    let newer = PathBuf::from(
        std::env::var_os("BRAIN_QUALIFICATION_IMAGE").expect("explicit B artifact required"),
    )
    .canonicalize()
    .unwrap();
    let catalog = |image: &Path| {
        image
            .parent()
            .unwrap()
            .join("_internal/app/persistence/auth_sql")
    };
    let count = |image: &Path| {
        std::fs::read_dir(catalog(image))
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "sql"))
            .count()
    };
    assert_eq!(count(&older), 32);
    assert_eq!(count(&newer), 33);
    qualify_frozen_startup(Some(&older));
}

fn retained_fixture(operation: &str, data: &Path, rows: Option<usize>) -> serde_json::Value {
    let python = std::env::var_os("BRAIN_QUALIFICATION_PYTHON")
        .expect("explicit SQLCipher/DPAPI qualification Python required");
    let mut command = std::process::Command::new(python);
    command.current_dir(Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."));
    command.args([
        "-m",
        "tools.startup_maintenance_qualification_fixture",
        operation,
    ]);
    command.arg(data);
    if let Some(rows) = rows {
        command.arg(rows.to_string());
    }
    let output = command.output().unwrap();
    assert!(
        output.status.success(),
        "synthetic fixture failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

fn assert_retained_work(before: &serde_json::Value, after: &serde_json::Value) {
    for key in [
        "onboarding_progress_outbox",
        "onboarding_progress_outbox_count",
        "capability_license_pending_deliveries",
        "capability_license_pending_deliveries_count",
        "messages",
        "message_count",
        "commands",
        "runtime.env",
        ".ofca-master-key.dpapi",
    ] {
        assert_eq!(before[key], after[key], "retained work changed: {key}");
    }
}

#[test]
#[ignore = "requires independently frozen A32/B33 artifacts; invalid retained authority must refuse"]
fn frozen_invalid_retained_authority_never_becomes_first_run() {
    let image = PathBuf::from(std::env::var_os("BRAIN_QUALIFICATION_IMAGE").unwrap())
        .canonicalize()
        .unwrap();
    let older = PathBuf::from(std::env::var_os("BRAIN_QUALIFICATION_A_IMAGE").unwrap())
        .canonicalize()
        .unwrap();
    let data = Directory::new();
    let _installer_signal = windows::application_mutex().unwrap();
    let sid = windows::current_sid().unwrap();
    assert!(windows::listeners().unwrap().is_empty());
    let mut a = windows::Brain::start(
        &older,
        &data.0,
        "qualification-native-handoff-00000000000000000000000001",
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(30);
    while !policy::verify_listener(&windows::listeners().unwrap(), a.pid, &older, &sid).unwrap() {
        assert_eq!(a.exit_code().unwrap(), None);
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(a.stop());
    let master = data.0.join(".ofca-master-key.dpapi");
    let saved = data.0.join("synthetic-master.retained");
    let original_key = std::fs::read(&master).unwrap();
    for invalid in 0..2 {
        if invalid == 0 {
            std::fs::rename(&master, &saved).unwrap();
        } else {
            std::fs::rename(&saved, &master).unwrap();
            std::fs::write(
                data.0.join("auth.sqlite3"),
                b"synthetic corrupt encrypted database",
            )
            .unwrap();
        }
        let before = std::fs::read(data.0.join("auth.sqlite3")).unwrap();
        let mut child = windows::Brain::start(
            &image,
            &data.0,
            "qualification-native-handoff-00000000000000000000000001",
        )
        .unwrap();
        let deadline = Instant::now() + Duration::from_secs(30);
        let mut frames = Vec::new();
        loop {
            let (bytes, _) = child.bootstrap_bytes().unwrap();
            frames.extend_from_slice(&bytes);
            assert!(windows::listeners().unwrap().is_empty());
            if let Some(exit) = child.exit_code().unwrap() {
                assert_eq!(exit, 78, "refusal did not use the controlled process exit");
                frames.extend_from_slice(&child.bootstrap_bytes().unwrap().0);
                break;
            }
            assert!(
                Instant::now() < deadline,
                "refusal did not terminate the child"
            );
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(child.stop());
        let status = String::from_utf8(frames).unwrap();
        assert!(status.contains("\"phase\":\"recovery-required\""));
        assert!(!status.contains("ready-to-check-listener"));
        assert_eq!(std::fs::read(data.0.join("auth.sqlite3")).unwrap(), before);
        assert!(!data.0.join("runtime.env").exists());
        assert!(!data.0.join("canonical.sqlite3").exists());
        if invalid == 0 {
            assert!(!master.exists(), "missing original key was regenerated");
            assert_eq!(std::fs::read(&saved).unwrap(), original_key);
        } else {
            assert_eq!(std::fs::read(&master).unwrap(), original_key);
        }
    }
}

#[test]
#[ignore = "requires frozen A32/B33 images and explicit qualification Python; large synthetic retained state"]
fn frozen_recovery_preserves_pending_work_and_newer_publication_without_restore() {
    let image = PathBuf::from(std::env::var_os("BRAIN_QUALIFICATION_IMAGE").unwrap())
        .canonicalize()
        .unwrap();
    let older = PathBuf::from(std::env::var_os("BRAIN_QUALIFICATION_A_IMAGE").unwrap())
        .canonicalize()
        .unwrap();
    let rows: usize = std::env::var("BRAIN_QUALIFICATION_ROWS")
        .unwrap_or_else(|_| "150000".into())
        .parse()
        .unwrap();
    let data = Directory::new();
    let _installer_signal = windows::application_mutex().unwrap();
    let sid = windows::current_sid().unwrap();
    assert!(windows::listeners().unwrap().is_empty());
    let mut a = windows::Brain::start(
        &older,
        &data.0,
        "qualification-native-handoff-00000000000000000000000001",
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(30);
    while !policy::verify_listener(&windows::listeners().unwrap(), a.pid, &older, &sid).unwrap() {
        assert_eq!(a.exit_code().unwrap(), None);
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(a.stop());
    let original = retained_fixture("seed", &data.0, Some(rows));
    assert_eq!(original["auth_version"], 32);
    assert_eq!(original["canonical_version"], 11);
    assert_eq!(original["message_count"], rows);
    assert_eq!(original["onboarding_progress_outbox_count"], 1);
    assert_eq!(original["capability_license_pending_deliveries_count"], 1);

    // First interruption: abrupt process loss while the large canonical
    // encrypted snapshot is being copied, before durable transition intent.
    // Second interruption: ordinary parent closure after auth33 committed,
    // while the existing canonical runner is still preparing v12.
    for interruption in 0..2 {
        let started = Instant::now();
        let mut child = windows::Brain::start(
            &image,
            &data.0,
            "qualification-native-handoff-00000000000000000000000001",
        )
        .unwrap();
        let mut supervisor = bootstrap::Supervisor::new(started);
        let mut frames = Vec::new();
        loop {
            assert_eq!(
                child.exit_code().unwrap(),
                None,
                "frozen recovery refused before the chosen boundary"
            );
            let (bytes, closed) = child.bootstrap_bytes().unwrap();
            assert!(!closed);
            supervisor.receive(&bytes, Instant::now()).unwrap();
            frames.extend_from_slice(&bytes);
            assert!(
                !supervisor.ready(),
                "normal admission preceded interruption"
            );
            assert!(windows::listeners().unwrap().is_empty());
            let migrating = frames
                .split(|b| *b == b'\n')
                .filter(|line| !line.is_empty())
                .filter(|line| {
                    serde_json::from_slice::<serde_json::Value>(line).unwrap()["phase"]
                        == "migrating"
                })
                .count();
            let copying =
                std::fs::metadata(data.0.join("upgrade-checkpoint/canonical.sqlite3.bak"))
                    .is_ok_and(|file| file.len() > 4096);
            if (interruption == 0 && copying) || (interruption == 1 && migrating >= 2) {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        println!(
            "frozen interruption {interruption}: {:.3}s before listener admission",
            started.elapsed().as_secs_f64()
        );
        if interruption == 0 {
            use windows_sys::Win32::{Foundation::*, System::Threading::*};
            unsafe {
                let owned = OpenProcess(PROCESS_TERMINATE | PROCESS_SYNCHRONIZE, 0, child.pid);
                assert!(!owned.is_null());
                assert_ne!(TerminateProcess(owned, 91), 0);
                assert_eq!(WaitForSingleObject(owned, 5000), WAIT_OBJECT_0);
                CloseHandle(owned);
            }
            assert!(child.stop());
            assert_eq!(child.exit_code().unwrap(), Some(91));
            assert!(!data.0.join(".ofca-upgrade-state.dpapi").exists());
        } else {
            assert!(
                child.stop(),
                "normal pre-listener cancellation used forced Job cleanup"
            );
            assert!(data.0.join(".ofca-upgrade-state.dpapi").is_file());
        }
        assert!(windows::listeners().unwrap().is_empty());
        let observed = retained_fixture("observe", &data.0, None);
        assert_retained_work(&original, &observed);
        assert_eq!(observed["canonical_version"], 11);
        assert_eq!(
            observed["auth_version"],
            if interruption == 0 { 32 } else { 33 }
        );
    }
    let publication = retained_fixture("publish", &data.0, None);
    assert!(publication["publication"].is_string());
    for cycle in 0..2 {
        let started = Instant::now();
        let mut child = windows::Brain::start(
            &image,
            &data.0,
            "qualification-native-handoff-00000000000000000000000001",
        )
        .unwrap();
        let mut supervisor = bootstrap::Supervisor::new(started);
        let mut frames = Vec::new();
        loop {
            assert_eq!(
                child.exit_code().unwrap(),
                None,
                "retained installation failed to resume"
            );
            let (bytes, closed) = child.bootstrap_bytes().unwrap();
            assert!(!closed);
            supervisor.receive(&bytes, Instant::now()).unwrap();
            frames.extend_from_slice(&bytes);
            if policy::verify_listener(&windows::listeners().unwrap(), child.pid, &image, &sid)
                .unwrap()
            {
                assert!(supervisor.ready());
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        println!(
            "frozen resume cycle {cycle}: {:.3}s to verified listener",
            started.elapsed().as_secs_f64()
        );
        assert!(!data.0.join("upgrade-checkpoint").exists());
        assert!(
            !String::from_utf8(frames)
                .unwrap()
                .contains("\"phase\":\"checkpointing\""),
            "resume recreated an authoritative checkpoint"
        );
        assert!(
            child.stop(),
            "healthy resumed installation used forced Job cleanup"
        );
        assert_eq!(child.exit_code().unwrap(), Some(0));
        let observed = retained_fixture("observe", &data.0, None);
        assert_retained_work(&original, &observed);
        assert_eq!(observed["auth_version"], 33);
        assert_eq!(observed["canonical_version"], 12);
        assert_eq!(
            observed["publication"], publication["publication"],
            "resume restored away newer security state"
        );
    }
}

fn qualify_frozen_startup(older: Option<&Path>) {
    let image = PathBuf::from(
        std::env::var_os("BRAIN_QUALIFICATION_IMAGE").expect("explicit frozen artifact required"),
    )
    .canonicalize()
    .unwrap();
    let data = Directory::new();
    let _installer_signal = windows::application_mutex().unwrap();
    let sid = windows::current_sid().unwrap();
    let mut original_key = None;
    if let Some(older) = older {
        assert!(windows::listeners().unwrap().is_empty());
        let mut child = windows::Brain::start(
            older,
            &data.0,
            "qualification-native-handoff-00000000000000000000000001",
        )
        .unwrap();
        let deadline = Instant::now() + Duration::from_secs(30);
        while !policy::verify_listener(&windows::listeners().unwrap(), child.pid, older, &sid)
            .unwrap()
        {
            assert_eq!(child.exit_code().unwrap(), None);
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(50));
        }
        original_key = Some(std::fs::read(data.0.join(".ofca-master-key.dpapi")).unwrap());
        assert!(child.stop(), "A did not quiesce normally before transition");
        assert_eq!(child.exit_code().unwrap(), Some(0));
    }
    for cycle in 0..2 {
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
        let mut frames = Vec::new();
        loop {
            assert_eq!(
                child.exit_code().unwrap(),
                None,
                "frozen child refused real native startup"
            );
            let (bytes, closed) = child.bootstrap_bytes().unwrap();
            frames.extend_from_slice(&bytes);
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
        let phases: Vec<String> = frames
            .split(|byte| *byte == b'\n')
            .filter(|line| !line.is_empty())
            .map(|line| {
                serde_json::from_slice::<serde_json::Value>(line).unwrap()["phase"]
                    .as_str()
                    .unwrap()
                    .to_owned()
            })
            .collect();
        if older.is_some() && cycle == 0 {
            assert!(phases.iter().any(|phase| phase == "checkpointing"));
            assert!(phases.iter().any(|phase| phase == "migrating"));
            assert!(data.0.join(".ofca-upgrade-state.dpapi").is_file());
        } else {
            assert!(!phases.iter().any(|phase| phase == "checkpointing"));
        }
        assert!(
            !data.0.join("upgrade-checkpoint").exists(),
            "normal work preceded checkpoint completion"
        );
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
