use serde::Serialize;
use tauri::State;

use crate::sidecar::{Supervisor, Unavailable};

#[derive(Serialize)]
pub struct BackendConfig {
    base_url: String,
    token: String,
}

/// The only way the WebView learns where the API is and which secret to send.
/// Rejects with "starting" or "failed" until the backend is healthy.
#[tauri::command]
pub fn get_backend_config(supervisor: State<'_, Supervisor>) -> Result<BackendConfig, Unavailable> {
    let backend = supervisor.backend()?;
    Ok(BackendConfig { base_url: format!("http://127.0.0.1:{}", backend.port), token: backend.token })
}
