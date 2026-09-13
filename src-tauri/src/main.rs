// Hide the console window in release builds; the backend logs to stderr, which
// the shell relays, so a teacher never sees a terminal.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Desktop shell.
//!
//! The shell owns the backend process: it starts it, waits for the line
//! announcing which loopback port it is on and which session token to use, and
//! opens the window with those values injected. The window is built here rather
//! than declared in `tauri.conf.json` because the injection has to happen
//! before the page runs.
//!
//! **The window always opens.** An earlier version gave up when the backend
//! failed to start, which on Windows meant double-clicking the application and
//! watching nothing happen at all: no window, no message, nothing to report.
//! Now a failure opens the window carrying its reason, and also writes it to a
//! log file beside the data folder, because a problem nobody can see is a
//! problem nobody can fix.

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

// A release build without custom-protocol still compiles, and opens a window
// pointed at the Vite dev server: ERR_CONNECTION_REFUSED on the teacher's
// screen. Refuse to build it instead (P-11).
#[cfg(all(dev, not(debug_assertions)))]
compile_error!(
    "release build without the custom-protocol feature: the window would load devUrl \
     instead of the embedded interface. Use `cargo build --release --features custom-protocol`."
);

/// Must match READY_PREFIX in backend/src/aiclassroom/main.py.
const READY_PREFIX: &str = "AICLASSROOM_READY ";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const MAIN_WINDOW: &str = "main";

#[cfg(windows)]
const BACKEND_NAME: &str = "aiclassroom-backend.exe";
#[cfg(not(windows))]
const BACKEND_NAME: &str = "aiclassroom-backend";

#[derive(Debug, Clone, Deserialize)]
struct Handshake {
    port: u16,
    token: String,
}

/// Holds the backend process so it can be stopped when the window closes.
struct Backend(Mutex<Option<CommandChild>>);

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let handle = app.handle().clone();

            // Whatever happens here, a window opens.
            match start_backend(app) {
                Ok((mut receiver, child)) => {
                    app.manage(Backend(Mutex::new(Some(child))));

                    tauri::async_runtime::spawn(async move {
                        let handshake = wait_for_handshake(&mut receiver).await;
                        let problem = match handshake {
                            Some(_) => None,
                            None => Some(
                                "El motor local no anunció su puerto a tiempo. \
                                 Puede que un antivirus lo haya bloqueado."
                                    .to_string(),
                            ),
                        };
                        if let Some(reason) = &problem {
                            record(reason);
                        }
                        if let Err(error) = open_main_window(&handle, handshake, problem) {
                            record(&format!("no se pudo abrir la ventana: {error}"));
                        }

                        // Keep draining the pipe: a full stdout buffer would
                        // block the backend mid-class.
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
                }
                Err(reason) => {
                    record(&reason);
                    open_main_window(&handle, None, Some(reason))?;
                }
            }

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

type BackendProcess = (tauri::async_runtime::Receiver<CommandEvent>, CommandChild);

/// Starts the packaged backend, or explains why it could not.
fn start_backend(app: &tauri::App) -> Result<BackendProcess, String> {
    let executable = backend_executable().ok_or_else(|| {
        format!(
            "No se encontró el motor local ({BACKEND_NAME}). Debería estar en \
             runtime\\backend junto a la aplicación. Si la carpeta está \
             incompleta, vuelve a descomprimirla; si un antivirus se lo ha \
             llevado, consulta docs/antivirus.md."
        )
    })?;

    // The shell knows where the portable folder is, so it tells the backend
    // rather than letting it infer the path from its own location.
    let data_dir = portable_data_dir();

    app.shell()
        .command(executable.to_string_lossy().to_string())
        .args(["--data-dir", &data_dir.to_string_lossy()])
        .spawn()
        .map_err(|error| format!("No se pudo iniciar el motor local: {error}"))
}

/// Where the backend executable lives, relative to this one.
///
/// The portable layout of spec section 17 puts it in `runtime/backend/`, beside
/// the `_internal` folder PyInstaller needs. It is deliberately NOT a Tauri
/// sidecar: that mechanism expects a lone executable next to the application,
/// which a PyInstaller directory build is not.
fn backend_executable() -> Option<PathBuf> {
    let beside = std::env::current_exe().ok()?.parent()?.to_path_buf();

    let candidates = [
        // The portable folder a teacher unzips.
        beside.join("runtime").join("backend").join(BACKEND_NAME),
        // A flat layout, if someone rearranges it.
        beside.join(BACKEND_NAME),
        // `cargo tauri dev`, running from src-tauri/target/<profile>/.
        beside
            .join("..")
            .join("..")
            .join("..")
            .join("backend")
            .join("dist")
            .join("backend")
            .join(BACKEND_NAME),
    ];

    candidates.into_iter().find(|candidate| candidate.is_file())
}

/// `data/` next to the executable the teacher double-clicks.
fn portable_data_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|executable| executable.parent().map(|folder| folder.join("data")))
        .unwrap_or_else(|| PathBuf::from("data"))
}

/// Leaves a trace of a startup problem on disk.
///
/// In a release build there is no console, so without this a failure before the
/// window exists is completely invisible.
fn record(reason: &str) {
    eprintln!("[shell] {reason}");

    let data_dir = portable_data_dir();
    if std::fs::create_dir_all(&data_dir).is_err() {
        return;
    }
    let _ = std::fs::write(
        data_dir.join("arranque.log"),
        format!(
            "AI Classroom Live no pudo arrancar del todo.\r\n\r\n{reason}\r\n\r\n\
             Ejecutable: {:?}\r\n",
            std::env::current_exe().ok()
        ),
    );
}

/// Reads the backend's stdout until it announces its port, or gives up.
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
                            record(&format!("anuncio del motor ilegible: {error}"));
                            return None;
                        }
                    }
                }
            }
            CommandEvent::Stderr(line) => {
                eprint!("[backend] {}", String::from_utf8_lossy(&line));
            }
            CommandEvent::Terminated(status) => {
                record(&format!("el motor local no llegó a arrancar: {status:?}"));
                return None;
            }
            _ => {}
        }
    }
}

fn open_main_window(
    handle: &tauri::AppHandle,
    handshake: Option<Handshake>,
    problem: Option<String>,
) -> Result<(), tauri::Error> {
    if handle.get_webview_window(MAIN_WINDOW).is_some() {
        return Ok(());
    }

    let mut builder =
        WebviewWindowBuilder::new(handle, MAIN_WINDOW, WebviewUrl::App("index.html".into()))
            .title("AI Classroom Live")
            .inner_size(960.0, 820.0)
            .min_inner_size(640.0, 560.0)
            .resizable(true);

    // serde_json escapes both values, so neither can break out of the literal.
    let mut script = String::new();
    if let Some(handshake) = handshake {
        let payload = serde_json::json!({ "port": handshake.port, "token": handshake.token });
        script.push_str(&format!("window.__AICLASSROOM_BACKEND__ = {payload};"));
    }
    if let Some(reason) = problem {
        let payload = serde_json::json!(reason);
        script.push_str(&format!("window.__AICLASSROOM_ERROR__ = {payload};"));
    }
    if !script.is_empty() {
        builder = builder.initialization_script(script);
    }

    builder.build()?;
    Ok(())
}
