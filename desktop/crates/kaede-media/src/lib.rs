//! Safe upload, media-capability, link-preview, and GIF operations.
//!
//! All public operations return [`MediaError`] and share the same ticket,
//! credential-isolation, and response-validation boundary.

#![allow(clippy::missing_errors_doc)]

use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

use bytes::Bytes;
use kaede_api::{ApiClient, ApiClientError};
use kaede_core::{Attachment, CustomEmoji};
use kaede_protocol::{EntityRef, Snowflake};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use url::{Url, form_urlencoded};

/// Maximum age of a public asset before its origin must be checked again.
pub const PUBLIC_ASSET_CACHE_TTL: Duration = Duration::from_secs(5 * 60);

#[derive(Clone, Debug, Serialize)]
struct TicketRequest<'a> {
    filename: &'a str,
    content_type: &'a str,
    size: u64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct UploadTicket {
    pub id: Snowflake,
    pub origin_domain: kaede_protocol::Domain,
    pub upload_url: Url,
    pub upload_method: String,
    pub expires_at: String,
    #[serde(default)]
    pub upload_headers: HashMap<String, String>,
}

#[derive(Clone, Debug)]
pub struct PendingUpload {
    pub ticket: UploadTicket,
    pub filename: String,
    pub content_type: String,
    pub size: u64,
    pub sha256: String,
}

#[derive(Clone)]
pub struct MediaClient {
    api: ApiClient,
}

impl MediaClient {
    #[must_use]
    pub const fn new(api: ApiClient) -> Self {
        Self { api }
    }

    pub async fn upload_attachment(
        &self,
        channel: &EntityRef,
        path: &Path,
        content_type: &str,
    ) -> Result<PendingUpload, MediaError> {
        let metadata = tokio::fs::metadata(path).await?;
        if !metadata.is_file() || metadata.len() == 0 {
            return Err(MediaError::EmptyFile);
        }
        let filename = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or(MediaError::InvalidFilename)?
            .to_owned();
        let bytes = Bytes::from(tokio::fs::read(path).await?);
        let ticket: UploadTicket = self
            .api
            .post(
                &format!("channels/{channel}/attachments"),
                &TicketRequest {
                    filename: &filename,
                    content_type,
                    size: metadata.len(),
                },
            )
            .await?;
        if ticket.upload_method != "PUT" {
            return Err(MediaError::UnsupportedUploadMethod);
        }
        let sha256 = format!("{:x}", Sha256::digest(&bytes));
        self.api
            .upload_presigned(
                ticket.upload_url.clone(),
                content_type,
                metadata.len(),
                &ticket.upload_headers,
                bytes,
            )
            .await?;
        Ok(PendingUpload {
            ticket,
            filename,
            content_type: content_type.to_owned(),
            size: metadata.len(),
            sha256,
        })
    }

    pub async fn upload_profile_asset(
        &self,
        kind: ProfileAssetKind,
        path: &Path,
        content_type: &str,
    ) -> Result<Attachment, MediaError> {
        self.upload_asset(
            &format!("users/@me/assets/{}", kind.as_str()),
            path,
            content_type,
        )
        .await
    }

    pub async fn upload_guild_asset(
        &self,
        guild: &EntityRef,
        kind: GuildAssetKind,
        path: &Path,
        content_type: &str,
    ) -> Result<Attachment, MediaError> {
        self.upload_asset(
            &format!("guilds/{guild}/assets/{}", kind.as_str()),
            path,
            content_type,
        )
        .await
    }

    async fn upload_asset(
        &self,
        endpoint: &str,
        path: &Path,
        content_type: &str,
    ) -> Result<Attachment, MediaError> {
        let metadata = tokio::fs::metadata(path).await?;
        if !metadata.is_file() || metadata.len() == 0 {
            return Err(MediaError::EmptyFile);
        }
        let filename = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or(MediaError::InvalidFilename)?;
        let ticket: UploadTicket = self
            .api
            .post(
                endpoint,
                &TicketRequest {
                    filename,
                    content_type,
                    size: metadata.len(),
                },
            )
            .await?;
        let bytes = Bytes::from(tokio::fs::read(path).await?);
        self.api
            .upload_presigned(
                ticket.upload_url,
                content_type,
                metadata.len(),
                &ticket.upload_headers,
                bytes,
            )
            .await?;
        let commit = serde_json::json!({"attachment_id": ticket.id});
        for _ in 0..30 {
            let attachment: Attachment = self.api.put(endpoint, &commit).await?;
            match attachment.scan_status.as_deref() {
                Some("clean") => return Ok(attachment),
                Some("rejected" | "infected" | "error") => {
                    return Err(MediaError::ProcessingRejected);
                }
                _ => tokio::time::sleep(std::time::Duration::from_secs(1)).await,
            }
        }
        Err(MediaError::ProcessingTimeout)
    }

    pub async fn upload_guild_emoji(
        &self,
        guild: &EntityRef,
        name: &str,
        path: &Path,
        content_type: &str,
    ) -> Result<CustomEmoji, MediaError> {
        let metadata = tokio::fs::metadata(path).await?;
        if !metadata.is_file() || metadata.len() == 0 {
            return Err(MediaError::EmptyFile);
        }
        let filename = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or(MediaError::InvalidFilename)?;
        let ticket: UploadTicket = self
            .api
            .post(
                &format!("guilds/{guild}/emojis/tickets"),
                &TicketRequest {
                    filename,
                    content_type,
                    size: metadata.len(),
                },
            )
            .await?;
        let bytes = Bytes::from(tokio::fs::read(path).await?);
        self.api
            .upload_presigned(
                ticket.upload_url,
                content_type,
                metadata.len(),
                &ticket.upload_headers,
                bytes,
            )
            .await?;

        let commit = serde_json::json!({"attachment_id": ticket.id, "name": name});
        for _ in 0..30 {
            let value: serde_json::Value = self
                .api
                .post(&format!("guilds/{guild}/emojis"), &commit)
                .await?;
            if value.get("name").is_some() {
                return serde_json::from_value(value).map_err(MediaError::InvalidResponse);
            }
            let status = value
                .get("scan_status")
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default();
            if matches!(status, "rejected" | "infected" | "error") {
                return Err(MediaError::ProcessingRejected);
            }
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        }
        Err(MediaError::ProcessingTimeout)
    }

    pub async fn delete_guild_emoji(
        &self,
        guild: &EntityRef,
        emoji: Snowflake,
    ) -> Result<(), MediaError> {
        let _: serde_json::Value = self
            .api
            .delete(&format!("guilds/{guild}/emojis/{emoji}"))
            .await?;
        Ok(())
    }

    pub async fn attachment_status(&self, id: Snowflake) -> Result<Attachment, MediaError> {
        self.api
            .get(&format!("attachments/{id}"))
            .await
            .map_err(Into::into)
    }

    pub async fn cache_public_asset(
        &self,
        origin: &kaede_protocol::Domain,
        content_hash: &str,
        variant: &str,
        directory: &Path,
    ) -> Result<PathBuf, MediaError> {
        if content_hash.len() != 64
            || !content_hash.bytes().all(|byte| byte.is_ascii_hexdigit())
            || !matches!(
                variant,
                "thumbnail_128" | "thumbnail_512" | "thumbnail_1024"
            )
        {
            return Err(MediaError::InvalidAssetReference);
        }
        tokio::fs::create_dir_all(directory).await?;
        let path = public_asset_cache_path(directory, origin, content_hash, variant);
        if public_asset_cache_is_fresh(&path).await? {
            return Ok(path);
        }
        let mut url = if origin == self.api.endpoint().domain() {
            self.api.endpoint().public_origin()
        } else {
            Url::parse(&format!("https://{origin}/"))?
        };
        url.set_path(&format!("/media/assets/{content_hash}/{variant}"));
        url.set_query(Some("v=2"));
        let bytes = self.api.get_public_bytes(&url, 8 * 1024 * 1024).await?;
        let temporary = path.with_extension("tmp");
        tokio::fs::write(&temporary, bytes).await?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            tokio::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600)).await?;
        }
        tokio::fs::rename(&temporary, &path).await?;
        Ok(path)
    }

    pub async fn link_preview(&self, url: &Url) -> Result<LinkPreview, MediaError> {
        self.api
            .post("link-previews", &serde_json::json!({"url": url}))
            .await
            .map_err(Into::into)
    }

    pub async fn gifs(&self, query: Option<&str>, page: u16) -> Result<GifPage, MediaError> {
        let path = {
            let mut query_string = form_urlencoded::Serializer::new(String::new());
            if let Some(query) = query.filter(|value| !value.trim().is_empty()) {
                query_string.append_pair("query", query.trim());
            }
            query_string.append_pair("page", &page.max(1).to_string());
            query_string.append_pair("limit", "24");
            format!("gifs?{}", query_string.finish())
        };
        self.api.get(&path).await.map_err(Into::into)
    }
}

fn public_asset_cache_path(
    directory: &Path,
    origin: &kaede_protocol::Domain,
    content_hash: &str,
    variant: &str,
) -> PathBuf {
    let origin_scope = format!("{:x}", Sha256::digest(origin.as_str().as_bytes()));
    directory.join(format!("{origin_scope}-{content_hash}-{variant}.asset"))
}

/// Reports whether a public asset path is a regular file inside the short
/// revalidation window. Future mtimes are deliberately treated as stale.
pub async fn public_asset_cache_is_fresh(path: &Path) -> Result<bool, std::io::Error> {
    public_asset_cache_is_fresh_at(path, SystemTime::now()).await
}

async fn public_asset_cache_is_fresh_at(
    path: &Path,
    now: SystemTime,
) -> Result<bool, std::io::Error> {
    let metadata = match tokio::fs::metadata(path).await {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error),
    };
    if !metadata.is_file() {
        return Ok(false);
    }
    let modified = metadata.modified()?;
    Ok(now
        .duration_since(modified)
        .is_ok_and(|age| age <= PUBLIC_ASSET_CACHE_TTL))
}

#[derive(Clone, Copy, Debug)]
pub enum ProfileAssetKind {
    Avatar,
    Banner,
}

#[derive(Clone, Copy, Debug)]
pub enum GuildAssetKind {
    Icon,
    Banner,
}

impl GuildAssetKind {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Icon => "icon",
            Self::Banner => "banner",
        }
    }
}

impl ProfileAssetKind {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Avatar => "avatar",
            Self::Banner => "banner",
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
pub struct LinkPreview {
    pub url: Url,
    pub title: Option<String>,
    pub description: Option<String>,
    pub site_name: Option<String>,
    pub media_url: Option<String>,
    pub media_type: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct GifPage {
    pub items: Vec<GifItem>,
    pub page: u16,
    pub next_page: Option<u16>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct GifItem {
    pub id: String,
    pub title: String,
    pub url: Url,
    pub preview_url: Url,
    pub width: u32,
    pub height: u32,
}

#[derive(Debug, Error)]
pub enum MediaError {
    #[error(transparent)]
    Api(#[from] ApiClientError),
    #[error("file could not be read: {0}")]
    Io(#[from] std::io::Error),
    #[error("public media URL is invalid: {0}")]
    Url(#[from] url::ParseError),
    #[error("file is empty")]
    EmptyFile,
    #[error("filename is not valid UTF-8")]
    InvalidFilename,
    #[error("media service returned an invalid response: {0}")]
    InvalidResponse(serde_json::Error),
    #[error("the uploaded media was rejected during processing")]
    ProcessingRejected,
    #[error("media processing did not finish in time")]
    ProcessingTimeout,
    #[error("server requested an unsupported upload method")]
    UnsupportedUploadMethod,
    #[error("the public media reference was invalid")]
    InvalidAssetReference,
}

#[cfg(test)]
mod tests {
    use std::{
        error::Error,
        sync::{
            Arc,
            atomic::{AtomicU64, AtomicUsize, Ordering},
        },
    };

    use kaede_api::InstanceEndpoint;
    use kaede_protocol::Domain;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
        task::JoinHandle,
    };

    use super::*;

    const CONTENT_HASH: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    static NEXT_TEST_DIRECTORY: AtomicU64 = AtomicU64::new(0);

    fn client() -> Result<MediaClient, Box<dyn Error>> {
        let endpoint = InstanceEndpoint::production(Domain::parse("home.example")?)?;
        Ok(MediaClient::new(ApiClient::new(endpoint)?))
    }

    fn development_client(origin: &Url) -> Result<MediaClient, Box<dyn Error>> {
        let endpoint = InstanceEndpoint::development(Domain::parse("home.example")?, origin)?;
        Ok(MediaClient::new(ApiClient::new(endpoint)?))
    }

    fn test_directory(label: &str) -> PathBuf {
        let sequence = NEXT_TEST_DIRECTORY.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "kaede-media-{label}-{}-{sequence}",
            std::process::id()
        ))
    }

    async fn public_asset_server(
        body: &'static [u8],
    ) -> Result<(Url, Arc<AtomicUsize>, JoinHandle<std::io::Result<String>>), Box<dyn Error>> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
        let origin = Url::parse(&format!("http://{}/", listener.local_addr()?))?;
        let request_count = Arc::new(AtomicUsize::new(0));
        let request_count_for_server = Arc::clone(&request_count);
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await?;
            request_count_for_server.fetch_add(1, Ordering::SeqCst);
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1_024];
            while !request.windows(4).any(|window| window == b"\r\n\r\n") {
                let read = stream.read(&mut buffer).await?;
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..read]);
            }
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            stream.write_all(headers.as_bytes()).await?;
            stream.write_all(body).await?;
            Ok(String::from_utf8_lossy(&request).into_owned())
        });
        Ok((origin, request_count, server))
    }

    #[tokio::test]
    async fn public_asset_references_are_rejected_before_network_access()
    -> Result<(), Box<dyn Error>> {
        let media = client()?;
        let directory =
            std::env::temp_dir().join(format!("kaede-media-invalid-{}", std::process::id()));
        let result = media
            .cache_public_asset(
                &Domain::parse("remote.example")?,
                "../../credential",
                "original",
                &directory,
            )
            .await;
        assert!(matches!(result, Err(MediaError::InvalidAssetReference)));
        assert!(!tokio::fs::try_exists(directory).await?);
        Ok(())
    }

    #[tokio::test]
    async fn fresh_public_asset_is_returned_without_network_recheck() -> Result<(), Box<dyn Error>>
    {
        let directory = test_directory("fresh");
        tokio::fs::create_dir_all(&directory).await?;
        let domain = Domain::parse("home.example")?;
        let path = public_asset_cache_path(&directory, &domain, CONTENT_HASH, "thumbnail_128");
        tokio::fs::write(&path, b"cached").await?;
        let (origin, request_count, server) = public_asset_server(b"revalidated").await?;
        let media = development_client(&origin)?;

        let cached = media
            .cache_public_asset(&domain, CONTENT_HASH, "thumbnail_128", &directory)
            .await?;

        assert_eq!(cached, path);
        assert_eq!(tokio::fs::read(&cached).await?, b"cached");
        assert_eq!(request_count.load(Ordering::SeqCst), 0);
        server.abort();
        tokio::fs::remove_dir_all(directory).await?;
        Ok(())
    }

    #[tokio::test]
    async fn same_hash_from_another_origin_cannot_bypass_revalidation() -> Result<(), Box<dyn Error>>
    {
        let directory = test_directory("origin-scope");
        tokio::fs::create_dir_all(&directory).await?;
        let domain = Domain::parse("home.example")?;
        let foreign_domain = Domain::parse("remote.example")?;
        let foreign_path =
            public_asset_cache_path(&directory, &foreign_domain, CONTENT_HASH, "thumbnail_128");
        tokio::fs::write(&foreign_path, b"foreign").await?;
        let (origin, request_count, server) = public_asset_server(b"home").await?;
        let media = development_client(&origin)?;

        let cached = media
            .cache_public_asset(&domain, CONTENT_HASH, "thumbnail_128", &directory)
            .await?;
        let request = server.await??;

        assert_ne!(cached, foreign_path);
        assert_eq!(tokio::fs::read(&cached).await?, b"home");
        assert_eq!(tokio::fs::read(&foreign_path).await?, b"foreign");
        assert_eq!(request_count.load(Ordering::SeqCst), 1);
        assert!(request.starts_with(&format!(
            "GET /media/assets/{CONTENT_HASH}/thumbnail_128?v=2 HTTP/1.1\r\n"
        )));
        tokio::fs::remove_dir_all(directory).await?;
        Ok(())
    }

    #[tokio::test]
    async fn expired_public_asset_is_revalidated_before_it_is_returned()
    -> Result<(), Box<dyn Error>> {
        let directory = test_directory("expired");
        tokio::fs::create_dir_all(&directory).await?;
        let domain = Domain::parse("home.example")?;
        let path = public_asset_cache_path(&directory, &domain, CONTENT_HASH, "thumbnail_128");
        tokio::fs::write(&path, b"expired").await?;
        let expired_at = SystemTime::now() - PUBLIC_ASSET_CACHE_TTL - Duration::from_secs(1);
        std::fs::File::open(&path)?.set_modified(expired_at)?;
        let (origin, request_count, server) = public_asset_server(b"revalidated").await?;
        let media = development_client(&origin)?;

        let cached = media
            .cache_public_asset(&domain, CONTENT_HASH, "thumbnail_128", &directory)
            .await?;
        let request = server.await??;

        assert_eq!(cached, path);
        assert_eq!(tokio::fs::read(&cached).await?, b"revalidated");
        assert_eq!(request_count.load(Ordering::SeqCst), 1);
        assert!(request.starts_with(&format!(
            "GET /media/assets/{CONTENT_HASH}/thumbnail_128?v=2 HTTP/1.1\r\n"
        )));
        tokio::fs::remove_dir_all(directory).await?;
        Ok(())
    }

    #[test]
    fn asset_kinds_map_only_to_server_owned_paths() {
        assert_eq!(ProfileAssetKind::Avatar.as_str(), "avatar");
        assert_eq!(ProfileAssetKind::Banner.as_str(), "banner");
        assert_eq!(GuildAssetKind::Icon.as_str(), "icon");
        assert_eq!(GuildAssetKind::Banner.as_str(), "banner");
    }
}
