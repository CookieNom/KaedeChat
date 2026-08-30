#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    collections::{BTreeMap, BTreeSet},
    fmt::Display,
    net::IpAddr,
    str::FromStr,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use bytes::Bytes;
use kaede_api::{ApiClient, ApiClientError, InstanceEndpoint, PublicDownloadPolicy};
use kaede_audio::{
    AudioError, CaptureSettings, InputMode, NativeCapture, NativePlayback, NoiseSuppression,
    ProcessorChain, SpeechProcessor, VOICE_SAMPLE_RATE, input_devices, output_devices,
};
use kaede_auth::{AuthError, LoginOutcome, SessionManager};
use kaede_gateway::{GatewayCommand, GatewayHandle};
use kaede_platform::{
    AccountRegistry, AudioQualityPreference, DesktopPreferences, DevicePreference,
    InputModePreference, KnownAccount, PlatformError, PlatformPaths, ScreenShareProfilePreference,
    SystemCredentialVault,
};
use kaede_protocol::{Domain, EntityRef};
use kaede_turnstile::EmbeddedTurnstile;
use kaede_voice::{
    ExpectedVoicePolicy, MediaPublishSettings, ScreenShareSettings, VoiceCommand, VoiceError,
    VoiceGrantRequest, VoiceHandle, VoiceMediaSettings, VoiceStatus, camera_devices,
    screen_source_thumbnail, screen_sources,
};
use parking_lot::Mutex as SyncMutex;
use reqwest::{Method, header::HeaderValue};
use secrecy::{ExposeSecret, SecretString};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tauri::{
    AppHandle, Manager, State, WebviewUrl, WebviewWindowBuilder,
    ipc::{InvokeBody, Request, Response},
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};
#[cfg(not(target_os = "windows"))]
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_updater::UpdaterExt;
#[cfg(target_os = "windows")]
use tokio::sync::oneshot;
use tokio::sync::{Mutex, RwLock, mpsc};

type NativeSession = SessionManager<SystemCredentialVault, EmbeddedTurnstile>;

const AUTOSTART_ARGUMENT: &str = "--kaede-autostart";

fn is_autostart_launch(arguments: &[String]) -> bool {
    arguments
        .iter()
        .any(|argument| argument == AUTOSTART_ARGUMENT)
}

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
    voice_target: RwLock<Option<InstalledVoiceTarget>>,
    voice_video: Mutex<Option<mpsc::Receiver<kaede_voice::RemoteVideoFrame>>>,
    voice_install: VoiceInstallFence,
    voice_restart: mpsc::UnboundedSender<VoiceRestartRequest>,
    voice_ui: RwLock<VoiceUiState>,
    push_to_talk_sender: Arc<SyncMutex<Option<mpsc::UnboundedSender<VoiceCommand>>>>,
    hotkey: Arc<SyncMutex<HotkeyRegistration>>,
    preferences: RwLock<DesktopPreferences>,
    paths: PlatformPaths,
}

/// Serializes the small voice publication boundary without holding a lock
/// across token fetches, media setup, or `LiveKit` connection work. A newer
/// join/leave can invalidate an in-flight operation, while a completed handle
/// can only be published after checking that generation under the same lock.
struct VoiceInstallFence {
    generation: AtomicU64,
    install: Mutex<()>,
}

impl VoiceInstallFence {
    fn new() -> Self {
        Self {
            generation: AtomicU64::new(0),
            install: Mutex::new(()),
        }
    }

    async fn begin(&self) -> u64 {
        let _guard = self.install.lock().await;
        self.generation.fetch_add(1, Ordering::AcqRel) + 1
    }

    async fn lock_if_current(&self, expected: u64) -> Option<tokio::sync::MutexGuard<'_, ()>> {
        let guard = self.install.lock().await;
        if self.generation.load(Ordering::Acquire) == expected {
            Some(guard)
        } else {
            None
        }
    }

    async fn invalidate(&self) -> tokio::sync::MutexGuard<'_, ()> {
        let guard = self.install.lock().await;
        self.generation.fetch_add(1, Ordering::AcqRel);
        guard
    }

    async fn reserve_restart(
        &self,
        target: &RwLock<Option<InstalledVoiceTarget>>,
    ) -> Option<(u64, VoiceTarget)> {
        let _guard = self.install.lock().await;
        let current = self.generation.load(Ordering::Acquire);
        let installed = target.read().await.clone()?;
        if installed.generation != current {
            return None;
        }
        let generation = self.generation.fetch_add(1, Ordering::AcqRel) + 1;
        Some((generation, installed.target))
    }

    async fn reserve_restart_if_current(
        &self,
        expected: u64,
        target: &RwLock<Option<InstalledVoiceTarget>>,
    ) -> Option<(u64, VoiceTarget)> {
        let _guard = self.install.lock().await;
        if self.generation.load(Ordering::Acquire) != expected {
            return None;
        }
        let installed = target.read().await.clone()?;
        if installed.generation != expected {
            return None;
        }
        let generation = self.generation.fetch_add(1, Ordering::AcqRel) + 1;
        Some((generation, installed.target))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct VoiceRestartRequest {
    generation: u64,
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
    expected_policy: ExpectedVoicePolicy,
    e2ee_key: Option<SecretString>,
    sender_device_id: Option<String>,
    connection_id: String,
}

#[derive(Clone, Debug)]
struct InstalledVoiceTarget {
    generation: u64,
    target: VoiceTarget,
}

#[derive(Clone)]
struct RegisteredHotkey {
    configured: String,
    shortcut: Shortcut,
}

#[derive(Clone)]
struct HotkeyRegistration {
    push_to_talk: Option<RegisteredHotkey>,
    priority_push_to_talk: Option<RegisteredHotkey>,
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

    fn operation(code: &str, message: impl Into<String>, source: impl Display) -> Self {
        tracing::warn!(error_code = code, cause = %source, "native desktop operation failed");
        Self::local(code, message)
    }
}

impl From<ApiClientError> for NativeError {
    fn from(error: ApiClientError) -> Self {
        let message = error.user_message();
        match error {
            ApiClientError::Server { status, error } => Self {
                code: error.code.clone(),
                message,
                status: status.as_u16(),
                detail: serde_json::to_value(&*error).unwrap_or(Value::Null),
            },
            error => {
                let code = match &error {
                    ApiClientError::InvalidEndpoint => "INVALID_NATIVE_ENDPOINT",
                    ApiClientError::InsecureEndpoint => "INSECURE_NATIVE_ENDPOINT",
                    ApiClientError::Transport(_) => "NATIVE_TRANSPORT_ERROR",
                    ApiClientError::Decode(_) => "INVALID_SERVER_RESPONSE",
                    ApiClientError::UploadLengthMismatch => "UPLOAD_FILE_CHANGED",
                    ApiClientError::ForbiddenUploadHeader => "UNSAFE_UPLOAD_INSTRUCTIONS",
                    ApiClientError::UploadRejected(_) => "UPLOAD_REJECTED",
                    ApiClientError::DownloadRejected(_) => "MEDIA_DOWNLOAD_REJECTED",
                    ApiClientError::UnsafeNetworkTarget => "UNSAFE_MEDIA_TARGET",
                    ApiClientError::Resolve(_) => "MEDIA_RESOLUTION_FAILED",
                    ApiClientError::InvalidRedirect => "INVALID_MEDIA_REDIRECT",
                    ApiClientError::ResponseTooLarge => "MEDIA_TOO_LARGE",
                    ApiClientError::Url(_) => "INVALID_SERVER_URL",
                    ApiClientError::Header(_) => "INVALID_UPLOAD_INSTRUCTIONS",
                    ApiClientError::Server { .. } => unreachable!("server errors returned above"),
                };
                Self::operation(code, message, error)
            }
        }
    }
}

impl From<AuthError> for NativeError {
    fn from(error: AuthError) -> Self {
        match error {
            AuthError::Api(error) => error.into(),
            AuthError::Platform(PlatformError::ChallengeCancelled) => Self::local(
                "VERIFICATION_CANCELLED",
                "Verification was cancelled. Try again when you're ready.",
            ),
            AuthError::Platform(PlatformError::Keyring(error)) => Self::operation(
                "CREDENTIAL_STORE_UNAVAILABLE",
                "Kaede could not access your saved sign-in. Unlock your operating-system credential manager and try again.",
                error,
            ),
            AuthError::Platform(PlatformError::InvalidVaultRecord(error)) => Self::operation(
                "SAVED_SESSION_INVALID",
                "Your saved sign-in data is damaged. Remove this saved account and sign in again.",
                error,
            ),
            AuthError::Platform(error) => Self::operation(
                "DESKTOP_AUTHENTICATION_UNAVAILABLE",
                "Desktop sign-in could not access a required operating-system service. Restart Kaede and try again.",
                error,
            ),
            AuthError::MissingNativeTokens => Self::operation(
                "INVALID_AUTHENTICATION_RESPONSE",
                "Your instance returned an incomplete sign-in response. Update Kaede and try again; if it continues, contact your instance administrator.",
                "desktop authentication response did not contain native tokens",
            ),
            AuthError::UnexpectedAuthState => Self::operation(
                "INVALID_AUTHENTICATION_RESPONSE",
                "Your instance returned an unexpected sign-in response. Update Kaede and try again; if it continues, contact your instance administrator.",
                "the server returned an unexpected authentication state",
            ),
            AuthError::PasswordProtocolRequired => Self::local(
                "INVALID_PASSWORD_PROTOCOL",
                "This desktop flow cannot safely prepare password protection. Update Kaede and try again.",
            ),
            AuthError::InvalidPasswordProtocol => Self::local(
                "INVALID_PASSWORD_PROTOCOL",
                "This desktop flow prepared invalid password protection data. Update Kaede and try again.",
            ),
            AuthError::NotAuthenticated => Self::local(
                "NOT_AUTHENTICATED",
                "Your session is no longer available. Sign in again to continue.",
            ),
        }
    }
}

impl From<VoiceError> for NativeError {
    fn from(error: VoiceError) -> Self {
        match error {
            VoiceError::Api(error) => error.into(),
            error => {
                let message = error.user_message();
                let code = match &error {
                    VoiceError::Audio(_) => "VOICE_AUDIO_UNAVAILABLE",
                    VoiceError::LiveKit(_) => "VOICE_SERVICE_UNAVAILABLE",
                    VoiceError::Camera(_) | VoiceError::CameraWorker(_) => "CAMERA_UNAVAILABLE",
                    VoiceError::VoiceActivityDenied => "VOICE_ACTIVITY_NOT_ALLOWED",
                    VoiceError::EncryptionPolicyMismatch | VoiceError::EncryptionKeyMissing => {
                        "VOICE_E2EE_POLICY_MISMATCH"
                    }
                    VoiceError::CaptureThread(_) | VoiceError::ScreenWorker(_) => {
                        "SCREEN_CAPTURE_UNAVAILABLE"
                    }
                    VoiceError::Api(_) => unreachable!("API voice errors returned above"),
                };
                Self::operation(code, message, error)
            }
        }
    }
}

fn audio_device_error(code: &str, kind: &str, error: AudioError) -> NativeError {
    let message = match &error {
        AudioError::DeviceNotFound => format!(
            "The selected {kind} is no longer available. Choose another device and try again."
        ),
        AudioError::UnsupportedFormat(_) => format!(
            "The selected {kind} uses an audio format Kaede does not support. Choose another device and try again."
        ),
        AudioError::Backend(_) => format!(
            "Kaede could not access the selected {kind}. Check your system audio permissions and whether another app is using it, then try again."
        ),
    };
    NativeError::operation(code, message, error)
}

#[derive(Clone, Debug, Deserialize)]
struct NativeRequest {
    method: String,
    path: String,
    body: Option<Value>,
    if_match: Option<String>,
    #[serde(default)]
    headers: BTreeMap<String, String>,
}

#[derive(Debug, Default, PartialEq, Eq)]
struct NativeForwardHeaders {
    if_match: Option<String>,
    audit_log_reason: Option<String>,
}

fn native_forward_headers(request: &NativeRequest) -> Result<NativeForwardHeaders, NativeError> {
    let invalid = || {
        NativeError::local(
            "INVALID_NATIVE_HEADER",
            "The desktop client blocked an unsupported request header.",
        )
    };
    let mut forwarded = NativeForwardHeaders {
        if_match: request.if_match.clone(),
        audit_log_reason: None,
    };
    if forwarded
        .if_match
        .as_deref()
        .is_some_and(|value| HeaderValue::from_str(value).is_err())
    {
        return Err(invalid());
    }
    for (name, value) in &request.headers {
        if name.eq_ignore_ascii_case("if-match") {
            if HeaderValue::from_str(value).is_err()
                || forwarded
                    .if_match
                    .as_ref()
                    .is_some_and(|existing| existing != value)
            {
                return Err(invalid());
            }
            forwarded.if_match = Some(value.clone());
        } else if name.eq_ignore_ascii_case("x-audit-log-reason") {
            if forwarded.audit_log_reason.is_some()
                || value.len() > 512
                || HeaderValue::from_str(value).is_err()
            {
                return Err(invalid());
            }
            forwarded.audit_log_reason = Some(value.clone());
        } else {
            return Err(invalid());
        }
    }
    Ok(forwarded)
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

#[derive(Debug, Serialize)]
struct NativeUpdateStatus {
    current_version: String,
    supported: bool,
    support_message: Option<&'static str>,
    available: bool,
    version: Option<String>,
    notes: Option<String>,
    published_at: Option<String>,
}

#[derive(Debug, Serialize)]
struct NativeTaskbarPinStatus {
    supported: bool,
    allowed: bool,
    pinned: bool,
}

#[derive(Debug, Serialize)]
struct NativeAutostartStatus {
    enabled: bool,
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
    PriorityPushToTalkDown,
    PriorityPushToTalkUp,
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
#[allow(clippy::needless_pass_by_value)] // Tauri injects AppHandle command arguments by value.
fn native_autostart_status(app: AppHandle) -> Result<NativeAutostartStatus, NativeError> {
    let enabled = app.autolaunch().is_enabled().map_err(|error| {
        NativeError::operation(
            "AUTOSTART_STATUS_FAILED",
            "Kaede could not read the system sign-in startup setting.",
            error,
        )
    })?;
    Ok(NativeAutostartStatus { enabled })
}

#[tauri::command]
fn native_autostart_set(
    app: AppHandle,
    enabled: bool,
) -> Result<NativeAutostartStatus, NativeError> {
    let manager = app.autolaunch();
    let result = if enabled {
        manager.enable()
    } else {
        manager.disable()
    };
    result.map_err(|error| {
        NativeError::operation(
            "AUTOSTART_UPDATE_FAILED",
            "Kaede could not update the system sign-in startup setting.",
            error,
        )
    })?;
    native_autostart_status(app)
}

#[tauri::command]
async fn native_update_check(app: AppHandle) -> Result<NativeUpdateStatus, NativeError> {
    let current_version = app.package_info().version.to_string();
    #[cfg(target_os = "linux")]
    if std::env::var_os("APPIMAGE").is_none() {
        return Ok(NativeUpdateStatus {
            current_version,
            supported: false,
            support_message: Some(
                "Automatic installation is available for the AppImage build. Package-manager installations should be updated through that package manager.",
            ),
            available: false,
            version: None,
            notes: None,
            published_at: None,
        });
    }
    let updater = app.updater().map_err(|error| {
        NativeError::operation(
            "UPDATE_CONFIGURATION_FAILED",
            "Kaede could not initialize its signed update checker.",
            error,
        )
    })?;
    let update = updater.check().await.map_err(|error| {
        NativeError::operation(
            "UPDATE_CHECK_FAILED",
            "Kaede could not check GitHub Releases for updates. Check your connection and try again.",
            error,
        )
    })?;

    Ok(match update {
        Some(update) => NativeUpdateStatus {
            current_version,
            supported: true,
            support_message: None,
            available: true,
            version: Some(update.version.clone()),
            notes: update.body,
            published_at: update.date.map(|date| date.to_string()),
        },
        None => NativeUpdateStatus {
            current_version,
            supported: true,
            support_message: None,
            available: false,
            version: None,
            notes: None,
            published_at: None,
        },
    })
}

#[tauri::command]
async fn native_update_install(app: AppHandle) -> Result<(), NativeError> {
    #[cfg(target_os = "linux")]
    if std::env::var_os("APPIMAGE").is_none() {
        return Err(NativeError::local(
            "UPDATE_UNSUPPORTED_INSTALLATION",
            "This Linux installation is managed by a package manager. Update Kaede through that package manager, or use the AppImage build for in-app updates.",
        ));
    }
    let updater = app.updater().map_err(|error| {
        NativeError::operation(
            "UPDATE_CONFIGURATION_FAILED",
            "Kaede could not initialize its signed update installer.",
            error,
        )
    })?;
    let Some(update) = updater.check().await.map_err(|error| {
        NativeError::operation(
            "UPDATE_CHECK_FAILED",
            "Kaede could not verify the available update. Check your connection and try again.",
            error,
        )
    })?
    else {
        return Ok(());
    };
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|error| {
            NativeError::operation(
                "UPDATE_INSTALL_FAILED",
                "Kaede could not download or install the signed update. Try again shortly.",
                error,
            )
        })?;
    app.restart();
}

#[cfg(target_os = "windows")]
fn windows_taskbar_manager() -> Result<windows::UI::Shell::TaskbarManager, NativeError> {
    use windows::UI::Shell::{ITaskbarManagerDesktopAppSupportStatics, TaskbarManager};

    windows::core::factory::<TaskbarManager, ITaskbarManagerDesktopAppSupportStatics>().map_err(
        |error| {
            NativeError::operation(
                "TASKBAR_PIN_UNAVAILABLE",
                "This Windows version does not support taskbar pin requests from desktop apps. You can right-click Kaede in the taskbar and choose Pin to taskbar.",
                error,
            )
        },
    )?;
    windows::UI::Shell::TaskbarManager::GetDefault().map_err(|error| {
        NativeError::operation(
            "TASKBAR_PIN_UNAVAILABLE",
            "Windows does not offer taskbar pinning to this installation. You can right-click Kaede in the taskbar and choose Pin to taskbar.",
            error,
        )
    })
}

#[cfg(target_os = "windows")]
#[tauri::command]
async fn native_taskbar_pin_status() -> Result<NativeTaskbarPinStatus, NativeError> {
    let manager = windows_taskbar_manager()?;
    let supported = manager.IsSupported().unwrap_or(false);
    let allowed = supported && manager.IsPinningAllowed().unwrap_or(false);
    let pinned = if supported {
        manager
            .IsCurrentAppPinnedAsync()
            .map_err(|error| {
                NativeError::operation(
                    "TASKBAR_PIN_STATUS_FAILED",
                    "Kaede could not read its Windows taskbar pin status.",
                    error,
                )
            })?
            .await
            .unwrap_or(false)
    } else {
        false
    };
    Ok(NativeTaskbarPinStatus {
        supported,
        allowed,
        pinned,
    })
}

#[cfg(not(target_os = "windows"))]
#[tauri::command]
async fn native_taskbar_pin_status() -> Result<NativeTaskbarPinStatus, NativeError> {
    Ok(NativeTaskbarPinStatus {
        supported: false,
        allowed: false,
        pinned: false,
    })
}

#[cfg(target_os = "windows")]
#[tauri::command]
async fn native_taskbar_pin_request(app: AppHandle) -> Result<NativeTaskbarPinStatus, NativeError> {
    let (sender, receiver) = oneshot::channel();
    let foreground_app = app.clone();
    app.run_on_main_thread(move || {
        show_main_window(&foreground_app);
        let operation = windows_taskbar_manager().and_then(|manager| {
            if !manager.IsSupported().unwrap_or(false)
                || !manager.IsPinningAllowed().unwrap_or(false)
            {
                return Err(NativeError::local(
                    "TASKBAR_PIN_NOT_ALLOWED",
                    "Windows is not allowing this app to request a taskbar pin. Right-click Kaede in the taskbar and choose Pin to taskbar.",
                ));
            }
            manager.RequestPinCurrentAppAsync().map_err(|error| {
                NativeError::operation(
                    "TASKBAR_PIN_FAILED",
                    "Windows could not open the taskbar pin confirmation. Right-click Kaede in the taskbar and choose Pin to taskbar.",
                    error,
                )
            })
        });
        let _ = sender.send(operation);
    })
    .map_err(|error| {
        NativeError::operation(
            "TASKBAR_PIN_FAILED",
            "Kaede could not bring its Windows taskbar pin request to the foreground.",
            error,
        )
    })?;
    let operation = receiver.await.map_err(|error| {
        NativeError::operation(
            "TASKBAR_PIN_FAILED",
            "Kaede could not start the Windows taskbar pin request.",
            error,
        )
    })??;
    let pinned = operation
        .await
        .map_err(|error| {
            NativeError::operation(
                "TASKBAR_PIN_FAILED",
                "Windows did not complete the taskbar pin request. You can pin Kaede from its taskbar context menu.",
                error,
            )
        })?;
    Ok(NativeTaskbarPinStatus {
        supported: true,
        allowed: true,
        pinned,
    })
}

#[cfg(not(target_os = "windows"))]
#[tauri::command]
async fn native_taskbar_pin_request(
    _app: AppHandle,
) -> Result<NativeTaskbarPinStatus, NativeError> {
    Err(NativeError::local(
        "TASKBAR_PIN_UNAVAILABLE",
        "Taskbar pinning is only available in the Windows desktop app.",
    ))
}

#[tauri::command]
async fn native_set_instance(
    instance: String,
    state: State<'_, NativeState>,
) -> Result<String, NativeError> {
    let domain = Domain::parse(instance).map_err(|error| {
        NativeError::operation(
            "INVALID_INSTANCE",
            "Enter a valid instance domain, such as chat.example.",
            error,
        )
    })?;
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

/// Restore a known account without relying on `WebView` storage. The account
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

    let registry = AccountRegistry::load(&state.paths).await.map_err(|error| {
        NativeError::operation(
            "ACCOUNT_REGISTRY_FAILED",
            "Kaede could not read your saved accounts. Restart the app and try again.",
            error,
        )
    })?;
    let known = match preferred {
        Some(domain) => registry
            .accounts
            .iter()
            .filter(|account| account.instance == domain.to_string())
            .max_by_key(|account| account.last_used_unix_ms),
        None => registry.most_recent(),
    };

    let domain = if let Some(known) = known {
        Domain::parse(known.instance.clone()).map_err(|error| {
            NativeError::operation(
                "INVALID_STORED_INSTANCE",
                "A saved account has an invalid instance address. Remove that saved account and sign in again.",
                error,
            )
        })?
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
                    state,
                )
                .await?;
            }
            Ok(false) => {}
            Err(error) => {
                tracing::warn!(
                    %error,
                    account = %known.account_key,
                    "stored desktop session could not be restored"
                );
                return Err(error.into());
            }
        }
    }
    Ok(Some(domain))
}

async fn configured_api(state: &NativeState) -> Result<ApiClient, NativeError> {
    if let Some(account) = state.account.read().await.as_ref() {
        return Ok(account.api.clone());
    }

    // A hard process restart can reach an API command before the WebView's
    // startup hook. Retry restoration whenever there is no active account,
    // including when the WebView already remembered the preferred instance.
    let preferred = state.instance.read().await.clone();
    restore_known_account(state, preferred.as_ref()).await?;
    if let Some(account) = state.account.read().await.as_ref() {
        return Ok(account.api.clone());
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
    let config = session.config().await.map_err(NativeError::from)?;
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
        .map_err(NativeError::from)
}

async fn activate_account(account: NativeAccount, state: &NativeState) -> Result<(), NativeError> {
    if let Some(commands) = state.gateway_commands.write().await.take() {
        let _ = commands.send(GatewayCommand::Shutdown).await;
    }
    let token = account
        .session
        .access_token()
        .await
        .map_err(NativeError::from)?;
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
        .map_err(|error| {
            NativeError::operation(
                "ACCOUNT_REGISTRY_FAILED",
                "Kaede could not read your saved accounts. Your sign-in succeeded, but this account could not be added to the account chooser.",
                error,
            )
        })?;
    registry.remember(KnownAccount {
        instance: domain.to_string(),
        account_key: account_key.to_owned(),
        label: label.to_owned(),
        last_used_unix_ms: unix_time_millis(),
    });
    registry
        .save(&state.paths)
        .await
        .map_err(|error| {
            NativeError::operation(
                "ACCOUNT_REGISTRY_FAILED",
                "Your sign-in succeeded, but Kaede could not save this account in the account chooser. Check that the app can write to its settings directory.",
                error,
            )
        })
}

async fn forget_account(state: &NativeState, account_key: &str) -> Result<(), NativeError> {
    let mut registry = AccountRegistry::load(&state.paths).await.map_err(|error| {
        NativeError::operation(
            "ACCOUNT_REGISTRY_FAILED",
            "Kaede could not read your saved accounts. Restart the app and try signing out again.",
            error,
        )
    })?;
    registry.forget(account_key);
    registry
        .save(&state.paths)
        .await
        .map_err(|error| {
            NativeError::operation(
                "ACCOUNT_REGISTRY_FAILED",
                "You were signed out, but Kaede could not remove the account from the account chooser. Restart the app and remove it again.",
                error,
            )
        })
}

fn submitted_password_kdf_version(body: &Value) -> Result<u8, NativeError> {
    let Some(value) = body.get("password_kdf_version") else {
        return Err(NativeError::local(
            "INVALID_PASSWORD_PROTOCOL",
            "This Kaede client did not submit password protection metadata. Update the app and try again.",
        ));
    };
    match value.as_u64() {
        Some(2) => Ok(2),
        _ => Err(NativeError::local(
            "INVALID_PASSWORD_PROTOCOL",
            "This Kaede client submitted an unsupported password protection version. Update the app and try again.",
        )),
    }
}

fn canonical_base64url_material(value: &str, decoded_length: usize) -> bool {
    let Ok(mut decoded) = URL_SAFE_NO_PAD.decode(value) else {
        return false;
    };
    let valid = decoded.len() == decoded_length && URL_SAFE_NO_PAD.encode(&decoded) == value;
    decoded.fill(0);
    valid
}

fn submitted_derived_password(body: &Value) -> Result<&str, NativeError> {
    let password = body
        .get("password")
        .and_then(Value::as_str)
        .filter(|value| value.len() == 43 && canonical_base64url_material(value, 32));
    password.ok_or_else(|| {
        NativeError::local(
            "INVALID_PASSWORD_PROTOCOL",
            "This Kaede client did not prepare password protection correctly. Update the app and try again.",
        )
    })
}

fn validate_password_kdf(body: &Value, require_vault_salt: bool) -> Result<(), NativeError> {
    let Some(kdf) = body.get("password_kdf").and_then(Value::as_object) else {
        return Err(NativeError::local(
            "INVALID_PASSWORD_PROTOCOL",
            "This Kaede client did not submit password protection metadata. Update the app and try again.",
        ));
    };
    let expected_fields = if require_vault_salt { 5 } else { 4 };
    let auth_salt_valid = kdf
        .get("auth_salt")
        .and_then(Value::as_str)
        .is_some_and(|value| value.len() == 22 && canonical_base64url_material(value, 16));
    let vault_salt_valid = !require_vault_salt
        || kdf
            .get("vault_salt")
            .and_then(Value::as_str)
            .is_some_and(|value| value.len() == 22 && canonical_base64url_material(value, 16));
    if kdf.len() != expected_fields
        || kdf.get("version").and_then(Value::as_u64) != Some(2)
        || kdf.get("algorithm").and_then(Value::as_str) != Some("PBKDF2-SHA256")
        || kdf.get("iterations").and_then(Value::as_u64) != Some(600_000)
        || !auth_salt_valid
        || !vault_salt_valid
    {
        return Err(NativeError::local(
            "INVALID_PASSWORD_PROTOCOL",
            "This Kaede client submitted invalid password protection metadata. Update the app and try again.",
        ));
    }
    Ok(())
}

fn validate_native_password_request(
    method: &str,
    path: &str,
    body: &Value,
) -> Result<(), NativeError> {
    let password_bearing = body.get("password").is_some();
    match (method, path) {
        ("POST", "auth/login" | "auth/email/change" | "auth/mfa/setup" | "auth/mfa/disable") => {
            submitted_derived_password(body)?;
            submitted_password_kdf_version(body)?;
            if path == "auth/login" && body.get("password_upgrade").is_some() {
                return Err(NativeError::local(
                    "INVALID_PASSWORD_PROTOCOL",
                    "Password upgrades are not supported during sign-in. Reset the account password before trying again.",
                ));
            }
        }
        ("POST", "auth/register") => {
            submitted_derived_password(body)?;
            validate_password_kdf(body, true)?;
        }
        ("POST", "auth/password/reset") => {
            submitted_derived_password(body)?;
            validate_password_kdf(body, false)?;
        }
        _ if password_bearing => {
            return Err(NativeError::local(
                "INVALID_PASSWORD_PROTOCOL",
                "This Kaede client attempted to send password material through an unsupported route. Update the app and try again.",
            ));
        }
        _ => {}
    }
    Ok(())
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
    let password_kdf_version = submitted_password_kdf_version(body)?;
    if body.get("password_upgrade").is_some() {
        return Err(NativeError::local(
            "INVALID_PASSWORD_PROTOCOL",
            "Password upgrades are not supported during sign-in. Reset the account password before trying again.",
        ));
    }
    debug_assert_eq!(password_kdf_version, 2);
    let mut outcome = session
        .login_with_password_protocol(identifier, password, "Kaede Desktop", supplied.as_ref())
        .await
        .map_err(NativeError::from)?;
    if matches!(outcome, LoginOutcome::ChallengeRequired) {
        let token = challenge_token(&session, "kaede-login-v1").await?;
        outcome = session
            .login_with_password_protocol(identifier, password, "Kaede Desktop", token.as_ref())
            .await
            .map_err(NativeError::from)?;
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
        .ok_or_else(|| {
            NativeError::local(
                "MFA_TICKET_INVALID",
                "Your sign-in attempt expired. Start sign-in again to request a new authentication challenge.",
            )
        })?;
    match pending
        .session
        .complete_mfa(&pending.ticket, code, "Kaede Desktop")
        .await
        .map_err(NativeError::from)?
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
            "Authentication was not completed. Start sign-in again and retry your authentication code.",
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
    let password_kdf = body
        .get("password_kdf")
        .filter(|value| !value.is_null())
        .ok_or_else(|| {
            NativeError::local(
                "INVALID_PASSWORD_PROTOCOL",
                "This Kaede client did not prepare password protection metadata. Update the app and try again.",
            )
        })?;
    let challenge = challenge_token(&session, "kaede-register-v1").await?;
    let result = session
        .register_with_password_protocol(
            username,
            email,
            password,
            password_kdf,
            challenge.as_ref(),
        )
        .await
        .map_err(NativeError::from)?;
    serde_json::to_value(result).map_err(|error| {
        NativeError::operation(
            "INVALID_REGISTRATION_RESPONSE",
            "Your account was created, but Kaede could not read the confirmation. Try signing in.",
            error,
        )
    })
}

async fn generic_request(
    api: &ApiClient,
    request: &NativeRequest,
    headers: &NativeForwardHeaders,
) -> Result<NativeResponse, ApiClientError> {
    let method = Method::from_bytes(request.method.as_bytes())
        .map_err(|_| ApiClientError::InvalidEndpoint)?;
    let response = api
        .request_json_response(
            method,
            request.path.trim_start_matches('/'),
            request.body.as_ref(),
            headers.if_match.as_deref(),
            headers.audit_log_reason.as_deref(),
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
    let forwarded_headers = native_forward_headers(&request)?;
    // The native bridge is the last trusted boundary before network I/O. Do
    // not let an old or damaged bundled webview transmit literal passwords
    // while relying on the server to reject their malformed metadata.
    validate_native_password_request(&request.method, path, body)?;
    let special = match (request.method.as_str(), path) {
        ("POST", "auth/login") => Some(login_request(body, &state).await?),
        ("POST", "auth/mfa") => Some(mfa_request(body, &state).await?),
        ("POST", "auth/register") => Some(register_request(body, &state).await?),
        ("POST", "auth/refresh") => {
            let account = state.account.read().await.clone().ok_or_else(|| {
                NativeError::local(
                    "NOT_AUTHENTICATED",
                    "Your session is no longer available. Sign in again to continue.",
                )
            })?;
            account.session.refresh().await.map_err(NativeError::from)?;
            Some(json!({"status": "ok"}))
        }
        ("POST", "auth/logout") => {
            if let Some(account) = state.account.write().await.take() {
                let account_key = account.account_key.clone();
                account.session.logout().await.map_err(NativeError::from)?;
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
            let response = match generic_request(&api, &request, &forwarded_headers).await {
                Ok(response) => response,
                Err(ApiClientError::Server { status, .. }) if status.as_u16() == 401 => {
                    let account = state.account.read().await.clone().ok_or_else(|| {
                        NativeError::local(
                            "NOT_AUTHENTICATED",
                            "Your session is no longer available. Sign in again to continue.",
                        )
                    })?;
                    account.session.refresh().await.map_err(NativeError::from)?;
                    generic_request(&api, &request, &forwarded_headers)
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
const NATIVE_SOUNDBOARD_MAX_BYTES: usize = 512 * 1024;

#[derive(Debug)]
struct ValidatedSoundboardMedia {
    url: url::Url,
    network_policy: PublicDownloadPolicy,
}

fn validate_soundboard_media_url(
    value: &str,
    authority_domain: &str,
    media_origin: &str,
    configured_authority: &Domain,
    configured_origin: &url::Url,
) -> Result<ValidatedSoundboardMedia, NativeError> {
    let authority = Domain::parse(authority_domain).map_err(|_| {
        NativeError::local(
            "INVALID_SOUNDBOARD_SOURCE",
            "Kaede blocked a guild sound with an invalid source instance.",
        )
    })?;
    let url = url::Url::parse(value).map_err(|_| {
        NativeError::local(
            "INVALID_SOUNDBOARD_URL",
            "Kaede blocked an invalid guild sound link.",
        )
    })?;
    let expected_origin = url::Url::parse(media_origin).map_err(|_| {
        NativeError::local(
            "INVALID_SOUNDBOARD_ORIGIN",
            "Kaede blocked a guild sound with an invalid media origin.",
        )
    })?;
    let canonical_origin = expected_origin.origin().ascii_serialization();
    if url.scheme() != "https"
        || expected_origin.scheme() != "https"
        || expected_origin.path() != "/"
        || expected_origin.query().is_some()
        || expected_origin.fragment().is_some()
        || !expected_origin.username().is_empty()
        || expected_origin.password().is_some()
        || media_origin.trim_end_matches('/') != canonical_origin
        || url.origin().ascii_serialization() != canonical_origin
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err(NativeError::local(
            "UNSAFE_SOUNDBOARD_URL",
            "Kaede blocked a guild sound link from an unexpected media origin.",
        ));
    }
    let media_host = url.host_str().ok_or_else(|| {
        NativeError::local(
            "INVALID_SOUNDBOARD_URL",
            "Kaede blocked an invalid guild sound link.",
        )
    })?;
    let configured_host = configured_origin.host_str().unwrap_or_default();
    let configured_loopback = is_explicit_loopback_host(configured_host);
    let local_media_host = is_explicit_loopback_host(media_host);
    let authority_media_host = media_host == authority.as_str()
        || media_host
            .strip_suffix(authority.as_str())
            .is_some_and(|prefix| prefix.ends_with('.') && prefix.len() > 1);
    let allow_loopback_development = authority == *configured_authority
        && configured_loopback
        && local_media_host
        && (media_host == configured_host || authority_media_host);
    if local_media_host && !allow_loopback_development {
        return Err(NativeError::local(
            "UNSAFE_SOUNDBOARD_URL",
            "Kaede blocked a guild sound link that targets a local or private network.",
        ));
    }
    Ok(ValidatedSoundboardMedia {
        url,
        network_policy: if allow_loopback_development {
            PublicDownloadPolicy::LoopbackDevelopmentOnly
        } else {
            PublicDownloadPolicy::PublicOnly
        },
    })
}

fn is_explicit_loopback_host(value: &str) -> bool {
    value == "localhost"
        || value.ends_with(".localhost")
        || value
            .parse::<IpAddr>()
            .is_ok_and(|address| address.is_loopback())
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn validate_attachment_media_path(path: &str) -> Result<&str, NativeError> {
    if path.contains(['#', '\\']) || !path.starts_with('/') || path.starts_with("//") {
        return Err(NativeError::local(
            "INVALID_MEDIA_PATH",
            "This media link is invalid. Ask the sender to upload the file again.",
        ));
    }
    if validate_history_media_path(path) {
        return Ok(path.trim_start_matches('/'));
    }
    if path.contains('?') {
        return Err(NativeError::local(
            "INVALID_MEDIA_PATH",
            "This media link is invalid. Ask the sender to upload the file again.",
        ));
    }
    let parts = path.trim_start_matches('/').split('/').collect::<Vec<_>>();
    let ["media", domain, id, variant] = parts.as_slice() else {
        return Err(NativeError::local(
            "INVALID_MEDIA_PATH",
            "This media link is invalid. Ask the sender to upload the file again.",
        ));
    };
    Domain::parse(domain).map_err(|_| {
        NativeError::local(
            "INVALID_MEDIA_PATH",
            "This media link contains an invalid instance address. Ask the sender to upload the file again.",
        )
    })?;
    let numeric_id = id.parse::<u64>().map_err(|_| {
        NativeError::local(
            "INVALID_MEDIA_PATH",
            "This media link contains an invalid file identifier. Ask the sender to upload the file again.",
        )
    })?;
    if numeric_id == 0 || numeric_id > i64::MAX as u64 || numeric_id.to_string() != *id {
        return Err(NativeError::local(
            "INVALID_MEDIA_PATH",
            "This media link contains an invalid file identifier. Ask the sender to upload the file again.",
        ));
    }
    if !matches!(
        *variant,
        "original" | "thumbnail_128" | "thumbnail_512" | "thumbnail_1024" | "poster"
    ) {
        return Err(NativeError::local(
            "INVALID_MEDIA_PATH",
            "This media preview is unavailable. Open the original file or ask the sender to upload it again.",
        ));
    }
    Ok(path.trim_start_matches('/'))
}

fn validate_history_media_path(path: &str) -> bool {
    let Some((route, query)) = path.split_once('?') else {
        return false;
    };
    let parts = route.trim_start_matches('/').split('/').collect::<Vec<_>>();
    let [
        "api",
        "v1",
        "dms",
        conversation,
        "history-media",
        message,
        attachment,
        variant,
    ] = parts.as_slice()
    else {
        return false;
    };
    if conversation.parse::<EntityRef>().is_err()
        || message.parse::<EntityRef>().is_err()
        || attachment.parse::<EntityRef>().is_err()
        || !matches!(
            *variant,
            "original" | "thumbnail_128" | "thumbnail_512" | "thumbnail_1024" | "poster"
        )
    {
        return false;
    }
    let pairs = url::form_urlencoded::parse(query.as_bytes()).collect::<Vec<_>>();
    if pairs.len() != 2 {
        return false;
    }
    let expires = pairs
        .iter()
        .find(|(key, _)| key == "expires")
        .map(|(_, value)| value.as_ref());
    let token = pairs
        .iter()
        .find(|(key, _)| key == "token")
        .map(|(_, value)| value.as_ref());
    expires.is_some_and(|value| {
        !value.is_empty()
            && value.bytes().all(|byte| byte.is_ascii_digit())
            && value.parse::<u64>().is_ok_and(|timestamp| timestamp > 0)
    }) && token.is_some_and(|value| {
        (40..=48).contains(&value.len())
            && value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    })
}

#[tauri::command]
async fn native_media_request(
    path: String,
    state: State<'_, NativeState>,
) -> Result<Response, NativeError> {
    let path = validate_attachment_media_path(&path)?;
    let api = configured_api(&state).await?;
    let bytes = match api.get_root_bytes(path, NATIVE_MEDIA_MAX_BYTES).await {
        Ok(bytes) => bytes,
        Err(ApiClientError::Server { status, .. }) if status.as_u16() == 401 => {
            let account = state.account.read().await.clone().ok_or_else(|| {
                NativeError::local(
                    "NOT_AUTHENTICATED",
                    "Your session is no longer available. Sign in again to continue.",
                )
            })?;
            account.session.refresh().await.map_err(NativeError::from)?;
            api.get_root_bytes(path, NATIVE_MEDIA_MAX_BYTES)
                .await
                .map_err(NativeError::from)?
        }
        Err(error) => return Err(error.into()),
    };
    Ok(Response::new(bytes.to_vec()))
}

#[tauri::command]
async fn native_soundboard_media(
    url: String,
    authority_domain: String,
    media_origin: String,
    expected_sha256: String,
    state: State<'_, NativeState>,
) -> Result<Response, NativeError> {
    if !valid_sha256(&expected_sha256) {
        return Err(NativeError::local(
            "INVALID_SOUNDBOARD_DIGEST",
            "Kaede blocked a guild sound with invalid integrity information.",
        ));
    }
    let api = configured_api(&state).await?;
    let configured_origin = api.endpoint().public_origin();
    let target = validate_soundboard_media_url(
        &url,
        &authority_domain,
        &media_origin,
        api.endpoint().domain(),
        &configured_origin,
    )?;
    let bytes = tokio::time::timeout(
        Duration::from_secs(10),
        api.get_public_bytes_no_redirect(
            &target.url,
            NATIVE_SOUNDBOARD_MAX_BYTES,
            target.network_policy,
        ),
    )
    .await
    .map_err(|error| {
        NativeError::operation(
            "SOUNDBOARD_DOWNLOAD_TIMEOUT",
            "The guild sound download timed out. Ask someone to play it again.",
            error,
        )
    })?
    .map_err(|error| {
        NativeError::operation(
            "SOUNDBOARD_DOWNLOAD_FAILED",
            "The guild sound could not be downloaded safely. Ask someone to play it again.",
            error,
        )
    })?;
    if bytes.is_empty() {
        return Err(NativeError::local(
            "EMPTY_SOUNDBOARD_MEDIA",
            "The guild sound response was empty.",
        ));
    }
    let actual_sha256 = format!("{:x}", Sha256::digest(&bytes));
    if actual_sha256 != expected_sha256 {
        return Err(NativeError::local(
            "SOUNDBOARD_INTEGRITY_FAILED",
            "Kaede blocked a guild sound that failed its integrity check.",
        ));
    }
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
            "Kaede could not read the selected file. Select it again and retry the upload.",
        ));
    };
    let length_bytes: [u8; 4] = payload
        .get(..4)
        .and_then(|value| value.try_into().ok())
        .ok_or_else(|| {
            NativeError::local(
                "INVALID_UPLOAD_BODY",
                "Upload instructions are missing. Select the file again and retry.",
            )
        })?;
    let ticket_length = u32::from_le_bytes(length_bytes) as usize;
    let ticket_end = 4_usize.checked_add(ticket_length).ok_or_else(|| {
        NativeError::local(
            "INVALID_UPLOAD_BODY",
            "The upload instructions are invalid. Select the file again and retry.",
        )
    })?;
    let ticket: NativeUploadTicket =
        serde_json::from_slice(payload.get(4..ticket_end).ok_or_else(|| {
            NativeError::local(
                "INVALID_UPLOAD_BODY",
                "The upload instructions are incomplete. Select the file again and retry.",
            )
        })?)
        .map_err(|error| {
            NativeError::operation(
                "INVALID_UPLOAD_BODY",
                "Kaede could not read the upload instructions. Select the file again and retry.",
                error,
            )
        })?;
    let bytes = payload.get(ticket_end..).ok_or_else(|| {
        NativeError::local(
            "INVALID_UPLOAD_BODY",
            "Kaede could not read the complete file. Select it again and retry the upload.",
        )
    })?;
    let url = url::Url::parse(&ticket.upload_url).map_err(|error| {
        NativeError::operation(
            "INVALID_UPLOAD_URL",
            "The upload link is invalid or expired. Select the file again to request a new link.",
            error,
        )
    })?;
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
    let command = decode_gateway_command(&command, &payload)?;
    state
        .gateway_commands
        .read()
        .await
        .as_ref()
        .ok_or_else(|| {
            NativeError::local(
                "GATEWAY_DISCONNECTED",
                "Realtime updates are offline. Check your connection and wait for Kaede to reconnect.",
            )
        })?
        .send(command)
        .await
        .map_err(|_| {
            NativeError::local(
                "GATEWAY_DISCONNECTED",
                "Realtime updates disconnected before this action was sent. Wait for Kaede to reconnect and try again.",
            )
        })
}

fn decode_gateway_command(command: &str, payload: &Value) -> Result<GatewayCommand, NativeError> {
    let decoded = match command {
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
            guild_id: required_string(payload, "guild_id")?,
            guild_domain: required_string(payload, "guild_domain")?,
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
            guild_id: required_string(payload, "guild_id")?,
            guild_domain: required_string(payload, "guild_domain")?,
            ranges: serde_json::from_value(payload.get("ranges").cloned().unwrap_or_default())
                .map_err(|error| {
                    NativeError::operation(
                        "INVALID_GATEWAY_COMMAND",
                        "Kaede could not request the member list. Close and reopen the guild, then try again.",
                        error,
                    )
                })?,
        },
        "request_channel_info" => {
            let fields = required_channel_info_fields(payload)?;
            let (guild_id, guild_domain) = canonical_gateway_guild(payload)?;
            GatewayCommand::RequestChannelInfo {
                guild_id,
                guild_domain,
                fields,
            }
        }
        "request_soundboard_sounds" => GatewayCommand::RequestSoundboardSounds {
            guilds: required_gateway_guilds(payload)?,
        },
        "voice_state" => {
            required_gateway_object(
                payload,
                &["self_mute", "self_deaf"],
                "The voice state was invalid. Try those voice controls again.",
            )?;
            GatewayCommand::VoiceState {
                self_mute: required_gateway_bool(
                    payload,
                    "self_mute",
                    "The microphone state was invalid. Try that voice control again.",
                )?,
                self_deaf: required_gateway_bool(
                    payload,
                    "self_deaf",
                    "The deafen state was invalid. Try that voice control again.",
                )?,
            }
        }
        _ => {
            return Err(NativeError::local(
                "INVALID_GATEWAY_COMMAND",
                "This realtime action is not supported by this version of Kaede. Update the app and try again.",
            ));
        }
    };
    Ok(decoded)
}

fn required_gateway_guilds(payload: &Value) -> Result<Vec<(String, String)>, NativeError> {
    required_gateway_object(
        payload,
        &["guilds"],
        "The soundboard guild list was invalid.",
    )?;
    let guilds = payload
        .get("guilds")
        .and_then(Value::as_array)
        .filter(|guilds| (1..=100).contains(&guilds.len()))
        .ok_or_else(|| {
            NativeError::local(
                "INVALID_GATEWAY_COMMAND",
                "The soundboard guild list was invalid.",
            )
        })?;
    let mut seen = BTreeSet::new();
    guilds
        .iter()
        .map(|guild| {
            let parsed = required_gateway_guild(guild)?;
            if !seen.insert(format!("{}@{}", parsed.0, parsed.1)) {
                return Err(NativeError::local(
                    "INVALID_GATEWAY_COMMAND",
                    "The soundboard guild list contained a duplicate guild.",
                ));
            }
            Ok(parsed)
        })
        .collect()
}

fn required_channel_info_fields(payload: &Value) -> Result<Vec<String>, NativeError> {
    required_gateway_object(
        payload,
        &["guild_id", "guild_domain", "fields"],
        "The live voice channel information request was invalid.",
    )?;
    let fields = payload
        .get("fields")
        .and_then(Value::as_array)
        .filter(|fields| (1..=2).contains(&fields.len()))
        .ok_or_else(|| {
            NativeError::local(
                "INVALID_GATEWAY_COMMAND",
                "The live voice channel information fields were invalid.",
            )
        })?;
    let mut seen = BTreeSet::new();
    let mut rendered = Vec::with_capacity(fields.len());
    for field in fields {
        let Some(field) = field.as_str() else {
            return Err(NativeError::local(
                "INVALID_GATEWAY_COMMAND",
                "The live voice channel information fields were invalid.",
            ));
        };
        if !matches!(field, "status" | "voice_start_time") || !seen.insert(field) {
            return Err(NativeError::local(
                "INVALID_GATEWAY_COMMAND",
                "The live voice channel information fields were invalid.",
            ));
        }
        rendered.push(field.to_owned());
    }
    Ok(rendered)
}

fn required_gateway_guild(payload: &Value) -> Result<(String, String), NativeError> {
    required_gateway_object(
        payload,
        &["guild_id", "guild_domain"],
        "The gateway guild reference was invalid.",
    )?;
    canonical_gateway_guild(payload)
}

fn canonical_gateway_guild(payload: &Value) -> Result<(String, String), NativeError> {
    let id = required_string(payload, "guild_id")?;
    let domain = required_string(payload, "guild_domain")?;
    let wire = format!("{id}@{domain}");
    let guild = EntityRef::from_str(&wire).map_err(|_| {
        NativeError::local(
            "INVALID_GATEWAY_COMMAND",
            "The gateway guild reference was invalid.",
        )
    })?;
    if guild.to_string() != wire {
        return Err(NativeError::local(
            "INVALID_GATEWAY_COMMAND",
            "The gateway guild reference was not canonical.",
        ));
    }
    Ok((guild.id.to_string(), guild.domain.to_string()))
}

fn required_gateway_object<'a>(
    payload: &'a Value,
    keys: &[&str],
    message: &str,
) -> Result<&'a serde_json::Map<String, Value>, NativeError> {
    payload
        .as_object()
        .filter(|object| {
            object.len() == keys.len() && keys.iter().all(|key| object.contains_key(*key))
        })
        .ok_or_else(|| NativeError::local("INVALID_GATEWAY_COMMAND", message))
}

fn required_gateway_bool(payload: &Value, key: &str, message: &str) -> Result<bool, NativeError> {
    payload
        .get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| NativeError::local("INVALID_GATEWAY_COMMAND", message))
}

fn required_string(value: &Value, key: &str) -> Result<String, NativeError> {
    let label = match key {
        "guild_id" | "guild_domain" => "guild information",
        _ => "required information",
    };
    value
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| {
            NativeError::local(
                "INVALID_NATIVE_ARGUMENT",
                format!(
                    "Kaede could not complete this action because {label} is missing. Reopen this screen and try again."
                ),
            )
        })
}

fn configured_hotkey(
    configured: Option<&str>,
    code: &str,
    label: &str,
) -> Result<Option<RegisteredHotkey>, NativeError> {
    let Some(configured) = configured.map(str::trim).filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let shortcut = Shortcut::from_str(configured).map_err(|error| {
        NativeError::operation(
            code,
            format!("That {label} shortcut is invalid. Enter one key with optional modifier keys."),
            error,
        )
    })?;
    Ok(Some(RegisteredHotkey {
        configured: configured.to_owned(),
        shortcut,
    }))
}

fn hotkey_status(registration: &HotkeyRegistration) -> String {
    let status = |registered: Option<&RegisteredHotkey>| {
        registered.map_or_else(
            || "disabled".to_owned(),
            |registered| format!("active globally: {}", registered.configured),
        )
    };
    format!(
        "Push to talk is {}; priority push to talk is {}.",
        status(registration.push_to_talk.as_ref()),
        status(registration.priority_push_to_talk.as_ref())
    )
}

fn hotkeys_conflict(
    push_to_talk: Option<&RegisteredHotkey>,
    priority_push_to_talk: Option<&RegisteredHotkey>,
) -> bool {
    push_to_talk
        .zip(priority_push_to_talk)
        .is_some_and(|(normal, priority)| normal.shortcut == priority.shortcut)
}

fn replace_global_hotkeys(
    app: &AppHandle,
    registration: &mut HotkeyRegistration,
    push_to_talk: Option<&str>,
    priority_push_to_talk: Option<&str>,
) -> Result<bool, NativeError> {
    let next_push_to_talk =
        configured_hotkey(push_to_talk, "INVALID_PUSH_TO_TALK_HOTKEY", "push-to-talk")?;
    let next_priority = configured_hotkey(
        priority_push_to_talk,
        "INVALID_PRIORITY_PUSH_TO_TALK_HOTKEY",
        "priority push-to-talk",
    )?;
    if hotkeys_conflict(next_push_to_talk.as_ref(), next_priority.as_ref()) {
        return Err(NativeError::local(
            "PUSH_TO_TALK_HOTKEY_CONFLICT",
            "Normal and priority push to talk need different shortcuts.",
        ));
    }
    let same_shortcuts = |left: Option<&RegisteredHotkey>, right: Option<&RegisteredHotkey>| {
        left.map(|value| value.shortcut) == right.map(|value| value.shortcut)
    };
    if same_shortcuts(
        registration.push_to_talk.as_ref(),
        next_push_to_talk.as_ref(),
    ) && same_shortcuts(
        registration.priority_push_to_talk.as_ref(),
        next_priority.as_ref(),
    ) {
        registration.push_to_talk = next_push_to_talk;
        registration.priority_push_to_talk = next_priority;
        registration.status = hotkey_status(registration);
        return Ok(false);
    }

    let manager = app.global_shortcut();
    let previous = registration.clone();
    let mut removed = Vec::<Shortcut>::new();
    for registered in [
        previous.push_to_talk.as_ref(),
        previous.priority_push_to_talk.as_ref(),
    ]
    .into_iter()
    .flatten()
    {
        if let Err(error) = manager.unregister(registered.shortcut) {
            for shortcut in removed {
                let _ = manager.register(shortcut);
            }
            return Err(NativeError::operation(
                "GLOBAL_HOTKEY_UNAVAILABLE",
                "Kaede could not replace the existing voice shortcuts. The previous shortcuts remain selected; try again.",
                error,
            ));
        }
        removed.push(registered.shortcut);
    }

    let mut installed = Vec::<Shortcut>::new();
    for registered in [next_push_to_talk.as_ref(), next_priority.as_ref()]
        .into_iter()
        .flatten()
    {
        if let Err(error) = manager.register(registered.shortcut) {
            for shortcut in installed {
                let _ = manager.unregister(shortcut);
            }
            for registered in [
                previous.push_to_talk.as_ref(),
                previous.priority_push_to_talk.as_ref(),
            ]
            .into_iter()
            .flatten()
            {
                let _ = manager.register(registered.shortcut);
            }
            return Err(NativeError::operation(
                "GLOBAL_HOTKEY_UNAVAILABLE",
                "One of those voice shortcuts is unavailable. The previous shortcuts were restored; choose a different combination and try again.",
                error,
            ));
        }
        installed.push(registered.shortcut);
    }

    registration.push_to_talk = next_push_to_talk;
    registration.priority_push_to_talk = next_priority;
    registration.status = hotkey_status(registration);
    Ok(true)
}

fn release_voice_hotkeys(sender: Option<&mpsc::UnboundedSender<VoiceCommand>>) {
    let Some(sender) = sender else { return };
    let _ = sender.send(VoiceCommand::SetPushToTalk(false));
    let _ = sender.send(VoiceCommand::SetPriorityPushToTalk(false));
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
        .map_err(|error| {
            NativeError::operation(
                "AUDIO_ENUMERATION_FAILED",
                "Kaede's audio-device scan stopped unexpectedly. Restart the app and open voice settings again.",
                error,
            )
        })?
        .map_err(|error| audio_device_error("AUDIO_ENUMERATION_FAILED", "microphone", error))?;
    let outputs = tokio::task::spawn_blocking(output_devices)
        .await
        .map_err(|error| {
            NativeError::operation(
                "AUDIO_ENUMERATION_FAILED",
                "Kaede's audio-device scan stopped unexpectedly. Restart the app and open voice settings again.",
                error,
            )
        })?
        .map_err(|error| audio_device_error("AUDIO_ENUMERATION_FAILED", "speaker", error))?;
    let cameras = tokio::task::spawn_blocking(camera_devices)
        .await
        .map_err(|error| {
            NativeError::operation(
                "CAMERA_ENUMERATION_FAILED",
                "Kaede's camera scan stopped unexpectedly. Restart the app and open voice settings again.",
                error,
            )
        })?
        .map_err(NativeError::from)?;
    let screens = tokio::task::spawn_blocking(screen_sources)
        .await
        .map_err(|error| {
            NativeError::operation(
                "SCREEN_ENUMERATION_FAILED",
                "Kaede could not list screens and windows. Check screen-recording permission, restart the app, and try again.",
                error,
            )
        })?;
    Ok(json!({
        "inputs": inputs,
        "outputs": outputs,
        "cameras": cameras,
        "screens": screens,
    }))
}

#[tauri::command]
async fn native_screen_thumbnail(source_id: String) -> Result<Response, NativeError> {
    if source_id.len() > 128
        || !source_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b':' | b'-' | b'_'))
    {
        return Err(NativeError::local(
            "INVALID_SCREEN_SOURCE",
            "That screen-share source is invalid. Refresh the source list and try again.",
        ));
    }
    let thumbnail = tokio::task::spawn_blocking(move || screen_source_thumbnail(&source_id))
        .await
        .map_err(|error| {
            NativeError::operation(
                "SCREEN_PREVIEW_FAILED",
                "Kaede could not prepare that screen preview.",
                error,
            )
        })?
        .ok_or_else(|| {
            NativeError::local(
                "SCREEN_PREVIEW_UNAVAILABLE",
                "That window or display could not be previewed. It may have closed or require the system picker.",
            )
        })?;
    let mut packet = Vec::with_capacity(12 + thumbnail.rgba.len());
    packet.extend_from_slice(b"KST1");
    packet.extend_from_slice(&thumbnail.width.to_le_bytes());
    packet.extend_from_slice(&thumbnail.height.to_le_bytes());
    packet.extend_from_slice(&thumbnail.rgba);
    Ok(Response::new(packet))
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

fn media_publish_settings(preferences: &DesktopPreferences) -> MediaPublishSettings {
    MediaPublishSettings {
        audio_max_bitrate: match preferences.audio_quality {
            AudioQualityPreference::DataSaver => 24_000,
            AudioQualityPreference::Standard => 48_000,
            AudioQualityPreference::High => 96_000,
            AudioQualityPreference::Studio => 128_000,
        },
    }
}

fn screen_share_settings(preferences: &DesktopPreferences) -> ScreenShareSettings {
    match preferences.screen_share_profile {
        ScreenShareProfilePreference::DataSaver => ScreenShareSettings {
            width: 1280,
            height: 720,
            frame_rate: 15,
            max_bitrate: 1_200_000,
        },
        ScreenShareProfilePreference::Smooth => ScreenShareSettings::default(),
        ScreenShareProfilePreference::Sharp => ScreenShareSettings {
            width: 1920,
            height: 1080,
            frame_rate: 30,
            max_bitrate: 4_500_000,
        },
        ScreenShareProfilePreference::Source => ScreenShareSettings {
            width: 3840,
            height: 2160,
            frame_rate: 30,
            max_bitrate: 8_000_000,
        },
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
            .map_err(|error| audio_device_error("AUDIO_INPUT_FAILED", "microphone", error))?;
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
    .map_err(|error| {
        NativeError::operation(
            "AUDIO_INPUT_FAILED",
            "The microphone test stopped unexpectedly. Restart Kaede and try the test again.",
            error,
        )
    })?
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
            .map_err(|error| audio_device_error("AUDIO_OUTPUT_FAILED", "speaker", error))?;
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
    .map_err(|error| {
        NativeError::operation(
            "AUDIO_OUTPUT_FAILED",
            "The speaker test stopped unexpectedly. Restart Kaede and try the test again.",
            error,
        )
    })?
}

#[tauri::command]
#[allow(clippy::too_many_arguments)] // Tauri maps these stable IPC fields by parameter name.
async fn native_voice_join(
    reference: String,
    is_call: bool,
    expected_policy: ExpectedVoicePolicy,
    e2ee_key: Option<String>,
    sender_device_id: Option<String>,
    connection_id: String,
    takeover: bool,
    state: State<'_, NativeState>,
) -> Result<(), NativeError> {
    join_native_voice(
        VoiceTarget {
            reference,
            is_call,
            expected_policy,
            e2ee_key: e2ee_key.map(SecretString::from),
            sender_device_id,
            connection_id,
        },
        takeover,
        &state,
    )
    .await
}

async fn join_native_voice(
    target: VoiceTarget,
    takeover: bool,
    state: &NativeState,
) -> Result<(), NativeError> {
    let generation = state.voice_install.begin().await;
    join_native_voice_reserved(target, takeover, state, generation).await
}

#[allow(clippy::too_many_lines)] // One fenced join keeps validation and installation ordered.
async fn join_native_voice_reserved(
    target: VoiceTarget,
    takeover: bool,
    state: &NativeState,
    generation: u64,
) -> Result<(), NativeError> {
    let entity = EntityRef::from_str(&target.reference).map_err(|error| {
        NativeError::operation(
            "INVALID_VOICE_REFERENCE",
            "This voice channel reference is invalid. Close and reopen the channel, then try joining again.",
            error,
        )
    })?;
    if !target.is_call
        && (target.expected_policy.channel_id != entity.id.to_string()
            || target.expected_policy.channel_domain != entity.domain.as_str())
    {
        return Err(NativeError::local(
            "VOICE_E2EE_POLICY_MISMATCH",
            "The voice channel changed before Kaede could join. Refresh the conversation and try again.",
        ));
    }
    if target.expected_policy.e2ee != target.e2ee_key.is_some() {
        return Err(NativeError::local(
            "VOICE_E2EE_POLICY_MISMATCH",
            "The voice encryption policy did not match the supplied media key. Nothing was connected.",
        ));
    }
    let account =
        state.account.read().await.clone().ok_or_else(|| {
            NativeError::local("NOT_AUTHENTICATED", "Sign in before joining voice.")
        })?;
    let preferences = state.preferences.read().await.clone();
    let capture = capture_settings(&preferences);
    let media = media_publish_settings(&preferences);
    let output = preferences.output_device.map(|device| device.id);
    let media = VoiceMediaSettings {
        capture,
        output_device: output,
        publish: media,
        // A join establishes LiveKit publication before returning its handle.
        // Keep both media directions closed until the generation fence accepts
        // the handle and we can reconcile the latest UI state atomically.
        initially_muted: true,
        initially_deafened: true,
    };
    let media_key = target
        .e2ee_key
        .as_ref()
        .map(|encoded| {
            let encoded = encoded.expose_secret();
            let decoded = URL_SAFE_NO_PAD.decode(encoded).map_err(|error| {
                NativeError::operation(
                    "INVALID_E2EE_MEDIA_KEY",
                    "The encrypted call key was invalid. Refresh the conversation and try again.",
                    error,
                )
            })?;
            if decoded.len() != 32 || URL_SAFE_NO_PAD.encode(&decoded) != encoded {
                return Err(NativeError::local(
                    "INVALID_E2EE_MEDIA_KEY",
                    "The encrypted call key was invalid. Refresh the conversation and try again.",
                ));
            }
            Ok(decoded)
        })
        .transpose()?;
    let grant_request = VoiceGrantRequest {
        sender_device_id: target.sender_device_id.as_deref(),
        connection_id: &target.connection_id,
        takeover,
    };
    let mut handle = if target.is_call {
        kaede_voice::join_call(
            account.api.clone(),
            &entity,
            media,
            target.expected_policy.clone(),
            media_key,
            grant_request,
        )
        .await
    } else {
        kaede_voice::join_channel(
            account.api.clone(),
            &entity,
            media,
            target.expected_policy.clone(),
            media_key,
            grant_request,
        )
        .await
    }
    .map_err(NativeError::from)?;
    let grant_stale = handle.grant_stale.clone();
    let Some(install_guard) = state.voice_install.lock_if_current(generation).await else {
        handle.leave().await;
        return Ok(());
    };
    // Voice controls also take this lock before updating voice_ui. Whichever
    // operation wins therefore applies the user's latest state to the handle
    // that remains installed; a control cannot be stranded on the old room.
    let mut installed_voice = state.voice.lock().await;
    let voice_ui = state.voice_ui.read().await.clone();
    if let Some(gate) = handle.input_level.as_ref() {
        gate.set_muted(voice_ui.muted || voice_ui.deafened);
    }
    let _ = handle.commands.send(VoiceCommand::SetMuted(voice_ui.muted));
    let _ = handle
        .commands
        .send(VoiceCommand::SetDeafened(voice_ui.deafened));
    *state.push_to_talk_sender.lock() = Some(handle.commands.clone());
    *state.voice_video.lock().await = handle.video_frames.take();
    let previous = installed_voice.replace(handle);
    *state.voice_target.write().await = Some(InstalledVoiceTarget { generation, target });
    drop(installed_voice);
    drop(install_guard);
    tauri::async_runtime::spawn(forward_voice_restart(
        grant_stale,
        generation,
        state.voice_restart.clone(),
    ));
    if let Some(previous) = previous {
        previous.leave().await;
    }
    Ok(())
}

async fn forward_voice_restart(
    mut grant_stale: tokio::sync::watch::Receiver<bool>,
    generation: u64,
    restart: mpsc::UnboundedSender<VoiceRestartRequest>,
) {
    loop {
        if *grant_stale.borrow_and_update() {
            let _ = restart.send(VoiceRestartRequest { generation });
            return;
        }
        if grant_stale.changed().await.is_err() {
            return;
        }
    }
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
        VoiceControl::PriorityPushToTalkDown => VoiceCommand::SetPriorityPushToTalk(true),
        VoiceControl::PriorityPushToTalkUp => VoiceCommand::SetPriorityPushToTalk(false),
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
            source_id: preferences
                .screen_source
                .as_ref()
                .map(|device| device.id.clone()),
            settings: screen_share_settings(&preferences),
        },
        VoiceControl::ScreenOff => VoiceCommand::SetScreenShare {
            enabled: false,
            source_id: None,
            settings: screen_share_settings(&preferences),
        },
    };
    voice
        .as_mut()
        .ok_or_else(|| {
            NativeError::local(
                "VOICE_NOT_CONNECTED",
                "Join a voice channel before using voice controls.",
            )
        })?
        .commands
        .send(command)
        .map_err(|_| {
            NativeError::local(
                "VOICE_DISCONNECTED",
                "The voice session ended before that change was applied. Join voice again and retry.",
            )
        })?;
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

#[tauri::command]
async fn native_media_quality_set(
    screen_profile: ScreenShareProfilePreference,
    audio_quality: AudioQualityPreference,
    share_system_audio: bool,
    source_id: Option<String>,
    state: State<'_, NativeState>,
) -> Result<(), NativeError> {
    let source = source_id.as_deref().and_then(|requested| {
        screen_sources()
            .into_iter()
            .find(|candidate| candidate.id == requested)
            .map(|candidate| DevicePreference {
                id: candidate.id,
                label: candidate.label,
            })
    });
    if source_id.is_some() && source.is_none() {
        return Err(NativeError::local(
            "SCREEN_SOURCE_UNAVAILABLE",
            "That window or display is no longer available. Refresh the source list and choose it again.",
        ));
    }
    let mut preferences = state.preferences.write().await;
    let previous_preferences = preferences.clone();
    let restart_voice = preferences.audio_quality != audio_quality;
    preferences.screen_share_profile = screen_profile;
    preferences.audio_quality = audio_quality;
    preferences.share_system_audio = share_system_audio;
    preferences.screen_source = source;
    preferences.save(&state.paths).await.map_err(|error| {
        NativeError::operation(
            "PREFERENCES_SAVE_FAILED",
            "Kaede could not save media quality settings. Check the app configuration directory and try again.",
            error,
        )
    })?;
    drop(preferences);
    let restart = if restart_voice {
        state
            .voice_install
            .reserve_restart(&state.voice_target)
            .await
    } else {
        None
    };
    if let Some((generation, target)) = restart
        && let Err(error) = join_native_voice_reserved(target, false, &state, generation).await
    {
        let rollback_result = previous_preferences.save(&state.paths).await;
        *state.preferences.write().await = previous_preferences;
        if let Err(rollback_error) = rollback_result {
            tracing::error!(%rollback_error, "failed to roll back media preferences after voice restart failure");
        }
        return Err(error);
    }
    Ok(())
}

async fn leave_active_voice(state: &NativeState) {
    let install_guard = state.voice_install.invalidate().await;
    *state.push_to_talk_sender.lock() = None;
    *state.voice_video.lock().await = None;
    let voice = state.voice.lock().await.take();
    *state.voice_target.write().await = None;
    *state.voice_ui.write().await = VoiceUiState::default();
    drop(install_guard);
    if let Some(voice) = voice {
        voice.leave().await;
    }
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
        map.insert(
            "priority_speakers".to_owned(),
            json!(
                voice
                    .priority_speakers
                    .borrow()
                    .iter()
                    .cloned()
                    .collect::<Vec<_>>()
            ),
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
        NativeError::local(
            "INVALID_VIDEO_FRAME",
            "An incoming video stream could not be displayed. Leave voice and join again; update Kaede if it keeps happening.",
        )
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
    serde_json::to_value(&*state.preferences.read().await).map_err(|error| {
        NativeError::operation(
            "PREFERENCES_INVALID",
            "Kaede could not read your desktop preferences. Restart the app; if the problem continues, reset desktop settings.",
            error,
        )
    })
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
        || previous.automatic_gain_control != preferences.automatic_gain_control
        || previous.audio_quality != preferences.audio_quality;
    let hotkey_result = {
        let mut hotkey = state.hotkey.lock();
        replace_global_hotkeys(
            &app,
            &mut hotkey,
            preferences.push_to_talk_hotkey.as_deref(),
            preferences.priority_push_to_talk_hotkey.as_deref(),
        )
    };
    let hotkeys_changed = match hotkey_result {
        Ok(changed) => changed,
        Err(error) => {
            release_voice_hotkeys(state.push_to_talk_sender.lock().as_ref());
            return Err(error);
        }
    };
    if hotkeys_changed {
        release_voice_hotkeys(state.push_to_talk_sender.lock().as_ref());
    }
    if let Err(error) = preferences.save(&state.paths).await {
        let mut hotkey = state.hotkey.lock();
        if let Err(rollback_error) = replace_global_hotkeys(
            &app,
            &mut hotkey,
            previous.push_to_talk_hotkey.as_deref(),
            previous.priority_push_to_talk_hotkey.as_deref(),
        ) {
            tracing::error!(
                ?rollback_error,
                "failed to restore voice shortcuts after preference-save failure"
            );
        }
        return Err(NativeError::operation(
            "PREFERENCES_SAVE_FAILED",
            "Kaede could not save your desktop preferences. Check that the app can write to its settings directory and try again.",
            error,
        ));
    }
    *state.preferences.write().await = preferences;
    let restart = if restart_voice {
        state
            .voice_install
            .reserve_restart(&state.voice_target)
            .await
    } else {
        None
    };
    if let Some((generation, target)) = restart {
        join_native_voice_reserved(target, false, &state, generation).await?;
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
        .map_err(|error| {
            NativeError::operation(
                "NOTIFICATION_IDENTITY_FAILED",
                "Windows notifications could not be configured. Check that Kaede is allowed to send notifications, then restart the app.",
                error,
            )
        })?;
    key.set_value("DisplayName", &"Kaede Chat")
        .and_then(|_| key.set_value("IconBackgroundColor", &"0"))
        .map_err(|error| {
            NativeError::operation(
                "NOTIFICATION_IDENTITY_FAILED",
                "Windows notifications could not be configured. Check that Kaede is allowed to send notifications, then restart the app.",
                error,
            )
        })?;
    if let Ok(executable) = std::env::current_exe() {
        let icon_path = executable.to_string_lossy().into_owned();
        key.set_value("IconUri", &icon_path).map_err(|error| {
            NativeError::operation(
                "NOTIFICATION_IDENTITY_FAILED",
                "Windows notifications could not use the Kaede app identity. Restart the app; if this continues, reinstall Kaede.",
                error,
            )
        })?;
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
#[allow(clippy::unnecessary_wraps)]
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
        .map_err(|error| {
            NativeError::operation(
                "NOTIFICATION_FAILED",
                "Windows did not display the notification. Allow Kaede notifications in Windows Settings and turn off Do Not Disturb, then try again.",
                error,
            )
        })
}

#[cfg(not(target_os = "windows"))]
fn show_native_notification(app: &AppHandle, title: &str, body: &str) -> Result<(), NativeError> {
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|error| {
            NativeError::operation(
                "NOTIFICATION_FAILED",
                "Your system did not display the notification. Allow Kaede notifications and turn off Do Not Disturb, then try again.",
                error,
            )
        })
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
            tracing::error!(%error, "browser verification helper failed");
            eprintln!(
                "Kaede could not complete browser verification. Close the verification window and try again."
            );
            std::process::exit(1);
        }
        return;
    }
    let launched_at_sign_in = is_autostart_launch(&arguments);

    let paths = PlatformPaths::discover().unwrap_or_else(|error| {
        tracing::error!(%error, "private application directory could not be located");
        eprintln!(
            "Kaede could not open its private application-data directory. Check that your account can access the system application-data folder, then restart Kaede."
        );
        std::process::exit(1);
    });
    let (preferences, startup_notice) = match tauri::async_runtime::block_on(
        DesktopPreferences::load(&paths),
    ) {
        Ok(preferences) => (preferences, None),
        Err(error) => {
            tracing::warn!(%error, "desktop preferences could not be loaded; using defaults");
            (
                    DesktopPreferences::default(),
                    Some(
                        "Kaede could not read your saved desktop settings, so safe defaults were restored. Review and save Voice & Video settings to replace the damaged settings file."
                            .to_owned(),
                    ),
                )
        }
    };
    let (gateway_events_tx, gateway_events_rx) = mpsc::unbounded_channel();
    let (voice_restart_tx, mut voice_restart_rx) = mpsc::unbounded_channel();
    let push_to_talk_sender = Arc::new(SyncMutex::new(None::<mpsc::UnboundedSender<VoiceCommand>>));
    let event_sender = push_to_talk_sender.clone();
    let hotkey = Arc::new(SyncMutex::new(HotkeyRegistration {
        push_to_talk: None,
        priority_push_to_talk: None,
        status: "Push to talk is disabled; priority push to talk is disabled.".to_owned(),
    }));
    let event_hotkey = hotkey.clone();
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
        voice_install: VoiceInstallFence::new(),
        voice_restart: voice_restart_tx,
        voice_ui: RwLock::new(VoiceUiState::default()),
        push_to_talk_sender,
        hotkey,
        preferences: RwLock::new(preferences),
        paths,
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec![AUTOSTART_ARGUMENT]),
        ))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |_app, shortcut, event| {
                    let pressed = event.state == ShortcutState::Pressed;
                    let command = {
                        let hotkey = event_hotkey.lock();
                        if hotkey
                            .push_to_talk
                            .as_ref()
                            .is_some_and(|registered| registered.shortcut == *shortcut)
                        {
                            Some(VoiceCommand::SetPushToTalk(pressed))
                        } else if hotkey
                            .priority_push_to_talk
                            .as_ref()
                            .is_some_and(|registered| registered.shortcut == *shortcut)
                        {
                            Some(VoiceCommand::SetPriorityPushToTalk(pressed))
                        } else {
                            None
                        }
                    };
                    if let Some(command) = command
                        && let Some(sender) = event_sender.lock().as_ref()
                    {
                        let _ = sender.send(command);
                    }
                })
                .build(),
        )
        .plugin(tauri_plugin_single_instance::init(
            |app, arguments, _working_directory| {
                if !is_autostart_launch(&arguments) {
                    show_main_window(app);
                }
            },
        ))
        .manage(state)
        .setup(move |app| {
            let voice_restart_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                while let Some(request) = voice_restart_rx.recv().await {
                    let state = voice_restart_handle.state::<NativeState>();
                    let Some((generation, target)) = state
                        .voice_install
                        .reserve_restart_if_current(request.generation, &state.voice_target)
                        .await
                    else {
                        continue;
                    };
                    if let Err(error) =
                        join_native_voice_reserved(target, false, &state, generation).await
                    {
                        tracing::warn!(?error, "native voice grant refresh failed");
                        if let Some(_install_guard) =
                            state.voice_install.lock_if_current(generation).await
                            && let Some(voice) = state.voice.lock().await.as_ref()
                        {
                            voice.mark_failed(error.message.clone());
                        }
                    }
                }
            });
            // Warm the persisted account and OS credential vault immediately.
            // The frontend and configured_api also await/retry this same
            // serialized operation, which closes the hard-restart race.
            let restore_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let state = restore_handle.state::<NativeState>();
                if let Err(error) = restore_known_account(&state, None).await {
                    tracing::warn!(?error, "desktop session could not be restored at startup");
                }
            });
            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title(format!("Kaede Chat · {}", env!("CARGO_PKG_VERSION")))
                    .inner_size(1360.0, 860.0)
                    .min_inner_size(920.0, 620.0)
                    .resizable(true)
                    .visible(!launched_at_sign_in)
                    .center()
                    .on_navigation(|url| {
                        cfg!(dev)
                            || !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"))
                    })
                    .build()?;
            if let Some(notice) = startup_notice.as_deref()
                && let Err(error) =
                    show_native_notification(app.handle(), "Desktop settings were reset", notice)
            {
                tracing::warn!(?error, "desktop settings warning could not be displayed");
            }
            {
                let state = app.state::<NativeState>();
                let preferences = state.preferences.blocking_read().clone();
                let mut hotkey = state.hotkey.lock();
                if let Err(error) = replace_global_hotkeys(
                    app.handle(),
                    &mut hotkey,
                    preferences.push_to_talk_hotkey.as_deref(),
                    preferences.priority_push_to_talk_hotkey.as_deref(),
                ) {
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
            native_update_check,
            native_update_install,
            native_autostart_status,
            native_autostart_set,
            native_taskbar_pin_status,
            native_taskbar_pin_request,
            native_set_instance,
            native_restore_session,
            native_api_request,
            native_media_request,
            native_soundboard_media,
            native_upload_object,
            native_gateway_next,
            native_gateway_command,
            native_audio_devices,
            native_screen_thumbnail,
            native_test_input,
            native_test_output,
            native_voice_join,
            native_voice_control,
            native_media_quality_set,
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
            tracing::error!(%error, "desktop application failed to start");
            eprintln!(
                "Kaede could not start its desktop window. Restart the app; if it keeps failing, check the system logs or reinstall Kaede."
            );
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
    use std::{collections::BTreeMap, error::Error};

    use kaede_api::ApiClientError;
    use kaede_auth::AuthError;
    use kaede_protocol::{ApiError, Domain};
    use reqwest::StatusCode;
    use serde_json::Value;

    use super::{
        GatewayCommand, HotkeyRegistration, InstalledVoiceTarget, NativeError, NativeRequest,
        PublicDownloadPolicy, VoiceCommand, VoiceInstallFence, VoiceRestartRequest, VoiceTarget,
        configured_hotkey, decode_gateway_command, forward_voice_restart, hotkey_status,
        hotkeys_conflict, is_autostart_launch, native_forward_headers, release_voice_hotkeys,
        submitted_password_kdf_version, valid_sha256, validate_attachment_media_path,
        validate_native_password_request, validate_soundboard_media_url,
    };

    #[test]
    fn voice_shortcuts_are_distinct_and_report_both_states() {
        let Ok(normal) = configured_hotkey(
            Some("Ctrl+Shift+Space"),
            "INVALID_PUSH_TO_TALK_HOTKEY",
            "push-to-talk",
        ) else {
            panic!("normal shortcut must parse");
        };
        let Ok(same) = configured_hotkey(
            Some("Ctrl+Shift+Space"),
            "INVALID_PRIORITY_PUSH_TO_TALK_HOTKEY",
            "priority push-to-talk",
        ) else {
            panic!("equivalent priority shortcut must parse");
        };
        assert!(hotkeys_conflict(normal.as_ref(), same.as_ref()));

        let Ok(priority) = configured_hotkey(
            Some("Alt+Shift+Space"),
            "INVALID_PRIORITY_PUSH_TO_TALK_HOTKEY",
            "priority push-to-talk",
        ) else {
            panic!("priority shortcut must parse");
        };
        assert!(!hotkeys_conflict(normal.as_ref(), priority.as_ref()));
        let registration = HotkeyRegistration {
            push_to_talk: normal,
            priority_push_to_talk: priority,
            status: String::new(),
        };
        let status = hotkey_status(&registration);
        assert!(status.contains("Ctrl+Shift+Space"));
        assert!(status.contains("Alt+Shift+Space"));
    }

    #[test]
    fn voice_shortcut_release_is_not_dropped_by_a_full_queue() {
        let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
        for _ in 0..1_024 {
            assert!(sender.send(VoiceCommand::SetPushToTalk(true)).is_ok());
        }
        release_voice_hotkeys(Some(&sender));

        for _ in 0..1_024 {
            assert!(matches!(
                receiver.try_recv(),
                Ok(VoiceCommand::SetPushToTalk(true))
            ));
        }
        assert!(matches!(
            receiver.try_recv(),
            Ok(VoiceCommand::SetPushToTalk(false))
        ));
        assert!(matches!(
            receiver.try_recv(),
            Ok(VoiceCommand::SetPriorityPushToTalk(false))
        ));
    }

    fn plaintext_voice_target() -> VoiceTarget {
        VoiceTarget {
            reference: "1@example.com".to_owned(),
            is_call: false,
            expected_policy: kaede_voice::ExpectedVoicePolicy {
                e2ee: false,
                room: "voice-room".to_owned(),
                channel_id: "1".to_owned(),
                channel_domain: "example.com".to_owned(),
                bitrate: 64_000,
                user_limit: 0,
                rtc_region: None,
                video_quality_mode: 1,
                encryption_policy_generation: None,
                encryption_epoch: None,
                media_protocol: None,
                media_suite: None,
                media_session_id: None,
                media_epoch: None,
            },
            e2ee_key: None,
            sender_device_id: None,
            connection_id: "voice-connection".to_owned(),
        }
    }

    fn derived_password() -> String {
        "A".repeat(43)
    }

    #[tokio::test]
    async fn voice_install_fence_rejects_superseded_completion() {
        let fence = VoiceInstallFence::new();
        let first = fence.begin().await;
        let second = fence.begin().await;

        assert!(fence.lock_if_current(first).await.is_none());
        assert!(fence.lock_if_current(second).await.is_some());
    }

    #[tokio::test]
    async fn voice_install_fence_invalidates_an_unpublished_join() {
        let fence = VoiceInstallFence::new();
        let joining = fence.begin().await;
        drop(fence.invalidate().await);

        assert!(fence.lock_if_current(joining).await.is_none());
    }

    #[tokio::test]
    async fn voice_restart_snapshot_and_generation_share_the_leave_fence() {
        let fence = VoiceInstallFence::new();
        let installed = fence.begin().await;
        let target = tokio::sync::RwLock::new(Some(InstalledVoiceTarget {
            generation: installed,
            target: plaintext_voice_target(),
        }));
        let Some((restart, _)) = fence.reserve_restart(&target).await else {
            panic!("an active voice target should reserve a restart generation");
        };

        let leave_guard = fence.invalidate().await;
        *target.write().await = None;
        drop(leave_guard);

        assert!(fence.lock_if_current(restart).await.is_none());
        assert!(fence.reserve_restart(&target).await.is_none());
    }

    #[tokio::test]
    async fn stale_voice_grant_forwards_one_restart_request() {
        let (grant_stale, receiver) = tokio::sync::watch::channel(false);
        let (restart, mut requests) = tokio::sync::mpsc::unbounded_channel();
        let forwarding = tokio::spawn(forward_voice_restart(receiver, 7, restart));

        assert!(grant_stale.send(true).is_ok());
        assert_eq!(
            requests.recv().await,
            Some(VoiceRestartRequest { generation: 7 })
        );
        assert!(forwarding.await.is_ok());
        assert!(requests.try_recv().is_err());
    }

    #[tokio::test]
    async fn voice_permission_restart_is_deduplicated_and_room_fenced() {
        let fence = VoiceInstallFence::new();
        let installed = fence.begin().await;
        let target = tokio::sync::RwLock::new(Some(InstalledVoiceTarget {
            generation: installed,
            target: plaintext_voice_target(),
        }));
        let Some((restart, retained)) = fence.reserve_restart_if_current(installed, &target).await
        else {
            panic!("the installed room should reserve one restart");
        };

        assert_eq!(retained.reference, "1@example.com");
        assert!(
            fence
                .reserve_restart_if_current(installed, &target)
                .await
                .is_none()
        );
        assert!(fence.lock_if_current(restart).await.is_some());

        let stale_room = restart;
        let current_room = fence.begin().await;
        assert!(
            fence
                .reserve_restart_if_current(stale_room, &target)
                .await
                .is_none()
        );
        assert!(
            fence
                .reserve_restart_if_current(current_room, &target)
                .await
                .is_none()
        );
    }

    #[tokio::test]
    async fn preference_restart_cannot_supersede_an_explicit_join() {
        let fence = VoiceInstallFence::new();
        let installed = fence.begin().await;
        let target = tokio::sync::RwLock::new(Some(InstalledVoiceTarget {
            generation: installed,
            target: plaintext_voice_target(),
        }));

        let joining = fence.begin().await;

        assert!(fence.reserve_restart(&target).await.is_none());
        assert!(fence.lock_if_current(joining).await.is_some());
    }

    fn password_kdf(include_vault_salt: bool) -> serde_json::Value {
        if include_vault_salt {
            serde_json::json!({
                "version": 2,
                "algorithm": "PBKDF2-SHA256",
                "iterations": 600_000,
                "auth_salt": "A".repeat(22),
                "vault_salt": "A".repeat(22),
            })
        } else {
            serde_json::json!({
                "version": 2,
                "algorithm": "PBKDF2-SHA256",
                "iterations": 600_000,
                "auth_salt": "A".repeat(22),
            })
        }
    }

    fn assert_invalid_password_request(method: &str, path: &str, body: &serde_json::Value) {
        let Err(error) = validate_native_password_request(method, path, body) else {
            panic!("malformed password request should be rejected: {method} {path}");
        };
        assert_eq!(error.code, "INVALID_PASSWORD_PROTOCOL");
    }

    #[test]
    fn autostart_argument_is_detected_without_hiding_regular_launches() {
        assert!(is_autostart_launch(&[
            "kaede-chat".to_owned(),
            "--kaede-autostart".to_owned()
        ]));
        assert!(!is_autostart_launch(&["kaede-chat".to_owned()]));
    }

    #[test]
    fn gateway_command_decoder_preserves_qualified_soundboard_and_voice_state() {
        let Ok(GatewayCommand::RequestSoundboardSounds { guilds }) = decode_gateway_command(
            "request_soundboard_sounds",
            &serde_json::json!({
                "guilds": [
                    {"guild_id": "7", "guild_domain": "alpha.example"},
                    {"guild_id": "7", "guild_domain": "beta.example"},
                ],
            }),
        ) else {
            panic!("qualified soundboard guilds should decode");
        };
        assert_eq!(
            guilds,
            vec![
                ("7".to_owned(), "alpha.example".to_owned()),
                ("7".to_owned(), "beta.example".to_owned()),
            ]
        );

        let Ok(GatewayCommand::RequestChannelInfo {
            guild_id,
            guild_domain,
            fields,
        }) = decode_gateway_command(
            "request_channel_info",
            &serde_json::json!({
                "guild_id": "7",
                "guild_domain": "alpha.example",
                "fields": ["status", "voice_start_time"],
            }),
        )
        else {
            panic!("qualified channel info request should decode");
        };
        assert_eq!(guild_id, "7");
        assert_eq!(guild_domain, "alpha.example");
        assert_eq!(fields, ["status", "voice_start_time"]);

        let Ok(GatewayCommand::VoiceState {
            self_mute,
            self_deaf,
        }) = decode_gateway_command(
            "voice_state",
            &serde_json::json!({"self_mute": true, "self_deaf": false}),
        )
        else {
            panic!("voice state should decode");
        };
        assert!(self_mute);
        assert!(!self_deaf);
    }

    fn assert_invalid_gateway_commands<const N: usize>(cases: [(&str, Value); N]) {
        for (command, payload) in cases {
            let Err(error) = decode_gateway_command(command, &payload) else {
                panic!("ambiguous gateway command should be rejected: {command}");
            };
            assert!(matches!(
                error.code.as_str(),
                "INVALID_GATEWAY_COMMAND" | "INVALID_NATIVE_ARGUMENT"
            ));
        }
    }

    #[test]
    fn gateway_decoder_rejects_ambiguous_soundboard_and_voice_payloads() {
        let oversized_guilds = (1..=101)
            .map(|id| {
                serde_json::json!({
                    "guild_id": id.to_string(),
                    "guild_domain": "alpha.example",
                })
            })
            .collect::<Vec<_>>();
        assert_invalid_gateway_commands([
            (
                "voice_state",
                serde_json::json!({"self_mute": 1, "self_deaf": false}),
            ),
            (
                "voice_state",
                serde_json::json!({"self_mute": false, "self_deaf": false, "extra": true}),
            ),
            (
                "request_soundboard_sounds",
                serde_json::json!({"guilds": [{"guild_id": "7"}]}),
            ),
            (
                "request_soundboard_sounds",
                serde_json::json!({"guilds": []}),
            ),
            (
                "request_soundboard_sounds",
                serde_json::json!({
                    "guilds": [
                        {"guild_id": "7", "guild_domain": "alpha.example"},
                        {"guild_id": "7", "guild_domain": "alpha.example"},
                    ],
                }),
            ),
            (
                "request_soundboard_sounds",
                serde_json::json!({
                    "guilds": [{"guild_id": "07", "guild_domain": "alpha.example"}],
                }),
            ),
            (
                "request_soundboard_sounds",
                serde_json::json!({
                    "guilds": [{"guild_id": "7", "guild_domain": "Alpha.Example"}],
                }),
            ),
            (
                "request_soundboard_sounds",
                serde_json::json!({
                    "guilds": [{
                        "guild_id": "7",
                        "guild_domain": "alpha.example",
                        "extra": true,
                    }],
                }),
            ),
            (
                "request_soundboard_sounds",
                serde_json::json!({
                    "guilds": [{"guild_id": "7", "guild_domain": "alpha.example"}],
                    "extra": true,
                }),
            ),
            (
                "request_soundboard_sounds",
                serde_json::json!({"guilds": oversized_guilds}),
            ),
        ]);
    }

    #[test]
    fn gateway_decoder_rejects_ambiguous_channel_info_payloads() {
        assert_invalid_gateway_commands([
            (
                "request_channel_info",
                serde_json::json!({
                    "guild_id": "7",
                    "guild_domain": "alpha.example",
                    "fields": [],
                }),
            ),
            (
                "request_channel_info",
                serde_json::json!({
                    "guild_id": "7",
                    "guild_domain": "alpha.example",
                    "fields": ["status", "status"],
                }),
            ),
            (
                "request_channel_info",
                serde_json::json!({
                    "guild_id": "7",
                    "guild_domain": "alpha.example",
                    "fields": ["unknown"],
                }),
            ),
            (
                "request_channel_info",
                serde_json::json!({
                    "guild_id": "07",
                    "guild_domain": "alpha.example",
                    "fields": ["status"],
                }),
            ),
            (
                "request_channel_info",
                serde_json::json!({
                    "guild_id": "7",
                    "guild_domain": "alpha.example",
                    "fields": ["status"],
                    "extra": true,
                }),
            ),
            ("unknown", serde_json::json!({})),
        ]);
    }

    #[test]
    fn native_request_headers_are_case_insensitive_and_fail_closed() {
        let request = NativeRequest {
            method: "PATCH".to_owned(),
            path: "/guilds/1".to_owned(),
            body: Some(serde_json::json!({"name": "Community"})),
            if_match: Some("\"version-3\"".to_owned()),
            headers: BTreeMap::from([(
                "x-AuDiT-LoG-ReAsOn".to_owned(),
                "keep the audit trail useful".to_owned(),
            )]),
        };
        let Ok(forwarded) = native_forward_headers(&request) else {
            panic!("allowlisted headers should be accepted");
        };
        assert_eq!(forwarded.if_match.as_deref(), Some("\"version-3\""));
        assert_eq!(
            forwarded.audit_log_reason.as_deref(),
            Some("keep the audit trail useful")
        );

        let rejected = NativeRequest {
            headers: BTreeMap::from([(
                "Authorization".to_owned(),
                "Bearer attacker-controlled".to_owned(),
            )]),
            ..request
        };
        let Err(error) = native_forward_headers(&rejected) else {
            panic!("unknown headers must fail closed");
        };
        assert_eq!(error.code, "INVALID_NATIVE_HEADER");
    }

    #[test]
    fn password_protocol_version_is_required_and_only_accepts_version_two() {
        let Err(missing) = submitted_password_kdf_version(&serde_json::json!({})) else {
            panic!("missing password protocol should be rejected");
        };
        assert_eq!(missing.code, "INVALID_PASSWORD_PROTOCOL");
        assert_eq!(
            submitted_password_kdf_version(&serde_json::json!({"password_kdf_version": 2})).ok(),
            Some(2)
        );
        for version in [0, 1, 3] {
            let Err(error) = submitted_password_kdf_version(
                &serde_json::json!({"password_kdf_version": version}),
            ) else {
                panic!("unknown password protocol should be rejected");
            };
            assert_eq!(error.code, "INVALID_PASSWORD_PROTOCOL");
        }
    }

    #[test]
    fn native_password_boundary_accepts_canonical_v2_payloads() {
        for path in [
            "auth/login",
            "auth/email/change",
            "auth/mfa/setup",
            "auth/mfa/disable",
        ] {
            assert!(
                validate_native_password_request(
                    "POST",
                    path,
                    &serde_json::json!({
                        "password": derived_password(),
                        "password_kdf_version": 2,
                    }),
                )
                .is_ok(),
                "{path}"
            );
        }
        assert!(
            validate_native_password_request(
                "POST",
                "auth/register",
                &serde_json::json!({
                    "password": derived_password(),
                    "password_kdf": password_kdf(true),
                }),
            )
            .is_ok()
        );
        assert!(
            validate_native_password_request(
                "POST",
                "auth/password/reset",
                &serde_json::json!({
                    "password": derived_password(),
                    "password_kdf": password_kdf(false),
                }),
            )
            .is_ok()
        );
    }

    #[test]
    fn native_password_boundary_rejects_raw_and_malformed_material() {
        for path in [
            "auth/login",
            "auth/email/change",
            "auth/mfa/setup",
            "auth/mfa/disable",
        ] {
            assert_invalid_password_request(
                "POST",
                path,
                &serde_json::json!({
                    "password": "literal-password",
                    "password_kdf_version": 2,
                }),
            );
            assert_invalid_password_request(
                "POST",
                path,
                &serde_json::json!({
                    "password": derived_password(),
                    "password_kdf_version": 1,
                }),
            );
        }
        assert_invalid_password_request(
            "POST",
            "auth/login",
            &serde_json::json!({
                "password": derived_password(),
                "password_kdf_version": 2,
                "password_upgrade": {},
            }),
        );

        for path in ["auth/register", "auth/password/reset"] {
            assert_invalid_password_request(
                "POST",
                path,
                &serde_json::json!({
                    "password": "literal-password",
                    "password_kdf": password_kdf(path == "auth/register"),
                }),
            );
        }

        let mut invalid_registration_kdf = password_kdf(true);
        invalid_registration_kdf["iterations"] = serde_json::json!(1);
        assert_invalid_password_request(
            "POST",
            "auth/register",
            &serde_json::json!({
                "password": derived_password(),
                "password_kdf": invalid_registration_kdf,
            }),
        );
        assert_invalid_password_request(
            "POST",
            "auth/register",
            &serde_json::json!({
                "password": derived_password(),
                "password_kdf": password_kdf(false),
            }),
        );

        let mut reset_kdf_with_vault = password_kdf(true);
        reset_kdf_with_vault["algorithm"] = serde_json::json!("PBKDF2-SHA256");
        assert_invalid_password_request(
            "POST",
            "auth/password/reset",
            &serde_json::json!({
                "password": derived_password(),
                "password_kdf": reset_kdf_with_vault,
            }),
        );
    }

    #[test]
    fn native_password_boundary_blocks_unsupported_routes_before_network_io() {
        let payload = serde_json::json!({
            "password": derived_password(),
            "password_kdf_version": 2,
        });
        assert_invalid_password_request("GET", "auth/login", &payload);
        assert_invalid_password_request("POST", "auth/login?retry=1", &payload);
        assert_invalid_password_request("POST", "users/@me", &payload);
        assert!(
            validate_native_password_request(
                "POST",
                "users/@me",
                &serde_json::json!({"display_name": "Kaede"}),
            )
            .is_ok()
        );
    }

    #[test]
    fn attachment_media_paths_are_narrowly_scoped() {
        let Ok(valid_path) =
            validate_attachment_media_path("/media/chat.example/75512661369970688/thumbnail_512")
        else {
            panic!("expected a valid attachment path");
        };
        assert_eq!(
            valid_path,
            "media/chat.example/75512661369970688/thumbnail_512"
        );
        let history_path = "/api/v1/dms/43@home.example/history-media/50@remote.example/60@remote.example/original?expires=2000000000&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO";
        assert_eq!(
            validate_attachment_media_path(history_path).ok(),
            Some(history_path.trim_start_matches('/'))
        );
        let expired_history_path = "/api/v1/dms/43@home.example/history-media/50@remote.example/60@remote.example/original?expires=1&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO";
        assert_eq!(
            validate_attachment_media_path(expired_history_path).ok(),
            Some(expired_history_path.trim_start_matches('/'))
        );
        for rejected in [
            "/media/chat.example/0/original",
            "/media/chat.example/01/original",
            "/media/chat.example/75512661369970688/unknown",
            "/media/../75512661369970688/original",
            "/media/chat.example/75512661369970688/original?token=secret",
            "/media/chat.example/75512661369970688/original/extra",
            "https://remote.example/media/60",
            "//remote.example/media/60",
            "/api/v1/dms/43@home.example/history-media/50@remote.example/60@remote.example/original?expires=2000000000&token=bad",
            "/api/v1/users/@me?expires=2000000000&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO",
        ] {
            assert!(
                validate_attachment_media_path(rejected).is_err(),
                "{rejected}"
            );
        }
    }

    #[test]
    fn soundboard_media_capabilities_are_bound_to_the_exact_guild_authority()
    -> Result<(), Box<dyn Error>> {
        let configured_authority = Domain::parse("home.example")?;
        let configured_origin = url::Url::parse("https://home.example")?;
        let validate = |value: &str, authority: &str, media_origin: &str| {
            validate_soundboard_media_url(
                value,
                authority,
                media_origin,
                &configured_authority,
                &configured_origin,
            )
        };
        let valid = validate_soundboard_media_url(
            "https://media.guild.example/sounds/one?signature=opaque",
            "Guild.Example",
            "https://media.guild.example",
            &configured_authority,
            &configured_origin,
        );
        assert!(valid.is_ok());
        let object_storage = validate(
            "https://kaede-sounds.s3.example.com/sounds/one?signature=opaque",
            "guild.example",
            "https://kaede-sounds.s3.example.com",
        );
        assert!(matches!(
            object_storage,
            Ok(target) if target.network_policy == PublicDownloadPolicy::PublicOnly
        ));
        assert!(
            validate(
                "https://media.guild.example:8443/sounds/one",
                "guild.example",
                "https://media.guild.example",
            )
            .is_err()
        );
        for rejected in [
            "http://media.guild.example/sounds/one",
            "https://media.guild.example.attacker.test/sounds/one",
            "https://user@media.guild.example/sounds/one",
            "https://media.guild.example/sounds/one#replacement",
        ] {
            assert!(
                validate(rejected, "guild.example", "https://media.guild.example",).is_err(),
                "{rejected}"
            );
        }
        let local_authority = Domain::parse("alpha.localhost")?;
        let local_origin = url::Url::parse("https://alpha.localhost")?;
        let local = validate_soundboard_media_url(
            "https://media.alpha.localhost:18443/sounds/one",
            "alpha.localhost",
            "https://media.alpha.localhost:18443",
            &local_authority,
            &local_origin,
        );
        assert!(matches!(
            local,
            Ok(target)
                if target.network_policy == PublicDownloadPolicy::LoopbackDevelopmentOnly
        ));
        assert!(
            validate(
                "https://media.alpha.localhost:18443/sounds/one",
                "alpha.localhost",
                "https://media.alpha.localhost:18443",
            )
            .is_err()
        );
        assert!(
            validate(
                "https://127.0.0.1/sounds/one",
                "guild.example",
                "https://127.0.0.1",
            )
            .is_err()
        );
        Ok(())
    }

    #[test]
    fn soundboard_digest_accepts_only_canonical_lowercase_sha256() {
        assert!(valid_sha256(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        ));
        assert!(!valid_sha256("abc"));
        assert!(!valid_sha256(
            "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD"
        ));
    }

    #[test]
    fn native_server_errors_keep_support_details_but_show_clear_wording() {
        let native = NativeError::from(ApiClientError::Server {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            error: Box::new(ApiError {
                code: "INTERNAL_SERVER_ERROR".to_owned(),
                message: "Internal Server Error".to_owned(),
                trace_id: Some("7f21d8d0-example-trace".to_owned()),
                permissions: None,
                retry_after_ms: None,
                max_bytes: None,
                timeout_until: None,
                timeout_indefinite: None,
                reason: None,
                errors: Vec::new(),
            }),
        });

        assert_eq!(native.code, "INTERNAL_SERVER_ERROR");
        assert_eq!(native.status, 500);
        assert!(native.message.contains("Try again"));
        assert!(native.message.contains("Error reference: 7f21d8d0-exa."));
        assert!(!native.message.contains("Internal Server Error"));
        assert_eq!(native.detail["trace_id"], "7f21d8d0-example-trace");
    }

    #[test]
    fn invalid_prepared_password_has_safe_actionable_native_error() {
        let native = NativeError::from(AuthError::InvalidPasswordProtocol);

        assert_eq!(native.code, "INVALID_PASSWORD_PROTOCOL");
        assert_eq!(native.status, 0);
        assert!(native.message.contains("Update Kaede"));
        assert!(!native.message.contains("canonical KDF"));
        assert_eq!(native.detail, serde_json::Value::Null);
    }

    #[test]
    fn native_operation_errors_do_not_expose_internal_causes() {
        let error = NativeError::operation(
            "PREFERENCES_INVALID",
            "Kaede could not read your desktop preferences. Reset desktop settings and try again.",
            "expected value at line 4 column 18 in /private/config.json",
        );

        assert!(error.message.contains("Reset desktop settings"));
        assert!(!error.message.contains("line 4"));
        assert_eq!(error.detail, serde_json::Value::Null);
    }
}
