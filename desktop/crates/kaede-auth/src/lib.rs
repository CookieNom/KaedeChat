//! Native authentication and rotating-session lifecycle.
//!
//! Public session operations uniformly return [`AuthError`]; documenting that
//! same error surface on every endpoint wrapper would add noise without adding
//! contract detail.

#![allow(clippy::missing_errors_doc)]

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use kaede_api::{ApiClient, ApiClientError};
use kaede_platform::{
    CredentialVault, PlatformError, StoredSession, TurnstileBroker, TurnstileChallenge,
};
use secrecy::{ExposeSecret, SecretString};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;
use tokio::sync::{Mutex, RwLock};

const PASSWORD_KDF_VERSION: u64 = 2;
const PASSWORD_KDF_ALGORITHM: &str = "PBKDF2-SHA256";
const PASSWORD_KDF_ITERATIONS: u64 = 600_000;

// A refresh whose response is lost has already rotated the token on the server.
// The server accepts the superseded token for a short grace period (60 seconds
// by default), so transient failures are retried with the same token, and only
// while the retry can still land inside that window.
const REFRESH_RETRY_BACKOFF: [Duration; 2] = [Duration::from_secs(1), Duration::from_secs(4)];
const REFRESH_RETRY_BUDGET: Duration = Duration::from_secs(45);

fn canonical_base64url_material(value: &str, decoded_length: usize) -> bool {
    let Ok(mut decoded) = URL_SAFE_NO_PAD.decode(value) else {
        return false;
    };
    let valid = decoded.len() == decoded_length && URL_SAFE_NO_PAD.encode(&decoded) == value;
    decoded.fill(0);
    valid
}

pub fn validate_authentication_secret(value: &str) -> Result<(), AuthError> {
    if value.len() == 43 && canonical_base64url_material(value, 32) {
        Ok(())
    } else {
        Err(AuthError::InvalidPasswordProtocol)
    }
}

pub fn validate_password_kdf(value: &Value, require_vault_salt: bool) -> Result<(), AuthError> {
    let Some(kdf) = value.as_object() else {
        return Err(AuthError::InvalidPasswordProtocol);
    };
    let expected_fields = if require_vault_salt { 5 } else { 4 };
    let auth_salt_valid = kdf
        .get("auth_salt")
        .and_then(Value::as_str)
        .is_some_and(|salt| salt.len() == 22 && canonical_base64url_material(salt, 16));
    let vault_salt_valid = !require_vault_salt
        || kdf
            .get("vault_salt")
            .and_then(Value::as_str)
            .is_some_and(|salt| salt.len() == 22 && canonical_base64url_material(salt, 16));
    if kdf.len() == expected_fields
        && kdf.get("version").and_then(Value::as_u64) == Some(PASSWORD_KDF_VERSION)
        && kdf.get("algorithm").and_then(Value::as_str) == Some(PASSWORD_KDF_ALGORITHM)
        && kdf.get("iterations").and_then(Value::as_u64) == Some(PASSWORD_KDF_ITERATIONS)
        && auth_salt_valid
        && vault_salt_valid
    {
        Ok(())
    } else {
        Err(AuthError::InvalidPasswordProtocol)
    }
}

#[derive(Clone, Debug, Deserialize)]
pub struct SessionSummary {
    pub id: String,
    pub device_name: Option<String>,
    pub user_agent: Option<String>,
    pub ip_address: Option<String>,
    pub created_at: String,
    pub last_used_at: String,
    pub expires_at: String,
    pub current: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct AuthConfig {
    pub email_required: bool,
    pub password_recovery_enabled: bool,
    pub turnstile: TurnstileConfig,
    pub gif_picker_enabled: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct TurnstileConfig {
    pub enabled: bool,
    pub site_key: Option<String>,
}

#[derive(Serialize)]
struct LoginRequest<'a> {
    identifier: &'a str,
    password: &'a str,
    password_kdf_version: u8,
    device_name: &'a str,
    turnstile_token: Option<&'a str>,
}

#[derive(Serialize)]
struct RefreshRequest<'a> {
    refresh_token: &'a str,
}

#[derive(Serialize)]
struct RegisterRequest<'a> {
    username: &'a str,
    email: Option<&'a str>,
    password: &'a str,
    password_kdf: &'a Value,
    turnstile_token: Option<&'a str>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RegistrationResult {
    pub id: String,
    pub handle: String,
    pub email_verification_required: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct StatusResult {
    pub status: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct MfaSetupResult {
    pub secret: SecretString,
    pub uri: SecretString,
}

#[derive(Clone, Debug, Deserialize)]
pub struct MfaEnableResult {
    pub status: String,
    pub recovery_codes: Vec<SecretString>,
}

#[derive(Serialize)]
struct MfaRequest<'a> {
    ticket: &'a str,
    code: &'a str,
    device_name: &'a str,
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: Option<SecretString>,
    refresh_token: Option<SecretString>,
    mfa_required: bool,
    mfa_ticket: Option<SecretString>,
}

#[derive(Clone, Debug)]
pub enum LoginOutcome {
    Authenticated,
    MfaRequired(SecretString),
    ChallengeRequired,
}

pub struct SessionManager<V, T>
where
    V: CredentialVault,
    T: TurnstileBroker,
{
    api: ApiClient,
    vault: Arc<V>,
    turnstile: Arc<T>,
    account_key: String,
    session: RwLock<Option<StoredSession>>,
    refresh_lock: Mutex<()>,
    /// Incremented whenever new tokens are adopted, so callers that queued
    /// behind an in-flight refresh reuse its result instead of rotating again.
    generation: AtomicU64,
}

impl<V, T> SessionManager<V, T>
where
    V: CredentialVault,
    T: TurnstileBroker,
{
    #[must_use]
    pub fn new(api: ApiClient, vault: Arc<V>, turnstile: Arc<T>, account_key: String) -> Self {
        Self {
            api,
            vault,
            turnstile,
            account_key,
            session: RwLock::new(None),
            refresh_lock: Mutex::new(()),
            generation: AtomicU64::new(0),
        }
    }

    pub async fn restore(&self) -> Result<bool, AuthError> {
        let Some(session) = self.vault.load(&self.account_key).await? else {
            return Ok(false);
        };
        self.adopt(session).await;
        Ok(true)
    }

    pub async fn config(&self) -> Result<AuthConfig, AuthError> {
        self.api.get("auth/config").await.map_err(Into::into)
    }

    // Preserve the published async API while making the unsafe unprepared
    // password path fail before it can perform network I/O.
    #[allow(clippy::unused_async)]
    pub async fn register(
        &self,
        username: &str,
        email: Option<&str>,
        password: &str,
        challenge_token: Option<&SecretString>,
    ) -> Result<RegistrationResult, AuthError> {
        let _ = (username, email, password, challenge_token);
        Err(AuthError::PasswordProtocolRequired)
    }

    /// Register using password material and KDF metadata prepared by a trusted
    /// frontend.
    pub async fn register_with_password_protocol(
        &self,
        username: &str,
        email: Option<&str>,
        password: &str,
        password_kdf: &Value,
        challenge_token: Option<&SecretString>,
    ) -> Result<RegistrationResult, AuthError> {
        validate_authentication_secret(password)?;
        validate_password_kdf(password_kdf, true)?;
        self.api
            .post(
                "auth/register",
                &RegisterRequest {
                    username,
                    email,
                    password,
                    password_kdf,
                    turnstile_token: challenge_token.map(ExposeSecret::expose_secret),
                },
            )
            .await
            .map_err(Into::into)
    }

    pub async fn verify_email(&self, token: &SecretString) -> Result<StatusResult, AuthError> {
        self.api
            .post(
                "auth/verify-email",
                &serde_json::json!({"token": token.expose_secret()}),
            )
            .await
            .map_err(Into::into)
    }

    pub async fn resend_verification(&self, email: &str) -> Result<StatusResult, AuthError> {
        self.api
            .post(
                "auth/verify-email/resend",
                &serde_json::json!({"email": email}),
            )
            .await
            .map_err(Into::into)
    }

    pub async fn forgot_password(&self, email: &str) -> Result<StatusResult, AuthError> {
        self.api
            .post("auth/password/forgot", &serde_json::json!({"email": email}))
            .await
            .map_err(Into::into)
    }

    // Preserve the published async API while this legacy-shaped entry point
    // deliberately fails closed without awaiting network I/O.
    #[allow(clippy::unused_async)]
    pub async fn reset_password(
        &self,
        token: &SecretString,
        password: &str,
    ) -> Result<StatusResult, AuthError> {
        let _ = (token, password);
        Err(AuthError::PasswordProtocolRequired)
    }

    /// Replace a password using a client-derived authentication secret and
    /// exact KDF-v2 authentication metadata. The server chooses a fresh vault
    /// salt after the reset, so this request intentionally has no vault salt.
    pub async fn reset_password_with_password_protocol(
        &self,
        token: &SecretString,
        password: &str,
        password_kdf: &Value,
    ) -> Result<StatusResult, AuthError> {
        validate_authentication_secret(password)?;
        validate_password_kdf(password_kdf, false)?;
        self.api
            .post(
                "auth/password/reset",
                &serde_json::json!({
                    "token": token.expose_secret(),
                    "password": password,
                    "password_kdf": password_kdf,
                }),
            )
            .await
            .map_err(Into::into)
    }

    // Preserve the published async API while this legacy-shaped entry point
    // deliberately fails closed without awaiting network I/O.
    #[allow(clippy::unused_async)]
    pub async fn request_email_change(
        &self,
        email: &str,
        password: &str,
    ) -> Result<StatusResult, AuthError> {
        let _ = (email, password);
        Err(AuthError::PasswordProtocolRequired)
    }

    pub async fn request_email_change_with_password_protocol(
        &self,
        email: &str,
        password: &str,
    ) -> Result<StatusResult, AuthError> {
        validate_authentication_secret(password)?;
        self.api
            .post(
                "auth/email/change",
                &serde_json::json!({
                    "email": email,
                    "password": password,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                }),
            )
            .await
            .map_err(Into::into)
    }

    pub async fn confirm_email_change(
        &self,
        token: &SecretString,
    ) -> Result<StatusResult, AuthError> {
        self.api
            .post(
                "auth/email/change/confirm",
                &serde_json::json!({"token": token.expose_secret()}),
            )
            .await
            .map_err(Into::into)
    }

    // Preserve the published async API while this legacy-shaped entry point
    // deliberately fails closed without awaiting network I/O.
    #[allow(clippy::unused_async)]
    pub async fn setup_mfa(
        &self,
        password: &str,
        current_code: Option<&str>,
    ) -> Result<MfaSetupResult, AuthError> {
        let _ = (password, current_code);
        Err(AuthError::PasswordProtocolRequired)
    }

    pub async fn setup_mfa_with_password_protocol(
        &self,
        password: &str,
        current_code: Option<&str>,
    ) -> Result<MfaSetupResult, AuthError> {
        validate_authentication_secret(password)?;
        self.api
            .post(
                "auth/mfa/setup",
                &serde_json::json!({
                    "password": password,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                    "current_code": current_code,
                }),
            )
            .await
            .map_err(Into::into)
    }

    pub async fn enable_mfa(&self, code: &str) -> Result<MfaEnableResult, AuthError> {
        self.api
            .post("auth/mfa/enable", &serde_json::json!({"code": code}))
            .await
            .map_err(Into::into)
    }

    // Preserve the published async API while this legacy-shaped entry point
    // deliberately fails closed without awaiting network I/O.
    #[allow(clippy::unused_async)]
    pub async fn disable_mfa(&self, password: &str, code: &str) -> Result<StatusResult, AuthError> {
        let _ = (password, code);
        Err(AuthError::PasswordProtocolRequired)
    }

    pub async fn disable_mfa_with_password_protocol(
        &self,
        password: &str,
        code: &str,
    ) -> Result<StatusResult, AuthError> {
        validate_authentication_secret(password)?;
        self.api
            .post(
                "auth/mfa/disable",
                &serde_json::json!({
                    "password": password,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                    "code": code,
                }),
            )
            .await
            .map_err(Into::into)
    }

    pub async fn sessions(&self) -> Result<Vec<SessionSummary>, AuthError> {
        self.api.get("auth/sessions").await.map_err(Into::into)
    }

    pub async fn revoke_session(&self, session_id: &str) -> Result<(), AuthError> {
        self.api
            .delete_empty(&format!("auth/sessions/{session_id}"))
            .await
            .map_err(Into::into)
    }

    // Preserve the published async API while making the unsafe unprepared
    // password path fail before it can perform network I/O.
    #[allow(clippy::unused_async)]
    pub async fn login(
        &self,
        identifier: &str,
        password: &str,
        device_name: &str,
        challenge_token: Option<&SecretString>,
    ) -> Result<LoginOutcome, AuthError> {
        let _ = (identifier, password, device_name, challenge_token);
        Err(AuthError::PasswordProtocolRequired)
    }

    /// Authenticate with KDF-v2 password material prepared by the frontend.
    pub async fn login_with_password_protocol(
        &self,
        identifier: &str,
        password: &str,
        device_name: &str,
        challenge_token: Option<&SecretString>,
    ) -> Result<LoginOutcome, AuthError> {
        validate_authentication_secret(password)?;
        let response = self
            .api
            .post::<_, TokenResponse>(
                "auth/login",
                &LoginRequest {
                    identifier,
                    password,
                    password_kdf_version: 2,
                    device_name,
                    turnstile_token: challenge_token.map(ExposeSecret::expose_secret),
                },
            )
            .await;
        match response {
            Ok(tokens) => self.accept_tokens(tokens).await,
            Err(ApiClientError::Server { error, .. }) if error.code == "TURNSTILE_REQUIRED" => {
                Ok(LoginOutcome::ChallengeRequired)
            }
            Err(error) => Err(error.into()),
        }
    }

    /// Authenticate with material prepared against an exact KDF-v2 context.
    /// This stronger boundary is intended for native consumers that perform
    /// the public KDF lookup themselves.
    pub async fn login_with_prepared_password(
        &self,
        identifier: &str,
        password: &SecretString,
        password_kdf: &Value,
        device_name: &str,
        challenge_token: Option<&SecretString>,
    ) -> Result<LoginOutcome, AuthError> {
        validate_password_kdf(password_kdf, true)?;
        self.login_with_password_protocol(
            identifier,
            password.expose_secret(),
            device_name,
            challenge_token,
        )
        .await
    }

    pub async fn solve_turnstile(
        &self,
        site_key: String,
        action: &str,
        request_id: String,
    ) -> Result<SecretString, AuthError> {
        let origin = self.api.endpoint().public_origin();
        self.turnstile
            .solve(TurnstileChallenge {
                origin,
                site_key,
                action: action.to_owned(),
                request_id,
            })
            .await
            .map_err(Into::into)
    }

    pub async fn refresh(&self) -> Result<(), AuthError> {
        // Each rotation revokes the access tokens issued by the previous one, so
        // callers that hit a 401 together must share a single rotation.
        let observed = self.generation.load(Ordering::Acquire);
        let _guard = self.refresh_lock.lock().await;
        if self.generation.load(Ordering::Acquire) != observed {
            return Ok(());
        }
        let refresh_token = self
            .session
            .read()
            .await
            .as_ref()
            .map(|session| session.refresh_token.clone())
            .ok_or(AuthError::NotAuthenticated)?;
        let response = match self.request_refresh(&refresh_token).await {
            Ok(response) => response,
            Err(ApiClientError::Server { status, error }) if status.as_u16() == 401 => {
                // The server has retired this session for good. Drop it so the
                // app signs out instead of retrying a token that cannot recover.
                self.discard_rejected_session().await;
                return Err(ApiClientError::Server { status, error }.into());
            }
            Err(error) => return Err(error.into()),
        };
        if response.mfa_required {
            return Err(AuthError::UnexpectedAuthState);
        }
        let session = native_tokens(response)?;
        // The previous refresh token is already retired server-side. Adopt the
        // new one before persisting it: if the vault write fails, this process
        // keeps working, and the next successful refresh saves again.
        self.adopt(session.clone()).await;
        if let Err(error) = self.vault.save(&self.account_key, &session).await {
            tracing::warn!(%error, "could not save the rotated desktop session");
        }
        Ok(())
    }

    async fn request_refresh(
        &self,
        refresh_token: &SecretString,
    ) -> Result<TokenResponse, ApiClientError> {
        let started = Instant::now();
        let mut backoff = REFRESH_RETRY_BACKOFF.into_iter();
        loop {
            let error = match self
                .api
                .post(
                    "auth/refresh",
                    &RefreshRequest {
                        refresh_token: refresh_token.expose_secret(),
                    },
                )
                .await
            {
                Ok(response) => return Ok(response),
                Err(error) => error,
            };
            let Some(delay) = backoff
                .next()
                .and_then(|backoff| refresh_retry_delay(&error, backoff))
                .filter(|delay| started.elapsed() + *delay <= REFRESH_RETRY_BUDGET)
            else {
                return Err(error);
            };
            tracing::warn!(%error, ?delay, "session refresh failed; retrying");
            tokio::time::sleep(delay).await;
        }
    }

    async fn discard_rejected_session(&self) {
        self.api.set_access_token(None).await;
        *self.session.write().await = None;
        if let Err(error) = self.vault.delete(&self.account_key).await {
            tracing::warn!(%error, "could not remove the rejected desktop session");
        }
    }

    async fn adopt(&self, session: StoredSession) {
        self.api
            .set_access_token(Some(session.access_token.clone()))
            .await;
        *self.session.write().await = Some(session);
        self.generation.fetch_add(1, Ordering::AcqRel);
    }

    pub async fn complete_mfa(
        &self,
        ticket: &SecretString,
        code: &str,
        device_name: &str,
    ) -> Result<LoginOutcome, AuthError> {
        let response: TokenResponse = self
            .api
            .post(
                "auth/mfa",
                &MfaRequest {
                    ticket: ticket.expose_secret(),
                    code,
                    device_name,
                },
            )
            .await?;
        self.accept_tokens(response).await
    }

    pub async fn access_token(&self) -> Result<SecretString, AuthError> {
        self.session
            .read()
            .await
            .as_ref()
            .map(|session| session.access_token.clone())
            .ok_or(AuthError::NotAuthenticated)
    }

    pub async fn logout(&self) -> Result<(), AuthError> {
        let _guard = self.refresh_lock.lock().await;
        let server_result: Result<serde_json::Value, ApiClientError> =
            self.api.post("auth/logout", &serde_json::json!({})).await;
        self.api.set_access_token(None).await;
        *self.session.write().await = None;
        self.vault.delete(&self.account_key).await?;
        if let Err(error) = server_result {
            tracing::warn!(
                ?error,
                "server logout failed after local credentials were removed"
            );
        }
        Ok(())
    }

    async fn accept_tokens(&self, response: TokenResponse) -> Result<LoginOutcome, AuthError> {
        if response.mfa_required {
            return response
                .mfa_ticket
                .map(LoginOutcome::MfaRequired)
                .ok_or(AuthError::UnexpectedAuthState);
        }
        let session = native_tokens(response)?;
        self.vault.save(&self.account_key, &session).await?;
        self.adopt(session).await;
        Ok(LoginOutcome::Authenticated)
    }
}

fn native_tokens(response: TokenResponse) -> Result<StoredSession, AuthError> {
    Ok(StoredSession {
        access_token: response
            .access_token
            .ok_or(AuthError::MissingNativeTokens)?,
        refresh_token: response
            .refresh_token
            .ok_or(AuthError::MissingNativeTokens)?,
    })
}

/// Returns how long to wait before retrying a failed refresh, or `None` when
/// the failure is a definite answer that retrying cannot change.
fn refresh_retry_delay(error: &ApiClientError, backoff: Duration) -> Option<Duration> {
    match error {
        // The server may have committed the rotation before the response was
        // lost or garbled; the grace period makes the same token valid again.
        ApiClientError::Transport(_) | ApiClientError::Decode(_) => Some(backoff),
        ApiClientError::Server { status, error } => match status.as_u16() {
            429 | 500 | 502 | 503 | 504 => {
                let delay = error
                    .retry_after_ms
                    .map_or(backoff, |ms| Duration::from_millis(ms).max(backoff));
                Some(delay)
            }
            _ => None,
        },
        _ => None,
    }
}

#[derive(Debug, Error)]
pub enum AuthError {
    #[error(transparent)]
    Api(#[from] ApiClientError),
    #[error(transparent)]
    Platform(#[from] PlatformError),
    #[error("desktop authentication response did not contain native tokens")]
    MissingNativeTokens,
    #[error("the account is not authenticated")]
    NotAuthenticated,
    #[error("the server returned an unexpected authentication state")]
    UnexpectedAuthState,
    #[error("password KDF v2 material must be prepared by a trusted client")]
    PasswordProtocolRequired,
    #[error("prepared password material is not canonical KDF v2 data")]
    InvalidPasswordProtocol,
}

#[cfg(test)]
mod tests {
    use std::error::Error;
    use std::sync::Arc;

    use async_trait::async_trait;
    use kaede_api::{ApiClient, ApiClientError, InstanceEndpoint};
    use kaede_platform::{
        CredentialVault, PlatformError, StoredSession, TurnstileBroker, TurnstileChallenge,
    };
    use kaede_protocol::Domain;
    use secrecy::{ExposeSecret, SecretString};
    use serde_json::json;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, TcpStream};
    use tokio::sync::Mutex;
    use tokio::task::JoinHandle;
    use url::Url;

    use super::{
        AuthError, LoginRequest, RegisterRequest, SessionManager, validate_authentication_secret,
        validate_password_kdf,
    };

    const ROTATED: &str =
        r#"{"access_token":"access-2","refresh_token":"kc1_rt_rotated","mfa_required":false}"#;
    const ROTATED_AGAIN: &str =
        r#"{"access_token":"access-3","refresh_token":"kc1_rt_third","mfa_required":false}"#;
    const UNAVAILABLE: &str =
        r#"{"detail":{"code":"SERVICE_UNAVAILABLE","message":"Try again shortly"}}"#;
    const INVALID_REFRESH: &str =
        r#"{"detail":{"code":"INVALID_REFRESH_TOKEN","message":"Refresh token is invalid"}}"#;

    struct MemoryVault {
        saved: Mutex<Option<StoredSession>>,
        fail_saves: bool,
    }

    impl MemoryVault {
        fn holding(refresh_token: &str, fail_saves: bool) -> Arc<Self> {
            Arc::new(Self {
                saved: Mutex::new(Some(StoredSession {
                    access_token: SecretString::from("access-1"),
                    refresh_token: SecretString::from(refresh_token),
                })),
                fail_saves,
            })
        }
    }

    #[async_trait]
    impl CredentialVault for MemoryVault {
        async fn load(&self, _: &str) -> Result<Option<StoredSession>, PlatformError> {
            Ok(self.saved.lock().await.clone())
        }

        async fn save(&self, _: &str, session: &StoredSession) -> Result<(), PlatformError> {
            if self.fail_saves {
                return Err(PlatformError::Other(
                    "credential manager is locked".to_owned(),
                ));
            }
            *self.saved.lock().await = Some(session.clone());
            Ok(())
        }

        async fn delete(&self, _: &str) -> Result<(), PlatformError> {
            *self.saved.lock().await = None;
            Ok(())
        }
    }

    struct NoTurnstile;

    #[async_trait]
    impl TurnstileBroker for NoTurnstile {
        async fn solve(&self, _: TurnstileChallenge) -> Result<SecretString, PlatformError> {
            Err(PlatformError::ChallengeCancelled)
        }
    }

    type Server = JoinHandle<std::io::Result<Vec<String>>>;

    /// Answers one request per connection with the scripted replies and
    /// returns the request bodies it received.
    async fn serve(replies: Vec<(u16, &'static str)>) -> Result<(Url, Server), Box<dyn Error>> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
        let origin = Url::parse(&format!("http://{}/", listener.local_addr()?))?;
        let server = tokio::spawn(async move {
            let mut bodies = Vec::new();
            for (status, body) in replies {
                let (mut stream, _) = listener.accept().await?;
                bodies.push(read_body(&mut stream).await?);
                let reply = format!(
                    "HTTP/1.1 {status} Scripted\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream.write_all(reply.as_bytes()).await?;
            }
            Ok(bodies)
        });
        Ok((origin, server))
    }

    async fn read_body(stream: &mut TcpStream) -> std::io::Result<String> {
        let mut request = Vec::new();
        let mut chunk = [0_u8; 1_024];
        loop {
            let read = stream.read(&mut chunk).await?;
            if read == 0 {
                return Ok(String::new());
            }
            request.extend_from_slice(&chunk[..read]);
            let text = String::from_utf8_lossy(&request).into_owned();
            let Some(head_end) = text.find("\r\n\r\n") else {
                continue;
            };
            let length = text[..head_end]
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().ok())
                        .flatten()
                })
                .unwrap_or(0);
            let body_start = head_end + 4;
            if request.len() >= body_start + length {
                return Ok(
                    String::from_utf8_lossy(&request[body_start..body_start + length]).into_owned(),
                );
            }
        }
    }

    async fn restored_session(
        origin: &Url,
        vault: Arc<MemoryVault>,
    ) -> Result<SessionManager<MemoryVault, NoTurnstile>, Box<dyn Error>> {
        let endpoint = InstanceEndpoint::development(Domain::parse("dev.example")?, origin)?;
        let session = SessionManager::new(
            ApiClient::new(endpoint)?,
            vault,
            Arc::new(NoTurnstile),
            "maple@dev.example".to_owned(),
        );
        assert!(session.restore().await?);
        Ok(session)
    }

    #[tokio::test]
    async fn refresh_retries_transient_failures_with_the_same_token() -> Result<(), Box<dyn Error>>
    {
        let (origin, server) = serve(vec![(503, UNAVAILABLE), (200, ROTATED)]).await?;
        let vault = MemoryVault::holding("kc1_rt_original", false);
        let session = restored_session(&origin, vault.clone()).await?;

        session.refresh().await?;

        let bodies = server.await??;
        assert_eq!(bodies.len(), 2);
        assert!(bodies.iter().all(|body| body.contains("kc1_rt_original")));
        assert_eq!(session.access_token().await?.expose_secret(), "access-2");
        let Some(saved) = vault.saved.lock().await.clone() else {
            panic!("the rotated session should be saved");
        };
        assert_eq!(saved.refresh_token.expose_secret(), "kc1_rt_rotated");
        Ok(())
    }

    #[tokio::test]
    async fn failed_vault_write_keeps_using_the_rotated_token() -> Result<(), Box<dyn Error>> {
        let (origin, server) = serve(vec![(200, ROTATED), (200, ROTATED_AGAIN)]).await?;
        let vault = MemoryVault::holding("kc1_rt_original", true);
        let session = restored_session(&origin, vault).await?;

        session.refresh().await?;
        session.refresh().await?;

        let bodies = server.await??;
        assert!(bodies[0].contains("kc1_rt_original"));
        assert!(bodies[1].contains("kc1_rt_rotated"));
        assert_eq!(session.access_token().await?.expose_secret(), "access-3");
        Ok(())
    }

    #[tokio::test]
    async fn concurrent_refreshes_share_one_rotation() -> Result<(), Box<dyn Error>> {
        let (origin, server) = serve(vec![(200, ROTATED)]).await?;
        let session =
            restored_session(&origin, MemoryVault::holding("kc1_rt_original", false)).await?;

        let (first, second) = tokio::join!(session.refresh(), session.refresh());

        first?;
        second?;
        assert_eq!(server.await??.len(), 1);
        assert_eq!(session.access_token().await?.expose_secret(), "access-2");
        Ok(())
    }

    #[tokio::test]
    async fn rejected_refresh_discards_the_session_without_retrying() -> Result<(), Box<dyn Error>>
    {
        let (origin, server) = serve(vec![(401, INVALID_REFRESH)]).await?;
        let vault = MemoryVault::holding("kc1_rt_revoked", false);
        let session = restored_session(&origin, vault.clone()).await?;

        assert!(matches!(
            session.refresh().await,
            Err(AuthError::Api(ApiClientError::Server { status, .. })) if status.as_u16() == 401
        ));
        assert!(matches!(
            session.refresh().await,
            Err(AuthError::NotAuthenticated)
        ));
        assert_eq!(server.await??.len(), 1);
        assert!(vault.saved.lock().await.is_none());
        Ok(())
    }

    fn password_kdf(include_vault_salt: bool) -> serde_json::Value {
        let mut value = json!({
            "version": 2,
            "algorithm": "PBKDF2-SHA256",
            "iterations": 600_000,
            "auth_salt": "A".repeat(22),
        });
        if include_vault_salt {
            value["vault_salt"] = json!("A".repeat(22));
        }
        value
    }

    #[test]
    fn password_request_serde_preserves_supplied_protocol_fields() {
        let Ok(login) = serde_json::to_value(LoginRequest {
            identifier: "turtle",
            password: "authentication-secret",
            password_kdf_version: 2,
            device_name: "Kaede Desktop",
            turnstile_token: None,
        }) else {
            panic!("login request should serialize");
        };
        assert_eq!(login["password_kdf_version"], 2);
        assert!(login.get("password_upgrade").is_none());

        let kdf = json!({"version": 2, "algorithm": "PBKDF2-SHA256"});
        let Ok(registration) = serde_json::to_value(RegisterRequest {
            username: "turtle",
            email: None,
            password: "authentication-secret",
            password_kdf: &kdf,
            turnstile_token: None,
        }) else {
            panic!("registration request should serialize");
        };
        assert_eq!(registration["password_kdf"], kdf);
    }

    #[test]
    fn prepared_password_boundary_accepts_only_canonical_v2_material() {
        assert!(validate_authentication_secret(&"A".repeat(43)).is_ok());
        assert!(validate_password_kdf(&password_kdf(true), true).is_ok());
        assert!(validate_password_kdf(&password_kdf(false), false).is_ok());

        assert!(matches!(
            validate_authentication_secret("literal-password"),
            Err(AuthError::InvalidPasswordProtocol)
        ));
        for invalid in [
            json!({
                "version": 0,
                "algorithm": "legacy",
                "iterations": 0,
                "auth_salt": null,
                "vault_salt": "A".repeat(22),
            }),
            {
                let mut value = password_kdf(true);
                value["iterations"] = json!(1);
                value
            },
            {
                let mut value = password_kdf(true);
                value["unexpected"] = json!(true);
                value
            },
        ] {
            assert!(matches!(
                validate_password_kdf(&invalid, true),
                Err(AuthError::InvalidPasswordProtocol)
            ));
        }
        assert!(matches!(
            validate_password_kdf(&password_kdf(false), true),
            Err(AuthError::InvalidPasswordProtocol)
        ));
        for (field, invalid) in [
            ("version", json!(0)),
            ("algorithm", json!("legacy")),
            ("iterations", json!(1)),
            ("auth_salt", serde_json::Value::Null),
            ("auth_salt", json!("A".repeat(21))),
            ("auth_salt", json!(format!("{}B", "A".repeat(21)))),
            ("vault_salt", json!(format!("{}=", "A".repeat(22)))),
        ] {
            let mut value = password_kdf(true);
            value[field] = invalid;
            assert!(
                matches!(
                    validate_password_kdf(&value, true),
                    Err(AuthError::InvalidPasswordProtocol)
                ),
                "{field}"
            );
        }
        assert!(validate_authentication_secret(&format!("{}B", "A".repeat(42))).is_err());
    }
}
