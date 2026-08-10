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
    let mut api_error = serde_json::from_slice::<ApiError>(&body).unwrap_or_else(|_| ApiError {
        code: format!("HTTP_{}", status.as_u16()),
        message: status
            .canonical_reason()
            .unwrap_or("Request failed")
            .to_owned(),
        trace_id: None,
        permissions: None,
        retry_after_ms: None,
        errors: Vec::new(),
    });
    api_error.trace_id = api_error.trace_id.or(trace_id);
    Err(ApiClientError::Server {
        status,
        error: Box::new(api_error),
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

#[cfg(test)]
mod tests {
    use std::error::Error;

    use kaede_protocol::Domain;
    use url::Url;

    use super::{ApiClientError, InstanceEndpoint};

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
}
