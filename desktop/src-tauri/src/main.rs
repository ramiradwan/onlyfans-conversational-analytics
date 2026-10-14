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
#[cfg(windows)]
mod host;
mod policy;
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
    let Ok(journey) = policy::launch_journey(&args) else {
        return;
    };
    let Ok(_application_mutex) = windows::application_mutex() else {
        return;
    };
    let (sender, receiver) = mpsc::sync_channel(8);
    let state = Arc::new(host::Host {
        admitted: AtomicBool::new(false),
        stopped: AtomicBool::new(false),
        quitting: AtomicBool::new(false),
        actions: sender,
    });
    let activation = state.clone();
    let setup = state.clone();
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
            browser::desktop_refresh_presentation,
            browser::desktop_browser_task,
            browser::desktop_setup_code,
            frame::desktop_window
        ])
        .setup(move |app| {
            let executable = std::env::current_exe()?;
            let image = executable
                .parent()
                .ok_or("invalid_installation")?
                .join("Brain.exe");
            let data = policy::data_directory(&image).map_err(std::io::Error::other)?;
            let navigation = setup.clone();
            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("Conversation Analytics")
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
            std::thread::spawn(move || host::supervise(handle, setup, receiver, journey));
            Ok(())
        })
        .build(tauri::generate_context!());
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
