#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    collections::BTreeMap,
    str::FromStr,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use bytes::Bytes;
use kaede_api::{ApiClient, ApiClientError, InstanceEndpoint};
use kaede_audio::{
    CaptureSettings, InputMode, NativeCapture, NativePlayback, NoiseSuppression, ProcessorChain,
    SpeechProcessor, VOICE_SAMPLE_RATE, input_devices, output_devices,
};
use kaede_auth::{LoginOutcome, SessionManager};
use kaede_gateway::{GatewayCommand, GatewayHandle};
use kaede_platform::{
    AccountRegistry, DesktopPreferences, InputModePreference, KnownAccount, PlatformPaths,
    SystemCredentialVault,
};
use kaede_protocol::{Domain, EntityRef};
use kaede_turnstile::EmbeddedTurnstile;
use kaede_voice::{VoiceCommand, VoiceHandle, VoiceStatus, camera_devices, screen_sources};
use parking_lot::Mutex as SyncMutex;
use reqwest::Method;
use secrecy::SecretString;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tauri::{
    AppHandle, Manager, State, WebviewUrl, WebviewWindowBuilder,
    ipc::{InvokeBody, Request, Response},
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
#[cfg(not(target_os = "windows"))]
use tauri_plugin_notification::NotificationExt;
use tokio::sync::{Mutex, RwLock, mpsc};

type NativeSession = SessionManager<SystemCredentialVault, EmbeddedTurnstile>;

struct NativeAccount {
    api: ApiClient,
    session: Arc<NativeSession>,
    account_key: String,
}

struct PendingMfa {
    api: ApiClient,
    session: Arc<NativeSession>,
    account_key: String,
    ticket: SecretString,
}

struct NativeState {
    instance: RwLock<Option<Domain>>,
    account: RwLock<Option<Arc<NativeAccount>>>,
    restore_lock: Mutex<()>,
    pending_mfa: Mutex<Option<PendingMfa>>,
    gateway_commands: RwLock<Option<mpsc::Sender<GatewayCommand>>>,
    gateway_events_tx: mpsc::UnboundedSender<Value>,
    gateway_events_rx: Mutex<mpsc::UnboundedReceiver<Value>>,
    voice: Mutex<Option<VoiceHandle>>,
    voice_target: RwLock<Option<VoiceTarget>>,
    voice_video: Mutex<Option<mpsc::Receiver<kaede_voice::RemoteVideoFrame>>>,
    voice_generation: AtomicU64,
    voice_ui: RwLock<VoiceUiState>,
    push_to_talk_sender: Arc<SyncMutex<Option<mpsc::Sender<VoiceCommand>>>>,
    hotkey: SyncMutex<HotkeyRegistration>,
    preferences: RwLock<DesktopPreferences>,
    paths: PlatformPaths,
}

#[derive(Clone, Debug, Default)]
struct VoiceUiState {
    muted: bool,
    deafened: bool,
}

#[derive(Clone, Debug)]
struct VoiceTarget {
    reference: String,
    is_call: bool,
}

struct HotkeyRegistration {
    registered: Option<String>,
    status: String,
}

#[derive(Debug, Serialize)]
struct NativeError {
    code: String,
    message: String,
    status: u16,
    detail: Value,
}

impl NativeError {
    fn local(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_owned(),
            message: message.into(),
            status: 0,
            detail: Value::Null,
        }
    }
}

impl From<ApiClientError> for NativeError {
    fn from(error: ApiClientError) -> Self {
        if let ApiClientError::Server { status, error } = error {
            return Self {
                code: error.code.clone(),
                message: error.message.clone(),
                status: status.as_u16(),
                detail: serde_json::to_value(&*error).unwrap_or(Value::Null),
            };
        }
        Self::local("NATIVE_TRANSPORT_ERROR", error.to_string())
    }
}

#[derive(Clone, Debug, Deserialize)]
struct NativeRequest {
    method: String,
    path: String,
    body: Option<Value>,
    if_match: Option<String>,
}

#[derive(Debug, Serialize)]
struct NativeResponse {
    status: u16,
    body: Value,
    headers: BTreeMap<String, String>,
}

#[derive(Debug, Serialize)]
struct NativeSessionBootstrap {
    instance: Option<String>,
    authenticated: bool,
}

#[derive(Debug, Deserialize)]
struct NativeUploadTicket {
    upload_url: String,
    content_type: String,
    size: u64,
    #[serde(default)]
    upload_headers: std::collections::HashMap<String, String>,
}

#[derive(Debug, Serialize)]
#[allow(clippy::struct_excessive_bools)]
struct PlatformInfo {
    native: bool,
    os: &'static str,
    arch: &'static str,
    native_voice: bool,
    native_notifications: bool,
    secure_credentials: bool,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum VoiceControl {
    Mute,
    Unmute,
    Deafen,
    Undeafen,
    PushToTalkDown,
    PushToTalkUp,
    CameraOn,
    CameraOff,
    ScreenOn,
    ScreenOff,
}

#[tauri::command]
fn native_platform_info() -> PlatformInfo {
    PlatformInfo {
        native: true,
        os: std::env::consts::OS,
        arch: std::env::consts::ARCH,
        native_voice: true,
        native_notifications: true,
        secure_credentials: true,
    }
}

#[tauri::command]
async fn native_set_instance(
    instance: String,
    state: State<'_, NativeState>,
) -> Result<String, NativeError> {
    let domain = Domain::parse(instance)
        .map_err(|error| NativeError::local("INVALID_INSTANCE", error.to_string()))?;
    if state
        .account
        .read()
        .await
        .as_ref()
        .is_some_and(|account| account.api.endpoint().domain() == &domain)
    {
        *state.instance.write().await = Some(domain.clone());
        return Ok(domain.to_string());
    }
    restore_known_account(&state, Some(&domain)).await?;
    Ok(domain.to_string())
}

#[tauri::command]
async fn native_restore_session(
    state: State<'_, NativeState>,
) -> Result<NativeSessionBootstrap, NativeError> {
    let instance = restore_known_account(&state, None).await?;
    Ok(NativeSessionBootstrap {
        instance: instance.map(|domain| domain.to_string()),
        authenticated: state.account.read().await.is_some(),
    })
}

/// Restore a known account without relying on WebView storage. The account
/// index contains no secrets; refresh credentials remain in the platform vault.
async fn restore_known_account(
    state: &NativeState,
    preferred: Option<&Domain>,
) -> Result<Option<Domain>, NativeError> {
    let _restore = state.restore_lock.lock().await;

    if let Some(account) = state.account.read().await.as_ref() {
        let active = account.api.endpoint().domain().clone();
        if preferred.is_none_or(|domain| domain == &active) {
            *state.instance.write().await = Some(active.clone());
            return Ok(Some(active));
        }
    }

    // Selecting another instance suspends the current in-memory connection,
    // but deliberately keeps its refresh credential in the OS vault so the
    // user can switch back without re-entering a password.
    if preferred.is_some() && state.account.write().await.take().is_some() {
        if let Some(commands) = state.gateway_commands.write().await.take() {
            let _ = commands.send(GatewayCommand::Shutdown).await;
        }
        leave_active_voice(state).await;
    }

    let registry = AccountRegistry::load(&state.paths)
        .await
        .map_err(|error| NativeError::local("ACCOUNT_REGISTRY_FAILED", error.to_string()))?;
    let known = match preferred {
        Some(domain) => registry
            .accounts
            .iter()
            .filter(|account| account.instance == domain.to_string())
            .max_by_key(|account| account.last_used_unix_ms),
        None => registry.most_recent(),
    };

    let domain = if let Some(known) = known {
        Domain::parse(known.instance.clone())
            .map_err(|error| NativeError::local("INVALID_STORED_INSTANCE", error.to_string()))?
    } else if let Some(domain) = preferred {
        domain.clone()
    } else {
        return Ok(None);
    };
    *state.instance.write().await = Some(domain.clone());

    if let Some(known) = known {
        let api = ApiClient::new(
            InstanceEndpoint::production(domain.clone()).map_err(NativeError::from)?,
        )
        .map_err(NativeError::from)?;
        let session = new_session(api.clone(), known.account_key.clone());
        match session.restore().await {
            Ok(true) => {
                activate_account(
                    NativeAccount {
                        api,
                        session,
                        account_key: known.account_key.clone(),
                    },
                    &state,
                )
                .await?;
            }
            Ok(false) => {}
            Err(error) => tracing::warn!(
                %error,
                account = %known.account_key,
                "stored desktop session could not be restored"
            ),
        }
    }
    Ok(Some(domain))
}

async fn configured_api(state: &NativeState) -> Result<ApiClient, NativeError> {
    if let Some(account) = state.account.read().await.as_ref() {
        return Ok(account.api.clone());
    }
    if state.instance.read().await.is_none() {
        restore_known_account(state, None).await?;
        if let Some(account) = state.account.read().await.as_ref() {
            return Ok(account.api.clone());
        }
    }
    let domain = state
        .instance
        .read()
        .await
        .clone()
        .ok_or_else(|| NativeError::local("INSTANCE_REQUIRED", "Choose your home instance."))?;
    let endpoint = InstanceEndpoint::production(domain).map_err(NativeError::from)?;
    ApiClient::new(endpoint).map_err(NativeError::from)
}

fn new_session(api: ApiClient, account_key: String) -> Arc<NativeSession> {
    Arc::new(SessionManager::new(
        api,
        Arc::new(SystemCredentialVault),
        Arc::new(EmbeddedTurnstile),
        account_key,
    ))
}

async fn challenge_token(
    session: &NativeSession,
    action: &str,
) -> Result<Option<SecretString>, NativeError> {
    let config = session
        .config()
        .await
        .map_err(|error| NativeError::local("AUTH_CONFIG_FAILED", error.to_string()))?;
    if !config.turnstile.enabled {
        return Ok(None);
    }
    let site_key = config.turnstile.site_key.ok_or_else(|| {
        NativeError::local(
            "TURNSTILE_MISCONFIGURED",
            "The instance challenge is misconfigured.",
        )
    })?;
    session
        .solve_turnstile(site_key, action, uuid::Uuid::new_v4().to_string())
        .await
        .map(Some)
        .map_err(|error| NativeError::local("TURNSTILE_CANCELLED", error.to_string()))
}

async fn activate_account(account: NativeAccount, state: &NativeState) -> Result<(), NativeError> {
    if let Some(commands) = state.gateway_commands.write().await.take() {
        let _ = commands.send(GatewayCommand::Shutdown).await;
    }
    let token = account
        .session
        .access_token()
        .await
        .map_err(|error| NativeError::local("NOT_AUTHENTICATED", error.to_string()))?;
    let gateway = kaede_gateway::spawn(account.api.endpoint().gateway_url().clone(), token);
    start_gateway_forwarder(gateway, state).await;
    *state.account.write().await = Some(Arc::new(account));
    Ok(())
}

async fn start_gateway_forwarder(gateway: GatewayHandle, state: &NativeState) {
    *state.gateway_commands.write().await = Some(gateway.commands.clone());
    let mut events = gateway.events;
    let sender = state.gateway_events_tx.clone();
    tokio::spawn(async move {
        while let Some(envelope) = events.recv().await {
            if let Ok(value) = serde_json::to_value(envelope) {
                let _ = sender.send(value);
            }
        }
    });
}

fn unix_time_millis() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(i64::MAX)
}

async fn remember_account(
    state: &NativeState,
    domain: &Domain,
    account_key: &str,
    label: &str,
) -> Result<(), NativeError> {
    let mut registry = AccountRegistry::load(&state.paths)
        .await
        .map_err(|error| NativeError::local("ACCOUNT_REGISTRY_FAILED", error.to_string()))?;
    registry.remember(KnownAccount {
        instance: domain.to_string(),
        account_key: account_key.to_owned(),
        label: label.to_owned(),
        last_used_unix_ms: unix_time_millis(),
    });
    registry
        .save(&state.paths)
        .await
        .map_err(|error| NativeError::local("ACCOUNT_REGISTRY_FAILED", error.to_string()))
}

async fn forget_account(state: &NativeState, account_key: &str) -> Result<(), NativeError> {
    let mut registry = AccountRegistry::load(&state.paths)
        .await
        .map_err(|error| NativeError::local("ACCOUNT_REGISTRY_FAILED", error.to_string()))?;
    registry.forget(account_key);
    registry
        .save(&state.paths)
        .await
        .map_err(|error| NativeError::local("ACCOUNT_REGISTRY_FAILED", error.to_string()))
}

async fn login_request(body: &Value, state: &NativeState) -> Result<Value, NativeError> {
    let identifier = body
        .get("identifier")
        .and_then(Value::as_str)
        .ok_or_else(|| NativeError::local("INVALID_LOGIN", "Enter your username or email."))?;
    let password = body
        .get("password")
        .and_then(Value::as_str)
        .ok_or_else(|| NativeError::local("INVALID_LOGIN", "Enter your password."))?;
    let domain = state
        .instance
        .read()
        .await
        .clone()
        .ok_or_else(|| NativeError::local("INSTANCE_REQUIRED", "Choose your home instance."))?;
    let api =
        ApiClient::new(InstanceEndpoint::production(domain.clone()).map_err(NativeError::from)?)
            .map_err(NativeError::from)?;
    let account_key = format!("{}@{}", identifier.trim().to_ascii_lowercase(), domain);
    let session = new_session(api.clone(), account_key.clone());
    let supplied = body
        .get("turnstile_token")
        .and_then(Value::as_str)
        .map(|value| SecretString::from(value.to_owned()));
    let mut outcome = session
        .login(identifier, password, "Kaede Desktop", supplied.as_ref())
        .await
        .map_err(|error| NativeError::local("LOGIN_FAILED", error.to_string()))?;
    if matches!(outcome, LoginOutcome::ChallengeRequired) {
        let token = challenge_token(&session, "kaede-login-v1").await?;
        outcome = session
            .login(identifier, password, "Kaede Desktop", token.as_ref())
            .await
            .map_err(|error| NativeError::local("LOGIN_FAILED", error.to_string()))?;
    }
    match outcome {
        LoginOutcome::Authenticated => {
            let remembered_key = account_key.clone();
            activate_account(
                NativeAccount {
                    api,
                    session,
                    account_key,
                },
                state,
            )
            .await?;
            remember_account(state, &domain, &remembered_key, identifier).await?;
            Ok(json!({"mfa_required": false}))
        }
        LoginOutcome::MfaRequired(ticket) => {
            *state.pending_mfa.lock().await = Some(PendingMfa {
                api,
                session,
                account_key,
                ticket,
            });
            Ok(json!({"mfa_required": true, "mfa_ticket": "native-pending"}))
        }
        LoginOutcome::ChallengeRequired => Err(NativeError::local(
            "TURNSTILE_REQUIRED",
            "Verification is required to sign in.",
        )),
    }
}

async fn mfa_request(body: &Value, state: &NativeState) -> Result<Value, NativeError> {
    let code = body.get("code").and_then(Value::as_str).ok_or_else(|| {
        NativeError::local("MFA_CODE_REQUIRED", "Enter your authentication code.")
    })?;
    let pending = state
        .pending_mfa
        .lock()
        .await
        .take()
        .ok_or_else(|| NativeError::local("MFA_TICKET_INVALID", "Start sign-in again."))?;
    match pending
        .session
        .complete_mfa(&pending.ticket, code, "Kaede Desktop")
        .await
        .map_err(|error| NativeError::local("MFA_FAILED", error.to_string()))?
    {
        LoginOutcome::Authenticated => {
            let domain = pending.api.endpoint().domain().clone();
            let remembered_key = pending.account_key.clone();
            activate_account(
                NativeAccount {
                    api: pending.api,
                    session: pending.session,
                    account_key: pending.account_key,
                },
                state,
            )
            .await?;
            remember_account(state, &domain, &remembered_key, &remembered_key).await?;
            Ok(json!({"mfa_required": false}))
        }
        _ => Err(NativeError::local(
            "MFA_FAILED",
            "Authentication was not completed.",
        )),
    }
}

async fn register_request(body: &Value, state: &NativeState) -> Result<Value, NativeError> {
    let api = configured_api(state).await?;
    let session = new_session(api, "registration".to_owned());
    let username = body
        .get("username")
        .and_then(Value::as_str)
        .ok_or_else(|| NativeError::local("INVALID_REGISTRATION", "Enter a username."))?;
    let email = body.get("email").and_then(Value::as_str);
    let password = body
        .get("password")
        .and_then(Value::as_str)
        .ok_or_else(|| NativeError::local("INVALID_REGISTRATION", "Enter a password."))?;
    let challenge = challenge_token(&session, "kaede-register-v1").await?;
    let result = session
        .register(username, email, password, challenge.as_ref())
        .await
        .map_err(|error| NativeError::local("REGISTRATION_FAILED", error.to_string()))?;
    serde_json::to_value(result)
        .map_err(|error| NativeError::local("NATIVE_SERIALIZATION_ERROR", error.to_string()))
}

async fn generic_request(
    api: &ApiClient,
    request: &NativeRequest,
) -> Result<NativeResponse, ApiClientError> {
    let method = Method::from_bytes(request.method.as_bytes())
        .map_err(|_| ApiClientError::InvalidEndpoint)?;
    let response = api
        .request_json_response(
            method,
            request.path.trim_start_matches('/'),
            request.body.as_ref(),
            request.if_match.as_ref(),
        )
        .await?;
    Ok(NativeResponse {
        status: response.status.as_u16(),
        body: response.body,
        headers: response.headers.into_iter().collect(),
    })
}

#[tauri::command]
async fn native_api_request(
    request: NativeRequest,
    state: State<'_, NativeState>,
) -> Result<NativeResponse, NativeError> {
    let path = request.path.trim_start_matches('/');
    let body = request.body.as_ref().unwrap_or(&Value::Null);
    let special =
        match (request.method.as_str(), path) {
            ("POST", "auth/login") => Some(login_request(body, &state).await?),
            ("POST", "auth/mfa") => Some(mfa_request(body, &state).await?),
            ("POST", "auth/register") => Some(register_request(body, &state).await?),
            ("POST", "auth/refresh") => {
                let account = state
                    .account
                    .read()
                    .await
                    .clone()
                    .ok_or_else(|| NativeError::local("NOT_AUTHENTICATED", "Sign in again."))?;
                account.session.refresh().await.map_err(|error| {
                    NativeError::local("SESSION_REFRESH_FAILED", error.to_string())
                })?;
                Some(json!({"status": "ok"}))
            }
            ("POST", "auth/logout") => {
                if let Some(account) = state.account.write().await.take() {
                    let account_key = account.account_key.clone();
                    account
                        .session
                        .logout()
                        .await
                        .map_err(|error| NativeError::local("LOGOUT_FAILED", error.to_string()))?;
                    forget_account(&state, &account_key).await?;
                }
                if let Some(commands) = state.gateway_commands.write().await.take() {
                    let _ = commands.send(GatewayCommand::Shutdown).await;
                }
                leave_active_voice(&state).await;
                Some(json!({"status": "ok"}))
            }
            _ => {
                let api = configured_api(&state).await?;
                let response = match generic_request(&api, &request).await {
                    Ok(response) => response,
                    Err(ApiClientError::Server { status, .. }) if status.as_u16() == 401 => {
                        let account = state.account.read().await.clone().ok_or_else(|| {
                            NativeError::local("NOT_AUTHENTICATED", "Sign in again.")
                        })?;
                        account.session.refresh().await.map_err(|error| {
                            NativeError::local("SESSION_REFRESH_FAILED", error.to_string())
                        })?;
                        generic_request(&api, &request)
                            .await
                            .map_err(NativeError::from)?
                    }
                    Err(error) => return Err(error.into()),
                };
                if path == "auth/config" {
                    let mut body = response.body;
                    if body.get("turnstile").is_some() {
                        body["turnstile"]["enabled"] = Value::Bool(false);
                        body["native_challenge"] = Value::Bool(true);
                    }
                    return Ok(NativeResponse { body, ..response });
                }
                return Ok(response);
            }
        };
    let mut value = special.unwrap_or(Value::Null);
    if path == "auth/config" && value.get("turnstile").is_some() {
        value["turnstile"]["enabled"] = Value::Bool(false);
        value["native_challenge"] = Value::Bool(true);
    }
    Ok(NativeResponse {
        status: 200,
        body: value,
        headers: BTreeMap::new(),
    })
}

const NATIVE_MEDIA_MAX_BYTES: usize = 20 * 1024 * 1024;

fn validate_attachment_media_path(path: &str) -> Result<&str, NativeError> {
    if path.contains(['?', '#', '\\']) {
        return Err(NativeError::local(
            "INVALID_MEDIA_PATH",
            "The requested media path is invalid.",
        ));
    }
    let parts = path.trim_start_matches('/').split('/').collect::<Vec<_>>();
    let ["media", domain, id, variant] = parts.as_slice() else {
        return Err(NativeError::local(
            "INVALID_MEDIA_PATH",
            "The requested media path is invalid.",
        ));
    };
    Domain::parse(domain)
        .map_err(|_| NativeError::local("INVALID_MEDIA_PATH", "The media domain is invalid."))?;
    let numeric_id = id.parse::<u64>().map_err(|_| {
        NativeError::local("INVALID_MEDIA_PATH", "The media identifier is invalid.")
    })?;
    if numeric_id == 0 || numeric_id > i64::MAX as u64 || numeric_id.to_string() != *id {
        return Err(NativeError::local(
            "INVALID_MEDIA_PATH",
            "The media identifier is invalid.",
        ));
    }
    if !matches!(
        *variant,
        "original" | "thumbnail_128" | "thumbnail_512" | "thumbnail_1024" | "poster"
    ) {
        return Err(NativeError::local(
            "INVALID_MEDIA_PATH",
            "The media variant is invalid.",
        ));
    }
    Ok(path.trim_start_matches('/'))
}

#[tauri::command]
async fn native_media_request(
    path: String,
    state: State<'_, NativeState>,
) -> Result<Response, NativeError> {
    let path = validate_attachment_media_path(&path)?;
    let api = configured_api(&state).await?;
    let bytes =
        match api.get_root_bytes(path, NATIVE_MEDIA_MAX_BYTES).await {
            Ok(bytes) => bytes,
            Err(ApiClientError::Server { status, .. }) if status.as_u16() == 401 => {
                let account = state
                    .account
                    .read()
                    .await
                    .clone()
                    .ok_or_else(|| NativeError::local("NOT_AUTHENTICATED", "Sign in again."))?;
                account.session.refresh().await.map_err(|error| {
                    NativeError::local("SESSION_REFRESH_FAILED", error.to_string())
                })?;
                api.get_root_bytes(path, NATIVE_MEDIA_MAX_BYTES)
                    .await
                    .map_err(NativeError::from)?
            }
            Err(error) => return Err(error.into()),
        };
    Ok(Response::new(bytes.to_vec()))
}

#[tauri::command]
async fn native_upload_object(
    request: Request<'_>,
    state: State<'_, NativeState>,
) -> Result<(), NativeError> {
    let InvokeBody::Raw(payload) = request.body() else {
        return Err(NativeError::local(
            "INVALID_UPLOAD_BODY",
            "Expected a binary upload body.",
        ));
    };
    let length_bytes: [u8; 4] = payload
        .get(..4)
        .and_then(|value| value.try_into().ok())
        .ok_or_else(|| NativeError::local("INVALID_UPLOAD_BODY", "Upload metadata is missing."))?;
    let ticket_length = u32::from_le_bytes(length_bytes) as usize;
    let ticket_end = 4_usize.checked_add(ticket_length).ok_or_else(|| {
        NativeError::local("INVALID_UPLOAD_BODY", "Upload metadata is too large.")
    })?;
    let ticket: NativeUploadTicket =
        serde_json::from_slice(payload.get(4..ticket_end).ok_or_else(|| {
            NativeError::local("INVALID_UPLOAD_BODY", "Upload metadata is truncated.")
        })?)
        .map_err(|error| NativeError::local("INVALID_UPLOAD_BODY", error.to_string()))?;
    let bytes = payload
        .get(ticket_end..)
        .ok_or_else(|| NativeError::local("INVALID_UPLOAD_BODY", "Upload body is truncated."))?;
    let url = url::Url::parse(&ticket.upload_url)
        .map_err(|error| NativeError::local("INVALID_UPLOAD_URL", error.to_string()))?;
    let api = configured_api(&state).await?;
    api.upload_presigned(
        url,
        &ticket.content_type,
        ticket.size,
        &ticket.upload_headers,
        Bytes::copy_from_slice(bytes),
    )
    .await
    .map_err(NativeError::from)
}

#[tauri::command]
async fn native_gateway_next(state: State<'_, NativeState>) -> Result<Option<Value>, NativeError> {
    let mut receiver = state.gateway_events_rx.lock().await;
    Ok(
        tokio::time::timeout(Duration::from_secs(25), receiver.recv())
            .await
            .ok()
            .flatten(),
    )
}

#[tauri::command]
async fn native_gateway_command(
    command: String,
    payload: Value,
    state: State<'_, NativeState>,
) -> Result<(), NativeError> {
    let command = match command.as_str() {
        "presence" => GatewayCommand::Presence {
            status: payload
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("online")
                .to_owned(),
            custom_status: payload
                .get("custom_status")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned),
        },
        "request_members" => GatewayCommand::RequestMembers {
            guild_id: required_string(&payload, "guild_id")?,
            guild_domain: required_string(&payload, "guild_domain")?,
            query: payload
                .get("query")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned(),
            limit: u16::try_from(
                payload
                    .get("limit")
                    .and_then(Value::as_u64)
                    .unwrap_or(100)
                    .min(100),
            )
            .unwrap_or(100),
        },
        "subscribe_members" => GatewayCommand::SubscribeMemberList {
            guild_id: required_string(&payload, "guild_id")?,
            guild_domain: required_string(&payload, "guild_domain")?,
            ranges: serde_json::from_value(payload.get("ranges").cloned().unwrap_or_default())
                .map_err(|error| {
                    NativeError::local("INVALID_GATEWAY_COMMAND", error.to_string())
                })?,
        },
        _ => {
            return Err(NativeError::local(
                "INVALID_GATEWAY_COMMAND",
                "Unknown gateway command.",
            ));
        }
    };
    state
        .gateway_commands
        .read()
        .await
        .as_ref()
        .ok_or_else(|| {
            NativeError::local("GATEWAY_DISCONNECTED", "Realtime connection is offline.")
        })?
        .send(command)
        .await
        .map_err(|_| NativeError::local("GATEWAY_DISCONNECTED", "Realtime connection is offline."))
}

fn required_string(value: &Value, key: &str) -> Result<String, NativeError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| NativeError::local("INVALID_NATIVE_ARGUMENT", format!("Missing {key}.")))
}

fn replace_global_hotkey(
    app: &AppHandle,
    registration: &mut HotkeyRegistration,
    configured: Option<&str>,
) -> Result<(), NativeError> {
    let replacement = configured
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned);
    if replacement == registration.registered {
        return Ok(());
    }
    let manager = app.global_shortcut();
    if let Some(next) = replacement.as_deref() {
        manager.register(next).map_err(|error| {
            NativeError::local(
                "INVALID_PUSH_TO_TALK_HOTKEY",
                format!("Could not register this shortcut: {error}"),
            )
        })?;
    }
    if let Some(previous) = registration.registered.as_deref()
        && let Err(error) = manager.unregister(previous)
    {
        if let Some(next) = replacement.as_deref() {
            let _ = manager.unregister(next);
        }
        return Err(NativeError::local(
            "GLOBAL_HOTKEY_UNAVAILABLE",
            format!("Could not replace the previous shortcut: {error}"),
        ));
    }
    registration.registered = replacement;
    registration.status = configured
        .filter(|value| !value.trim().is_empty())
        .map_or_else(
            || "Global push to talk is disabled.".to_owned(),
            |value| format!("Active globally: {value}"),
        );
    Ok(())
}

#[tauri::command]
#[allow(clippy::needless_pass_by_value)]
fn native_hotkey_status(state: State<'_, NativeState>) -> String {
    state.hotkey.lock().status.clone()
}

#[tauri::command]
async fn native_audio_devices() -> Result<Value, NativeError> {
    let inputs = tokio::task::spawn_blocking(input_devices)
        .await
        .map_err(|error| NativeError::local("AUDIO_ENUMERATION_FAILED", error.to_string()))?
        .map_err(|error| NativeError::local("AUDIO_ENUMERATION_FAILED", error.to_string()))?;
    let outputs = tokio::task::spawn_blocking(output_devices)
        .await
        .map_err(|error| NativeError::local("AUDIO_ENUMERATION_FAILED", error.to_string()))?
        .map_err(|error| NativeError::local("AUDIO_ENUMERATION_FAILED", error.to_string()))?;
    let cameras = tokio::task::spawn_blocking(camera_devices)
        .await
        .map_err(|error| NativeError::local("CAMERA_ENUMERATION_FAILED", error.to_string()))?
        .unwrap_or_default();
    let screens = tokio::task::spawn_blocking(screen_sources)
        .await
        .map_err(|error| NativeError::local("SCREEN_ENUMERATION_FAILED", error.to_string()))?;
    Ok(json!({
        "inputs": inputs,
        "outputs": outputs,
        "cameras": cameras,
        "screens": screens,
    }))
}

fn capture_settings(preferences: &DesktopPreferences) -> CaptureSettings {
    CaptureSettings {
        device_id: preferences
            .input_device
            .as_ref()
            .map(|device| device.id.clone()),
        mode: match preferences.input_mode {
            InputModePreference::PushToTalk => InputMode::PushToTalk,
            InputModePreference::VoiceActivity => InputMode::VoiceActivity,
        },
        vad_threshold: preferences.vad_threshold,
        noise_suppression: match preferences.noise_suppression {
            kaede_platform::NoiseSuppressionPreference::Off => NoiseSuppression::Off,
            kaede_platform::NoiseSuppressionPreference::Standard => NoiseSuppression::Standard,
            kaede_platform::NoiseSuppressionPreference::VoiceIsolation => {
                NoiseSuppression::VoiceIsolation
            }
        },
        echo_cancellation: preferences.echo_cancellation,
        automatic_gain_control: preferences.automatic_gain_control,
    }
}

#[tauri::command]
#[allow(clippy::cast_precision_loss)]
async fn native_test_input(
    device_id: Option<String>,
    state: State<'_, NativeState>,
) -> Result<f32, NativeError> {
    let preferences = state.preferences.read().await;
    let mut settings = capture_settings(&preferences);
    settings.device_id = device_id;
    drop(preferences);
    tokio::task::spawn_blocking(move || {
        let capture = NativeCapture::open(&settings)
            .map_err(|error| NativeError::local("AUDIO_INPUT_FAILED", error.to_string()))?;
        let mut processors = ProcessorChain::default();
        processors.push(Box::new(SpeechProcessor::from_settings(&settings)));
        let mut peak = 0.0_f32;
        for _ in 0..120 {
            std::thread::sleep(Duration::from_millis(10));
            let _ = capture.drain_voice_frame(Duration::from_millis(10), &mut processors);
            peak = peak.max(capture.gate.level());
        }
        Ok(peak)
    })
    .await
    .map_err(|error| NativeError::local("AUDIO_INPUT_FAILED", error.to_string()))?
}

#[tauri::command]
#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
async fn native_test_output(
    device_id: Option<String>,
    _state: State<'_, NativeState>,
) -> Result<(), NativeError> {
    let output = device_id;
    tokio::task::spawn_blocking(move || {
        let playback = NativePlayback::open(output.as_deref())
            .map_err(|error| NativeError::local("AUDIO_OUTPUT_FAILED", error.to_string()))?;
        let frame_samples = (VOICE_SAMPLE_RATE / 100) as usize;
        for frame in 0..60 {
            let samples = (0..frame_samples)
                .map(|sample| {
                    let position = frame * frame_samples + sample;
                    let envelope = if frame < 5 {
                        frame as f32 / 5.0
                    } else if frame > 54 {
                        (60 - frame) as f32 / 5.0
                    } else {
                        1.0
                    };
                    (std::f32::consts::TAU * 440.0 * position as f32 / VOICE_SAMPLE_RATE as f32)
                        .sin()
                        * 0.12
                        * envelope
                })
                .collect::<Vec<_>>();
            playback.push_voice_frame(&samples, VOICE_SAMPLE_RATE, 1);
            std::thread::sleep(Duration::from_millis(10));
        }
        Ok(())
    })
    .await
    .map_err(|error| NativeError::local("AUDIO_OUTPUT_FAILED", error.to_string()))?
}

#[tauri::command]
async fn native_voice_join(
    reference: String,
    is_call: bool,
    state: State<'_, NativeState>,
) -> Result<(), NativeError> {
    join_native_voice(reference, is_call, &state).await
}

async fn join_native_voice(
    reference: String,
    is_call: bool,
    state: &NativeState,
) -> Result<(), NativeError> {
    let generation = state.voice_generation.fetch_add(1, Ordering::AcqRel) + 1;
    let entity = EntityRef::from_str(&reference)
        .map_err(|error| NativeError::local("INVALID_VOICE_REFERENCE", error.to_string()))?;
    let account =
        state.account.read().await.clone().ok_or_else(|| {
            NativeError::local("NOT_AUTHENTICATED", "Sign in before joining voice.")
        })?;
    let preferences = state.preferences.read().await.clone();
    let capture = capture_settings(&preferences);
    let output = preferences.output_device.map(|device| device.id);
    let mut handle = if is_call {
        kaede_voice::join_call(account.api.clone(), &entity, capture, output).await
    } else {
        kaede_voice::join_channel(account.api.clone(), &entity, capture, output).await
    }
    .map_err(|error| NativeError::local("VOICE_JOIN_FAILED", error.to_string()))?;
    if state.voice_generation.load(Ordering::Acquire) != generation {
        handle.leave().await;
        return Ok(());
    }
    *state.push_to_talk_sender.lock() = Some(handle.commands.clone());
    *state.voice_video.lock().await = handle.video_frames.take();
    if let Some(previous) = state.voice.lock().await.replace(handle) {
        previous.leave().await;
    }
    *state.voice_target.write().await = Some(VoiceTarget { reference, is_call });
    *state.voice_ui.write().await = VoiceUiState::default();
    Ok(())
}

#[tauri::command]
async fn native_voice_control(
    control: VoiceControl,
    state: State<'_, NativeState>,
) -> Result<(), NativeError> {
    let mut voice = state.voice.lock().await;
    let preferences = state.preferences.read().await.clone();
    let command = match control {
        VoiceControl::Mute => VoiceCommand::SetMuted(true),
        VoiceControl::Unmute => VoiceCommand::SetMuted(false),
        VoiceControl::Deafen => VoiceCommand::SetDeafened(true),
        VoiceControl::Undeafen => VoiceCommand::SetDeafened(false),
        VoiceControl::PushToTalkDown => VoiceCommand::SetPushToTalk(true),
        VoiceControl::PushToTalkUp => VoiceCommand::SetPushToTalk(false),
        VoiceControl::CameraOn => VoiceCommand::SetCamera {
            enabled: true,
            device_id: preferences.camera_device.map(|device| device.id),
        },
        VoiceControl::CameraOff => VoiceCommand::SetCamera {
            enabled: false,
            device_id: None,
        },
        VoiceControl::ScreenOn => VoiceCommand::SetScreenShare {
            enabled: true,
            source_id: preferences.screen_source.map(|device| device.id),
        },
        VoiceControl::ScreenOff => VoiceCommand::SetScreenShare {
            enabled: false,
            source_id: None,
        },
    };
    voice
        .as_mut()
        .ok_or_else(|| NativeError::local("VOICE_NOT_CONNECTED", "Join voice first."))?
        .commands
        .send(command)
        .await
        .map_err(|_| NativeError::local("VOICE_DISCONNECTED", "The voice session ended."))?;
    let mut ui = state.voice_ui.write().await;
    match control {
        VoiceControl::Mute => ui.muted = true,
        VoiceControl::Unmute => ui.muted = false,
        VoiceControl::Deafen => {
            ui.deafened = true;
            ui.muted = true;
        }
        VoiceControl::Undeafen => ui.deafened = false,
        _ => {}
    }
    Ok(())
}

async fn leave_active_voice(state: &NativeState) {
    state.voice_generation.fetch_add(1, Ordering::AcqRel);
    *state.push_to_talk_sender.lock() = None;
    *state.voice_video.lock().await = None;
    *state.voice_target.write().await = None;
    if let Some(voice) = state.voice.lock().await.take() {
        voice.leave().await;
    }
    *state.voice_ui.write().await = VoiceUiState::default();
}

#[tauri::command]
async fn native_voice_leave(state: State<'_, NativeState>) -> Result<(), NativeError> {
    leave_active_voice(&state).await;
    Ok(())
}

#[tauri::command]
async fn native_voice_status(state: State<'_, NativeState>) -> Result<Value, NativeError> {
    let voice = state.voice.lock().await;
    let Some(voice) = voice.as_ref() else {
        return Ok(json!({"state": "disconnected"}));
    };
    let mut value = match voice.status.borrow().clone() {
        VoiceStatus::Disconnected => json!({"state": "disconnected"}),
        VoiceStatus::Connecting => json!({"state": "connecting"}),
        VoiceStatus::Reconnecting => json!({"state": "reconnecting"}),
        VoiceStatus::Failed(message) => json!({"state": "failed", "message": message}),
        VoiceStatus::Connected {
            room,
            can_speak,
            can_stream,
            screen_sharing,
            camera_enabled,
        } => json!({"state": "connected", "room": room, "can_speak": can_speak,
                "can_stream": can_stream, "screen": screen_sharing, "camera": camera_enabled}),
        VoiceStatus::MediaError {
            message,
            room,
            can_speak,
            can_stream,
            screen_sharing,
            camera_enabled,
        } => json!({"state": "media_error", "message": message, "room": room,
                "can_speak": can_speak, "can_stream": can_stream,
                "screen": screen_sharing, "camera": camera_enabled}),
    };
    let ui = state.voice_ui.read().await;
    if let Value::Object(map) = &mut value {
        map.insert("muted".to_owned(), Value::Bool(ui.muted));
        map.insert("deafened".to_owned(), Value::Bool(ui.deafened));
        map.insert(
            "input_level".to_owned(),
            json!(voice.input_level.as_ref().map_or(0.0, |gate| gate.level())),
        );
    }
    Ok(value)
}

#[tauri::command]
async fn native_voice_next_video(
    state: State<'_, NativeState>,
) -> Result<tauri::ipc::Response, NativeError> {
    let mut receiver = state.voice_video.lock().await;
    let Some(receiver) = receiver.as_mut() else {
        return Ok(tauri::ipc::Response::new(Vec::<u8>::new()));
    };
    let Some(frame) = tokio::time::timeout(Duration::from_millis(250), receiver.recv())
        .await
        .ok()
        .flatten()
    else {
        return Ok(tauri::ipc::Response::new(Vec::<u8>::new()));
    };
    let participant = frame.participant.as_bytes();
    let participant_length: u16 = participant.len().try_into().map_err(|_| {
        NativeError::local("INVALID_VIDEO_FRAME", "Participant identity is too long.")
    })?;
    let mut packet = Vec::with_capacity(15 + participant.len() + frame.rgba.len());
    packet.extend_from_slice(b"KVD1");
    packet.extend_from_slice(&frame.width.to_le_bytes());
    packet.extend_from_slice(&frame.height.to_le_bytes());
    packet.extend_from_slice(&participant_length.to_le_bytes());
    packet.push(u8::from(frame.removed));
    packet.extend_from_slice(participant);
    packet.extend_from_slice(&frame.rgba);
    Ok(tauri::ipc::Response::new(packet))
}

#[tauri::command]
async fn native_preferences_get(state: State<'_, NativeState>) -> Result<Value, NativeError> {
    serde_json::to_value(&*state.preferences.read().await)
        .map_err(|error| NativeError::local("PREFERENCES_INVALID", error.to_string()))
}

#[tauri::command]
async fn native_preferences_set(
    preferences: DesktopPreferences,
    app: AppHandle,
    state: State<'_, NativeState>,
) -> Result<(), NativeError> {
    let previous = state.preferences.read().await.clone();
    let restart_voice = previous.input_device != preferences.input_device
        || previous.output_device != preferences.output_device
        || previous.input_mode != preferences.input_mode
        || (previous.vad_threshold - preferences.vad_threshold).abs() > f32::EPSILON
        || previous.noise_suppression != preferences.noise_suppression
        || previous.echo_cancellation != preferences.echo_cancellation
        || previous.automatic_gain_control != preferences.automatic_gain_control;
    {
        let mut hotkey = state.hotkey.lock();
        replace_global_hotkey(
            &app,
            &mut hotkey,
            preferences.push_to_talk_hotkey.as_deref(),
        )?;
    }
    preferences
        .save(&state.paths)
        .await
        .map_err(|error| NativeError::local("PREFERENCES_SAVE_FAILED", error.to_string()))?;
    *state.preferences.write().await = preferences;
    let target = if restart_voice {
        state.voice_target.read().await.clone()
    } else {
        None
    };
    if let Some(target) = target {
        join_native_voice(target.reference, target.is_call, &state).await?;
    }
    Ok(())
}

#[tauri::command]
async fn native_notify(
    app: AppHandle,
    title: String,
    body: String,
    sensitive: bool,
    _deep_link: Option<String>,
) -> Result<(), NativeError> {
    let body = if sensitive {
        "Open Kaede Chat to view this message."
    } else {
        &body
    };
    show_native_notification(&app, &title, body)
}

#[tauri::command]
fn native_notifications_prepare() -> Result<(), NativeError> {
    prepare_native_notifications()
}

#[cfg(target_os = "windows")]
// This is intentionally distinct from the old raw bundle identifier. Windows
// caches unpackaged notification identities; a human-readable, stable AUMID
// lets the registered DisplayName take effect for portable builds as well.
const WINDOWS_NOTIFICATION_APP_ID: &str = "KaedeChat.Desktop";

#[cfg(target_os = "windows")]
fn prepare_native_notifications() -> Result<(), NativeError> {
    use winreg::{RegKey, enums::HKEY_CURRENT_USER};

    let current_user = RegKey::predef(HKEY_CURRENT_USER);
    let (key, _) = current_user
        .create_subkey(format!(
            r"SOFTWARE\Classes\AppUserModelId\{WINDOWS_NOTIFICATION_APP_ID}"
        ))
        .map_err(|error| NativeError::local("NOTIFICATION_IDENTITY_FAILED", error.to_string()))?;
    key.set_value("DisplayName", &"Kaede Chat")
        .and_then(|_| key.set_value("IconBackgroundColor", &"0"))
        .map_err(|error| NativeError::local("NOTIFICATION_IDENTITY_FAILED", error.to_string()))?;
    if let Ok(executable) = std::env::current_exe() {
        let icon_path = executable.to_string_lossy().into_owned();
        key.set_value("IconUri", &icon_path).map_err(|error| {
            NativeError::local("NOTIFICATION_IDENTITY_FAILED", error.to_string())
        })?;
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn prepare_native_notifications() -> Result<(), NativeError> {
    Ok(())
}

#[cfg(target_os = "windows")]
fn show_native_notification(_app: &AppHandle, title: &str, body: &str) -> Result<(), NativeError> {
    prepare_native_notifications()?;
    tauri_winrt_notification::Toast::new(WINDOWS_NOTIFICATION_APP_ID)
        .title(title)
        .text1(body)
        .show()
        .map_err(|error| NativeError::local("NOTIFICATION_FAILED", error.to_string()))
}

#[cfg(not(target_os = "windows"))]
fn show_native_notification(app: &AppHandle, title: &str, body: &str) -> Result<(), NativeError> {
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|error| NativeError::local("NOTIFICATION_FAILED", error.to_string()))
}

#[allow(clippy::too_many_lines)]
fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "kaede=info".into()),
        )
        .init();

    let arguments = std::env::args().collect::<Vec<_>>();
    if arguments.get(1).map(String::as_str) == Some(kaede_turnstile::HELPER_FLAG) {
        let result = match (arguments.get(2), arguments.get(3), arguments.get(4)) {
            (Some(url), Some(origin), Some(request_id)) => {
                kaede_turnstile::run_helper(url, origin, request_id)
            }
            _ => Err(kaede_platform::PlatformError::Other(
                "verification helper arguments are missing".to_owned(),
            )),
        };
        if let Err(error) = result {
            eprintln!("{error}");
            std::process::exit(1);
        }
        return;
    }

    let paths = PlatformPaths::discover().unwrap_or_else(|error| {
        eprintln!("Could not locate Kaede's private application directory: {error}");
        std::process::exit(1);
    });
    let preferences =
        tauri::async_runtime::block_on(DesktopPreferences::load(&paths)).unwrap_or_default();
    let (gateway_events_tx, gateway_events_rx) = mpsc::unbounded_channel();
    let push_to_talk_sender = Arc::new(SyncMutex::new(None::<mpsc::Sender<VoiceCommand>>));
    let event_sender = push_to_talk_sender.clone();
    let hotkey = HotkeyRegistration {
        registered: None,
        status: "Global push to talk is disabled.".to_owned(),
    };
    let state = NativeState {
        instance: RwLock::new(None),
        account: RwLock::new(None),
        restore_lock: Mutex::new(()),
        pending_mfa: Mutex::new(None),
        gateway_commands: RwLock::new(None),
        gateway_events_tx,
        gateway_events_rx: Mutex::new(gateway_events_rx),
        voice: Mutex::new(None),
        voice_target: RwLock::new(None),
        voice_video: Mutex::new(None),
        voice_generation: AtomicU64::new(0),
        voice_ui: RwLock::new(VoiceUiState::default()),
        push_to_talk_sender,
        hotkey: SyncMutex::new(hotkey),
        preferences: RwLock::new(preferences),
        paths,
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |_app, _shortcut, event| {
                    if let Some(sender) = event_sender.lock().as_ref() {
                        let _ = sender.try_send(VoiceCommand::SetPushToTalk(
                            event.state == ShortcutState::Pressed,
                        ));
                    }
                })
                .build(),
        )
        .plugin(tauri_plugin_single_instance::init(
            |app, _arguments, _working_directory| {
                show_main_window(app);
            },
        ))
        .manage(state)
        .setup(|app| {
            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title(format!("Kaede Chat · {}", env!("CARGO_PKG_VERSION")))
                    .inner_size(1360.0, 860.0)
                    .min_inner_size(920.0, 620.0)
                    .resizable(true)
                    .center()
                    .on_navigation(|url| {
                        cfg!(dev)
                            || !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"))
                    })
                    .build()?;
            {
                let state = app.state::<NativeState>();
                let configured = state
                    .preferences
                    .blocking_read()
                    .push_to_talk_hotkey
                    .clone();
                let mut hotkey = state.hotkey.lock();
                if let Err(error) =
                    replace_global_hotkey(app.handle(), &mut hotkey, configured.as_deref())
                {
                    hotkey.status = error.message;
                }
            }
            let _ = window.set_title(&format!("Kaede Chat · {}", env!("CARGO_PKG_VERSION")));
            let show = MenuItem::with_id(app, "show", "Show Kaede Chat", true, None::<&str>)?;
            let leave_voice =
                MenuItem::with_id(app, "leave_voice", "Leave voice", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &leave_voice, &quit])?;
            let mut tray = TrayIconBuilder::new()
                .tooltip("Kaede Chat")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => show_main_window(app),
                    "leave_voice" => {
                        let app = app.clone();
                        tauri::async_runtime::spawn(async move {
                            let state = app.state::<NativeState>();
                            leave_active_voice(&state).await;
                        });
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if matches!(
                        event,
                        TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        }
                    ) {
                        show_main_window(tray.app_handle());
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            native_platform_info,
            native_set_instance,
            native_restore_session,
            native_api_request,
            native_media_request,
            native_upload_object,
            native_gateway_next,
            native_gateway_command,
            native_audio_devices,
            native_test_input,
            native_test_output,
            native_voice_join,
            native_voice_control,
            native_voice_leave,
            native_voice_status,
            native_voice_next_video,
            native_preferences_get,
            native_preferences_set,
            native_hotkey_status,
            native_notifications_prepare,
            native_notify,
        ])
        .run(tauri::generate_context!())
        .unwrap_or_else(|error| {
            eprintln!("Kaede desktop failed: {error}");
            std::process::exit(1);
        });
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[cfg(test)]
mod tests {
    use super::validate_attachment_media_path;

    #[test]
    fn attachment_media_paths_are_narrowly_scoped() {
        assert_eq!(
            validate_attachment_media_path("/media/chat.example/75512661369970688/thumbnail_512")
                .expect("valid attachment path"),
            "media/chat.example/75512661369970688/thumbnail_512"
        );
        for rejected in [
            "/media/chat.example/0/original",
            "/media/chat.example/01/original",
            "/media/chat.example/75512661369970688/unknown",
            "/media/../75512661369970688/original",
            "/media/chat.example/75512661369970688/original?token=secret",
            "/media/chat.example/75512661369970688/original/extra",
        ] {
            assert!(
                validate_attachment_media_path(rejected).is_err(),
                "{rejected}"
            );
        }
    }
}
