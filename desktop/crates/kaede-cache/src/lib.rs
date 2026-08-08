use std::{path::Path, sync::Arc};

use kaede_core::Message;
use kaede_protocol::EntityRef;
use parking_lot::Mutex;
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Serialize, de::DeserializeOwned};
use thiserror::Error;
use tokio::task;

const SCHEMA_VERSION: i32 = 1;
const MAX_MESSAGES_PER_CHANNEL: i64 = 5_000;

#[derive(Clone)]
pub struct Cache {
    connection: Arc<Mutex<Connection>>,
}

impl Cache {
    /// Opens or initializes the per-installation `SQLite` cache.
    ///
    /// # Errors
    ///
    /// Returns [`CacheError`] when `SQLite` cannot open, configure, or migrate it.
    pub fn open(path: &Path) -> Result<Self, CacheError> {
        let connection = Connection::open(path)?;
        connection.pragma_update(None, "journal_mode", "WAL")?;
        connection.pragma_update(None, "foreign_keys", true)?;
        connection.pragma_update(None, "secure_delete", true)?;
        connection.execute_batch(
            "CREATE TABLE IF NOT EXISTS cache_meta (
                 key TEXT PRIMARY KEY,
                 value TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS entities (
                 account TEXT NOT NULL,
                 kind TEXT NOT NULL,
                 id TEXT NOT NULL,
                 domain TEXT NOT NULL,
                 payload BLOB NOT NULL,
                 updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
                 PRIMARY KEY (account, kind, id, domain)
             );
             CREATE TABLE IF NOT EXISTS channel_messages (
                 account TEXT NOT NULL,
                 channel_id TEXT NOT NULL,
                 channel_domain TEXT NOT NULL,
                 message_id TEXT NOT NULL,
                 message_domain TEXT NOT NULL,
                 created_at TEXT NOT NULL,
                 payload BLOB NOT NULL,
                 PRIMARY KEY (account, message_id, message_domain)
             );
             CREATE INDEX IF NOT EXISTS ix_channel_messages_timeline
             ON channel_messages(account, channel_id, channel_domain, created_at, message_id);
             CREATE TABLE IF NOT EXISTS media_cache (
                 account TEXT NOT NULL,
                 channel_id TEXT NOT NULL,
                 channel_domain TEXT NOT NULL,
                 cache_key TEXT NOT NULL,
                 path TEXT NOT NULL,
                 size INTEGER NOT NULL,
                 last_accessed INTEGER NOT NULL DEFAULT (unixepoch()),
                 PRIMARY KEY (account, cache_key)
             );",
        )?;
        connection.execute(
            "INSERT INTO cache_meta(key, value) VALUES ('schema_version', ?1)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [SCHEMA_VERSION.to_string()],
        )?;
        Ok(Self {
            connection: Arc::new(Mutex::new(connection)),
        })
    }

    /// Stores one account-scoped, origin-scoped entity snapshot.
    ///
    /// # Errors
    ///
    /// Returns [`CacheError`] for serialization, worker, or database failures.
    pub async fn put_entity<T: Serialize + Send + 'static>(
        &self,
        account: String,
        kind: String,
        key: EntityRef,
        value: T,
    ) -> Result<(), CacheError> {
        let payload = serde_json::to_vec(&value)?;
        let connection = self.connection.clone();
        task::spawn_blocking(move || {
            connection.lock().execute(
                "INSERT INTO entities(account, kind, id, domain, payload)
                 VALUES (?1, ?2, ?3, ?4, ?5)
                 ON CONFLICT(account, kind, id, domain) DO UPDATE
                 SET payload = excluded.payload, updated_at = unixepoch()",
                params![
                    account,
                    kind,
                    key.id.to_string(),
                    key.domain.as_str(),
                    payload
                ],
            )?;
            Ok::<(), rusqlite::Error>(())
        })
        .await??;
        Ok(())
    }

    /// Loads one account-scoped, origin-scoped entity snapshot.
    ///
    /// # Errors
    ///
    /// Returns [`CacheError`] for decoding, worker, or database failures.
    pub async fn get_entity<T: DeserializeOwned + Send + 'static>(
        &self,
        account: String,
        kind: String,
        key: EntityRef,
    ) -> Result<Option<T>, CacheError> {
        let connection = self.connection.clone();
        let payload = task::spawn_blocking(move || {
            connection
                .lock()
                .query_row(
                    "SELECT payload FROM entities
                     WHERE account = ?1 AND kind = ?2 AND id = ?3 AND domain = ?4",
                    params![account, kind, key.id.to_string(), key.domain.as_str()],
                    |row| row.get::<_, Vec<u8>>(0),
                )
                .optional()
                .map_err(CacheError::Database)
        })
        .await??;
        payload
            .map(|value| serde_json::from_slice(&value).map_err(CacheError::Json))
            .transpose()
    }

    /// Atomically merges a bounded message page into a channel timeline.
    ///
    /// # Errors
    ///
    /// Returns [`CacheError`] for serialization, worker, or database failures.
    pub async fn put_messages(
        &self,
        account: String,
        channel: EntityRef,
        messages: Vec<Message>,
    ) -> Result<(), CacheError> {
        let rows = messages
            .into_iter()
            .map(|message| {
                let key = message.key();
                let created_at = message.created_at.to_rfc3339();
                let payload = serde_json::to_vec(&message)?;
                Ok((key, created_at, payload))
            })
            .collect::<Result<Vec<_>, serde_json::Error>>()?;
        let connection = self.connection.clone();
        task::spawn_blocking(move || {
            let mut connection = connection.lock();
            let transaction = connection.transaction()?;
            for (key, created_at, payload) in rows {
                transaction.execute(
                    "INSERT INTO channel_messages(
                         account, channel_id, channel_domain, message_id,
                         message_domain, created_at, payload
                     ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
                     ON CONFLICT(account, message_id, message_domain) DO UPDATE
                     SET channel_id = excluded.channel_id,
                         channel_domain = excluded.channel_domain,
                         created_at = excluded.created_at,
                         payload = excluded.payload",
                    params![
                        account,
                        channel.id.to_string(),
                        channel.domain.as_str(),
                        key.id.to_string(),
                        key.domain.as_str(),
                        created_at,
                        payload,
                    ],
                )?;
            }
            transaction.execute(
                "DELETE FROM channel_messages WHERE rowid IN (
                     SELECT rowid FROM channel_messages
                     WHERE account = ?1 AND channel_id = ?2 AND channel_domain = ?3
                     ORDER BY created_at DESC, message_id DESC
                     LIMIT -1 OFFSET ?4
                 )",
                params![
                    account,
                    channel.id.to_string(),
                    channel.domain.as_str(),
                    MAX_MESSAGES_PER_CHANNEL,
                ],
            )?;
            transaction.commit()?;
            Ok::<(), rusqlite::Error>(())
        })
        .await??;
        Ok(())
    }

    /// Returns the newest cached messages for an account and composite channel.
    ///
    /// # Errors
    ///
    /// Returns [`CacheError`] for decoding, worker, or database failures.
    pub async fn channel_messages(
        &self,
        account: String,
        channel: EntityRef,
        limit: u16,
    ) -> Result<Vec<Message>, CacheError> {
        let connection = self.connection.clone();
        let rows = task::spawn_blocking(move || {
            let connection = connection.lock();
            let mut statement = connection.prepare(
                "SELECT payload FROM channel_messages
                 WHERE account = ?1 AND channel_id = ?2 AND channel_domain = ?3
                 ORDER BY created_at DESC, message_id DESC LIMIT ?4",
            )?;
            statement
                .query_map(
                    params![
                        account,
                        channel.id.to_string(),
                        channel.domain.as_str(),
                        i64::from(limit.clamp(1, 200)),
                    ],
                    |row| row.get::<_, Vec<u8>>(0),
                )?
                .collect::<Result<Vec<_>, _>>()
                .map_err(CacheError::Database)
        })
        .await??;
        rows.into_iter()
            .map(|payload| serde_json::from_slice(&payload).map_err(CacheError::Json))
            .collect()
    }

    /// Associates a decoded media file with its authorized account and channel.
    ///
    /// # Errors
    ///
    /// Returns [`CacheError`] for worker or database failures.
    pub async fn put_media(
        &self,
        account: String,
        channel: EntityRef,
        cache_key: String,
        path: String,
        size: u64,
    ) -> Result<(), CacheError> {
        let connection = self.connection.clone();
        task::spawn_blocking(move || {
            connection.lock().execute(
                "INSERT INTO media_cache(account, channel_id, channel_domain, cache_key, path, size)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                 ON CONFLICT(account, cache_key) DO UPDATE SET
                     channel_id = excluded.channel_id,
                     channel_domain = excluded.channel_domain,
                     path = excluded.path,
                     size = excluded.size,
                     last_accessed = unixepoch()",
                params![
                    account,
                    channel.id.to_string(),
                    channel.domain.as_str(),
                    cache_key,
                    path,
                    i64::try_from(size).unwrap_or(i64::MAX),
                ],
            )?;
            Ok::<(), rusqlite::Error>(())
        })
        .await??;
        Ok(())
    }

    /// Enforces an account-scoped byte budget using least-recently-used media
    /// entries and returns the paths that the caller must remove from disk.
    /// Authoritative attachment records are not affected and can be fetched
    /// again after the user reopens the message.
    ///
    /// # Errors
    ///
    /// Returns [`CacheError`] when the eviction transaction cannot complete.
    pub async fn prune_media(
        &self,
        account: String,
        max_bytes: u64,
    ) -> Result<Vec<String>, CacheError> {
        let max_bytes = i64::try_from(max_bytes).unwrap_or(i64::MAX);
        let connection = self.connection.clone();
        Ok(task::spawn_blocking(move || {
            let mut connection = connection.lock();
            let transaction = connection.transaction()?;
            let total = transaction.query_row(
                "SELECT COALESCE(SUM(size), 0) FROM media_cache WHERE account = ?1",
                [&account],
                |row| row.get::<_, i64>(0),
            )?;
            if total <= max_bytes {
                transaction.commit()?;
                return Ok(Vec::new());
            }
            let mut reclaimed = 0_i64;
            let mut victims = Vec::new();
            {
                let mut statement = transaction.prepare(
                    "SELECT cache_key, path, size FROM media_cache
                     WHERE account = ?1 ORDER BY last_accessed ASC, cache_key ASC",
                )?;
                let rows = statement.query_map([&account], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, i64>(2)?,
                    ))
                })?;
                for row in rows {
                    let (key, path, size) = row?;
                    victims.push((key, path));
                    reclaimed = reclaimed.saturating_add(size.max(0));
                    if total.saturating_sub(reclaimed) <= max_bytes {
                        break;
                    }
                }
            }
            for (key, _) in &victims {
                transaction.execute(
                    "DELETE FROM media_cache WHERE account = ?1 AND cache_key = ?2",
                    params![account, key],
                )?;
            }
            transaction.commit()?;
            Ok::<Vec<String>, rusqlite::Error>(victims.into_iter().map(|(_, path)| path).collect())
        })
        .await??)
    }

    /// Removes all cached state for one revoked channel and returns media paths
    /// that the caller must remove from disk.
    ///
    /// # Errors
    ///
    /// Returns [`CacheError`] when the purge transaction cannot complete.
    pub async fn purge_channel(
        &self,
        account: String,
        channel: EntityRef,
    ) -> Result<Vec<String>, CacheError> {
        let connection = self.connection.clone();
        Ok(task::spawn_blocking(move || {
            let mut connection = connection.lock();
            let transaction = connection.transaction()?;
            let paths = {
                let mut statement = transaction.prepare(
                    "SELECT path FROM media_cache
                     WHERE account = ?1 AND channel_id = ?2 AND channel_domain = ?3",
                )?;
                statement
                    .query_map(
                        params![account, channel.id.to_string(), channel.domain.as_str()],
                        |row| row.get::<_, String>(0),
                    )?
                    .collect::<Result<Vec<_>, _>>()?
            };
            transaction.execute(
                "DELETE FROM channel_messages
                 WHERE account = ?1 AND channel_id = ?2 AND channel_domain = ?3",
                params![account, channel.id.to_string(), channel.domain.as_str()],
            )?;
            transaction.execute(
                "DELETE FROM media_cache
                 WHERE account = ?1 AND channel_id = ?2 AND channel_domain = ?3",
                params![account, channel.id.to_string(), channel.domain.as_str()],
            )?;
            transaction.execute(
                "DELETE FROM entities
                 WHERE account = ?1 AND kind = 'channel' AND id = ?2 AND domain = ?3",
                params![account, channel.id.to_string(), channel.domain.as_str()],
            )?;
            transaction.commit()?;
            Ok::<Vec<String>, rusqlite::Error>(paths)
        })
        .await??)
    }

    /// Removes all cached state for a signed-out account and returns media paths
    /// that the caller must remove from disk.
    ///
    /// # Errors
    ///
    /// Returns [`CacheError`] when the purge transaction cannot complete.
    pub async fn purge_account(&self, account: String) -> Result<Vec<String>, CacheError> {
        let connection = self.connection.clone();
        Ok(task::spawn_blocking(move || {
            let mut connection = connection.lock();
            let transaction = connection.transaction()?;
            let paths = {
                let mut statement =
                    transaction.prepare("SELECT path FROM media_cache WHERE account = ?1")?;
                statement
                    .query_map([&account], |row| row.get::<_, String>(0))?
                    .collect::<Result<Vec<_>, _>>()?
            };
            transaction.execute("DELETE FROM media_cache WHERE account = ?1", [&account])?;
            transaction.execute(
                "DELETE FROM channel_messages WHERE account = ?1",
                [&account],
            )?;
            transaction.execute("DELETE FROM entities WHERE account = ?1", [&account])?;
            transaction.commit()?;
            Ok::<Vec<String>, rusqlite::Error>(paths)
        })
        .await??)
    }
}

#[derive(Debug, Error)]
pub enum CacheError {
    #[error("cache database failed: {0}")]
    Database(#[from] rusqlite::Error),
    #[error("cache payload failed: {0}")]
    Json(#[from] serde_json::Error),
    #[error("cache worker failed: {0}")]
    Worker(#[from] task::JoinError),
}

#[cfg(test)]
mod tests {
    use std::{error::Error, fs, time::SystemTime};

    use kaede_protocol::{Domain, EntityRef, Snowflake};
    use serde_json::json;

    use super::Cache;

    fn key(id: u64, domain: &str) -> Result<EntityRef, Box<dyn Error>> {
        Ok(EntityRef::new(Snowflake::new(id)?, Domain::parse(domain)?))
    }

    fn cache_path(name: &str) -> Result<std::path::PathBuf, Box<dyn Error>> {
        let nonce = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)?
            .as_nanos();
        Ok(std::env::temp_dir().join(format!(
            "kaede-cache-{name}-{}-{nonce}.sqlite3",
            std::process::id()
        )))
    }

    #[tokio::test]
    async fn entities_are_scoped_by_account_and_origin() -> Result<(), Box<dyn Error>> {
        let path = cache_path("entities")?;
        let cache = Cache::open(&path)?;
        let alpha = key(42, "alpha.example")?;
        let beta = key(42, "beta.example")?;

        cache
            .put_entity(
                "account-a".into(),
                "user".into(),
                alpha.clone(),
                json!({"name": "alpha"}),
            )
            .await?;
        cache
            .put_entity(
                "account-a".into(),
                "user".into(),
                beta.clone(),
                json!({"name": "beta"}),
            )
            .await?;

        let alpha_value = cache
            .get_entity::<serde_json::Value>("account-a".into(), "user".into(), alpha)
            .await?;
        let beta_value = cache
            .get_entity::<serde_json::Value>("account-a".into(), "user".into(), beta.clone())
            .await?;
        let other_account = cache
            .get_entity::<serde_json::Value>("account-b".into(), "user".into(), beta)
            .await?;

        assert_eq!(alpha_value, Some(json!({"name": "alpha"})));
        assert_eq!(beta_value, Some(json!({"name": "beta"})));
        assert_eq!(other_account, None);
        drop(cache);
        let _ = fs::remove_file(path);
        Ok(())
    }

    #[tokio::test]
    async fn access_revocation_purges_only_the_target_channel() -> Result<(), Box<dyn Error>> {
        let path = cache_path("purge")?;
        let cache = Cache::open(&path)?;
        let revoked = key(9, "guild.example")?;
        let retained = key(10, "guild.example")?;

        cache
            .put_entity(
                "account".into(),
                "channel".into(),
                revoked.clone(),
                json!({"name": "private"}),
            )
            .await?;
        cache
            .put_entity(
                "account".into(),
                "channel".into(),
                retained.clone(),
                json!({"name": "public"}),
            )
            .await?;
        cache
            .put_media(
                "account".into(),
                revoked.clone(),
                "private-image".into(),
                "/cache/private-image".into(),
                512,
            )
            .await?;

        let paths = cache
            .purge_channel("account".into(), revoked.clone())
            .await?;
        assert_eq!(paths, vec!["/cache/private-image"]);
        assert_eq!(
            cache
                .get_entity::<serde_json::Value>("account".into(), "channel".into(), revoked)
                .await?,
            None
        );
        assert!(
            cache
                .get_entity::<serde_json::Value>("account".into(), "channel".into(), retained)
                .await?
                .is_some()
        );
        drop(cache);
        let _ = fs::remove_file(path);
        Ok(())
    }

    #[tokio::test]
    async fn media_budget_evicts_oldest_entries_without_crossing_accounts()
    -> Result<(), Box<dyn Error>> {
        let path = cache_path("media-budget")?;
        let cache = Cache::open(&path)?;
        let channel = key(9, "guild.example")?;
        cache
            .put_media(
                "account-a".into(),
                channel.clone(),
                "old".into(),
                "/cache/old".into(),
                80,
            )
            .await?;
        cache
            .put_media(
                "account-a".into(),
                channel.clone(),
                "new".into(),
                "/cache/new".into(),
                80,
            )
            .await?;
        cache
            .put_media(
                "account-b".into(),
                channel,
                "other".into(),
                "/cache/other".into(),
                500,
            )
            .await?;

        let evicted = cache.prune_media("account-a".into(), 100).await?;
        assert_eq!(evicted.len(), 1);
        assert!(matches!(evicted[0].as_str(), "/cache/old" | "/cache/new"));
        assert!(cache.prune_media("account-b".into(), 600).await?.is_empty());
        drop(cache);
        let _ = fs::remove_file(path);
        Ok(())
    }
}
