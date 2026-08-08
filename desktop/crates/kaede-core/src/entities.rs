use chrono::{DateTime, Utc};
use kaede_protocol::{Domain, EntityRef, PermissionBits, ResourceVersion, Snowflake};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct User {
    pub id: Snowflake,
    pub origin_domain: Domain,
    pub username: String,
    pub display_name: Option<String>,
    pub avatar_hash: Option<String>,
    pub banner_hash: Option<String>,
    pub bio: Option<String>,
    pub custom_status: Option<String>,
    #[serde(default)]
    pub profile_version: ResourceVersion,
    #[serde(default)]
    pub profile_resolved: bool,
    #[serde(default)]
    pub handle: String,
    #[serde(default)]
    pub email: Option<String>,
    #[serde(default)]
    pub email_verified: bool,
    #[serde(default)]
    pub mfa_enabled: bool,
}

impl User {
    #[must_use]
    pub fn key(&self) -> EntityRef {
        EntityRef::new(self.id, self.origin_domain.clone())
    }

    #[must_use]
    pub fn label(&self) -> &str {
        self.display_name.as_deref().unwrap_or(&self.username)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Guild {
    pub id: Snowflake,
    pub origin_domain: Domain,
    pub name: String,
    pub description: Option<String>,
    pub icon_hash: Option<String>,
    pub banner_hash: Option<String>,
    pub owner_id: Snowflake,
    pub owner_domain: Domain,
    #[serde(default)]
    pub permissions: PermissionBits,
    #[serde(default)]
    pub permission_generation: String,
    #[serde(default, alias = "history_generation")]
    pub history_policy_generation: String,
    #[serde(default)]
    pub federated_history_policy: String,
    #[serde(default)]
    pub unavailable: bool,
    #[serde(default)]
    pub version: Option<ResourceVersion>,
    #[serde(default)]
    pub channels: Vec<Channel>,
    #[serde(default)]
    pub roles: Vec<Role>,
}

impl Guild {
    #[must_use]
    pub fn key(&self) -> EntityRef {
        EntityRef::new(self.id, self.origin_domain.clone())
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(from = "u8", into = "u8")]
pub enum ChannelKind {
    Text,
    DirectMessage,
    Voice,
    Category,
    Announcement,
    Unknown(u8),
}

impl From<u8> for ChannelKind {
    fn from(value: u8) -> Self {
        match value {
            0 => Self::Text,
            1 => Self::DirectMessage,
            2 => Self::Voice,
            4 => Self::Category,
            5 => Self::Announcement,
            unknown => Self::Unknown(unknown),
        }
    }
}

impl From<ChannelKind> for u8 {
    fn from(value: ChannelKind) -> Self {
        match value {
            ChannelKind::Text => 0,
            ChannelKind::DirectMessage => 1,
            ChannelKind::Voice => 2,
            ChannelKind::Category => 4,
            ChannelKind::Announcement => 5,
            ChannelKind::Unknown(value) => value,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Channel {
    pub id: Snowflake,
    pub origin_domain: Domain,
    pub guild_id: Option<Snowflake>,
    pub guild_domain: Option<Domain>,
    #[serde(rename = "type")]
    pub kind: ChannelKind,
    pub name: Option<String>,
    pub topic: Option<String>,
    #[serde(default)]
    pub position: i32,
    pub parent_id: Option<Snowflake>,
    pub parent_domain: Option<Domain>,
    #[serde(default)]
    pub permissions: PermissionBits,
    #[serde(default)]
    pub permissions_synced: bool,
    #[serde(default, alias = "slowmode_seconds")]
    pub rate_limit_per_user: u32,
    #[serde(default)]
    pub federated_history_policy: Option<String>,
    pub last_message_id: Option<Snowflake>,
    pub last_message_domain: Option<Domain>,
    #[serde(default)]
    pub version: Option<ResourceVersion>,
    #[serde(default)]
    pub recipients: Vec<User>,
}

impl Channel {
    #[must_use]
    pub fn key(&self) -> EntityRef {
        EntityRef::new(self.id, self.origin_domain.clone())
    }

    #[must_use]
    pub fn guild_key(&self) -> Option<EntityRef> {
        Some(EntityRef::new(self.guild_id?, self.guild_domain.clone()?))
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Role {
    pub id: Snowflake,
    pub origin_domain: Domain,
    pub guild_id: Snowflake,
    pub guild_domain: Domain,
    pub name: String,
    #[serde(default)]
    pub color: u32,
    pub permissions: PermissionBits,
    pub position: i32,
    #[serde(default)]
    pub hoist: bool,
    #[serde(default)]
    pub mentionable: bool,
    #[serde(default)]
    pub version: Option<ResourceVersion>,
}

impl Role {
    #[must_use]
    pub fn key(&self) -> EntityRef {
        EntityRef::new(self.id, self.origin_domain.clone())
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Member {
    pub user: User,
    pub guild_id: Snowflake,
    pub guild_domain: Domain,
    pub nickname: Option<String>,
    #[serde(default)]
    pub role_ids: Vec<Snowflake>,
    #[serde(alias = "timed_out_until")]
    pub timeout_until: Option<DateTime<Utc>>,
    #[serde(default)]
    pub timeout_indefinite: bool,
    #[serde(default)]
    pub voice_flags: u32,
    #[serde(default, alias = "version")]
    pub member_version: ResourceVersion,
    pub joined_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Attachment {
    pub id: Snowflake,
    pub origin_domain: Domain,
    pub filename: String,
    pub content_type: Option<String>,
    pub size: u64,
    pub scan_status: Option<String>,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub blurhash: Option<String>,
    #[serde(default)]
    pub variants: Value,
    /// Private native cache path. It is never accepted from or serialized to
    /// the server and is purged when channel access is revoked.
    #[serde(skip)]
    pub local_path: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(untagged)]
pub enum MessageBody {
    Plaintext { content: String },
    Encrypted { e2ee: Value },
    Empty {},
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Message {
    pub id: Snowflake,
    pub origin_domain: Domain,
    pub channel_id: Snowflake,
    pub channel_domain: Domain,
    pub author_id: Option<Snowflake>,
    pub author_domain: Option<Domain>,
    pub author: Option<User>,
    #[serde(default)]
    pub content: Option<String>,
    pub e2ee: Option<Value>,
    #[serde(alias = "nonce")]
    pub client_nonce: Option<String>,
    #[serde(default, alias = "mentions")]
    pub mention_user_refs: Vec<EntityRef>,
    #[serde(default)]
    pub attachments: Vec<Attachment>,
    #[serde(default)]
    pub flags: u64,
    pub created_at: DateTime<Utc>,
    pub edited_at: Option<DateTime<Utc>>,
    pub message_type: Option<i32>,
    pub referenced_message_id: Option<Snowflake>,
    pub referenced_message_domain: Option<Domain>,
    pub deleted_at: Option<DateTime<Utc>>,
    pub delivery_status: Option<String>,
}

/// Sanitized metadata returned by the home instance's SSRF-protected preview
/// service. Native clients must never scrape message URLs directly.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct LinkPreview {
    pub url: String,
    pub title: Option<String>,
    pub description: Option<String>,
    pub site_name: Option<String>,
    pub media_url: Option<String>,
    pub media_type: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PendingMessageState {
    Sending,
    Queued,
    Failed,
}

#[derive(Clone, Debug)]
pub struct PendingMessage {
    pub channel: EntityRef,
    pub author: EntityRef,
    pub client_nonce: String,
    pub content: String,
    pub attachment_ids: Vec<Snowflake>,
    pub mention_user_ids: Vec<EntityRef>,
    pub referenced_message_id: Option<EntityRef>,
    pub created_at: DateTime<Utc>,
    pub state: PendingMessageState,
    pub failure_reason: Option<String>,
}

impl Message {
    #[must_use]
    pub fn key(&self) -> EntityRef {
        EntityRef::new(self.id, self.origin_domain.clone())
    }

    #[must_use]
    pub fn channel_key(&self) -> EntityRef {
        EntityRef::new(self.channel_id, self.channel_domain.clone())
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PresenceStatus {
    Online,
    Idle,
    Dnd,
    Invisible,
    Offline,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Presence {
    pub user_id: Snowflake,
    pub user_domain: Domain,
    pub status: PresenceStatus,
    pub custom_status: Option<String>,
}

#[allow(clippy::struct_excessive_bools)]
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct VoiceState {
    pub user_id: Snowflake,
    pub user_domain: Domain,
    pub guild_id: Option<Snowflake>,
    pub guild_domain: Option<Domain>,
    pub channel_id: Option<Snowflake>,
    pub channel_domain: Option<Domain>,
    #[serde(default)]
    pub self_mute: bool,
    #[serde(default)]
    pub self_deaf: bool,
    #[serde(default)]
    pub server_mute: bool,
    #[serde(default)]
    pub server_deaf: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Call {
    pub id: Snowflake,
    pub channel_id: Snowflake,
    pub channel_domain: Domain,
    pub authority_domain: Domain,
    pub room: String,
    pub state: String,
    pub created_at: i64,
    pub ended_at: Option<i64>,
    pub caller: EntityRef,
    #[serde(default)]
    pub participants: Vec<EntityRef>,
}

impl Call {
    #[must_use]
    pub fn key(&self) -> EntityRef {
        EntityRef::new(self.id, self.authority_domain.clone())
    }

    #[must_use]
    pub fn channel_key(&self) -> EntityRef {
        EntityRef::new(self.channel_id, self.channel_domain.clone())
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ReadState {
    pub channel_id: Snowflake,
    pub channel_domain: Domain,
    #[serde(default)]
    pub guild_id: Option<Snowflake>,
    #[serde(default)]
    pub guild_domain: Option<Domain>,
    #[serde(default, alias = "read_message_id")]
    pub last_read_message_id: Option<Snowflake>,
    #[serde(default, alias = "read_message_domain")]
    pub last_read_message_domain: Option<Domain>,
    #[serde(default)]
    pub unread: bool,
    #[serde(default)]
    pub mention_count: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct UserSettings {
    pub locale: String,
    pub theme: String,
    pub dm_privacy: String,
    #[serde(default)]
    pub notification_settings: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Relationship {
    #[serde(rename = "type")]
    pub kind: String,
    pub user: User,
    pub created_at: Option<DateTime<Utc>>,
    pub updated_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CustomEmoji {
    pub id: Snowflake,
    pub origin_domain: Domain,
    pub guild_id: Snowflake,
    pub guild_domain: Domain,
    pub name: String,
    #[serde(default)]
    pub animated: bool,
    pub media_hash: String,
    pub version: Option<ResourceVersion>,
}

impl CustomEmoji {
    #[must_use]
    pub fn key(&self) -> EntityRef {
        EntityRef::new(self.id, self.origin_domain.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_guild_snapshot_deserializes_without_browser_urls() {
        let payload = serde_json::json!({
            "id": "42",
            "origin_domain": "chat.example",
            "name": "Lanterns",
            "description": null,
            "icon_hash": "abc",
            "banner_hash": null,
            "owner_id": "7",
            "owner_domain": "chat.example",
            "permissions": "3072",
            "permission_generation": "3",
            "federated_history_policy": "disabled",
            "history_policy_generation": "2",
            "unavailable": false,
            "version": "2026-08-07T12:00:00+00:00",
            "channels": [{
                "id": "43", "origin_domain": "chat.example",
                "guild_id": "42", "guild_domain": "chat.example",
                "type": 0, "name": "general", "topic": null, "position": 0,
                "parent_id": null, "parent_domain": null, "permissions": "3072",
                "permissions_synced": false, "rate_limit_per_user": 0,
                "federated_history_policy": "inherit", "last_message_id": null,
                "last_message_domain": null,
                "version": "2026-08-07T12:00:00+00:00"
            }]
        });
        let Ok(guild) = serde_json::from_value::<Guild>(payload) else {
            panic!("backend guild payload should deserialize");
        };
        assert_eq!(guild.icon_hash.as_deref(), Some("abc"));
        assert_eq!(guild.channels.len(), 1);
    }

    #[test]
    fn message_uses_server_nonce_and_mention_field_names() {
        let payload = serde_json::json!({
            "id": "50", "origin_domain": "remote.example",
            "channel_id": "43", "channel_domain": "chat.example",
            "author_id": "8", "author_domain": "remote.example", "author": null,
            "content": "hello", "e2ee": null, "message_type": 0, "flags": 0,
            "client_nonce": "n-1", "referenced_message_id": null,
            "referenced_message_domain": null,
            "mention_user_refs": ["8@remote.example"], "attachments": [],
            "edited_at": null, "deleted_at": null,
            "created_at": "2026-08-07T12:00:00+00:00"
        });
        let Ok(message) = serde_json::from_value::<Message>(payload) else {
            panic!("backend message payload should deserialize");
        };
        assert_eq!(message.client_nonce.as_deref(), Some("n-1"));
        assert_eq!(message.mention_user_refs[0].to_string(), "8@remote.example");
    }

    #[test]
    fn read_state_accepts_the_backend_field_names() {
        let payload = serde_json::json!({
            "channel_id": "43",
            "channel_domain": "chat.example",
            "guild_id": "42",
            "guild_domain": "chat.example",
            "read_message_id": "99",
            "read_message_domain": "remote.example",
            "unread": true,
            "mention_count": 3
        });
        let Ok(state) = serde_json::from_value::<ReadState>(payload) else {
            panic!("backend read-state payload should deserialize");
        };
        assert_eq!(
            state
                .last_read_message_id
                .map(|id| id.to_string())
                .as_deref(),
            Some("99")
        );
        assert_eq!(state.mention_count, 3);
        assert!(state.unread);
    }

    #[test]
    fn account_payload_accepts_security_fields() {
        let payload = serde_json::json!({
            "id": "7", "origin_domain": "chat.example", "username": "lantern",
            "display_name": null, "avatar_hash": null, "banner_hash": null,
            "bio": null, "custom_status": null, "email": "lantern@example.com",
            "email_verified": true, "mfa_enabled": true
        });
        let Ok(user) = serde_json::from_value::<User>(payload) else {
            panic!("account payload should deserialize");
        };
        assert!(user.email_verified);
        assert!(user.mfa_enabled);
    }
}
