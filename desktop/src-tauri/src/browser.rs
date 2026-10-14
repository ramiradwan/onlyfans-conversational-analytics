use crate::{host, policy};
use std::{path::PathBuf, sync::atomic::Ordering};

fn task_url(code: &str) -> Result<String, &'static str> {
    entry_url(code, false)
}

fn entry_url(code: &str, receiving: bool) -> Result<String, &'static str> {
    if code.len() != 43
        || !code
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
    {
        return Err("browser_entry_invalid");
    }
    Ok(format!(
        "{}/{}/entry#code={code}",
        policy::ORIGIN,
        if receiving {
            "setup-code"
        } else {
            "browser-tasks"
        }
    ))
}

/// Only the installed Chrome executable; renderer inputs never select a profile,
/// executable, command line, or destination. Chrome owns profile selection.
pub(crate) fn chrome_path() -> Result<PathBuf, &'static str> {
    for root in [
        "LOCALAPPDATA",
        "ProgramW6432",
        "ProgramFiles",
        "ProgramFiles(x86)",
    ] {
        if let Some(root) = std::env::var_os(root) {
            let path = PathBuf::from(root).join("Google/Chrome/Application/chrome.exe");
            if path.is_absolute() && path.is_file() {
                return Ok(path);
            }
        }
    }
    Err("chrome_unavailable")
}

/// Re-admit the same native workspace; this cannot renew a domain operation.
#[tauri::command]
pub async fn desktop_refresh_presentation(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
    generation: String,
) -> Result<String, &'static str> {
    if window.label() != "main"
        || !policy::local_document(&window.url().map_err(|_| "window_unavailable")?)
        || !state.admitted.load(Ordering::SeqCst)
        || state.quitting.load(Ordering::SeqCst)
        || uuid::Uuid::parse_str(&generation).is_err()
    {
        return Err("desktop_admission_required");
    }
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    state
        .actions
        .try_send(host::Action::RefreshPresentation(generation, sender))
        .map_err(|_| "handoff_unavailable")?;
    tauri::async_runtime::spawn_blocking(move || {
        receiver
            .recv_timeout(std::time::Duration::from_secs(10))
            .map_err(|_| "handoff_unavailable")?
    })
    .await
    .map_err(|_| "handoff_unavailable")?
}

#[tauri::command]
pub fn desktop_browser_task(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
    entry_code: String,
) -> Result<(), &'static str> {
    open_entry(window, state, task_url(&entry_code)?)
}

#[tauri::command]
pub fn desktop_setup_code(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
    entry_code: String,
) -> Result<(), &'static str> {
    open_entry(window, state, entry_url(&entry_code, true)?)
}

fn open_entry(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
    url: String,
) -> Result<(), &'static str> {
    if window.label() != "main"
        || !policy::local_document(&window.url().map_err(|_| "window_unavailable")?)
        || !state.admitted.load(Ordering::SeqCst)
        || state.quitting.load(Ordering::SeqCst)
    {
        return Err("desktop_admission_required");
    }
    std::process::Command::new(chrome_path()?)
        .arg(url)
        .spawn()
        .map_err(|_| "chrome_launch_failed")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn browser_command_only_opens_the_fixed_scoped_landing() {
        assert_eq!(
            entry_url(&"A".repeat(43), true).unwrap(),
            format!(
                "{}/setup-code/entry#code={}",
                policy::ORIGIN,
                "A".repeat(43)
            )
        );
        assert_eq!(
            task_url(&"A".repeat(43)).unwrap(),
            format!(
                "{}/browser-tasks/entry#code={}",
                policy::ORIGIN,
                "A".repeat(43)
            )
        );
        for value in [
            "https://example.com",
            "--profile-directory=Default",
            "x&url=http://evil",
            "",
            "A\n",
        ] {
            assert!(task_url(value).is_err());
            assert!(entry_url(value, true).is_err());
        }
        assert!(task_url(&"A".repeat(44)).is_err());
    }
}
