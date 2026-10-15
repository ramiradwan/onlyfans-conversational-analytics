//! Native prerequisite recovery before Tauri, presentation storage or Brain.
use serde::Deserialize;
use std::path::Path;

pub const RECOVERY_STATE: &str = "desktop.webview_runtime_needed";
const GET_RUNTIME: i32 = 1001;
const CHECK_AGAIN: i32 = 1002;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Policy {
    schema: String,
    webview2_minimum_version: String,
    webview2_acquisition_uri: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Version([u16; 4]);
impl Version {
    pub fn parse(value: &str) -> Result<Self, &'static str> {
        let mut parts = [0; 4];
        let mut input = value.split('.');
        for component in &mut parts {
            let text = input.next().ok_or("webview_version_invalid")?;
            *component = text.parse().map_err(|_| "webview_version_invalid")?;
            if component.to_string() != text {
                return Err("webview_version_invalid");
            }
        }
        if input.next().is_some() || parts[0] == 0 {
            return Err("webview_version_invalid");
        }
        Ok(Self(parts))
    }
}

fn policy() -> Result<Policy, &'static str> {
    let policy: Policy = serde_json::from_str(include_str!(
        "../../../shared/desktop-release/prerequisite-policy.json"
    ))
    .map_err(|_| "prerequisite_policy_invalid")?;
    if policy.schema != "ofca-desktop-prerequisite-policy/v1" {
        return Err("prerequisite_policy_invalid");
    }
    Version::parse(&policy.webview2_minimum_version)?;
    // This embedded approved source is never selected by a renderer or URI.
    let uri = url::Url::parse(&policy.webview2_acquisition_uri)
        .map_err(|_| "prerequisite_policy_invalid")?;
    if uri.scheme() != "https"
        || uri.as_str() != policy.webview2_acquisition_uri
        || uri.host_str() != Some("developer.microsoft.com")
        || uri.path() != "/en-us/microsoft-edge/webview2/consumer/"
        || !uri.username().is_empty()
        || uri.password().is_some()
        || uri.port().is_some()
        || uri.query().is_some()
        || uri.fragment().is_some()
    {
        return Err("prerequisite_policy_invalid");
    }
    Ok(policy)
}

pub fn required_version(image: &Path) -> Result<Version, &'static str> {
    let baseline = Version::parse(&policy()?.webview2_minimum_version)?;
    let identity = crate::windows::package_identity()?;
    let release = crate::deployment::load(
        &image
            .parent()
            .ok_or("invalid_installation")?
            .join("desktop-release.json"),
        identity.as_ref(),
    )?;
    match release {
        Some(release) => Ok(baseline.max(Version::parse(&release.webview2_minimum_version)?)),
        None => Ok(baseline),
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecoveryReason {
    Missing,
    TooOld,
    Unavailable,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecoveryAction {
    GetRuntime,
    CheckAgain,
    Close,
}

pub trait Platform {
    fn available_version(&mut self) -> Result<String, ()>;
    fn recover(&mut self, reason: RecoveryReason) -> Result<RecoveryAction, &'static str>;
    // No URL, executable, command, profile or filesystem arguments.
    fn acquire_runtime(&mut self) -> Result<(), &'static str>;
}

pub fn ensure_ready(required: Version, platform: &mut impl Platform) -> Result<bool, &'static str> {
    loop {
        let reason = match platform.available_version() {
            Ok(value) => match Version::parse(&value) {
                Ok(version) if version >= required => return Ok(true),
                Ok(_) => RecoveryReason::TooOld,
                Err(_) => RecoveryReason::Unavailable,
            },
            Err(_) => RecoveryReason::Missing,
        };
        match platform.recover(reason)? {
            RecoveryAction::CheckAgain => (),
            RecoveryAction::GetRuntime => {
                platform.acquire_runtime()?;
                // Acquisition is not installation proof. Reopen after install.
                return Ok(false);
            }
            RecoveryAction::Close => return Ok(false),
        }
    }
}

pub struct WindowsPlatform;
impl Platform for WindowsPlatform {
    fn available_version(&mut self) -> Result<String, ()> {
        // Tauri's pinned Wry loader calls the real Microsoft version API.
        tauri::webview_version().map_err(|_| ())
    }
    fn recover(&mut self, reason: RecoveryReason) -> Result<RecoveryAction, &'static str> {
        show_recovery(reason)
    }
    fn acquire_runtime(&mut self) -> Result<(), &'static str> {
        use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};
        let uri = wide(&policy()?.webview2_acquisition_uri);
        let verb = wide("open");
        let result = unsafe {
            ShellExecuteW(
                std::ptr::null_mut(),
                verb.as_ptr(),
                uri.as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                SW_SHOWNORMAL,
            )
        };
        if result as isize <= 32 {
            Err("webview_acquisition_unavailable")
        } else {
            Ok(())
        }
    }
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}

unsafe extern "system" fn recovery_callback(
    hwnd: windows_sys::Win32::Foundation::HWND,
    message: u32,
    _: usize,
    _: isize,
    _: isize,
) -> i32 {
    use windows_sys::Win32::UI::{
        Controls::{TDN_CREATED, TDN_DESTROYED},
        WindowsAndMessaging::{RemovePropW, SetPropW},
    };
    if message == TDN_CREATED as u32 {
        // Executable journey anchor on the actual native dialog.
        SetPropW(hwnd, wide(RECOVERY_STATE).as_ptr(), 1usize as _);
    } else if message == TDN_DESTROYED as u32 {
        RemovePropW(hwnd, wide(RECOVERY_STATE).as_ptr());
    }
    0
}

pub fn show_recovery(reason: RecoveryReason) -> Result<RecoveryAction, &'static str> {
    use windows_sys::Win32::UI::{Controls::*, WindowsAndMessaging::IDCANCEL};
    let title = wide("Conversation Analytics");
    let instruction = wide(match reason {
        RecoveryReason::Missing => "Install Microsoft Edge WebView2 to open the app",
        RecoveryReason::TooOld => "Update Microsoft Edge WebView2 to open the app",
        RecoveryReason::Unavailable => "Check your Microsoft Edge WebView2 installation",
    });
    let content = wide("WebView2 displays the app window. Install Microsoft's Evergreen Runtime, then open Conversation Analytics again.\n\nIf this computer is offline or installation is managed, ask your administrator for the offline installer. If you have already installed it, choose Check again.");
    let acquire = wide("Get WebView2\nOpen Microsoft's download page, then close the app");
    let retry = wide("Check again\nCheck the installed runtime before opening the app");
    let buttons = [
        TASKDIALOG_BUTTON {
            nButtonID: GET_RUNTIME,
            pszButtonText: acquire.as_ptr(),
        },
        TASKDIALOG_BUTTON {
            nButtonID: CHECK_AGAIN,
            pszButtonText: retry.as_ptr(),
        },
    ];
    let config = TASKDIALOGCONFIG {
        cbSize: std::mem::size_of::<TASKDIALOGCONFIG>() as u32,
        pszWindowTitle: title.as_ptr(),
        pszMainInstruction: instruction.as_ptr(),
        pszContent: content.as_ptr(),
        dwFlags: TDF_USE_COMMAND_LINKS | TDF_ALLOW_DIALOG_CANCELLATION,
        dwCommonButtons: TDCBF_CANCEL_BUTTON,
        cButtons: buttons.len() as u32,
        pButtons: buttons.as_ptr(),
        nDefaultButton: CHECK_AGAIN,
        pfCallback: Some(recovery_callback),
        ..Default::default()
    };
    let mut selection = 0;
    let result = unsafe {
        TaskDialogIndirect(
            &config,
            &mut selection,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if result < 0 {
        return Err("webview_recovery_unavailable");
    }
    match selection {
        GET_RUNTIME => Ok(RecoveryAction::GetRuntime),
        CHECK_AGAIN => Ok(RecoveryAction::CheckAgain),
        IDCANCEL => Ok(RecoveryAction::Close),
        _ => Err("webview_recovery_unavailable"),
    }
}

pub fn show_failure(reason: &'static str) {
    if reason == "runtime_installation_conflict" {
        show_installation_conflict();
        return;
    }
    use windows_sys::Win32::UI::{Controls::*, WindowsAndMessaging::*};
    let (heading, message) = match reason {
        "data_directory_unavailable" | "invalid_data_directory" | "servicing_storage_invalid" | "servicing_intent_invalid" => (
            "Your data folder is unavailable",
            "Close Conversation Analytics and ask your administrator to check your data folder's permissions and location. Keep your existing data and keys. After access is restored, reopen the app.",
        ),
        "servicing_pending" => ("Finishing the update", "Conversation Analytics is finishing an update. Wait a moment, then open it again from the Start menu. Keep your existing data and keys."),
        "webview_acquisition_unavailable" => ("WebView2 could not be acquired", "Microsoft's download page could not be opened. Use your browser to get Microsoft Edge WebView2 from Microsoft's website, or ask your administrator. After installation, open Conversation Analytics again."),
        "webview_recovery_unavailable" => ("WebView2 recovery is unavailable", "Conversation Analytics could not open its WebView2 recovery window. Get Microsoft Edge WebView2 from Microsoft's website or ask your administrator, then open the app again."),
        _ => ("This installation could not be verified", "Conversation Analytics could not verify this installation. Repair or reinstall the app from its approved source, or contact your administrator."),
    };
    let title = wide("Conversation Analytics");
    let heading = wide(heading);
    let message = wide(message);
    // The property contains only a source-selected static error code. No
    // paths, activation arguments, account data or credentials are exposed.
    let code = wide(&format!("OFCA.StartupFailure.{reason}"));
    let mut config: TASKDIALOGCONFIG = unsafe { std::mem::zeroed() };
    config.cbSize = std::mem::size_of::<TASKDIALOGCONFIG>() as u32;
    config.pszWindowTitle = title.as_ptr();
    config.pszMainInstruction = heading.as_ptr();
    config.pszContent = message.as_ptr();
    config.dwCommonButtons = TDCBF_CLOSE_BUTTON;
    config.pfCallback = Some(failure_callback);
    config.lpCallbackData = code.as_ptr() as isize;
    if unsafe {
        TaskDialogIndirect(
            &config,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    } < 0
    {
        unsafe {
            MessageBoxW(
                std::ptr::null_mut(),
                message.as_ptr(),
                title.as_ptr(),
                MB_OK | MB_ICONERROR,
            );
        }
    }
}

pub const FAILURE_STATE: &str = "desktop.installation_unavailable";

unsafe extern "system" fn failure_callback(
    hwnd: windows_sys::Win32::Foundation::HWND,
    message: u32,
    _: usize,
    _: isize,
    data: isize,
) -> i32 {
    use windows_sys::Win32::UI::{
        Controls::{TDN_CREATED, TDN_DESTROYED},
        WindowsAndMessaging::{RemovePropW, SetPropW},
    };
    let code = data as *const u16;
    if message == TDN_CREATED as u32 {
        SetPropW(hwnd, wide(FAILURE_STATE).as_ptr(), 1usize as _);
        SetPropW(hwnd, code, 1usize as _);
    }
    if message == TDN_DESTROYED as u32 {
        RemovePropW(hwnd, wide(FAILURE_STATE).as_ptr());
        RemovePropW(hwnd, code);
    }
    0
}

pub const INSTALLATION_CONFLICT_STATE: &str = "desktop.installation_conflict";

unsafe extern "system" fn conflict_callback(
    hwnd: windows_sys::Win32::Foundation::HWND,
    message: u32,
    _: usize,
    _: isize,
    _: isize,
) -> i32 {
    use windows_sys::Win32::UI::{
        Controls::{TDN_CREATED, TDN_DESTROYED},
        WindowsAndMessaging::{RemovePropW, SetPropW},
    };
    if message == TDN_CREATED as u32 {
        SetPropW(
            hwnd,
            wide(INSTALLATION_CONFLICT_STATE).as_ptr(),
            1usize as _,
        );
    }
    if message == TDN_DESTROYED as u32 {
        RemovePropW(hwnd, wide(INSTALLATION_CONFLICT_STATE).as_ptr());
    }
    0
}

fn show_installation_conflict() {
    use windows_sys::Win32::UI::{Controls::*, WindowsAndMessaging::*};
    let title = wide("Conversation Analytics");
    let heading = wide("Another installation may be using your data folder");
    let guidance = wide("Quit the other Conversation Analytics installation from its tray menu, then reopen this one. If it is still blocked, use your existing installation and contact support. Your files were left unchanged.");
    let mut config: TASKDIALOGCONFIG = unsafe { std::mem::zeroed() };
    config.cbSize = std::mem::size_of::<TASKDIALOGCONFIG>() as u32;
    config.pszWindowTitle = title.as_ptr();
    config.pszMainInstruction = heading.as_ptr();
    config.pszContent = guidance.as_ptr();
    config.dwCommonButtons = TDCBF_CLOSE_BUTTON;
    config.pfCallback = Some(conflict_callback);
    if unsafe {
        TaskDialogIndirect(
            &config,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    } < 0
    {
        unsafe {
            MessageBoxW(
                std::ptr::null_mut(),
                guidance.as_ptr(),
                title.as_ptr(),
                MB_OK | MB_ICONERROR,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    struct Fake {
        versions: VecDeque<Result<String, ()>>,
        actions: VecDeque<RecoveryAction>,
        reasons: Vec<RecoveryReason>,
        downloads: usize,
    }
    impl Platform for Fake {
        fn available_version(&mut self) -> Result<String, ()> {
            self.versions.pop_front().expect("bounded test probe")
        }
        fn recover(&mut self, reason: RecoveryReason) -> Result<RecoveryAction, &'static str> {
            self.reasons.push(reason);
            Ok(self.actions.pop_front().expect("explicit recovery action"))
        }
        fn acquire_runtime(&mut self) -> Result<(), &'static str> {
            self.downloads += 1;
            Ok(())
        }
    }
    fn fake(versions: Vec<Result<&str, ()>>, actions: Vec<RecoveryAction>) -> Fake {
        Fake {
            versions: versions.into_iter().map(|v| v.map(str::to_owned)).collect(),
            actions: actions.into(),
            reasons: vec![],
            downloads: 0,
        }
    }
    #[test]
    fn readiness_requires_a_compatible_runtime_not_a_preview_channel_or_lexical_version() {
        let minimum = Version::parse("134.0.3124.0").unwrap();
        for (text, admitted) in [
            ("134.0.3124.0", true),
            ("134.0.3124.1", true),
            ("135.0.0.0", true),
            ("99.0.0.0", false),
            ("134.0.3123.999", false),
            ("156.0.0.0 dev", false),
            ("", false),
            ("0.0.0.0", false),
        ] {
            let mut platform = fake(vec![Ok(text)], vec![RecoveryAction::Close]);
            assert_eq!(ensure_ready(minimum, &mut platform).unwrap(), admitted);
            assert_eq!(platform.downloads, 0);
        }
    }
    #[test]
    fn readiness_is_rechecked_before_launch_and_download_is_only_a_fixed_action() {
        let minimum = Version::parse(&policy().unwrap().webview2_minimum_version).unwrap();
        let mut retry = fake(
            vec![Err(()), Ok("134.0.3123.0"), Ok("134.0.3124.0")],
            vec![RecoveryAction::CheckAgain, RecoveryAction::CheckAgain],
        );
        assert!(ensure_ready(minimum, &mut retry).unwrap());
        assert_eq!(
            retry.reasons,
            [RecoveryReason::Missing, RecoveryReason::TooOld]
        );
        let mut download = fake(vec![Err(())], vec![RecoveryAction::GetRuntime]);
        assert!(!ensure_ready(minimum, &mut download).unwrap());
        assert_eq!(download.downloads, 1);
    }
}
