use crate::{
    admission, bootstrap,
    policy::{self, RestartPolicy},
    windows,
};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{Receiver, SyncSender},
        Arc,
    },
    time::{Duration, Instant},
};
use tauri::{AppHandle, Manager};

pub enum Action {
    Activate(Option<String>),
    RefreshPresentation(String, SyncSender<Result<String, &'static str>>),
    Quit,
}
pub struct Host {
    pub admitted: AtomicBool,
    pub stopped: AtomicBool,
    pub quitting: AtomicBool,
    pub actions: SyncSender<Action>,
}

impl Host {
    pub fn quit(&self) {
        self.quitting.store(true, Ordering::SeqCst);
        self.admitted.store(false, Ordering::SeqCst);
        let _ = self.actions.try_send(Action::Quit);
    }
}

fn unavailable(app: &AppHandle, state: &Host) {
    state.admitted.store(false, Ordering::SeqCst);
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.navigate(
            "http://tauri.localhost/unavailable.html"
                .parse()
                .expect("fixed URL"),
        );
        let _ = window.show();
    }
}

pub fn supervise(
    app: AppHandle,
    state: Arc<Host>,
    receiver: Receiver<Action>,
    journey: Option<String>,
) {
    if run(&app, &state, &receiver, journey).is_err() {
        unavailable(&app, &state);
    }
    // Failure is stable until the user quits. App links never start a crash loop.
    while !state.quitting.load(Ordering::SeqCst) {
        if matches!(
            receiver.recv_timeout(Duration::from_millis(100)),
            Ok(Action::Quit)
        ) {
            break;
        }
    }
    state.stopped.store(true, Ordering::SeqCst);
    app.exit(0);
}

fn run(
    app: &AppHandle,
    state: &Host,
    receiver: &Receiver<Action>,
    mut journey: Option<String>,
) -> Result<(), &'static str> {
    let executable = std::env::current_exe().map_err(|_| "invalid_installation")?;
    let image: PathBuf = executable
        .parent()
        .ok_or("invalid_installation")?
        .join("Brain.exe")
        .canonicalize()
        .map_err(|_| "invalid_installation")?;
    let data = policy::data_directory(&image)?;
    let sid = windows::current_sid()?;
    let mut restart = RestartPolicy::default();
    loop {
        if state.quitting.load(Ordering::SeqCst) {
            return Ok(());
        }
        // Repeat on replacement too: never adopt or terminate a stranger.
        if !windows::listeners()?.is_empty() {
            return Err("port_conflict");
        }
        let secret = admission::launcher_secret()?;
        let mut admission = policy::AdmissionLease::new(
            windows::Brain::start(&image, &data, &secret)?,
            &state.admitted,
        );
        let brain = &mut admission.backend;
        let mut startup = bootstrap::Supervisor::new(Instant::now());
        loop {
            if state.quitting.load(Ordering::SeqCst) {
                brain.stop();
                return Ok(());
            }
            let (bytes, closed) = brain.bootstrap_bytes()?;
            startup.receive(&bytes, Instant::now())?;
            if brain.exit_code()?.is_some() {
                return Err("child_start_failed");
            }
            if closed && !startup.ready() {
                return Err("child_status_closed");
            }
            if policy::verify_listener(&windows::listeners()?, brain.pid, &image, &sid)? {
                if startup.ready() {
                    break;
                }
                return Err("child_listener_before_validation");
            }
            startup.check_deadline(Instant::now())?;
            std::thread::sleep(Duration::from_millis(100));
        }
        let target = admission::entry(&secret, journey.as_deref())?;
        if brain.exit_code()?.is_some()
            || !policy::verify_listener(&windows::listeners()?, brain.pid, &image, &sid)?
        {
            return Err("child_unavailable");
        }
        state.admitted.store(true, Ordering::SeqCst);
        let window = app.get_webview_window("main").ok_or("window_unavailable")?;
        window.navigate(target).map_err(|_| "window_unavailable")?;
        loop {
            if state.quitting.load(Ordering::SeqCst) {
                restart.quit();
                state.admitted.store(false, Ordering::SeqCst);
                brain.stop();
                return Ok(());
            }
            let mut exit = brain.exit_code()?;
            if exit.is_none() {
                let listener = windows::listeners()
                    .and_then(|rows| policy::verify_listener(&rows, brain.pid, &image, &sid));
                if listener != Ok(true) {
                    // Shutdown closes the socket before the process signals. Revoke
                    // authority now, then observe the actual exit within the same
                    // bounded grace period used by ordinary owned-child shutdown.
                    state.admitted.store(false, Ordering::SeqCst);
                    window
                        .navigate(
                            "http://tauri.localhost/index.html"
                                .parse()
                                .expect("fixed URL"),
                        )
                        .map_err(|_| "window_unavailable")?;
                    exit = brain.wait_for_exit(Duration::from_secs(15), &state.quitting)?;
                    if state.quitting.load(Ordering::SeqCst) {
                        restart.quit();
                        brain.stop();
                        return Ok(());
                    }
                    if exit.is_none() {
                        return Err(listener.err().unwrap_or("listener_lost"));
                    }
                }
            }
            if let Some(code) = exit {
                state.admitted.store(false, Ordering::SeqCst);
                if restart.should_restart(code, data.join("runtime.env").is_file()) {
                    window
                        .navigate(
                            "http://tauri.localhost/index.html"
                                .parse()
                                .expect("fixed URL"),
                        )
                        .map_err(|_| "window_unavailable")?;
                    break;
                }
                return Err("child_stopped");
            }
            match receiver.recv_timeout(Duration::from_millis(100)) {
                Ok(Action::RefreshPresentation(generation, reply)) => {
                    let result = if state.admitted.load(Ordering::SeqCst) {
                        admission::refresh(&secret, &generation)
                    } else {
                        Err("desktop_admission_required")
                    };
                    let _ = reply.try_send(result);
                }
                Ok(Action::Quit) => {
                    state.quit();
                }
                Ok(Action::Activate(next)) => {
                    let document = window.url().map_err(|_| "window_unavailable")?;
                    if document.path() != "/onboarding" && (next.is_none() || next == journey) {
                        continue;
                    }
                    if admission::presentation_current(&secret, &document, next.as_deref())? {
                        if next.is_some() {
                            journey = next;
                        }
                    } else if let Ok(target) = admission::entry(&secret, next.as_deref()) {
                        // A stale/different locator cannot stop an active backend.
                        if brain.exit_code()?.is_some() {
                            return Err("child_stopped");
                        }
                        window.navigate(target).map_err(|_| "window_unavailable")?;
                        journey = next;
                    }
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                Err(_) => {
                    state.quit();
                }
            }
        }
    }
}
