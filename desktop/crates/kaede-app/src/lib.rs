//! Authenticated desktop account runtime.
//!
//! This is the only layer allowed to combine REST snapshots, gateway events,
//! credential refresh and local state. UI code receives immutable snapshots and
//! never mutates protocol entities directly.

#![allow(clippy::missing_errors_doc)]

use std::{
    collections::HashSet,
    future::Future,
    path::PathBuf,
    sync::{Arc, Weak},
    time::Duration,
};

use futures_util::{StreamExt, stream};
use kaede_api::{
    ApiClient, ApiClientError, InstanceEndpoint,
    service::{KaedeService, MessagePage},
};
use kaede_auth::{AuthError, LoginOutcome, RegistrationResult, SessionManager, StatusResult};
use kaede_cache::{Cache, CacheError};
use kaede_core::{
    AppState, Attachment, LinkPreview, Message, PendingMessage, PendingMessageState, ReduceError,
    Reduction, User, UserSettings, VoiceState,
};
use kaede_gateway::{GatewayCommand, GatewayStatus};
use kaede_platform::{Notification, PlatformError, PlatformPaths, SystemCredentialVault};
use kaede_protocol::{Domain, EntityRef};
use kaede_turnstile::EmbeddedTurnstile;
use secrecy::{ExposeSecret, SecretString};
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::{Mutex, RwLock, mpsc};

pub type NativeSession = SessionManager<SystemCredentialVault, EmbeddedTurnstile>;

const PUBLIC_ASSET_BATCH_SIZE: usize = 64;
const PUBLIC_ASSET_REVALIDATION_SLOTS: usize = 8;
// Sweep well inside the five-minute disk TTL so assets added between ticks do
// not remain trusted for another full TTL in an otherwise idle session.
const PUBLIC_ASSET_REFRESH_INTERVAL: Duration =
    Duration::from_secs(kaede_media::PUBLIC_ASSET_CACHE_TTL.as_secs() / 5);

#[derive(Clone, Debug)]
pub struct NativeMediaAsset {
    pub path: PathBuf,
    pub content_type: String,
}

#[derive(Clone, Debug)]
pub enum AccountEvent {
    StateChanged,
    GatewayStatus(GatewayStatus),
    ReconcileRequired,
    PurgeChannel(EntityRef),
    Notification(Notification),
    /// The server moved this account or replaced its voice authorization.
    /// Consumers must discard the current `LiveKit` session and obtain a fresh
    /// grant from the home instance before publishing any more media.
    VoiceReauthorization {
        channel: EntityRef,
        move_session_id: Option<String>,
    },
    Error(String),
}

pub struct AccountRuntime {
    pub api: ApiClient,
    pub service: KaedeService,
    pub session: Arc<NativeSession>,
    pub state: Arc<RwLock<AppState>>,
    pub commands: mpsc::Sender<GatewayCommand>,
    pub cache: Cache,
    media_dir: PathBuf,
    public_asset_dir: PathBuf,
    account_key: String,
    events: mpsc::UnboundedSender<AccountEvent>,
    reconcile_lock: Mutex<()>,
    public_asset_refresh_lock: Mutex<()>,
    public_asset_revalidations: Mutex<HashSet<String>>,
}

impl AccountRuntime {
    #[must_use]
    pub fn account_key(&self) -> &str {
        &self.account_key
    }

    #[must_use]
    pub fn instance(&self) -> &Domain {
        self.api.endpoint().domain()
    }

    /// This raw-password-shaped compatibility entry point always fails closed.
    #[deprecated(note = "use register_with_password_protocol with client-derived KDF-v2 material")]
    #[allow(clippy::unused_async)]
    pub async fn register(
        instance: &str,
        username: &str,
        email: Option<&str>,
        password: &str,
    ) -> Result<RegistrationResult, AccountError> {
        let _ = (instance, username, email, password);
        Err(AuthError::PasswordProtocolRequired.into())
    }

    /// Register with an authentication secret and full KDF-v2 context prepared
    /// by a trusted client. No raw account password may cross this boundary.
    pub async fn register_with_password_protocol(
        instance: &str,
        username: &str,
        email: Option<&str>,
        authentication_secret: &SecretString,
        password_kdf: &serde_json::Value,
    ) -> Result<RegistrationResult, AccountError> {
        let session = unauthenticated_session(instance, "registration")?;
        let config = session.config().await?;
        let challenge = if config.turnstile.enabled {
            let site_key = config
                .turnstile
                .site_key
                .ok_or(AccountError::ChallengeMisconfigured)?;
            Some(
                session
                    .solve_turnstile(
                        site_key,
                        "kaede-register-v1",
                        uuid::Uuid::new_v4().to_string(),
                    )
                    .await?,
            )
        } else {
            None
        };
        session
            .register_with_password_protocol(
                username,
                email,
                authentication_secret.expose_secret(),
                password_kdf,
                challenge.as_ref(),
            )
            .await
            .map_err(Into::into)
    }

    pub async fn request_password_reset(
        instance: &str,
        email: &str,
    ) -> Result<StatusResult, AccountError> {
        unauthenticated_session(instance, "recovery")?
            .forgot_password(email)
            .await
            .map_err(Into::into)
    }

    #[deprecated(
        note = "use reset_password_with_password_protocol with client-derived KDF-v2 material"
    )]
    #[allow(clippy::unused_async)]
    pub async fn reset_password(
        instance: &str,
        token: &str,
        password: &str,
    ) -> Result<StatusResult, AccountError> {
        let _ = (instance, token, password);
        Err(AuthError::PasswordProtocolRequired.into())
    }

    pub async fn reset_password_with_password_protocol(
        instance: &str,
        token: &str,
        authentication_secret: &SecretString,
        password_kdf: &serde_json::Value,
    ) -> Result<StatusResult, AccountError> {
        unauthenticated_session(instance, "recovery")?
            .reset_password_with_password_protocol(
                &SecretString::from(token.to_owned()),
                authentication_secret.expose_secret(),
                password_kdf,
            )
            .await
            .map_err(Into::into)
    }

    pub async fn verify_email(instance: &str, token: &str) -> Result<StatusResult, AccountError> {
        unauthenticated_session(instance, "verification")?
            .verify_email(&SecretString::from(token.to_owned()))
            .await
            .map_err(Into::into)
    }

    /// Restore a previously authenticated desktop account from the operating
    /// system credential vault. The access token is still validated by the
    /// home instance before any cached state is exposed to the UI.
    pub async fn restore(
        instance: &str,
        account_key: &str,
        events: mpsc::UnboundedSender<AccountEvent>,
    ) -> Result<Option<Arc<Self>>, AccountError> {
        let domain = Domain::parse(instance.trim())?;
        let endpoint = InstanceEndpoint::production(domain)?;
        let api = ApiClient::new(endpoint)?;
        let session = Arc::new(SessionManager::new(
            api.clone(),
            Arc::new(SystemCredentialVault),
            Arc::new(EmbeddedTurnstile),
            account_key.to_owned(),
        ));
        if !session.restore().await? {
            return Ok(None);
        }
        match Self::finish_connect(
            api.clone(),
            session.clone(),
            account_key.to_owned(),
            events.clone(),
        )
        .await
        {
            Ok(runtime) => Ok(Some(runtime)),
            Err(error) => {
                // An expired stored session must not trap the user in a restore
                // loop. Refresh once; if that fails the UI returns to login.
                tracing::info!(%error, "stored access token was not accepted; attempting refresh");
                if session.refresh().await.is_err() {
                    return Ok(None);
                }
                Self::finish_connect(api, session, account_key.to_owned(), events)
                    .await
                    .map(Some)
            }
        }
    }

    /// This raw-password-shaped compatibility entry point always fails closed.
    #[deprecated(note = "use connect_with_password_protocol with client-derived KDF-v2 material")]
    #[allow(clippy::unused_async)]
    pub async fn connect(
        instance: &str,
        identifier: &str,
        password: &str,
        device_name: &str,
        events: mpsc::UnboundedSender<AccountEvent>,
    ) -> Result<Arc<Self>, AccountError> {
        let _ = (instance, identifier, password, device_name, events);
        Err(AuthError::PasswordProtocolRequired.into())
    }

    /// Connect with an authentication secret and exact full KDF-v2 context
    /// prepared by a trusted client after selecting the instance origin.
    pub async fn connect_with_password_protocol(
        instance: &str,
        identifier: &str,
        authentication_secret: &SecretString,
        password_kdf: &serde_json::Value,
        device_name: &str,
        events: mpsc::UnboundedSender<AccountEvent>,
    ) -> Result<Arc<Self>, AccountError> {
        let domain = Domain::parse(instance.trim())?;
        let endpoint = InstanceEndpoint::production(domain.clone())?;
        let api = ApiClient::new(endpoint)?;
        let account_key = format!("{}@{}", identifier.trim().to_lowercase(), domain);
        let session = Arc::new(SessionManager::new(
            api.clone(),
            Arc::new(SystemCredentialVault),
            Arc::new(EmbeddedTurnstile),
            account_key.clone(),
        ));
        authenticate_prepared(
            &session,
            identifier,
            authentication_secret,
            password_kdf,
            device_name,
        )
        .await?;

        Self::finish_connect(api, session, account_key, events).await
    }

    /// Finish an authentication attempt that requires a TOTP or recovery code.
    ///
    /// The MFA ticket is kept only in process memory and is never written to
    /// preferences or the credential vault. Successful completion stores only
    /// the rotated access and refresh tokens in the platform credential store.
    pub async fn connect_mfa(
        instance: &str,
        identifier: &str,
        ticket: &SecretString,
        code: &str,
        device_name: &str,
        events: mpsc::UnboundedSender<AccountEvent>,
    ) -> Result<Arc<Self>, AccountError> {
        let domain = Domain::parse(instance.trim())?;
        let endpoint = InstanceEndpoint::production(domain.clone())?;
        let api = ApiClient::new(endpoint)?;
        let account_key = format!("{}@{}", identifier.trim().to_lowercase(), domain);
        let session = Arc::new(SessionManager::new(
            api.clone(),
            Arc::new(SystemCredentialVault),
            Arc::new(EmbeddedTurnstile),
            account_key.clone(),
        ));
        match session.complete_mfa(ticket, code, device_name).await? {
            LoginOutcome::Authenticated => {
                Self::finish_connect(api, session, account_key, events).await
            }
            LoginOutcome::MfaRequired(_) | LoginOutcome::ChallengeRequired => {
                Err(AccountError::UnexpectedMfaState)
            }
        }
    }

    #[allow(clippy::too_many_lines)]
    async fn finish_connect(
        api: ApiClient,
        session: Arc<NativeSession>,
        account_key: String,
        events: mpsc::UnboundedSender<AccountEvent>,
    ) -> Result<Arc<Self>, AccountError> {
        let paths = PlatformPaths::discover()?;
        paths.create_private().await?;
        let cache = Cache::open(&paths.cache_dir.join("entities.sqlite3"))?;
        let media_dir = paths.cache_dir.join("message-media");
        let public_asset_dir = paths
            .cache_dir
            .join("public-assets")
            .join(safe_cache_component(&account_key));
        tokio::fs::create_dir_all(&media_dir).await?;
        tokio::fs::create_dir_all(&public_asset_dir).await?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            tokio::fs::set_permissions(&media_dir, std::fs::Permissions::from_mode(0o700)).await?;
            tokio::fs::set_permissions(&public_asset_dir, std::fs::Permissions::from_mode(0o700))
                .await?;
        }
        let me: User = api.get("users/@me").await?;
        let service = KaedeService::new(api.clone());
        let guilds = service.guilds().await?;
        let dms = service.direct_messages().await?;
        // These enrich the home view but must not prevent sign-in when an
        // older or partially upgraded instance lacks one optional endpoint.
        let read_states = optional_snapshot(service.read_states().await, &events, "read states");
        let settings = optional_value(service.settings().await, &events, "user settings");
        let relationships =
            optional_snapshot(service.relationships().await, &events, "relationships");
        let emojis = optional_snapshot(service.available_emojis().await, &events, "emoji");
        let guild_notifications = optional_snapshot(
            service.all_guild_notification_settings().await,
            &events,
            "guild notification preferences",
        );
        let mut initial = AppState::default();
        initial.hydrate_identity(me);
        if let Some(settings) = settings {
            initial.hydrate_settings(settings);
        }
        initial.hydrate_guilds(guilds);
        initial.hydrate_channels(dms);
        initial.hydrate_read_states(read_states);
        for relationship in relationships {
            let key = relationship.user.key();
            initial.users.insert(key.clone(), relationship.user.clone());
            initial.relationships.insert(key, relationship);
        }
        for emoji in emojis {
            initial.emojis.insert(emoji.key(), emoji);
        }
        for preference in guild_notifications {
            initial.guild_notification_levels.insert(
                EntityRef::new(preference.guild_id, preference.guild_domain),
                preference.level,
            );
        }
        let state = Arc::new(RwLock::new(initial));

        let token = session.access_token().await?;
        let mut gateway = kaede_gateway::spawn(api.endpoint().gateway_url().clone(), token);
        let runtime = Arc::new(Self {
            api,
            service,
            session,
            state: state.clone(),
            commands: gateway.commands.clone(),
            cache: cache.clone(),
            media_dir,
            public_asset_dir,
            account_key: account_key.clone(),
            events: events.clone(),
            reconcile_lock: Mutex::new(()),
            public_asset_refresh_lock: Mutex::new(()),
            public_asset_revalidations: Mutex::new(HashSet::new()),
        });
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    event = gateway.events.recv() => {
                        let Some(event) = event else { break; };
                        let voice_reauthorization = voice_reauthorization(&event);
                        let typing_key = typing_key(&event);
                        let notification = {
                            let current = state.read().await;
                            notification_for_event(&current, &event)
                        };
                        let reduction = {
                            let mut current = state.write().await;
                            reduce_gateway_event(&mut current, event)
                        };
                        match reduction {
                            Ok(reduction) => {
                                purge_revoked_channels(
                                    &cache,
                                    &account_key,
                                    &reduction.purge_channels,
                                ).await;
                                publish_reduction(&events, reduction);
                                if let Some((channel, move_session_id)) = voice_reauthorization {
                                    let _ = events.send(AccountEvent::VoiceReauthorization {
                                        channel,
                                        move_session_id,
                                    });
                                }
                                if let Some(notification) = notification {
                                    let _ = events.send(AccountEvent::Notification(notification));
                                }
                                if let Some(key) = typing_key {
                                    let inserted = state.read().await.typing.get(&key).copied();
                                    let cleanup_state = state.clone();
                                    let cleanup_events = events.clone();
                                    tokio::spawn(async move {
                                        tokio::time::sleep(std::time::Duration::from_secs(10)).await;
                                        let mut state = cleanup_state.write().await;
                                        if inserted.is_some() && state.typing.get(&key).copied() == inserted {
                                            state.typing.remove(&key);
                                            drop(state);
                                            let _ = cleanup_events.send(AccountEvent::StateChanged);
                                        }
                                    });
                                }
                            }
                            Err(error) => {
                                tracing::warn!(%error, "realtime update payload was invalid");
                                let _ = events.send(AccountEvent::Error(
                                    "Kaede received a realtime update this app version could not understand. Your data is being refreshed; update Kaede if this keeps happening."
                                        .to_owned(),
                                ));
                                let _ = events.send(AccountEvent::ReconcileRequired);
                            }
                        }
                    }
                    changed = gateway.status.changed() => {
                        if changed.is_err() { break; }
                        let gateway_status = gateway.status.borrow().clone();
                        if matches!(gateway_status, GatewayStatus::Disconnected | GatewayStatus::Reconnecting | GatewayStatus::AuthenticationFailed) {
                            let mut current = state.write().await;
                            current.presences.clear();
                            current.voice_states.clear();
                            current.calls.clear();
                            drop(current);
                            let _ = events.send(AccountEvent::StateChanged);
                        }
                        let _ = events.send(AccountEvent::GatewayStatus(gateway_status));
                    }
                }
            }
        });
        runtime.refresh_public_assets().await;
        tokio::spawn(run_periodic_public_asset_refresh(
            Arc::downgrade(&runtime),
            PUBLIC_ASSET_REFRESH_INTERVAL,
            |runtime| async move {
                runtime.refresh_public_assets().await;
                let _ = runtime.events.send(AccountEvent::StateChanged);
            },
        ));
        Ok(runtime)
    }

    /// Resolve public avatars, banners, and guild art into an account-scoped
    /// cache. Failures are intentionally isolated per asset: federation lag or
    /// one unavailable media origin must not prevent the rest of the UI from
    /// rendering or make account sign-in fail.
    pub async fn refresh_public_assets(&self) {
        let _guard = self.public_asset_refresh_lock.lock().await;
        let cached_entries = {
            let mut state = self.state.write().await;
            let requests = prune_unreferenced_public_assets(&mut state);
            requests
                .iter()
                .filter_map(|(origin, hash, variant)| {
                    let key = public_asset_key(origin, hash, variant);
                    state
                        .public_assets
                        .get(&key)
                        .map(|path| (key, path.clone()))
                })
                .collect::<Vec<_>>()
        };
        let unusable_entries = unusable_public_asset_entries(cached_entries).await;
        let (requests, available_keys, active_keys, newly_unusable_keys) = {
            let mut state = self.state.write().await;
            let requests = prune_unreferenced_public_assets(&mut state);
            let mut newly_unusable_keys = HashSet::new();
            for (key, checked_path) in unusable_entries {
                if state.public_assets.get(&key) == Some(&checked_path) {
                    state.public_assets.remove(&key);
                    newly_unusable_keys.insert(key);
                }
            }
            let available_keys = state.public_assets.keys().cloned().collect::<HashSet<_>>();
            let active_keys = requests
                .iter()
                .map(|(origin, hash, variant)| public_asset_key(origin, hash, variant))
                .collect::<HashSet<_>>();
            (requests, available_keys, active_keys, newly_unusable_keys)
        };
        let revalidation_keys = {
            let mut pending = self.public_asset_revalidations.lock().await;
            pending.retain(|key| active_keys.contains(key) && !available_keys.contains(key));
            pending.extend(newly_unusable_keys);
            pending.clone()
        };
        let requests =
            schedule_public_asset_requests(requests, &available_keys, &revalidation_keys);
        if requests.is_empty() {
            return;
        }
        // A READY payload can contain thousands of members. Never serialize
        // all remote downloads onto sign-in or allow an unavailable peer to
        // occupy an unbounded number of sockets. Subsequent state changes
        // continue filling the content-addressed cache in bounded batches.
        let media = kaede_media::MediaClient::new(self.api.clone());
        let public_asset_dir = self.public_asset_dir.clone();
        let resolved = stream::iter(requests)
            .map(|(origin, hash, variant)| {
                let media = media.clone();
                let public_asset_dir = public_asset_dir.clone();
                async move {
                    match media
                        .cache_public_asset(&origin, &hash, &variant, &public_asset_dir)
                        .await
                    {
                        Ok(path) => Some((
                            public_asset_key(&origin, &hash, &variant),
                            path.to_string_lossy().into_owned(),
                        )),
                        Err(error) => {
                            tracing::debug!(
                                %error,
                                %origin,
                                content_hash = %hash,
                                variant = %variant,
                                "public media asset was unavailable"
                            );
                            None
                        }
                    }
                }
            })
            .buffer_unordered(4)
            .filter_map(|item| async move { item })
            .collect::<Vec<_>>()
            .await;
        let resolved_keys = resolved
            .iter()
            .map(|(key, _)| key.clone())
            .collect::<HashSet<_>>();
        let mut state = self.state.write().await;
        let current_requests = prune_unreferenced_public_assets(&mut state);
        let current_keys = current_requests
            .iter()
            .map(|(origin, hash, variant)| public_asset_key(origin, hash, variant))
            .collect::<HashSet<_>>();
        state.public_assets.extend(
            resolved
                .into_iter()
                .filter(|(key, _)| current_keys.contains(key)),
        );
        drop(state);
        self.public_asset_revalidations
            .lock()
            .await
            .retain(|key| !resolved_keys.contains(key));
    }

    pub async fn load_channel(&self, channel: &EntityRef) -> Result<Vec<Message>, AccountError> {
        let messages = match self
            .service
            .messages(
                channel,
                MessagePage {
                    limit: 50,
                    ..MessagePage::default()
                },
            )
            .await
        {
            Ok(messages) => {
                self.cache
                    .put_messages(self.account_key.clone(), channel.clone(), messages.clone())
                    .await?;
                messages
            }
            Err(network_error) => {
                let cached = self
                    .cache
                    .channel_messages(self.account_key.clone(), channel.clone(), 50)
                    .await?;
                if cached.is_empty() {
                    return Err(network_error.into());
                }
                let _ = self.events.send(AccountEvent::Error(
                    "You are offline. Showing the most recent cached messages.".to_owned(),
                ));
                cached
            }
        };
        self.state
            .write()
            .await
            .hydrate_messages(channel, messages.clone());
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(messages)
    }

    /// Resolve a bounded set of previews through the home instance. This is
    /// deliberately separate from message loading so a slow or broken website
    /// can never block opening a conversation.
    pub async fn refresh_link_previews(&self, channel: &EntityRef) {
        let candidates = {
            let state = self.state.read().await;
            state
                .channel_messages(channel)
                .into_iter()
                .rev()
                .filter(|message| !state.link_previews.contains_key(&message.key()))
                .filter_map(|message| {
                    let url = message.content.as_deref().and_then(first_http_url)?;
                    Some((message.key(), url.to_owned()))
                })
                .take(6)
                .collect::<Vec<_>>()
        };
        let mut changed = false;
        for (message, url) in candidates {
            let value = match self.service.link_preview(&url).await {
                Ok(value) => value,
                Err(error) => {
                    tracing::debug!(%error, %url, "message link has no available preview");
                    continue;
                }
            };
            let Ok(preview) = serde_json::from_value::<LinkPreview>(value) else {
                continue;
            };
            self.state
                .write()
                .await
                .link_previews
                .insert(message, preview);
            changed = true;
        }
        if changed {
            let _ = self.events.send(AccountEvent::StateChanged);
        }
    }

    /// Populate bounded, scanned attachment thumbnails in the private native
    /// cache. The API performs channel authorization and remote-instance media
    /// brokering before redirecting to a short-lived object capability.
    #[allow(clippy::too_many_lines)]
    pub async fn refresh_message_media(&self, channel: &EntityRef) {
        let candidates = {
            let state = self.state.read().await;
            state
                .channel_messages(channel)
                .into_iter()
                .rev()
                .flat_map(|message| {
                    message.attachments.into_iter().filter_map(|attachment| {
                        let content_type = attachment.content_type.as_deref()?;
                        let variant = if content_type.starts_with("image/") {
                            "thumbnail_512"
                        } else if content_type.starts_with("video/") {
                            "poster"
                        } else {
                            return None;
                        };
                        (attachment.scan_status.as_deref() == Some("clean")
                            && attachment.local_path.is_none())
                        .then_some((attachment, variant))
                    })
                })
                .take(8)
                .collect::<Vec<_>>()
        };
        let mut changed = false;
        for (attachment, variant) in candidates {
            let media_path = match authenticated_attachment_media_path(&attachment, variant) {
                Ok(path) => path,
                Err(error) => {
                    tracing::debug!(%error, id = %attachment.id, "attachment media path was invalid");
                    continue;
                }
            };
            let cache_key = format!("{}:{}:{variant}", attachment.origin_domain, attachment.id);
            let digest = format!("{:x}", Sha256::digest(cache_key.as_bytes()));
            let path = self.media_dir.join(digest);
            let bytes = match self.api.get_root_bytes(&media_path, 5 * 1024 * 1024).await {
                Ok(bytes) => bytes,
                Err(error) => {
                    tracing::debug!(%error, id = %attachment.id, "attachment preview was unavailable");
                    continue;
                }
            };
            let temporary = path.with_extension("tmp");
            if tokio::fs::write(&temporary, &bytes).await.is_err() {
                continue;
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if tokio::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))
                    .await
                    .is_err()
                {
                    let _ = tokio::fs::remove_file(&temporary).await;
                    continue;
                }
            }
            if tokio::fs::rename(&temporary, &path).await.is_err() {
                let _ = tokio::fs::remove_file(&temporary).await;
                continue;
            }
            let path_string = path.to_string_lossy().into_owned();
            if self
                .cache
                .put_media(
                    self.account_key.clone(),
                    channel.clone(),
                    cache_key,
                    path_string.clone(),
                    bytes.len() as u64,
                )
                .await
                .is_err()
            {
                let _ = tokio::fs::remove_file(&path).await;
                continue;
            }
            if let Ok(evicted) = self
                .cache
                .prune_media(self.account_key.clone(), 512 * 1024 * 1024)
                .await
            {
                for evicted in evicted {
                    let _ = tokio::fs::remove_file(evicted).await;
                }
            }
            let mut state = self.state.write().await;
            for message in state.messages.values_mut() {
                if message.channel_key() != *channel {
                    continue;
                }
                for current in &mut message.attachments {
                    if current.id == attachment.id
                        && current.origin_domain == attachment.origin_domain
                    {
                        current.local_path = Some(path_string.clone());
                    }
                }
            }
            changed = true;
        }
        if changed {
            let _ = self.events.send(AccountEvent::StateChanged);
        }
    }

    /// Fetch the first viewable attachment from a message into the private,
    /// account-scoped cache. Authorization and remote-media brokering remain
    /// on the home instance; callers receive only a local file path.
    pub async fn message_media(
        &self,
        message: &EntityRef,
    ) -> Result<NativeMediaAsset, AccountError> {
        let (channel, attachment, content_type) = {
            let state = self.state.read().await;
            let message = state
                .messages
                .get(message)
                .ok_or(AccountError::MediaUnavailable)?;
            let attachment = message
                .attachments
                .iter()
                .find(|attachment| {
                    attachment.scan_status.as_deref() == Some("clean")
                        && attachment
                            .content_type
                            .as_deref()
                            .is_some_and(|content_type| {
                                content_type.starts_with("image/")
                                    || content_type.starts_with("video/")
                            })
                })
                .cloned()
                .ok_or(AccountError::MediaUnavailable)?;
            let content_type = attachment
                .content_type
                .clone()
                .ok_or(AccountError::MediaUnavailable)?;
            (message.channel_key(), attachment, content_type)
        };
        let cache_key = format!("{}:{}:original", attachment.origin_domain, attachment.id);
        let digest = format!("{:x}", Sha256::digest(cache_key.as_bytes()));
        let path = self.media_dir.join(format!("{digest}.media"));
        if tokio::fs::metadata(&path)
            .await
            .is_ok_and(|metadata| metadata.is_file() && metadata.len() > 0)
        {
            return Ok(NativeMediaAsset { path, content_type });
        }
        let media_path = authenticated_attachment_media_path(&attachment, "original")?;
        let bytes = self
            .api
            .get_root_bytes(&media_path, 16 * 1024 * 1024)
            .await?;
        let temporary = path.with_extension("tmp");
        tokio::fs::write(&temporary, &bytes).await?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            tokio::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600)).await?;
        }
        if let Err(error) = tokio::fs::rename(&temporary, &path).await {
            if tokio::fs::metadata(&path)
                .await
                .is_ok_and(|metadata| metadata.is_file() && metadata.len() > 0)
            {
                let _ = tokio::fs::remove_file(&temporary).await;
            } else {
                let _ = tokio::fs::remove_file(&temporary).await;
                return Err(AccountError::Io(error));
            }
        }
        if let Err(error) = self
            .cache
            .put_media(
                self.account_key.clone(),
                channel,
                cache_key,
                path.to_string_lossy().into_owned(),
                bytes.len() as u64,
            )
            .await
        {
            let _ = tokio::fs::remove_file(&path).await;
            return Err(AccountError::Cache(error));
        }
        if let Ok(evicted) = self
            .cache
            .prune_media(self.account_key.clone(), 512 * 1024 * 1024)
            .await
        {
            for evicted in evicted {
                let _ = tokio::fs::remove_file(evicted).await;
            }
        }
        Ok(NativeMediaAsset { path, content_type })
    }

    pub async fn load_older_messages(
        &self,
        channel: &EntityRef,
    ) -> Result<Vec<Message>, AccountError> {
        let before = self
            .state
            .read()
            .await
            .message_order
            .get(channel)
            .and_then(|order| order.front())
            .cloned();
        let messages = self
            .service
            .messages(
                channel,
                MessagePage {
                    before: before.as_ref(),
                    limit: 100,
                    ..MessagePage::default()
                },
            )
            .await?;
        self.cache
            .put_messages(self.account_key.clone(), channel.clone(), messages.clone())
            .await?;
        self.state
            .write()
            .await
            .hydrate_older_messages(channel, messages.clone());
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(messages)
    }

    pub async fn load_around_message(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
    ) -> Result<Vec<Message>, AccountError> {
        let messages = self
            .service
            .messages(
                channel,
                MessagePage {
                    around: Some(message),
                    limit: 100,
                    ..MessagePage::default()
                },
            )
            .await?;
        self.state
            .write()
            .await
            .hydrate_messages(channel, messages.clone());
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(messages)
    }

    pub async fn load_guild(&self, guild: &EntityRef) -> Result<(), AccountError> {
        let refreshed = self.service.guild(guild).await?;
        self.state.write().await.hydrate_guilds([refreshed]);
        // Local guilds can return a durable page directly. Remote guild member
        // lists are requested over the gateway and arrive in bounded chunks.
        match self.service.members(guild, None, 1_000).await {
            Ok(first_page) => {
                let mut page = first_page;
                let mut loaded = 0usize;
                loop {
                    let page_len = page.len();
                    let after = page.last().map(|member| member.user.key());
                    {
                        let mut state = self.state.write().await;
                        for member in page {
                            let user = member.user.key();
                            state.users.insert(user.clone(), member.user.clone());
                            state.members.insert((guild.clone(), user), member);
                        }
                    }
                    loaded = loaded.saturating_add(page_len);
                    if page_len < 1_000 || loaded >= 20_000 {
                        break;
                    }
                    let Some(after) = after else {
                        break;
                    };
                    page = self.service.members(guild, Some(&after), 1_000).await?;
                }
            }
            Err(error) => {
                tracing::debug!(%error, %guild, "member REST snapshot unavailable; requesting gateway chunk");
                let _ = self
                    .commands
                    .send(GatewayCommand::RequestMembers {
                        guild_id: guild.id.to_string(),
                        guild_domain: guild.domain.to_string(),
                        query: String::new(),
                        limit: 100,
                    })
                    .await;
            }
        }
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(())
    }

    /// Refresh the non-durable occupancy snapshot for one voice channel.
    /// Gateway voice events provide the low-latency path, while this snapshot
    /// closes gaps after reconnects and across eventually-consistent peers.
    pub async fn refresh_voice_occupancy(&self, channel: &EntityRef) -> Result<(), AccountError> {
        let occupancy = self.service.voice_occupancy(channel).await?;
        let mut state = self.state.write().await;
        let guild_domain = state
            .channels
            .get(channel)
            .and_then(|value| value.guild_domain.clone());
        state.voice_states.retain(|_, voice| {
            voice.channel_id != Some(channel.id)
                || voice.channel_domain.as_ref() != Some(&channel.domain)
        });
        for occupant in occupancy.participants {
            let identity = occupant.identity.clone();
            state.voice_states.insert(
                identity,
                VoiceState {
                    user_id: occupant.user_id,
                    user_domain: occupant.user_domain,
                    guild_id: occupant.guild_id,
                    guild_domain: guild_domain.clone(),
                    channel_id: Some(occupant.channel_id),
                    channel_domain: Some(channel.domain.clone()),
                    self_mute: occupant.self_mute,
                    self_deaf: occupant.self_deaf,
                    server_mute: occupant.server_mute,
                    server_deaf: occupant.server_deaf,
                },
            );
        }
        drop(state);
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(())
    }

    /// Replace the durable account snapshot after a gateway sequence gap.
    /// Message history and optimistic sends are retained only for channels the
    /// authoritative snapshot still exposes. Ephemeral presence and voice
    /// state are intentionally discarded because those events are not replayed.
    pub async fn reconcile(&self) -> Result<(), AccountError> {
        let _guard = self.reconcile_lock.lock().await;
        let (me, guilds, dms) = tokio::try_join!(
            self.service.me(),
            self.service.guilds(),
            self.service.direct_messages(),
        )?;
        let read_states = optional_snapshot(
            self.service.read_states().await,
            &self.events,
            "read states",
        );
        let settings = optional_value(self.service.settings().await, &self.events, "user settings");
        let relationships = optional_snapshot(
            self.service.relationships().await,
            &self.events,
            "relationships",
        );
        let emojis =
            optional_snapshot(self.service.available_emojis().await, &self.events, "emoji");
        let guild_notifications = optional_snapshot(
            self.service.all_guild_notification_settings().await,
            &self.events,
            "guild notification preferences",
        );

        let mut refreshed = AppState::default();
        refreshed.hydrate_identity(me);
        if let Some(settings) = settings {
            refreshed.hydrate_settings(settings);
        }
        refreshed.hydrate_guilds(guilds);
        refreshed.hydrate_channels(dms);
        refreshed.hydrate_read_states(read_states);
        for relationship in relationships {
            let key = relationship.user.key();
            refreshed
                .users
                .insert(key.clone(), relationship.user.clone());
            refreshed.relationships.insert(key, relationship);
        }
        for emoji in emojis {
            refreshed.emojis.insert(emoji.key(), emoji);
        }
        for preference in guild_notifications {
            refreshed.guild_notification_levels.insert(
                EntityRef::new(preference.guild_id, preference.guild_domain),
                preference.level,
            );
        }

        let visible = refreshed.channels.keys().cloned().collect::<HashSet<_>>();
        let (revoked, guilds) = {
            let mut current = self.state.write().await;
            let revoked = current
                .channels
                .keys()
                .filter(|channel| !visible.contains(*channel))
                .cloned()
                .collect::<Vec<_>>();
            refreshed.messages = std::mem::take(&mut current.messages)
                .into_iter()
                .filter(|(_, message)| visible.contains(&message.channel_key()))
                .collect();
            refreshed.message_order = std::mem::take(&mut current.message_order)
                .into_iter()
                .filter(|(channel, _)| visible.contains(channel))
                .collect();
            refreshed.pending_messages = std::mem::take(&mut current.pending_messages)
                .into_iter()
                .filter(|(_, message)| visible.contains(&message.channel))
                .collect();
            refreshed.sequence = current.sequence;
            refreshed.session_id.clone_from(&current.session_id);
            let guilds = refreshed.guilds.keys().cloned().collect::<Vec<_>>();
            *current = refreshed;
            (revoked, guilds)
        };
        purge_revoked_channels(&self.cache, &self.account_key, &revoked).await;
        for guild in guilds {
            let _ = self
                .commands
                .send(GatewayCommand::RequestMembers {
                    guild_id: guild.id.to_string(),
                    guild_domain: guild.domain.to_string(),
                    query: String::new(),
                    limit: 100,
                })
                .await;
        }
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(())
    }

    pub async fn update_profile(
        &self,
        patch: &kaede_api::service::ProfilePatch,
    ) -> Result<User, AccountError> {
        let user = self.service.update_me(patch).await?;
        {
            let mut state = self.state.write().await;
            state.hydrate_identity(user.clone());
            let _ = prune_unreferenced_public_assets(&mut state);
        }
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(user)
    }

    pub async fn update_settings(
        &self,
        patch: &serde_json::Value,
    ) -> Result<UserSettings, AccountError> {
        let value = self.service.update_settings(patch).await?;
        let settings: UserSettings = serde_json::from_value(value)?;
        self.state.write().await.hydrate_settings(settings.clone());
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(settings)
    }

    pub async fn request_friend(&self, handle: &str) -> Result<(), AccountError> {
        self.service.request_friend(handle).await?;
        self.reload_relationships().await
    }

    pub async fn accept_friend(&self, user: &EntityRef) -> Result<(), AccountError> {
        self.service.accept_friend(user).await?;
        self.reload_relationships().await
    }

    pub async fn remove_relationship(&self, user: &EntityRef) -> Result<(), AccountError> {
        self.service.remove_friend(user).await?;
        self.reload_relationships().await
    }

    pub async fn set_blocked(&self, user: &EntityRef, blocked: bool) -> Result<(), AccountError> {
        self.service.set_blocked(user, blocked).await?;
        self.reload_relationships().await
    }

    pub async fn open_dm(&self, handle: &str) -> Result<kaede_core::Channel, AccountError> {
        let channel = self.service.open_dm(handle).await?;
        self.state.write().await.hydrate_channels([channel.clone()]);
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(channel)
    }

    pub async fn set_presence(
        &self,
        status: &str,
        custom_status: Option<String>,
    ) -> Result<(), AccountError> {
        self.commands
            .send(GatewayCommand::Presence {
                status: status.to_owned(),
                custom_status,
            })
            .await
            .map_err(|_| AccountError::GatewayClosed)
    }

    async fn reload_relationships(&self) -> Result<(), AccountError> {
        let relationships = self.service.relationships().await?;
        let mut state = self.state.write().await;
        state.relationships.clear();
        for relationship in relationships {
            let key = relationship.user.key();
            state.users.insert(key.clone(), relationship.user.clone());
            state.relationships.insert(key, relationship);
        }
        drop(state);
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(())
    }

    pub async fn send_message(
        &self,
        channel: &EntityRef,
        content: String,
    ) -> Result<(), AccountError> {
        self.send_message_with_attachments(channel, content, Vec::new())
            .await
    }

    pub async fn send_message_with_attachments(
        &self,
        channel: &EntityRef,
        content: String,
        attachment_ids: Vec<kaede_protocol::Snowflake>,
    ) -> Result<(), AccountError> {
        self.send_message_with_context(channel, content, attachment_ids, Vec::new(), None)
            .await
    }

    pub async fn send_message_with_context(
        &self,
        channel: &EntityRef,
        content: String,
        attachment_ids: Vec<kaede_protocol::Snowflake>,
        mention_user_ids: Vec<EntityRef>,
        referenced_message_id: Option<EntityRef>,
    ) -> Result<(), AccountError> {
        let nonce = uuid::Uuid::new_v4().to_string();
        let author = self
            .state
            .read()
            .await
            .current_user
            .as_ref()
            .map(User::key)
            .ok_or(AccountError::MissingIdentity)?;
        self.state.write().await.enqueue_message(PendingMessage {
            channel: channel.clone(),
            author,
            client_nonce: nonce.clone(),
            content: content.clone(),
            attachment_ids: attachment_ids.clone(),
            mention_user_ids: mention_user_ids.clone(),
            referenced_message_id: referenced_message_id.clone(),
            created_at: chrono::Utc::now(),
            state: PendingMessageState::Sending,
            failure_reason: None,
        });
        let _ = self.events.send(AccountEvent::StateChanged);
        let request = kaede_api::service::MessageCreate {
            content: Some(content),
            e2ee: None,
            client_nonce: nonce.clone(),
            attachment_ids,
            mention_user_ids,
            referenced_message_id,
        };
        match self.service.send_message(channel, &request).await {
            Ok(response) => {
                if response.get("status").and_then(serde_json::Value::as_str) == Some("queued") {
                    self.state.write().await.mark_message_queued(&nonce);
                } else if let Ok(message) = serde_json::from_value::<Message>(response) {
                    self.state
                        .write()
                        .await
                        .hydrate_messages(channel, vec![message]);
                }
                let _ = self.events.send(AccountEvent::StateChanged);
                Ok(())
            }
            Err(error) => {
                let reason = user_facing_send_error(&error);
                self.state.write().await.fail_message(&nonce, reason);
                let _ = self.events.send(AccountEvent::StateChanged);
                Err(error.into())
            }
        }
    }

    pub async fn retry_message(&self, nonce: &str) -> Result<(), AccountError> {
        let pending = self
            .state
            .write()
            .await
            .retry_message(nonce)
            .ok_or(AccountError::PendingMessageMissing)?;
        let _ = self.events.send(AccountEvent::StateChanged);
        let request = kaede_api::service::MessageCreate {
            content: Some(pending.content),
            e2ee: None,
            client_nonce: pending.client_nonce.clone(),
            attachment_ids: pending.attachment_ids,
            mention_user_ids: pending.mention_user_ids,
            referenced_message_id: pending.referenced_message_id,
        };
        match self.service.send_message(&pending.channel, &request).await {
            Ok(response) => {
                if response.get("status").and_then(serde_json::Value::as_str) == Some("queued") {
                    self.state
                        .write()
                        .await
                        .mark_message_queued(&pending.client_nonce);
                } else if let Ok(message) = serde_json::from_value::<Message>(response) {
                    self.state
                        .write()
                        .await
                        .hydrate_messages(&pending.channel, vec![message]);
                }
                let _ = self.events.send(AccountEvent::StateChanged);
                Ok(())
            }
            Err(error) => {
                let reason = user_facing_send_error(&error);
                self.state
                    .write()
                    .await
                    .fail_message(&pending.client_nonce, reason);
                let _ = self.events.send(AccountEvent::StateChanged);
                Err(error.into())
            }
        }
    }

    pub async fn edit_message(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
        content: &str,
    ) -> Result<Message, AccountError> {
        let updated = self.service.edit_message(channel, message, content).await?;
        self.state
            .write()
            .await
            .hydrate_messages(channel, vec![updated.clone()]);
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(updated)
    }

    pub async fn delete_message(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
    ) -> Result<(), AccountError> {
        self.service.delete_message(channel, message).await?;
        self.state.write().await.messages.remove(message);
        if let Some(order) = self.state.write().await.message_order.get_mut(channel) {
            order.retain(|candidate| candidate != message);
        }
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(())
    }

    pub async fn set_reaction(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
        emoji: &str,
        enabled: bool,
    ) -> Result<(), AccountError> {
        if enabled {
            self.service.react(channel, message, emoji).await?;
        } else {
            self.service
                .remove_reaction(channel, message, emoji)
                .await?;
        }
        Ok(())
    }

    pub async fn set_pinned(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
        pinned: bool,
    ) -> Result<(), AccountError> {
        self.service.set_pinned(channel, message, pinned).await?;
        Ok(())
    }

    /// Toggle a pin against server state rather than assuming the local cache
    /// is complete. Pin state is not embedded in ordinary message payloads.
    pub async fn toggle_pinned(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
    ) -> Result<bool, AccountError> {
        let pinned = self
            .service
            .pins(channel)
            .await?
            .iter()
            .any(|candidate| candidate.key() == *message);
        self.set_pinned(channel, message, !pinned).await?;
        Ok(!pinned)
    }

    pub async fn acknowledge_channel(
        &self,
        channel: &EntityRef,
        message: Option<&EntityRef>,
    ) -> Result<(), AccountError> {
        self.service.acknowledge(channel, message).await?;
        // The server does not echo our own acknowledgement back on the
        // gateway, so clear the local unread state immediately.
        {
            let mut state = self.state.write().await;
            let (guild_id, guild_domain) = state
                .channels
                .get(channel)
                .map_or((None, None), |c| (c.guild_id, c.guild_domain.clone()));
            let entry =
                state
                    .read_states
                    .entry(channel.clone())
                    .or_insert_with(|| kaede_core::ReadState {
                        channel_id: channel.id,
                        channel_domain: channel.domain.clone(),
                        guild_id,
                        guild_domain,
                        last_read_message_id: None,
                        last_read_message_domain: None,
                        unread: false,
                        mention_count: 0,
                    });
            entry.unread = false;
            entry.mention_count = 0;
            if let Some(message) = message {
                entry.last_read_message_id = Some(message.id);
                entry.last_read_message_domain = Some(message.domain.clone());
            }
        }
        let _ = self.events.send(AccountEvent::StateChanged);
        Ok(())
    }

    pub async fn notify_typing(&self, channel: &EntityRef) -> Result<(), AccountError> {
        self.service.typing(channel).await?;
        Ok(())
    }

    pub async fn shutdown(&self) {
        let _ = self.commands.send(GatewayCommand::Shutdown).await;
    }

    pub async fn logout(&self) -> Result<(), AccountError> {
        let _ = self.commands.send(GatewayCommand::Shutdown).await;
        self.session.logout().await?;
        Ok(())
    }
}

fn voice_reauthorization(
    event: &kaede_protocol::GatewayEnvelope,
) -> Option<(EntityRef, Option<String>)> {
    if event.t.as_deref() != Some("VOICE_TOKEN") {
        return None;
    }
    let grant = event.d.get("grant")?.as_object()?;
    let move_session_id = if let Some(correlation) = event.d.get("move_session_id") {
        let correlation = correlation.as_str()?;
        if !(32..=64).contains(&correlation.len())
            || !correlation
                .bytes()
                .all(|value| value.is_ascii_alphanumeric() || matches!(value, b'_' | b'-'))
            || grant.get("move_session_id")?.as_str()? != correlation
        {
            return None;
        }
        Some(correlation.to_owned())
    } else if grant
        .get("move_session_id")
        .is_some_and(|value| !value.is_null())
    {
        return None;
    } else {
        None
    };
    let id = event.d.get("channel_id")?.as_str()?;
    let domain = event.d.get("channel_domain")?.as_str()?;
    let channel = format!("{id}@{domain}").parse().ok()?;
    Some((channel, move_session_id))
}

fn unauthenticated_session(instance: &str, purpose: &str) -> Result<NativeSession, AccountError> {
    let domain = Domain::parse(instance.trim())?;
    let endpoint = InstanceEndpoint::production(domain.clone())?;
    let api = ApiClient::new(endpoint)?;
    Ok(SessionManager::new(
        api,
        Arc::new(SystemCredentialVault),
        Arc::new(EmbeddedTurnstile),
        format!("{purpose}@{domain}"),
    ))
}

fn user_facing_send_error(error: &ApiClientError) -> String {
    match error {
        ApiClientError::Server { error, .. } if error.code == "MEMBER_TIMED_OUT" => {
            "You are timed out and cannot send messages in this guild.".to_owned()
        }
        ApiClientError::Server { error, .. }
            if matches!(error.code.as_str(), "FORBIDDEN" | "MISSING_PERMISSIONS") =>
        {
            "You do not have permission to send messages in this channel.".to_owned()
        }
        _ => "Message not delivered. Check your connection and retry.".to_owned(),
    }
}

async fn purge_revoked_channels(cache: &Cache, account: &str, channels: &[EntityRef]) {
    for channel in channels {
        match cache
            .purge_channel(account.to_owned(), channel.clone())
            .await
        {
            Ok(paths) => {
                for path in paths {
                    if let Err(error) = tokio::fs::remove_file(&path).await
                        && error.kind() != std::io::ErrorKind::NotFound
                    {
                        tracing::warn!(%error, %path, "could not remove revoked media cache file");
                    }
                }
            }
            Err(error) => {
                tracing::error!(%error, %channel, "could not purge revoked channel cache");
            }
        }
    }
}

async fn authenticate_prepared(
    session: &NativeSession,
    identifier: &str,
    authentication_secret: &SecretString,
    password_kdf: &serde_json::Value,
    device_name: &str,
) -> Result<(), AccountError> {
    match session
        .login_with_prepared_password(
            identifier,
            authentication_secret,
            password_kdf,
            device_name,
            None,
        )
        .await?
    {
        LoginOutcome::Authenticated => Ok(()),
        LoginOutcome::MfaRequired(ticket) => Err(AccountError::MfaRequired(ticket)),
        LoginOutcome::ChallengeRequired => {
            let config = session.config().await?;
            let site_key = config
                .turnstile
                .site_key
                .ok_or(AccountError::ChallengeMisconfigured)?;
            let token = session
                .solve_turnstile(site_key, "kaede-login-v1", uuid::Uuid::new_v4().to_string())
                .await?;
            match session
                .login_with_prepared_password(
                    identifier,
                    authentication_secret,
                    password_kdf,
                    device_name,
                    Some(&token),
                )
                .await?
            {
                LoginOutcome::Authenticated => Ok(()),
                LoginOutcome::MfaRequired(ticket) => Err(AccountError::MfaRequired(ticket)),
                LoginOutcome::ChallengeRequired => Err(AccountError::ChallengeRejected),
            }
        }
    }
}

fn first_http_url(content: &str) -> Option<&str> {
    content.split_whitespace().find_map(|candidate| {
        let candidate = candidate.trim_end_matches(['.', ',', ')', ']', '}', '!', '?']);
        let parsed = url::Url::parse(candidate).ok()?;
        matches!(parsed.scheme(), "http" | "https").then_some(candidate)
    })
}

fn public_asset_key(origin: &Domain, content_hash: &str, variant: &str) -> String {
    format!("{origin}/{content_hash}/{variant}")
}

type PublicAssetRequest = (Domain, String, String);

fn prune_unreferenced_public_assets(state: &mut AppState) -> HashSet<PublicAssetRequest> {
    let mut requests = HashSet::new();
    for user in state.users.values().chain(state.current_user.iter()) {
        if let Some(hash) = user.avatar_hash.as_deref() {
            requests.insert((
                user.origin_domain.clone(),
                hash.to_owned(),
                "thumbnail_128".to_owned(),
            ));
        }
        if let Some(hash) = user.banner_hash.as_deref() {
            requests.insert((
                user.origin_domain.clone(),
                hash.to_owned(),
                "thumbnail_1024".to_owned(),
            ));
        }
    }
    for guild in state.guilds.values() {
        if let Some(hash) = guild.icon_hash.as_deref() {
            requests.insert((
                guild.origin_domain.clone(),
                hash.to_owned(),
                "thumbnail_128".to_owned(),
            ));
        }
        if let Some(hash) = guild.banner_hash.as_deref() {
            requests.insert((
                guild.origin_domain.clone(),
                hash.to_owned(),
                "thumbnail_1024".to_owned(),
            ));
        }
    }
    let current_keys = requests
        .iter()
        .map(|(origin, hash, variant)| public_asset_key(origin, hash, variant))
        .collect::<HashSet<_>>();
    state
        .public_assets
        .retain(|key, _| current_keys.contains(key));
    requests
}

fn schedule_public_asset_requests(
    requests: HashSet<PublicAssetRequest>,
    available_keys: &HashSet<String>,
    revalidation_keys: &HashSet<String>,
) -> Vec<PublicAssetRequest> {
    let mut missing = Vec::new();
    let mut revalidations = Vec::new();
    for request in requests {
        let key = public_asset_key(&request.0, &request.1, &request.2);
        if available_keys.contains(&key) {
            continue;
        }
        if revalidation_keys.contains(&key) {
            revalidations.push(request);
        } else {
            missing.push(request);
        }
    }
    let reserved_revalidations = revalidations.len().min(PUBLIC_ASSET_REVALIDATION_SLOTS);
    let missing_count = missing
        .len()
        .min(PUBLIC_ASSET_BATCH_SIZE - reserved_revalidations);
    let mut scheduled = Vec::with_capacity(PUBLIC_ASSET_BATCH_SIZE);
    scheduled.extend(missing.drain(..missing_count));
    let revalidation_count = revalidations
        .len()
        .min(PUBLIC_ASSET_BATCH_SIZE - scheduled.len());
    scheduled.extend(revalidations.drain(..revalidation_count));
    let remaining = PUBLIC_ASSET_BATCH_SIZE - scheduled.len();
    scheduled.extend(missing.into_iter().take(remaining));
    scheduled
}

async fn unusable_public_asset_entries(entries: Vec<(String, String)>) -> Vec<(String, String)> {
    stream::iter(entries)
        .map(|(key, path)| async move {
            let cache_path = PathBuf::from(&path);
            match kaede_media::public_asset_cache_is_fresh(&cache_path).await {
                Ok(true) => None,
                Ok(false) => Some((key, path)),
                Err(error) => {
                    tracing::debug!(%error, %path, "public media cache path was unreadable");
                    Some((key, path))
                }
            }
        })
        .buffer_unordered(32)
        .filter_map(|entry| async move { entry })
        .collect()
        .await
}

fn reduce_gateway_event(
    state: &mut AppState,
    event: kaede_protocol::GatewayEnvelope,
) -> Result<Reduction, ReduceError> {
    let reduction = state.reduce(event)?;
    let _ = prune_unreferenced_public_assets(state);
    Ok(reduction)
}

async fn run_periodic_public_asset_refresh<T, F, Fut>(
    owner: Weak<T>,
    period: Duration,
    mut refresh: F,
) where
    T: Send + Sync + 'static,
    F: FnMut(Arc<T>) -> Fut,
    Fut: Future<Output = ()>,
{
    let first_tick = tokio::time::Instant::now() + period;
    let mut interval = tokio::time::interval_at(first_tick, period);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        interval.tick().await;
        let Some(owner) = owner.upgrade() else {
            break;
        };
        refresh(owner).await;
    }
}

fn safe_cache_component(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '-' | '.' | '_') {
                character
            } else {
                '_'
            }
        })
        .take(160)
        .collect()
}

fn typing_key(envelope: &kaede_protocol::GatewayEnvelope) -> Option<(EntityRef, EntityRef)> {
    if envelope.t.as_deref() != Some("TYPING_START") {
        return None;
    }
    let channel_id = envelope.d.get("channel_id")?.as_str()?.parse().ok()?;
    let channel_domain = envelope.d.get("channel_domain")?.as_str()?.parse().ok()?;
    let user_id = envelope.d.get("user_id")?.as_str()?.parse().ok()?;
    let user_domain = envelope.d.get("user_domain")?.as_str()?.parse().ok()?;
    Some((
        EntityRef::new(channel_id, channel_domain),
        EntityRef::new(user_id, user_domain),
    ))
}

fn publish_reduction(events: &mpsc::UnboundedSender<AccountEvent>, reduction: Reduction) {
    if reduction.reconcile_required {
        let _ = events.send(AccountEvent::ReconcileRequired);
    }
    for channel in reduction.purge_channels {
        let _ = events.send(AccountEvent::PurgeChannel(channel));
    }
    if let Some(message) = reduction.user_error {
        let _ = events.send(AccountEvent::Error(message));
    }
    if reduction.changed {
        let _ = events.send(AccountEvent::StateChanged);
    }
}

fn notification_for_event(
    state: &AppState,
    envelope: &kaede_protocol::GatewayEnvelope,
) -> Option<Notification> {
    if envelope.t.as_deref() != Some("MESSAGE_CREATE") {
        return None;
    }
    let message = serde_json::from_value::<Message>(envelope.d.clone()).ok()?;
    let current = state.current_user.as_ref()?;
    let current_key = current.key();
    if message
        .author_id
        .zip(message.author_domain.clone())
        .is_some_and(|(id, domain)| EntityRef::new(id, domain) == current_key)
    {
        return None;
    }
    let settings = state.user_settings.as_ref()?;
    if settings
        .notification_settings
        .get("desktop")
        .and_then(serde_json::Value::as_bool)
        == Some(false)
    {
        return None;
    }
    let channel = state.channels.get(&message.channel_key())?;
    if let Some(guild) = channel.guild_key() {
        let level = state
            .guild_notification_levels
            .get(&guild)
            .map_or("mentions", String::as_str);
        if level == "none"
            || (level == "mentions" && !message.mention_user_refs.contains(&current_key))
        {
            return None;
        }
    }
    let author = message.author.as_ref().map_or_else(
        || "New message".to_owned(),
        |author| author.label().to_owned(),
    );
    let channel_name = channel.name.as_deref().unwrap_or("direct message");
    let deep_link = url::Url::parse(&format!(
        "kaede://open/message/{}/{}",
        message.channel_key(),
        message.key()
    ))
    .ok();
    Some(Notification {
        title: format!("{author} in {channel_name}"),
        body: message
            .content
            .unwrap_or_else(|| "Sent an attachment".to_owned()),
        deep_link,
        sensitive: settings
            .notification_settings
            .get("desktop_message_previews")
            .and_then(serde_json::Value::as_bool)
            != Some(true),
    })
}

fn optional_snapshot<T>(
    result: Result<Vec<T>, ApiClientError>,
    events: &mpsc::UnboundedSender<AccountEvent>,
    label: &str,
) -> Vec<T> {
    match result {
        Ok(value) => value,
        Err(error) => {
            tracing::warn!(%error, %label, "optional account snapshot unavailable");
            let _ = events.send(AccountEvent::Error(format!(
                "Some {label} could not be loaded. Kaede will retry in the background."
            )));
            Vec::new()
        }
    }
}

fn optional_value<T>(
    result: Result<T, ApiClientError>,
    events: &mpsc::UnboundedSender<AccountEvent>,
    label: &str,
) -> Option<T> {
    match result {
        Ok(value) => Some(value),
        Err(error) => {
            tracing::warn!(%error, %label, "optional account snapshot unavailable");
            let _ = events.send(AccountEvent::Error(format!(
                "Some {label} could not be loaded. Kaede will retry in the background."
            )));
            None
        }
    }
}

fn authenticated_attachment_media_path(
    attachment: &Attachment,
    variant: &str,
) -> Result<String, AccountError> {
    let Some(history_path) = attachment.history_media_url.as_deref() else {
        return Ok(format!(
            "/media/{}/{}/{variant}",
            attachment.origin_domain, attachment.id
        ));
    };
    if !valid_history_media_path(history_path, attachment) {
        return Err(AccountError::MediaUnavailable);
    }
    Ok(history_path.to_owned())
}

fn valid_history_media_path(path: &str, attachment: &Attachment) -> bool {
    if !path.starts_with("/api/v1/dms/") || path.starts_with("//") || path.contains(['#', '\\']) {
        return false;
    }
    let Ok(url) = url::Url::parse(&format!("https://kaede.invalid{path}")) else {
        return false;
    };
    if url.scheme() != "https"
        || url.host_str() != Some("kaede.invalid")
        || url.fragment().is_some()
    {
        return false;
    }
    let Some(segments) = url.path_segments().map(Iterator::collect::<Vec<_>>) else {
        return false;
    };
    let [
        "api",
        "v1",
        "dms",
        conversation,
        "history-media",
        message,
        media,
        variant,
    ] = segments.as_slice()
    else {
        return false;
    };
    if conversation.parse::<EntityRef>().is_err() || message.parse::<EntityRef>().is_err() {
        return false;
    }
    let Ok(media_ref) = media.parse::<EntityRef>() else {
        return false;
    };
    if media_ref != EntityRef::new(attachment.id, attachment.origin_domain.clone())
        || !matches!(
            *variant,
            "original" | "thumbnail_128" | "thumbnail_512" | "thumbnail_1024" | "poster"
        )
    {
        return false;
    }
    let pairs = url.query_pairs().collect::<Vec<_>>();
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

#[derive(Debug, Error)]
pub enum AccountError {
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Id(#[from] kaede_protocol::IdError),
    #[error(transparent)]
    Api(#[from] ApiClientError),
    #[error(transparent)]
    Auth(#[from] AuthError),
    #[error(transparent)]
    Cache(#[from] CacheError),
    #[error(transparent)]
    Platform(#[from] PlatformError),
    #[error("multi-factor authentication is required")]
    MfaRequired(SecretString),
    #[error("the server returned an unexpected multi-factor authentication state")]
    UnexpectedMfaState,
    #[error("the instance did not provide a native verification key")]
    ChallengeMisconfigured,
    #[error("verification was not accepted")]
    ChallengeRejected,
    #[error("the authenticated session did not include a user identity")]
    MissingIdentity,
    #[error("the realtime connection closed")]
    GatewayClosed,
    #[error("the pending message no longer exists")]
    PendingMessageMissing,
    #[error("this message does not contain viewable media")]
    MediaUnavailable,
    #[error("desktop state payload was invalid: {0}")]
    StatePayload(#[from] serde_json::Error),
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;
    use kaede_protocol::GatewayEnvelope;

    const PUBLIC_ASSET_HASH: &str =
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    fn public_asset_user(avatar_hash: Option<&str>) -> User {
        let Ok(user) = serde_json::from_value(serde_json::json!({
            "id": "1",
            "origin_domain": "home.example",
            "username": "turtle",
            "display_name": null,
            "avatar_hash": avatar_hash,
            "banner_hash": null,
            "bio": null,
            "custom_status": null
        })) else {
            panic!("public asset user fixture should deserialize");
        };
        user
    }

    fn history_attachment(path: &str) -> Attachment {
        let Ok(attachment) = serde_json::from_value(serde_json::json!({
            "id": "60",
            "origin_domain": "remote.example",
            "filename": "photo.png",
            "content_type": "image/png",
            "size": 1024,
            "scan_status": "clean",
            "width": 64,
            "height": 64,
            "blurhash": null,
            "variants": {},
            "history_media_url": path
        })) else {
            panic!("attachment fixture should deserialize");
        };
        attachment
    }

    #[allow(deprecated)]
    #[tokio::test]
    async fn raw_password_account_entry_points_fail_closed_before_io() {
        assert!(matches!(
            AccountRuntime::register("not a domain", "turtle", None, "raw-password").await,
            Err(AccountError::Auth(AuthError::PasswordProtocolRequired))
        ));
        assert!(matches!(
            AccountRuntime::reset_password("not a domain", "token", "raw-password").await,
            Err(AccountError::Auth(AuthError::PasswordProtocolRequired))
        ));
        let (events, _receiver) = mpsc::unbounded_channel();
        assert!(matches!(
            AccountRuntime::connect(
                "not a domain",
                "turtle",
                "raw-password",
                "Kaede Desktop",
                events,
            )
            .await,
            Err(AccountError::Auth(AuthError::PasswordProtocolRequired))
        ));
    }

    #[test]
    fn public_asset_pruning_removes_paths_after_metadata_clears_hash() {
        let mut state = AppState::default();
        let user = public_asset_user(Some(PUBLIC_ASSET_HASH));
        let current_key = public_asset_key(&user.origin_domain, PUBLIC_ASSET_HASH, "thumbnail_128");
        let stale_key = public_asset_key(
            &user.origin_domain,
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "thumbnail_128",
        );
        state.hydrate_identity(user);
        state
            .public_assets
            .insert(current_key.clone(), "/cache/current".to_owned());
        state
            .public_assets
            .insert(stale_key, "/cache/stale".to_owned());

        let requests = prune_unreferenced_public_assets(&mut state);

        assert_eq!(requests.len(), 1);
        assert_eq!(
            state.public_assets.get(&current_key).map(String::as_str),
            Some("/cache/current")
        );
        assert_eq!(state.public_assets.len(), 1);

        state.hydrate_identity(public_asset_user(None));
        let requests = prune_unreferenced_public_assets(&mut state);

        assert!(requests.is_empty());
        assert!(state.public_assets.is_empty());
    }

    #[test]
    fn gateway_metadata_clear_prunes_public_asset_before_publish() {
        let mut state = AppState::default();
        let user = public_asset_user(Some(PUBLIC_ASSET_HASH));
        let key = public_asset_key(&user.origin_domain, PUBLIC_ASSET_HASH, "thumbnail_128");
        state.hydrate_identity(user);
        state.public_assets.insert(key, "/cache/avatar".to_owned());
        let Ok(payload) = serde_json::to_value(public_asset_user(None)) else {
            panic!("public asset user fixture should serialize");
        };
        let event = GatewayEnvelope {
            op: 0,
            d: payload,
            s: Some(1),
            t: Some("USER_UPDATE".to_owned()),
        };

        let Ok(reduction) = reduce_gateway_event(&mut state, event) else {
            panic!("user update should reduce");
        };

        assert!(reduction.changed);
        assert!(state.public_assets.is_empty());
        assert!(
            state
                .current_user
                .as_ref()
                .is_some_and(|user| user.avatar_hash.is_none())
        );
    }

    #[tokio::test]
    async fn unusable_in_memory_public_asset_paths_are_selected_for_revalidation()
    -> Result<(), Box<dyn std::error::Error>> {
        let directory =
            std::env::temp_dir().join(format!("kaede-app-public-assets-{}", uuid::Uuid::new_v4()));
        tokio::fs::create_dir_all(&directory).await?;
        let fresh = directory.join("fresh.asset");
        let stale = directory.join("stale.asset");
        let future = directory.join("future.asset");
        let non_file = directory.join("directory.asset");
        let missing = directory.join("missing.asset");
        tokio::fs::write(&fresh, b"fresh").await?;
        tokio::fs::write(&stale, b"stale").await?;
        tokio::fs::write(&future, b"future").await?;
        tokio::fs::create_dir(&non_file).await?;
        std::fs::File::open(&stale)?
            .set_modified(std::time::SystemTime::now() - std::time::Duration::from_secs(60 * 60))?;
        std::fs::File::open(&future)?
            .set_modified(std::time::SystemTime::now() + std::time::Duration::from_secs(60 * 60))?;

        let unusable = unusable_public_asset_entries(vec![
            ("fresh".to_owned(), fresh.to_string_lossy().into_owned()),
            ("stale".to_owned(), stale.to_string_lossy().into_owned()),
            ("future".to_owned(), future.to_string_lossy().into_owned()),
            (
                "non-file".to_owned(),
                non_file.to_string_lossy().into_owned(),
            ),
            ("missing".to_owned(), missing.to_string_lossy().into_owned()),
        ])
        .await
        .into_iter()
        .map(|(key, _)| key)
        .collect::<HashSet<_>>();

        assert_eq!(
            unusable,
            HashSet::from([
                "stale".to_owned(),
                "future".to_owned(),
                "non-file".to_owned(),
                "missing".to_owned(),
            ])
        );
        tokio::fs::remove_dir_all(directory).await?;
        Ok(())
    }

    #[test]
    fn bounded_batch_reserves_capacity_for_stale_revalidation() {
        let Ok(domain) = Domain::parse("home.example") else {
            panic!("test domain should parse");
        };
        let mut requests = HashSet::new();
        for index in 0_u8..80 {
            requests.insert((
                domain.clone(),
                format!("{index:064x}"),
                "thumbnail_128".to_owned(),
            ));
        }
        let revalidation = (
            domain.clone(),
            "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff".to_owned(),
            "thumbnail_128".to_owned(),
        );
        let revalidation_key = public_asset_key(&revalidation.0, &revalidation.1, &revalidation.2);
        requests.insert(revalidation);
        let revalidation_keys = HashSet::from([revalidation_key.clone()]);

        let scheduled =
            schedule_public_asset_requests(requests.clone(), &HashSet::new(), &revalidation_keys);

        assert_eq!(
            scheduled.len(),
            PUBLIC_ASSET_BATCH_SIZE,
            "the network batch must stay bounded"
        );
        assert_eq!(
            scheduled
                .iter()
                .filter(|request| {
                    public_asset_key(&request.0, &request.1, &request.2) == revalidation_key
                })
                .count(),
            1,
            "a recurring missing backlog must not starve an expired cache entry"
        );
        let scheduled_after_failed_revalidation =
            schedule_public_asset_requests(requests, &HashSet::new(), &revalidation_keys);
        assert!(scheduled_after_failed_revalidation.iter().any(|request| {
            public_asset_key(&request.0, &request.1, &request.2) == revalidation_key
        }));
    }

    #[tokio::test(start_paused = true)]
    async fn periodic_refresh_waits_for_interval_and_stops_with_weak_owner() {
        assert_eq!(PUBLIC_ASSET_REFRESH_INTERVAL, Duration::from_secs(60));
        assert!(PUBLIC_ASSET_REFRESH_INTERVAL < kaede_media::PUBLIC_ASSET_CACHE_TTL);
        let owner = Arc::new(());
        let invocations = Arc::new(AtomicUsize::new(0));
        let callback_invocations = Arc::clone(&invocations);
        let task = tokio::spawn(run_periodic_public_asset_refresh(
            Arc::downgrade(&owner),
            PUBLIC_ASSET_REFRESH_INTERVAL,
            move |_| {
                let callback_invocations = Arc::clone(&callback_invocations);
                async move {
                    callback_invocations.fetch_add(1, Ordering::SeqCst);
                }
            },
        ));
        tokio::task::yield_now().await;

        assert_eq!(Arc::strong_count(&owner), 1);
        assert_eq!(invocations.load(Ordering::SeqCst), 0);
        let Some(before_first_tick) =
            PUBLIC_ASSET_REFRESH_INTERVAL.checked_sub(Duration::from_secs(1))
        else {
            panic!("public asset refresh interval should exceed one second");
        };
        tokio::time::advance(before_first_tick).await;
        tokio::task::yield_now().await;
        assert_eq!(invocations.load(Ordering::SeqCst), 0);

        tokio::time::advance(Duration::from_secs(1)).await;
        tokio::task::yield_now().await;
        assert_eq!(invocations.load(Ordering::SeqCst), 1);
        assert_eq!(Arc::strong_count(&owner), 1);

        drop(owner);
        tokio::time::advance(PUBLIC_ASSET_REFRESH_INTERVAL).await;
        tokio::task::yield_now().await;
        let Ok(()) = task.await else {
            panic!("periodic refresh task should stop cleanly");
        };
        assert_eq!(invocations.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn history_media_capability_is_same_origin_and_attachment_scoped() {
        let valid = "/api/v1/dms/43@home.example/history-media/50@remote.example/60@remote.example/original?expires=2000000000&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO";
        let attachment = history_attachment(valid);
        assert_eq!(
            authenticated_attachment_media_path(&attachment, "thumbnail_512")
                .ok()
                .as_deref(),
            Some(valid)
        );
        let expired = "/api/v1/dms/43@home.example/history-media/50@remote.example/60@remote.example/original?expires=1&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO";
        assert_eq!(
            authenticated_attachment_media_path(&history_attachment(expired), "original")
                .ok()
                .as_deref(),
            Some(expired)
        );
        for invalid in [
            "https://remote.example/media/60",
            "//remote.example/media/60",
            "/api/v1/dms/43@home.example/history-media/50@remote.example/61@remote.example/original?expires=2000000000&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO",
            "/api/v1/dms/43@home.example/history-media/50@remote.example/60@remote.example/original?expires=2000000000&token=bad",
            "/api/v1/users/@me?expires=2000000000&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO",
        ] {
            assert!(
                authenticated_attachment_media_path(&history_attachment(invalid), "original")
                    .is_err(),
                "accepted unsafe history path: {invalid}"
            );
        }
    }

    #[test]
    fn voice_token_requires_a_composite_channel_reference() {
        let event = GatewayEnvelope {
            op: 0,
            d: serde_json::json!({
                "channel_id": "42",
                "channel_domain": "Remote.Example",
                "move_session_id": "abcdefghijklmnopqrstuvwxyz0123456789_AB",
                "grant": {
                    "token": "redacted",
                    "move_session_id": "abcdefghijklmnopqrstuvwxyz0123456789_AB"
                }
            }),
            s: Some(1),
            t: Some("VOICE_TOKEN".to_owned()),
        };
        assert_eq!(
            voice_reauthorization(&event)
                .map(|(value, correlation)| { (value.to_string(), correlation) }),
            Some((
                "42@remote.example".to_owned(),
                Some("abcdefghijklmnopqrstuvwxyz0123456789_AB".to_owned())
            ))
        );

        let local = GatewayEnvelope {
            d: serde_json::json!({
                "channel_id": "43",
                "channel_domain": "local.example",
                "grant": {"token": "redacted", "move_session_id": null}
            }),
            ..event.clone()
        };
        assert_eq!(
            voice_reauthorization(&local)
                .map(|(value, correlation)| (value.to_string(), correlation)),
            Some(("43@local.example".to_owned(), None))
        );

        let mismatched = GatewayEnvelope {
            d: serde_json::json!({
                "channel_id": "42",
                "channel_domain": "remote.example",
                "move_session_id": "abcdefghijklmnopqrstuvwxyz0123456789_AB",
                "grant": {"move_session_id": "z0123456789012345678901234567890"}
            }),
            ..event.clone()
        };
        assert!(voice_reauthorization(&mismatched).is_none());

        let malformed = GatewayEnvelope {
            d: serde_json::json!({"channel_id": "42"}),
            ..event
        };
        assert!(voice_reauthorization(&malformed).is_none());
    }
}
