#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
#![allow(non_snake_case)]

#[cfg(windows)]
mod admission;
mod bootstrap;
#[cfg(windows)]
mod browser;
mod deployment;
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
#[cfg(windows)]
mod windows;

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
    if !_ownership.primary() {
        if servicing_session.is_some() {
            return;
        }
        if let Err(reason) = windows::forward_instance(&names, &executable, &args) {
            prerequisite::show_failure(reason);
        }
        return;
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
    let (sender, receiver) = mpsc::sync_channel(8);
    let state = Arc::new(host::Host {
        admitted: AtomicBool::new(false),
        stopped: AtomicBool::new(false),
        quitting: AtomicBool::new(false),
        update_check_active: AtomicBool::new(false),
        servicing_cancel: std::sync::Mutex::new(None),
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
                    let _ = activation.actions.try_send(host::Action::Activate(journey));
                    frame::show_main(app);
                }
            },
        ))
        .manage(state.clone())
        .invoke_handler(tauri::generate_handler![
            desktop_state,
            desktop_update_status,
            browser::desktop_refresh_presentation,
            browser::desktop_browser_task,
            browser::desktop_setup_code,
            frame::desktop_window
        ])
        .setup(move |app| {
            let navigation = setup.clone();
            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("Conversation Analytics")
                    // Navigation briefly exposes the native surface before
                    // either dark startup document has painted.
                    .background_color(tauri::window::Color(20, 25, 22, 255))
                    .data_directory(
                        policy::webview_directory(&data).map_err(std::io::Error::other)?,
                    )
                    .inner_size(1180.0, 800.0)
                    .min_inner_size(640.0, 480.0)
                    .decorations(false)
                    .initialization_script(include_str!("../../window-frame.js"))
                    .devtools(false)
                    .disable_drag_drop_handler()
                    .on_navigation(move |url| {
                        policy::shell_document(url)
                            || (navigation.admitted.load(Ordering::SeqCst)
                                && policy::local_document(url))
                    })
                    .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                    .on_download(|_, _| false)
                    .build()?;
            let close = window.clone();
            window.on_window_event(move |event| {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = close.hide();
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
                        let _ = quit_state.actions.try_send(host::Action::Activate(None));
                        frame::show_main(app);
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
