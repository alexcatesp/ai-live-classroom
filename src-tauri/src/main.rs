// Hide the console window in release builds; the backend logs to stderr, which
// the shell relays, so a teacher never sees a terminal.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Desktop shell.
//!
//! The shell owns the backend process: it starts it as a sidecar, waits for the
//! line announcing which loopback port it is on and which session token to use,
//! and only then opens the window with those values injected. The window is
//! built here rather than declared in `tauri.conf.json` because the injection
//! has to happen before the page runs.
//!
//! If the backend never announces itself, the window still opens. The interface
//! detects the missing handshake and explains the situation, which beats a
//! silent failure with no window at all.

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Must match READY_PREFIX in backend/src/aiclassroom/main.py.
const READY_PREFIX: &str = "AICLASSROOM_READY ";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const MAIN_WINDOW: &str = "main";

#[derive(Debug, Clone, Deserialize)]
struct Handshake {
    port: u16,
    token: String,
}

/// Holds the sidecar so it can be stopped when the window closes.
struct Backend(Mutex<Option<CommandChild>>);

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let handle = app.handle().clone();
            // The shell knows where the portable folder is, so it tells the
            // backend rather than letting it infer the path from its own
            // location inside runtime/backend.
            let data_dir = portable_data_dir();

            let (mut receiver, child) = app
                .shell()
                .sidecar("aiclassroom-backend")
                .map_err(|error| format!("No se encontró el motor local: {error}"))?
                .args(["--data-dir", &data_dir.to_string_lossy()])
                .spawn()
                .map_err(|error| format!("No se pudo iniciar el motor local: {error}"))?;

            app.manage(Backend(Mutex::new(Some(child))));

            tauri::async_runtime::spawn(async move {
                let handshake = wait_for_handshake(&mut receiver).await;
                if handshake.is_none() {
                    eprintln!("[shell] el motor local no anunció su puerto; se abre la ventana igualmente");
                }
                if let Err(error) = open_main_window(&handle, handshake) {
                    eprintln!("[shell] no se pudo abrir la ventana: {error}");
                }

                // Keep draining the pipe: a full stdout buffer would block the
                // backend mid-class.
                while let Some(event) = receiver.recv().await {
                    match event {
                        CommandEvent::Stderr(line) => {
                            eprint!("[backend] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Terminated(status) => {
                            eprintln!("[backend] el motor local ha terminado: {status:?}");
                            break;
                        }
                        _ => {}
                    }
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("no se pudo construir la aplicación");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            // Without this the backend would outlive the window and keep the
            // microphone open, which spec section 14 rules out.
            if let Some(backend) = handle.try_state::<Backend>() {
                if let Ok(mut guard) = backend.0.lock() {
                    if let Some(child) = guard.take() {
                        let _ = child.kill();
                    }
                }
            }
        }
    });
}

/// `data/` next to the executable the teacher double-clicks.
///
/// Falling back to the working directory keeps `cargo tauri dev` usable, where
/// the executable lives deep inside target/.
fn portable_data_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|executable| executable.parent().map(|folder| folder.join("data")))
        .unwrap_or_else(|| PathBuf::from("data"))
}

/// Reads the sidecar's stdout until it announces its port, or gives up.
async fn wait_for_handshake(
    receiver: &mut tauri::async_runtime::Receiver<CommandEvent>,
) -> Option<Handshake> {
    let deadline = tokio::time::Instant::now() + STARTUP_TIMEOUT;

    loop {
        let event = match tokio::time::timeout_at(deadline, receiver.recv()).await {
            Ok(Some(event)) => event,
            // The channel closed, or the backend took too long to answer.
            Ok(None) | Err(_) => return None,
        };

        match event {
            CommandEvent::Stdout(line) => {
                let line = String::from_utf8_lossy(&line);
                if let Some(payload) = line.trim().strip_prefix(READY_PREFIX) {
                    match serde_json::from_str::<Handshake>(payload) {
                        Ok(handshake) => return Some(handshake),
                        Err(error) => {
                            eprintln!("[shell] anuncio del motor ilegible: {error}");
                            return None;
                        }
                    }
                }
            }
            CommandEvent::Stderr(line) => {
                eprint!("[backend] {}", String::from_utf8_lossy(&line));
            }
            CommandEvent::Terminated(status) => {
                eprintln!("[backend] el motor local no llegó a arrancar: {status:?}");
                return None;
            }
            _ => {}
        }
    }
}

fn open_main_window(
    handle: &tauri::AppHandle,
    handshake: Option<Handshake>,
) -> Result<(), tauri::Error> {
    let mut builder = WebviewWindowBuilder::new(handle, MAIN_WINDOW, WebviewUrl::App("index.html".into()))
        .title("AI Classroom Live")
        .inner_size(960.0, 820.0)
        .min_inner_size(640.0, 560.0)
        .resizable(true);

    if let Some(handshake) = handshake {
        // serde_json escapes the token, so it cannot break out of the literal.
        let payload = serde_json::json!({ "port": handshake.port, "token": handshake.token });
        builder = builder.initialization_script(format!(
            "window.__AICLASSROOM_BACKEND__ = {payload};"
        ));
    }

    builder.build()?;
    Ok(())
}
