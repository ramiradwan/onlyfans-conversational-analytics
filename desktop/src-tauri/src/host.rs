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
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{AppHandle, Manager};

pub enum Action {
    Activate(Option<String>),
    RefreshPresentation(String, SyncSender<Result<String, &'static str>>),
    Prepared(Result<crate::servicing::PreparedUpdate, &'static str>),
    Quit,
}
pub struct Host {
    pub admitted: AtomicBool,
    pub stopped: AtomicBool,
    pub quitting: AtomicBool,
    pub update_check_active: AtomicBool,
    pub servicing_cancel: Mutex<Option<crate::servicing_handoff::Cancellation>>,
    pub release: Option<crate::deployment::DesktopRelease>,
    pub actions: SyncSender<Action>,
}

impl Host {
    pub fn quit(&self) {
        self.quitting.store(true, Ordering::SeqCst);
        if let Some(cancel) = self
            .servicing_cancel
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_ref()
        {
            cancel.signal();
        }
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

fn observe_servicing(window: &tauri::WebviewWindow, code: &'static str) {
    if let Ok(handle) = window.hwnd() {
        windows::observe_servicing(handle.0 as _, code);
    }
}

pub fn supervise(
    app: AppHandle,
    state: Arc<Host>,
    receiver: Receiver<Action>,
    journey: Option<String>,
    image: PathBuf,
    data: PathBuf,
) {
    match run(&app, &state, &receiver, journey, &image, &data) {
        Ok(true) => {
            state.stopped.store(true, Ordering::SeqCst);
            app.exit(if state.quitting.load(Ordering::SeqCst) {
                0
            } else {
                crate::handoff_contract::EXIT_FOR_SERVICING as i32
            });
            return;
        }
        Err(_) => unavailable(&app, &state),
        Ok(false) => (),
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
    state: &Arc<Host>,
    receiver: &Receiver<Action>,
    mut journey: Option<String>,
    image: &std::path::Path,
    data: &std::path::Path,
) -> Result<bool, &'static str> {
    let sid = windows::current_sid()?;
    let mut restart = RestartPolicy::default();
    loop {
        if state.quitting.load(Ordering::SeqCst) {
            return Ok(false);
        }
        // Repeat on replacement too: never adopt or terminate a stranger.
        if !windows::listeners()?.is_empty() {
            return Err("port_conflict");
        }
        let secret = admission::launcher_secret()?;
        let mut admission = policy::AdmissionLease::new(
            windows::Brain::start(image, data, &secret)?,
            &state.admitted,
        );
        let brain = &mut admission.backend;
        let mut startup = bootstrap::Supervisor::new(Instant::now());
        loop {
            if state.quitting.load(Ordering::SeqCst) {
                brain.stop();
                return Ok(false);
            }
            let (bytes, closed) = brain.bootstrap_bytes()?;
            startup.receive(&bytes, Instant::now())?;
            if brain.exit_code()?.is_some() {
                return Err("child_start_failed");
            }
            if closed && !startup.ready() {
                return Err("child_status_closed");
            }
            if policy::verify_listener(&windows::listeners()?, brain.pid, image, &sid)? {
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
            || !policy::verify_listener(&windows::listeners()?, brain.pid, image, &sid)?
        {
            return Err("child_unavailable");
        }
        state.admitted.store(true, Ordering::SeqCst);
        crate::servicing_handoff::admitted(data)?;
        let window = app.get_webview_window("main").ok_or("window_unavailable")?;
        window.navigate(target).map_err(|_| "window_unavailable")?;
        let mut next_check = Instant::now() + Duration::from_secs(15);
        loop {
            if state.quitting.load(Ordering::SeqCst) {
                restart.quit();
                state.admitted.store(false, Ordering::SeqCst);
                brain.stop();
                return Ok(false);
            }
            let mut exit = brain.exit_code()?;
            if exit.is_none() {
                let listener = windows::listeners()
                    .and_then(|rows| policy::verify_listener(&rows, brain.pid, image, &sid));
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
                        return Ok(false);
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
            if Instant::now() >= next_check {
                let started = prepare_update(state);
                if started
                    && state
                        .release
                        .as_ref()
                        .is_some_and(|release| release.update_owner == "direct-appinstaller")
                {
                    observe_servicing(&window, "prepare_started");
                }
                next_check = Instant::now()
                    + if started {
                        Duration::from_secs(6 * 60 * 60)
                    } else {
                        Duration::from_secs(10)
                    };
            }
            match receiver.recv_timeout(Duration::from_millis(100)) {
                Ok(Action::Prepared(Ok(update))) => {
                    let Some(release) = state.release.as_ref() else {
                        continue;
                    };
                    if state.quitting.load(Ordering::SeqCst) {
                        continue;
                    }
                    let cancel = match crate::servicing_handoff::Cancellation::new() {
                        Ok(value) => value,
                        Err(reason) => {
                            observe_servicing(&window, reason);
                            continue;
                        }
                    };
                    {
                        let mut signal = state
                            .servicing_cancel
                            .lock()
                            .unwrap_or_else(|error| error.into_inner());
                        if state.quitting.load(Ordering::SeqCst) {
                            continue;
                        }
                        *signal = Some(cancel.clone());
                    }
                    let mut receipt = match startup.shutdown_receipt() {
                        Ok(value) => value,
                        Err(reason) => {
                            observe_servicing(&window, reason);
                            continue;
                        }
                    };
                    let mut pending =
                        match crate::servicing_handoff::start(data, release, &update, cancel) {
                            Ok(value) => value,
                            Err(reason) => {
                                observe_servicing(&window, reason);
                                continue;
                            }
                        };
                    state.admitted.store(false, Ordering::SeqCst);
                    observe_servicing(&window, "draining");
                    let _ = window.navigate(
                        "http://tauri.localhost/index.html"
                            .parse()
                            .expect("fixed URL"),
                    );
                    if brain.stop_for_servicing(
                        &mut receipt,
                        Duration::from_secs(90),
                        &state.quitting,
                    ) == Ok(true)
                        && !state.quitting.load(Ordering::SeqCst)
                        && pending.commit().is_ok()
                    {
                        observe_servicing(&window, "committed");
                        return Ok(true);
                    }
                    observe_servicing(&window, "drain_or_commit_refused");
                    drop(pending);
                    unavailable(app, state);
                    // Retain the owned Brain and Job Object on refusal. A servicing
                    // deadline never becomes forced cleanup or normal admission.
                    while !state.quitting.load(Ordering::SeqCst) {
                        if matches!(
                            receiver.recv_timeout(Duration::from_millis(100)),
                            Ok(Action::Quit) | Err(std::sync::mpsc::RecvTimeoutError::Disconnected)
                        ) {
                            state.quit();
                        }
                    }
                    brain.stop();
                    return Ok(false);
                }
                Ok(Action::Prepared(Err(reason))) => observe_servicing(&window, reason),
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

fn prepare_update(state: &Arc<Host>) -> bool {
    let Some(release) = state
        .release
        .as_ref()
        .filter(|value| value.update_owner == "direct-appinstaller")
    else {
        return true;
    };
    if state
        .update_check_active
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return false;
    }
    struct Check(Arc<Host>);
    impl Drop for Check {
        fn drop(&mut self) {
            self.0.update_check_active.store(false, Ordering::SeqCst);
        }
    }
    let owner = state.clone();
    let release = release.clone();
    std::thread::spawn(move || {
        let _check = Check(owner.clone());
        let result = crate::servicing::prepare_direct(&release, || {
            owner.quitting.load(Ordering::SeqCst) || !owner.admitted.load(Ordering::SeqCst)
        });
        let _ = owner.actions.try_send(Action::Prepared(result));
    });
    true
}
