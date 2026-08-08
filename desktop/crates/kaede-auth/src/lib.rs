//! Native authentication and rotating-session lifecycle.
//!
//! Public session operations uniformly return [`AuthError`]; documenting that
//! same error surface on every endpoint wrapper would add noise without adding
//! contract detail.

#![allow(clippy::missing_errors_doc)]

use std::sync::Arc;

use kaede_api::{ApiClient, ApiClientError};
use kaede_platform::{
    CredentialVault, PlatformError, StoredSession, TurnstileBroker, TurnstileChallenge,
};
use secrecy::{ExposeSecret, SecretString};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::sync::{Mutex, RwLock};

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
    turnstile_token: Option<&'a str>,
}

#[derive(Clone, Debug, Deserialize)]
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

    pub async fn register(
        &self,
        username: &str,
        email: Option<&str>,
        password: &str,
        challenge_token: Option<&SecretString>,
    ) -> Result<RegistrationResult, AuthError> {
        self.api
            .post(
                "auth/register",
                &RegisterRequest {
                    username,
                    email,
                    password,
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

    pub async fn reset_password(
        &self,
        token: &SecretString,
        password: &str,
    ) -> Result<StatusResult, AuthError> {
        self.api
            .post(
                "auth/password/reset",
                &serde_json::json!({"token": token.expose_secret(), "password": password}),
            )
            .await
            .map_err(Into::into)
    }

    pub async fn request_email_change(
        &self,
        email: &str,
        password: &str,
    ) -> Result<StatusResult, AuthError> {
        self.api
            .post(
                "auth/email/change",
                &serde_json::json!({"email": email, "password": password}),
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

    pub async fn setup_mfa(
        &self,
        password: &str,
        current_code: Option<&str>,
    ) -> Result<MfaSetupResult, AuthError> {
        self.api
            .post(
                "auth/mfa/setup",
                &serde_json::json!({"password": password, "current_code": current_code}),
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

    pub async fn disable_mfa(&self, password: &str, code: &str) -> Result<StatusResult, AuthError> {
        self.api
            .post(
                "auth/mfa/disable",
                &serde_json::json!({"password": password, "code": code}),
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

    pub async fn login(
        &self,
        identifier: &str,
        password: &str,
        device_name: &str,
        challenge_token: Option<&SecretString>,
    ) -> Result<LoginOutcome, AuthError> {
        let response = self
            .api
            .post::<_, TokenResponse>(
                "auth/login",
                &LoginRequest {
                    identifier,
                    password,
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
}
