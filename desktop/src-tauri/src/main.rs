#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
#![allow(non_snake_case)]

#[cfg(windows)]
mod admission;
#[cfg(windows)]
mod appearance;
mod bootstrap;
#[cfg(windows)]
mod browser;
mod deployment;
#[cfg(windows)]
mod engine_acquisition;
#[cfg(windows)]
mod engine_acquisition_broker;
#[cfg(windows)]
mod engine_acquisition_quote;
#[cfg(windows)]
mod frame;
mod handoff_contract;
#[cfg(windows)]
mod host;
mod policy;
#[cfg(windows)]
mod prerequisite;
#[cfg(windows)]
mod servicing;
mod servicing_feed;
#[cfg(windows)]
mod servicing_handoff;
#[cfg(any(windows, test))]
mod support_channels;
#[cfg(windows)]
mod windows;
mod worker_diagnostics;
mod worker_host;

#[cfg(test)]
mod support_channels_tests {
    use super::support_channels::{dialog_content, initialization_script, COMPILED_CHANNELS};

    #[test]
    fn support_channels_formats_one_plain_text_block_in_provider_order() {
        let projection = r#"{"tiktok":"https://video.example.com/a&b","whatsapp":"https://msg.example.com/help"}"#;
        assert_eq!(
            dialog_content("Setup ended.", projection),
            "Setup ended.\n\nGet help\nWhatsApp https://msg.example.com/help\nTikTok https://video.example.com/a&b"
        );
        assert_eq!(dialog_content("Setup ended.", "{}"), "Setup ended.");
    }

    #[test]
    fn support_channels_binds_the_compiled_projection_and_omits_empty_script() {
        assert!(initialization_script("{}").is_empty());
        let projection = r#"{"telegram":"https://chat.example.com/help"}"#;
        let script = initialization_script(projection);
        assert!(script.contains(projection));
        assert!(script.contains("/data-recovery.html"));
        assert!(script.contains("/unavailable.html"));
        assert!(!script.contains("clipboard"));
        assert!(!script.contains("invoke"));
        let expected = std::env::var("OFCA_TEST_SUPPORT_CHANNELS").unwrap_or_else(|_| "{}".into());
        assert_eq!(COMPILED_CHANNELS, expected);
    }

    #[cfg(windows)]
    #[test]
    fn support_channels_keeps_native_close_and_disables_hyperlinks() {
        use windows_sys::Win32::UI::Controls::{TDCBF_CLOSE_BUTTON, TDF_ENABLE_HYPERLINKS};
        let config = super::prerequisite::failure_dialog_config();
        let buttons = config.dwCommonButtons;
        assert_eq!(buttons, TDCBF_CLOSE_BUTTON);
        assert_eq!(config.dwFlags & TDF_ENABLE_HYPERLINKS, 0);
        assert!(config.hwndParent.is_null());
    }
}

#[cfg(windows)]
#[tauri::command]
fn desktop_state(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
) -> Result<bool, &'static str> {
    use std::sync::atomic::Ordering;
    if window.label() != "main"
        || !policy::local_document(&window.url().map_err(|_| "window_unavailable")?)
        || !state.admitted.load(Ordering::SeqCst)
        || state.quitting.load(Ordering::SeqCst)
    {
        return Err("desktop_admission_required");
    }
    Ok(true)
}

#[cfg(windows)]
#[tauri::command]
fn desktop_chrome_available(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
) -> Result<bool, &'static str> {
    desktop_state(window, state)?;
    Ok(browser::chrome_path().is_ok())
}

#[cfg(windows)]
struct UpdateCheckGuard(std::sync::Arc<host::Host>);
#[cfg(windows)]
impl Drop for UpdateCheckGuard {
    fn drop(&mut self) {
        self.0
            .update_check_active
            .store(false, std::sync::atomic::Ordering::SeqCst);
    }
}

#[cfg(windows)]
#[tauri::command]
async fn desktop_update_status(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
) -> Result<servicing::UpdateStatus, &'static str> {
    use std::sync::atomic::Ordering;
    desktop_state(window.clone(), state.clone())?;
    let owner = state.inner().clone();
    if owner.engine_acquisition.busy() {
        return Err("optional_acquisition_busy");
    }
    owner
        .update_check_active
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .map_err(|_| "update_check_in_progress")?;
    let guard = UpdateCheckGuard(owner.clone());
    let status = tauri::async_runtime::spawn_blocking(move || {
        let _guard = guard;
        servicing::inspect(owner.release.as_ref(), || {
            owner.quitting.load(Ordering::SeqCst) || !owner.admitted.load(Ordering::SeqCst)
        })
    })
    .await
    .map_err(|_| "update_check_unavailable")?;
    // A lost admission or closed window cannot receive a result from an old check.
    desktop_state(window, state)?;
    Ok(status)
}

#[cfg(windows)]
#[tauri::command]
async fn desktop_engine_acquisition_review(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
    explicit_selection: engine_acquisition::Selection,
) -> Result<engine_acquisition_quote::Review, &'static str> {
    use std::sync::atomic::Ordering;
    desktop_state(window.clone(), state.clone())?;
    let owner = state.inner().clone();
    if owner.engine_acquisition.busy() {
        return Err("optional_acquisition_busy");
    }
    owner
        .update_check_active
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .map_err(|_| "update_check_in_progress")?;
    let guard = UpdateCheckGuard(owner.clone());
    let review = tauri::async_runtime::spawn_blocking(move || {
        let _guard = guard;
        engine_acquisition_quote::review(explicit_selection, &|| {
            owner.quitting.load(Ordering::SeqCst) || !owner.admitted.load(Ordering::SeqCst)
        })
    })
    .await
    .map_err(|_| "optional_quote_unavailable")??;
    desktop_state(window, state)?;
    Ok(review)
}
#[cfg(windows)]
#[tauri::command]
async fn desktop_engine_acquisition_start(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
    explicit_selection: engine_acquisition::Selection,
    review_digest: String,
) -> Result<engine_acquisition::Status, &'static str> {
    let result = async {
        use std::sync::atomic::Ordering;
        desktop_state(window.clone(), state.clone())?;
        let owner = state.inner().clone();
        owner
            .update_check_active
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "update_check_in_progress")?;
        let guard = UpdateCheckGuard(owner.clone());
        let result = tauri::async_runtime::spawn_blocking(move || {
            if owner.admitted.load(Ordering::SeqCst) && !owner.quitting.load(Ordering::SeqCst) {
                engine_acquisition_broker::admitted_servicing();
            }
            let release = owner.release.clone();
            let acquisition = owner.engine_acquisition.clone();
            acquisition.start(explicit_selection, release, review_digest, move || {
                let _hold = &guard;
                owner.quitting.load(Ordering::SeqCst) || !owner.admitted.load(Ordering::SeqCst)
            })
        })
        .await
        .map_err(|_| "optional_owner_unavailable")??;
        desktop_state(window.clone(), state)?;
        Ok(result)
    }
    .await;
    if let Ok(handle) = window.hwnd() {
        windows::observe_servicing_result(handle.0 as _, result)
    } else {
        result
    }
}

#[cfg(windows)]
#[tauri::command]
fn desktop_engine_acquisition_status(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
) -> Result<engine_acquisition::Status, &'static str> {
    desktop_state(window, state.clone())?;
    Ok(state.engine_acquisition.status())
}

#[cfg(windows)]
#[tauri::command]
fn desktop_engine_acquisition_cancel(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<host::Host>>,
    operation_id: String,
) -> Result<engine_acquisition::Status, &'static str> {
    desktop_state(window, state.clone())?;
    state.engine_acquisition.cancel(&operation_id)
}

#[cfg(windows)]
fn main() {
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    };
    use tauri::{
        menu::{Menu, MenuItem},
        tray::TrayIconBuilder,
        WebviewUrl, WebviewWindowBuilder,
    };
    let args: Vec<String> = std::env::args().collect();
    if engine_acquisition_broker::requested(&args) {
        std::process::exit(if engine_acquisition_broker::run(&args).is_ok() {
            0
        } else {
            1
        });
    }
    if worker_host::requested(&args) {
        std::process::exit(if worker_host::run(&args).is_ok() {
            0
        } else {
            1
        });
    }
    if servicing_handoff::helper_requested(&args) {
        std::process::exit(if servicing_handoff::helper_main(&args).is_ok() {
            0
        } else {
            1
        });
    }
    let servicing_session = if args.len() == 3
        && args[1] == servicing_handoff::ACTIVATION_FLAG
        && handoff_contract::hex(&args[2], 32)
    {
        Some(args[2].clone())
    } else {
        None
    };
    let journey = if servicing_session.is_some() {
        None
    } else {
        let Ok(journey) = policy::launch_journey(&args) else {
            return;
        };
        journey
    };
    let direct_open = journey.is_none() && servicing_session.is_none();
    let foreground_open = direct_open && windows::foreground_startup();
    let Ok(_application_mutex) = windows::application_mutex() else {
        return;
    };
    let preparation = (|| {
        let executable = std::env::current_exe().map_err(|_| "invalid_installation")?;
        let image = executable
            .parent()
            .ok_or("invalid_installation")?
            .join("Brain.exe");
        let data = policy::data_directory(&image)?;
        let release = deployment::load(
            &image.parent().unwrap().join("desktop-release.json"),
            windows::package_identity()?.as_ref(),
        )?;
        // Qualification owns its isolated root. Customer-root adoption from
        // another registered Inno location requires the explicit transition.
        let qualification = release
            .as_ref()
            .is_some_and(|r| r.purpose == "qualification");
        if !qualification {
            let registered = windows::legacy_registration()?;
            windows::validate_legacy_registration(&executable, registered.as_deref(), false)?;
            deployment::validate_msix_coexistence(
                release.as_ref(),
                policy::uses_customer_data_root(&data)?,
                windows::registered_msix_family,
            )?;
        }
        let names = deployment::instance_names(&data, &windows::current_sid()?, release.as_ref())?;
        let ownership = windows::claim_runtime_root(&names, &executable)?;
        Ok::<_, &'static str>((executable, image, data, names, ownership, release))
    })();
    let (executable, image, data, names, _ownership, release) = match preparation {
        Ok(value) => value,
        Err(reason) => {
            prerequisite::show_failure(reason);
            return;
        }
    };
    match _ownership.continue_startup(
        &names,
        &executable,
        (servicing_session.is_none() && (!direct_open || foreground_open))
            .then_some(args.as_slice()),
    ) {
        Ok(true) => (),
        Ok(false) => return,
        Err(reason) => {
            prerequisite::show_failure(reason);
            return;
        }
    }
    let startup = windows::package_identity().and_then(|identity| {
        servicing_handoff::startup(
            &data,
            release.as_ref(),
            identity.as_ref().map(|value| value.version.as_str()),
            servicing_session.as_deref(),
        )
    });
    if let Err(reason) = startup {
        if servicing_session.is_none() {
            prerequisite::show_failure(reason);
        }
        return;
    }
    // Run before even constructing the Tauri runtime, which caches whether
    // WebView2 is installed. No Brain or writable presentation root exists yet.
    let readiness = std::env::current_exe()
        .map_err(|_| "invalid_installation")
        .and_then(|image| prerequisite::required_version(&image))
        .and_then(|minimum| {
            prerequisite::ensure_ready(minimum, &mut prerequisite::WindowsPlatform)
        });
    match readiness {
        Ok(true) => (),
        Ok(false) => return,
        Err(reason) => {
            prerequisite::show_failure(reason);
            return;
        }
    }
    let image = match image.canonicalize() {
        Ok(image) => image,
        Err(_) => {
            prerequisite::show_failure("invalid_installation");
            return;
        }
    };
    // Resolve presentation before creating the native window or WebView.
    let appearance = appearance::Appearance::read(&data);
    let startup_appearance = appearance.mode();
    let (sender, receiver) = mpsc::sync_channel(8);
    let state = Arc::new(host::Host {
        admitted: AtomicBool::new(false),
        retained_recovery: AtomicBool::new(false),
        stopped: AtomicBool::new(false),
        quitting: AtomicBool::new(false),
        update_check_active: AtomicBool::new(false),
        servicing_cancel: std::sync::Mutex::new(None),
        engine_acquisition: engine_acquisition::Owner::new(data.clone()),
        release,
        actions: sender,
    });
    let activation = state.clone();
    let setup = state.clone();
    let mut context = tauri::generate_context!();
    context.config_mut().identifier = names.plugin_id;
    let application = tauri::Builder::default()
        // Must be registered before any plugin can create windows or a child.
        .plugin(tauri_plugin_single_instance::init(
            move |app, args, _cwd| {
                if let Ok(journey) = policy::launch_journey(&args) {
                    if activation.quitting.load(Ordering::SeqCst) {
                        return;
                    }
                    let explicit_open = journey.is_none();
                    activation.activate(journey);
                    frame::show_main(app, explicit_open);
                }
            },
        ))
        .manage(state.clone())
        .manage(appearance)
        .manage(frame::PasskeyOpen::default())
        .invoke_handler(tauri::generate_handler![
            desktop_state,
            desktop_chrome_available,
            desktop_update_status,
            desktop_engine_acquisition_review,
            desktop_engine_acquisition_start,
            desktop_engine_acquisition_status,
            desktop_engine_acquisition_cancel,
            browser::desktop_refresh_presentation,
            browser::desktop_browser_task,
            browser::desktop_setup_code,
            browser::desktop_text_analysis_reissue,
            browser::desktop_account_passkeys,
            frame::desktop_window
        ])
        .setup(move |app| {
            let navigation = setup.clone();
            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("Conversation Analytics")
                    .theme(startup_appearance.window_theme())
                    .background_color(startup_appearance.background())
                    .data_directory(
                        policy::webview_directory(&data).map_err(std::io::Error::other)?,
                    )
                    .inner_size(1180.0, 800.0)
                    .min_inner_size(640.0, 480.0)
                    .decorations(false)
                    .initialization_script(&startup_appearance.initialization_script())
                    .initialization_script(include_str!("../../window-frame.js"))
                    .initialization_script(support_channels::initialization_script(
                        support_channels::COMPILED_CHANNELS,
                    ))
                    .devtools(false)
                    .disable_drag_drop_handler()
                    .on_navigation(move |url| {
                        policy::startup_navigation(
                            url,
                            navigation.admitted.load(Ordering::SeqCst),
                            navigation.retained_recovery.load(Ordering::SeqCst),
                        )
                    })
                    .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                    .on_download(|_, _| false)
                    .build()?;
            if foreground_open {
                frame::record_interactive_open(&window);
            }
            let close = window.clone();
            let close_state = setup.clone();
            window.on_window_event(move |event| {
                if matches!(event, tauri::WindowEvent::Focused(false)) {
                    tauri::Manager::state::<frame::PasskeyOpen>(&close).clear();
                }
                if matches!(event, tauri::WindowEvent::ThemeChanged(_)) {
                    let appearance = tauri::Manager::state::<appearance::Appearance>(&close);
                    if appearance.mode() == appearance::Mode::System {
                        let _ = close.set_background_color(Some(appearance.mode().background()));
                    }
                }
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = frame::close_main(&close, &close_state, close.url().ok().as_ref());
                }
            });
            let open = MenuItem::with_id(
                app,
                "open",
                "Open Conversation Analytics",
                true,
                None::<&str>,
            )?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &quit])?;
            let quit_state = setup.clone();
            let mut tray = TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("Conversation Analytics")
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "open" => {
                        quit_state.activate(None);
                        frame::show_main(app, true);
                    }
                    "quit" => quit_state.quit(),
                    _ => {}
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                host::supervise(handle, setup, receiver, journey, image, data)
            });
            Ok(())
        })
        .build(context);
    if let Ok(application) = application {
        application.run(move |_app, event| {
            if let tauri::RunEvent::ExitRequested { api, .. } = event {
                if !state.stopped.load(Ordering::SeqCst) {
                    api.prevent_exit();
                    state.quit();
                }
            }
        });
    }
}

#[cfg(not(windows))]
fn main() {
    compile_error!("Bridge desktop is supported on Windows only");
}
