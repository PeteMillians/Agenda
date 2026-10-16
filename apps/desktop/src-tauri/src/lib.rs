mod commands;
mod sidecar;
mod tray;

use tauri::{AppHandle, Manager, RunEvent, WindowEvent};

/// Passed by the autostart entry so a login launch goes straight to the tray.
const MINIMIZED_FLAG: &str = "--minimized";

pub fn run() {
    let app = tauri::Builder::default()
        // Must be first: a second launch focuses this window and exits before doing anything else.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| tray::show_main_window(app)))
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec![MINIMIZED_FLAG]),
        ))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .manage(sidecar::Supervisor::default())
        .invoke_handler(tauri::generate_handler![commands::get_backend_config])
        .setup(|app| {
            tray::create(app.handle())?;
            enable_autostart(app.handle());
            if !std::env::args().any(|arg| arg == MINIMIZED_FLAG) {
                tray::show_main_window(app.handle());
            }
            sidecar::start(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing hides to the tray so reminders and sync keep running; quit is in the tray menu.
            // Not in Linux dev builds: WSLg has no tray, so a hidden window could never come back.
            let tray_available = !(cfg!(debug_assertions) && cfg!(target_os = "linux"));
            if let WindowEvent::CloseRequested { api, .. } = event
                && tray_available
            {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Agenda");

    app.run(|app, event| {
        if let RunEvent::Exit = event {
            app.state::<sidecar::Supervisor>().shutdown();
        }
    });
}

/// Start at login so reminders work after a reboot. Release builds only, so `tauri dev`
/// never registers a debug binary.
#[cfg(not(debug_assertions))]
fn enable_autostart(app: &AppHandle) {
    use tauri_plugin_autostart::ManagerExt;
    let launcher = app.autolaunch();
    if !launcher.is_enabled().unwrap_or(false)
        && let Err(err) = launcher.enable()
    {
        eprintln!("[shell] warn: could not enable start at login: {err}");
    }
}

#[cfg(debug_assertions)]
fn enable_autostart(_app: &AppHandle) {}
