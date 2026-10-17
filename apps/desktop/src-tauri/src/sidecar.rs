//! Launches and supervises the Python backend (docs/Agenda_Architecture.html §03, §08).
//!
//! Protocol: the secret goes to the child's stdin; the child answers on stdout with
//! `AGENDA_READY port=<n>` or `AGENDA_ERROR code=<CODE>`. A `shutdown` line on stdin
//! asks it to stop; if this process dies, the closed pipe tells the child to exit.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::async_runtime::Receiver;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::{Command, CommandChild, CommandEvent};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

const READY_PREFIX: &str = "AGENDA_READY port=";
const ERROR_PREFIX: &str = "AGENDA_ERROR ";
const MAX_ATTEMPTS: u32 = 5;
const READY_TIMEOUT: Duration = Duration::from_secs(30);
const SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

#[derive(Clone)]
pub struct Backend {
    pub port: u16,
    pub token: String,
}

/// Why the WebView cannot have a backend config right now.
#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Unavailable {
    Starting,
    Failed,
}

#[derive(Default)]
pub struct Supervisor {
    backend: Mutex<Option<Backend>>,
    child: Mutex<Option<CommandChild>>,
    child_alive: AtomicBool,
    stopping: AtomicBool,
    failed: AtomicBool,
}

impl Supervisor {
    pub fn backend(&self) -> Result<Backend, Unavailable> {
        match self.backend.lock().unwrap().clone() {
            Some(backend) => Ok(backend),
            None if self.failed.load(SeqCst) => Err(Unavailable::Failed),
            None => Err(Unavailable::Starting),
        }
    }

    /// Ask the backend to stop, wait briefly, then force it. Called once on app exit.
    pub fn shutdown(&self) {
        self.stopping.store(true, SeqCst);
        *self.backend.lock().unwrap() = None;
        let Some(mut child) = self.child.lock().unwrap().take() else {
            return;
        };
        let _ = child.write(b"shutdown\n");
        let deadline = Instant::now() + SHUTDOWN_GRACE;
        while self.child_alive.load(SeqCst) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(50));
        }
        if self.child_alive.load(SeqCst) {
            log::warn("backend did not stop in time; killing it");
            let _ = child.kill();
        }
    }

    fn kill_child(&self) {
        if let Some(child) = self.child.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}

#[derive(Clone, Serialize)]
struct Restarting {
    attempt: u32,
    retry_in_ms: u64,
}

/// Spawn the backend and keep it alive until the app exits.
pub fn start(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let mut failures = 0;
        loop {
            let was_ready = run_once(&app).await;
            if app.state::<Supervisor>().stopping.load(SeqCst) {
                return;
            }
            // A crash after a healthy run is a fresh problem, not a repeat of the last one.
            failures = if was_ready { 1 } else { failures + 1 };
            if failures >= MAX_ATTEMPTS {
                log::error("backend failed repeatedly; giving up");
                app.state::<Supervisor>().failed.store(true, SeqCst);
                let _ = app.emit("backend:failed", ());
                return;
            }
            let delay = Duration::from_secs(1 << (failures - 1));
            let _ = app.emit(
                "backend:restarting",
                Restarting { attempt: failures, retry_in_ms: delay.as_millis() as u64 },
            );
            tokio::time::sleep(delay).await;
        }
    });
}

/// One backend lifetime: spawn, handshake, drain output until it exits.
/// Returns whether it ever became ready.
async fn run_once(app: &AppHandle) -> bool {
    let supervisor = app.state::<Supervisor>();
    let token = new_secret();
    let (mut rx, mut child) = match spawn(app) {
        Ok(spawned) => spawned,
        Err(err) => {
            log::error(&format!("could not start backend: {err}"));
            return false;
        }
    };
    supervisor.child_alive.store(true, SeqCst);
    // Over stdin, never argv or env: those are visible to other local processes.
    if let Err(err) = child.write(format!("{token}\n").as_bytes()) {
        log::error(&format!("could not send launch secret: {err}"));
    }
    *supervisor.child.lock().unwrap() = Some(child);

    let became_ready = Arc::new(AtomicBool::new(false));
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                let text = String::from_utf8_lossy(&bytes);
                let line = text.trim();
                if let Some(port) = line.strip_prefix(READY_PREFIX).and_then(|p| p.parse().ok()) {
                    let (app, token, flag) = (app.clone(), token.clone(), became_ready.clone());
                    tauri::async_runtime::spawn(
                        async move { finish_handshake(app, port, token, flag).await },
                    );
                } else if let Some(reason) = line.strip_prefix(ERROR_PREFIX) {
                    log::error(&format!("backend refused to start: {reason}"));
                } else {
                    log::warn(&format!("unexpected backend stdout: {line}"));
                }
            }
            CommandEvent::Stderr(bytes) => log::backend(String::from_utf8_lossy(&bytes).trim_end()),
            CommandEvent::Error(err) => log::error(&format!("backend pipe error: {err}")),
            CommandEvent::Terminated(status) => {
                log::warn(&format!("backend exited with {:?}", status.code));
                break;
            }
            _ => {}
        }
    }

    supervisor.child_alive.store(false, SeqCst);
    *supervisor.backend.lock().unwrap() = None;
    supervisor.child.lock().unwrap().take();
    became_ready.load(SeqCst)
}

async fn finish_handshake(app: AppHandle, port: u16, token: String, became_ready: Arc<AtomicBool>) {
    let supervisor = app.state::<Supervisor>();
    if !wait_until_ready(port).await {
        log::error("backend never reported ready; restarting it");
        supervisor.kill_child();
        return;
    }
    if supervisor.stopping.load(SeqCst) {
        return;
    }
    *supervisor.backend.lock().unwrap() = Some(Backend { port, token });
    became_ready.store(true, SeqCst);
    let _ = app.emit("backend:ready", ());
}

/// Poll GET /health/ready until it answers 200. Plain TCP keeps an HTTP client out of the shell.
async fn wait_until_ready(port: u16) -> bool {
    let deadline = Instant::now() + READY_TIMEOUT;
    let request =
        format!("GET /health/ready HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    while Instant::now() < deadline {
        if let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)).await {
            let mut response = Vec::new();
            if stream.write_all(request.as_bytes()).await.is_ok()
                && stream.read_to_end(&mut response).await.is_ok()
                && response.starts_with(b"HTTP/1.1 200")
            {
                return true;
            }
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    false
}

fn spawn(app: &AppHandle) -> Result<(Receiver<CommandEvent>, CommandChild), String> {
    let data_dir = data_dir(app)?;
    backend_command(app)?.args(["--data-dir", &data_dir.to_string_lossy()]).spawn().map_err(|e| e.to_string())
}

/// Release builds run the bundled executable (bundle.externalBin).
#[cfg(not(debug_assertions))]
fn backend_command(app: &AppHandle) -> Result<Command, String> {
    app.shell().sidecar("agenda-backend").map_err(|e| e.to_string())
}

/// `tauri dev` runs the backend straight from its venv so Python edits need no rebuild.
#[cfg(debug_assertions)]
fn backend_command(app: &AppHandle) -> Result<Command, String> {
    let backend = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../services/backend");
    let python = if cfg!(windows) {
        backend.join(".venv/Scripts/python.exe")
    } else {
        backend.join(".venv/bin/python")
    };
    if !python.exists() {
        return Err(format!("{} not found; run `uv sync` in services/backend", python.display()));
    }
    Ok(app
        .shell()
        .command(python.to_string_lossy().into_owned())
        .args(["-m", "app.main", "--dev"])
        .current_dir(backend))
}

fn data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let base = app.path().app_data_dir().map_err(|e| e.to_string())?;
    // Keep dev data away from the installed app's real database.
    Ok(if cfg!(debug_assertions) { base.join("dev") } else { base })
}

fn new_secret() -> String {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).expect("OS random number generator unavailable");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Minimal logging to stderr; the backend keeps its own log file.
mod log {
    pub fn backend(line: &str) {
        eprintln!("[backend] {line}");
    }
    pub fn warn(message: &str) {
        eprintln!("[shell] warn: {message}");
    }
    pub fn error(message: &str) {
        eprintln!("[shell] error: {message}");
    }
}
