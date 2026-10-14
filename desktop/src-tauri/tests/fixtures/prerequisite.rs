//! Real native recovery controls; qualification-only and never packaged.
#![allow(dead_code)]
#[path = "../../src/bootstrap.rs"]
mod bootstrap;
#[path = "../../src/deployment.rs"]
mod deployment;
#[path = "../../src/policy.rs"]
mod policy;
#[path = "../../src/prerequisite.rs"]
mod prerequisite;
#[path = "../../src/windows.rs"]
mod windows;

use prerequisite::{Platform, RecoveryAction, RecoveryReason, Version};
use std::time::{Duration, Instant};
use windows_sys::Win32::{
    Foundation::*,
    UI::{Controls::TDM_CLICK_BUTTON, WindowsAndMessaging::*},
};

struct Search {
    pid: u32,
    hwnd: usize,
}
unsafe extern "system" fn find(hwnd: HWND, data: isize) -> i32 {
    let mut pid = 0;
    GetWindowThreadProcessId(hwnd, &mut pid);
    let name: Vec<u16> = prerequisite::RECOVERY_STATE
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let search = &mut *(data as *mut Search);
    if pid == search.pid && !GetPropW(hwnd, name.as_ptr()).is_null() {
        search.hwnd = hwnd as usize;
        return 0;
    }
    1
}

fn close_conflict() {
    unsafe extern "system" fn find(hwnd: HWND, data: isize) -> i32 {
        let mut pid = 0;
        GetWindowThreadProcessId(hwnd, &mut pid);
        let marker: Vec<u16> = prerequisite::INSTALLATION_CONFLICT_STATE
            .encode_utf16()
            .chain(Some(0))
            .collect();
        if pid == std::process::id() && !GetPropW(hwnd, marker.as_ptr()).is_null() {
            *(data as *mut usize) = hwnd as usize;
            return 0;
        }
        1
    }
    unsafe extern "system" fn labels(hwnd: HWND, data: isize) -> i32 {
        let observed = &mut *(data as *mut [bool; 2]);
        let mut caption = [0u16; 1024];
        let length = GetWindowTextW(hwnd, caption.as_mut_ptr(), caption.len() as i32);
        let mut class = [0u16; 64];
        let count = GetClassNameW(hwnd, class.as_mut_ptr(), class.len() as i32);
        let kind = String::from_utf16_lossy(&class[..count.max(0) as usize]);
        if length > 0 {
            observed[if kind == "Button" { 0 } else { 1 }] = true;
        }
        1
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let mut found = 0usize;
        unsafe {
            EnumWindows(Some(find), &mut found as *mut usize as isize);
        }
        if found != 0 {
            let mut observed = [false; 2];
            unsafe {
                EnumChildWindows(
                    found as HWND,
                    Some(labels),
                    &mut observed as *mut [bool; 2] as isize,
                );
            }
            if observed == [true; 2] {
                unsafe {
                    SendMessageW(found as HWND, TDM_CLICK_BUTTON as u32, IDCLOSE as usize, 0);
                }
                return;
            }
        }
        assert!(
            Instant::now() < deadline,
            "installation conflict controls and guidance were not rendered"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn click_dialog(pid: u32, button: i32) {
    let deadline = Instant::now() + Duration::from_secs(10);
    let hwnd = loop {
        let mut found = Search { pid, hwnd: 0 };
        unsafe {
            EnumWindows(Some(find), &mut found as *mut Search as isize);
        }
        if found.hwnd != 0 {
            break found.hwnd as HWND;
        }
        assert!(
            Instant::now() < deadline,
            "native prerequisite dialog was not rendered"
        );
        std::thread::sleep(Duration::from_millis(10));
    };
    // TaskDialog's semantic button IDs do not promise matching child HWND
    // IDs. Observe the actual command-link captions after construction.
    loop {
        let mut labels = [false; 2];
        unsafe {
            EnumChildWindows(
                hwnd,
                Some(observe_labels),
                &mut labels as *mut [bool; 2] as isize,
            );
        }
        if labels == [true; 2] {
            break;
        }
        if Instant::now() >= deadline {
            unsafe {
                PostMessageW(hwnd, WM_CLOSE, 0, 0);
            }
            panic!("native recovery command links were not rendered");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    unsafe {
        SendMessageW(hwnd, TDM_CLICK_BUTTON as u32, button as usize, 0);
    }
}

unsafe extern "system" fn observe_labels(hwnd: HWND, data: isize) -> i32 {
    let mut text = [0u16; 1024];
    let count = GetWindowTextW(hwnd, text.as_mut_ptr(), text.len() as i32);
    let text = String::from_utf16_lossy(&text[..count.max(0) as usize]);
    let labels = &mut *(data as *mut [bool; 2]);
    labels[0] |= text.starts_with("Get WebView2");
    labels[1] |= text.starts_with("Check again");
    1
}

struct Qualification {
    scenario: String,
    probes: usize,
    dialogs: usize,
    downloads: usize,
}
impl Platform for Qualification {
    fn available_version(&mut self) -> Result<String, ()> {
        self.probes += 1;
        match self.scenario.as_str() {
            "loader-missing" | "loader-present" => {
                prerequisite::WindowsPlatform.available_version()
            }
            "retry" if self.probes == 2 => Ok("134.0.3124.0".into()),
            "too-old" => Ok("133.0.0.0".into()),
            "preview" => Ok("156.0.0.0 dev".into()),
            "retry" | "acquire" => Err(()),
            _ => panic!("closed qualification scenario"),
        }
    }
    fn recover(&mut self, reason: RecoveryReason) -> Result<RecoveryAction, &'static str> {
        self.dialogs += 1;
        let button = match self.scenario.as_str() {
            "retry" => 1002,
            "acquire" => 1001,
            _ => IDCANCEL,
        };
        let click = std::thread::spawn(move || click_dialog(std::process::id(), button));
        let result = prerequisite::show_recovery(reason);
        click.join().expect("native control observation");
        result
    }
    fn acquire_runtime(&mut self) -> Result<(), &'static str> {
        // Observe the purpose-specific action without opening a real browser
        // or installing a runtime during an automated qualification test.
        self.downloads += 1;
        Ok(())
    }
}

fn main() {
    let scenario = std::env::args().nth(1).unwrap();
    if scenario == "installation-conflict" {
        let closer = std::thread::spawn(close_conflict);
        prerequisite::show_failure("runtime_installation_conflict");
        closer.join().unwrap();
        println!(
            "{}",
            serde_json::json!({"state":prerequisite::INSTALLATION_CONFLICT_STATE,"close_and_guidance_observed":true})
        );
        return;
    }
    if scenario == "--close-bridge" {
        let pid = std::env::args().nth(2).unwrap().parse().unwrap();
        click_dialog(pid, IDCANCEL);
        return;
    }
    let mut platform = Qualification {
        scenario,
        probes: 0,
        dialogs: 0,
        downloads: 0,
    };
    let result = prerequisite::ensure_ready(Version::parse("134.0.3124.0").unwrap(), &mut platform);
    let admitted = result.expect("native prerequisite operation");
    println!(
        "{}",
        serde_json::json!({"schema":"ofca-prerequisite-qualification/v1",
        "scenario":platform.scenario,"ready":admitted,"probes":platform.probes,
        "dialogs":platform.dialogs,"acquisition_requested":platform.downloads,
        "real_loader_probe":platform.scenario.starts_with("loader-"),"native_controls_observed":platform.dialogs})
    );
}
