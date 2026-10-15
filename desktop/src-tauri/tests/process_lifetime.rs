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
#[ignore = "internal owned-process fixture for root ownership OS tests"]
fn native_root_owner_fixture() {
    use windows_sys::Win32::{Foundation::*, UI::WindowsAndMessaging::*};
    unsafe extern "system" fn receive(
        hwnd: HWND,
        message: u32,
        wparam: usize,
        lparam: isize,
    ) -> isize {
        if message == WM_COPYDATA {
            let data =
                &*(lparam as *const windows_sys::Win32::System::DataExchange::COPYDATASTRUCT);
            assert_eq!(data.dwData, 1542);
            assert!(data.cbData <= 65536);
            let bytes = std::slice::from_raw_parts(data.lpData.cast::<u8>(), data.cbData as usize);
            let root = PathBuf::from(std::env::var_os("OFCA_ROOT_OWNER_TEST_DIRECTORY").unwrap());
            std::fs::write(root.join("forwarded"), bytes).unwrap();
            PostQuitMessage(0);
            return 1;
        }
        DefWindowProcW(hwnd, message, wparam, lparam)
    }
    let root = PathBuf::from(std::env::var_os("OFCA_ROOT_OWNER_TEST_DIRECTORY").unwrap());
    let names = deployment::instance_names(&root, &windows::current_sid().unwrap(), None).unwrap();
    let executable = std::env::current_exe().unwrap();
    let ownership = windows::claim_runtime_root(&names, &executable).unwrap();
    assert!(ownership.primary());
    let id = if std::env::var_os("OFCA_ROOT_OWNER_TEST_OLD").is_some() {
        "org.conversation-analytics.bridge"
    } else {
        &names.plugin_id
    };
    let class: Vec<u16> = format!("{id}-sic").encode_utf16().chain(Some(0)).collect();
    let caption: Vec<u16> = format!("{id}-siw").encode_utf16().chain(Some(0)).collect();
    unsafe {
        let definition = WNDCLASSW {
            lpfnWndProc: Some(receive),
            lpszClassName: class.as_ptr(),
            ..std::mem::zeroed()
        };
        assert_ne!(RegisterClassW(&definition), 0);
        let window = CreateWindowExW(
            0,
            class.as_ptr(),
            caption.as_ptr(),
            0,
            0,
            0,
            0,
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null(),
        );
        assert!(!window.is_null());
        std::fs::write(root.join("started"), std::process::id().to_string()).unwrap();
        let mut message = std::mem::zeroed();
        while GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) > 0 {
            DispatchMessageW(&message);
        }
        DestroyWindow(window);
    }
}

#[test]
fn native_root_ownership_fences_other_namespaces_and_forwards_only_verified_peers() {
    use std::os::windows::process::CommandExt;
    let root = Directory::new();
    let executable = std::env::current_exe().unwrap();
    let mut process = std::process::Command::new(&executable);
    process.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    process
        .args([
            "native_root_owner_fixture",
            "--exact",
            "--ignored",
            "--test-threads=1",
        ])
        .env("OFCA_ROOT_OWNER_TEST_DIRECTORY", &root.0);
    let mut child = process.spawn().unwrap();
    assert_eq!(started(&root.0), child.id());
    let names =
        deployment::instance_names(&root.0, &windows::current_sid().unwrap(), None).unwrap();
    let secondary = windows::claim_runtime_root(&names, &executable).unwrap();
    assert!(!secondary.primary());
    let mut other = names.clone();
    other.plugin_id.push_str(".another-family");
    assert!(windows::claim_runtime_root(&other, &executable).is_err());
    assert!(
        windows::claim_runtime_root(&names, Path::new(env!("CARGO_BIN_EXE_brain-fixture")))
            .is_err()
    );
    let arguments = vec![
        executable.to_string_lossy().into_owned(),
        "--open-workspace".into(),
        "ofca://onboarding/?journey=00000000-0000-0000-0000-000000000001".into(),
    ];
    windows::forward_instance(&names, &executable, &arguments).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while child.try_wait().unwrap().is_none() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    let forwarded = std::fs::read_to_string(root.0.join("forwarded")).unwrap();
    assert!(
        forwarded.ends_with("ofca://onboarding/?journey=00000000-0000-0000-0000-000000000001\0")
    );
    drop(secondary);
    assert!(windows::claim_runtime_root(&names, &executable)
        .unwrap()
        .primary());
}

#[test]
fn uncoordinated_legacy_instance_refuses_before_data_or_child_creation() {
    use std::os::windows::process::CommandExt;
    let root = Directory::new();
    let executable = std::env::current_exe().unwrap();
    let mut command = std::process::Command::new(&executable);
    command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    command
        .args([
            "native_root_owner_fixture",
            "--exact",
            "--ignored",
            "--test-threads=1",
        ])
        .env("OFCA_ROOT_OWNER_TEST_DIRECTORY", &root.0)
        .env("OFCA_ROOT_OWNER_TEST_OLD", "1");
    let mut child = command.spawn().unwrap();
    assert_eq!(started(&root.0), child.id());
    let names =
        deployment::instance_names(&root.0, &windows::current_sid().unwrap(), None).unwrap();
    assert!(windows::claim_runtime_root(&names, &executable).is_err());
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(windows::claim_runtime_root(&names, &executable)
        .unwrap()
        .primary());
    assert!(!root.0.join("auth.sqlite3").exists());
    assert!(!root.0.join("webview").exists());
}

#[test]
fn registered_legacy_source_requires_explicit_transition_but_qualification_stays_isolated() {
    let root = Directory::new();
    let registered = root.0.join("registered");
    let candidate = root.0.join("candidate");
    std::fs::create_dir(&registered).unwrap();
    std::fs::create_dir(&candidate).unwrap();
    let own = registered.join("Bridge.exe");
    assert!(windows::validate_legacy_registration(&own, Some(&registered), false).is_ok());
    assert!(windows::validate_legacy_registration(
        &candidate.join("Bridge.exe"),
        Some(&registered),
        false
    )
    .is_err());
    assert!(windows::validate_legacy_registration(
        &candidate.join("Bridge.exe"),
        Some(&registered),
        true
    )
    .is_ok());
    assert!(
        windows::validate_legacy_registration(&own, Some(&root.0.join("missing")), false).is_err()
    );
    assert_eq!(std::fs::read_dir(&registered).unwrap().count(), 0);
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
fn servicing_deadline_preserves_containment_and_requires_actual_child_exit() {
    let data = Directory::new();
    std::fs::write(data.0.join("drain-delay"), b"500").unwrap();
    let image = Path::new(env!("CARGO_BIN_EXE_brain-fixture"))
        .canonicalize()
        .unwrap();
    let mut child = windows::Brain::start(&image, &data.0, "test-lifetime").unwrap();
    assert_eq!(started(&data.0), child.pid);
    assert_eq!(
        child.stop_gracefully(Duration::from_millis(10)).unwrap(),
        None
    );
    assert_eq!(
        child.exit_code().unwrap(),
        None,
        "servicing deadline killed the child"
    );
    assert_eq!(
        child.stop_gracefully(Duration::from_secs(2)).unwrap(),
        Some(0)
    );
    assert!(data.0.join("graceful").is_file());
}

#[test]
fn servicing_requires_drain_receipt_and_all_owned_processes_to_exit() {
    use std::sync::atomic::AtomicBool;
    let image = Path::new(env!("CARGO_BIN_EXE_brain-fixture"))
        .canonicalize()
        .unwrap();
    let good = b"{\"schema\":\"ofca-runtime-shutdown/v1\",\"after_seq\":3,\"drained\":true}\n";
    let refused = b"{\"schema\":\"ofca-runtime-shutdown/v1\",\"after_seq\":3,\"drained\":false}\n";
    for (tail, descendant, quitting, expected) in [
        (good.as_slice(), false, false, Ok(true)),
        (
            refused.as_slice(),
            false,
            false,
            Err("servicing_child_drain_unconfirmed"),
        ),
        (
            b"".as_slice(),
            false,
            false,
            Err("servicing_child_drain_unconfirmed"),
        ),
        (
            b"invalid\n".as_slice(),
            false,
            false,
            Err("runtime_shutdown_frame_invalid"),
        ),
        (
            good.as_slice(),
            true,
            false,
            Err("servicing_owned_processes_remain"),
        ),
        (good.as_slice(), false, true, Ok(false)),
    ] {
        let data = Directory::new();
        std::fs::write(data.0.join("servicing"), tail).unwrap();
        if descendant {
            std::fs::write(data.0.join("spawn-descendant"), b"owned child").unwrap();
        }
        let mut child = windows::Brain::start(&image, &data.0, "test-lifetime").unwrap();
        assert_eq!(started(&data.0), child.pid);
        let mut startup = bootstrap::Supervisor::new(Instant::now());
        let deadline = Instant::now() + Duration::from_secs(2);
        while !startup.ready() {
            let (bytes, _) = child.bootstrap_bytes().unwrap();
            startup.receive(&bytes, Instant::now()).unwrap();
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        let mut receipt = startup.shutdown_receipt().unwrap();
        let result = child.stop_for_servicing(
            &mut receipt,
            Duration::from_millis(500),
            &AtomicBool::new(quitting),
        );
        assert_eq!(result, expected);
        if descendant {
            assert_eq!(child.exit_code().unwrap(), Some(0));
            assert!(child
                .stop_for_servicing(
                    &mut receipt,
                    Duration::from_secs(2),
                    &AtomicBool::new(false)
                )
                .unwrap());
        }
        child.stop();
    }
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
    qualify_frozen_startup(None, false);
}

#[test]
#[ignore = "requires a newly frozen Brain with the runtime shutdown observation"]
fn frozen_brain_reports_owned_drain_before_servicing_exit() {
    qualify_frozen_startup(None, true);
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
    qualify_frozen_startup(Some(&older), false);
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
    if data.join(".qualification-upgrade-fixture.json").is_file() {
        command.arg("--governed-qualification-root");
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

fn installed_corrective_context(version: &str) -> (PathBuf, PathBuf, PathBuf) {
    assert!(std::env::var_os("LOCAL_ANALYTICS_DATA_DIR").is_none());
    let actual = windows::package_identity()
        .unwrap()
        .expect("actual registered package context required");
    assert_eq!(actual.name, "OFCA.Qualification.Direct");
    assert_eq!(actual.publisher, "CN=OFCA MSIX Qualification Direct");
    assert_eq!(actual.version, version);
    assert_eq!(actual.architecture, "x64");
    let image = PathBuf::from(std::env::var_os("BRAIN_QUALIFICATION_IMAGE").unwrap())
        .canonicalize()
        .unwrap();
    let release = deployment::load(
        &image.parent().unwrap().join("desktop-release.json"),
        Some(&actual),
    )
    .unwrap()
    .unwrap();
    assert_eq!(release.purpose, "qualification");
    let data = policy::data_directory(&image).unwrap();
    let expected = PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap())
        .join("OnlyFans Conversational Analytics/qualification/OFCA.Qualification.Direct")
        .canonicalize()
        .unwrap();
    assert_eq!(data, dunce::simplified(&expected));
    assert!(data.join(".qualification-upgrade-fixture.json").is_file());
    let receipt = PathBuf::from(std::env::var_os("BRAIN_QUALIFICATION_RECEIPT_DIRECTORY").unwrap());
    assert!(receipt.is_absolute() && receipt.is_dir());
    (image, data, receipt)
}

fn interrupt_before_listener(child: &mut windows::Brain, migrating_count: usize) {
    let mut supervisor = bootstrap::Supervisor::new(Instant::now());
    let mut frames = Vec::new();
    loop {
        assert_eq!(
            child.exit_code().unwrap(),
            None,
            "maintenance stopped before interruption"
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
        let migrations = frames
            .split(|b| *b == b'\n')
            .filter(|line| !line.is_empty())
            .filter(|line| {
                serde_json::from_slice::<serde_json::Value>(line).unwrap()["phase"] == "migrating"
            })
            .count();
        if migrations >= migrating_count {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(
        child.stop(),
        "pre-listener cancellation used forced Job cleanup"
    );
    // run_brain returns 1 when parent closure cancels pre-listener startup.
    assert_eq!(child.exit_code().unwrap(), Some(1));
    assert!(windows::listeners().unwrap().is_empty());
}

#[test]
#[ignore = "requires registered qualification package 1.0.6.0, claimed governed root and frozen A32; leaves interrupted evidence for C"]
fn installed_b_records_interrupted_intent_in_the_governed_root() {
    let (image, data, receipt) = installed_corrective_context("1.0.6.0");
    assert!(
        !data.join("auth.sqlite3").exists(),
        "cannot adopt existing installation"
    );
    let older = PathBuf::from(std::env::var_os("BRAIN_QUALIFICATION_A_IMAGE").unwrap())
        .canonicalize()
        .unwrap();
    let rows: usize = std::env::var("BRAIN_QUALIFICATION_ROWS")
        .unwrap_or_else(|_| "150000".into())
        .parse()
        .unwrap();
    assert!((100_000..=500_000).contains(&rows));
    let _installer_signal = windows::application_mutex().unwrap();
    let sid = windows::current_sid().unwrap();
    assert!(windows::listeners().unwrap().is_empty());
    let mut a = windows::Brain::start(
        &older,
        &data,
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
    let original = retained_fixture("seed", &data, Some(rows));
    assert_eq!(original["auth_version"], 32);
    assert_eq!(original["canonical_version"], 11);
    std::fs::write(
        receipt.join("original.json"),
        serde_json::to_vec_pretty(&original).unwrap(),
    )
    .unwrap();
    let mut b = windows::Brain::start(
        &image,
        &data,
        "qualification-native-handoff-00000000000000000000000001",
    )
    .unwrap();
    interrupt_before_listener(&mut b, 2);
    let publication = retained_fixture("publish", &data, None);
    assert_retained_work(&original, &publication);
    assert!(publication["publication"].is_string());
    assert_eq!(publication["auth_version"], 33);
    assert_eq!(publication["canonical_version"], 11);
    assert_eq!(publication["upgrade"]["schema"], "ofca-upgrade/v2");
    assert_eq!(publication["upgrade"]["original_kind"], "package");
    assert_eq!(publication["upgrade"]["original_version"], "1.0.6.0");
    assert_eq!(publication["upgrade"]["phase"], "intent");
    std::fs::write(
        receipt.join("interrupted-b.json"),
        serde_json::to_vec_pretty(&publication).unwrap(),
    )
    .unwrap();
}

#[test]
#[ignore = "requires registered qualification package 1.0.7.0 over interrupted B; tests durable successor intent then forward resume"]
fn installed_c_resumes_governed_state_after_durable_successor_intent() {
    let (image, data, receipt) = installed_corrective_context("1.0.7.0");
    let original: serde_json::Value =
        serde_json::from_slice(&std::fs::read(receipt.join("original.json")).unwrap()).unwrap();
    let publication: serde_json::Value =
        serde_json::from_slice(&std::fs::read(receipt.join("interrupted-b.json")).unwrap())
            .unwrap();
    let _installer_signal = windows::application_mutex().unwrap();
    let sid = windows::current_sid().unwrap();
    assert!(windows::listeners().unwrap().is_empty());
    let mut c = windows::Brain::start(
        &image,
        &data,
        "qualification-native-handoff-00000000000000000000000001",
    )
    .unwrap();
    interrupt_before_listener(&mut c, 1);
    let retargeted = retained_fixture("observe", &data, None);
    assert_retained_work(&original, &retargeted);
    assert_eq!(retargeted["publication"], publication["publication"]);
    assert_eq!(retargeted["canonical_version"], 11);
    assert_eq!(retargeted["upgrade"]["phase"], "intent");
    assert_eq!(retargeted["upgrade"]["original_version"], "1.0.6.0");
    assert_eq!(retargeted["upgrade"]["resume_version"], "1.0.7.0");
    assert_ne!(
        retargeted["upgrade"]["resume_target"],
        publication["upgrade"]["resume_target"]
    );
    for key in [
        "original_target",
        "checkpoint_inventory",
        "checkpoint_manifest_sha256",
        "original_schemas",
    ] {
        assert_eq!(
            retargeted["upgrade"][key], publication["upgrade"][key],
            "original checkpoint changed: {key}"
        );
    }
    std::fs::write(
        receipt.join("interrupted-c.json"),
        serde_json::to_vec_pretty(&retargeted).unwrap(),
    )
    .unwrap();
    for cycle in 0..2 {
        let started = Instant::now();
        let mut c = windows::Brain::start(
            &image,
            &data,
            "qualification-native-handoff-00000000000000000000000001",
        )
        .unwrap();
        let mut supervisor = bootstrap::Supervisor::new(started);
        let mut frames = Vec::new();
        loop {
            assert_eq!(c.exit_code().unwrap(), None, "successor failed to resume");
            let (bytes, closed) = c.bootstrap_bytes().unwrap();
            assert!(!closed);
            supervisor.receive(&bytes, Instant::now()).unwrap();
            frames.extend_from_slice(&bytes);
            if policy::verify_listener(&windows::listeners().unwrap(), c.pid, &image, &sid).unwrap()
            {
                assert!(supervisor.ready());
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        println!(
            "installed corrective resume cycle {cycle}: {:.3}s",
            started.elapsed().as_secs_f64()
        );
        assert!(!String::from_utf8(frames)
            .unwrap()
            .contains("\"phase\":\"checkpointing\""));
        assert!(!data.join("upgrade-checkpoint").exists());
        assert!(c.stop(), "healthy successor used forced Job cleanup");
        let completed = retained_fixture("observe", &data, None);
        assert_retained_work(&original, &completed);
        assert_eq!(completed["publication"], publication["publication"]);
        assert_eq!(completed["canonical_version"], 12);
        assert_eq!(completed["upgrade"]["phase"], "complete");
        assert_eq!(completed["upgrade"]["resume_version"], "1.0.7.0");
        std::fs::write(
            receipt.join(format!("completed-c-{cycle}.json")),
            serde_json::to_vec_pretty(&completed).unwrap(),
        )
        .unwrap();
    }
}

#[test]
#[ignore = "requires the recorded interrupted B fixture and its original receipts; never seeds or replaces retained authority"]
fn installed_b_reinterrupts_only_its_recorded_fixture() {
    let (image, data, receipt) = installed_corrective_context("1.0.6.0");
    let original: serde_json::Value =
        serde_json::from_slice(&std::fs::read(receipt.join("original.json")).unwrap()).unwrap();
    let failed: serde_json::Value =
        serde_json::from_slice(&std::fs::read(receipt.join("after-failed-b.json")).unwrap())
            .unwrap();
    let current = retained_fixture("observe", &data, None);
    assert_retained_work(&original, &current);
    assert_eq!(current, failed, "recorded fixture changed after inspection");
    assert_eq!(current["auth_version"], 33);
    assert_eq!(current["canonical_version"], 11);
    assert_eq!(current["upgrade"]["schema"], "ofca-upgrade/v2");
    assert_eq!(current["upgrade"]["original_version"], "1.0.6.0");
    assert_eq!(current["upgrade"]["resume_version"], "1.0.6.0");
    assert_eq!(current["upgrade"]["phase"], "intent");
    let _installer_signal = windows::application_mutex().unwrap();
    assert!(windows::listeners().unwrap().is_empty());
    let mut b = windows::Brain::start(
        &image,
        &data,
        "qualification-native-handoff-00000000000000000000000001",
    )
    .unwrap();
    interrupt_before_listener(&mut b, 1);
    let publication = retained_fixture("publish", &data, None);
    assert_retained_work(&original, &publication);
    assert_eq!(publication["canonical_version"], 11);
    assert!(publication["publication"].is_string());
    assert_eq!(
        publication["upgrade"], current["upgrade"],
        "same-target resume replaced checkpoint evidence"
    );
    std::fs::write(
        receipt.join("interrupted-b.json"),
        serde_json::to_vec_pretty(&publication).unwrap(),
    )
    .unwrap();
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

fn qualify_frozen_startup(older: Option<&Path>, servicing: bool) {
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
        if servicing {
            let mut receipt = observation.shutdown_receipt().unwrap();
            assert!(
                child
                    .stop_for_servicing(
                        &mut receipt,
                        Duration::from_secs(15),
                        &std::sync::atomic::AtomicBool::new(false)
                    )
                    .unwrap(),
                "frozen child did not prove drained owners and actual process exit"
            );
            assert!(receipt.confirmed());
        } else {
            assert!(
                child.stop(),
                "healthy frozen maintenance used forced Job cleanup"
            );
        }
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
