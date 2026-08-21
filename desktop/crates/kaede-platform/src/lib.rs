use std::{env, path::PathBuf};

use async_trait::async_trait;
use base64::{Engine as _, engine::general_purpose::STANDARD};
use futures_util::StreamExt;
use ring::signature;
use secrecy::{ExposeSecret, SecretString};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::task;
use url::Url;

const SERVICE: &str = "chat.kaede.desktop";

#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
pub struct KnownAccount {
    pub instance: String,
    pub account_key: String,
    pub label: String,
    pub last_used_unix_ms: i64,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, Eq, PartialEq)]
pub struct AccountRegistry {
    pub accounts: Vec<KnownAccount>,
}

#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
pub struct GifFavorite {
    pub id: String,
    pub title: String,
    pub url: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, Eq, PartialEq)]
pub struct GifFavorites {
    pub items: Vec<GifFavorite>,
}

impl GifFavorites {
    /// Load device-local GIF favorites, ignoring malformed or unsafe entries.
    ///
    /// # Errors
    ///
    /// Returns an error when an existing favorites file cannot be read or
    /// decoded. A missing file produces an empty collection.
    pub async fn load(paths: &PlatformPaths) -> Result<Self, PlatformError> {
        let path = paths.config_dir.join("gif-favorites.json");
        match tokio::fs::read(path).await {
            Ok(bytes) => {
                let mut favorites: Self =
                    serde_json::from_slice(&bytes).map_err(PlatformError::InvalidPreferences)?;
                favorites.items.retain(|item| {
                    !item.id.is_empty()
                        && item.id.len() <= 256
                        && item.title.len() <= 256
                        && Url::parse(&item.url).is_ok_and(|url| url.scheme() == "https")
                });
                favorites.items.truncate(100);
                Ok(favorites)
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(error) => Err(PlatformError::Io(error)),
        }
    }

    /// Atomically persist the bounded device-local GIF favorites collection.
    ///
    /// # Errors
    ///
    /// Returns an error when the configuration directory or favorites file
    /// cannot be created, written, or replaced.
    pub async fn save(&self, paths: &PlatformPaths) -> Result<(), PlatformError> {
        paths.create_private().await?;
        let path = paths.config_dir.join("gif-favorites.json");
        let temporary = paths.config_dir.join("gif-favorites.json.tmp");
        let bytes = serde_json::to_vec_pretty(self)?;
        tokio::fs::write(&temporary, bytes)
            .await
            .map_err(PlatformError::Io)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            tokio::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))
                .await
                .map_err(PlatformError::Io)?;
        }
        tokio::fs::rename(temporary, path)
            .await
            .map_err(PlatformError::Io)
    }

    pub fn toggle(&mut self, favorite: GifFavorite) -> bool {
        if let Some(index) = self.items.iter().position(|item| item.url == favorite.url) {
            self.items.remove(index);
            return false;
        }
        self.items.insert(0, favorite);
        self.items.truncate(100);
        true
    }
}

/// Preferences that must survive application upgrades and audio-device
/// renumbering.  Device labels are retained as a conservative fallback because
/// CPAL/Nokhwa identifiers are not guaranteed to be stable across reboots.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(default)]
pub struct DesktopPreferences {
    pub input_device: Option<DevicePreference>,
    pub output_device: Option<DevicePreference>,
    pub camera_device: Option<DevicePreference>,
    pub screen_source: Option<DevicePreference>,
    pub input_mode: InputModePreference,
    pub vad_threshold: f32,
    pub push_to_talk_hotkey: Option<String>,
    pub noise_suppression: NoiseSuppressionPreference,
    pub echo_cancellation: bool,
    pub automatic_gain_control: bool,
    pub screen_share_profile: ScreenShareProfilePreference,
    pub audio_quality: AudioQualityPreference,
    pub share_system_audio: bool,
}

impl Default for DesktopPreferences {
    fn default() -> Self {
        Self {
            input_device: None,
            output_device: None,
            camera_device: None,
            screen_source: None,
            input_mode: InputModePreference::VoiceActivity,
            vad_threshold: 0.025,
            push_to_talk_hotkey: None,
            noise_suppression: NoiseSuppressionPreference::Standard,
            echo_cancellation: true,
            automatic_gain_control: true,
            screen_share_profile: ScreenShareProfilePreference::Smooth,
            audio_quality: AudioQualityPreference::Standard,
            share_system_audio: true,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ScreenShareProfilePreference {
    DataSaver,
    #[default]
    Smooth,
    Sharp,
    Source,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum AudioQualityPreference {
    DataSaver,
    #[default]
    Standard,
    High,
    Studio,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum NoiseSuppressionPreference {
    Off,
    #[default]
    Standard,
    VoiceIsolation,
}

#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
pub struct DevicePreference {
    pub id: String,
    pub label: String,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum InputModePreference {
    PushToTalk,
    #[default]
    VoiceActivity,
}

impl DesktopPreferences {
    /// Loads desktop-only preferences, returning secure defaults when absent.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError`] for unreadable or malformed preference data.
    pub async fn load(paths: &PlatformPaths) -> Result<Self, PlatformError> {
        let path = paths.config_dir.join("preferences.json");
        match tokio::fs::read(&path).await {
            Ok(bytes) => {
                let mut value: Self =
                    serde_json::from_slice(&bytes).map_err(PlatformError::InvalidPreferences)?;
                value.vad_threshold = value.vad_threshold.clamp(0.001, 1.0);
                Ok(value)
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                Ok(Self { ..Self::default() })
            }
            Err(error) => Err(PlatformError::Io(error)),
        }
    }

    /// Atomically writes desktop-only preferences with private permissions.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError`] when serialization or durable replacement fails.
    pub async fn save(&self, paths: &PlatformPaths) -> Result<(), PlatformError> {
        paths.create_private().await?;
        let path = paths.config_dir.join("preferences.json");
        let temporary = paths.config_dir.join("preferences.json.tmp");
        let bytes = serde_json::to_vec_pretty(self)?;
        tokio::fs::write(&temporary, bytes)
            .await
            .map_err(PlatformError::Io)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            tokio::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))
                .await
                .map_err(PlatformError::Io)?;
        }
        tokio::fs::rename(temporary, path)
            .await
            .map_err(PlatformError::Io)
    }
}

impl AccountRegistry {
    /// Loads the non-secret list of accounts known to this installation.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError`] for unreadable or malformed registry data.
    pub async fn load(paths: &PlatformPaths) -> Result<Self, PlatformError> {
        let path = paths.config_dir.join("accounts.json");
        match tokio::fs::read(&path).await {
            Ok(bytes) => serde_json::from_slice(&bytes).map_err(PlatformError::InvalidRegistry),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(error) => Err(PlatformError::Io(error)),
        }
    }

    /// Atomically stores the non-secret account registry.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError`] when serialization or durable replacement fails.
    pub async fn save(&self, paths: &PlatformPaths) -> Result<(), PlatformError> {
        paths.create_private().await?;
        let path = paths.config_dir.join("accounts.json");
        let temporary = paths.config_dir.join("accounts.json.tmp");
        let bytes = serde_json::to_vec_pretty(self)?;
        tokio::fs::write(&temporary, bytes)
            .await
            .map_err(PlatformError::Io)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            tokio::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))
                .await
                .map_err(PlatformError::Io)?;
        }
        tokio::fs::rename(temporary, path)
            .await
            .map_err(PlatformError::Io)
    }

    pub fn remember(&mut self, account: KnownAccount) {
        self.accounts
            .retain(|candidate| candidate.account_key != account.account_key);
        self.accounts.push(account);
        self.accounts
            .sort_by_key(|candidate| std::cmp::Reverse(candidate.last_used_unix_ms));
    }

    #[must_use]
    pub fn most_recent(&self) -> Option<&KnownAccount> {
        self.accounts.first()
    }

    pub fn forget(&mut self, account_key: &str) {
        self.accounts
            .retain(|candidate| candidate.account_key != account_key);
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlatformPaths {
    pub data_dir: PathBuf,
    pub cache_dir: PathBuf,
    pub config_dir: PathBuf,
}

impl PlatformPaths {
    /// Resolves the operating system's private data, cache, and configuration paths.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError::MissingHomeDirectory`] when the platform does
    /// not provide the required base directory.
    pub fn discover() -> Result<Self, PlatformError> {
        #[cfg(unix)]
        let home = env::var_os("HOME").map(PathBuf::from);
        #[cfg(target_os = "windows")]
        let (data_dir, cache_dir, config_dir) = {
            let base = env::var_os("LOCALAPPDATA")
                .or_else(|| env::var_os("APPDATA"))
                .map(PathBuf::from)
                .ok_or(PlatformError::MissingHomeDirectory)?;
            let root = base.join("Kaede Chat");
            (root.join("Data"), root.join("Cache"), root.join("Config"))
        };
        #[cfg(target_os = "macos")]
        let (data_dir, cache_dir, config_dir) = {
            let home = home.ok_or(PlatformError::MissingHomeDirectory)?;
            (
                home.join("Library/Application Support/Kaede Chat"),
                home.join("Library/Caches/Kaede Chat"),
                home.join("Library/Preferences/Kaede Chat"),
            )
        };
        #[cfg(all(unix, not(target_os = "macos")))]
        let (data_dir, cache_dir, config_dir) = {
            let home = home.ok_or(PlatformError::MissingHomeDirectory)?;
            let data = env::var_os("XDG_DATA_HOME")
                .map_or_else(|| home.join(".local/share"), PathBuf::from);
            let cache =
                env::var_os("XDG_CACHE_HOME").map_or_else(|| home.join(".cache"), PathBuf::from);
            let config =
                env::var_os("XDG_CONFIG_HOME").map_or_else(|| home.join(".config"), PathBuf::from);
            (
                data.join("kaede-chat"),
                cache.join("kaede-chat"),
                config.join("kaede-chat"),
            )
        };
        Ok(Self {
            data_dir,
            cache_dir,
            config_dir,
        })
    }

    /// Creates all application directories and restricts them to the current user.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError`] if a directory or its permissions cannot be set.
    pub async fn create_private(&self) -> Result<(), PlatformError> {
        for path in [&self.data_dir, &self.cache_dir, &self.config_dir] {
            tokio::fs::create_dir_all(path)
                .await
                .map_err(PlatformError::Io)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                tokio::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
                    .await
                    .map_err(PlatformError::Io)?;
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct StoredSession {
    pub access_token: SecretString,
    pub refresh_token: SecretString,
}

#[async_trait]
pub trait CredentialVault: Send + Sync {
    async fn load(&self, account_key: &str) -> Result<Option<StoredSession>, PlatformError>;
    async fn save(&self, account_key: &str, session: &StoredSession) -> Result<(), PlatformError>;
    async fn delete(&self, account_key: &str) -> Result<(), PlatformError>;
}

#[derive(Clone, Default)]
pub struct SystemCredentialVault;

#[async_trait]
impl CredentialVault for SystemCredentialVault {
    async fn load(&self, account_key: &str) -> Result<Option<StoredSession>, PlatformError> {
        let account_key = account_key.to_owned();
        task::spawn_blocking(move || {
            let entry = keyring::Entry::new(SERVICE, &account_key)?;
            match entry.get_password() {
                Ok(value) => {
                    let stored: StoredSessionRecord =
                        serde_json::from_str(&value).map_err(PlatformError::InvalidVaultRecord)?;
                    Ok(Some(StoredSession {
                        access_token: SecretString::from(stored.access_token),
                        refresh_token: SecretString::from(stored.refresh_token),
                    }))
                }
                Err(keyring::Error::NoEntry) => Ok(None),
                Err(error) => Err(PlatformError::Keyring(error)),
            }
        })
        .await
        .map_err(PlatformError::Worker)?
    }

    async fn save(&self, account_key: &str, session: &StoredSession) -> Result<(), PlatformError> {
        let account_key = account_key.to_owned();
        // `secrecy` intentionally refuses to implement `Serialize` for secret
        // strings.  Expose them only at this boundary, immediately before the
        // record is handed to the operating-system credential vault.
        let serialized = serde_json::to_string(&StoredSessionRecord {
            access_token: session.access_token.expose_secret(),
            refresh_token: session.refresh_token.expose_secret(),
        })?;
        task::spawn_blocking(move || {
            keyring::Entry::new(SERVICE, &account_key)?.set_password(&serialized)?;
            Ok(())
        })
        .await
        .map_err(PlatformError::Worker)?
    }

    async fn delete(&self, account_key: &str) -> Result<(), PlatformError> {
        let account_key = account_key.to_owned();
        task::spawn_blocking(move || {
            let entry = keyring::Entry::new(SERVICE, &account_key)?;
            match entry.delete_credential() {
                Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
                Err(error) => Err(PlatformError::Keyring(error)),
            }
        })
        .await
        .map_err(PlatformError::Worker)?
    }
}

#[derive(Serialize, Deserialize)]
struct StoredSessionRecord<T = String> {
    access_token: T,
    refresh_token: T,
}

#[derive(Clone, Debug)]
pub struct Notification {
    pub title: String,
    pub body: String,
    pub deep_link: Option<Url>,
    pub sensitive: bool,
}

#[async_trait]
pub trait NotificationService: Send + Sync {
    async fn show(&self, notification: Notification) -> Result<(), PlatformError>;
}

/// Cross-platform native notification service. Notification delivery stays
/// inside the application process; it never launches a shell or script host.
#[derive(Clone, Default)]
pub struct SystemNotificationService;

#[async_trait]
impl NotificationService for SystemNotificationService {
    async fn show(&self, notification: Notification) -> Result<(), PlatformError> {
        let body = if notification.sensitive {
            "Open Kaede Chat to view this message.".to_owned()
        } else {
            notification.body
        };
        task::spawn_blocking(move || {
            let mut native = notify_rust::Notification::new();
            native
                .appname("Kaede Chat")
                .summary(&notification.title)
                .body(&body);
            #[cfg(target_os = "windows")]
            native.app_id(SERVICE);
            native
                .show()
                .map(|_| ())
                .map_err(|error| PlatformError::Other(error.to_string()))
        })
        .await
        .map_err(PlatformError::Worker)?
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct UpdateManifest {
    pub version: String,
    pub package_url: Url,
    /// Lowercase hexadecimal SHA-256 digest of the complete package.
    pub sha256: String,
    /// Base64 Ed25519 signature over the canonical unsigned manifest bytes.
    pub signature: String,
}

const MAX_UPDATE_PACKAGE_BYTES: usize = 512 * 1024 * 1024;

/// Downloads only independently signed update metadata and packages. Applying
/// the staged package remains platform-specific and must preserve the
/// operating system's code-signing checks.
#[derive(Clone)]
pub struct UpdateClient {
    http: reqwest::Client,
}

impl UpdateClient {
    /// Creates an HTTPS-only update client.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError`] if the native TLS HTTP client cannot be built.
    pub fn new() -> Result<Self, PlatformError> {
        let http = reqwest::Client::builder()
            .https_only(true)
            .redirect(reqwest::redirect::Policy::limited(3))
            .user_agent(concat!("Kaede-Desktop/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(PlatformError::UpdateTransport)?;
        Ok(Self { http })
    }

    /// Downloads and verifies a bounded signed update manifest.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError`] for transport, size, schema, or signature failures.
    pub async fn fetch_manifest(
        &self,
        url: Url,
        public_key: &[u8; 32],
    ) -> Result<UpdateManifest, PlatformError> {
        if url.scheme() != "https" {
            return Err(PlatformError::InvalidUpdateManifest);
        }
        let response = self
            .http
            .get(url)
            .send()
            .await
            .map_err(PlatformError::UpdateTransport)?
            .error_for_status()
            .map_err(PlatformError::UpdateTransport)?;
        if response
            .content_length()
            .is_some_and(|length| length > 64 * 1024)
        {
            return Err(PlatformError::InvalidUpdateManifest);
        }
        let bytes = response
            .bytes()
            .await
            .map_err(PlatformError::UpdateTransport)?;
        if bytes.len() > 64 * 1024 {
            return Err(PlatformError::InvalidUpdateManifest);
        }
        let manifest: UpdateManifest =
            serde_json::from_slice(&bytes).map_err(PlatformError::InvalidUpdateJson)?;
        manifest.verify(public_key)?;
        Ok(manifest)
    }

    /// Downloads a bounded package, verifies its digest, and stages it privately.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError`] for transport, size, hash, or filesystem failures.
    pub async fn download_verified(
        &self,
        manifest: &UpdateManifest,
        paths: &PlatformPaths,
    ) -> Result<PathBuf, PlatformError> {
        let response = self
            .http
            .get(manifest.package_url.clone())
            .send()
            .await
            .map_err(PlatformError::UpdateTransport)?
            .error_for_status()
            .map_err(PlatformError::UpdateTransport)?;
        if response
            .content_length()
            .is_some_and(|length| length > MAX_UPDATE_PACKAGE_BYTES as u64)
        {
            return Err(PlatformError::UpdateTooLarge);
        }
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(PlatformError::UpdateTransport)?;
            if bytes.len().saturating_add(chunk.len()) > MAX_UPDATE_PACKAGE_BYTES {
                return Err(PlatformError::UpdateTooLarge);
            }
            bytes.extend_from_slice(&chunk);
        }
        manifest.verify_package(&bytes)?;
        paths.create_private().await?;
        let safe_version = manifest
            .version
            .chars()
            .filter(|character| character.is_ascii_alphanumeric() || matches!(character, '.' | '-'))
            .take(64)
            .collect::<String>();
        if safe_version.is_empty() {
            return Err(PlatformError::InvalidUpdateManifest);
        }
        let target = paths
            .cache_dir
            .join(format!("update-{safe_version}.package"));
        let temporary = target.with_extension("package.tmp");
        tokio::fs::write(&temporary, bytes)
            .await
            .map_err(PlatformError::Io)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            tokio::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))
                .await
                .map_err(PlatformError::Io)?;
        }
        tokio::fs::rename(&temporary, &target)
            .await
            .map_err(PlatformError::Io)?;
        Ok(target)
    }
}

impl UpdateManifest {
    fn signed_bytes(&self) -> Vec<u8> {
        format!("{}\n{}\n{}\n", self.version, self.package_url, self.sha256).into_bytes()
    }

    /// Verifies the canonical manifest bytes using the compiled Ed25519 key.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError`] when fields are unsafe or the signature is invalid.
    pub fn verify(&self, public_key: &[u8; 32]) -> Result<(), PlatformError> {
        if self.package_url.scheme() != "https"
            || self.sha256.len() != 64
            || !self.sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(PlatformError::InvalidUpdateManifest);
        }
        let decoded = STANDARD
            .decode(&self.signature)
            .map_err(|_| PlatformError::InvalidUpdateManifest)?;
        signature::UnparsedPublicKey::new(&signature::ED25519, public_key)
            .verify(&self.signed_bytes(), &decoded)
            .map_err(|_| PlatformError::InvalidUpdateSignature)
    }

    /// Confirms that a complete staged package has the signed SHA-256 digest.
    ///
    /// # Errors
    ///
    /// Returns [`PlatformError::UpdateHashMismatch`] for mismatched package data.
    pub fn verify_package(&self, package: &[u8]) -> Result<(), PlatformError> {
        let actual = format!("{:x}", Sha256::digest(package));
        if actual == self.sha256.to_ascii_lowercase() {
            Ok(())
        } else {
            Err(PlatformError::UpdateHashMismatch)
        }
    }
}

/// Parse only Kaede-owned application links. Web links remain in the system
/// browser and arbitrary schemes are never handed to application routing.
///
/// # Errors
///
/// Returns [`PlatformError::InvalidDeepLink`] for unrecognized or malformed links.
pub fn parse_deep_link(value: &str) -> Result<DeepLink, PlatformError> {
    let url = Url::parse(value).map_err(|_| PlatformError::InvalidDeepLink)?;
    if url.scheme() != "kaede" || url.host_str() != Some("open") {
        return Err(PlatformError::InvalidDeepLink);
    }
    let segments = url
        .path_segments()
        .ok_or(PlatformError::InvalidDeepLink)?
        .collect::<Vec<_>>();
    match segments.as_slice() {
        ["channel", channel] => Ok(DeepLink::Channel(
            channel
                .parse()
                .map_err(|_| PlatformError::InvalidDeepLink)?,
        )),
        ["message", channel, message] => Ok(DeepLink::Message {
            channel: channel
                .parse()
                .map_err(|_| PlatformError::InvalidDeepLink)?,
            message: message
                .parse()
                .or_else(|_| {
                    let channel: kaede_protocol::EntityRef = channel.parse()?;
                    let id = message.parse()?;
                    Ok::<_, kaede_protocol::IdError>(kaede_protocol::EntityRef::new(
                        id,
                        channel.domain,
                    ))
                })
                .map_err(|_| PlatformError::InvalidDeepLink)?,
        }),
        ["invite", reference] => {
            let (code, domain) = reference
                .split_once('@')
                .map_or((*reference, None), |(code, domain)| (code, Some(domain)));
            if code.len() != 8 || !code.bytes().all(|byte| byte.is_ascii_alphanumeric()) {
                return Err(PlatformError::InvalidDeepLink);
            }
            let Some(domain) = domain else {
                return Ok(DeepLink::Invite(code.to_owned()));
            };
            let domain = kaede_protocol::Domain::parse(domain)
                .map_err(|_| PlatformError::InvalidDeepLink)?;
            Ok(DeepLink::Invite(format!("{code}@{domain}")))
        }
        _ => Err(PlatformError::InvalidDeepLink),
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DeepLink {
    Channel(kaede_protocol::EntityRef),
    Message {
        channel: kaede_protocol::EntityRef,
        message: kaede_protocol::EntityRef,
    },
    Invite(String),
}

#[derive(Clone, Debug)]
pub struct TurnstileChallenge {
    pub origin: Url,
    pub site_key: String,
    pub action: String,
    pub request_id: String,
}

#[async_trait]
pub trait TurnstileBroker: Send + Sync {
    async fn solve(&self, challenge: TurnstileChallenge) -> Result<SecretString, PlatformError>;
}

#[derive(Clone, Debug)]
pub struct RedactedSession<'a>(pub &'a StoredSession);

impl std::fmt::Display for RedactedSession<'_> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let _ = self.0.access_token.expose_secret();
        formatter.write_str("StoredSession([REDACTED])")
    }
}

#[derive(Debug, Error)]
pub enum PlatformError {
    #[error("platform credential store failed: {0}")]
    Keyring(#[from] keyring::Error),
    #[error("platform worker failed: {0}")]
    Worker(#[from] task::JoinError),
    #[error("credential record is invalid: {0}")]
    InvalidVaultRecord(serde_json::Error),
    #[error("account registry is invalid: {0}")]
    InvalidRegistry(serde_json::Error),
    #[error("desktop preferences are invalid: {0}")]
    InvalidPreferences(serde_json::Error),
    #[error("credential record could not be encoded: {0}")]
    Encode(#[from] serde_json::Error),
    #[error("the verification challenge was cancelled")]
    ChallengeCancelled,
    #[error("this platform capability is unavailable: {0}")]
    Unsupported(&'static str),
    #[error("platform operation failed: {0}")]
    Other(String),
    #[error("the platform home directory could not be determined")]
    MissingHomeDirectory,
    #[error("platform file operation failed: {0}")]
    Io(std::io::Error),
    #[error("the application link is invalid")]
    InvalidDeepLink,
    #[error("the update manifest is invalid")]
    InvalidUpdateManifest,
    #[error("the update manifest is not valid JSON: {0}")]
    InvalidUpdateJson(serde_json::Error),
    #[error("the update signature is invalid")]
    InvalidUpdateSignature,
    #[error("the downloaded update does not match its signed digest")]
    UpdateHashMismatch,
    #[error("the update package exceeds the desktop safety limit")]
    UpdateTooLarge,
    #[error("update transport failed: {0}")]
    UpdateTransport(reqwest::Error),
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;

    fn temporary_paths(label: &str) -> PlatformPaths {
        let root = std::env::temp_dir().join(format!(
            "kaede-platform-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0, |duration| duration.as_nanos())
        ));
        PlatformPaths {
            data_dir: root.join("data"),
            cache_dir: root.join("cache"),
            config_dir: root.join("config"),
        }
    }

    #[test]
    fn deep_links_require_composite_identifiers() {
        assert!(matches!(
            parse_deep_link("kaede://open/channel/42@chat.example"),
            Ok(DeepLink::Channel(_))
        ));
        assert!(parse_deep_link("https://chat.example/channel/42").is_err());
        assert!(parse_deep_link("kaede://open/channel/42").is_err());
        assert!(parse_deep_link("kaede://open/invite/not-valid!").is_err());
        assert!(matches!(
            parse_deep_link("kaede://open/invite/Ab12Cd34@Chat.Example"),
            Ok(DeepLink::Invite(value)) if value == "Ab12Cd34@chat.example"
        ));
    }

    #[test]
    fn update_package_hash_is_checked() {
        let package = b"signed package bytes";
        let manifest = UpdateManifest {
            version: "1.2.3".to_owned(),
            package_url: Url::parse("https://updates.example/kaede.pkg")
                .expect("static update URL"),
            sha256: format!("{:x}", Sha256::digest(package)),
            signature: String::new(),
        };
        assert!(manifest.verify_package(package).is_ok());
        assert!(manifest.verify_package(b"tampered").is_err());
    }

    #[tokio::test]
    async fn preferences_round_trip_all_native_device_choices() {
        let paths = temporary_paths("preferences");
        let preferences = DesktopPreferences {
            input_device: Some(DevicePreference {
                id: "input-id".to_owned(),
                label: "Studio microphone".to_owned(),
            }),
            output_device: Some(DevicePreference {
                id: "output-id".to_owned(),
                label: "Headphones".to_owned(),
            }),
            camera_device: Some(DevicePreference {
                id: "camera-id".to_owned(),
                label: "Camera".to_owned(),
            }),
            screen_source: Some(DevicePreference {
                id: "screen:4".to_owned(),
                label: "Main display".to_owned(),
            }),
            input_mode: InputModePreference::PushToTalk,
            vad_threshold: 0.04,
            push_to_talk_hotkey: Some("Shift+Backquote".to_owned()),
            noise_suppression: NoiseSuppressionPreference::VoiceIsolation,
            echo_cancellation: true,
            automatic_gain_control: false,
            screen_share_profile: ScreenShareProfilePreference::Sharp,
            audio_quality: AudioQualityPreference::High,
            share_system_audio: false,
        };
        preferences.save(&paths).await.expect("save preferences");
        assert_eq!(
            DesktopPreferences::load(&paths)
                .await
                .expect("load preferences"),
            preferences
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let metadata = std::fs::metadata(paths.config_dir.join("preferences.json"))
                .expect("preference metadata");
            assert_eq!(metadata.permissions().mode() & 0o077, 0);
        }
        let _ = tokio::fs::remove_dir_all(paths.config_dir.parent().expect("root path")).await;
    }
}
