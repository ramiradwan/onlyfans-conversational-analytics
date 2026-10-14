use crate::{host::Host, policy};
use std::sync::{atomic::Ordering, Arc};

pub fn show_main(app: &tauri::AppHandle) {
    use tauri::Manager;
    if let Some(window) = app.get_webview_window("main") {
        if window.is_minimized().unwrap_or(false) {
            let _ = window.unminimize();
        }
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WindowAction {
    State,
    Minimize,
    ToggleMaximize,
    Close,
    StartDragging,
    Focus,
}

/// Window presentation only; never a process, navigation or filesystem command.
#[tauri::command]
pub fn desktop_window(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, Arc<Host>>,
    action: WindowAction,
) -> Result<bool, &'static str> {
    let url = window.url().map_err(|_| "window_unavailable")?;
    if !policy::window_controls(
        window.label(),
        &url,
        state.admitted.load(Ordering::SeqCst),
        state.quitting.load(Ordering::SeqCst),
    ) {
        return Err("window_control_denied");
    }
    let result = match action {
        WindowAction::State => Ok(()),
        WindowAction::Minimize => window.minimize(),
        WindowAction::ToggleMaximize => {
            if window.is_maximized().map_err(|_| "window_unavailable")? {
                window.unmaximize()
            } else {
                window.maximize()
            }
        }
        WindowAction::Close => window.hide(),
        WindowAction::StartDragging => window.start_dragging(),
        WindowAction::Focus => {
            if !window.is_focused().unwrap_or(false)
                && !crate::browser::chrome_path()
                    .is_ok_and(|path| crate::windows::foreground_matches(&path))
            {
                return Err("window_focus_deferred");
            }
            if window.is_minimized().map_err(|_| "window_unavailable")? {
                window.unminimize().map_err(|_| "window_unavailable")?;
            }
            window.show().map_err(|_| "window_unavailable")?;
            window.set_focus()
        }
    };
    result.map_err(|_| "window_unavailable")?;
    window.is_maximized().map_err(|_| "window_unavailable")
}
