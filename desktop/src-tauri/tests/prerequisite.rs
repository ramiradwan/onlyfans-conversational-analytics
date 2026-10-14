#![cfg(all(windows, feature = "qualification-tools"))]
use std::os::windows::process::CommandExt;
use std::{
    process::Command,
    time::{Duration, Instant},
};
use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;

fn run(scenario: &str) -> serde_json::Value {
    let mut command = Command::new(env!("CARGO_BIN_EXE_prerequisite-fixture"));
    command.creation_flags(CREATE_NO_WINDOW);
    command.arg(scenario);
    if scenario == "loader-missing" {
        // Real Microsoft loader failure, isolated to this child. Do not
        // uninstall or alter the machine's shared Evergreen registration.
        command.env(
            "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
            std::env::temp_dir().join(format!("ofca-runtime-absent-{}", std::process::id())),
        );
    } else {
        command.env_remove("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER");
        command.env_remove("WEBVIEW2_RELEASE_CHANNEL_PREFERENCE");
    }
    command
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut child = command.spawn().unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if child.try_wait().unwrap().is_some() {
            break;
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            let output = child.wait_with_output().unwrap();
            panic!(
                "owned prerequisite fixture {scenario} did not finish: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn real_native_installation_conflict_has_close_and_guidance() {
    let value = run("installation-conflict");
    assert_eq!(value["state"], "desktop.installation_conflict");
    assert_eq!(value["close_and_guidance_observed"], true);
}

#[test]
fn real_native_recovery_controls_cover_loader_failure_retry_download_and_close() {
    for scenario in ["loader-missing", "too-old", "preview", "retry", "acquire"] {
        let value = run(scenario);
        assert_eq!(value["ready"], scenario == "retry");
        assert_eq!(value["dialogs"], 1);
        assert_eq!(value["native_controls_observed"], 1);
        assert_eq!(
            value["acquisition_requested"],
            if scenario == "acquire" { 1 } else { 0 }
        );
        if scenario == "loader-missing" {
            assert_eq!(value["real_loader_probe"], true);
        }
    }
}

#[test]
#[ignore = "requires this qualification machine's installed compatible Evergreen WebView2"]
fn real_installed_runtime_is_checked_without_opening_a_recovery_dialog() {
    let value = run("loader-present");
    assert_eq!(value["ready"], true);
    assert_eq!(value["real_loader_probe"], true);
    assert_eq!(value["dialogs"], 0);
}

#[test]
fn real_bridge_refuses_missing_runtime_before_creating_a_webview_profile_or_brain_state() {
    let data = std::env::temp_dir().join(format!(
        "ofca-native-prerequisite-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    assert!(!data.exists());
    let image = std::env::var_os("BRIDGE_QUALIFICATION_IMAGE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| env!("CARGO_BIN_EXE_Bridge").into());
    let mut bridge = Command::new(image)
        .creation_flags(CREATE_NO_WINDOW)
        .env("LOCAL_ANALYTICS_DATA_DIR", &data)
        .env(
            "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
            data.join("missing-runtime"),
        )
        .spawn()
        .unwrap();
    let mut driver = Command::new(env!("CARGO_BIN_EXE_prerequisite-fixture"))
        .creation_flags(CREATE_NO_WINDOW)
        .arg("--close-bridge")
        .arg(bridge.id().to_string())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    while driver.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            driver.kill().unwrap();
            bridge.kill().unwrap();
            let output = driver.wait_with_output().unwrap();
            bridge.wait().unwrap();
            panic!(
                "native Bridge recovery was not observable: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let output = driver.wait_with_output().unwrap();
    if !output.status.success() {
        bridge.kill().unwrap();
        bridge.wait().unwrap();
    }
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    while bridge.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            bridge.kill().unwrap();
            bridge.wait().unwrap();
            panic!("Bridge did not close after prerequisite refusal");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(
        !data.exists(),
        "missing-runtime launch created presentation or Brain authority"
    );
}
