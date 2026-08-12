//! Hardened HTTP transport for the Kaede home-instance API.
//!
//! Every fallible public transport helper returns [`ApiClientError`]. Keeping
//! that invariant here avoids repeating the same error documentation for each
//! HTTP verb wrapper.

#![allow(clippy::missing_errors_doc)]

use std::{collections::HashMap, sync::Arc, time::Duration};

use bytes::Bytes;
use kaede_protocol::{ApiError, Domain, ResourceVersion};
use reqwest::{Method, StatusCode, header};
use secrecy::{ExposeSecret, SecretString};
use serde::{Serialize, de::DeserializeOwned};
use thiserror::Error;
use tokio::sync::RwLock;
use url::Url;

pub mod service;

const CLIENT_HEADER: &str = "X-Kaede-Client";
const CLIENT_KIND: &str = "desktop";

/// A successful JSON response with the wire metadata needed by the shared UI.
///
/// Status codes are significant for queued federated writes and empty
/// responses. Selected response headers carry optimistic-concurrency and rate
/// limit information without exposing unrelated transport details.
#[derive(Clone, Debug)]
pub struct JsonResponse {
    pub status: StatusCode,
    pub headers: HashMap<String, String>,
    pub body: serde_json::Value,
}

#[derive(Clone, Debug)]
pub struct InstanceEndpoint {
    domain: Domain,
    api_base: Url,
    gateway: Url,
}

impl InstanceEndpoint {
    /// Builds the canonical HTTPS API and WSS gateway endpoints for an instance.
    ///
    /// # Errors
    ///
    /// Returns an error if the normalized domain cannot be represented as a URL.
    pub fn production(domain: Domain) -> Result<Self, ApiClientError> {
        let api_base = Url::parse(&format!("https://{domain}/api/v1/"))?;
        let gateway = Url::parse(&format!(
            "wss://{domain}/gateway?v={}&encoding=json",
            kaede_protocol::PROTOCOL_VERSION
        ))?;
        Ok(Self {
            domain,
            api_base,
            gateway,
        })
    }

    /// Builds endpoints for a local development origin.
    ///
    /// Plain HTTP is deliberately restricted to loopback addresses.
    ///
    /// # Errors
    ///
    /// Returns an error for malformed endpoints or insecure non-loopback HTTP.
    pub fn development(domain: Domain, origin: &Url) -> Result<Self, ApiClientError> {
        if origin.scheme() != "http" && origin.scheme() != "https" {
            return Err(ApiClientError::InsecureEndpoint);
        }
        if origin.scheme() == "http" && !is_loopback_url(origin) {
            return Err(ApiClientError::InsecureEndpoint);
        }
        let api_base = origin.join("/api/v1/")?;
        let mut gateway = origin.join(&format!(
            "/gateway?v={}&encoding=json",
            kaede_protocol::PROTOCOL_VERSION
        ))?;
        gateway
            .set_scheme(if origin.scheme() == "https" {
                "wss"
            } else {
                "ws"
            })
            .map_err(|()| ApiClientError::InvalidEndpoint)?;
        Ok(Self {
            domain,
            api_base,
            gateway,
        })
    }

    #[must_use]
    pub fn domain(&self) -> &Domain {
        &self.domain
    }

    #[must_use]
    pub fn gateway_url(&self) -> &Url {
        &self.gateway
    }

    #[must_use]
    pub fn public_origin(&self) -> Url {
        let mut origin = self.api_base.clone();
        origin.set_path("/");
        origin.set_query(None);
        origin.set_fragment(None);
        origin
    }

    fn api_url(&self, path: &str) -> Result<Url, ApiClientError> {
        let relative = path.trim_start_matches('/');
        if relative.is_empty()
            || relative.contains("://")
            || relative.contains('\\')
            || relative
                .split('/')
                .any(|component| component == "." || component == "..")
        {
            return Err(ApiClientError::InvalidEndpoint);
        }
        let url = self.api_base.join(relative)?;
        let same_origin = url.scheme() == self.api_base.scheme()
            && url.host_str() == self.api_base.host_str()
            && url.port_or_known_default() == self.api_base.port_or_known_default();
        if !same_origin || !url.path().starts_with(self.api_base.path()) {
            return Err(ApiClientError::InvalidEndpoint);
        }
        Ok(url)
    }

    fn root_url(&self, path: &str) -> Result<Url, ApiClientError> {
        let relative = path.trim_start_matches('/');
        if relative.is_empty()
            || relative.contains("://")
            || relative.contains('\\')
            || relative
                .split('/')
                .any(|component| component == "." || component == "..")
        {
            return Err(ApiClientError::InvalidEndpoint);
        }
        let origin = self.public_origin();
        let url = origin.join(relative)?;
        let same_origin = url.scheme() == origin.scheme()
            && url.host_str() == origin.host_str()
            && url.port_or_known_default() == origin.port_or_known_default();
        if !same_origin {
            return Err(ApiClientError::InvalidEndpoint);
        }
        Ok(url)
    }
}

#[derive(Clone)]
pub struct ApiClient {
    endpoint: InstanceEndpoint,
    http: reqwest::Client,
    upload_http: reqwest::Client,
    access_token: Arc<RwLock<Option<SecretString>>>,
}

impl ApiClient {
    /// Creates a client with separate authenticated API and credential-free upload transports.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying HTTP clients cannot be constructed.
    pub fn new(endpoint: InstanceEndpoint) -> Result<Self, ApiClientError> {
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(30))
            .user_agent(concat!("KaedeDesktop/", env!("CARGO_PKG_VERSION")))
            .build()?;
        let upload_http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(300))
            .user_agent(concat!("KaedeDesktop/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(Self {
            endpoint,
            http,
            upload_http,
            access_token: Arc::new(RwLock::new(None)),
        })
    }

    #[must_use]
    pub fn endpoint(&self) -> &InstanceEndpoint {
        &self.endpoint
    }

    pub async fn set_access_token(&self, token: Option<SecretString>) {
        *self.access_token.write().await = token;
    }

    pub async fn get<T: DeserializeOwned>(&self, path: &str) -> Result<T, ApiClientError> {
        self.request::<(), T>(Method::GET, path, None, None).await
    }

    pub async fn delete<T: DeserializeOwned>(&self, path: &str) -> Result<T, ApiClientError> {
        self.request::<(), T>(Method::DELETE, path, None, None)
            .await
    }

    pub async fn delete_empty(&self, path: &str) -> Result<(), ApiClientError> {
        self.request::<(), serde_json::Value>(Method::DELETE, path, None, None)
            .await
            .map(|_| ())
    }

    pub async fn post<B: Serialize + ?Sized, T: DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<T, ApiClientError> {
        self.request(Method::POST, path, Some(body), None).await
    }

    pub async fn patch<B: Serialize + ?Sized, T: DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
        version: Option<&ResourceVersion>,
    ) -> Result<T, ApiClientError> {
        self.request(Method::PATCH, path, Some(body), version).await
    }

    pub async fn put<B: Serialize + ?Sized, T: DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<T, ApiClientError> {
        self.request(Method::PUT, path, Some(body), None).await
    }

    pub async fn request_with_version<B: Serialize + ?Sized, T: DeserializeOwned>(
        &self,
        method: Method,
        path: &str,
        body: &B,
        version: Option<&ResourceVersion>,
    ) -> Result<T, ApiClientError> {
        self.request(method, path, Some(body), version).await
    }

    /// Sends a dynamically described JSON request for the shared web/native UI bridge.
    ///
    /// The bridge deliberately accepts only the methods used by Kaede's API and
    /// still passes through this client's endpoint, authentication, redirect,
    /// timeout, and error handling. It is not a generic URL fetch primitive.
    pub async fn request_json(
        &self,
        method: Method,
        path: &str,
        body: Option<&serde_json::Value>,
        version: Option<&ResourceVersion>,
    ) -> Result<serde_json::Value, ApiClientError> {
        self.request(method, path, body, version).await
    }

    /// Sends a dynamically described request while preserving status and
    /// client-relevant headers for the Tauri bridge.
    pub async fn request_json_response(
        &self,
        method: Method,
        path: &str,
        body: Option<&serde_json::Value>,
        version: Option<&ResourceVersion>,
    ) -> Result<JsonResponse, ApiClientError> {
        let response = self
            .build_request(method, path, body, version)
            .await?
            .send()
            .await?;
        decode_json_response(response).await
    }

    pub async fn delete_with_version<T: DeserializeOwned>(
        &self,
        path: &str,
        version: &ResourceVersion,
    ) -> Result<T, ApiClientError> {
        self.request::<(), T>(Method::DELETE, path, None, Some(version))
            .await
    }

    pub async fn get_bytes(&self, path: &str, max_bytes: usize) -> Result<Bytes, ApiClientError> {
        let url = self.endpoint.api_url(path)?;
        self.get_authenticated_url_bytes(url, max_bytes).await
    }

    /// Downloads an authenticated resource rooted outside `/api/v1` on the
    /// configured home instance. This is used for Kaede's `/media` routes.
    pub async fn get_root_bytes(
        &self,
        path: &str,
        max_bytes: usize,
    ) -> Result<Bytes, ApiClientError> {
        let url = self.endpoint.root_url(path)?;
        self.get_authenticated_url_bytes(url, max_bytes).await
    }

    async fn get_authenticated_url_bytes(
        &self,
        url: Url,
        max_bytes: usize,
    ) -> Result<Bytes, ApiClientError> {
        let token = self.access_token.read().await.clone();
        let mut request = self
            .http
            .get(url.clone())
            .header(CLIENT_HEADER, CLIENT_KIND);
        if let Some(token) = token.as_ref() {
            request = request.bearer_auth(token.expose_secret());
        }
        let mut response = request.send().await?;
        if response.status().is_redirection() {
            let location = response
                .headers()
                .get(header::LOCATION)
                .and_then(|value| value.to_str().ok())
                .ok_or(ApiClientError::InvalidRedirect)?;
            let target = url.join(location)?;
            if target.scheme() != "https" && !is_loopback_url(&target) {
                return Err(ApiClientError::InsecureEndpoint);
            }
            let same_origin = target.scheme() == url.scheme()
                && target.host_str() == url.host_str()
                && target.port_or_known_default() == url.port_or_known_default();
            let mut redirected = self.http.get(target).header(CLIENT_HEADER, CLIENT_KIND);
            if same_origin && let Some(token) = token.as_ref() {
                redirected = redirected.bearer_auth(token.expose_secret());
            }
            response = redirected.send().await?;
        }
        if !response.status().is_success() {
            return decode_response::<serde_json::Value>(response)
                .await
                .map(|_| Bytes::new());
        }
        let length = response.content_length().unwrap_or(0);
        if length > max_bytes as u64 {
            return Err(ApiClientError::ResponseTooLarge);
        }
        let body = response.bytes().await?;
        if body.len() > max_bytes {
            return Err(ApiClientError::ResponseTooLarge);
        }
        Ok(body)
    }

    /// Downloads a public media URL without attaching Kaede credentials.
    ///
    /// Redirects are followed once with the credential-free transport. This is
    /// suitable for public avatar, banner, guild-icon, and emoji capabilities.
    pub async fn get_public_bytes(
        &self,
        url: &Url,
        max_bytes: usize,
    ) -> Result<Bytes, ApiClientError> {
        if url.scheme() != "https" && !is_loopback_url(url) {
            return Err(ApiClientError::InsecureEndpoint);
        }
        let mut response = self.upload_http.get(url.clone()).send().await?;
        if response.status().is_redirection() {
            let location = response
                .headers()
                .get(header::LOCATION)
                .and_then(|value| value.to_str().ok())
                .ok_or(ApiClientError::InvalidRedirect)?;
            let target = url.join(location)?;
            if target.scheme() != "https" && !is_loopback_url(&target) {
                return Err(ApiClientError::InsecureEndpoint);
            }
            response = self.upload_http.get(target).send().await?;
        }
        if !response.status().is_success() {
            return decode_response::<serde_json::Value>(response)
                .await
                .map(|_| Bytes::new());
        }
        if response
            .content_length()
            .is_some_and(|length| length > max_bytes as u64)
        {
            return Err(ApiClientError::ResponseTooLarge);
        }
        let body = response.bytes().await?;
        if body.len() > max_bytes {
            return Err(ApiClientError::ResponseTooLarge);
        }
        Ok(body)
    }

    async fn request<B: Serialize + ?Sized, T: DeserializeOwned>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
        version: Option<&ResourceVersion>,
    ) -> Result<T, ApiClientError> {
        let response = self
            .build_request(method, path, body, version)
            .await?
            .send()
            .await?;
        decode_response(response).await
    }

    async fn build_request<B: Serialize + ?Sized>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
        version: Option<&ResourceVersion>,
    ) -> Result<reqwest::RequestBuilder, ApiClientError> {
        let url = self.endpoint.api_url(path)?;
        let mut request = self
            .http
            .request(method, url)
            .header(CLIENT_HEADER, CLIENT_KIND)
            .header(header::ACCEPT, "application/json");
        if let Some(token) = self.access_token.read().await.as_ref() {
            request = request.bearer_auth(token.expose_secret());
        }
        if let Some(body) = body {
            request = request.json(body);
        }
        if let Some(version) = version {
            request = request.header(header::IF_MATCH, format!("\"{version}\""));
        }
        Ok(request)
    }

    pub async fn upload_presigned(
        &self,
        url: Url,
        content_type: &str,
        content_length: u64,
        headers: &HashMap<String, String>,
        body: Bytes,
    ) -> Result<(), ApiClientError> {
        if url.scheme() != "https" && !is_loopback_url(&url) {
            return Err(ApiClientError::InsecureEndpoint);
        }
        if body.len() as u64 != content_length {
            return Err(ApiClientError::UploadLengthMismatch);
        }
        let mut request = self
            .upload_http
            .put(url)
            .header(header::CONTENT_TYPE, content_type)
            .header(header::CONTENT_LENGTH, content_length)
            .body(body);
        for (name, value) in headers {
            let name = header::HeaderName::from_bytes(name.as_bytes())?;
            if name == header::AUTHORIZATION || name == header::COOKIE {
                return Err(ApiClientError::ForbiddenUploadHeader);
            }
            request = request.header(name, value);
        }
        let response = request.send().await?;
        if response.status().is_success() {
            Ok(())
        } else {
            Err(ApiClientError::UploadRejected(response.status()))
        }
    }
}

fn is_loopback_url(url: &Url) -> bool {
    matches!(
        url.host_str(),
        Some("localhost" | "127.0.0.1" | "[::1]" | "::1")
    )
}

async fn decode_response<T: DeserializeOwned>(
    response: reqwest::Response,
) -> Result<T, ApiClientError> {
    let status = response.status();
    let trace_id = response
        .headers()
        .get("X-Kaede-Trace-Id")
        .and_then(|value| value.to_str().ok())
        .map(ToOwned::to_owned);
    let body = response.bytes().await?;
    if status.is_success() {
        if status == StatusCode::NO_CONTENT {
            return serde_json::from_slice(b"null").map_err(ApiClientError::Decode);
        }
        return serde_json::from_slice(&body).map_err(ApiClientError::Decode);
    }
    let mut api_error = decode_api_error(status, &body);
    api_error.trace_id = api_error.trace_id.or(trace_id);
    Err(ApiClientError::Server {
        status,
        error: Box::new(api_error),
    })
}

#[derive(serde::Deserialize)]
struct LegacyApiErrorEnvelope {
    detail: ApiError,
}

fn decode_api_error(status: StatusCode, body: &[u8]) -> ApiError {
    serde_json::from_slice::<ApiError>(body)
        .or_else(|_| {
            serde_json::from_slice::<LegacyApiErrorEnvelope>(body).map(|envelope| envelope.detail)
        })
        .unwrap_or_else(|_| ApiError {
            code: format!("HTTP_{}", status.as_u16()),
            message: status
                .canonical_reason()
                .unwrap_or("Request failed")
                .to_owned(),
            trace_id: None,
            permissions: None,
            retry_after_ms: None,
            max_bytes: None,
            timeout_until: None,
            timeout_indefinite: None,
            reason: None,
            errors: Vec::new(),
        })
}

async fn decode_json_response(response: reqwest::Response) -> Result<JsonResponse, ApiClientError> {
    let status = response.status();
    if !status.is_success() {
        return decode_response::<serde_json::Value>(response)
            .await
            .map(|body| JsonResponse {
                status,
                headers: HashMap::new(),
                body,
            });
    }
    let headers = [
        header::ETAG.as_str(),
        "x-kaede-trace-id",
        "x-ratelimit-bucket",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset-after",
        header::RETRY_AFTER.as_str(),
    ]
    .into_iter()
    .filter_map(|name| {
        response
            .headers()
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(|value| (name.to_owned(), value.to_owned()))
    })
    .collect();
    let body = if status == StatusCode::NO_CONTENT {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&response.bytes().await?).map_err(ApiClientError::Decode)?
    };
    Ok(JsonResponse {
        status,
        headers,
        body,
    })
}

#[derive(Debug, Error)]
pub enum ApiClientError {
    #[error("instance endpoint is invalid")]
    InvalidEndpoint,
    #[error("refusing an insecure non-loopback endpoint")]
    InsecureEndpoint,
    #[error("request failed: {0}")]
    Transport(#[from] reqwest::Error),
    #[error("response could not be decoded: {0}")]
    Decode(serde_json::Error),
    #[error("server rejected the request ({status}): {error:?}")]
    Server {
        status: StatusCode,
        error: Box<ApiError>,
    },
    #[error("upload body length did not match the upload ticket")]
    UploadLengthMismatch,
    #[error("presigned upload attempted to include credentials")]
    ForbiddenUploadHeader,
    #[error("object storage rejected the upload with {0}")]
    UploadRejected(StatusCode),
    #[error("server returned an invalid redirect")]
    InvalidRedirect,
    #[error("response exceeded the configured limit")]
    ResponseTooLarge,
    #[error("URL is invalid: {0}")]
    Url(#[from] url::ParseError),
    #[error("upload header is invalid: {0}")]
    Header(#[from] header::InvalidHeaderName),
}

impl ApiClientError {
    /// Returns wording suitable for showing directly to a person.
    ///
    /// The `Display` implementation intentionally retains transport and decode
    /// details for logs. Those details are useful to developers but are often
    /// opaque (and may contain URLs), so UI surfaces should use this method.
    #[must_use]
    pub fn user_message(&self) -> String {
        match self {
            Self::InvalidEndpoint =>
                "Kaede could not create a safe request for this action. Restart the app and try again."
                    .to_owned(),
            Self::InsecureEndpoint =>
                "Kaede blocked an insecure connection. Use an HTTPS instance address and try again."
                    .to_owned(),
            Self::Transport(error) if error.is_timeout() =>
                "The request timed out. Check your connection and try again.".to_owned(),
            Self::Transport(error) if error.is_connect() =>
                "Kaede could not connect to your instance. Check your connection and the instance address, then try again."
                    .to_owned(),
            Self::Transport(_) =>
                "Kaede lost contact with your instance. Check your connection and try again."
                    .to_owned(),
            Self::Decode(_) =>
                "Your instance returned a response this version of Kaede could not understand. Update the app and try again."
                    .to_owned(),
            Self::Server { status, error } => server_error_message(*status, error),
            Self::UploadLengthMismatch =>
                "The selected file changed while it was being uploaded. Select the file again and retry."
                    .to_owned(),
            Self::ForbiddenUploadHeader =>
                "Kaede blocked unsafe upload instructions. Select the file again to request a new upload."
                    .to_owned(),
            Self::UploadRejected(status) if *status == StatusCode::PAYLOAD_TOO_LARGE =>
                "The selected file is too large for the upload service. Choose a smaller file."
                    .to_owned(),
            Self::UploadRejected(status)
                if matches!(*status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) =>
            {
                "The upload link expired or was rejected. Select the file again to request a new upload."
                    .to_owned()
            }
            Self::UploadRejected(_) =>
                "The upload service rejected the file. Select it again or retry in a moment."
                    .to_owned(),
            Self::InvalidRedirect =>
                "The media link is invalid or expired. Retry the download or ask the sender to upload it again."
                    .to_owned(),
            Self::ResponseTooLarge =>
                "This file is too large for Kaede to download safely.".to_owned(),
            Self::Url(_) =>
                "The server returned an invalid link. Retry the action; if it keeps failing, contact your instance administrator."
                    .to_owned(),
            Self::Header(_) =>
                "The server returned invalid upload instructions. Select the file again to request a new upload."
                    .to_owned(),
        }
    }
}

fn server_error_message(status: StatusCode, error: &ApiError) -> String {
    if let Some(max_bytes) = error.max_bytes
        && (status == StatusCode::PAYLOAD_TOO_LARGE || error.code.contains("TOO_LARGE"))
    {
        return format!(
            "The selected file is too large. Choose a file no larger than {} and try again.",
            format_byte_limit(max_bytes)
        );
    }

    if status == StatusCode::TOO_MANY_REQUESTS {
        return error.retry_after_ms.map_or_else(
            || "You're doing that too quickly. Wait a moment and try again.".to_owned(),
            |milliseconds| {
                let seconds = milliseconds.div_ceil(1_000).max(1);
                let unit = if seconds == 1 { "second" } else { "seconds" };
                format!("You're doing that too quickly. Try again in {seconds} {unit}.")
            },
        );
    }

    if status == StatusCode::UNPROCESSABLE_ENTITY
        && let Some(issue) = error.errors.first()
    {
        let field = validation_field(&issue.loc);
        let problem = issue.msg.trim().trim_end_matches('.');
        if !problem.is_empty() && is_safe_server_message(problem) {
            return field.map_or_else(
                || format!("Check the information you entered: {problem}."),
                |field| format!("Check {field}: {problem}."),
            );
        }
    }

    let protected_message = sensitive_code_message(&error.code);
    let supplied = error.message.trim();
    let supplied_is_useful = protected_message.is_none()
        && !supplied.is_empty()
        && is_safe_server_message(supplied)
        && !is_generic_server_message(supplied, &error.code, status);
    let message = if let Some(message) = protected_message {
        message.to_owned()
    } else if supplied_is_useful {
        supplied.to_owned()
    } else {
        match status {
            StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY =>
                "The request could not be completed. Check the information you entered and try again."
                    .to_owned(),
            StatusCode::UNAUTHORIZED =>
                "Your session expired. Sign in again to continue.".to_owned(),
            StatusCode::FORBIDDEN =>
                "You do not have permission to do that.".to_owned(),
            StatusCode::NOT_FOUND =>
                "That item could not be found. It may have been deleted, or you may no longer have access."
                    .to_owned(),
            StatusCode::CONFLICT | StatusCode::PRECONDITION_FAILED =>
                "This item changed somewhere else. Refresh it and try again.".to_owned(),
            StatusCode::PAYLOAD_TOO_LARGE =>
                "The selected file is too large. Choose a smaller file and try again.".to_owned(),
            StatusCode::UNSUPPORTED_MEDIA_TYPE =>
                "That file type is not supported. Choose a different file and try again."
                    .to_owned(),
            StatusCode::BAD_GATEWAY
            | StatusCode::SERVICE_UNAVAILABLE
            | StatusCode::GATEWAY_TIMEOUT =>
                "Your instance is temporarily unavailable. Wait a moment and try again."
                    .to_owned(),
            status if status.is_server_error() =>
                "Your instance could not complete the request. Try again; if it keeps failing, contact your instance administrator."
                    .to_owned(),
            _ => "The request could not be completed. Try again.".to_owned(),
        }
    };

    let message = append_retry_guidance(message, error.retry_after_ms);

    if status.is_server_error()
        || (!supplied_is_useful
            && !matches!(
                status,
                StatusCode::BAD_REQUEST
                    | StatusCode::UNAUTHORIZED
                    | StatusCode::FORBIDDEN
                    | StatusCode::NOT_FOUND
                    | StatusCode::CONFLICT
                    | StatusCode::PRECONDITION_FAILED
                    | StatusCode::PAYLOAD_TOO_LARGE
                    | StatusCode::UNSUPPORTED_MEDIA_TYPE
                    | StatusCode::UNPROCESSABLE_ENTITY
            ))
    {
        append_error_reference(message, error.trace_id.as_deref())
    } else {
        message
    }
}

fn append_retry_guidance(mut message: String, retry_after_ms: Option<u64>) -> String {
    const VAGUE_RETRY_SUFFIX: &str = " Wait a moment and try again.";

    let Some(milliseconds) = retry_after_ms else {
        return message;
    };
    if message.ends_with(VAGUE_RETRY_SUFFIX) {
        message.truncate(message.len() - VAGUE_RETRY_SUFFIX.len());
    }
    let lower = message.to_ascii_lowercase();
    if lower.contains("try again in ")
        || lower.contains("retry in ")
        || lower.contains("retry after ")
        || lower.contains("try again after ")
    {
        return message;
    }

    let seconds = milliseconds.div_ceil(1_000).max(1);
    let unit = if seconds == 1 { "second" } else { "seconds" };
    if !message.ends_with(['.', '!', '?']) {
        message.push('.');
    }
    format!("{message} Try again in {seconds} {unit}.")
}

fn sensitive_code_message(code: &str) -> Option<&'static str> {
    match code {
        "INVALID_CREDENTIALS" => Some("The username, email address, or password is incorrect."),
        "INVALID_REFRESH_TOKEN" | "AUTHENTICATION_REQUIRED" => {
            Some("Your session has expired. Sign in again.")
        }
        "INVALID_TOKEN" => {
            Some("This security link or token is invalid or has expired. Request a new one.")
        }
        "PASSWORD_WORK_BUSY" => {
            Some("The server is handling too many password requests. Wait a moment and try again.")
        }
        _ => None,
    }
}

fn format_byte_limit(bytes: u64) -> String {
    const KIBIBYTE: u64 = 1_024;
    const MEBIBYTE: u64 = 1_024 * KIBIBYTE;
    const GIBIBYTE: u64 = 1_024 * MEBIBYTE;

    for (unit, size) in [("GB", GIBIBYTE), ("MB", MEBIBYTE), ("KB", KIBIBYTE)] {
        if bytes >= size {
            if bytes.is_multiple_of(size) {
                return format!("{} {unit}", bytes / size);
            }
            let tenths = bytes.saturating_mul(10).div_ceil(size);
            return format!("{}.{:01} {unit}", tenths / 10, tenths % 10);
        }
    }
    format!("{bytes} bytes")
}

fn append_error_reference(message: String, trace_id: Option<&str>) -> String {
    let Some(trace_id) = trace_id else {
        return message;
    };
    let trace_id = trace_id.trim();
    if trace_id.is_empty()
        || trace_id.len() > 64
        || !trace_id.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
        })
    {
        return message;
    }
    let reference = trace_id.chars().take(12).collect::<String>();
    format!("{message} Error reference: {reference}.")
}

fn validation_field(location: &[serde_json::Value]) -> Option<String> {
    location.iter().rev().find_map(|part| {
        let value = part.as_str()?.trim();
        if value.is_empty() || matches!(value, "body" | "path" | "query") {
            return None;
        }
        Some(value.replace('_', " "))
    })
}

fn is_generic_server_message(message: &str, code: &str, status: StatusCode) -> bool {
    let normalized = message.trim().to_ascii_lowercase();
    let normalized_code = code.trim().replace('_', " ").to_ascii_lowercase();
    normalized == normalized_code
        || matches!(
            normalized.as_str(),
            "request failed"
                | "request validation failed"
                | "bad request"
                | "unauthorized"
                | "forbidden"
                | "not found"
                | "conflict"
                | "unprocessable entity"
                | "too many requests"
                | "internal server error"
                | "bad gateway"
                | "service unavailable"
                | "gateway timeout"
        )
        || status
            .canonical_reason()
            .is_some_and(|reason| normalized.eq_ignore_ascii_case(reason))
}

fn is_safe_server_message(message: &str) -> bool {
    if message.chars().count() > 300
        || message
            .chars()
            .any(|character| character.is_control() || matches!(character, '<' | '>'))
    {
        return false;
    }
    let normalized = message.to_ascii_lowercase();
    let technical_or_sensitive = [
        "traceback",
        "stack trace",
        "exception",
        "sqlalchemy",
        "postgres",
        "sqlite",
        "database error",
        "database query",
        "select *",
        "insert into",
        "delete from",
        "authorization:",
        "bearer ",
        "token",
        "secret",
        "password",
        "private key",
        "-----begin",
        "file://",
        "/home/",
        "/var/",
        "/etc/",
        "/usr/",
        "\\users\\",
        "\\appdata\\",
        ".rs:",
        ".py:",
    ];
    !technical_or_sensitive
        .iter()
        .any(|pattern| normalized.contains(pattern))
        && !normalized.as_bytes().windows(3).any(|window| {
            window[0].is_ascii_alphabetic()
                && window[1] == b':'
                && matches!(window[2], b'\\' | b'/')
        })
}

#[cfg(test)]
mod tests {
    use std::error::Error;

    use kaede_protocol::{ApiError, Domain, ValidationIssue};
    use reqwest::StatusCode;
    use url::Url;

    use super::{ApiClientError, InstanceEndpoint, decode_api_error};

    #[test]
    fn production_endpoint_is_same_origin_https_and_wss() -> Result<(), Box<dyn Error>> {
        let endpoint = InstanceEndpoint::production(Domain::parse("Chat.Example")?)?;
        assert_eq!(endpoint.public_origin().as_str(), "https://chat.example/");
        assert_eq!(
            endpoint.gateway_url().as_str(),
            "wss://chat.example/gateway?v=1&encoding=json"
        );
        Ok(())
    }

    #[test]
    fn development_http_is_restricted_to_loopback() -> Result<(), Box<dyn Error>> {
        let domain = Domain::parse("dev.example")?;
        let remote = Url::parse("http://192.0.2.10:8080")?;
        let rejected = InstanceEndpoint::development(domain.clone(), &remote);
        assert!(matches!(rejected, Err(ApiClientError::InsecureEndpoint)));

        let local_origin = Url::parse("http://127.0.0.1:18081")?;
        let local = InstanceEndpoint::development(domain, &local_origin)?;
        assert_eq!(
            local.gateway_url().as_str(),
            "ws://127.0.0.1:18081/gateway?v=1&encoding=json"
        );
        Ok(())
    }

    #[test]
    fn api_paths_cannot_escape_the_home_instance_api() -> Result<(), Box<dyn Error>> {
        let endpoint = InstanceEndpoint::production(Domain::parse("chat.example")?)?;

        assert!(endpoint.api_url("channels/123/messages").is_ok());
        assert!(matches!(
            endpoint.api_url("https://attacker.example/steal"),
            Err(ApiClientError::InvalidEndpoint)
        ));
        assert!(matches!(
            endpoint.api_url("../.well-known/kaede/server"),
            Err(ApiClientError::InvalidEndpoint)
        ));
        assert!(matches!(
            endpoint.api_url("channels\\..\\auth/login"),
            Err(ApiClientError::InvalidEndpoint)
        ));
        Ok(())
    }

    #[test]
    fn root_paths_stay_on_the_home_instance() -> Result<(), Box<dyn Error>> {
        let endpoint = InstanceEndpoint::production(Domain::parse("chat.example")?)?;

        assert_eq!(
            endpoint
                .root_url("media/chat.example/123/original")?
                .as_str(),
            "https://chat.example/media/chat.example/123/original"
        );
        assert!(endpoint.root_url("../admin").is_err());
        assert!(endpoint.root_url("https://evil.example/file").is_err());
        Ok(())
    }

    #[test]
    fn user_messages_hide_transport_internals_and_explain_recovery() {
        assert_eq!(
            ApiClientError::ResponseTooLarge.user_message(),
            "This file is too large for Kaede to download safely."
        );
        assert_eq!(
            ApiClientError::UploadRejected(StatusCode::FORBIDDEN).user_message(),
            "The upload link expired or was rejected. Select the file again to request a new upload."
        );
        assert!(
            !ApiClientError::InvalidEndpoint
                .user_message()
                .contains("endpoint")
        );
    }

    #[test]
    fn current_and_legacy_error_envelopes_decode_to_the_same_api_error() {
        let current = decode_api_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            br#"{
                "code":"VALIDATION_ERROR",
                "message":"Check the information you entered.",
                "trace_id":"current.trace-1",
                "retry_after_ms":1500,
                "errors":[{
                    "location":["body","display_name"],
                    "message":"Field is required",
                    "type":"missing"
                }]
            }"#,
        );
        let legacy = decode_api_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            br#"{
                "detail":{
                    "code":"VALIDATION_ERROR",
                    "message":"Check the information you entered.",
                    "trace_id":"legacy.trace-1",
                    "retry_after_ms":1500,
                    "errors":[{
                        "loc":["body","display_name"],
                        "msg":"Field is required",
                        "type":"missing"
                    }]
                }
            }"#,
        );

        assert_eq!(current.code, legacy.code);
        assert_eq!(current.message, legacy.message);
        assert_eq!(current.retry_after_ms, legacy.retry_after_ms);
        assert_eq!(current.errors[0].loc, legacy.errors[0].loc);
        assert_eq!(current.errors[0].msg, legacy.errors[0].msg);
        assert_eq!(current.trace_id.as_deref(), Some("current.trace-1"));
        assert_eq!(legacy.trace_id.as_deref(), Some("legacy.trace-1"));
    }

    #[test]
    fn malformed_error_body_uses_the_status_fallback() {
        let error = decode_api_error(StatusCode::BAD_GATEWAY, b"upstream exploded");

        assert_eq!(error.code, "HTTP_502");
        assert_eq!(error.message, "Bad Gateway");
        assert!(error.trace_id.is_none());
    }

    #[test]
    fn generic_server_failures_become_actionable_messages() {
        let error = ApiClientError::Server {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            error: Box::new(ApiError {
                code: "INTERNAL_SERVER_ERROR".to_owned(),
                message: "Internal Server Error".to_owned(),
                trace_id: Some("trace.for-support".to_owned()),
                permissions: None,
                retry_after_ms: None,
                max_bytes: None,
                timeout_until: None,
                timeout_indefinite: None,
                reason: None,
                errors: Vec::new(),
            }),
        };
        let message = error.user_message();
        assert!(message.contains("Try again"));
        assert!(message.contains("instance administrator"));
        assert!(message.contains("Error reference: trace.for-su."));
        assert!(!message.contains("Internal Server Error"));
        assert!(!message.contains("trace.for-support"));
    }

    #[test]
    fn validation_and_rate_limit_messages_include_the_next_step() {
        let validation = ApiClientError::Server {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            error: Box::new(ApiError {
                code: "REQUEST_VALIDATION_FAILED".to_owned(),
                message: "Request validation failed".to_owned(),
                trace_id: None,
                permissions: None,
                retry_after_ms: None,
                max_bytes: None,
                timeout_until: None,
                timeout_indefinite: None,
                reason: None,
                errors: vec![ValidationIssue {
                    loc: vec![serde_json::json!("body"), serde_json::json!("display_name")],
                    msg: "Field is required".to_owned(),
                    kind: "missing".to_owned(),
                }],
            }),
        };
        assert_eq!(
            validation.user_message(),
            "Check display name: Field is required."
        );

        let rate_limit = ApiClientError::Server {
            status: StatusCode::TOO_MANY_REQUESTS,
            error: Box::new(ApiError {
                code: "RATE_LIMITED".to_owned(),
                message: "Too Many Requests".to_owned(),
                trace_id: None,
                permissions: None,
                retry_after_ms: Some(1_001),
                max_bytes: None,
                timeout_until: None,
                timeout_indefinite: None,
                reason: None,
                errors: Vec::new(),
            }),
        };
        assert_eq!(
            rate_limit.user_message(),
            "You're doing that too quickly. Try again in 2 seconds."
        );

        let unavailable_voice_home = ApiClientError::Server {
            status: StatusCode::SERVICE_UNAVAILABLE,
            error: Box::new(ApiError {
                code: "VOICE_HOME_UNREACHABLE".to_owned(),
                message: "The voice service at the channel's home instance is unavailable."
                    .to_owned(),
                trace_id: None,
                permissions: None,
                retry_after_ms: Some(2_500),
                max_bytes: None,
                timeout_until: None,
                timeout_indefinite: None,
                reason: None,
                errors: Vec::new(),
            }),
        };
        assert_eq!(
            unavailable_voice_home.user_message(),
            "The voice service at the channel's home instance is unavailable. Try again in 3 seconds."
        );

        let oversized = ApiClientError::Server {
            status: StatusCode::PAYLOAD_TOO_LARGE,
            error: Box::new(ApiError {
                code: "ATTACHMENT_TOO_LARGE".to_owned(),
                message: "Payload Too Large".to_owned(),
                trace_id: None,
                permissions: None,
                retry_after_ms: None,
                max_bytes: Some(8 * 1_024 * 1_024),
                timeout_until: None,
                timeout_indefinite: None,
                reason: None,
                errors: Vec::new(),
            }),
        };
        assert_eq!(
            oversized.user_message(),
            "The selected file is too large. Choose a file no larger than 8 MB and try again."
        );
    }

    #[test]
    fn meaningful_server_messages_are_preserved() {
        let error = ApiClientError::Server {
            status: StatusCode::CONFLICT,
            error: Box::new(ApiError {
                code: "USERNAME_TAKEN".to_owned(),
                message: "That username is already in use.".to_owned(),
                trace_id: None,
                permissions: None,
                retry_after_ms: None,
                max_bytes: None,
                timeout_until: None,
                timeout_indefinite: None,
                reason: None,
                errors: Vec::new(),
            }),
        };
        assert_eq!(error.user_message(), "That username is already in use.");
    }

    #[test]
    fn technical_or_sensitive_server_text_is_never_shown() {
        for unsafe_message in [
            "Traceback (most recent call last):\nFile /app/service.py:42",
            "SQLAlchemy exception: SELECT * FROM users",
            "Authorization: Bearer do-not-display-this-token",
            "Could not read C:\\Users\\person\\private-key.pem",
            "<html><body>proxy failure</body></html>",
        ] {
            let error = ApiClientError::Server {
                status: StatusCode::BAD_REQUEST,
                error: Box::new(ApiError {
                    code: "REQUEST_FAILED".to_owned(),
                    message: unsafe_message.to_owned(),
                    trace_id: None,
                    permissions: None,
                    retry_after_ms: None,
                    max_bytes: None,
                    timeout_until: None,
                    timeout_indefinite: None,
                    reason: None,
                    errors: Vec::new(),
                }),
            };
            assert_eq!(
                error.user_message(),
                "The request could not be completed. Check the information you entered and try again."
            );
        }

        let overlong = ApiClientError::Server {
            status: StatusCode::BAD_REQUEST,
            error: Box::new(ApiError {
                code: "REQUEST_FAILED".to_owned(),
                message: "x".repeat(301),
                trace_id: None,
                permissions: None,
                retry_after_ms: None,
                max_bytes: None,
                timeout_until: None,
                timeout_indefinite: None,
                reason: None,
                errors: Vec::new(),
            }),
        };
        assert_eq!(
            overlong.user_message(),
            "The request could not be completed. Check the information you entered and try again."
        );

        let unsafe_validation = ApiClientError::Server {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            error: Box::new(ApiError {
                code: "VALIDATION_ERROR".to_owned(),
                message: "Request validation failed".to_owned(),
                trace_id: None,
                permissions: None,
                retry_after_ms: None,
                max_bytes: None,
                timeout_until: None,
                timeout_indefinite: None,
                reason: None,
                errors: vec![ValidationIssue {
                    loc: vec![serde_json::json!("body"), serde_json::json!("profile")],
                    msg: "Traceback at /home/kaede/service.py token=do-not-display".to_owned(),
                    kind: "value_error".to_owned(),
                }],
            }),
        };
        assert_eq!(
            unsafe_validation.user_message(),
            "The request could not be completed. Check the information you entered and try again."
        );
    }
}
