//! Slint callback wiring and platform lifecycle for the desktop executable.
//!
//! This module intentionally owns the wide UI adapter boundary. Slint uses
//! signed 32-bit model values and callback-owned state, so conversions are
//! clamped by the backend's much tighter limits and callback resources are
//! moved by value into `'static` closures.

#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]
#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_sign_loss,
    clippy::needless_pass_by_value,
    clippy::semicolon_if_nothing_returned,
    clippy::struct_excessive_bools,
    clippy::too_many_lines
)]

#[cfg(feature = "native-voice")]
use std::cell::RefCell;
use std::{
    collections::HashMap,
    io::Write,
    net::{TcpListener, TcpStream},
    path::Path,
    rc::Rc,
    sync::{Arc, OnceLock},
};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use copypasta::{ClipboardContext, ClipboardProvider};
#[cfg(feature = "native-voice")]
use global_hotkey::{GlobalHotKeyEvent, GlobalHotKeyManager, HotKeyState, hotkey::HotKey};
use kaede_app::{AccountEvent, AccountRuntime};
use kaede_core::{AppState, ChannelKind, Message, markup::SpanKind};
use kaede_platform::{
    AccountRegistry, CredentialVault, DeepLink, GifFavorite, GifFavorites, KnownAccount,
    NotificationService, PlatformPaths, SystemCredentialVault, SystemNotificationService,
    UpdateClient, parse_deep_link,
};
#[cfg(feature = "native-voice")]
use kaede_platform::{DesktopPreferences, DevicePreference, InputModePreference};
use kaede_protocol::{EntityRef, permission};
use kaede_turnstile::{HELPER_FLAG as TURNSTILE_HELPER_FLAG, run_helper as run_turnstile_helper};
use kaede_ui::{
    AccountItem, AdminChannelItem, AdminMemberItem, AdminRecordItem, AppWindow, ChannelItem,
    CompletionItem, EmojiItem, FriendItem, GifItem, GuildItem, MemberItem, MessageItem,
    OverwriteItem, OverwritePermissionItem, PermissionItem, ProfileRoleItem, RoleItem, SessionItem,
    VideoTileItem,
};
use secrecy::{ExposeSecret, SecretString};
use serde::{Deserialize, Serialize};
use single_instance::SingleInstance;
#[cfg(feature = "native-platform")]
use slint::CloseRequestResponse;
use slint::{Color, ComponentHandle, Image, Model, ModelRc, SharedString, VecModel};
#[cfg(any(feature = "native-platform", feature = "native-voice"))]
use slint::{Rgba8Pixel, SharedPixelBuffer, Timer, TimerMode};
use tokio::sync::{RwLock, mpsc};
use tracing_subscriber::EnvFilter;
#[cfg(feature = "native-platform")]
use tray_icon::{
    Icon, MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent,
    menu::{Menu, MenuEvent, MenuItem},
};

mod emoji;

type ActiveAccount = Arc<RwLock<Option<Arc<AccountRuntime>>>>;
type PendingMfaState = Arc<RwLock<Option<PendingMfa>>>;
#[cfg(feature = "native-voice")]
type ActiveVoice = Arc<tokio::sync::Mutex<Option<kaede_voice::VoiceHandle>>>;
type PendingAttachments = Arc<RwLock<HashMap<EntityRef, Vec<PendingAttachment>>>>;
type SlowModeDeadlines = Arc<RwLock<HashMap<EntityRef, std::time::Instant>>>;
type OverwriteMasks = Arc<RwLock<HashMap<(String, String), (u64, u64)>>>;
type NativeGifFavorites = Arc<RwLock<GifFavorites>>;
#[cfg(feature = "native-voice")]
type VoicePreferences = Arc<RwLock<VoicePreferenceState>>;

static PENDING_DEEP_LINK: OnceLock<tokio::sync::Mutex<Option<DeepLink>>> = OnceLock::new();
/// Local cache paths for downloaded KLIPY GIF stills, keyed by source URL.
/// Slint cannot animate GIFs, so messages and the picker render a decoded
/// still frame the same way the web client renders the moving image.
static GIF_STILLS: OnceLock<std::sync::RwLock<HashMap<String, String>>> = OnceLock::new();
static GIF_STILL_DIR: OnceLock<std::path::PathBuf> = OnceLock::new();
/// Sidebar categories the user collapsed this session. The web client also
/// keeps this in memory only.
static COLLAPSED_CATEGORIES: OnceLock<std::sync::Mutex<std::collections::HashSet<String>>> =
    OnceLock::new();

fn gif_stills() -> &'static std::sync::RwLock<HashMap<String, String>> {
    GIF_STILLS.get_or_init(|| std::sync::RwLock::new(HashMap::new()))
}

fn collapsed_categories() -> &'static std::sync::Mutex<std::collections::HashSet<String>> {
    COLLAPSED_CATEGORIES.get_or_init(|| std::sync::Mutex::new(std::collections::HashSet::new()))
}
#[cfg(feature = "native-voice")]
static ACTIVE_VOICE: OnceLock<ActiveVoice> = OnceLock::new();
#[cfg(feature = "native-voice")]
static VOICE_PREFERENCES: OnceLock<VoicePreferences> = OnceLock::new();

struct NavigationState {
    attachments: PendingAttachments,
    slow_mode_deadlines: SlowModeDeadlines,
    overwrite_masks: OverwriteMasks,
    gif_favorites: NativeGifFavorites,
    paths: PlatformPaths,
}

#[derive(Clone)]
struct PendingAttachment {
    id: kaede_protocol::Snowflake,
    filename: String,
}

#[cfg(feature = "native-voice")]
#[derive(Clone)]
struct VoicePreferenceState {
    input_devices: HashMap<String, String>,
    output_devices: HashMap<String, String>,
    camera_devices: HashMap<String, String>,
    screen_sources: HashMap<String, String>,
    input_device: Option<String>,
    output_device: Option<String>,
    camera_device: Option<String>,
    screen_source: Option<String>,
    mode: kaede_audio::InputMode,
    vad_threshold: f32,
    push_to_talk_hotkey: Option<String>,
}

struct PendingMfa {
    instance: String,
    identifier: String,
    ticket: SecretString,
}

#[cfg(feature = "native-platform")]
struct SystemTray {
    _icon: TrayIcon,
    _event_timer: Timer,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct InstanceEndpoint {
    port: u16,
    secret: String,
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments = std::env::args().collect::<Vec<_>>();
    if arguments
        .get(1)
        .is_some_and(|value| value == TURNSTILE_HELPER_FLAG)
    {
        let url = arguments.get(2).ok_or("missing challenge URL")?;
        let origin = arguments.get(3).ok_or("missing challenge origin")?;
        let request_id = arguments.get(4).ok_or("missing challenge request ID")?;
        run_turnstile_helper(url, origin, request_id)?;
        return Ok(());
    }
    if arguments
        .get(1)
        .is_some_and(|value| value == kaede_media_viewer::HELPER_FLAG)
    {
        let path = arguments.get(2).ok_or("missing media path")?;
        let content_type = arguments.get(3).ok_or("missing media content type")?;
        kaede_media_viewer::run_helper(path, content_type)?;
        return Ok(());
    }
    let paths = PlatformPaths::discover()?;
    let _ = GIF_STILL_DIR.set(paths.cache_dir.join("gif-stills"));
    let instance = SingleInstance::new("chat.kaede.desktop")?;
    if !instance.is_single() {
        if let Some(link) = arguments
            .get(1)
            .filter(|value| parse_deep_link(value).is_ok())
        {
            forward_deep_link(&paths, link)?;
        }
        return Ok(());
    }
    let (instance_listener, instance_endpoint) = create_instance_endpoint(&paths)?;
    let deep_link = arguments
        .get(1)
        .and_then(|value| parse_deep_link(value).ok());
    let _ = PENDING_DEEP_LINK.set(tokio::sync::Mutex::new(deep_link));

    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("kaede=info")),
        )
        .with_target(false)
        .compact()
        .init();
    let runtime = Arc::new(
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .thread_name("kaede-runtime")
            .build()?,
    );
    let window = AppWindow::new()?;
    let _system_tray = install_system_tray(&window);
    install_empty_models(&window);
    let active = Arc::new(RwLock::new(None));
    let pending_mfa = Arc::new(RwLock::new(None));
    let gif_favorites = Arc::new(RwLock::new(
        runtime
            .block_on(GifFavorites::load(&paths))
            .unwrap_or_else(|error| {
                tracing::warn!(%error, "could not load GIF favorites");
                GifFavorites::default()
            }),
    ));
    #[cfg(feature = "native-voice")]
    let desktop_paths = paths.clone();
    #[cfg(feature = "native-voice")]
    let saved_voice_preferences = runtime
        .block_on(DesktopPreferences::load(&desktop_paths))
        .unwrap_or_else(|error| {
            tracing::warn!(%error, "could not load desktop preferences");
            DesktopPreferences::default()
        });
    #[cfg(feature = "native-voice")]
    let voice_preferences = install_audio_devices(&window, &saved_voice_preferences);
    install_login(
        &window,
        runtime.clone(),
        active.clone(),
        pending_mfa.clone(),
    );
    install_account_onboarding(&window, runtime.clone());
    install_mfa(&window, runtime.clone(), active.clone(), pending_mfa);
    install_account_management(&window, runtime.clone(), active.clone());
    install_updates(&window, runtime.clone(), paths.clone());
    install_completions(&window, runtime.clone(), active.clone());
    install_navigation(
        &window,
        runtime.clone(),
        active.clone(),
        NavigationState {
            attachments: Arc::new(RwLock::new(HashMap::new())),
            slow_mode_deadlines: Arc::new(RwLock::new(HashMap::new())),
            overwrite_masks: Arc::new(RwLock::new(HashMap::new())),
            gif_favorites,
            paths: paths.clone(),
        },
    );
    #[cfg(feature = "native-voice")]
    let active_voice = Arc::new(tokio::sync::Mutex::new(None));
    #[cfg(feature = "native-voice")]
    let _ = ACTIVE_VOICE.set(active_voice.clone());
    #[cfg(feature = "native-voice")]
    let _ = VOICE_PREFERENCES.set(voice_preferences.clone());
    #[cfg(feature = "native-voice")]
    install_voice(
        &window,
        runtime.clone(),
        active.clone(),
        active_voice.clone(),
        voice_preferences.clone(),
        desktop_paths.clone(),
    );
    #[cfg(feature = "native-voice")]
    install_global_push_to_talk(
        &window,
        active_voice,
        voice_preferences,
        runtime.clone(),
        desktop_paths,
        saved_voice_preferences.push_to_talk_hotkey,
    );
    #[cfg(not(feature = "native-voice"))]
    install_voice_unavailable(&window);
    install_instance_listener(
        &window,
        runtime.clone(),
        active.clone(),
        instance_listener,
        instance_endpoint.clone(),
    );
    restore_last_account(&window, runtime.clone(), active.clone());
    window.run()?;
    #[cfg(feature = "native-voice")]
    runtime.block_on(leave_active_voice());
    runtime.block_on(async {
        if let Some(account) = active.write().await.take() {
            account.shutdown().await;
        }
    });
    remove_instance_endpoint(&paths, &instance_endpoint);
    drop(instance);
    Ok(())
}

#[cfg(feature = "native-platform")]
fn install_system_tray(window: &AppWindow) -> Option<SystemTray> {
    let show = MenuItem::with_id("show", "Open Kaede Chat", true, None);
    let quit = MenuItem::with_id("quit", "Quit", true, None);
    let menu = Menu::with_items(&[&show, &quit]).ok()?;
    let mut rgba = Vec::with_capacity(32 * 32 * 4);
    for y in 0..32_u32 {
        for x in 0..32_u32 {
            let rounded = (4..28).contains(&x) && (4..28).contains(&y);
            let bubble = rounded && !(x < 10 && y > 23);
            let (red, green, blue, alpha) = if bubble {
                (242, 112, 88, 255)
            } else {
                (0, 0, 0, 0)
            };
            rgba.extend_from_slice(&[red, green, blue, alpha]);
        }
    }
    let icon = Icon::from_rgba(rgba, 32, 32).ok()?;
    let tray = match TrayIconBuilder::new()
        .with_menu(Box::new(menu))
        .with_menu_on_left_click(false)
        .with_tooltip("Kaede Chat")
        .with_icon(icon)
        .build()
    {
        Ok(tray) => tray,
        Err(error) => {
            tracing::warn!(%error, "system tray is unavailable");
            return None;
        }
    };
    let weak = window.as_weak();
    let timer = Timer::default();
    timer.start(
        TimerMode::Repeated,
        std::time::Duration::from_millis(100),
        move || {
            while MenuEvent::receiver().try_recv().is_ok_and(|event| {
                if event.id().0 == "quit" {
                    let _ = slint::quit_event_loop();
                    false
                } else if event.id().0 == "show" {
                    if let Some(window) = weak.upgrade() {
                        let _ = window.show();
                    }
                    true
                } else {
                    true
                }
            }) {}
            while let Ok(event) = TrayIconEvent::receiver().try_recv() {
                if matches!(
                    event,
                    TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } | TrayIconEvent::DoubleClick {
                        button: MouseButton::Left,
                        ..
                    }
                ) && let Some(window) = weak.upgrade()
                {
                    let _ = window.show();
                }
            }
        },
    );
    window
        .window()
        .on_close_requested(|| CloseRequestResponse::HideWindow);
    Some(SystemTray {
        _icon: tray,
        _event_timer: timer,
    })
}

#[cfg(not(feature = "native-platform"))]
fn install_system_tray(_window: &AppWindow) -> Option<()> {
    None
}

fn install_updates(
    window: &AppWindow,
    runtime: Arc<tokio::runtime::Runtime>,
    paths: PlatformPaths,
) {
    let manifest_url = option_env!("KAEDE_DESKTOP_UPDATE_MANIFEST_URL")
        .and_then(|value| url::Url::parse(value).ok());
    let public_key = option_env!("KAEDE_DESKTOP_UPDATE_PUBLIC_KEY_BASE64")
        .and_then(|value| STANDARD.decode(value).ok())
        .and_then(|bytes| <[u8; 32]>::try_from(bytes).ok());
    let (Some(manifest_url), Some(public_key)) = (manifest_url, public_key) else {
        window.on_check_for_updates(|| {});
        return;
    };
    window.set_update_status("Ready to check for a signed update.".into());
    let weak = window.as_weak();
    window.on_check_for_updates(move || {
        let weak = weak.clone();
        let manifest_url = manifest_url.clone();
        let paths = paths.clone();
        let _ = weak.upgrade_in_event_loop(|window| {
            window.set_update_status("Checking for updates…".into());
        });
        runtime.spawn(async move {
            let result = async {
                let client = UpdateClient::new()?;
                let manifest = client.fetch_manifest(manifest_url, &public_key).await?;
                if manifest.version == env!("CARGO_PKG_VERSION") {
                    return Ok::<_, kaede_platform::PlatformError>(
                        "Kaede Desktop is up to date.".to_owned(),
                    );
                }
                let path = client.download_verified(&manifest, &paths).await?;
                Ok(format!(
                    "Version {} was verified and staged at {}. Restart through your platform installer to apply it.",
                    manifest.version,
                    path.display()
                ))
            }
            .await;
            let message = result.unwrap_or_else(|error| format!("Update check failed: {error}"));
            let _ = weak.upgrade_in_event_loop(move |window| {
                window.set_update_status(message.into());
            });
        });
    });
}

fn instance_endpoint_path(paths: &PlatformPaths) -> std::path::PathBuf {
    paths.config_dir.join("desktop-instance.json")
}

fn create_instance_endpoint(
    paths: &PlatformPaths,
) -> Result<(TcpListener, InstanceEndpoint), Box<dyn std::error::Error>> {
    std::fs::create_dir_all(&paths.config_dir)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&paths.config_dir, std::fs::Permissions::from_mode(0o700))?;
    }
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))?;
    listener.set_nonblocking(true)?;
    let endpoint = InstanceEndpoint {
        port: listener.local_addr()?.port(),
        secret: uuid::Uuid::new_v4().as_simple().to_string(),
    };
    let path = instance_endpoint_path(paths);
    let temporary = path.with_extension("json.tmp");
    std::fs::write(&temporary, serde_json::to_vec(&endpoint)?)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))?;
    }
    std::fs::rename(temporary, path)?;
    Ok((listener, endpoint))
}

fn forward_deep_link(paths: &PlatformPaths, link: &str) -> Result<(), Box<dyn std::error::Error>> {
    if link.len() > 4096 || parse_deep_link(link).is_err() {
        return Err("invalid application link".into());
    }
    let endpoint: InstanceEndpoint =
        serde_json::from_slice(&std::fs::read(instance_endpoint_path(paths))?)?;
    let mut stream = TcpStream::connect_timeout(
        &std::net::SocketAddr::from((std::net::Ipv4Addr::LOCALHOST, endpoint.port)),
        std::time::Duration::from_secs(2),
    )?;
    stream.set_write_timeout(Some(std::time::Duration::from_secs(2)))?;
    write!(stream, "{}\n{}", endpoint.secret, link)?;
    Ok(())
}

fn install_instance_listener(
    window: &AppWindow,
    runtime: Arc<tokio::runtime::Runtime>,
    active: ActiveAccount,
    listener: TcpListener,
    endpoint: InstanceEndpoint,
) {
    let weak = window.as_weak();
    let create_runtime = runtime.clone();
    let create_active = active.clone();
    window.on_create_guild(move |name| {
        let name = name.trim().to_owned();
        if name.is_empty() {
            return;
        }
        let weak = weak.clone();
        let active = create_active.clone();
        create_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.service.create_guild(&name).await {
                Ok(guild) => {
                    let guild_ref = guild.key();
                    if let Err(error) = account.load_guild(&guild_ref).await {
                        show_account_error(&weak, error.to_string());
                        return;
                    }
                    let selected_channel = match hydrate_guild_landing(&account, &guild_ref).await {
                        Ok(channel) => channel,
                        Err(error) => {
                            show_account_error(&weak, error.to_string());
                            return;
                        }
                    };
                    let state = account.state.read().await;
                    let snapshot = ui_snapshot(&state);
                    drop(state);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_selected_guild(guild_ref.to_string().into());
                        window.set_selected_channel(
                            selected_channel.map_or_else(SharedString::default, |value| {
                                value.to_string().into()
                            }),
                        );
                        apply_snapshot(&window, snapshot);
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let join_runtime = runtime.clone();
    let join_active = active.clone();
    window.on_join_guild(move |input| {
        let Some(code) = invite_code_from_input(input.as_str()) else {
            let _ = weak.upgrade_in_event_loop(|window| {
                window.set_error_message("Enter a valid invitation code or URL.".into());
            });
            return;
        };
        let weak = weak.clone();
        let active = join_active.clone();
        join_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let preview = match account.service.preview_invite(&code).await {
                Ok(value) => value,
                Err(error) => {
                    show_account_error(&weak, error.to_string());
                    return;
                }
            };
            let guild_ref = preview
                .get("guild")
                .map(|guild| entity_field(guild, &["id"], &["origin_domain", "domain"]))
                .and_then(|value| value.parse::<EntityRef>().ok());
            if let Err(error) = account.service.accept_invite(&code).await {
                show_account_error(&weak, error.to_string());
                return;
            }
            if let Err(error) = account.reconcile().await {
                show_account_error(&weak, error.to_string());
                return;
            }
            if let Some(guild_ref) = guild_ref.as_ref()
                && let Err(error) = account.load_guild(guild_ref).await
            {
                show_account_error(&weak, error.to_string());
                return;
            }
            let selected_channel = if let Some(guild) = guild_ref.as_ref() {
                match hydrate_guild_landing(&account, guild).await {
                    Ok(channel) => channel,
                    Err(error) => {
                        show_account_error(&weak, error.to_string());
                        return;
                    }
                }
            } else {
                None
            };
            let state = account.state.read().await;
            let snapshot = ui_snapshot(&state);
            drop(state);
            let _ = weak.upgrade_in_event_loop(move |window| {
                if let Some(guild) = guild_ref {
                    window.set_selected_guild(guild.to_string().into());
                }
                window.set_selected_channel(
                    selected_channel
                        .map_or_else(SharedString::default, |value| value.to_string().into()),
                );
                apply_snapshot(&window, snapshot);
            });
        });
    });

    let weak = window.as_weak();
    runtime.spawn(async move {
        let listener = match tokio::net::TcpListener::from_std(listener) {
            Ok(listener) => listener,
            Err(error) => {
                tracing::warn!(%error, "could not start desktop application-link listener");
                return;
            }
        };
        loop {
            let Ok((mut stream, address)) = listener.accept().await else {
                continue;
            };
            if !address.ip().is_loopback() {
                continue;
            }
            let mut bytes = Vec::with_capacity(512);
            let read = tokio::time::timeout(
                std::time::Duration::from_secs(2),
                tokio::io::AsyncReadExt::read_to_end(&mut stream, &mut bytes),
            )
            .await;
            if !matches!(read, Ok(Ok(_))) || bytes.len() > 8192 {
                continue;
            }
            let Ok(payload) = String::from_utf8(bytes) else {
                continue;
            };
            let Some((secret, value)) = payload.split_once('\n') else {
                continue;
            };
            if secret != endpoint.secret {
                continue;
            }
            let Ok(link) = parse_deep_link(value) else {
                continue;
            };
            if let Some(pending) = PENDING_DEEP_LINK.get() {
                *pending.lock().await = Some(link);
            }
            if let Some(account) = active.read().await.clone() {
                follow_pending_deep_link(&account, &weak).await;
            }
            let _ = weak.upgrade_in_event_loop(|window| {
                let _ = window.show();
            });
        }
    });
}

fn remove_instance_endpoint(paths: &PlatformPaths, endpoint: &InstanceEndpoint) {
    let path = instance_endpoint_path(paths);
    let matches = std::fs::read(&path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<InstanceEndpoint>(&bytes).ok())
        .is_some_and(|candidate| candidate.secret == endpoint.secret);
    if matches {
        let _ = std::fs::remove_file(path);
    }
}

#[cfg(feature = "native-voice")]
fn install_voice(
    window: &AppWindow,
    runtime: Arc<tokio::runtime::Runtime>,
    active: ActiveAccount,
    voice: ActiveVoice,
    preferences: VoicePreferences,
    paths: PlatformPaths,
) {
    let weak = window.as_weak();
    let join_runtime = runtime.clone();
    let join_active = active.clone();
    let join_voice = voice.clone();
    let join_preferences = preferences.clone();
    window.on_join_voice(move |value| {
        let Ok(channel) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        if let Some(window) = weak.upgrade() {
            window.set_voice_status("connecting".into());
        }
        let weak = weak.clone();
        let active = join_active.clone();
        let voice = join_voice.clone();
        let preferences = join_preferences.clone();
        join_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let preferences = preferences.read().await;
            let capture_settings = kaede_audio::CaptureSettings {
                device_id: preferences.input_device.clone(),
                mode: preferences.mode,
                vad_threshold: preferences.vad_threshold,
                ..kaede_audio::CaptureSettings::default()
            };
            let output_device = preferences.output_device.clone();
            drop(preferences);
            match kaede_voice::join_channel(
                account.api.clone(),
                &channel,
                capture_settings,
                output_device,
            )
            .await
            {
                Ok(handle) => {
                    activate_voice_handle(handle, voice, weak, Some((account, channel))).await
                }
                Err(error) => {
                    let message = friendly_error(&error.to_string());
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_voice_status("disconnected".into());
                        window.set_error_message(message.into());
                    });
                }
            }
        });
    });

    let weak = window.as_weak();
    let leave_voice = voice.clone();
    let leave_runtime = runtime.clone();
    window.on_leave_voice(move || {
        let weak = weak.clone();
        let voice = leave_voice.clone();
        leave_runtime.spawn(async move {
            if let Some(handle) = voice.lock().await.take() {
                handle.leave().await;
            }
            let _ = weak.upgrade_in_event_loop(|window| {
                window.set_voice_status("disconnected".into());
                window.set_voice_can_speak(false);
                window.set_voice_can_stream(false);
                window.set_voice_screen_sharing(false);
                window.set_voice_camera_enabled(false);
                window.set_voice_remote_videos(ModelRc::from(Rc::new(
                    VecModel::<VideoTileItem>::default(),
                )));
                window.set_voice_deafened(false);
                window.set_voice_muted(false);
            });
        });
    });

    let mute_voice = voice.clone();
    window.on_set_voice_muted(move |muted| {
        if let Ok(guard) = mute_voice.try_lock()
            && let Some(handle) = guard.as_ref()
        {
            let _ = handle
                .commands
                .try_send(kaede_voice::VoiceCommand::SetMuted(muted));
        }
    });

    let deafen_voice = voice.clone();
    window.on_set_voice_deafened(move |deafened| {
        if let Ok(guard) = deafen_voice.try_lock()
            && let Some(handle) = guard.as_ref()
        {
            let _ = handle
                .commands
                .try_send(kaede_voice::VoiceCommand::SetDeafened(deafened));
        }
    });

    let ptt_voice = voice.clone();
    window.on_set_push_to_talk(move |pressed| {
        if let Ok(guard) = ptt_voice.try_lock()
            && let Some(handle) = guard.as_ref()
        {
            let _ = handle
                .commands
                .try_send(kaede_voice::VoiceCommand::SetPushToTalk(pressed));
        }
    });

    let camera_voice = voice.clone();
    let camera_preferences = preferences.clone();
    window.on_set_camera(move |enabled| {
        if let Ok(guard) = camera_voice.try_lock()
            && let Some(handle) = guard.as_ref()
        {
            let device_id = camera_preferences
                .try_read()
                .ok()
                .and_then(|preferences| preferences.camera_device.clone());
            let _ = handle
                .commands
                .try_send(kaede_voice::VoiceCommand::SetCamera { enabled, device_id });
        }
    });

    let screen_voice = voice.clone();
    let screen_preferences = preferences.clone();
    window.on_set_screen_share(move |enabled| {
        if let Ok(guard) = screen_voice.try_lock()
            && let Some(handle) = guard.as_ref()
        {
            let source_id = screen_preferences
                .try_read()
                .ok()
                .and_then(|preferences| preferences.screen_source.clone());
            let _ = handle
                .commands
                .try_send(kaede_voice::VoiceCommand::SetScreenShare { enabled, source_id });
        }
    });

    let input_preferences = preferences.clone();
    let input_runtime = runtime.clone();
    let input_paths = paths.clone();
    window.on_select_input_device(move |label| {
        if let Ok(mut preferences) = input_preferences.try_write() {
            preferences.input_device = preferences
                .input_devices
                .get(label.as_str())
                .filter(|value| !value.is_empty())
                .cloned();
            persist_voice_preferences(&input_runtime, &input_paths, &preferences);
        }
    });
    let output_preferences = preferences.clone();
    let output_runtime = runtime.clone();
    let output_paths = paths.clone();
    window.on_select_output_device(move |label| {
        if let Ok(mut preferences) = output_preferences.try_write() {
            preferences.output_device = preferences
                .output_devices
                .get(label.as_str())
                .filter(|value| !value.is_empty())
                .cloned();
            persist_voice_preferences(&output_runtime, &output_paths, &preferences);
        }
    });
    let camera_preferences = preferences.clone();
    let camera_runtime = runtime.clone();
    let camera_paths = paths.clone();
    window.on_select_camera_device(move |label| {
        if let Ok(mut preferences) = camera_preferences.try_write() {
            preferences.camera_device = preferences
                .camera_devices
                .get(label.as_str())
                .filter(|value| !value.is_empty())
                .cloned();
            persist_voice_preferences(&camera_runtime, &camera_paths, &preferences);
        }
    });
    let screen_preferences = preferences.clone();
    let screen_runtime = runtime.clone();
    let screen_paths = paths.clone();
    window.on_select_screen_source(move |label| {
        if let Ok(mut preferences) = screen_preferences.try_write() {
            preferences.screen_source = preferences
                .screen_sources
                .get(label.as_str())
                .filter(|value| !value.is_empty())
                .cloned();
            persist_voice_preferences(&screen_runtime, &screen_paths, &preferences);
        }
    });
    let refresh_preferences = preferences.clone();
    let weak = window.as_weak();
    window.on_refresh_media_devices(move || {
        refresh_audio_devices(&weak, &refresh_preferences);
    });
    // CPAL does not expose one portable hot-plug callback. Reconcile the
    // inventory on a conservative interval instead. This updates selectors
    // without interrupting a live room; a removed active device is replaced
    // by the system default on the next connection.
    let hotplug_preferences = preferences.clone();
    let hotplug_weak = window.as_weak();
    runtime.spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(15));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        interval.tick().await;
        loop {
            interval.tick().await;
            let preferences = hotplug_preferences.clone();
            if hotplug_weak
                .upgrade_in_event_loop(move |window| {
                    refresh_audio_devices(&window.as_weak(), &preferences);
                })
                .is_err()
            {
                break;
            }
        }
    });
    let mode_preferences = preferences.clone();
    let mode_runtime = runtime.clone();
    let mode_paths = paths.clone();
    window.on_select_input_mode(move |mode| {
        if let Ok(mut preferences) = mode_preferences.try_write() {
            preferences.mode = if mode.as_str() == "push_to_talk" {
                kaede_audio::InputMode::PushToTalk
            } else {
                kaede_audio::InputMode::VoiceActivity
            };
            persist_voice_preferences(&mode_runtime, &mode_paths, &preferences);
        }
    });

    let start_runtime = runtime.clone();
    let start_active = active.clone();
    let start_voice = voice.clone();
    let start_preferences = preferences.clone();
    let weak = window.as_weak();
    window.on_start_call(move |channel| {
        let Ok(channel) = channel.as_str().parse::<EntityRef>() else {
            return;
        };
        let active = start_active.clone();
        let voice = start_voice.clone();
        let preferences = start_preferences.clone();
        let weak = weak.clone();
        start_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let result = async {
                let call = account
                    .service
                    .start_call(&channel)
                    .await
                    .map_err(|error| error.to_string())?;
                let settings = preferences.read().await;
                let capture = kaede_audio::CaptureSettings {
                    device_id: settings.input_device.clone(),
                    mode: settings.mode,
                    vad_threshold: settings.vad_threshold,
                    ..kaede_audio::CaptureSettings::default()
                };
                let output = settings.output_device.clone();
                drop(settings);
                kaede_voice::join_call(account.api.clone(), &call.key(), capture, output)
                    .await
                    .map_err(|error| error.to_string())
            }
            .await;
            match result {
                Ok(handle) => activate_voice_handle(handle, voice, weak, None).await,
                Err(error) => show_async_error(&weak, &error),
            }
        });
    });

    let accept_runtime = runtime.clone();
    let accept_active = active.clone();
    let accept_voice = voice.clone();
    let accept_preferences = preferences.clone();
    let weak = window.as_weak();
    window.on_accept_call(move |call| {
        let Ok(call) = call.as_str().parse::<EntityRef>() else {
            return;
        };
        let active = accept_active.clone();
        let voice = accept_voice.clone();
        let preferences = accept_preferences.clone();
        let weak = weak.clone();
        accept_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let result = async {
                account
                    .service
                    .act_on_call(&call, "accept")
                    .await
                    .map_err(|error| error.to_string())?;
                let settings = preferences.read().await;
                let capture = kaede_audio::CaptureSettings {
                    device_id: settings.input_device.clone(),
                    mode: settings.mode,
                    vad_threshold: settings.vad_threshold,
                    ..kaede_audio::CaptureSettings::default()
                };
                let output = settings.output_device.clone();
                drop(settings);
                kaede_voice::join_call(account.api.clone(), &call, capture, output)
                    .await
                    .map_err(|error| error.to_string())
            }
            .await;
            match result {
                Ok(handle) => activate_voice_handle(handle, voice, weak, None).await,
                Err(error) => show_async_error(&weak, &error),
            }
        });
    });

    for (action, end_local) in [("decline", false), ("end", true)] {
        let action_runtime = runtime.clone();
        let action_active = active.clone();
        let action_voice = voice.clone();
        let weak = window.as_weak();
        let handler = move |call: SharedString| {
            let Ok(call) = call.as_str().parse::<EntityRef>() else {
                return;
            };
            let active = action_active.clone();
            let voice = action_voice.clone();
            let weak = weak.clone();
            action_runtime.spawn(async move {
                let Some(account) = active.read().await.clone() else {
                    return;
                };
                if let Err(error) = account.service.act_on_call(&call, action).await {
                    show_async_error(&weak, &error.to_string());
                    return;
                }
                if end_local && let Some(handle) = voice.lock().await.take() {
                    handle.leave().await;
                }
                let _ = weak.upgrade_in_event_loop(|window| {
                    window.set_active_call_id(SharedString::default());
                    window.set_active_call_state(SharedString::default());
                });
            });
        };
        if action == "decline" {
            window.on_decline_call(handler);
        } else {
            window.on_end_call(handler);
        }
    }
}

#[cfg(feature = "native-voice")]
async fn activate_voice_handle(
    mut handle: kaede_voice::VoiceHandle,
    voice: ActiveVoice,
    weak: slint::Weak<AppWindow>,
    occupancy: Option<(Arc<AccountRuntime>, EntityRef)>,
) {
    let video_frames = handle.video_frames.take();
    let mut status = handle.status.clone();
    let mut occupancy_status = handle.status.clone();
    let previous = {
        let mut active = voice.lock().await;
        active.replace(handle)
    };
    if let Some(previous) = previous {
        previous.leave().await;
    }
    if let Some(mut video_frames) = video_frames {
        let video_weak = weak.clone();
        tokio::spawn(async move {
            let mut tiles = HashMap::<String, (Vec<u8>, u32, u32)>::new();
            while let Some(frame) = video_frames.recv().await {
                let participant = frame.participant;
                let removed = frame.removed;
                let rgba = frame.rgba;
                let width = frame.width;
                let height = frame.height;
                if removed {
                    tiles.remove(&participant);
                } else {
                    tiles.insert(participant, (rgba, width, height));
                }
                let mut rows = tiles
                    .iter()
                    .map(|(participant, (rgba, width, height))| {
                        (participant.clone(), rgba.clone(), *width, *height)
                    })
                    .collect::<Vec<_>>();
                rows.sort_by(|left, right| left.0.cmp(&right.0));
                let _ = video_weak.upgrade_in_event_loop(move |window| {
                    let rows = rows
                        .into_iter()
                        .map(|(participant, rgba, width, height)| VideoTileItem {
                            participant: participant.into(),
                            frame: Image::from_rgba8(
                                SharedPixelBuffer::<Rgba8Pixel>::clone_from_slice(
                                    &rgba, width, height,
                                ),
                            ),
                        })
                        .collect::<Vec<_>>();
                    window.set_voice_remote_videos(ModelRc::from(Rc::new(VecModel::from(rows))));
                });
            }
        });
    }
    if let Some((account, channel)) = occupancy {
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(std::time::Duration::from_secs(5));
            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                tokio::select! {
                    _ = interval.tick() => {
                        if let Err(error) = account.refresh_voice_occupancy(&channel).await {
                            tracing::debug!(%error, %channel, "voice occupancy refresh failed");
                        }
                    }
                    changed = occupancy_status.changed() => {
                        if changed.is_err() || matches!(*occupancy_status.borrow(), kaede_voice::VoiceStatus::Disconnected | kaede_voice::VoiceStatus::Failed(_)) {
                            break;
                        }
                    }
                }
            }
        });
    }
    tokio::spawn(async move {
        loop {
            let (label, can_speak, can_stream, screen_sharing, camera_enabled) =
                match status.borrow().clone() {
                    kaede_voice::VoiceStatus::Disconnected => {
                        ("disconnected".to_owned(), false, false, false, false)
                    }
                    kaede_voice::VoiceStatus::Connecting => {
                        ("connecting".to_owned(), false, false, false, false)
                    }
                    kaede_voice::VoiceStatus::Connected {
                        can_speak,
                        can_stream,
                        screen_sharing,
                        camera_enabled,
                        ..
                    } => (
                        "connected".to_owned(),
                        can_speak,
                        can_stream,
                        screen_sharing,
                        camera_enabled,
                    ),
                    kaede_voice::VoiceStatus::Reconnecting => {
                        ("reconnecting".to_owned(), false, false, false, false)
                    }
                    kaede_voice::VoiceStatus::MediaError {
                        message,
                        can_speak,
                        can_stream,
                        screen_sharing,
                        camera_enabled,
                        ..
                    } => {
                        let notice = friendly_error(&message);
                        let notice_weak = weak.clone();
                        let _ = notice_weak.upgrade_in_event_loop(move |window| {
                            window.set_error_message(notice.into());
                        });
                        (
                            "connected".to_owned(),
                            can_speak,
                            can_stream,
                            screen_sharing,
                            camera_enabled,
                        )
                    }
                    kaede_voice::VoiceStatus::Failed(reason) => {
                        (friendly_error(&reason), false, false, false, false)
                    }
                };
            let _ = weak.upgrade_in_event_loop(move |window| {
                window.set_voice_status(label.into());
                window.set_voice_can_speak(can_speak);
                window.set_voice_can_stream(can_stream);
                window.set_voice_screen_sharing(screen_sharing);
                window.set_voice_camera_enabled(camera_enabled);
            });
            if status.changed().await.is_err() {
                break;
            }
        }
    });
}

#[cfg(feature = "native-voice")]
fn show_async_error(weak: &slint::Weak<AppWindow>, error: &str) {
    let message = friendly_error(error);
    let _ = weak.upgrade_in_event_loop(move |window| window.set_error_message(message.into()));
}

#[cfg(feature = "native-voice")]
fn install_audio_devices(window: &AppWindow, saved: &DesktopPreferences) -> VoicePreferences {
    let mut input_devices = HashMap::new();
    let mut output_devices = HashMap::new();
    let mut camera_devices = HashMap::new();
    let mut screen_sources = HashMap::new();
    input_devices.insert("System default".to_owned(), String::new());
    output_devices.insert("System default".to_owned(), String::new());
    camera_devices.insert("System default".to_owned(), String::new());
    screen_sources.insert("Operating-system picker".to_owned(), String::new());
    if let Ok(devices) = kaede_audio::input_devices() {
        for device in devices {
            input_devices.insert(device.label, device.id);
        }
    }
    if let Ok(devices) = kaede_audio::output_devices() {
        for device in devices {
            output_devices.insert(device.label, device.id);
        }
    }
    if let Ok(devices) = kaede_voice::camera_devices() {
        for device in devices {
            camera_devices.insert(device.label, device.id);
        }
    }
    for source in kaede_voice::screen_sources() {
        screen_sources.insert(source.label, source.id);
    }
    let mut input_labels = input_devices.keys().cloned().collect::<Vec<_>>();
    let mut output_labels = output_devices.keys().cloned().collect::<Vec<_>>();
    let mut camera_labels = camera_devices.keys().cloned().collect::<Vec<_>>();
    let mut screen_labels = screen_sources.keys().cloned().collect::<Vec<_>>();
    input_labels.sort();
    output_labels.sort();
    input_labels.sort_by_key(|label| label != "System default");
    output_labels.sort_by_key(|label| label != "System default");
    camera_labels.sort();
    camera_labels.sort_by_key(|label| label != "System default");
    screen_labels.sort();
    screen_labels.sort_by_key(|label| label != "Operating-system picker");
    window.set_input_devices(ModelRc::from(Rc::new(VecModel::from(
        input_labels
            .into_iter()
            .map(SharedString::from)
            .collect::<Vec<_>>(),
    ))));
    window.set_output_devices(ModelRc::from(Rc::new(VecModel::from(
        output_labels
            .into_iter()
            .map(SharedString::from)
            .collect::<Vec<_>>(),
    ))));
    window.set_camera_devices(ModelRc::from(Rc::new(VecModel::from(
        camera_labels
            .into_iter()
            .map(SharedString::from)
            .collect::<Vec<_>>(),
    ))));
    window.set_screen_sources(ModelRc::from(Rc::new(VecModel::from(
        screen_labels
            .into_iter()
            .map(SharedString::from)
            .collect::<Vec<_>>(),
    ))));
    let input_device = resolve_device(saved.input_device.as_ref(), &input_devices);
    let output_device = resolve_device(saved.output_device.as_ref(), &output_devices);
    let camera_device = resolve_device(saved.camera_device.as_ref(), &camera_devices);
    let screen_source = resolve_device(saved.screen_source.as_ref(), &screen_sources);
    window.set_selected_input_device(device_label(input_device.as_deref(), &input_devices).into());
    window
        .set_selected_output_device(device_label(output_device.as_deref(), &output_devices).into());
    window
        .set_selected_camera_device(device_label(camera_device.as_deref(), &camera_devices).into());
    window.set_selected_screen_source(
        device_label_with_default(
            screen_source.as_deref(),
            &screen_sources,
            "Operating-system picker",
        )
        .into(),
    );
    let mode = match saved.input_mode {
        InputModePreference::PushToTalk => kaede_audio::InputMode::PushToTalk,
        InputModePreference::VoiceActivity => kaede_audio::InputMode::VoiceActivity,
    };
    window.set_input_mode(
        if mode == kaede_audio::InputMode::PushToTalk {
            "push_to_talk"
        } else {
            "voice_activity"
        }
        .into(),
    );
    window.set_push_to_talk_hotkey(saved.push_to_talk_hotkey.clone().unwrap_or_default().into());
    Arc::new(RwLock::new(VoicePreferenceState {
        input_devices,
        output_devices,
        camera_devices,
        screen_sources,
        input_device,
        output_device,
        camera_device,
        screen_source,
        mode,
        vad_threshold: if saved.vad_threshold > 0.0 {
            saved.vad_threshold
        } else {
            kaede_audio::CaptureSettings::default().vad_threshold
        },
        push_to_talk_hotkey: saved.push_to_talk_hotkey.clone(),
    }))
}

#[cfg(feature = "native-voice")]
fn resolve_device(
    saved: Option<&DevicePreference>,
    devices: &HashMap<String, String>,
) -> Option<String> {
    let saved = saved?;
    if !saved.id.is_empty() && devices.values().any(|candidate| candidate == &saved.id) {
        Some(saved.id.clone())
    } else {
        devices
            .get(&saved.label)
            .filter(|value| !value.is_empty())
            .cloned()
    }
}

#[cfg(feature = "native-voice")]
fn device_label<'a>(id: Option<&str>, devices: &'a HashMap<String, String>) -> &'a str {
    device_label_with_default(id, devices, "System default")
}

#[cfg(feature = "native-voice")]
fn device_label_with_default<'a>(
    id: Option<&str>,
    devices: &'a HashMap<String, String>,
    default: &'a str,
) -> &'a str {
    id.and_then(|id| {
        devices
            .iter()
            .find_map(|(label, candidate)| (candidate == id).then_some(label.as_str()))
    })
    .unwrap_or(default)
}

#[cfg(feature = "native-voice")]
fn refresh_audio_devices(weak: &slint::Weak<AppWindow>, preferences: &VoicePreferences) {
    let Some(window) = weak.upgrade() else {
        return;
    };
    let saved = preferences
        .try_read()
        .ok()
        .map(|current| desktop_preferences(&current))
        .unwrap_or_default();
    let refreshed = install_audio_devices(&window, &saved);
    let Ok(refreshed) = refreshed.try_read() else {
        return;
    };
    if let Ok(mut current) = preferences.try_write() {
        let selected_input = current.input_device.clone();
        let selected_output = current.output_device.clone();
        let selected_camera = current.camera_device.clone();
        current.input_devices.clone_from(&refreshed.input_devices);
        current.output_devices.clone_from(&refreshed.output_devices);
        current.camera_devices.clone_from(&refreshed.camera_devices);
        current.screen_sources.clone_from(&refreshed.screen_sources);
        current.input_device = selected_input.filter(|id| {
            current
                .input_devices
                .values()
                .any(|candidate| candidate == id)
        });
        current.output_device = selected_output.filter(|id| {
            current
                .output_devices
                .values()
                .any(|candidate| candidate == id)
        });
        current.camera_device = selected_camera.filter(|id| {
            current
                .camera_devices
                .values()
                .any(|candidate| candidate == id)
        });
        let selected_screen = current.screen_source.clone();
        current.screen_source = selected_screen.filter(|id| {
            current
                .screen_sources
                .values()
                .any(|candidate| candidate == id)
        });
    }
}

#[cfg(feature = "native-voice")]
fn desktop_preferences(state: &VoicePreferenceState) -> DesktopPreferences {
    let device = |id: Option<&String>, devices: &HashMap<String, String>| {
        id.map(|id| DevicePreference {
            id: id.clone(),
            label: device_label(Some(id), devices).to_owned(),
        })
    };
    DesktopPreferences {
        input_device: device(state.input_device.as_ref(), &state.input_devices),
        output_device: device(state.output_device.as_ref(), &state.output_devices),
        camera_device: device(state.camera_device.as_ref(), &state.camera_devices),
        screen_source: device(state.screen_source.as_ref(), &state.screen_sources),
        input_mode: if state.mode == kaede_audio::InputMode::PushToTalk {
            InputModePreference::PushToTalk
        } else {
            InputModePreference::VoiceActivity
        },
        vad_threshold: state.vad_threshold,
        push_to_talk_hotkey: state.push_to_talk_hotkey.clone(),
        ..DesktopPreferences::default()
    }
}

#[cfg(feature = "native-voice")]
fn persist_voice_preferences(
    runtime: &Arc<tokio::runtime::Runtime>,
    paths: &PlatformPaths,
    state: &VoicePreferenceState,
) {
    let preferences = desktop_preferences(state);
    let paths = paths.clone();
    runtime.spawn(async move {
        if let Err(error) = preferences.save(&paths).await {
            tracing::warn!(%error, "could not save desktop voice preferences");
        }
    });
}

#[cfg(feature = "native-voice")]
fn install_global_push_to_talk(
    window: &AppWindow,
    voice: ActiveVoice,
    preferences: VoicePreferences,
    runtime: Arc<tokio::runtime::Runtime>,
    paths: PlatformPaths,
    configured: Option<String>,
) {
    let manager = match GlobalHotKeyManager::new() {
        Ok(manager) => Rc::new(manager),
        Err(error) => {
            window.set_global_hotkey_status(
                format!("Unavailable on this desktop session: {error}").into(),
            );
            window.on_select_push_to_talk_hotkey(|_| {});
            return;
        }
    };

    GlobalHotKeyEvent::set_event_handler(Some(move |event: GlobalHotKeyEvent| {
        if let Ok(guard) = voice.try_lock()
            && let Some(handle) = guard.as_ref()
        {
            let _ = handle
                .commands
                .try_send(kaede_voice::VoiceCommand::SetPushToTalk(
                    event.state() == HotKeyState::Pressed,
                ));
        }
    }));

    let current = Rc::new(RefCell::new(None::<HotKey>));
    if let Some(configured) = configured.filter(|value| !value.trim().is_empty()) {
        match configured.parse::<HotKey>() {
            Ok(hotkey) => match manager.register(hotkey) {
                Ok(()) => {
                    *current.borrow_mut() = Some(hotkey);
                    window
                        .set_global_hotkey_status(format!("Active globally: {configured}").into());
                }
                Err(error) => window.set_global_hotkey_status(
                    format!("Could not register {configured}: {error}").into(),
                ),
            },
            Err(error) => {
                window.set_global_hotkey_status(format!("Invalid shortcut: {error}").into())
            }
        }
    }

    let weak = window.as_weak();
    window.on_select_push_to_talk_hotkey(move |value| {
        let value = value.trim().to_owned();
        let replacement = if value.is_empty() {
            None
        } else {
            match value.parse::<HotKey>() {
                Ok(hotkey) => Some(hotkey),
                Err(error) => {
                    if let Some(window) = weak.upgrade() {
                        window
                            .set_global_hotkey_status(format!("Invalid shortcut: {error}").into());
                    }
                    return;
                }
            }
        };
        let previous = current.borrow_mut().take();
        if let Some(previous) = previous {
            let _ = manager.unregister(previous);
        }
        if let Some(replacement) = replacement {
            if let Err(error) = manager.register(replacement) {
                if let Some(previous) = previous {
                    let _ = manager.register(previous);
                    *current.borrow_mut() = Some(previous);
                }
                if let Some(window) = weak.upgrade() {
                    window.set_global_hotkey_status(
                        format!("Could not register {value}: {error}").into(),
                    );
                }
                return;
            }
            *current.borrow_mut() = Some(replacement);
        }
        if let Ok(mut state) = preferences.try_write() {
            state.push_to_talk_hotkey = (!value.is_empty()).then_some(value.clone());
            persist_voice_preferences(&runtime, &paths, &state);
        }
        if let Some(window) = weak.upgrade() {
            window.set_global_hotkey_status(
                if value.is_empty() {
                    "Global push to talk is disabled.".to_owned()
                } else {
                    format!("Active globally: {value}")
                }
                .into(),
            );
        }
    });
}

#[cfg(not(feature = "native-voice"))]
fn install_voice_unavailable(window: &AppWindow) {
    window.set_voice_status("native audio support is unavailable in this build".into());
    window.on_join_voice(|_| {});
    window.on_leave_voice(|| {});
    window.on_set_voice_muted(|_| {});
    window.on_set_voice_deafened(|_| {});
    window.on_set_push_to_talk(|_| {});
    window.on_set_camera(|_| {});
    window.on_set_screen_share(|_| {});
    window.on_select_input_device(|_| {});
    window.on_select_output_device(|_| {});
    window.on_select_camera_device(|_| {});
    window.on_select_screen_source(|_| {});
    window.on_refresh_media_devices(|| {});
    window.on_select_input_mode(|_| {});
    window.on_start_call(|_| {});
    window.on_accept_call(|_| {});
    window.on_decline_call(|_| {});
    window.on_end_call(|_| {});
}

fn install_login(
    window: &AppWindow,
    runtime: Arc<tokio::runtime::Runtime>,
    active: ActiveAccount,
    pending_mfa: PendingMfaState,
) {
    let weak = window.as_weak();
    window.on_login(move |instance, identifier, password| {
        if instance.trim().is_empty() || identifier.trim().is_empty() || password.is_empty() {
            if let Some(window) = weak.upgrade() {
                window.set_error_message("Enter your instance, username, and password.".into());
            }
            return;
        }
        if let Some(window) = weak.upgrade() {
            window.set_busy(true);
            window.set_error_message(SharedString::default());
            window.set_auth_notice(SharedString::default());
        }
        let weak = weak.clone();
        let active = active.clone();
        let pending_mfa = pending_mfa.clone();
        let instance = instance.to_string();
        let identifier = identifier.to_string();
        let password = password.to_string();
        runtime.spawn(async move {
            let (event_tx, event_rx) = mpsc::unbounded_channel();
            let result = AccountRuntime::connect(
                &instance,
                &identifier,
                &password,
                desktop_device_name(),
                event_tx,
            )
            .await;
            match result {
                Ok(account) => {
                    remember_account(&account).await;
                    *active.write().await = Some(account.clone());
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let event_weak = weak.clone();
                    tokio::spawn(consume_account_events(account, event_rx, event_weak));
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_busy(false);
                        window.set_authenticated(true);
                        window.set_error_message(SharedString::default());
                        apply_snapshot(&window, snapshot);
                    });
                }
                Err(kaede_app::AccountError::MfaRequired(ticket)) => {
                    *pending_mfa.write().await = Some(PendingMfa {
                        instance,
                        identifier,
                        ticket,
                    });
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_busy(false);
                        window.set_mfa_required(true);
                        window.set_error_message(SharedString::default());
                    });
                }
                Err(error) => {
                    show_account_error(&weak, error.to_string());
                }
            }
        });
    });
}

fn install_account_onboarding(window: &AppWindow, runtime: Arc<tokio::runtime::Runtime>) {
    let weak = window.as_weak();
    let register_runtime = runtime.clone();
    window.on_register_account(move |instance, username, email, password| {
        if instance.trim().is_empty() || username.trim().is_empty() || password.len() < 12 {
            if let Some(window) = weak.upgrade() {
                window.set_error_message(
                    "Enter an instance, username, and a password of at least 12 characters.".into(),
                );
            }
            return;
        }
        if let Some(window) = weak.upgrade() {
            window.set_busy(true);
            window.set_error_message(SharedString::default());
            window.set_auth_notice(SharedString::default());
        }
        let weak = weak.clone();
        let instance = instance.to_string();
        let username = username.to_string();
        let email = optional_text(&email);
        let password = password.to_string();
        register_runtime.spawn(async move {
            match AccountRuntime::register(
                &instance,
                &username,
                email.as_deref(),
                &password,
            )
            .await
            {
                Ok(result) => {
                    let notice = if result.email_verification_required {
                        "Account created. Check your email, then use the Verify tab before signing in."
                    } else {
                        "Account created. You can return to sign in now."
                    };
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_busy(false);
                        window.set_auth_notice(notice.into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let recovery_runtime = runtime.clone();
    window.on_forgot_password(move |instance, email| {
        if instance.trim().is_empty() || email.trim().is_empty() {
            if let Some(window) = weak.upgrade() {
                window.set_error_message("Enter your instance and email address.".into());
            }
            return;
        }
        set_auth_busy(&weak);
        let weak = weak.clone();
        let instance = instance.to_string();
        let email = email.to_string();
        recovery_runtime.spawn(async move {
            match AccountRuntime::request_password_reset(&instance, &email).await {
                Ok(_) => auth_notice(
                    &weak,
                    "If that address belongs to an account, the home instance sent a reset email.",
                ),
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let verify_runtime = runtime.clone();
    window.on_verify_email(move |instance, token| {
        if instance.trim().is_empty() || token.trim().is_empty() {
            if let Some(window) = weak.upgrade() {
                window.set_error_message("Enter your instance and verification token.".into());
            }
            return;
        }
        set_auth_busy(&weak);
        let weak = weak.clone();
        let instance = instance.to_string();
        let token = token.to_string();
        verify_runtime.spawn(async move {
            match AccountRuntime::verify_email(&instance, &token).await {
                Ok(_) => auth_notice(&weak, "Email verified. You can sign in now."),
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    window.on_reset_password(move |instance, token, password| {
        if instance.trim().is_empty() || token.trim().is_empty() || password.len() < 12 {
            if let Some(window) = weak.upgrade() {
                window.set_error_message(
                    "Enter your instance, reset token, and a password of at least 12 characters."
                        .into(),
                );
            }
            return;
        }
        set_auth_busy(&weak);
        let weak = weak.clone();
        let instance = instance.to_string();
        let token = token.to_string();
        let password = password.to_string();
        runtime.spawn(async move {
            match AccountRuntime::reset_password(&instance, &token, &password).await {
                Ok(_) => auth_notice(&weak, "Password reset. You can sign in now."),
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });
}

fn set_auth_busy(weak: &slint::Weak<AppWindow>) {
    if let Some(window) = weak.upgrade() {
        window.set_busy(true);
        window.set_error_message(SharedString::default());
        window.set_auth_notice(SharedString::default());
    }
}

fn auth_notice(weak: &slint::Weak<AppWindow>, message: &'static str) {
    let _ = weak.upgrade_in_event_loop(move |window| {
        window.set_busy(false);
        window.set_auth_notice(message.into());
    });
}

fn restore_last_account(
    window: &AppWindow,
    runtime: Arc<tokio::runtime::Runtime>,
    active: ActiveAccount,
) {
    let weak = window.as_weak();
    runtime.spawn(async move {
        let Ok(paths) = PlatformPaths::discover() else {
            return;
        };
        let Ok(registry) = AccountRegistry::load(&paths).await else {
            return;
        };
        let Some(known) = registry.most_recent().cloned() else {
            return;
        };
        let _ = weak.upgrade_in_event_loop(|window| {
            window.set_busy(true);
            window.set_error_message(SharedString::default());
        });
        let (event_tx, event_rx) = mpsc::unbounded_channel();
        match AccountRuntime::restore(&known.instance, &known.account_key, event_tx).await {
            Ok(Some(account)) => {
                *active.write().await = Some(account.clone());
                let snapshot = ui_snapshot(&*account.state.read().await);
                let event_weak = weak.clone();
                tokio::spawn(consume_account_events(account, event_rx, event_weak));
                let _ = weak.upgrade_in_event_loop(move |window| {
                    window.set_busy(false);
                    window.set_authenticated(true);
                    apply_snapshot(&window, snapshot);
                });
            }
            Ok(None) => {
                let _ = weak.upgrade_in_event_loop(|window| window.set_busy(false));
            }
            Err(error) => show_account_error(&weak, error.to_string()),
        }
    });
}

fn install_account_management(
    window: &AppWindow,
    runtime: Arc<tokio::runtime::Runtime>,
    active: ActiveAccount,
) {
    refresh_known_accounts(window, runtime.clone());

    let weak = window.as_weak();
    let restore_runtime = runtime.clone();
    let restore_active = active.clone();
    window.on_restore_account(move |instance, account_key| {
        if let Some(window) = weak.upgrade() {
            window.set_busy(true);
            window.set_error_message(SharedString::default());
        }
        let weak = weak.clone();
        let active = restore_active.clone();
        let instance = instance.to_string();
        let account_key = account_key.to_string();
        restore_runtime.spawn(async move {
            let (event_tx, event_rx) = mpsc::unbounded_channel();
            match AccountRuntime::restore(&instance, &account_key, event_tx).await {
                Ok(Some(account)) => {
                    remember_account(&account).await;
                    *active.write().await = Some(account.clone());
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let event_weak = weak.clone();
                    tokio::spawn(consume_account_events(account, event_rx, event_weak));
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_busy(false);
                        window.set_authenticated(true);
                        apply_snapshot(&window, snapshot);
                    });
                }
                Ok(None) => show_account_error(
                    &weak,
                    "That saved session has expired. Sign in again to continue.".to_owned(),
                ),
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let forget_runtime = runtime.clone();
    window.on_forget_account(move |account_key| {
        let weak = weak.clone();
        let account_key = account_key.to_string();
        forget_runtime.spawn(async move {
            if let Err(error) = SystemCredentialVault.delete(&account_key).await {
                tracing::warn!(%error, "could not delete saved account credentials");
            }
            if let Ok(paths) = PlatformPaths::discover() {
                let mut registry = AccountRegistry::load(&paths).await.unwrap_or_default();
                registry.forget(&account_key);
                if let Err(error) = registry.save(&paths).await {
                    tracing::warn!(%error, "could not update saved account index");
                }
                apply_known_accounts(&weak, &registry.accounts);
            }
        });
    });

    let weak = window.as_weak();
    let switch_runtime = runtime.clone();
    let switch_active = active.clone();
    window.on_switch_account(move || {
        let weak = weak.clone();
        let active = switch_active.clone();
        switch_runtime.spawn(async move {
            #[cfg(feature = "native-voice")]
            leave_active_voice().await;
            if let Some(account) = active.write().await.take() {
                account.shutdown().await;
            }
            let _ = weak.upgrade_in_event_loop(|window| {
                window.set_authenticated(false);
                window.set_active_overlay(SharedString::default());
                window.set_selected_guild(SharedString::default());
                window.set_selected_channel(SharedString::default());
                window.set_error_message(SharedString::default());
            });
        });
    });

    let weak = window.as_weak();
    let sessions_runtime = runtime.clone();
    let sessions_active = active.clone();
    window.on_refresh_sessions(move || {
        let weak = weak.clone();
        let active = sessions_active.clone();
        sessions_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.session.sessions().await {
                Ok(sessions) => apply_sessions(&weak, sessions),
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let revoke_runtime = runtime;
    let revoke_active = active;
    window.on_revoke_session(move |session_id| {
        let weak = weak.clone();
        let active = revoke_active.clone();
        let session_id = session_id.to_string();
        revoke_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.session.revoke_session(&session_id).await {
                Ok(()) => match account.session.sessions().await {
                    Ok(sessions) => {
                        apply_sessions(&weak, sessions);
                        let _ = weak.upgrade_in_event_loop(|window| {
                            window.set_security_result("Device session revoked.".into());
                        });
                    }
                    Err(error) => show_account_error(&weak, error.to_string()),
                },
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });
}

fn refresh_known_accounts(window: &AppWindow, runtime: Arc<tokio::runtime::Runtime>) {
    let weak = window.as_weak();
    runtime.spawn(async move {
        let Ok(paths) = PlatformPaths::discover() else {
            return;
        };
        let Ok(registry) = AccountRegistry::load(&paths).await else {
            return;
        };
        apply_known_accounts(&weak, &registry.accounts);
    });
}

fn apply_known_accounts(weak: &slint::Weak<AppWindow>, accounts: &[KnownAccount]) {
    let accounts = accounts.to_vec();
    let _ = weak.upgrade_in_event_loop(move |window| {
        let model = ModelRc::from(Rc::new(VecModel::from(
            accounts
                .into_iter()
                .map(|account| AccountItem {
                    instance: account.instance.into(),
                    account_key: account.account_key.into(),
                    label: account.label.into(),
                })
                .collect::<Vec<_>>(),
        )));
        window.set_known_accounts(model);
    });
}

fn apply_sessions(weak: &slint::Weak<AppWindow>, sessions: Vec<kaede_auth::SessionSummary>) {
    let rows = sessions
        .into_iter()
        .map(|session| {
            let title = session
                .device_name
                .filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| "Kaede Desktop".to_owned());
            let mut parts = Vec::new();
            if let Some(ip) = session.ip_address.filter(|value| !value.is_empty()) {
                parts.push(ip);
            }
            parts.push(format!(
                "Last active {}",
                compact_timestamp(&session.last_used_at)
            ));
            parts.push(format!(
                "Expires {}",
                compact_timestamp(&session.expires_at)
            ));
            (session.id, title, parts.join(" · "), session.current)
        })
        .collect::<Vec<_>>();
    let _ = weak.upgrade_in_event_loop(move |window| {
        let model = ModelRc::from(Rc::new(VecModel::from(
            rows.into_iter()
                .map(|(id, title, detail, current)| SessionItem {
                    id: id.into(),
                    title: title.into(),
                    detail: detail.into(),
                    current,
                })
                .collect::<Vec<_>>(),
        )));
        window.set_sessions(model);
    });
}

fn compact_timestamp(value: &str) -> String {
    value
        .split_once('.')
        .map_or_else(|| value.to_owned(), |(prefix, _)| format!("{prefix}Z"))
        .replace('T', " ")
}

fn install_completions(
    window: &AppWindow,
    runtime: Arc<tokio::runtime::Runtime>,
    active: ActiveAccount,
) {
    let typing_sent = Arc::new(RwLock::new(HashMap::<EntityRef, std::time::Instant>::new()));
    let weak = window.as_weak();
    let completion_runtime = runtime.clone();
    let completion_active = active;
    window.on_update_completions(move |draft| {
        let query = kaede_core::markup::completion_at(draft.as_str(), draft.len());
        let channel = weak
            .upgrade()
            .and_then(|window| window.get_selected_channel().as_str().parse().ok());
        let should_publish_typing = !draft.trim().is_empty();
        let weak = weak.clone();
        let active = completion_active.clone();
        let typing_sent = typing_sent.clone();
        completion_runtime.spawn(async move {
            if should_publish_typing
                && let (Some(channel), Some(account)) = (channel, active.read().await.clone())
            {
                let mut sent = typing_sent.write().await;
                let due = sent
                    .get(&channel)
                    .is_none_or(|last| last.elapsed() >= std::time::Duration::from_secs(5));
                if due {
                    sent.insert(channel.clone(), std::time::Instant::now());
                    drop(sent);
                    if let Err(error) = account.notify_typing(&channel).await {
                        tracing::debug!(%error, %channel, "typing indicator was not published");
                    }
                }
            }
            let Some(query) = query else {
                set_completion_rows(&weak, Vec::new());
                return;
            };
            let Some(account) = active.read().await.clone() else {
                set_completion_rows(&weak, Vec::new());
                return;
            };
            let state = account.state.read().await;
            let needle = query.query.to_lowercase();
            let mut rows = match query.marker {
                kaede_core::markup::CompletionMarker::User => {
                    let mut users = state
                        .users
                        .values()
                        .filter(|user| {
                            let label = user.label().to_lowercase();
                            label.contains(&needle)
                                || user.username.to_lowercase().contains(&needle)
                        })
                        .map(|user| UiCompletion {
                            value: format!("<@{}>", user.key()),
                            label: format!("@{}", user.label()),
                            detail: user.handle.clone(),
                            kind: "user".to_owned(),
                        })
                        .collect::<Vec<_>>();
                    users.extend(
                        state
                            .roles
                            .values()
                            .filter(|role| {
                                role.mentionable && role.name.to_lowercase().contains(&needle)
                            })
                            .map(|role| UiCompletion {
                                value: format!("<@&{}>", role.key()),
                                label: format!("@{}", role.name),
                                detail: "Role".to_owned(),
                                kind: "role".to_owned(),
                            }),
                    );
                    users
                }
                kaede_core::markup::CompletionMarker::Channel => state
                    .channels
                    .values()
                    .filter_map(|channel| {
                        let name = channel.name.as_ref()?;
                        name.to_lowercase().contains(&needle).then(|| UiCompletion {
                            value: format!("#{name}"),
                            label: format!("#{name}"),
                            detail: channel.key().to_string(),
                            kind: "channel".to_owned(),
                        })
                    })
                    .collect(),
                kaede_core::markup::CompletionMarker::Emoji => {
                    let mut emojis = standard_emoji_completions(&needle);
                    emojis.extend(
                        state
                            .emojis
                            .values()
                            .filter(|emoji| emoji.name.to_lowercase().contains(&needle))
                            .map(|emoji| UiCompletion {
                                value: format!(
                                    "<{}:{}:{}@{}>",
                                    if emoji.animated { "a" } else { "" },
                                    emoji.name,
                                    emoji.id,
                                    emoji.origin_domain
                                ),
                                label: format!(":{}:", emoji.name),
                                detail: "Custom emoji".to_owned(),
                                kind: "emoji".to_owned(),
                            }),
                    );
                    emojis
                }
            };
            rows.sort_by(|left, right| left.label.cmp(&right.label));
            rows.truncate(12);
            drop(state);
            set_completion_rows(&weak, rows);
        });
    });

    let weak = window.as_weak();
    window.on_choose_completion(move |value| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let draft = window.get_draft().to_string();
        let Some(query) = kaede_core::markup::completion_at(&draft, draft.len()) else {
            return;
        };
        let mut updated = String::with_capacity(draft.len() + value.len() + 1);
        updated.push_str(&draft[..query.start]);
        updated.push_str(value.as_str());
        updated.push(' ');
        updated.push_str(&draft[query.end..]);
        window.set_draft(updated.into());
        window.set_completions(ModelRc::from(
            Rc::new(VecModel::<CompletionItem>::default()),
        ));
    });
}

struct UiCompletion {
    value: String,
    label: String,
    detail: String,
    kind: String,
}

fn standard_emoji_completions(needle: &str) -> Vec<UiCompletion> {
    emoji::search(needle, 12)
        .into_iter()
        .map(|emoji| UiCompletion {
            value: emoji.e.clone(),
            label: format!(":{}:", emoji.s),
            detail: "Unicode emoji".to_owned(),
            kind: "emoji".to_owned(),
        })
        .collect()
}

fn set_completion_rows(weak: &slint::Weak<AppWindow>, rows: Vec<UiCompletion>) {
    let _ = weak.upgrade_in_event_loop(move |window| {
        window.set_completions(ModelRc::from(Rc::new(VecModel::from(
            rows.into_iter()
                .map(|row| CompletionItem {
                    value: row.value.into(),
                    label: row.label.into(),
                    detail: row.detail.into(),
                    kind: row.kind.into(),
                })
                .collect::<Vec<_>>(),
        ))));
    });
}

async fn remember_account(account: &AccountRuntime) {
    let Ok(paths) = PlatformPaths::discover() else {
        return;
    };
    let mut registry = AccountRegistry::load(&paths).await.unwrap_or_default();
    let label = account
        .state
        .read()
        .await
        .current_user
        .as_ref()
        .map_or_else(
            || account.account_key().to_owned(),
            |user| user.handle.clone(),
        );
    registry.remember(KnownAccount {
        instance: account.instance().to_string(),
        account_key: account.account_key().to_owned(),
        label,
        last_used_unix_ms: chrono::Utc::now().timestamp_millis(),
    });
    if let Err(error) = registry.save(&paths).await {
        tracing::warn!(%error, "could not persist the non-secret account index");
    }
}

fn install_mfa(
    window: &AppWindow,
    runtime: Arc<tokio::runtime::Runtime>,
    active: ActiveAccount,
    pending_mfa: PendingMfaState,
) {
    let weak = window.as_weak();
    let pending_for_submit = pending_mfa.clone();
    window.on_submit_mfa(move |code| {
        if code.trim().is_empty() {
            return;
        }
        if let Some(window) = weak.upgrade() {
            window.set_busy(true);
            window.set_error_message(SharedString::default());
        }
        let weak = weak.clone();
        let active = active.clone();
        let pending = pending_for_submit.clone();
        let code = code.to_string();
        runtime.spawn(async move {
            let Some(challenge) = pending.write().await.take() else {
                let _ = weak.upgrade_in_event_loop(|window| {
                    window.set_busy(false);
                    window.set_mfa_required(false);
                    window.set_error_message(
                        "The sign-in attempt expired. Please sign in again.".into(),
                    );
                });
                return;
            };
            let (event_tx, event_rx) = mpsc::unbounded_channel();
            match AccountRuntime::connect_mfa(
                &challenge.instance,
                &challenge.identifier,
                &challenge.ticket,
                &code,
                desktop_device_name(),
                event_tx,
            )
            .await
            {
                Ok(account) => {
                    remember_account(&account).await;
                    *active.write().await = Some(account.clone());
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let event_weak = weak.clone();
                    tokio::spawn(consume_account_events(account, event_rx, event_weak));
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_busy(false);
                        window.set_mfa_required(false);
                        window.set_authenticated(true);
                        window.set_error_message(SharedString::default());
                        apply_snapshot(&window, snapshot);
                    });
                }
                Err(error) => {
                    *pending.write().await = Some(challenge);
                    show_account_error(&weak, error.to_string());
                }
            }
        });
    });

    let weak = window.as_weak();
    window.on_cancel_mfa(move || {
        if let Ok(mut pending) = pending_mfa.try_write() {
            *pending = None;
        }
        if let Some(window) = weak.upgrade() {
            window.set_mfa_required(false);
            window.set_busy(false);
            window.set_error_message(SharedString::default());
        }
    });
}

fn show_account_error(weak: &slint::Weak<AppWindow>, error: String) {
    let message = friendly_error(&error);
    let _ = weak.upgrade_in_event_loop(move |window| {
        window.set_busy(false);
        window.set_channel_loading(false);
        window.set_error_message(message.into());
    });
}

fn install_navigation(
    window: &AppWindow,
    runtime: Arc<tokio::runtime::Runtime>,
    active: ActiveAccount,
    state: NavigationState,
) {
    let NavigationState {
        attachments,
        slow_mode_deadlines,
        overwrite_masks,
        gif_favorites,
        paths,
    } = state;
    let weak = window.as_weak();
    let guild_runtime = runtime.clone();
    let guild_active = active.clone();
    window.on_guild_selected(move |value| {
        let Ok(guild) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        if let Some(window) = weak.upgrade() {
            window.set_channel_loading(true);
            window.set_error_message(SharedString::default());
        }
        let weak = weak.clone();
        let active = guild_active.clone();
        guild_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.load_guild(&guild).await {
                Ok(()) => {
                    let selected_channel = match hydrate_guild_landing(&account, &guild).await {
                        Ok(channel) => channel,
                        Err(error) => {
                            show_account_error(&weak, error.to_string());
                            return;
                        }
                    };
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_selected_channel(
                            selected_channel.map_or_else(SharedString::default, |value| {
                                value.to_string().into()
                            }),
                        );
                        window.set_channel_loading(false);
                        apply_snapshot(&window, snapshot);
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let load_runtime = runtime.clone();
    let load_active = active.clone();
    let load_deadlines = slow_mode_deadlines.clone();
    window.on_channel_selected(move |value| {
        let Ok(channel) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        if let Some(window) = weak.upgrade() {
            window.set_channel_loading(true);
            window.set_error_message(SharedString::default());
            window.set_messages(ModelRc::from(Rc::new(VecModel::<MessageItem>::default())));
            window.set_replying_message(SharedString::default());
            window.set_replying_author(SharedString::default());
            window.set_reply_can_notify(false);
            window.set_reply_notify(true);
            window.set_editing_message(SharedString::default());
            window.set_new_marker_message(SharedString::default());
            window.set_history_loading(false);
            window.set_history_complete(false);
            window.set_history_truncated(false);
            window.set_history_remote_available(false);
            window.set_history_page_warning(SharedString::default());
        }
        let weak = weak.clone();
        let active = load_active.clone();
        let deadlines = load_deadlines.clone();
        load_runtime.spawn(async move {
            let account = active.read().await.clone();
            if let Some(account) = account {
                let kind = account
                    .state
                    .read()
                    .await
                    .channels
                    .get(&channel)
                    .map(|value| value.kind);
                let result = match kind {
                    Some(ChannelKind::Voice) => account.refresh_voice_occupancy(&channel).await,
                    _ => account.load_channel(&channel).await.map(|_| ()),
                };
                if matches!(kind, Some(ChannelKind::DirectMessage))
                    && let Ok(active_call) = account.service.active_call(&channel).await
                    && let Some(call) = active_call.call
                {
                    account.state.write().await.calls.insert(call.key(), call);
                }
                match result {
                    Ok(()) => {
                        let remaining = deadlines
                            .read()
                            .await
                            .get(&channel)
                            .map_or(0, |deadline| {
                                deadline
                                    .checked_duration_since(std::time::Instant::now())
                                    .map_or(0, |duration| duration.as_secs().saturating_add(1) as i32)
                            });
                        // Capture the "New messages" divider position before
                        // acknowledging, exactly like the web client captures
                        // the read state at load time.
                        let (newest, new_marker) = {
                            let state = account.state.read().await;
                            let newest = state
                                .message_order
                                .get(&channel)
                                .and_then(|order| order.back())
                                .cloned();
                            let new_marker = state
                                .read_states
                                .get(&channel)
                                .filter(|read| read.unread || read.mention_count > 0)
                                .and_then(|read| read.last_read_message_id)
                                .and_then(|last_read| {
                                    state.message_order.get(&channel).and_then(|order| {
                                        order
                                            .iter()
                                            .find(|message| message.id > last_read)
                                            .map(ToString::to_string)
                                    })
                                })
                                .unwrap_or_default();
                            (newest, new_marker)
                        };
                        if let Err(error) = account
                            .acknowledge_channel(&channel, newest.as_ref())
                            .await
                        {
                            tracing::debug!(%error, %channel, "read acknowledgement was not persisted");
                        }
                        let snapshot = ui_snapshot(&*account.state.read().await);
                        let _ = weak
                            .upgrade_in_event_loop(move |window| {
                                window.set_slow_mode_remaining(remaining);
                                window.set_channel_loading(false);
                                window.set_new_marker_message(new_marker.into());
                                apply_snapshot(&window, snapshot);
                            });
                        if !matches!(kind, Some(ChannelKind::Voice)) {
                            account.refresh_link_previews(&channel).await;
                            account.refresh_message_media(&channel).await;
                            refresh_gif_stills(account.clone(), weak.clone()).await;
                        }
                    }
                    Err(error) => {
                        let message = friendly_error(&error.to_string());
                        let _ = weak.upgrade_in_event_loop(move |window| {
                            window.set_channel_loading(false);
                            window.set_error_message(message.into())
                        });
                    }
                }
            }
        });
    });

    let weak = window.as_weak();
    let send_runtime = runtime.clone();
    let send_active = active.clone();
    let send_attachments = attachments.clone();
    let send_deadlines = slow_mode_deadlines;
    window.on_send_message(move |content| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>() else {
            return;
        };
        let editing = window.get_editing_message().to_string();
        let replying = window.get_replying_message().to_string();
        let reply_notify = window.get_reply_notify();
        let weak = weak.clone();
        let active = send_active.clone();
        let attachments = send_attachments.clone();
        let deadlines = send_deadlines.clone();
        send_runtime.spawn(async move {
            let account = active.read().await.clone();
            if let Some(account) = account {
                let result = if editing.is_empty() {
                    let selected = attachments
                        .read()
                        .await
                        .get(&channel)
                        .cloned()
                        .unwrap_or_default();
                    let reference = replying.parse::<EntityRef>().ok();
                    let mentions = if reply_notify {
                        let state = account.state.read().await;
                        reference
                            .as_ref()
                            .and_then(|reference| state.messages.get(reference))
                            .and_then(|message| message.author.as_ref())
                            .filter(|author| {
                                state
                                    .current_user
                                    .as_ref()
                                    .is_none_or(|current| author.key() != current.key())
                            })
                            .map_or_else(Vec::new, |author| vec![author.key()])
                    } else {
                        Vec::new()
                    };
                    account
                        .send_message_with_context(
                            &channel,
                            content.to_string(),
                            selected.iter().map(|item| item.id).collect(),
                            mentions,
                            reference,
                        )
                        .await
                } else if let Ok(message) = editing.parse::<EntityRef>() {
                    account
                        .edit_message(&channel, &message, content.as_str())
                        .await
                        .map(|_| ())
                } else {
                    return;
                };
                if let Err(error) = result {
                    let message = friendly_error(&error.to_string());
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_error_message(message.into())
                    });
                } else {
                    if editing.is_empty() {
                        attachments.write().await.remove(&channel);
                    }
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_editing_message(SharedString::default());
                        window.set_pending_attachments(SharedString::default());
                        window.set_replying_message(SharedString::default());
                        window.set_replying_author(SharedString::default());
                        window.set_reply_can_notify(false);
                        window.set_reply_notify(true);
                    });
                    if editing.is_empty() {
                        let seconds = account
                            .state
                            .read()
                            .await
                            .channels
                            .get(&channel)
                            .map_or(0, |channel| channel.rate_limit_per_user);
                        if seconds > 0 {
                            deadlines.write().await.insert(
                                channel.clone(),
                                std::time::Instant::now()
                                    + std::time::Duration::from_secs(u64::from(seconds)),
                            );
                            for remaining in (1..=seconds).rev() {
                                let _ = weak.upgrade_in_event_loop(move |window| {
                                    window.set_slow_mode_remaining(remaining as i32)
                                });
                                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                            }
                            deadlines.write().await.remove(&channel);
                            let _ = weak
                                .upgrade_in_event_loop(|window| window.set_slow_mode_remaining(0));
                        }
                    }
                }
            }
        });
    });

    let weak = window.as_weak();
    let older_runtime = runtime.clone();
    let older_active = active.clone();
    window.on_load_older(move || {
        let Some(window) = weak.upgrade() else {
            return;
        };
        if window.get_history_loading() || window.get_history_complete() {
            return;
        }
        let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>() else {
            return;
        };
        window.set_history_loading(true);
        let weak = weak.clone();
        let active = older_active.clone();
        older_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                let _ = weak.upgrade_in_event_loop(|window| window.set_history_loading(false));
                return;
            };
            match account.load_older_messages(&channel).await {
                Ok(batch) => {
                    let page_error = batch
                        .last()
                        .and_then(|message| message.history_page_error_code.as_deref());
                    let complete = page_error.is_none()
                        && (batch.len() < 100
                            || batch
                                .last()
                                .is_some_and(|message| message.history_page_complete));
                    let page_warning = batch.last().and_then(|message| {
                        (message.history_page_error_code.as_deref()
                            == Some("FEDERATED_DM_HISTORY_UNAVAILABLE"))
                        .then(|| {
                            let seconds = message
                                .history_page_retry_after_ms
                                .unwrap_or(2_000)
                                .div_ceil(1_000)
                                .max(1);
                            format!(
                                "Older messages are temporarily unavailable from the home instance. Recent cached messages remain available; try again in about {seconds}s."
                            )
                        })
                    });
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_history_loading(false);
                        window.set_history_page_warning(
                            page_warning.unwrap_or_default().into(),
                        );
                        if complete {
                            window.set_history_complete(true);
                        }
                    });
                }
                Err(error) => {
                    let _ = weak.upgrade_in_event_loop(|window| window.set_history_loading(false));
                    show_account_error(&weak, error.to_string());
                }
            }
        });
    });

    let weak = window.as_weak();
    let category_runtime = runtime.clone();
    let category_active = active.clone();
    window.on_toggle_category(move |id| {
        if let Ok(mut set) = collapsed_categories().lock() {
            let id = id.to_string();
            if !set.remove(&id) {
                set.insert(id);
            }
        }
        let weak = weak.clone();
        let active = category_active.clone();
        category_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let snapshot = ui_snapshot(&*account.state.read().await);
            let _ = weak.upgrade_in_event_loop(move |window| apply_snapshot(&window, snapshot));
        });
    });

    let weak = window.as_weak();
    window.on_cancel_edit(move || {
        if let Some(window) = weak.upgrade() {
            window.set_editing_message(SharedString::default());
            window.set_draft(SharedString::default());
        }
    });

    let copy_handle_active = active.clone();
    window.on_copy_user_handle(move |value| {
        let Ok(user_ref) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(account) = copy_handle_active.try_read() else {
            return;
        };
        let Some(account) = account.as_ref() else {
            return;
        };
        let Ok(state) = account.state.try_read() else {
            return;
        };
        if let Some(user) = state.users.get(&user_ref) {
            let handle = if user.handle.is_empty() {
                format!("@{}@{}", user.username, user.origin_domain)
            } else {
                user.handle.clone()
            };
            copy_to_clipboard(&handle);
        }
    });

    let weak = window.as_weak();
    window.on_search_emojis(move |query| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        set_emoji_grid(&window, query.as_str());
    });

    let weak = window.as_weak();
    let pins_runtime = runtime.clone();
    let pins_active = active.clone();
    window.on_open_pins(move || {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>() else {
            return;
        };
        window.set_pinned_messages(ModelRc::from(Rc::new(VecModel::default())));
        window.set_active_overlay("pins".into());
        let weak = weak.clone();
        let active = pins_active.clone();
        pins_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.service.pins(&channel).await {
                Ok(messages) => {
                    let state = account.state.read().await;
                    let current_user = state.current_user.as_ref().map(kaede_core::User::key);
                    let rows = messages
                        .into_iter()
                        .map(|message| message_to_ui(&state, message, current_user.as_ref()))
                        .collect::<Vec<_>>();
                    drop(state);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_pinned_messages(ModelRc::from(Rc::new(VecModel::from(
                            rows.into_iter()
                                .map(|item| message_item(item, false))
                                .collect::<Vec<_>>(),
                        ))));
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let retry_runtime = runtime.clone();
    let retry_active = active.clone();
    window.on_retry_message(move |value| {
        let Some(nonce) = value.as_str().strip_prefix("pending:").map(str::to_owned) else {
            return;
        };
        let weak = weak.clone();
        let active = retry_active.clone();
        retry_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            if let Err(error) = account.retry_message(&nonce).await {
                show_account_error(&weak, error.to_string());
            }
        });
    });

    let weak = window.as_weak();
    let media_runtime = runtime.clone();
    let media_active = active.clone();
    window.on_open_message_media(move |value| {
        let Ok(message) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = media_active.clone();
        media_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.message_media(&message).await {
                Ok(asset) => {
                    if let Err(error) = kaede_media_viewer::spawn(&asset.path, &asset.content_type)
                    {
                        show_account_error(&weak, error.to_string());
                    }
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let reply_active = active.clone();
    window.on_reply_message(move |value| {
        let Ok(message_ref) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(account) = reply_active.try_read() else {
            return;
        };
        let Some(account) = account.as_ref() else {
            return;
        };
        let Ok(state) = account.state.try_read() else {
            return;
        };
        let Some(message) = state.messages.get(&message_ref) else {
            return;
        };
        let author = message.author.as_ref().map_or_else(
            || "Unknown author".to_owned(),
            |author| author.label().to_owned(),
        );
        let can_notify = message.author.as_ref().is_some_and(|author| {
            let is_self = state
                .current_user
                .as_ref()
                .is_some_and(|current| current.key() == author.key());
            let is_guild = state
                .channels
                .get(&message.channel_key())
                .is_some_and(|channel| channel.kind != ChannelKind::DirectMessage);
            is_guild && !is_self
        });
        window.set_replying_message(value);
        window.set_replying_author(author.into());
        window.set_reply_can_notify(can_notify);
        window.set_reply_notify(can_notify);
        window.set_editing_message(SharedString::default());
    });

    let weak = window.as_weak();
    window.on_cancel_reply(move || {
        if let Some(window) = weak.upgrade() {
            window.set_replying_message(SharedString::default());
            window.set_replying_author(SharedString::default());
            window.set_reply_can_notify(false);
            window.set_reply_notify(true);
        }
    });

    let weak = window.as_weak();
    let edit_active = active.clone();
    window.on_edit_message(move |value| {
        let Ok(message_ref) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(account) = edit_active.try_read() else {
            return;
        };
        let Some(account) = account.as_ref() else {
            return;
        };
        let Ok(state) = account.state.try_read() else {
            return;
        };
        let Some(message) = state.messages.get(&message_ref) else {
            return;
        };
        window.set_draft(message.content.clone().unwrap_or_default().into());
        window.set_editing_message(value);
        window.set_replying_message(SharedString::default());
        window.set_replying_author(SharedString::default());
        window.set_reply_can_notify(false);
    });

    let weak = window.as_weak();
    let edit_last_active = active.clone();
    window.on_edit_last_message(move || {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(account) = edit_last_active.try_read() else {
            return;
        };
        let Some(account) = account.as_ref() else {
            return;
        };
        let Ok(state) = account.state.try_read() else {
            return;
        };
        let Some(current) = state.current_user.as_ref().map(kaede_core::User::key) else {
            return;
        };
        let Some(message) = state
            .channel_messages(&channel)
            .into_iter()
            .rev()
            .find(|message| {
                message.deleted_at.is_none()
                    && message
                        .author
                        .as_ref()
                        .is_some_and(|author| author.key() == current)
            })
        else {
            return;
        };
        window.set_draft(message.content.clone().unwrap_or_default().into());
        window.set_editing_message(message.key().to_string().into());
        window.set_replying_message(SharedString::default());
        window.set_replying_author(SharedString::default());
        window.set_reply_can_notify(false);
    });

    let weak = window.as_weak();
    let delete_runtime = runtime.clone();
    let delete_active = active.clone();
    window.on_delete_message(move |value| {
        let Ok(message) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = delete_active.clone();
        delete_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            if let Err(error) = account.delete_message(&channel, &message).await {
                show_account_error(&weak, error.to_string());
            }
        });
    });

    let weak = window.as_weak();
    let react_runtime = runtime.clone();
    let react_active = active.clone();
    window.on_react_message(move |value, emoji| {
        let Ok(message) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = react_active.clone();
        react_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            if let Err(error) = account
                .set_reaction(&channel, &message, emoji.as_str(), true)
                .await
            {
                show_account_error(&weak, error.to_string());
            }
        });
    });

    let weak = window.as_weak();
    let toggle_pin_runtime = runtime.clone();
    let toggle_pin_active = active.clone();
    window.on_pin_message(move |value| {
        let Ok(message) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = toggle_pin_active.clone();
        toggle_pin_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            if let Err(error) = account.toggle_pinned(&channel, &message).await {
                show_account_error(&weak, error.to_string());
            }
        });
    });

    let copy_active = active.clone();
    window.on_copy_message(move |value| {
        let Ok(message) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(account) = copy_active.try_read() else {
            return;
        };
        let Some(account) = account.as_ref() else {
            return;
        };
        let Ok(state) = account.state.try_read() else {
            return;
        };
        if let Some(content) = state
            .messages
            .get(&message)
            .and_then(|message| message.content.as_ref())
        {
            copy_to_clipboard(content);
        }
    });

    let weak = window.as_weak();
    let gif_runtime = runtime.clone();
    let gif_active = active.clone();
    let gif_saved = gif_favorites.clone();
    let gif_page = Arc::new(RwLock::new((String::new(), 1u16)));
    let search_page = gif_page.clone();
    window.on_search_gifs(move |query| {
        let weak = weak.clone();
        let active = gif_active.clone();
        let saved = gif_saved.clone();
        let page_state = search_page.clone();
        gif_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            *page_state.write().await = (query.to_string(), 1);
            let saved_items = saved.read().await.items.clone();
            if query.trim().is_empty() && !saved_items.is_empty() {
                let rows = saved_items
                    .into_iter()
                    .map(|item| (item.id, item.title, item.url, true))
                    .collect::<Vec<_>>();
                let targets = rows
                    .iter()
                    .map(|row| (row.2.clone(), row.2.clone()))
                    .collect::<Vec<_>>();
                let _ = weak.upgrade_in_event_loop(move |window| {
                    window.set_gifs(gif_model(rows));
                });
                hydrate_gif_previews(&account, &weak, targets).await;
                return;
            }
            let media = kaede_media::MediaClient::new(account.api.clone());
            match media.gifs(Some(query.as_str()), 1).await {
                Ok(page) => {
                    let mut targets = Vec::new();
                    let rows = page
                        .items
                        .into_iter()
                        .map(|item| {
                            targets.push((item.url.to_string(), item.preview_url.to_string()));
                            let favorite = saved_items
                                .iter()
                                .any(|saved| saved.url == item.url.as_str());
                            (item.id, item.title, item.url.to_string(), favorite)
                        })
                        .collect::<Vec<_>>();
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_gifs(gif_model(rows));
                    });
                    hydrate_gif_previews(&account, &weak, targets).await;
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let more_runtime = runtime.clone();
    let more_active = active.clone();
    let more_saved = gif_favorites.clone();
    window.on_load_more_gifs(move || {
        let weak = weak.clone();
        let active = more_active.clone();
        let saved = more_saved.clone();
        let page_state = gif_page.clone();
        more_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let (query, page) = {
                let mut state = page_state.write().await;
                state.1 = state.1.saturating_add(1);
                state.clone()
            };
            let saved_items = saved.read().await.items.clone();
            let media = kaede_media::MediaClient::new(account.api.clone());
            match media.gifs(Some(query.as_str()), page).await {
                Ok(result) => {
                    let mut targets = Vec::new();
                    let rows = result
                        .items
                        .into_iter()
                        .map(|item| {
                            targets.push((item.url.to_string(), item.preview_url.to_string()));
                            let favorite = saved_items
                                .iter()
                                .any(|saved| saved.url == item.url.as_str());
                            (item.id, item.title, item.url.to_string(), favorite)
                        })
                        .collect::<Vec<_>>();
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        let mut existing = window.get_gifs().iter().collect::<Vec<_>>();
                        for (id, title, url, favorite) in rows {
                            existing.push(GifItem {
                                id: id.into(),
                                title: title.into(),
                                url: url.into(),
                                favorite,
                                has_preview: false,
                                preview: Image::default(),
                            });
                        }
                        window.set_gifs(ModelRc::from(Rc::new(VecModel::from(existing))));
                    });
                    hydrate_gif_previews(&account, &weak, targets).await;
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let favorite_runtime = runtime.clone();
    window.on_favorite_gif(move |id, title, url| {
        if klipy_gif_url(url.as_str()).is_none() {
            return;
        }
        let weak = weak.clone();
        let favorites = gif_favorites.clone();
        let paths = paths.clone();
        favorite_runtime.spawn(async move {
            let mut favorites = favorites.write().await;
            favorites.toggle(GifFavorite {
                id: id.to_string(),
                title: title.to_string(),
                url: url.to_string(),
            });
            if let Err(error) = favorites.save(&paths).await {
                show_account_error(&weak, error.to_string());
                return;
            }
            let saved_urls = favorites
                .items
                .iter()
                .map(|item| item.url.clone())
                .collect::<std::collections::HashSet<_>>();
            let _ = weak.upgrade_in_event_loop(move |window| {
                let updated = window
                    .get_gifs()
                    .iter()
                    .map(|mut item| {
                        item.favorite = saved_urls.contains(item.url.as_str());
                        item
                    })
                    .collect::<Vec<_>>();
                window.set_gifs(ModelRc::from(Rc::new(VecModel::from(updated))));
            });
        });
    });

    let weak = window.as_weak();
    let attach_runtime = runtime.clone();
    let attach_active = active.clone();
    let pending_for_attach = attachments.clone();
    window.on_attach_file(move || {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = attach_active.clone();
        let attachments = pending_for_attach.clone();
        attach_runtime.spawn(async move {
            let Some(file) = rfd::AsyncFileDialog::new()
                .set_title("Attach a file")
                .pick_file()
                .await
            else {
                return;
            };
            let count = attachments.read().await.get(&channel).map_or(0, Vec::len);
            if count >= 10 {
                show_account_error(
                    &weak,
                    "A message can contain at most 10 attachments.".to_owned(),
                );
                return;
            }
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let path = file.path();
            let media = kaede_media::MediaClient::new(account.api.clone());
            let content_type = content_type_for_path(path);
            let _ = weak.upgrade_in_event_loop(|window| {
                window.set_attachment_busy(true);
                window.set_error_message("Uploading attachment…".into());
            });
            match media.upload_attachment(&channel, path, content_type).await {
                Ok(upload) => {
                    let mut pending = attachments.write().await;
                    let files = pending.entry(channel).or_default();
                    files.push(PendingAttachment {
                        id: upload.ticket.id,
                        filename: upload.filename,
                    });
                    let label = files
                        .iter()
                        .map(|item| item.filename.as_str())
                        .collect::<Vec<_>>()
                        .join(", ");
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_attachment_busy(false);
                        window.set_pending_attachments(label.into());
                        window.set_error_message(SharedString::default());
                    });
                }
                Err(error) => {
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_attachment_busy(false);
                    });
                    show_account_error(&weak, error.to_string());
                }
            }
        });
    });

    let weak = window.as_weak();
    let clear_attachments = attachments.clone();
    window.on_clear_attachments(move || {
        let Some(window) = weak.upgrade() else {
            return;
        };
        if let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>()
            && let Ok(mut pending) = clear_attachments.try_write()
        {
            pending.remove(&channel);
        }
        window.set_pending_attachments(SharedString::default());
    });

    let weak = window.as_weak();
    let friend_runtime = runtime.clone();
    let friend_active = active.clone();
    window.on_friend_request(move |handle| {
        let handle = handle.trim().to_owned();
        if handle.is_empty() {
            return;
        }
        let weak = weak.clone();
        let active = friend_active.clone();
        friend_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            if let Err(error) = account.request_friend(&handle).await {
                show_account_error(&weak, error.to_string());
            }
        });
    });

    let weak = window.as_weak();
    let accept_runtime = runtime.clone();
    let accept_active = active.clone();
    window.on_friend_accept(move |value| {
        let Ok(user) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = accept_active.clone();
        accept_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            if let Err(error) = account.accept_friend(&user).await {
                show_account_error(&weak, error.to_string());
            }
        });
    });

    let weak = window.as_weak();
    let remove_runtime = runtime.clone();
    let remove_active = active.clone();
    window.on_friend_remove(move |value| {
        let Ok(user) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = remove_active.clone();
        remove_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            if let Err(error) = account.remove_relationship(&user).await {
                show_account_error(&weak, error.to_string());
            }
        });
    });

    let weak = window.as_weak();
    let message_runtime = runtime.clone();
    let message_active = active.clone();
    window.on_friend_message(move |value| {
        let value = value.to_string();
        let weak = weak.clone();
        let active = message_active.clone();
        message_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let handle = match value.parse::<EntityRef>() {
                Ok(user_ref) => account
                    .state
                    .read()
                    .await
                    .users
                    .get(&user_ref)
                    .map(|user| user.handle.clone())
                    .filter(|candidate| !candidate.is_empty()),
                Err(_) if value.starts_with('@') => Some(value),
                Err(_) => None,
            };
            let Some(handle) = handle else {
                show_account_error(
                    &weak,
                    "The selected profile has no usable handle.".to_owned(),
                );
                return;
            };
            match account.open_dm(&handle).await {
                Ok(channel) => {
                    let channel_ref = channel.key();
                    let channel_id = channel_ref.to_string();
                    if let Err(error) = account.load_channel(&channel_ref).await {
                        show_account_error(&weak, error.to_string());
                        return;
                    }
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_selected_guild(SharedString::default());
                        window.set_selected_channel(channel_id.into());
                        window.set_active_overlay(SharedString::default());
                        apply_snapshot(&window, snapshot);
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let profile_active = active.clone();
    window.on_friend_profile(move |value| {
        let Ok(user_ref) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(account) = profile_active.try_read() else {
            return;
        };
        let Some(account) = account.as_ref() else {
            return;
        };
        let Ok(state) = account.state.try_read() else {
            return;
        };
        let Some(user) = state.users.get(&user_ref) else {
            return;
        };
        let relationship = if state
            .current_user
            .as_ref()
            .map(kaede_core::User::key)
            .as_ref()
            == Some(&user_ref)
        {
            "self"
        } else {
            state
                .relationships
                .get(&user_ref)
                .map_or("none", |relationship| relationship.kind.as_str())
        };
        let selected_guild = window
            .get_selected_guild()
            .as_str()
            .parse::<EntityRef>()
            .ok();
        let assigned = selected_guild
            .as_ref()
            .and_then(|guild| state.members.get(&(guild.clone(), user_ref.clone())))
            .map(|member| member.role_ids.as_slice())
            .unwrap_or_default();
        let profile_roles = selected_guild
            .as_ref()
            .into_iter()
            .flat_map(|guild| {
                state.roles.values().filter(move |role| {
                    role.guild_id == guild.id && role.guild_domain == guild.domain
                })
            })
            .filter(|role| role.name != "@everyone")
            .map(|role| ProfileRoleItem {
                id: role.key().to_string().into(),
                name: role.name.clone().into(),
                color: role_color(role.color),
                assigned: assigned.contains(&role.id),
                editable: window.get_can_manage_roles(),
            })
            .collect::<Vec<_>>();
        window.set_profile_id(value);
        window.set_profile_name(user.label().into());
        window.set_profile_handle(user.handle.clone().into());
        window.set_profile_status(user.custom_status.clone().unwrap_or_default().into());
        window.set_profile_bio(user.bio.clone().unwrap_or_default().into());
        let avatar = public_asset_path(
            &state,
            &user.origin_domain,
            user.avatar_hash.as_deref(),
            "thumbnail_128",
        );
        let banner = public_asset_path(
            &state,
            &user.origin_domain,
            user.banner_hash.as_deref(),
            "thumbnail_1024",
        );
        window.set_profile_has_avatar(!avatar.is_empty());
        window.set_profile_avatar(load_ui_image(&avatar));
        window.set_profile_has_banner(!banner.is_empty());
        window.set_profile_banner(load_ui_image(&banner));
        window.set_profile_relationship(relationship.into());
        window.set_profile_roles(ModelRc::from(Rc::new(VecModel::from(profile_roles))));
        window.set_active_overlay("profile".into());
    });

    let weak = window.as_weak();
    let relationship_runtime = runtime.clone();
    let relationship_active = active.clone();
    window.on_profile_relationship_action(move |value, action| {
        let Ok(user) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = relationship_active.clone();
        relationship_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let result = match action.as_str() {
                "friend" => account.remove_relationship(&user).await,
                "incoming" => account.accept_friend(&user).await,
                "blocked" => account.set_blocked(&user, false).await,
                _ => {
                    let handle = account
                        .state
                        .read()
                        .await
                        .users
                        .get(&user)
                        .map(|user| user.handle.clone());
                    match handle {
                        Some(handle) => account.request_friend(&handle).await,
                        None => Err(kaede_app::AccountError::MissingIdentity),
                    }
                }
            };
            match result {
                Ok(()) => {
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_active_overlay(SharedString::default());
                        window.set_error_message("Relationship updated.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let block_runtime = runtime.clone();
    let block_active = active.clone();
    window.on_profile_block(move |value, blocked| {
        let Ok(user) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = block_active.clone();
        block_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.set_blocked(&user, blocked).await {
                Ok(()) => {
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_active_overlay(SharedString::default());
                        window.set_error_message("Privacy setting updated.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let role_runtime = runtime.clone();
    let role_active = active.clone();
    window.on_profile_role(move |user, role, assign| {
        let Ok(user) = user.as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(role) = role.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = role_active.clone();
        role_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account
                .service
                .assign_role(&guild, &user, &role, assign)
                .await
            {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_active_overlay(SharedString::default());
                        window.set_error_message("Member roles updated.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let presence_runtime = runtime.clone();
    let presence_active = active.clone();
    window.on_set_presence(move |status| {
        let status = status.to_string();
        if !matches!(status.as_str(), "online" | "idle" | "dnd" | "invisible") {
            show_account_error(&weak, "Unsupported presence status.".to_owned());
            return;
        }
        let custom_status = weak.upgrade().and_then(|window| {
            let value = window.get_current_profile_status().trim().to_owned();
            (!value.is_empty()).then_some(value)
        });
        let weak = weak.clone();
        let active = presence_active.clone();
        presence_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.set_presence(&status, custom_status).await {
                Ok(()) => {
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_presence_status(status.into());
                        window.set_error_message("Presence updated.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let profile_runtime = runtime.clone();
    let profile_active = active.clone();
    window.on_save_profile(move |display_name, custom_status, bio| {
        let weak = weak.clone();
        let active = profile_active.clone();
        let patch = kaede_api::service::ProfilePatch {
            display_name: optional_text(&display_name),
            custom_status: optional_text(&custom_status),
            bio: optional_text(&bio),
        };
        profile_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.update_profile(&patch).await {
                Ok(_) => {
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_error_message("Profile saved.".into())
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let profile_asset_runtime = runtime.clone();
    let profile_asset_active = active.clone();
    window.on_upload_profile_asset(move |kind| {
        let Some(kind) = (match kind.as_str() {
            "avatar" => Some(kaede_media::ProfileAssetKind::Avatar),
            "banner" => Some(kaede_media::ProfileAssetKind::Banner),
            _ => None,
        }) else {
            return;
        };
        let weak = weak.clone();
        let active = profile_asset_active.clone();
        profile_asset_runtime.spawn(async move {
            let Some(file) = rfd::AsyncFileDialog::new()
                .set_title("Choose a profile image")
                .add_filter("Images", &["png", "jpg", "jpeg", "gif", "webp"])
                .pick_file()
                .await
            else {
                return;
            };
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let media = kaede_media::MediaClient::new(account.api.clone());
            let _ = weak.upgrade_in_event_loop(|window| {
                window.set_error_message("Uploading and scanning profile image…".into())
            });
            match media
                .upload_profile_asset(kind, file.path(), content_type_for_path(file.path()))
                .await
            {
                Ok(_) => {
                    if let Ok(user) = account.service.me().await {
                        account.state.write().await.hydrate_identity(user);
                    }
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        apply_snapshot(&window, snapshot);
                        window.set_error_message("Profile image updated.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let preferences_runtime = runtime.clone();
    let preferences_active = active.clone();
    window.on_save_preferences(move |theme, privacy, notifications, developer| {
        let weak = weak.clone();
        let active = preferences_active.clone();
        let patch = serde_json::json!({
            "theme": theme.as_str(),
            "dm_privacy": privacy.as_str(),
            "notification_settings": {
                "desktop": notifications,
                "developer_mode": developer,
            },
        });
        preferences_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.update_settings(&patch).await {
                Ok(_) => {
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_desktop_notifications(notifications);
                        window.set_developer_mode(developer);
                        window.set_error_message("Preferences saved.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let email_runtime = runtime.clone();
    let email_active = active.clone();
    window.on_request_email_change(move |email, password| {
        if email.trim().is_empty() || password.is_empty() {
            if let Some(window) = weak.upgrade() {
                window.set_security_result("Enter a new email and current password.".into());
            }
            return;
        }
        let weak = weak.clone();
        let active = email_active.clone();
        let email = email.to_string();
        let password = password.to_string();
        email_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account
                .session
                .request_email_change(&email, &password)
                .await
            {
                Ok(_) => {
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_security_result(
                            "Verification sent. Confirm the change from your email.".into(),
                        )
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let setup_runtime = runtime.clone();
    let setup_active = active.clone();
    window.on_setup_mfa(move |password, current_code| {
        if password.is_empty() {
            if let Some(window) = weak.upgrade() {
                window.set_security_result("Enter your current password.".into());
            }
            return;
        }
        let weak = weak.clone();
        let active = setup_active.clone();
        let password = password.to_string();
        let current_code = optional_text(&current_code);
        setup_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account
                .session
                .setup_mfa(&password, current_code.as_deref())
                .await
            {
                Ok(setup) => {
                    let result = format!(
                        "Authenticator secret (shown once): {}\nURI: {}",
                        setup.secret.expose_secret(),
                        setup.uri.expose_secret()
                    );
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_security_result(result.into())
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let enable_runtime = runtime.clone();
    let enable_active = active.clone();
    window.on_enable_mfa(move |code| {
        if code.trim().is_empty() {
            return;
        }
        let weak = weak.clone();
        let active = enable_active.clone();
        let code = code.to_string();
        enable_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.session.enable_mfa(&code).await {
                Ok(enabled) => {
                    let codes = enabled
                        .recovery_codes
                        .iter()
                        .map(|code| code.expose_secret().to_owned())
                        .collect::<Vec<_>>()
                        .join("\n");
                    let result = format!("MFA enabled. Recovery codes (shown once):\n{codes}");
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_security_result(result.into())
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let disable_runtime = runtime.clone();
    let disable_active = active.clone();
    window.on_disable_mfa(move |password, code| {
        if password.is_empty() || code.trim().is_empty() {
            return;
        }
        let weak = weak.clone();
        let active = disable_active.clone();
        let password = password.to_string();
        let code = code.to_string();
        disable_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.session.disable_mfa(&password, &code).await {
                Ok(_) => {
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_security_result("Multi-factor authentication disabled.".into())
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let guild_settings_active = active.clone();
    let guild_settings_runtime = runtime.clone();
    window.on_open_guild_settings(move || {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(account) = guild_settings_active.try_read() else {
            return;
        };
        let Some(account) = account.as_ref() else {
            return;
        };
        let Ok(state) = account.state.try_read() else {
            return;
        };
        let selected = window.get_selected_role().to_string();
        if let Some(role) = state
            .roles
            .values()
            .find(|role| role.key().to_string() == selected)
        {
            show_role_editor(&window, role);
        }
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        drop(state);
        let weak = weak.clone();
        let active = guild_settings_active.clone();
        guild_settings_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let (audit, webhooks, invites, bans, instance_bans) = tokio::join!(
                account.service.audit_log(&guild, None),
                account.service.webhooks(&guild),
                account.service.invites(&guild),
                account.service.bans(&guild, None),
                account.service.instance_bans(&guild, None),
            );
            let audit = audit.map_or_else(|_| Vec::new(), |value| admin_records(&value, "audit"));
            let webhooks =
                webhooks.map_or_else(|_| Vec::new(), |value| admin_records(&value, "webhook"));
            let invites = invites.map_or_else(
                |_| Vec::new(),
                |value| admin_records(&serde_json::Value::Array(value), "invite"),
            );
            let bans = bans.map_or_else(|_| Vec::new(), |value| admin_records(&value, "ban"));
            let instance_bans = instance_bans.map_or_else(
                |_| Vec::new(),
                |value| admin_records(&value, "instance-ban"),
            );
            let _ = weak.upgrade_in_event_loop(move |window| {
                window.set_audit_records(record_model(audit));
                window.set_webhook_records(record_model(webhooks));
                window.set_invite_records(record_model(invites));
                window.set_ban_records(record_model(bans));
                window.set_instance_ban_records(record_model(instance_bans));
            });
        });
    });

    let weak = window.as_weak();
    let role_select_active = active.clone();
    window.on_guild_role_selected(move |value| {
        let Ok(role_ref) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(account) = role_select_active.try_read() else {
            return;
        };
        let Some(account) = account.as_ref() else {
            return;
        };
        let Ok(state) = account.state.try_read() else {
            return;
        };
        if let Some(role) = state.roles.get(&role_ref) {
            show_role_editor(&window, role);
        }
    });

    let weak = window.as_weak();
    let create_role_runtime = runtime.clone();
    let create_role_active = active.clone();
    window.on_guild_create_role(move |name| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = create_role_active.clone();
        create_role_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let request = kaede_api::service::RoleCreate {
                name: name.to_string(),
                permissions: kaede_protocol::PermissionBits(0),
                color: 0,
                hoist: false,
                mentionable: false,
            };
            match account.service.create_role(&guild, &request).await {
                Ok(role) => {
                    if let Err(error) = account.load_guild(&guild).await {
                        show_account_error(&weak, error.to_string());
                        return;
                    }
                    let id = role.key().to_string();
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_selected_role(id.into());
                        window.set_error_message("Role created.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let save_role_runtime = runtime.clone();
    let save_role_active = active.clone();
    window.on_guild_save_role(move |role, name, color, hoist, mentionable| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(role_ref) = role.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = save_role_active.clone();
        save_role_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let version = account
                .state
                .read()
                .await
                .roles
                .get(&role_ref)
                .and_then(|role| role.version.clone());
            let Some(version) = version else {
                show_account_error(
                    &weak,
                    "Reload the guild before editing this role.".to_owned(),
                );
                return;
            };
            let Some(color) = parse_hex_color(color.as_str()) else {
                show_account_error(
                    &weak,
                    "Enter a six-digit role color such as #99aab5.".to_owned(),
                );
                return;
            };
            let patch = kaede_api::service::RoleUpdate {
                name: Some(name.to_string()),
                color: Some(color),
                hoist: Some(hoist),
                mentionable: Some(mentionable),
                ..kaede_api::service::RoleUpdate::default()
            };
            match account
                .service
                .update_role(&guild, &role_ref, &patch, &version)
                .await
            {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_error_message("Role saved.".into())
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let permission_runtime = runtime.clone();
    let permission_active = active.clone();
    window.on_guild_toggle_role_permission(move |role, bit, enabled| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(role_ref) = role.as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(bit) = bit.parse::<u64>() else {
            return;
        };
        let weak = weak.clone();
        let active = permission_active.clone();
        permission_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let (mut permissions, version) = {
                let state = account.state.read().await;
                let Some(role) = state.roles.get(&role_ref) else {
                    return;
                };
                let Some(version) = role.version.clone() else {
                    return;
                };
                (role.permissions, version)
            };
            if enabled {
                permissions.0 |= bit;
            } else {
                permissions.0 &= !bit;
            }
            let patch = kaede_api::service::RoleUpdate {
                permissions: Some(permissions),
                ..kaede_api::service::RoleUpdate::default()
            };
            match account
                .service
                .update_role(&guild, &role_ref, &patch, &version)
                .await
            {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let delete_role_runtime = runtime.clone();
    let delete_role_active = active.clone();
    window.on_guild_delete_role(move |role| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(role) = role.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = delete_role_active.clone();
        delete_role_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.service.delete_role(&guild, &role).await {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_selected_role(SharedString::default());
                        window.set_error_message("Role deleted.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let move_role_runtime = runtime.clone();
    let move_role_active = active.clone();
    window.on_guild_move_role(move |role, delta| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(role) = role.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = move_role_active.clone();
        move_role_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let positions = {
                let state = account.state.read().await;
                let mut roles = state
                    .roles
                    .values()
                    .filter(|item| {
                        EntityRef::new(item.guild_id, item.guild_domain.clone()) == guild
                    })
                    .collect::<Vec<_>>();
                roles.sort_by_key(|item| item.position);
                let Some(index) = roles.iter().position(|item| item.key() == role) else {
                    return;
                };
                let target = (index as i32 + delta).clamp(0, roles.len() as i32 - 1) as usize;
                roles.swap(index, target);
                roles
                    .iter()
                    .enumerate()
                    .map(|(position, item)| kaede_api::service::PositionPatch {
                        id: item.id,
                        position: position as i32,
                        parent_id: None,
                        sync_permissions: false,
                        version: item.version.clone(),
                    })
                    .collect::<Vec<_>>()
            };
            match account.service.reorder_roles(&guild, &positions).await {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ =
                        weak.upgrade_in_event_loop(move |window| apply_snapshot(&window, snapshot));
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let channel_runtime = runtime.clone();
    let channel_active = active.clone();
    window.on_guild_create_channel(move |name, kind| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = channel_active.clone();
        channel_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let request = kaede_api::service::ChannelCreate {
                name: name.to_string(),
                kind: match kind.as_str() {
                    "voice" => 2,
                    "category" => 4,
                    _ => 0,
                },
                parent_id: None,
                topic: None,
                rate_limit_per_user: 0,
            };
            match account.service.create_channel(&guild, &request).await {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_error_message("Channel created.".into())
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let channel_select_active = active.clone();
    let channel_select_runtime = runtime.clone();
    let channel_select_masks = overwrite_masks.clone();
    let channel_select_weak = window.as_weak();
    window.on_guild_select_channel(move |value| {
        let Ok(channel_ref) = value.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = channel_select_weak.upgrade() else {
            return;
        };
        let Ok(account) = channel_select_active.try_read() else {
            return;
        };
        let Some(account) = account.as_ref() else {
            return;
        };
        let Ok(state) = account.state.try_read() else {
            return;
        };
        if let Some(channel) = state.channels.get(&channel_ref) {
            window.set_selected_admin_channel(channel.key().to_string().into());
            window.set_selected_admin_channel_name(channel.name.clone().unwrap_or_default().into());
            window
                .set_selected_admin_channel_topic(channel.topic.clone().unwrap_or_default().into());
            window.set_selected_admin_channel_parent(
                channel
                    .parent_id
                    .zip(channel.parent_domain.clone())
                    .map_or_else(String::new, |(id, domain)| {
                        EntityRef::new(id, domain).to_string()
                    })
                    .into(),
            );
            window.set_selected_admin_channel_slow_mode(channel.rate_limit_per_user as i32);
            window.set_selected_admin_channel_synced(channel.permissions_synced);
        }
        drop(state);
        let guild = window
            .get_selected_guild()
            .as_str()
            .parse::<EntityRef>()
            .ok();
        let weak = channel_select_weak.clone();
        let account = account.clone();
        let masks = channel_select_masks.clone();
        channel_select_runtime.spawn(async move {
            if let Some(guild) = guild
                && let Err(error) =
                    refresh_overwrite_models(&account, &weak, &guild, &channel_ref, &masks).await
            {
                show_account_error(&weak, error.to_string());
            }
        });
    });

    let select_overwrite_masks = overwrite_masks.clone();
    let select_overwrite_weak = window.as_weak();
    window.on_guild_select_overwrite(move |target, kind| {
        let key = (target.to_string(), kind.to_string());
        let Some(window) = select_overwrite_weak.upgrade() else {
            return;
        };
        let Ok(masks) = select_overwrite_masks.try_read() else {
            return;
        };
        let (allow, deny) = masks.get(&key).copied().unwrap_or_default();
        window.set_selected_overwrite(target);
        window.set_selected_overwrite_kind(kind);
        window.set_overwrite_permissions(overwrite_permission_model(allow, deny));
    });

    let overwrite_runtime = runtime.clone();
    let overwrite_active = active.clone();
    let overwrite_masks_for_set = overwrite_masks.clone();
    let overwrite_weak = window.as_weak();
    window.on_guild_set_overwrite_permission(move |target, kind, bit, state_value| {
        let Ok(target_ref) = target.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = overwrite_weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(channel) = window
            .get_selected_admin_channel()
            .as_str()
            .parse::<EntityRef>()
        else {
            return;
        };
        let Ok(bit) = bit.as_str().parse::<u64>() else {
            return;
        };
        let key = (target.to_string(), kind.to_string());
        let weak = overwrite_weak.clone();
        let active = overwrite_active.clone();
        let masks = overwrite_masks_for_set.clone();
        overwrite_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let (mut allow, mut deny) = masks.read().await.get(&key).copied().unwrap_or_default();
            allow &= !bit;
            deny &= !bit;
            match state_value.as_str() {
                "allow" => allow |= bit,
                "deny" => deny |= bit,
                _ => {}
            }
            let payload = serde_json::json!({
                "target_id": target_ref.to_string(),
                "target_type": kind.as_str(),
                "allow": allow.to_string(),
                "deny": deny.to_string(),
            });
            match account
                .service
                .set_overwrite(&guild, &channel, &payload)
                .await
            {
                Ok(_) => {
                    masks.write().await.insert(key, (allow, deny));
                    let rows = overwrite_permission_rows(allow, deny);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_selected_admin_channel_synced(false);
                        window.set_overwrite_permissions(ModelRc::from(Rc::new(VecModel::from(
                            rows,
                        ))));
                        window.set_error_message("Channel permissions saved.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let delete_overwrite_runtime = runtime.clone();
    let delete_overwrite_active = active.clone();
    let delete_overwrite_masks = overwrite_masks;
    let delete_overwrite_weak = window.as_weak();
    window.on_guild_delete_overwrite(move |target, kind| {
        let Ok(target_ref) = target.as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(window) = delete_overwrite_weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(channel) = window
            .get_selected_admin_channel()
            .as_str()
            .parse::<EntityRef>()
        else {
            return;
        };
        let key = (target.to_string(), kind.to_string());
        let weak = delete_overwrite_weak.clone();
        let active = delete_overwrite_active.clone();
        let masks = delete_overwrite_masks.clone();
        delete_overwrite_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account
                .service
                .delete_overwrite(&guild, &channel, &target_ref, kind.as_str())
                .await
            {
                Ok(_) => {
                    masks.write().await.insert(key, (0, 0));
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_overwrite_permissions(overwrite_permission_model(0, 0));
                        window.set_error_message("Channel overwrite reset.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let save_channel_runtime = runtime.clone();
    let save_channel_active = active.clone();
    window.on_guild_save_channel(move |channel, name, topic, parent, slow_mode| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(channel) = channel.as_str().parse::<EntityRef>() else {
            return;
        };
        let parent_id = parent
            .as_str()
            .parse::<EntityRef>()
            .ok()
            .map(|value| value.id);
        let weak = weak.clone();
        let active = save_channel_active.clone();
        save_channel_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let version = account
                .state
                .read()
                .await
                .channels
                .get(&channel)
                .and_then(|channel| channel.version.clone());
            let Some(version) = version else {
                show_account_error(&weak, "Reload this channel before editing it.".to_owned());
                return;
            };
            let patch = serde_json::json!({
                "name": name.as_str(),
                "topic": optional_text(&topic),
                "parent_id": parent_id,
                "rate_limit_per_user": slow_mode.clamp(0, 21_600),
            });
            match account
                .service
                .update_channel(&guild, &channel, &patch, &version)
                .await
            {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        apply_snapshot(&window, snapshot);
                        window.set_error_message("Channel saved.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let delete_channel_runtime = runtime.clone();
    let delete_channel_active = active.clone();
    window.on_guild_delete_channel(move |channel| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(channel) = channel.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = delete_channel_active.clone();
        delete_channel_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let version = account
                .state
                .read()
                .await
                .channels
                .get(&channel)
                .and_then(|channel| channel.version.clone());
            let Some(version) = version else {
                return;
            };
            match account
                .service
                .delete_channel(&guild, &channel, &version)
                .await
            {
                Ok(_) => {
                    account.state.write().await.channels.remove(&channel);
                    let _ = account.load_guild(&guild).await;
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_selected_admin_channel(SharedString::default());
                        apply_snapshot(&window, snapshot);
                        window.set_error_message("Channel deleted.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let sync_channel_runtime = runtime.clone();
    let sync_channel_active = active.clone();
    window.on_guild_sync_channel(move |channel| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(channel) = channel.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = sync_channel_active.clone();
        sync_channel_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account
                .service
                .sync_channel_permissions(&guild, &channel)
                .await
            {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        apply_snapshot(&window, snapshot);
                        window.set_error_message("Channel permissions synchronized.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let move_channel_runtime = runtime.clone();
    let move_channel_active = active.clone();
    window.on_guild_move_channel(move |channel, delta| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(channel) = channel.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = move_channel_active.clone();
        move_channel_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let positions = {
                let state = account.state.read().await;
                let mut channels = state
                    .channels
                    .values()
                    .filter(|item| item.guild_key().as_ref() == Some(&guild))
                    .collect::<Vec<_>>();
                channels.sort_by_key(|item| item.position);
                let Some(index) = channels.iter().position(|item| item.key() == channel) else {
                    return;
                };
                let target = (index as i32 + delta).clamp(0, channels.len() as i32 - 1) as usize;
                channels.swap(index, target);
                channels
                    .iter()
                    .enumerate()
                    .map(|(position, item)| kaede_api::service::PositionPatch {
                        id: item.id,
                        position: position as i32,
                        parent_id: item.parent_id,
                        sync_permissions: false,
                        version: item.version.clone(),
                    })
                    .collect::<Vec<_>>()
            };
            match account.service.reorder_channels(&guild, &positions).await {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ =
                        weak.upgrade_in_event_loop(move |window| apply_snapshot(&window, snapshot));
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let moderation_runtime = runtime.clone();
    let moderation_active = active.clone();
    window.on_guild_moderate_member(move |user, action, duration, reason| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(user) = user.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = moderation_active.clone();
        moderation_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let result = match action.as_str() {
                "timeout" => {
                    account
                        .service
                        .moderate_member(&guild, &user, &timeout_patch(duration.as_str()))
                        .await
                }
                "untimeout" => {
                    account
                        .service
                        .moderate_member(&guild, &user, &serde_json::json!({"timeout_until": null}))
                        .await
                }
                "kick" => account.service.kick_member(&guild, &user).await,
                "ban" => {
                    account
                        .service
                        .ban_member(
                            &guild,
                            &user,
                            &serde_json::json!({
                                "reason": optional_text(&reason),
                                "delete_message_seconds": 0,
                                "expires_at": ban_expiry(duration.as_str()),
                            }),
                        )
                        .await
                }
                _ => return,
            };
            match result {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_error_message("Moderation action applied.".into())
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let voice_moderation_runtime = runtime.clone();
    let voice_moderation_active = active.clone();
    window.on_guild_voice_moderate(move |user, action| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(user) = user.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = voice_moderation_active.clone();
        voice_moderation_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let result = if let Some(destination) = action.strip_prefix("move:") {
                let Ok(destination) = destination.parse::<EntityRef>() else {
                    return;
                };
                account
                    .service
                    .move_voice(&guild, &user, &destination)
                    .await
            } else {
                match action.as_str() {
                    "mute" => {
                        account
                            .service
                            .update_voice_moderation(&guild, &user, Some(true), None)
                            .await
                    }
                    "unmute" => {
                        account
                            .service
                            .update_voice_moderation(&guild, &user, Some(false), None)
                            .await
                    }
                    "deafen" => {
                        account
                            .service
                            .update_voice_moderation(&guild, &user, None, Some(true))
                            .await
                    }
                    "undeafen" => {
                        account
                            .service
                            .update_voice_moderation(&guild, &user, None, Some(false))
                            .await
                    }
                    "disconnect" => account.service.disconnect_voice(&guild, &user).await,
                    _ => return,
                }
            };
            match result {
                Ok(_) => {
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_error_message("Voice moderation updated.".into())
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let invite_runtime = runtime.clone();
    let invite_active = active.clone();
    window.on_guild_create_invite(move || {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let channel = window
            .get_selected_channel()
            .as_str()
            .parse::<EntityRef>()
            .ok();
        let weak = weak.clone();
        let active = invite_active.clone();
        invite_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let request = kaede_api::service::InviteCreate {
                channel_id: channel,
                max_age_seconds: Some(86_400),
                max_uses: None,
            };
            match account.service.create_invite(&guild, &request).await {
                Ok(value) => {
                    let code = value
                        .get("code")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or("invite created")
                        .to_owned();
                    let records = account.service.invites(&guild).await.map_or_else(
                        |_| Vec::new(),
                        |value| admin_records(&serde_json::Value::Array(value), "invite"),
                    );
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        copy_to_clipboard(&code);
                        window.set_invite_records(record_model(records));
                        window.set_error_message("Invite copied to the clipboard.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    window.on_guild_copy_invite(|code| copy_to_clipboard(code.as_str()));

    let weak = window.as_weak();
    let revoke_invite_runtime = runtime.clone();
    let revoke_invite_active = active.clone();
    window.on_guild_revoke_invite(move |code| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = revoke_invite_active.clone();
        revoke_invite_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.service.revoke_invite(&guild, code.as_str()).await {
                Ok(_) => {
                    let records = account.service.invites(&guild).await.map_or_else(
                        |_| Vec::new(),
                        |value| admin_records(&serde_json::Value::Array(value), "invite"),
                    );
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_invite_records(record_model(records));
                        window.set_error_message("Invite revoked.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let emoji_runtime = runtime.clone();
    let emoji_active = active.clone();
    window.on_guild_upload_emoji(move |name| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = emoji_active.clone();
        emoji_runtime.spawn(async move {
            let Some(file) = rfd::AsyncFileDialog::new()
                .set_title("Choose a custom emoji")
                .add_filter("Images", &["png", "jpg", "jpeg", "gif", "webp"])
                .pick_file()
                .await
            else {
                return;
            };
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let media = kaede_media::MediaClient::new(account.api.clone());
            let _ = weak.upgrade_in_event_loop(|window| {
                window.set_error_message("Uploading and scanning emoji…".into())
            });
            match media
                .upload_guild_emoji(
                    &guild,
                    name.as_str(),
                    file.path(),
                    content_type_for_path(file.path()),
                )
                .await
            {
                Ok(emoji) => {
                    account
                        .state
                        .write()
                        .await
                        .emojis
                        .insert(emoji.key(), emoji);
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        apply_snapshot(&window, snapshot);
                        window.set_error_message("Custom emoji created.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let delete_emoji_runtime = runtime.clone();
    let delete_emoji_active = active.clone();
    window.on_guild_delete_emoji(move |emoji| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(emoji) = emoji.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = delete_emoji_active.clone();
        delete_emoji_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let media = kaede_media::MediaClient::new(account.api.clone());
            match media.delete_guild_emoji(&guild, emoji.id).await {
                Ok(()) => {
                    account.state.write().await.emojis.remove(&emoji);
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        apply_snapshot(&window, snapshot);
                        window.set_error_message("Custom emoji deleted.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let webhook_runtime = runtime.clone();
    let webhook_active = active.clone();
    window.on_guild_create_webhook(move |channel, name| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(channel) = channel.as_str().parse::<EntityRef>() else {
            window.set_error_message("Enter a complete channel ID.".into());
            return;
        };
        let weak = weak.clone();
        let active = webhook_active.clone();
        webhook_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account
                .service
                .create_webhook(&guild, &channel, name.as_str())
                .await
            {
                Ok(value) => {
                    if let Some(token) = value.get("token").and_then(serde_json::Value::as_str) {
                        copy_to_clipboard(token);
                    }
                    let records = account
                        .service
                        .webhooks(&guild)
                        .await
                        .map_or_else(|_| Vec::new(), |value| admin_records(&value, "webhook"));
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_webhook_records(record_model(records));
                        window.set_error_message(
                            "Webhook created. Its one-time token was copied securely.".into(),
                        );
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let webhook_update_runtime = runtime.clone();
    let webhook_update_active = active.clone();
    window.on_guild_update_webhook(move |webhook, name| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(webhook) = webhook.as_str().parse::<EntityRef>() else {
            window.set_error_message("The webhook reference is invalid.".into());
            return;
        };
        let name = name.trim().to_owned();
        if name.is_empty() {
            window.set_error_message("Webhook names cannot be empty.".into());
            return;
        }
        let weak = weak.clone();
        let active = webhook_update_active.clone();
        webhook_update_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account
                .service
                .update_webhook(&guild, &webhook, &serde_json::json!({"name": name}))
                .await
            {
                Ok(_) => {
                    let records = account
                        .service
                        .webhooks(&guild)
                        .await
                        .map_or_else(|_| Vec::new(), |value| admin_records(&value, "webhook"));
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_webhook_records(record_model(records));
                        window.set_error_message("Webhook updated.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let webhook_delete_runtime = runtime.clone();
    let webhook_delete_active = active.clone();
    window.on_guild_delete_webhook(move |webhook| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(webhook) = webhook.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = webhook_delete_active.clone();
        webhook_delete_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.service.delete_webhook(&guild, &webhook).await {
                Ok(_) => {
                    let records = account
                        .service
                        .webhooks(&guild)
                        .await
                        .map_or_else(|_| Vec::new(), |value| admin_records(&value, "webhook"));
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_webhook_records(record_model(records));
                        window.set_error_message("Webhook deleted.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let webhook_rotate_runtime = runtime.clone();
    let webhook_rotate_active = active.clone();
    window.on_guild_rotate_webhook(move |webhook| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(webhook) = webhook.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = webhook_rotate_active.clone();
        webhook_rotate_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.service.rotate_webhook_token(&guild, &webhook).await {
                Ok(value) => {
                    let Some(token) = value.get("token").and_then(serde_json::Value::as_str) else {
                        show_account_error(
                            &weak,
                            "The server did not return the new token.".to_owned(),
                        );
                        return;
                    };
                    copy_to_clipboard(token);
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_error_message(
                            "Webhook token rotated and copied. It will not be shown again.".into(),
                        )
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let unban_runtime = runtime.clone();
    let unban_active = active.clone();
    window.on_guild_unban_member(move |user| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(user) = user.as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = unban_active.clone();
        unban_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.service.unban_member(&guild, &user).await {
                Ok(_) => {
                    let records = account
                        .service
                        .bans(&guild, None)
                        .await
                        .map_or_else(|_| Vec::new(), |value| admin_records(&value, "ban"));
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_ban_records(record_model(records));
                        window.set_error_message("Member unbanned.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let instance_ban_runtime = runtime.clone();
    let instance_ban_active = active.clone();
    window.on_guild_ban_instance(move |domain, duration, reason| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(domain) = domain.as_str().parse::<kaede_protocol::Domain>() else {
            window.set_error_message("Enter a valid instance domain.".into());
            return;
        };
        let weak = weak.clone();
        let active = instance_ban_active.clone();
        instance_ban_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let request = serde_json::json!({
                "reason": optional_text(&reason),
                "expires_at": ban_expiry(duration.as_str()),
            });
            match account
                .service
                .ban_instance(&guild, &domain, &request)
                .await
            {
                Ok(_) => {
                    let records = account
                        .service
                        .instance_bans(&guild, None)
                        .await
                        .map_or_else(
                            |_| Vec::new(),
                            |value| admin_records(&value, "instance-ban"),
                        );
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_instance_ban_records(record_model(records));
                        window.set_error_message("Federated instance blocked.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let instance_unban_runtime = runtime.clone();
    let instance_unban_active = active.clone();
    window.on_guild_unban_instance(move |domain| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(domain) = domain.as_str().parse::<kaede_protocol::Domain>() else {
            return;
        };
        let weak = weak.clone();
        let active = instance_unban_active.clone();
        instance_unban_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.service.unban_instance(&guild, &domain).await {
                Ok(_) => {
                    let records = account
                        .service
                        .instance_bans(&guild, None)
                        .await
                        .map_or_else(
                            |_| Vec::new(),
                            |value| admin_records(&value, "instance-ban"),
                        );
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_instance_ban_records(record_model(records));
                        window.set_error_message("Federated instance unblocked.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let guild_save_runtime = runtime.clone();
    let guild_save_active = active.clone();

    let guild_asset_runtime = runtime.clone();
    let guild_asset_active = active.clone();
    let guild_asset_weak = window.as_weak();
    window.on_guild_upload_asset(move |kind| {
        let Some(window) = guild_asset_weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Some(kind) = (match kind.as_str() {
            "icon" => Some(kaede_media::GuildAssetKind::Icon),
            "banner" => Some(kaede_media::GuildAssetKind::Banner),
            _ => None,
        }) else {
            return;
        };
        let weak = guild_asset_weak.clone();
        let active = guild_asset_active.clone();
        guild_asset_runtime.spawn(async move {
            let Some(file) = rfd::AsyncFileDialog::new()
                .set_title("Choose a guild image")
                .add_filter("Images", &["png", "jpg", "jpeg", "gif", "webp"])
                .pick_file()
                .await
            else {
                return;
            };
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let media = kaede_media::MediaClient::new(account.api.clone());
            let _ = weak.upgrade_in_event_loop(|window| {
                window.set_error_message("Uploading and scanning guild image…".into())
            });
            match media
                .upload_guild_asset(
                    &guild,
                    kind,
                    file.path(),
                    content_type_for_path(file.path()),
                )
                .await
            {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        apply_snapshot(&window, snapshot);
                        window.set_error_message("Guild image updated.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    window.on_guild_save(move |name, description, history_policy| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = guild_save_active.clone();
        guild_save_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let version = account
                .state
                .read()
                .await
                .guilds
                .get(&guild)
                .and_then(|guild| guild.version.clone());
            let Some(version) = version else {
                show_account_error(&weak, "Reload this guild before editing it.".to_owned());
                return;
            };
            let patch = serde_json::json!({
                "name": name.as_str(),
                "description": optional_text(&description),
                "federated_history_policy": history_policy.as_str(),
            });
            match account.service.update_guild(&guild, &patch, &version).await {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        apply_snapshot(&window, snapshot);
                        window.set_error_message("Guild settings saved.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let notification_runtime = runtime.clone();
    let notification_active = active.clone();
    window.on_guild_notification_level(move |level| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = notification_active.clone();
        notification_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account
                .service
                .update_guild_notification_settings(&guild, level.as_str())
                .await
            {
                Ok(preference) => {
                    account
                        .state
                        .write()
                        .await
                        .guild_notification_levels
                        .insert(guild, preference.level);
                    let _ = weak.upgrade_in_event_loop(|window| {
                        window.set_error_message("Notification preference saved.".into())
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let leave_runtime = runtime.clone();
    let leave_active = active.clone();
    window.on_guild_leave(move || {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = leave_active.clone();
        leave_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            match account.service.leave_guild(&guild).await {
                Ok(_) => {
                    account.state.write().await.guilds.remove(&guild);
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_active_overlay(SharedString::default());
                        window.set_selected_guild(SharedString::default());
                        window.set_selected_channel(SharedString::default());
                        apply_snapshot(&window, snapshot);
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let transfer_runtime = runtime.clone();
    let transfer_active = active.clone();
    window.on_guild_transfer(move |user| {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let Ok(user) = user.as_str().parse::<EntityRef>() else {
            window.set_error_message("Enter a complete local member ID.".into());
            return;
        };
        let weak = weak.clone();
        let active = transfer_active.clone();
        transfer_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            if user.domain != guild.domain {
                show_account_error(
                    &weak,
                    "Ownership can only be transferred to a member on the guild's home instance."
                        .to_owned(),
                );
                return;
            }
            let version = account
                .state
                .read()
                .await
                .guilds
                .get(&guild)
                .and_then(|guild| guild.version.clone());
            let Some(version) = version else {
                return;
            };
            match account
                .service
                .transfer_guild(&guild, &user, &version)
                .await
            {
                Ok(_) => {
                    let _ = account.load_guild(&guild).await;
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        apply_snapshot(&window, snapshot);
                        window.set_error_message("Guild ownership transferred.".into());
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    let delete_guild_runtime = runtime.clone();
    let delete_guild_active = active.clone();
    window.on_guild_delete(move || {
        let Some(window) = weak.upgrade() else {
            return;
        };
        let Ok(guild) = window.get_selected_guild().as_str().parse::<EntityRef>() else {
            return;
        };
        let weak = weak.clone();
        let active = delete_guild_active.clone();
        delete_guild_runtime.spawn(async move {
            let Some(account) = active.read().await.clone() else {
                return;
            };
            let version = account
                .state
                .read()
                .await
                .guilds
                .get(&guild)
                .and_then(|guild| guild.version.clone());
            let Some(version) = version else {
                show_account_error(&weak, "Reload the guild before deleting it.".to_owned());
                return;
            };
            match account.service.delete_guild(&guild, &version).await {
                Ok(_) => {
                    account.state.write().await.guilds.remove(&guild);
                    let snapshot = ui_snapshot(&*account.state.read().await);
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_active_overlay(SharedString::default());
                        window.set_selected_guild(SharedString::default());
                        window.set_selected_channel(SharedString::default());
                        apply_snapshot(&window, snapshot);
                    });
                }
                Err(error) => show_account_error(&weak, error.to_string()),
            }
        });
    });

    let weak = window.as_weak();
    window.on_logout(move || {
        let weak = weak.clone();
        let active = active.clone();
        runtime.spawn(async move {
            #[cfg(feature = "native-voice")]
            leave_active_voice().await;
            let account = active.write().await.take();
            if let Some(account) = account {
                let account_key = account.account_key().to_owned();
                if let Err(error) = account.logout().await {
                    tracing::warn!(%error, "server logout completed with a local warning");
                }
                if let Ok(paths) = PlatformPaths::discover() {
                    let mut registry = AccountRegistry::load(&paths).await.unwrap_or_default();
                    registry.forget(&account_key);
                    if let Err(error) = registry.save(&paths).await {
                        tracing::warn!(%error, "could not remove the signed-out account from the account chooser");
                    }
                    apply_known_accounts(&weak, &registry.accounts);
                }
            }
            let _ = weak.upgrade_in_event_loop(|window| {
                window.set_authenticated(false);
                window.set_active_overlay(SharedString::default());
                window.set_selected_guild(SharedString::default());
                window.set_selected_channel(SharedString::default());
                install_empty_models(&window);
            });
        });
    });
}

#[cfg(feature = "native-voice")]
async fn leave_active_voice() {
    let Some(voice) = ACTIVE_VOICE.get() else {
        return;
    };
    if let Some(handle) = voice.lock().await.take() {
        handle.leave().await;
    }
}

fn content_type_for_path(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|extension| extension.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("png") => "image/png",
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        Some("avif") => "image/avif",
        Some("mp4") => "video/mp4",
        Some("webm") => "video/webm",
        Some("mov") => "video/quicktime",
        Some("mp3") => "audio/mpeg",
        Some("ogg" | "oga") => "audio/ogg",
        Some("wav") => "audio/wav",
        Some("flac") => "audio/flac",
        Some("pdf") => "application/pdf",
        Some("txt" | "log" | "md") => "text/plain",
        Some("json") => "application/json",
        Some("zip") => "application/zip",
        _ => "application/octet-stream",
    }
}

fn show_role_editor(window: &AppWindow, role: &kaede_core::Role) {
    window.set_selected_role(role.key().to_string().into());
    window.set_selected_role_name(role.name.clone().into());
    window.set_selected_role_color(format!("#{:06x}", role.color & 0x00ff_ffff).into());
    window.set_selected_role_hoist(role.hoist);
    window.set_selected_role_mentionable(role.mentionable);
    set_permission_model(window, role.permissions.0);
}

fn parse_hex_color(value: &str) -> Option<u32> {
    let value = value.trim().strip_prefix('#').unwrap_or(value.trim());
    (value.len() == 6)
        .then(|| u32::from_str_radix(value, 16).ok())
        .flatten()
}

fn optional_text(value: &SharedString) -> Option<String> {
    let value = value.trim();
    (!value.is_empty()).then(|| value.to_owned())
}

fn timeout_patch(duration: &str) -> serde_json::Value {
    if duration == "Permanent" {
        return serde_json::json!({"timeout_until": null, "timeout_indefinite": true});
    }
    let span = match duration {
        "10 minutes" => chrono::Duration::minutes(10),
        "1 day" => chrono::Duration::days(1),
        "7 days" => chrono::Duration::days(7),
        "28 days" => chrono::Duration::days(28),
        _ => chrono::Duration::hours(1),
    };
    serde_json::json!({
        "timeout_until": (chrono::Utc::now() + span).to_rfc3339(),
        "timeout_indefinite": false,
    })
}

fn ban_expiry(duration: &str) -> Option<String> {
    let span = match duration {
        "1 day" => Some(chrono::Duration::days(1)),
        "7 days" => Some(chrono::Duration::days(7)),
        "30 days" => Some(chrono::Duration::days(30)),
        _ => None,
    };
    span.map(|span| (chrono::Utc::now() + span).to_rfc3339())
}

fn copy_to_clipboard(value: &str) {
    if let Ok(mut clipboard) = ClipboardContext::new() {
        let _ = clipboard.set_contents(value.to_owned());
    }
}

async fn consume_account_events(
    account: Arc<AccountRuntime>,
    mut events: mpsc::UnboundedReceiver<AccountEvent>,
    weak: slint::Weak<AppWindow>,
) {
    follow_pending_deep_link(&account, &weak).await;
    while let Some(event) = events.recv().await {
        match event {
            AccountEvent::StateChanged => {
                account.refresh_public_assets().await;
                let snapshot = ui_snapshot(&*account.state.read().await);
                let ack_account = account.clone();
                let runtime = tokio::runtime::Handle::current();
                let _ = weak.upgrade_in_event_loop(move |window| {
                    apply_snapshot(&window, snapshot);
                    // Messages that arrive while their conversation is open
                    // are acknowledged immediately, like the web client's
                    // bottom-pinned timeline.
                    if let Ok(channel) = window.get_selected_channel().as_str().parse::<EntityRef>()
                    {
                        runtime.spawn(acknowledge_open_channel(ack_account, channel));
                    }
                });
                tokio::spawn(refresh_gif_stills(account.clone(), weak.clone()));
            }
            AccountEvent::PurgeChannel(_channel) => {
                // Persistent media and entity cache removal is performed by the
                // cache coordinator before this event is rendered.
            }
            AccountEvent::Notification(notification) => {
                tokio::spawn(async move {
                    if let Err(error) = SystemNotificationService.show(notification).await {
                        tracing::debug!(%error, "desktop notification was not displayed");
                    }
                });
            }
            AccountEvent::VoiceReauthorization {
                channel,
                move_session_id,
            } => {
                #[cfg(feature = "native-voice")]
                reauthorize_voice(account.clone(), channel, move_session_id, weak.clone()).await;
                #[cfg(not(feature = "native-voice"))]
                let _ = (channel, move_session_id);
            }
            AccountEvent::ReconcileRequired => {
                let _ = weak.upgrade_in_event_loop(|window| {
                    window.set_error_message(
                        "Realtime updates were interrupted; refreshing state…".into(),
                    );
                });
                if let Err(error) = account.reconcile().await {
                    let message = friendly_error(&error.to_string());
                    let _ = weak.upgrade_in_event_loop(move |window| {
                        window.set_error_message(message.into());
                    });
                }
            }
            AccountEvent::GatewayStatus(status) => {
                tracing::debug!(?status, "gateway status changed");
                let label = match status {
                    kaede_gateway::GatewayStatus::Connected => "connected",
                    kaede_gateway::GatewayStatus::Connecting => "connecting",
                    kaede_gateway::GatewayStatus::Reconnecting
                    | kaede_gateway::GatewayStatus::Disconnected => "reconnecting",
                    kaede_gateway::GatewayStatus::AuthenticationFailed => "authentication_failed",
                };
                let _ = weak.upgrade_in_event_loop(move |window| {
                    window.set_realtime_status(label.into());
                });
            }
            AccountEvent::Error(message) => {
                let _ = weak
                    .upgrade_in_event_loop(move |window| window.set_error_message(message.into()));
            }
        }
    }
}

#[cfg(feature = "native-voice")]
async fn reauthorize_voice(
    account: Arc<AccountRuntime>,
    channel: EntityRef,
    move_session_id: Option<String>,
    weak: slint::Weak<AppWindow>,
) {
    let (Some(voice), Some(preferences)) = (ACTIVE_VOICE.get(), VOICE_PREFERENCES.get()) else {
        return;
    };
    // A replacement grant is a hard authorization boundary. Stop the old
    // transport first so stale SPEAK/STREAM rights cannot survive a move or
    // permission change, then ask the home instance for the current grant.
    let handle = {
        let mut active = voice.lock().await;
        if active
            .as_ref()
            .is_none_or(|handle| handle.move_session_id != move_session_id)
        {
            return;
        }
        active.take()
    };
    let Some(handle) = handle else { return };
    handle.leave().await;
    let preferences = preferences.read().await;
    let capture_settings = kaede_audio::CaptureSettings {
        device_id: preferences.input_device.clone(),
        mode: preferences.mode,
        vad_threshold: preferences.vad_threshold,
        ..kaede_audio::CaptureSettings::default()
    };
    let output_device = preferences.output_device.clone();
    drop(preferences);
    match kaede_voice::join_channel(
        account.api.clone(),
        &channel,
        capture_settings,
        output_device,
    )
    .await
    {
        Ok(handle) => {
            activate_voice_handle(handle, voice.clone(), weak, Some((account, channel))).await;
        }
        Err(error) => {
            let message = friendly_error(&error.to_string());
            let _ = weak.upgrade_in_event_loop(move |window| {
                window.set_voice_status("disconnected".into());
                window.set_voice_can_speak(false);
                window.set_error_message(
                    format!("Voice authorization changed and reconnection failed: {message}")
                        .into(),
                );
            });
        }
    }
}

async fn follow_pending_deep_link(account: &Arc<AccountRuntime>, weak: &slint::Weak<AppWindow>) {
    let Some(pending) = PENDING_DEEP_LINK.get() else {
        return;
    };
    let Some(link) = pending.lock().await.take() else {
        return;
    };
    let mut invite_guild = None;
    let (result, requested_channel) = match link {
        DeepLink::Channel(channel) => {
            let result = account.load_channel(&channel).await.map(|_| ());
            (result, Some(channel))
        }
        DeepLink::Message { channel, message } => {
            let result = account
                .load_around_message(&channel, &message)
                .await
                .map(|_| ());
            (result, Some(channel))
        }
        DeepLink::Invite(code) => {
            let preview = account.service.preview_invite(&code).await.ok();
            invite_guild = preview
                .as_ref()
                .and_then(|value| value.get("guild"))
                .map(|guild| entity_field(guild, &["id"], &["origin_domain", "domain"]))
                .and_then(|value| value.parse::<EntityRef>().ok());
            let preview_channel = preview.as_ref().and_then(|value| {
                let channel = string_field(value, &["channel_id"]);
                let domain = invite_guild.as_ref()?.domain.to_string();
                (!channel.is_empty())
                    .then(|| format!("{channel}@{domain}"))
                    .and_then(|value| value.parse::<EntityRef>().ok())
            });
            let result = account
                .service
                .accept_invite(&code)
                .await
                .map_err(kaede_app::AccountError::from)
                .map(|_| ());
            if result.is_ok() {
                let _ = account.reconcile().await;
            }
            (result, preview_channel)
        }
    };
    if let Err(error) = result {
        show_account_error(weak, error.to_string());
        return;
    }
    let selected_channel = {
        let state = account.state.read().await;
        requested_channel.or_else(|| {
            invite_guild.as_ref().and_then(|guild| {
                state
                    .channels
                    .values()
                    .filter(|channel| channel.guild_key().as_ref() == Some(guild))
                    .filter(|channel| channel.kind != ChannelKind::Category)
                    .min_by_key(|channel| channel.position)
                    .map(kaede_core::Channel::key)
            })
        })
    };
    let Some(selected_channel) = selected_channel else {
        let state = account.state.read().await;
        let snapshot = ui_snapshot(&state);
        let _ = weak.upgrade_in_event_loop(move |window| apply_snapshot(&window, snapshot));
        return;
    };
    let needs_hydration = !account
        .state
        .read()
        .await
        .message_order
        .contains_key(&selected_channel);
    if needs_hydration && let Err(error) = account.load_channel(&selected_channel).await {
        show_account_error(weak, error.to_string());
        return;
    }
    let state = account.state.read().await;
    let guild = state
        .channels
        .get(&selected_channel)
        .and_then(kaede_core::Channel::guild_key)
        .map(|value| value.to_string())
        .unwrap_or_default();
    let snapshot = ui_snapshot(&state);
    let channel = selected_channel.to_string();
    let _ = weak.upgrade_in_event_loop(move |window| {
        window.set_selected_guild(guild.into());
        window.set_selected_channel(channel.into());
        apply_snapshot(&window, snapshot);
    });
}

struct UiSnapshot {
    current_user_id: String,
    current_user: String,
    profile_name: String,
    profile_handle: String,
    profile_status: String,
    profile_bio: String,
    profile_avatar: String,
    profile_banner: String,
    theme: String,
    dm_privacy: String,
    guilds: Vec<UiGuild>,
    channels: Vec<UiChannel>,
    messages: HashMap<String, Vec<UiMessage>>,
    typing: HashMap<String, String>,
    members: Vec<UiMember>,
    friends: Vec<UiFriend>,
    direct_messages: Vec<UiChannel>,
    emojis: Vec<UiEmoji>,
    roles: Vec<UiRole>,
    admin_members: Vec<UiAdminMember>,
    calls: Vec<UiCall>,
    voice_members: Vec<UiVoiceMember>,
    home_mentions: i32,
}

struct UiGuild {
    id: String,
    name: String,
    initials: String,
    icon: String,
    banner: String,
    mentions: i32,
    permissions: u64,
    owner: String,
    description: String,
    history_policy: String,
    sync_status: String,
    sync_error_code: String,
    history_sync_status: String,
    history_sync_error_code: String,
    history_sync_retry_after_ms: u64,
}
#[derive(Clone)]
struct UiChannel {
    id: String,
    name: String,
    kind: String,
    unread: bool,
    mentions: i32,
    can_send: bool,
    guild: Option<String>,
    topic: String,
    parent: String,
    position: i32,
    synced: bool,
    slow_mode: i32,
    history_truncated: bool,
    history_remote_available: bool,
    oldest_available_message: String,
}
#[derive(Clone)]
struct UiMessage {
    id: String,
    author: String,
    author_id: String,
    avatar: String,
    avatar_path: String,
    time: String,
    epoch: i64,
    date: String,
    day_label: String,
    body: String,
    pending: bool,
    retrying: bool,
    failed: bool,
    edited: bool,
    attachments: String,
    failure_reason: String,
    mine: bool,
    preview_url: String,
    preview_title: String,
    preview_description: String,
    preview_site: String,
    preview_media_type: String,
    attachment_preview: String,
    attachment_kind: String,
    reference_author: String,
    reference_body: String,
    gif_url: String,
}
struct UiMember {
    id: String,
    name: String,
    status: String,
    online: bool,
    presence: String,
    group: String,
    group_color: u32,
    group_rank: i32,
    guild: String,
    avatar_path: String,
}
struct UiFriend {
    id: String,
    initials: String,
    name: String,
    handle: String,
    status: String,
    relationship: String,
    online: bool,
    avatar_path: String,
}
struct UiEmoji {
    id: String,
    guild: String,
    value: String,
    label: String,
}
struct UiRole {
    id: String,
    guild: String,
    name: String,
    color: u32,
    position: i32,
    hoist: bool,
    mentionable: bool,
    permissions: u64,
    editable: bool,
}
struct UiAdminMember {
    id: String,
    guild: String,
    initials: String,
    name: String,
    handle: String,
    roles: String,
    timed_out: bool,
    manageable: bool,
    avatar_path: String,
}

struct UiCall {
    id: String,
    channel: String,
    state: String,
    caller: String,
}

struct UiVoiceMember {
    channel: String,
    id: String,
    name: String,
    avatar_path: String,
    muted: bool,
    deafened: bool,
}

struct UiAdminRecord {
    id: String,
    title: String,
    subtitle: String,
    kind: String,
}

fn day_label(time: &chrono::DateTime<chrono::Local>) -> String {
    let today = chrono::Local::now().date_naive();
    let date = time.date_naive();
    if date == today {
        "Today".to_owned()
    } else if today.pred_opt() == Some(date) {
        "Yesterday".to_owned()
    } else {
        time.format("%B %e, %Y").to_string().replace("  ", " ")
    }
}

fn render_message_body(state: &AppState, content: &str) -> String {
    let mut rendered = String::with_capacity(content.len());
    for span in kaede_core::markup::parse(content) {
        match span.kind {
            SpanKind::UserMention(reference) => {
                rendered.push('@');
                rendered.push_str(
                    state
                        .users
                        .get(&reference)
                        .map_or("unknown-user", kaede_core::User::label),
                );
            }
            SpanKind::RoleMention(reference) => {
                rendered.push('@');
                rendered.push_str(
                    state
                        .roles
                        .get(&reference)
                        .map_or("unknown-role", |role| role.name.as_str()),
                );
            }
            SpanKind::CustomEmoji { name, .. } => {
                rendered.push(':');
                rendered.push_str(&name);
                rendered.push(':');
            }
            SpanKind::Spoiler => rendered.push_str("████████"),
            SpanKind::CodeBlock => {
                rendered.push('\n');
                rendered.push_str(&span.text);
                rendered.push('\n');
            }
            SpanKind::Strike => {
                rendered.push('~');
                rendered.push_str(&span.text);
                rendered.push('~');
            }
            SpanKind::Text
            | SpanKind::Bold
            | SpanKind::Italic
            | SpanKind::InlineCode
            | SpanKind::Link => rendered.push_str(&span.text),
        }
    }
    rendered
}

fn delivery_status_guidance(status: Option<&str>, code: Option<&str>) -> &'static str {
    match (status, code) {
        (
            Some("retrying"),
            Some("KAED_FED_DM_STORAGE_QUOTA_EXCEEDED" | "FEDERATED_DM_STORAGE_QUOTA_EXCEEDED"),
        ) => "The receiving instance is making room in its direct-message cache",
        (Some("retrying"), Some("KAED_FED_INBOX_QUOTA_EXCEEDED")) => {
            "The receiving instance is temporarily full of federation events"
        }
        (Some("retrying"), Some("KAED_FED_REPLICA_QUOTA_EXCEEDED")) => {
            "The receiving instance's replica cache is temporarily full"
        }
        (
            Some("retrying"),
            Some(
                "KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED"
                | "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED",
            ),
        ) => "The receiving instance's remote-account storage is temporarily full",
        (
            Some("retrying"),
            Some(
                "KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED"
                | "FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED",
            ),
        ) => "The receiving instance's remote-server storage is temporarily full",
        (
            Some("failed"),
            Some("KAED_FED_DM_STORAGE_QUOTA_EXCEEDED" | "FEDERATED_DM_STORAGE_QUOTA_EXCEEDED"),
        ) => "The receiving instance reached its direct-message storage limit",
        (Some("failed"), Some("KAED_FED_INBOX_QUOTA_EXCEEDED")) => {
            "The receiving instance reached its federation-event limit"
        }
        (Some("failed"), Some("KAED_FED_REPLICA_QUOTA_EXCEEDED")) => {
            "The receiving instance's replica cache is full"
        }
        (
            Some("failed"),
            Some(
                "KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED"
                | "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED",
            ),
        ) => "The receiving instance's remote-account storage is full",
        (
            Some("failed"),
            Some(
                "KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED"
                | "FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED",
            ),
        ) => "The receiving instance's remote-server storage is full",
        _ => "",
    }
}

fn message_to_ui(
    state: &AppState,
    message: Message,
    current_user: Option<&EntityRef>,
) -> UiMessage {
    let message_key = message.key();
    let author = message.author.as_ref().map_or_else(
        || "Unknown author".to_owned(),
        |user| user.label().to_owned(),
    );
    let content = message.content.unwrap_or_else(|| {
        if message.e2ee.is_some() {
            "Encrypted message".to_owned()
        } else {
            String::new()
        }
    });
    let gif_url = klipy_gif_url(&content).unwrap_or_default();
    let preview = state.link_previews.get(&message_key);
    let reference = message
        .referenced_message_id
        .zip(message.referenced_message_domain.clone())
        .and_then(|(id, domain)| state.messages.get(&EntityRef::new(id, domain)));
    let local_time = message.created_at.with_timezone(&chrono::Local);
    UiMessage {
        id: message_key.to_string(),
        avatar: initials(&author),
        avatar_path: message.author.as_ref().map_or_else(String::new, |user| {
            public_asset_path(
                state,
                &user.origin_domain,
                user.avatar_hash.as_deref(),
                "thumbnail_128",
            )
        }),
        author,
        author_id: message
            .author
            .as_ref()
            .map_or_else(String::new, |user| user.key().to_string()),
        time: local_time.format("%H:%M").to_string(),
        epoch: message.created_at.timestamp(),
        date: local_time.format("%Y-%m-%d").to_string(),
        day_label: day_label(&local_time),
        body: render_message_body(state, &content),
        pending: message.delivery_status.as_deref() == Some("pending"),
        retrying: message.delivery_status.as_deref() == Some("retrying"),
        failed: message.delivery_status.as_deref() == Some("failed"),
        edited: message.edited_at.is_some(),
        attachments: message
            .attachments
            .iter()
            .map(|attachment| attachment.filename.as_str())
            .collect::<Vec<_>>()
            .join(" · "),
        failure_reason: delivery_status_guidance(
            message.delivery_status.as_deref(),
            message.delivery_error_code.as_deref(),
        )
        .to_owned(),
        mine: message
            .author
            .as_ref()
            .is_some_and(|author| current_user == Some(&author.key())),
        preview_url: preview.map_or_else(String::new, |item| item.url.clone()),
        preview_title: preview
            .and_then(|item| item.title.clone())
            .unwrap_or_default(),
        preview_description: preview
            .and_then(|item| item.description.clone())
            .unwrap_or_default(),
        preview_site: preview
            .and_then(|item| item.site_name.clone())
            .unwrap_or_default(),
        preview_media_type: preview
            .and_then(|item| item.media_type.clone())
            .unwrap_or_default(),
        attachment_preview: message
            .attachments
            .iter()
            .find_map(|attachment| attachment.local_path.clone())
            .unwrap_or_default(),
        attachment_kind: message
            .attachments
            .iter()
            .find_map(|attachment| {
                let content_type = attachment.content_type.as_deref()?;
                if content_type.starts_with("video/") {
                    Some("video".to_owned())
                } else if content_type.starts_with("image/") {
                    Some("image".to_owned())
                } else {
                    None
                }
            })
            .unwrap_or_default(),
        reference_author: reference
            .and_then(|message| message.author.as_ref())
            .map_or_else(
                || "Unknown author".to_owned(),
                |author| author.label().to_owned(),
            ),
        reference_body: reference.map_or_else(String::new, |message| {
            message.content.as_deref().map_or_else(
                || "Attachment".to_owned(),
                |content| render_message_body(state, content),
            )
        }),
        gif_url,
    }
}

fn klipy_gif_url(content: &str) -> Option<String> {
    let url = url::Url::parse(content.trim()).ok()?;
    let trusted_host = matches!(url.host_str(), Some("media.klipy.com" | "static.klipy.com"));
    (url.scheme() == "https"
        && trusted_host
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none())
    .then(|| url.to_string())
}

/// Populate the emoji picker grid: custom guild emojis first, then either
/// every category of the embedded Unicode catalog or the search results.
fn set_emoji_grid(window: &AppWindow, query: &str) {
    const COLUMNS: usize = 9;
    let needle = query.trim().to_lowercase();
    let mut rows: Vec<ModelRc<EmojiItem>> = Vec::new();
    let push_section = |rows: &mut Vec<ModelRc<EmojiItem>>, label: &str, items: Vec<EmojiItem>| {
        if items.is_empty() {
            return;
        }
        rows.push(ModelRc::from(Rc::new(VecModel::from(vec![EmojiItem {
            value: SharedString::default(),
            label: label.to_uppercase().into(),
            header: true,
        }]))));
        for chunk in items.chunks(COLUMNS) {
            rows.push(ModelRc::from(Rc::new(VecModel::from(chunk.to_vec()))));
        }
    };
    // The picker's category tabs filter with a "cat:<id>" query.
    let category = needle.strip_prefix("cat:").map(str::to_owned);
    let custom = window
        .get_emojis()
        .iter()
        .filter(|item| {
            category.is_none() && (needle.is_empty() || item.label.to_lowercase().contains(&needle))
        })
        .collect::<Vec<_>>();
    push_section(&mut rows, "Custom", custom);
    if let Some(category) = category {
        if let Some((id, label, _)) = emoji::CATEGORIES.iter().find(|(id, _, _)| *id == category) {
            let items = emoji::catalog()
                .iter()
                .filter(|emoji| emoji.g == *id)
                .map(|emoji| EmojiItem {
                    value: emoji.e.clone().into(),
                    label: format!(":{}:", emoji.s).into(),
                    header: false,
                })
                .collect::<Vec<_>>();
            push_section(&mut rows, label, items);
        }
    } else if needle.is_empty() {
        for (id, label, _) in emoji::CATEGORIES {
            let items = emoji::catalog()
                .iter()
                .filter(|emoji| emoji.g == id)
                .map(|emoji| EmojiItem {
                    value: emoji.e.clone().into(),
                    label: format!(":{}:", emoji.s).into(),
                    header: false,
                })
                .collect::<Vec<_>>();
            push_section(&mut rows, label, items);
        }
    } else {
        let items = emoji::search(&needle, 270)
            .into_iter()
            .map(|emoji| EmojiItem {
                value: emoji.e.clone().into(),
                label: format!(":{}:", emoji.s).into(),
                header: false,
            })
            .collect::<Vec<_>>();
        push_section(&mut rows, "Results", items);
    }
    window.set_emoji_grid(ModelRc::from(Rc::new(VecModel::from(rows))));
}

/// Download and cache a still image for a trusted KLIPY URL. Returns the
/// local path, or `None` when the URL is untrusted or the fetch fails.
async fn cached_gif_still(account: &Arc<AccountRuntime>, url: &str) -> Option<String> {
    klipy_gif_url(url)?;
    if let Some(path) = gif_stills()
        .read()
        .ok()
        .and_then(|map| map.get(url).cloned())
    {
        return Some(path);
    }
    let dir = GIF_STILL_DIR.get()?;
    let digest = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(url.as_bytes());
    let digest = &digest[digest.len().saturating_sub(96)..];
    let path = dir.join(format!("{digest}.img"));
    if tokio::fs::metadata(&path).await.is_err() {
        let parsed = url::Url::parse(url).ok()?;
        let bytes = account
            .api
            .get_public_bytes(&parsed, 8 * 1024 * 1024)
            .await
            .ok()?;
        if bytes.is_empty() {
            return None;
        }
        tokio::fs::create_dir_all(dir).await.ok()?;
        tokio::fs::write(&path, &bytes).await.ok()?;
    }
    let path = path.to_string_lossy().into_owned();
    if let Ok(mut map) = gif_stills().write() {
        map.insert(url.to_owned(), path.clone());
    }
    Some(path)
}

/// Build the GIF picker model on the UI thread from plain row data.
fn gif_model(rows: Vec<(String, String, String, bool)>) -> ModelRc<GifItem> {
    ModelRc::from(Rc::new(VecModel::from(
        rows.into_iter()
            .map(|(id, title, url, favorite)| GifItem {
                id: id.into(),
                title: title.into(),
                url: url.into(),
                favorite,
                has_preview: false,
                preview: Image::default(),
            })
            .collect::<Vec<_>>(),
    )))
}

/// Download preview stills for GIF picker rows and patch them into the model
/// as each one arrives, matched by row URL so stale results are ignored.
async fn hydrate_gif_previews(
    account: &Arc<AccountRuntime>,
    weak: &slint::Weak<AppWindow>,
    targets: Vec<(String, String)>,
) {
    for (key, fetch) in targets.into_iter().take(48) {
        let Some(path) = cached_gif_still(account, &fetch).await else {
            continue;
        };
        let _ = weak.upgrade_in_event_loop(move |window| {
            let gifs = window.get_gifs();
            for row in 0..gifs.row_count() {
                if let Some(mut item) = gifs.row_data(row)
                    && item.url.as_str() == key
                {
                    item.has_preview = true;
                    item.preview = load_ui_image(&path);
                    gifs.set_row_data(row, item);
                }
            }
        });
    }
}

/// Acknowledge the newest message of the open conversation when it is still
/// marked unread. `acknowledge_channel` clears the local read state, so this
/// converges instead of looping on its own `StateChanged` event.
async fn acknowledge_open_channel(account: Arc<AccountRuntime>, channel: EntityRef) {
    let newest = {
        let state = account.state.read().await;
        let Some(newest) = state
            .message_order
            .get(&channel)
            .and_then(|order| order.back())
            .cloned()
        else {
            return;
        };
        let unread = state.read_states.get(&channel).is_none_or(|read| {
            read.unread
                || read.mention_count > 0
                || read
                    .last_read_message_id
                    .is_none_or(|read_id| read_id < newest.id)
        });
        if !unread {
            return;
        }
        newest
    };
    if let Err(error) = account.acknowledge_channel(&channel, Some(&newest)).await {
        tracing::debug!(%error, %channel, "read acknowledgement was not persisted");
    }
}

/// Fetch still frames for any KLIPY GIF messages that are not cached yet and
/// re-render once they are available.
async fn refresh_gif_stills(account: Arc<AccountRuntime>, weak: slint::Weak<AppWindow>) {
    let urls = {
        let state = account.state.read().await;
        let cached = gif_stills().read().ok();
        state
            .messages
            .values()
            .filter_map(|message| message.content.as_deref().and_then(klipy_gif_url))
            .filter(|url| {
                cached
                    .as_ref()
                    .is_none_or(|map| !map.contains_key(url.as_str()))
            })
            .take(24)
            .collect::<std::collections::HashSet<_>>()
    };
    if urls.is_empty() {
        return;
    }
    let mut changed = false;
    for url in urls {
        if cached_gif_still(&account, &url).await.is_some() {
            changed = true;
        }
    }
    if changed {
        let snapshot = ui_snapshot(&*account.state.read().await);
        let _ = weak.upgrade_in_event_loop(move |window| apply_snapshot(&window, snapshot));
    }
}

fn message_item(item: UiMessage, compact: bool) -> MessageItem {
    // Slint cannot animate GIFs, so a sent KLIPY GIF renders its downloaded
    // still frame where the web client shows the animation.
    let gif_still = if item.gif_url.is_empty() {
        None
    } else {
        gif_stills()
            .read()
            .ok()
            .and_then(|stills| stills.get(&item.gif_url).cloned())
    };
    let (preview_path, preview_kind) = match &gif_still {
        Some(path) => (path.clone(), "gif".to_owned()),
        None => (item.attachment_preview.clone(), item.attachment_kind),
    };
    let body = if gif_still.is_some() {
        String::new()
    } else {
        item.body
    };
    MessageItem {
        kind: "message".into(),
        id: item.id.into(),
        author: item.author.into(),
        author_id: item.author_id.into(),
        avatar: item.avatar.into(),
        has_avatar: !item.avatar_path.is_empty(),
        avatar_image: load_ui_image(&item.avatar_path),
        time: item.time.into(),
        body: body.into(),
        pending: item.pending,
        retrying: item.retrying,
        failed: item.failed,
        edited: item.edited,
        attachments: item.attachments.into(),
        failure_reason: item.failure_reason.into(),
        mine: item.mine,
        preview_url: item.preview_url.into(),
        preview_title: item.preview_title.into(),
        preview_description: item.preview_description.into(),
        preview_site: item.preview_site.into(),
        preview_media_type: item.preview_media_type.into(),
        has_attachment_preview: !preview_path.is_empty(),
        attachment_preview: load_ui_image(&preview_path),
        attachment_kind: preview_kind.into(),
        has_reference: !item.reference_body.is_empty(),
        reference_author: item.reference_author.into(),
        reference_body: item.reference_body.into(),
        gif_url: item.gif_url.into(),
        compact,
    }
}

fn divider_item(kind: &str, label: &str) -> MessageItem {
    MessageItem {
        kind: kind.into(),
        id: SharedString::default(),
        author: SharedString::default(),
        author_id: SharedString::default(),
        avatar: SharedString::default(),
        has_avatar: false,
        avatar_image: Image::default(),
        time: label.into(),
        body: SharedString::default(),
        pending: false,
        retrying: false,
        failed: false,
        edited: false,
        attachments: SharedString::default(),
        failure_reason: SharedString::default(),
        mine: false,
        preview_url: SharedString::default(),
        preview_title: SharedString::default(),
        preview_description: SharedString::default(),
        preview_site: SharedString::default(),
        preview_media_type: SharedString::default(),
        has_attachment_preview: false,
        attachment_preview: Image::default(),
        attachment_kind: SharedString::default(),
        has_reference: false,
        reference_author: SharedString::default(),
        reference_body: SharedString::default(),
        gif_url: SharedString::default(),
        compact: false,
    }
}

fn ui_snapshot(state: &AppState) -> UiSnapshot {
    let current_user_id = state
        .current_user
        .as_ref()
        .map_or_else(String::new, |user| user.key().to_string());
    let current_user = state
        .current_user
        .as_ref()
        .map_or_else(String::new, |user| user.label().to_owned());
    let profile_name = state
        .current_user
        .as_ref()
        .and_then(|user| user.display_name.clone())
        .unwrap_or_else(|| current_user.clone());
    let profile_handle = state
        .current_user
        .as_ref()
        .map_or_else(String::new, |user| user.handle.clone());
    let profile_status = state
        .current_user
        .as_ref()
        .and_then(|user| user.custom_status.clone())
        .unwrap_or_default();
    let profile_bio = state
        .current_user
        .as_ref()
        .and_then(|user| user.bio.clone())
        .unwrap_or_default();
    let profile_avatar = state
        .current_user
        .as_ref()
        .map_or_else(String::new, |user| {
            public_asset_path(
                state,
                &user.origin_domain,
                user.avatar_hash.as_deref(),
                "thumbnail_128",
            )
        });
    let profile_banner = state
        .current_user
        .as_ref()
        .map_or_else(String::new, |user| {
            public_asset_path(
                state,
                &user.origin_domain,
                user.banner_hash.as_deref(),
                "thumbnail_1024",
            )
        });
    let theme = state
        .user_settings
        .as_ref()
        .map_or_else(|| "dark".to_owned(), |settings| settings.theme.clone());
    let dm_privacy = state.user_settings.as_ref().map_or_else(
        || "friends".to_owned(),
        |settings| settings.dm_privacy.clone(),
    );
    let mut guilds = state
        .guilds
        .values()
        .map(|guild| UiGuild {
            id: guild.key().to_string(),
            name: guild.name.clone(),
            initials: initials(&guild.name),
            icon: public_asset_path(
                state,
                &guild.origin_domain,
                guild.icon_hash.as_deref(),
                "thumbnail_128",
            ),
            banner: public_asset_path(
                state,
                &guild.origin_domain,
                guild.banner_hash.as_deref(),
                "thumbnail_1024",
            ),
            mentions: 0,
            permissions: guild.permissions.0,
            owner: EntityRef::new(guild.owner_id, guild.owner_domain.clone()).to_string(),
            description: guild.description.clone().unwrap_or_default(),
            history_policy: guild.federated_history_policy.clone(),
            sync_status: guild.sync_status.clone().unwrap_or_default(),
            sync_error_code: guild.sync_error_code.clone().unwrap_or_default(),
            history_sync_status: guild.history_sync_status.clone().unwrap_or_default(),
            history_sync_error_code: guild
                .history_sync_error_code
                .clone()
                .unwrap_or_default(),
            history_sync_retry_after_ms: guild.history_sync_retry_after_ms.unwrap_or_default(),
        })
        .collect::<Vec<_>>();
    guilds.sort_by(|left, right| left.name.to_lowercase().cmp(&right.name.to_lowercase()));
    let current_user_key = state.current_user.as_ref().map(kaede_core::User::key);
    let all_channels = state
        .channels
        .values()
        .map(|channel| UiChannel {
            id: channel.key().to_string(),
            name: channel.name.clone().unwrap_or_else(|| {
                if channel.kind == ChannelKind::DirectMessage {
                    let recipients = channel
                        .recipients
                        .iter()
                        .filter(|user| current_user_key.as_ref() != Some(&user.key()))
                        .map(|user| user.label().to_owned())
                        .collect::<Vec<_>>();
                    if recipients.is_empty() {
                        "Direct message".to_owned()
                    } else {
                        recipients.join(", ")
                    }
                } else {
                    "Untitled channel".to_owned()
                }
            }),
            kind: match channel.kind {
                ChannelKind::DirectMessage => "dm",
                ChannelKind::Voice => "voice",
                ChannelKind::Category => "category",
                _ => "text",
            }
            .to_owned(),
            unread: !matches!(channel.kind, ChannelKind::Voice | ChannelKind::Category)
                && channel.last_message_id.is_some_and(|last| {
                    state.read_states.get(&channel.key()).is_none_or(|read| {
                        read.unread
                            || read
                                .last_read_message_id
                                .is_none_or(|read_id| read_id < last)
                    })
                }),
            mentions: state
                .read_states
                .get(&channel.key())
                .map_or(0, |read| read.mention_count as i32),
            // DM payloads carry no permission bits; like the web client,
            // direct messages are never permission-gated.
            can_send: channel.kind == ChannelKind::DirectMessage
                || channel.permissions.contains(permission::SEND_MESSAGES),
            guild: channel.guild_key().map(|guild| guild.to_string()),
            topic: channel.topic.clone().unwrap_or_default(),
            parent: channel
                .parent_id
                .zip(channel.parent_domain.clone())
                .map_or_else(String::new, |(id, domain)| {
                    EntityRef::new(id, domain).to_string()
                }),
            position: channel.position,
            synced: channel.permissions_synced,
            slow_mode: channel.rate_limit_per_user as i32,
            history_truncated: channel.history_truncated,
            history_remote_available: channel.history_remote_available,
            oldest_available_message: channel
                .oldest_available_message_ref
                .as_ref()
                .map_or_else(String::new, ToString::to_string),
        })
        .collect::<Vec<_>>();
    let mut channels = all_channels
        .iter()
        .filter(|channel| channel.guild.is_some())
        .cloned()
        .collect::<Vec<_>>();
    channels.sort_by_key(|channel| channel.position);
    let mut direct_messages = all_channels
        .into_iter()
        .filter(|channel| channel.guild.is_none())
        .collect::<Vec<_>>();
    direct_messages.sort_by(|left, right| left.name.to_lowercase().cmp(&right.name.to_lowercase()));
    // Badge math mirrors the web client: a guild tile shows the sum of its
    // channels' mention counts; the home tile counts each unread DM as at
    // least one.
    let mut guild_mentions: HashMap<String, i32> = HashMap::new();
    for channel in &channels {
        if let Some(guild) = &channel.guild {
            *guild_mentions.entry(guild.clone()).or_default() += channel.mentions;
        }
    }
    for guild in &mut guilds {
        guild.mentions = guild_mentions.get(&guild.id).copied().unwrap_or(0);
    }
    let home_mentions = direct_messages
        .iter()
        .filter(|channel| channel.unread)
        .map(|channel| channel.mentions.max(1))
        .sum::<i32>();
    let mut typing_names: HashMap<String, Vec<String>> = HashMap::new();
    for ((channel, user), started) in &state.typing {
        if current_user_key.as_ref() == Some(user)
            || chrono::Utc::now().signed_duration_since(*started) > chrono::Duration::seconds(10)
        {
            continue;
        }
        let name = state
            .users
            .get(user)
            .map_or_else(|| "Someone".to_owned(), |user| user.label().to_owned());
        typing_names
            .entry(channel.to_string())
            .or_default()
            .push(name);
    }
    let typing = typing_names
        .into_iter()
        .map(|(channel, mut names)| {
            names.sort();
            names.dedup();
            let label = match names.as_slice() {
                [name] => format!("{name} is typing…"),
                [first, second] => format!("{first} and {second} are typing…"),
                [first, second, third] => {
                    format!("{first}, {second}, and {third} are typing…")
                }
                [first, second, rest @ ..] => {
                    format!("{first}, {second}, and {} more are typing…", rest.len())
                }
                [] => String::new(),
            };
            (channel, label)
        })
        .collect();
    let messages = state
        .message_order
        .keys()
        .chain(
            state
                .pending_messages
                .values()
                .map(|message| &message.channel),
        )
        .cloned()
        .collect::<std::collections::HashSet<_>>()
        .into_iter()
        .map(|channel| {
            let mut rows = state
                .channel_messages(&channel)
                .into_iter()
                .map(|message| message_to_ui(state, message, current_user_key.as_ref()))
                .collect::<Vec<_>>();
            rows.extend(
                state
                    .pending_messages
                    .values()
                    .filter(|pending| pending.channel == channel)
                    .map(|pending| {
                        let author = state
                            .users
                            .get(&pending.author)
                            .map_or_else(|| "You".to_owned(), |user| user.label().to_owned());
                        let pending_local = pending.created_at.with_timezone(&chrono::Local);
                        UiMessage {
                            id: format!("pending:{}", pending.client_nonce),
                            avatar: initials(&author),
                            avatar_path: state.users.get(&pending.author).map_or_else(
                                String::new,
                                |user| {
                                    public_asset_path(
                                        state,
                                        &user.origin_domain,
                                        user.avatar_hash.as_deref(),
                                        "thumbnail_128",
                                    )
                                },
                            ),
                            author,
                            author_id: pending.author.to_string(),
                            time: pending_local.format("%H:%M").to_string(),
                            epoch: pending.created_at.timestamp(),
                            date: pending_local.format("%Y-%m-%d").to_string(),
                            day_label: day_label(&pending_local),
                            body: pending.content.clone(),
                            pending: pending.state != kaede_core::PendingMessageState::Failed,
                            retrying: false,
                            failed: pending.state == kaede_core::PendingMessageState::Failed,
                            edited: false,
                            attachments: String::new(),
                            failure_reason: pending.failure_reason.clone().unwrap_or_default(),
                            mine: true,
                            preview_url: String::new(),
                            preview_title: String::new(),
                            preview_description: String::new(),
                            preview_site: String::new(),
                            preview_media_type: String::new(),
                            attachment_preview: String::new(),
                            attachment_kind: String::new(),
                            reference_author: pending
                                .referenced_message_id
                                .as_ref()
                                .and_then(|reference| state.messages.get(reference))
                                .and_then(|message| message.author.as_ref())
                                .map_or_else(
                                    || "Unknown author".to_owned(),
                                    |author| author.label().to_owned(),
                                ),
                            reference_body: pending
                                .referenced_message_id
                                .as_ref()
                                .and_then(|reference| state.messages.get(reference))
                                .map_or_else(String::new, |message| {
                                    message.content.as_deref().map_or_else(
                                        || "Attachment".to_owned(),
                                        |content| render_message_body(state, content),
                                    )
                                }),
                            gif_url: klipy_gif_url(&pending.content).unwrap_or_default(),
                        }
                    }),
            );
            (channel.to_string(), rows)
        })
        .collect();
    let mut members = state
        .members
        .values()
        .map(|member| {
            let key = member.user.key();
            let presence = state.presences.get(&key);
            let status = presence.map_or(kaede_core::PresenceStatus::Offline, |value| value.status);
            let online = !matches!(
                status,
                kaede_core::PresenceStatus::Offline | kaede_core::PresenceStatus::Invisible
            );
            // Roster grouping mirrors the web client: a member appears under
            // their highest hoisted role while online, otherwise under
            // Online/Offline.
            let hoisted = state
                .roles
                .values()
                .filter(|role| {
                    role.guild_id == member.guild_id
                        && role.guild_domain == member.guild_domain
                        && role.hoist
                        && role.position > 0
                        && member.role_ids.contains(&role.id)
                })
                .max_by_key(|role| role.position);
            let (group, group_color, group_rank) = match hoisted {
                Some(role) if online => (role.name.clone(), role.color, role.position),
                _ if online => ("Online".to_owned(), 0, -1),
                _ => ("Offline".to_owned(), 0, -2),
            };
            UiMember {
                id: key.to_string(),
                name: member
                    .nickname
                    .clone()
                    .unwrap_or_else(|| member.user.label().to_owned()),
                status: presence
                    .and_then(|presence| presence.custom_status.clone())
                    .unwrap_or_default(),
                online,
                presence: match status {
                    kaede_core::PresenceStatus::Online => "online",
                    kaede_core::PresenceStatus::Idle => "idle",
                    kaede_core::PresenceStatus::Dnd => "dnd",
                    kaede_core::PresenceStatus::Invisible | kaede_core::PresenceStatus::Offline => {
                        "offline"
                    }
                }
                .to_owned(),
                group,
                group_color,
                group_rank,
                guild: EntityRef::new(member.guild_id, member.guild_domain.clone()).to_string(),
                avatar_path: public_asset_path(
                    state,
                    &member.user.origin_domain,
                    member.user.avatar_hash.as_deref(),
                    "thumbnail_128",
                ),
            }
        })
        .collect::<Vec<_>>();
    members.sort_by(|left, right| {
        right
            .group_rank
            .cmp(&left.group_rank)
            .then_with(|| left.name.to_lowercase().cmp(&right.name.to_lowercase()))
    });
    let mut friends = state
        .relationships
        .values()
        .map(|relationship| {
            let user = &relationship.user;
            let key = user.key();
            let presence = state.presences.get(&key);
            let online = presence.is_some_and(|presence| {
                !matches!(
                    presence.status,
                    kaede_core::PresenceStatus::Offline | kaede_core::PresenceStatus::Invisible
                )
            });
            UiFriend {
                id: key.to_string(),
                initials: initials(user.label()),
                name: user.label().to_owned(),
                handle: if user.handle.is_empty() {
                    format!("@{}@{}", user.username, user.origin_domain)
                } else {
                    user.handle.clone()
                },
                status: presence
                    .and_then(|presence| presence.custom_status.clone())
                    .or_else(|| user.custom_status.clone())
                    .unwrap_or_default(),
                relationship: match relationship.kind.as_str() {
                    "pending_in" => "incoming",
                    "pending_out" => "outgoing",
                    other => other,
                }
                .to_owned(),
                online,
                avatar_path: public_asset_path(
                    state,
                    &user.origin_domain,
                    user.avatar_hash.as_deref(),
                    "thumbnail_128",
                ),
            }
        })
        .collect::<Vec<_>>();
    friends.sort_by(|left, right| {
        right
            .online
            .cmp(&left.online)
            .then_with(|| left.name.to_lowercase().cmp(&right.name.to_lowercase()))
    });
    // The Unicode catalog is embedded client-side (see `emoji`); the
    // snapshot carries only the custom guild emojis.
    let mut emojis = state
        .emojis
        .values()
        .map(|emoji| UiEmoji {
            id: emoji.key().to_string(),
            guild: EntityRef::new(emoji.guild_id, emoji.guild_domain.clone()).to_string(),
            value: format!(
                "<{}:{}:{}@{}>",
                if emoji.animated { "a" } else { "" },
                emoji.name,
                emoji.id,
                emoji.origin_domain
            ),
            label: format!(":{}:", emoji.name),
        })
        .collect::<Vec<_>>();
    emojis.sort_by(|left, right| left.label.cmp(&right.label));
    let current_user_ref = state.current_user.as_ref().map(kaede_core::User::key);
    let mut roles = state
        .roles
        .values()
        .map(|role| UiRole {
            id: role.key().to_string(),
            guild: EntityRef::new(role.guild_id, role.guild_domain.clone()).to_string(),
            name: role.name.clone(),
            color: role.color,
            position: role.position,
            hoist: role.hoist,
            mentionable: role.mentionable,
            permissions: role.permissions.0,
            editable: role.name != "@everyone",
        })
        .collect::<Vec<_>>();
    roles.sort_by(|left, right| right.position.cmp(&left.position));
    let role_names = state
        .roles
        .values()
        .map(|role| (role.id, role.name.clone()))
        .collect::<HashMap<_, _>>();
    let guild_owners = state
        .guilds
        .values()
        .map(|guild| {
            (
                guild.key(),
                EntityRef::new(guild.owner_id, guild.owner_domain.clone()),
            )
        })
        .collect::<HashMap<_, _>>();
    let mut admin_members = state
        .members
        .values()
        .map(|member| {
            let guild = EntityRef::new(member.guild_id, member.guild_domain.clone());
            let user = member.user.key();
            let name = member
                .nickname
                .clone()
                .unwrap_or_else(|| member.user.label().to_owned());
            UiAdminMember {
                id: user.to_string(),
                guild: guild.to_string(),
                initials: initials(&name),
                name,
                handle: if member.user.profile_resolved {
                    member.user.handle.clone()
                } else {
                    "Profile unavailable · refreshes automatically".to_owned()
                },
                roles: member
                    .role_ids
                    .iter()
                    .filter_map(|role| role_names.get(role))
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", "),
                timed_out: member.timeout_indefinite
                    || member
                        .timeout_until
                        .is_some_and(|until| until > chrono::Utc::now()),
                manageable: current_user_ref.as_ref() != Some(&user)
                    && guild_owners.get(&guild) != Some(&user),
                avatar_path: public_asset_path(
                    state,
                    &member.user.origin_domain,
                    member.user.avatar_hash.as_deref(),
                    "thumbnail_128",
                ),
            }
        })
        .collect::<Vec<_>>();
    admin_members.sort_by(|left, right| left.name.to_lowercase().cmp(&right.name.to_lowercase()));
    let calls = state
        .calls
        .values()
        .map(|call| UiCall {
            id: call.key().to_string(),
            channel: call.channel_key().to_string(),
            state: call.state.clone(),
            caller: call.caller.to_string(),
        })
        .collect();
    let mut voice_members = state
        .voice_states
        .values()
        .filter_map(|voice| {
            let channel = EntityRef::new(voice.channel_id?, voice.channel_domain.clone()?);
            let user = EntityRef::new(voice.user_id, voice.user_domain.clone());
            let profile = state.users.get(&user);
            let name = profile.map_or_else(|| user.to_string(), |value| value.label().to_owned());
            let avatar_path = profile.map_or_else(String::new, |value| {
                public_asset_path(
                    state,
                    &value.origin_domain,
                    value.avatar_hash.as_deref(),
                    "thumbnail_128",
                )
            });
            Some(UiVoiceMember {
                channel: channel.to_string(),
                id: user.to_string(),
                name,
                avatar_path,
                muted: voice.self_mute || voice.server_mute,
                deafened: voice.self_deaf || voice.server_deaf,
            })
        })
        .collect::<Vec<_>>();
    voice_members.sort_by(|left, right| left.name.to_lowercase().cmp(&right.name.to_lowercase()));
    UiSnapshot {
        current_user_id,
        current_user,
        profile_name,
        profile_handle,
        profile_status,
        profile_bio,
        profile_avatar,
        profile_banner,
        theme,
        dm_privacy,
        guilds,
        channels,
        messages,
        typing,
        members,
        friends,
        direct_messages,
        emojis,
        roles,
        admin_members,
        calls,
        voice_members,
        home_mentions,
    }
}

fn apply_snapshot(window: &AppWindow, snapshot: UiSnapshot) {
    window.set_current_user_initials(initials(&snapshot.current_user).into());
    window.set_current_user(snapshot.current_user.into());
    window.set_current_profile_name(snapshot.profile_name.into());
    window.set_current_profile_handle(snapshot.profile_handle.clone().into());
    window.set_current_profile_status(snapshot.profile_status.into());
    window.set_current_profile_bio(snapshot.profile_bio.into());
    window.set_current_profile_has_avatar(!snapshot.profile_avatar.is_empty());
    window.set_current_profile_avatar(if snapshot.profile_avatar.is_empty() {
        Image::default()
    } else {
        Image::load_from_path(Path::new(&snapshot.profile_avatar)).unwrap_or_default()
    });
    window.set_current_profile_has_banner(!snapshot.profile_banner.is_empty());
    window.set_current_profile_banner(if snapshot.profile_banner.is_empty() {
        Image::default()
    } else {
        Image::load_from_path(Path::new(&snapshot.profile_banner)).unwrap_or_default()
    });
    window.set_settings_theme(snapshot.theme.into());
    window.set_settings_dm_privacy(snapshot.dm_privacy.into());
    let selected_guild = window.get_selected_guild().to_string();
    window.set_guild_sync_paused(false);
    window.set_guild_sync_paused_title("Replica cache is full".into());
    window.set_guild_sync_paused_message(
        "Recent guild updates may be missing. Ask this instance's administrator to free space or raise the limit."
            .into(),
    );
    window.set_guild_history_warning(SharedString::default());
    if let Some(guild) = snapshot
        .guilds
        .iter()
        .find(|guild| guild.id == selected_guild)
    {
        window.set_guild_title(guild.name.clone().into());
        window.set_guild_description(guild.description.clone().into());
        window.set_guild_history_policy(guild.history_policy.clone().into());
        window.set_guild_has_icon(!guild.icon.is_empty());
        window.set_guild_icon(load_ui_image(&guild.icon));
        window.set_guild_has_banner(!guild.banner.is_empty());
        window.set_guild_banner(load_ui_image(&guild.banner));
        window.set_guild_sync_paused(guild.sync_status == "quota_paused");
        match guild.sync_error_code.as_str() {
            "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED"
            | "KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED" => {
                window.set_guild_sync_paused_title("Remote account cache is full".into());
                window.set_guild_sync_paused_message(
                    "This instance cannot cache another account needed by the guild. Ask its administrator to raise or free the identity cache limit."
                        .into(),
                );
            }
            "FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED"
            | "KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED" => {
                window.set_guild_sync_paused_title("Remote server cache is full".into());
                window.set_guild_sync_paused_message(
                    "This instance cannot cache another server needed by the guild. Ask its administrator to raise or free the server cache limit."
                        .into(),
                );
            }
            _ => {}
        }
        if guild.history_sync_status == "retrying" {
            let seconds = guild.history_sync_retry_after_ms.div_ceil(1_000).max(1);
            let prefix = if guild.history_sync_error_code == "KAED_FED_HISTORY_CAPACITY" {
                "Older guild history is waiting for remote capacity."
            } else {
                "Older guild history is temporarily delayed."
            };
            window.set_guild_history_warning(
                format!(
                    "{prefix} Recent messages remain available; Kaede retries automatically in about {seconds}s."
                )
                .into(),
            );
        } else if guild.history_sync_status == "failed" {
            let warning = match guild.history_sync_error_code.as_str() {
                "FEDERATED_GUILD_HISTORY_LIMIT_REACHED" => {
                    "Older guild history stopped at this instance's safety limit. Recent and new messages still work; ask the administrator to raise the federation history limit if needed."
                }
                "FEDERATED_GUILD_HISTORY_REJECTED" => {
                    "Older guild history could not be safely imported from the remote instance. Recent and new messages still work."
                }
                _ => {
                    "Older guild history could not be imported. Recent and new messages still work; contact the instance administrator if it stays unavailable."
                }
            };
            window.set_guild_history_warning(warning.into());
        }
        let administrator = guild.permissions & permission::ADMINISTRATOR != 0;
        window.set_can_manage_guild(
            administrator || guild.permissions & permission::MANAGE_GUILD != 0,
        );
        window.set_can_manage_channels(
            administrator || guild.permissions & permission::MANAGE_CHANNELS != 0,
        );
        window.set_can_manage_roles(
            administrator || guild.permissions & permission::MANAGE_ROLES != 0,
        );
        window.set_can_kick(administrator || guild.permissions & permission::KICK_MEMBERS != 0);
        window.set_can_ban(administrator || guild.permissions & permission::BAN_MEMBERS != 0);
        window.set_can_timeout(
            administrator || guild.permissions & permission::MODERATE_MEMBERS != 0,
        );
        window.set_can_invite(administrator || guild.permissions & permission::CREATE_INVITE != 0);
        window
            .set_can_mute_voice(administrator || guild.permissions & permission::MUTE_MEMBERS != 0);
        window.set_can_deafen_voice(
            administrator || guild.permissions & permission::DEAFEN_MEMBERS != 0,
        );
        window
            .set_can_move_voice(administrator || guild.permissions & permission::MOVE_MEMBERS != 0);
        window.set_is_guild_owner(snapshot.current_user_id == guild.owner);
    }
    window.set_guilds(ModelRc::from(Rc::new(VecModel::from(
        snapshot
            .guilds
            .into_iter()
            .map(|item| GuildItem {
                id: item.id.into(),
                initials: item.initials.into(),
                name: item.name.into(),
                mentions: item.mentions,
                has_icon: !item.icon.is_empty(),
                icon: load_ui_image(&item.icon),
            })
            .collect::<Vec<_>>(),
    ))));
    window.set_home_mentions(snapshot.home_mentions);
    let channel_metadata = snapshot
        .channels
        .iter()
        .chain(snapshot.direct_messages.iter())
        .map(|channel| {
            (
                channel.id.clone(),
                (
                    channel.name.clone(),
                    channel.kind.clone(),
                    channel.can_send,
                    channel.topic.clone(),
                    channel.history_truncated,
                    channel.history_remote_available,
                    channel.oldest_available_message.clone(),
                ),
            )
        })
        .collect::<HashMap<_, _>>();
    let current_channel = window.get_selected_channel().to_string();
    // Sidebar ordering mirrors the web client: ungrouped channels first,
    // then each category followed by its children. Collapsed categories
    // hide their children; voice channels list their occupants inline.
    let guild_channels = snapshot
        .channels
        .iter()
        .filter(|channel| channel.guild.as_deref() == Some(selected_guild.as_str()))
        .collect::<Vec<_>>();
    let mut ordered: Vec<&UiChannel> = Vec::new();
    let mut ungrouped = guild_channels
        .iter()
        .copied()
        .filter(|channel| channel.kind != "category" && channel.parent.is_empty())
        .collect::<Vec<_>>();
    ungrouped.sort_by_key(|channel| channel.position);
    ordered.extend(ungrouped);
    let mut categories = guild_channels
        .iter()
        .copied()
        .filter(|channel| channel.kind == "category")
        .collect::<Vec<_>>();
    categories.sort_by_key(|channel| channel.position);
    for category in categories {
        ordered.push(category);
        let mut children = guild_channels
            .iter()
            .copied()
            .filter(|channel| channel.kind != "category" && channel.parent == category.id)
            .collect::<Vec<_>>();
        children.sort_by_key(|channel| channel.position);
        ordered.extend(children);
    }
    let collapsed = collapsed_categories()
        .lock()
        .map(|set| set.clone())
        .unwrap_or_default();
    let mut channels: Vec<ChannelItem> = Vec::new();
    for channel in &ordered {
        let is_category = channel.kind == "category";
        if !is_category && !channel.parent.is_empty() && collapsed.contains(&channel.parent) {
            continue;
        }
        let occupants = snapshot
            .voice_members
            .iter()
            .filter(|member| member.channel == channel.id)
            .collect::<Vec<_>>();
        channels.push(ChannelItem {
            id: channel.id.clone().into(),
            name: channel.name.clone().into(),
            kind: channel.kind.clone().into(),
            unread: channel.unread,
            mentions: channel.mentions,
            collapsed: is_category && collapsed.contains(&channel.id),
            voice_count: occupants.len() as i32,
            has_avatar: false,
            avatar: Image::default(),
            muted: false,
            deafened: false,
        });
        if channel.kind == "voice" {
            for occupant in occupants {
                channels.push(ChannelItem {
                    id: occupant.id.clone().into(),
                    name: occupant.name.clone().into(),
                    kind: "voice-member".into(),
                    unread: false,
                    mentions: 0,
                    collapsed: false,
                    voice_count: 0,
                    has_avatar: !occupant.avatar_path.is_empty(),
                    avatar: load_ui_image(&occupant.avatar_path),
                    muted: occupant.muted,
                    deafened: occupant.deafened,
                });
            }
        }
    }
    if current_channel.is_empty()
        && let Some(first) = channels
            .iter()
            .find(|channel| channel.kind == "text" || channel.kind == "dm")
    {
        window.set_selected_channel(first.id.clone());
    }
    let selected = window.get_selected_channel().to_string();
    if let Some(call) = snapshot.calls.iter().find(|call| call.channel == selected) {
        window.set_active_call_id(call.id.clone().into());
        window.set_active_call_state(call.state.clone().into());
        window.set_active_call_incoming(call.caller != snapshot.current_user_id);
    } else {
        window.set_active_call_id(SharedString::default());
        window.set_active_call_state(SharedString::default());
        window.set_active_call_incoming(false);
    }
    if let Some((
        name,
        kind,
        can_send,
        topic,
        history_truncated,
        history_remote_available,
        oldest_available,
    )) = channel_metadata.get(&selected)
    {
        window.set_selected_channel_name(name.clone().into());
        window.set_selected_channel_kind(kind.clone().into());
        window.set_can_send(*can_send);
        window.set_selected_channel_topic(topic.clone().into());
        let history_became_remote = *history_remote_available
            && (!window.get_history_truncated() || !window.get_history_remote_available());
        window.set_history_truncated(*history_truncated);
        window.set_history_remote_available(*history_remote_available);
        if history_became_remote {
            // A CHANNEL_UPDATE can announce that the local recent-history
            // window just began rolling. Re-open pagination exactly on that
            // transition; do not reopen it after an authority page later
            // proves that the true beginning was reached.
            window.set_history_complete(false);
        }
        if *history_truncated
            && !*history_remote_available
            && !oldest_available.is_empty()
            && snapshot
                .messages
                .get(&selected)
                .and_then(|messages| messages.first())
                .is_some_and(|message| message.id == oldest_available.as_str())
        {
            // The oldest locally retained row is already visible. Do not make
            // a futile request for history this replica deliberately evicted.
            window.set_history_complete(true);
        }
    } else {
        window.set_selected_channel_name(SharedString::default());
        window.set_selected_channel_kind(SharedString::default());
        window.set_can_send(false);
        window.set_selected_channel_topic(SharedString::default());
        window.set_history_truncated(false);
        window.set_history_remote_available(false);
    }
    window.set_channels(ModelRc::from(Rc::new(VecModel::from(channels))));
    let mut admin_channels = snapshot
        .channels
        .iter()
        .filter(|channel| channel.guild.as_deref() == Some(selected_guild.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    admin_channels.sort_by_key(|channel| channel.position);
    let selected_admin_channel_id = window.get_selected_admin_channel().to_string();
    let selected_admin_channel = admin_channels
        .iter()
        .find(|channel| channel.id == selected_admin_channel_id)
        .or_else(|| admin_channels.first());
    if let Some(channel) = selected_admin_channel {
        window.set_selected_admin_channel(channel.id.clone().into());
        window.set_selected_admin_channel_name(channel.name.clone().into());
        window.set_selected_admin_channel_topic(channel.topic.clone().into());
        window.set_selected_admin_channel_parent(channel.parent.clone().into());
        window.set_selected_admin_channel_slow_mode(channel.slow_mode);
        window.set_selected_admin_channel_synced(channel.synced);
    } else {
        window.set_selected_admin_channel(SharedString::default());
    }
    window.set_admin_channels(ModelRc::from(Rc::new(VecModel::from(
        admin_channels
            .into_iter()
            .map(|channel| AdminChannelItem {
                id: channel.id.into(),
                name: channel.name.into(),
                kind: channel.kind.into(),
                topic: channel.topic.into(),
                parent: channel.parent.into(),
                position: channel.position,
                synced: channel.synced,
                slow_mode: channel.slow_mode,
            })
            .collect::<Vec<_>>(),
    ))));
    // Timeline construction mirrors the web client: day dividers, a single
    // "New messages" divider captured at channel-open time, and compact rows
    // for same-author messages within a seven-minute window.
    let new_marker = window.get_new_marker_message().to_string();
    let mut rows: Vec<MessageItem> = Vec::new();
    let mut previous_message: Option<(String, i64)> = None;
    let mut previous_date = String::new();
    for item in snapshot.messages.get(&selected).into_iter().flatten() {
        if item.date != previous_date {
            previous_date.clone_from(&item.date);
            rows.push(divider_item("day", &item.day_label));
            previous_message = None;
        }
        if !new_marker.is_empty() && item.id == new_marker {
            rows.push(divider_item("new", "New messages"));
            previous_message = None;
        }
        let compact = previous_message.as_ref().is_some_and(|(author, epoch)| {
            !item.author_id.is_empty()
                && author == &item.author_id
                && item.epoch.saturating_sub(*epoch) <= 420
        }) && item.reference_body.is_empty();
        previous_message = Some((item.author_id.clone(), item.epoch));
        rows.push(message_item(item.clone(), compact));
    }
    window.set_messages(ModelRc::from(Rc::new(VecModel::from(rows))));
    window.set_typing_indicator(
        snapshot
            .typing
            .get(&selected)
            .cloned()
            .unwrap_or_default()
            .into(),
    );
    window.set_voice_members(ModelRc::from(Rc::new(VecModel::from(
        snapshot
            .voice_members
            .into_iter()
            .filter(|member| member.channel == selected)
            .map(|member| kaede_ui::VoiceMemberItem {
                id: member.id.into(),
                initials: initials(&member.name).into(),
                has_avatar: !member.avatar_path.is_empty(),
                avatar: load_ui_image(&member.avatar_path),
                name: member.name.into(),
                muted: member.muted,
                deafened: member.deafened,
            })
            .collect::<Vec<_>>(),
    ))));
    // Roster rows are flattened with inline group headers ("{Role} — n",
    // "Online — n", "Offline — n") the way the web roster renders them.
    let roster_members = snapshot
        .members
        .into_iter()
        .filter(|item| item.guild == selected_guild)
        .collect::<Vec<_>>();
    let mut group_counts: HashMap<String, usize> = HashMap::new();
    for member in &roster_members {
        *group_counts.entry(member.group.clone()).or_default() += 1;
    }
    let mut roster_rows: Vec<MemberItem> = Vec::new();
    let mut current_group = String::new();
    for item in roster_members {
        if item.group != current_group {
            current_group.clone_from(&item.group);
            let count = group_counts.get(&item.group).copied().unwrap_or(0);
            roster_rows.push(MemberItem {
                id: SharedString::default(),
                initials: SharedString::default(),
                name: format!("{} — {count}", item.group.to_uppercase()).into(),
                status: SharedString::default(),
                online: false,
                presence: SharedString::default(),
                header: true,
                role_color: if item.group_color == 0 {
                    Color::from_rgb_u8(170, 160, 150)
                } else {
                    role_color(item.group_color)
                },
                has_avatar: false,
                avatar: Image::default(),
            });
        }
        roster_rows.push(MemberItem {
            id: item.id.into(),
            initials: initials(&item.name).into(),
            name: item.name.into(),
            status: item.status.into(),
            online: item.online,
            presence: item.presence.into(),
            header: false,
            role_color: Color::from_rgb_u8(244, 238, 229),
            has_avatar: !item.avatar_path.is_empty(),
            avatar: load_ui_image(&item.avatar_path),
        });
    }
    window.set_members(ModelRc::from(Rc::new(VecModel::from(roster_rows))));
    window.set_friends(ModelRc::from(Rc::new(VecModel::from(
        snapshot
            .friends
            .into_iter()
            .map(|item| FriendItem {
                id: item.id.into(),
                initials: item.initials.into(),
                name: item.name.into(),
                handle: item.handle.into(),
                status: item.status.into(),
                relationship: item.relationship.into(),
                online: item.online,
                has_avatar: !item.avatar_path.is_empty(),
                avatar: load_ui_image(&item.avatar_path),
            })
            .collect::<Vec<_>>(),
    ))));
    window.set_direct_messages(ModelRc::from(Rc::new(VecModel::from(
        snapshot
            .direct_messages
            .into_iter()
            .map(|item| ChannelItem {
                id: item.id.into(),
                name: item.name.into(),
                kind: item.kind.into(),
                unread: item.unread,
                mentions: item.mentions,
                collapsed: false,
                voice_count: 0,
                has_avatar: false,
                avatar: Image::default(),
                muted: false,
                deafened: false,
            })
            .collect::<Vec<_>>(),
    ))));
    window.set_emojis(ModelRc::from(Rc::new(VecModel::from(
        snapshot
            .emojis
            .iter()
            .map(|item| EmojiItem {
                value: item.value.clone().into(),
                label: item.label.clone().into(),
                header: false,
            })
            .collect::<Vec<_>>(),
    ))));
    window.set_emoji_records(ModelRc::from(Rc::new(VecModel::from(
        snapshot
            .emojis
            .into_iter()
            .filter(|item| item.guild == selected_guild)
            .map(|item| AdminRecordItem {
                id: item.id.into(),
                title: item.label.into(),
                subtitle: "Custom guild emoji".into(),
                kind: "emoji".into(),
            })
            .collect::<Vec<_>>(),
    ))));
    let selected_roles = snapshot
        .roles
        .into_iter()
        .filter(|role| role.guild == selected_guild)
        .collect::<Vec<_>>();
    let selected_role_id = window.get_selected_role().to_string();
    let selected_role = selected_roles
        .iter()
        .find(|role| role.id == selected_role_id)
        .or_else(|| selected_roles.first());
    if let Some(role) = selected_role {
        window.set_selected_role(role.id.clone().into());
        window.set_selected_role_name(role.name.clone().into());
        window.set_selected_role_color(format!("#{:06x}", role.color & 0x00ff_ffff).into());
        window.set_selected_role_hoist(role.hoist);
        window.set_selected_role_mentionable(role.mentionable);
        set_permission_model(window, role.permissions);
    } else {
        window.set_selected_role(SharedString::default());
        window.set_selected_role_name(SharedString::default());
        window.set_selected_role_color("#99aab5".into());
        set_permission_model(window, 0);
    }
    window.set_roles(ModelRc::from(Rc::new(VecModel::from(
        selected_roles
            .into_iter()
            .map(|role| RoleItem {
                id: role.id.into(),
                name: role.name.into(),
                color: role_color(role.color),
                position: role.position,
                hoist: role.hoist,
                mentionable: role.mentionable,
                editable: role.editable,
            })
            .collect::<Vec<_>>(),
    ))));
    window.set_admin_members(ModelRc::from(Rc::new(VecModel::from(
        snapshot
            .admin_members
            .into_iter()
            .filter(|member| member.guild == selected_guild)
            .map(|member| AdminMemberItem {
                id: member.id.into(),
                initials: member.initials.into(),
                name: member.name.into(),
                handle: member.handle.into(),
                roles: member.roles.into(),
                timed_out: member.timed_out,
                manageable: member.manageable,
                has_avatar: !member.avatar_path.is_empty(),
                avatar: load_ui_image(&member.avatar_path),
            })
            .collect::<Vec<_>>(),
    ))));
}

fn set_permission_model(window: &AppWindow, permissions: u64) {
    window.set_role_permissions(ModelRc::from(Rc::new(VecModel::from(
        kaede_protocol::PERMISSION_METADATA
            .iter()
            .filter(|metadata| metadata.resource_scopes.contains(&"guild"))
            .map(|metadata| PermissionItem {
                bit: metadata.bit.to_string().into(),
                label: metadata.label.into(),
                description: metadata.description.into(),
                group: metadata.group.into(),
                checked: permissions & metadata.bit != 0,
                danger: metadata.danger.into(),
            })
            .collect::<Vec<_>>(),
    ))));
}

async fn refresh_overwrite_models(
    account: &Arc<AccountRuntime>,
    weak: &slint::Weak<AppWindow>,
    guild: &EntityRef,
    channel: &EntityRef,
    masks: &OverwriteMasks,
) -> Result<(), kaede_app::AccountError> {
    let values = account.service.overwrites(guild, channel).await?;
    let mut current = HashMap::new();
    for value in values {
        let id = string_field(&value, &["target_id"]);
        let domain = string_field(&value, &["target_domain"]);
        let kind = string_field(&value, &["target_type"]);
        let target = if id.contains('@') || domain.is_empty() {
            id
        } else {
            format!("{id}@{domain}")
        };
        let allow = string_field(&value, &["allow"]).parse().unwrap_or(0);
        let deny = string_field(&value, &["deny"]).parse().unwrap_or(0);
        current.insert((target, kind), (allow, deny));
    }
    let state = account.state.read().await;
    let mut rows = state
        .roles
        .values()
        .filter(|role| role.guild_id == guild.id && role.guild_domain == guild.domain)
        .map(|role| {
            let id = role.key().to_string();
            let (allow, deny) = current
                .get(&(id.clone(), "role".to_owned()))
                .copied()
                .unwrap_or_default();
            OverwriteItem {
                id: id.into(),
                label: role.name.clone().into(),
                kind: "role".into(),
                allow: allow.to_string().into(),
                deny: deny.to_string().into(),
            }
        })
        .collect::<Vec<_>>();
    rows.extend(
        state
            .members
            .values()
            .filter(|member| member.guild_id == guild.id && member.guild_domain == guild.domain)
            .map(|member| {
                let id = member.user.key().to_string();
                let (allow, deny) = current
                    .get(&(id.clone(), "member".to_owned()))
                    .copied()
                    .unwrap_or_default();
                OverwriteItem {
                    id: id.into(),
                    label: member.user.label().into(),
                    kind: "member".into(),
                    allow: allow.to_string().into(),
                    deny: deny.to_string().into(),
                }
            }),
    );
    rows.sort_by(|left, right| {
        let left_everyone = left.label.as_str() == "@everyone";
        let right_everyone = right.label.as_str() == "@everyone";
        right_everyone
            .cmp(&left_everyone)
            .then_with(|| left.kind.cmp(&right.kind))
            .then_with(|| left.label.to_lowercase().cmp(&right.label.to_lowercase()))
    });
    drop(state);
    *masks.write().await = current;
    let _ = weak.upgrade_in_event_loop(move |window| {
        window.set_selected_overwrite(SharedString::default());
        window.set_selected_overwrite_kind(SharedString::default());
        window.set_overwrites(ModelRc::from(Rc::new(VecModel::from(rows))));
        window.set_overwrite_permissions(ModelRc::from(Rc::new(VecModel::default())));
    });
    Ok(())
}

fn overwrite_permission_model(allow: u64, deny: u64) -> ModelRc<OverwritePermissionItem> {
    ModelRc::from(Rc::new(VecModel::from(overwrite_permission_rows(
        allow, deny,
    ))))
}

fn overwrite_permission_rows(allow: u64, deny: u64) -> Vec<OverwritePermissionItem> {
    kaede_protocol::PERMISSION_METADATA
        .iter()
        .filter(|metadata| metadata.resource_scopes.contains(&"channel"))
        .map(|metadata| OverwritePermissionItem {
            bit: metadata.bit.to_string().into(),
            label: metadata.label.into(),
            description: metadata.description.into(),
            state: if deny & metadata.bit != 0 {
                "deny"
            } else if allow & metadata.bit != 0 {
                "allow"
            } else {
                "inherit"
            }
            .into(),
        })
        .collect()
}

fn role_color(value: u32) -> Color {
    Color::from_rgb_u8(
        ((value >> 16) & 0xff) as u8,
        ((value >> 8) & 0xff) as u8,
        (value & 0xff) as u8,
    )
}

fn public_asset_path(
    state: &AppState,
    origin: &kaede_protocol::Domain,
    content_hash: Option<&str>,
    variant: &str,
) -> String {
    let Some(content_hash) = content_hash else {
        return String::new();
    };
    state
        .public_assets
        .get(&format!("{origin}/{content_hash}/{variant}"))
        .cloned()
        .unwrap_or_default()
}

fn load_ui_image(path: &str) -> Image {
    if path.is_empty() {
        Image::default()
    } else {
        Image::load_from_path(Path::new(path)).unwrap_or_default()
    }
}

fn record_model(records: Vec<UiAdminRecord>) -> ModelRc<AdminRecordItem> {
    ModelRc::from(Rc::new(VecModel::from(
        records
            .into_iter()
            .map(|record| AdminRecordItem {
                id: record.id.into(),
                title: record.title.into(),
                subtitle: record.subtitle.into(),
                kind: record.kind.into(),
            })
            .collect::<Vec<_>>(),
    )))
}

fn admin_records(value: &serde_json::Value, kind: &str) -> Vec<UiAdminRecord> {
    let rows = value
        .as_array()
        .or_else(|| value.get("items").and_then(serde_json::Value::as_array))
        .map(Vec::as_slice)
        .unwrap_or_default();
    rows.iter()
        .map(|row| {
            let id = match kind {
                "instance-ban" => string_field(row, &["domain", "origin_domain"]),
                "invite" => string_field(row, &["code"]),
                _ => entity_field(
                    row,
                    &["user", "target", "id"],
                    &["origin_domain", "user_domain"],
                ),
            };
            let title = match kind {
                "audit" => string_field(row, &["action_name", "action", "event_type"]),
                "instance-ban" => string_field(row, &["domain", "origin_domain"]),
                "webhook" => string_field(row, &["name"]),
                "invite" => string_field(row, &["code"]),
                _ => string_field(row, &["username", "display_name", "handle", "user_id"]),
            };
            let subtitle = [
                string_field(row, &["reason"]),
                string_field(row, &["created_at"]),
                string_field(row, &["expires_at"]),
            ]
            .into_iter()
            .filter(|value| !value.is_empty())
            .collect::<Vec<_>>()
            .join(" · ");
            UiAdminRecord {
                id,
                title: if title.is_empty() {
                    "Record".to_owned()
                } else {
                    title
                },
                subtitle,
                kind: kind.to_owned(),
            }
        })
        .collect()
}

fn string_field(value: &serde_json::Value, names: &[&str]) -> String {
    names
        .iter()
        .find_map(|name| value.get(*name))
        .and_then(|value| {
            value
                .as_str()
                .map(ToOwned::to_owned)
                .or_else(|| value.as_u64().map(|value| value.to_string()))
        })
        .unwrap_or_default()
}

fn entity_field(value: &serde_json::Value, id_names: &[&str], domain_names: &[&str]) -> String {
    for name in id_names {
        if let Some(object) = value.get(*name).and_then(serde_json::Value::as_object) {
            let id = object.get("id").and_then(serde_json::Value::as_str);
            let domain = object
                .get("origin_domain")
                .or_else(|| object.get("domain"))
                .and_then(serde_json::Value::as_str);
            if let (Some(id), Some(domain)) = (id, domain) {
                return format!("{id}@{domain}");
            }
        }
    }
    let id = string_field(value, id_names);
    let domain = string_field(value, domain_names);
    if id.contains('@') || domain.is_empty() {
        id
    } else {
        format!("{id}@{domain}")
    }
}

fn install_empty_models(window: &AppWindow) {
    window.set_guilds(ModelRc::from(Rc::new(VecModel::<GuildItem>::default())));
    window.set_channels(ModelRc::from(Rc::new(VecModel::<ChannelItem>::default())));
    window.set_messages(ModelRc::from(Rc::new(VecModel::<MessageItem>::default())));
    window.set_members(ModelRc::from(Rc::new(VecModel::<MemberItem>::default())));
    window.set_friends(ModelRc::from(Rc::new(VecModel::<FriendItem>::default())));
    window.set_direct_messages(ModelRc::from(Rc::new(VecModel::<ChannelItem>::default())));
    window.set_emojis(ModelRc::from(Rc::new(VecModel::<EmojiItem>::default())));
    window.set_gifs(ModelRc::from(Rc::new(VecModel::<GifItem>::default())));
    window.set_roles(ModelRc::from(Rc::new(VecModel::<RoleItem>::default())));
    window.set_profile_roles(ModelRc::from(Rc::new(
        VecModel::<ProfileRoleItem>::default(),
    )));
    window.set_overwrites(ModelRc::from(Rc::new(VecModel::<OverwriteItem>::default())));
    window.set_overwrite_permissions(ModelRc::from(Rc::new(
        VecModel::<OverwritePermissionItem>::default(),
    )));
    window.set_role_permissions(ModelRc::from(
        Rc::new(VecModel::<PermissionItem>::default()),
    ));
    window.set_admin_members(ModelRc::from(Rc::new(
        VecModel::<AdminMemberItem>::default(),
    )));
    window.set_voice_members(ModelRc::from(Rc::new(
        VecModel::<kaede_ui::VoiceMemberItem>::default(),
    )));
    window.set_voice_remote_videos(ModelRc::from(Rc::new(VecModel::<VideoTileItem>::default())));
    window.set_admin_channels(ModelRc::from(Rc::new(
        VecModel::<AdminChannelItem>::default(),
    )));
    window.set_audit_records(ModelRc::from(Rc::new(
        VecModel::<AdminRecordItem>::default(),
    )));
    window.set_webhook_records(ModelRc::from(Rc::new(
        VecModel::<AdminRecordItem>::default(),
    )));
    window.set_invite_records(ModelRc::from(Rc::new(
        VecModel::<AdminRecordItem>::default(),
    )));
    window.set_ban_records(ModelRc::from(Rc::new(
        VecModel::<AdminRecordItem>::default(),
    )));
    window.set_instance_ban_records(ModelRc::from(Rc::new(
        VecModel::<AdminRecordItem>::default(),
    )));
    window.set_emoji_records(ModelRc::from(Rc::new(
        VecModel::<AdminRecordItem>::default(),
    )));
    window.set_sessions(ModelRc::from(Rc::new(VecModel::<SessionItem>::default())));
    window.set_completions(ModelRc::from(
        Rc::new(VecModel::<CompletionItem>::default()),
    ));
    window.set_emoji_grid(ModelRc::from(Rc::new(
        VecModel::<ModelRc<EmojiItem>>::default(),
    )));
    window.set_pinned_messages(ModelRc::from(Rc::new(VecModel::<MessageItem>::default())));
}

fn first_navigable_channel(state: &AppState, guild: &EntityRef) -> Option<EntityRef> {
    state
        .channels
        .values()
        .filter(|channel| channel.guild_key().as_ref() == Some(guild))
        .filter(|channel| channel.kind != ChannelKind::Category)
        .min_by_key(|channel| channel.position)
        .map(kaede_core::Channel::key)
}

/// Hydrate the first channel shown when entering a guild.
///
/// Selecting a guild and selecting a channel are separate UI actions.  The
/// snapshot renderer may choose a default channel for presentation, but it
/// must never be responsible for network I/O.  Keeping the initial fetch here
/// prevents the shell from presenting a selected, empty channel until the
/// user clicks it a second time.
async fn hydrate_guild_landing(
    account: &AccountRuntime,
    guild: &EntityRef,
) -> Result<Option<EntityRef>, kaede_app::AccountError> {
    let selected = {
        let state = account.state.read().await;
        first_navigable_channel(&state, guild)
    };
    let Some(channel) = selected else {
        return Ok(None);
    };
    let kind = account
        .state
        .read()
        .await
        .channels
        .get(&channel)
        .map(|value| value.kind);
    match kind {
        Some(ChannelKind::Voice) => account.refresh_voice_occupancy(&channel).await?,
        _ => {
            account.load_channel(&channel).await?;
        }
    }
    Ok(Some(channel))
}

fn invite_code_from_input(input: &str) -> Option<String> {
    let trimmed = input.trim().trim_end_matches('/');
    if let Ok(url) = url::Url::parse(trimmed) {
        if !matches!(url.scheme(), "http" | "https") {
            return None;
        }
        let domain = kaede_protocol::Domain::parse(url.host_str()?).ok()?;
        let mut segments = url.path_segments()?;
        if segments.next()? != "invite" {
            return None;
        }
        let code = segments.next()?;
        if segments.next().is_some() || !is_invite_code(code) {
            return None;
        }
        return Some(format!("{code}@{domain}"));
    }

    let (code, domain) = trimmed
        .split_once('@')
        .map_or((trimmed, None), |(code, domain)| (code, Some(domain)));
    if !is_invite_code(code) {
        return None;
    }
    domain.map_or_else(
        || Some(code.to_owned()),
        |domain| {
            kaede_protocol::Domain::parse(domain)
                .ok()
                .map(|domain| format!("{code}@{domain}"))
        },
    )
}

fn is_invite_code(value: &str) -> bool {
    value.len() == 8 && value.bytes().all(|byte| byte.is_ascii_alphanumeric())
}

fn initials(value: &str) -> String {
    value
        .split_whitespace()
        .filter_map(|part| part.chars().next())
        .take(2)
        .collect::<String>()
        .to_uppercase()
}

fn desktop_device_name() -> &'static str {
    match std::env::consts::OS {
        "windows" => "Kaede Desktop on Windows",
        "macos" => "Kaede Desktop on macOS",
        "linux" => "Kaede Desktop on Linux",
        _ => "Kaede Desktop",
    }
}

fn friendly_error(error: &str) -> String {
    let normalized = error.to_ascii_uppercase();
    if normalized.contains("FEDERATED_DM_HISTORY_UNAVAILABLE") {
        "Older direct-message history is temporarily unavailable from the conversation's home instance. Your cached messages are safe; try loading earlier messages again in a moment."
            .to_owned()
    } else if normalized.contains("FEDERATED_DM_STORAGE_QUOTA_EXCEEDED")
        || normalized.contains("KAED_FED_DM_STORAGE_QUOTA_EXCEEDED")
    {
        "The receiving instance has reached its direct-message storage safety limit. Your message was not accepted; retry later or contact that instance's administrator."
            .to_owned()
    } else if normalized.contains("KAED_FED_REPLICA_QUOTA_EXCEEDED") {
        "This instance paused updates for the remote guild because its replica cache is full. Recent changes may be missing until an administrator frees space or raises the limit."
            .to_owned()
    } else if normalized.contains("FEDERATED_GUILD_HISTORY_LIMIT_REACHED") {
        "Older guild history stopped at this instance's configured safety limit. Recent and new messages still work; ask the instance administrator to raise the federation history limit if needed."
            .to_owned()
    } else if normalized.contains("FEDERATED_GUILD_HISTORY_REJECTED") {
        "Older guild history could not be safely imported from the remote instance. Recent and new messages still work."
            .to_owned()
    } else if normalized.contains("KAED_FED_HISTORY_CAPACITY")
        || normalized.contains("FEDERATED_GUILD_HISTORY_TEMPORARILY_UNAVAILABLE")
    {
        "Older guild history is temporarily delayed. Recent messages remain available and Kaede retries automatically."
            .to_owned()
    } else if normalized.contains("FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED")
        || normalized.contains("KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED")
    {
        "This instance cannot cache another remote account right now. Contact your instance administrator if this continues."
            .to_owned()
    } else if normalized.contains("FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED")
        || normalized.contains("KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED")
    {
        "This instance cannot cache another remote server right now. Contact your instance administrator if this continues."
            .to_owned()
    } else if normalized.contains("KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED") {
        "The receiving instance cannot accept another pending friend request right now. Your request was not delivered."
            .to_owned()
    } else if normalized.contains("KAED_FED_INBOX_QUOTA_EXCEEDED") {
        "The receiving instance is temporarily full of pending federation events. Retry in a little while."
            .to_owned()
    } else if normalized.contains("MFA") {
        "Multi-factor authentication is required. The MFA panel will open next.".to_owned()
    } else if normalized.contains("INVALID_CREDENTIALS") || normalized.contains("401") {
        "The username or password was not accepted.".to_owned()
    } else if normalized.contains("EMAIL_NOT_VERIFIED") {
        "Verify your email address before signing in.".to_owned()
    } else if normalized.contains("TURNSTILE") || normalized.contains("VERIFICATION") {
        "Verification could not be completed. Please try again.".to_owned()
    } else if normalized.contains("SLOW_MODE") || normalized.contains("RETRY_AFTER") {
        "Slow mode is active. Wait for the countdown before sending another message.".to_owned()
    } else if normalized.contains("TIMEOUT") || normalized.contains("TIMED_OUT") {
        "You are timed out in this guild and cannot perform that action yet.".to_owned()
    } else if normalized.contains("ROLE_HIERARCHY") || normalized.contains("HIERARCHY") {
        "Your highest role is not above the member or role you tried to manage.".to_owned()
    } else if normalized.contains("VOICE") && normalized.contains("CONNECT") {
        "You do not have permission to connect to this voice channel.".to_owned()
    } else if normalized.contains("SPEAK") {
        "You may listen in this voice channel, but you do not have permission to speak.".to_owned()
    } else if normalized.contains("STREAM") {
        "You do not have permission to share video or your screen in this voice channel.".to_owned()
    } else if normalized.contains("FORBIDDEN")
        || normalized.contains("PERMISSION")
        || normalized.contains("403")
    {
        "You do not have permission to perform that action.".to_owned()
    } else if normalized.contains("429") || normalized.contains("RATE_LIMIT") {
        "You are doing that too quickly. Please wait a moment and try again.".to_owned()
    } else if normalized.contains("CONFLICT") || normalized.contains("412") {
        "This item changed elsewhere. Refresh it before saving again.".to_owned()
    } else {
        error.to_owned()
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used)]

    use super::*;

    #[test]
    fn parses_message_and_channel_deep_links() {
        let message =
            parse_deep_link("kaede://open/message/7@home.example/9").expect("message deep link");
        let DeepLink::Message { channel, message } = message else {
            panic!("expected message link");
        };
        assert_eq!(channel.to_string(), "7@home.example");
        assert_eq!(message.to_string(), "9@home.example");

        let channel =
            parse_deep_link("kaede://open/channel/7@home.example").expect("channel deep link");
        assert!(matches!(channel, DeepLink::Channel(_)));
    }

    #[test]
    fn rejects_external_navigation_as_an_app_deep_link() {
        assert!(parse_deep_link("https://example.test/channel/7").is_err());
        assert!(parse_deep_link("kaede://evil/message/7@home.example/9").is_err());
    }

    #[test]
    fn translates_authorization_failures_for_people() {
        assert_eq!(
            friendly_error("403 FORBIDDEN"),
            "You do not have permission to perform that action."
        );
        assert!(friendly_error("ROLE_HIERARCHY").contains("highest role"));
        assert!(friendly_error("SLOW_MODE retry_after_ms=1000").contains("Slow mode"));
        assert!(
            friendly_error("507 FEDERATED_DM_STORAGE_QUOTA_EXCEEDED")
                .contains("receiving instance")
        );
        assert!(
            friendly_error("KAED_FED_REPLICA_QUOTA_EXCEEDED")
                .contains("Recent changes may be missing")
        );
        assert!(
            friendly_error("503 FEDERATED_DM_HISTORY_UNAVAILABLE")
                .contains("temporarily unavailable")
        );
        assert!(
            friendly_error("KAED_FED_HISTORY_CAPACITY").contains("retries automatically")
        );
        assert!(
            friendly_error("FEDERATED_GUILD_HISTORY_LIMIT_REACHED")
                .contains("configured safety limit")
        );
        assert!(
            friendly_error("FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED")
                .contains("remote account")
        );
        assert!(
            friendly_error("FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED")
                .contains("remote server")
        );
        assert!(
            friendly_error("KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED")
                .contains("friend request")
        );
    }

    #[test]
    fn accepts_invite_codes_and_urls_without_accepting_arbitrary_paths() {
        assert_eq!(
            invite_code_from_input("Ab12Cd34"),
            Some("Ab12Cd34".to_owned())
        );
        assert_eq!(
            invite_code_from_input("https://chat.example/invite/Ab12Cd34/"),
            Some("Ab12Cd34@chat.example".to_owned())
        );
        assert_eq!(
            invite_code_from_input("Ab12Cd34@Chat.Example"),
            Some("Ab12Cd34@chat.example".to_owned())
        );
        assert_eq!(invite_code_from_input("not a code"), None);
        assert_eq!(invite_code_from_input("https://chat.example/invite/"), None);
        assert_eq!(
            invite_code_from_input("https://chat.example/not-an-invite/Ab12Cd34"),
            None
        );
    }
}
