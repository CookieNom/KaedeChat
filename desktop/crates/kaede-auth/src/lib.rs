//! Native authentication and rotating-session lifecycle.
//!
//! Public session operations uniformly return [`AuthError`]; documenting that
//! same error surface on every endpoint wrapper would add noise without adding
//! contract detail.

#![allow(clippy::missing_errors_doc)]

use std::sync::Arc;

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

fn canonical_base64url_material(value: &str, decoded_length: usize) -> bool {
    let Ok(mut decoded) = URL_SAFE_NO_PAD.decode(value) else {
        return false;
    };
    let valid = decoded.len() == decoded_length && URL_SAFE_NO_PAD.encode(&decoded) == value;
    decoded.fill(0);
    valid
}

fn validate_authentication_secret(value: &str) -> Result<(), AuthError> {
    if value.len() == 43 && canonical_base64url_material(value, 32) {
        Ok(())
    } else {
        Err(AuthError::InvalidPasswordProtocol)
    }
}

fn validate_password_kdf(value: &Value, require_vault_salt: bool) -> Result<(), AuthError> {
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
        }
    }

    pub async fn restore(&self) -> Result<bool, AuthError> {
        let Some(session) = self.vault.load(&self.account_key).await? else {
            return Ok(false);
        };
        self.api
            .set_access_token(Some(session.access_token.clone()))
            .await;
        *self.session.write().await = Some(session);
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
        let _guard = self.refresh_lock.lock().await;
        let refresh_token = self
            .session
            .read()
            .await
            .as_ref()
            .map(|session| session.refresh_token.clone())
            .ok_or(AuthError::NotAuthenticated)?;
        let response: TokenResponse = self
            .api
            .post(
                "auth/refresh",
                &RefreshRequest {
                    refresh_token: refresh_token.expose_secret(),
                },
            )
            .await?;
        match self.accept_tokens(response).await? {
            LoginOutcome::Authenticated => Ok(()),
            LoginOutcome::MfaRequired(_) | LoginOutcome::ChallengeRequired => {
                Err(AuthError::UnexpectedAuthState)
            }
        }
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
        let session = StoredSession {
            access_token: response
                .access_token
                .ok_or(AuthError::MissingNativeTokens)?,
            refresh_token: response
                .refresh_token
                .ok_or(AuthError::MissingNativeTokens)?,
        };
        self.vault.save(&self.account_key, &session).await?;
        self.api
            .set_access_token(Some(session.access_token.clone()))
            .await;
        *self.session.write().await = Some(session);
        Ok(LoginOutcome::Authenticated)
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
    use serde_json::json;

    use super::{
        AuthError, LoginRequest, RegisterRequest, validate_authentication_secret,
        validate_password_kdf,
    };

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
    fn password_protocol_fields_are_always_version_two() {
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
    }
}
