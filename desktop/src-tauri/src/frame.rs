use crate::{host::Host, policy};
use std::{
    sync::{atomic::Ordering, Arc, Mutex},
    time::{Duration, Instant},
};
use tauri::Manager;

const PASSKEY_OPEN_WINDOW: Duration = Duration::from_secs(120);

/// Presentation hint from one explicit native Open. It grants no access.
#[derive(Default)]
pub struct PasskeyOpen(Mutex<Option<Instant>>);

impl PasskeyOpen {
    fn record(&self, now: Instant, foreground: bool) {
        *self.0.lock().unwrap_or_else(|error| error.into_inner()) =
            foreground.then(|| now + PASSKEY_OPEN_WINDOW);
    }

    pub fn clear(&self) {
        self.record(Instant::now(), false);
    }

    fn claim(&self, now: Instant, foreground: bool) -> bool {
        self.0
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
            .is_some_and(|deadline| foreground && now < deadline)
    }
}

fn foreground(window: &tauri::WebviewWindow) -> bool {
    window.is_visible().unwrap_or(false)
        && !window.is_minimized().unwrap_or(true)
        && window.is_focused().unwrap_or(false)
}

pub fn record_interactive_open(window: &tauri::WebviewWindow) {
    window
        .state::<PasskeyOpen>()
        .record(Instant::now(), foreground(window));
}

pub fn show_main(app: &tauri::AppHandle, explicit_open: bool) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_minimized().unwrap_or(false) {
            let _ = window.unminimize();
        }
        let _ = window.show();
        let _ = window.set_focus();
        if explicit_open {
            record_interactive_open(&window);
            if app.state::<Arc<Host>>().admitted.load(Ordering::SeqCst)
                && window.url().is_ok_and(|url| policy::local_document(&url))
            {
                let _ = window.eval("window.dispatchEvent(new Event('dipsy-passkey-open'))");
            }
        } else {
            app.state::<PasskeyOpen>().clear();
        }
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
    PasskeyForeground,
    ClaimPasskeyUnlock,
    DiscardPasskeyUnlock,
    OpenPasskeySettings,
    AppearanceLight,
    AppearanceDark,
    AppearanceSystem,
}

pub fn close_main(
    window: &tauri::WebviewWindow,
    state: &Host,
    url: Option<&url::Url>,
) -> tauri::Result<bool> {
    window.state::<PasskeyOpen>().clear();
    close_or_hide(state, window.label(), url, || {
        window.hide()?;
        window.is_maximized()
    })
}

pub(crate) fn close_or_hide(
    state: &Host,
    label: &str,
    url: Option<&url::Url>,
    hide: impl FnOnce() -> tauri::Result<bool>,
) -> tauri::Result<bool> {
    if url.is_some_and(|url| state.close_retained_recovery(label, url)) {
        return Ok(false);
    }
    hide()
}

/// Window presentation, including the retained-data Close gesture.
#[tauri::command]
pub fn desktop_window(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, Arc<Host>>,
    appearance: tauri::State<'_, crate::appearance::Appearance>,
    passkey_open: tauri::State<'_, PasskeyOpen>,
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
        WindowAction::PasskeyForeground
        | WindowAction::ClaimPasskeyUnlock
        | WindowAction::DiscardPasskeyUnlock
        | WindowAction::OpenPasskeySettings => {
            if !state.admitted.load(Ordering::SeqCst)
                || !policy::local_document(&url)
                || url.path().starts_with("/onboarding/")
            {
                return Err("window_control_denied");
            }
            let current = foreground(&window);
            return match action {
                WindowAction::ClaimPasskeyUnlock => Ok(passkey_open.claim(Instant::now(), current)),
                WindowAction::DiscardPasskeyUnlock => {
                    passkey_open.clear();
                    Ok(true)
                }
                WindowAction::OpenPasskeySettings if current => {
                    passkey_open.clear();
                    crate::windows::open_passkey_privacy_settings().map(|()| true)
                }
                _ => Ok(current),
            };
        }
        WindowAction::AppearanceLight
        | WindowAction::AppearanceDark
        | WindowAction::AppearanceSystem => {
            // Appearance is available only to the admitted configured document,
            // not to the packaged shell, browser helper or setup admission.
            if !state.admitted.load(Ordering::SeqCst)
                || !matches!(
                    url.path(),
                    "/" | "/inbox" | "/analytics" | "/graph-explorer" | "/settings" | "/account"
                )
            {
                return Err("window_control_denied");
            }
            let mode = match action {
                WindowAction::AppearanceLight => crate::appearance::Mode::Light,
                WindowAction::AppearanceDark => crate::appearance::Mode::Dark,
                _ => crate::appearance::Mode::System,
            };
            appearance.select(mode)?;
            window
                .set_background_color(Some(mode.background()))
                .map_err(|_| "window_unavailable")?;
            window.set_theme(mode.window_theme())
        }
        WindowAction::Minimize => {
            passkey_open.clear();
            window.minimize()
        }
        WindowAction::ToggleMaximize => {
            if window.is_maximized().map_err(|_| "window_unavailable")? {
                window.unmaximize()
            } else {
                window.maximize()
            }
        }
        WindowAction::Close => {
            return close_main(&window, &state, Some(&url)).map_err(|_| "window_unavailable");
        }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passkey_open_is_one_use_and_bound_to_foreground() {
        let now = Instant::now();
        let open = PasskeyOpen::default();
        assert!(!open.claim(now, true));
        open.record(now, false);
        assert!(!open.claim(now, true));
        open.record(now, true);
        assert!(open.claim(now, true));
        assert!(!open.claim(now, true));
        open.record(now, true);
        assert!(!open.claim(now, false));
        assert!(!open.claim(now, true));
        open.record(now, true);
        open.clear();
        assert!(!open.claim(now, true));
        open.record(now, true);
        assert!(!open.claim(now + PASSKEY_OPEN_WINDOW, true));
    }
}
