//! Typed, home-instance-only operations used by desktop feature modules.
//!
//! These methods are deliberately thin endpoint adapters and uniformly surface
//! [`ApiClientError`], so repeating an identical `# Errors` section on every
//! operation would obscure the contract-specific documentation.

#![allow(clippy::missing_errors_doc)]

use std::collections::HashMap;

use kaede_core::{
    Call, Channel, CustomEmoji, Guild, Member, Message, ReadState, Relationship, User, UserSettings,
};
use kaede_protocol::{Domain, EntityRef, PermissionBits, ResourceVersion, Snowflake};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use url::form_urlencoded;

use crate::{ApiClient, ApiClientError};

#[derive(Clone)]
pub struct KaedeService {
    api: ApiClient,
}

impl KaedeService {
    #[must_use]
    pub const fn new(api: ApiClient) -> Self {
        Self { api }
    }
    #[must_use]
    pub const fn api(&self) -> &ApiClient {
        &self.api
    }

    pub async fn me(&self) -> Result<User, ApiClientError> {
        self.api.get("users/@me").await
    }
    pub async fn update_me(&self, patch: &ProfilePatch) -> Result<User, ApiClientError> {
        self.api.patch("users/@me", patch, None).await
    }
    pub async fn settings(&self) -> Result<UserSettings, ApiClientError> {
        self.api.get("users/@me/settings").await
    }
    pub async fn update_settings(&self, patch: &Value) -> Result<Value, ApiClientError> {
        self.api.patch("users/@me/settings", patch, None).await
    }
    pub async fn read_states(&self) -> Result<Vec<ReadState>, ApiClientError> {
        self.api.get("users/@me/read-states").await
    }
    pub async fn lookup_user(&self, handle: &str) -> Result<User, ApiClientError> {
        let query = form_urlencoded::Serializer::new(String::new())
            .append_pair("handle", handle)
            .finish();
        self.api.get(&format!("users/lookup?{query}")).await
    }

    pub async fn guilds(&self) -> Result<Vec<Guild>, ApiClientError> {
        self.api.get("users/@me/guilds").await
    }
    pub async fn guild(&self, guild: &EntityRef) -> Result<Guild, ApiClientError> {
        self.api.get(&format!("guilds/{guild}")).await
    }
    pub async fn create_guild(&self, name: &str) -> Result<Guild, ApiClientError> {
        self.api
            .post("guilds", &serde_json::json!({"name": name}))
            .await
    }
    pub async fn update_guild(
        &self,
        guild: &EntityRef,
        patch: &Value,
        version: &ResourceVersion,
    ) -> Result<Guild, ApiClientError> {
        self.api
            .patch(&format!("guilds/{guild}"), patch, Some(version))
            .await
    }
    pub async fn leave_guild(&self, guild: &EntityRef) -> Result<Value, ApiClientError> {
        self.api
            .delete(&format!("guilds/{guild}/members/@me"))
            .await
    }
    pub async fn transfer_guild(
        &self,
        guild: &EntityRef,
        user: &EntityRef,
        version: &ResourceVersion,
    ) -> Result<Guild, ApiClientError> {
        self.api
            .request_with_version(
                reqwest::Method::PUT,
                &format!("guilds/{guild}/owner"),
                &serde_json::json!({"user_id": user}),
                Some(version),
            )
            .await
    }
    pub async fn delete_guild(
        &self,
        guild: &EntityRef,
        version: &ResourceVersion,
    ) -> Result<Value, ApiClientError> {
        self.api
            .delete_with_version(&format!("guilds/{guild}"), version)
            .await
    }

    pub async fn create_channel(
        &self,
        guild: &EntityRef,
        request: &ChannelCreate,
    ) -> Result<Channel, ApiClientError> {
        self.api
            .post(&format!("guilds/{guild}/channels"), request)
            .await
    }
    pub async fn guild_notification_settings(
        &self,
        guild: &EntityRef,
    ) -> Result<NotificationSettings, ApiClientError> {
        self.api
            .get(&format!("guilds/{guild}/notification-settings"))
            .await
    }
    pub async fn all_guild_notification_settings(
        &self,
    ) -> Result<Vec<NotificationSettings>, ApiClientError> {
        self.api.get("users/@me/guild-notification-settings").await
    }
    pub async fn update_guild_notification_settings(
        &self,
        guild: &EntityRef,
        level: &str,
    ) -> Result<NotificationSettings, ApiClientError> {
        self.api
            .put(
                &format!("guilds/{guild}/notification-settings"),
                &serde_json::json!({"level": level}),
            )
            .await
    }
    pub async fn update_channel(
        &self,
        guild: &EntityRef,
        channel: &EntityRef,
        patch: &Value,
        version: &ResourceVersion,
    ) -> Result<Channel, ApiClientError> {
        self.api
            .patch(
                &format!("guilds/{guild}/channels/{channel}"),
                patch,
                Some(version),
            )
            .await
    }
    pub async fn delete_channel(
        &self,
        guild: &EntityRef,
        channel: &EntityRef,
        version: &ResourceVersion,
    ) -> Result<Value, ApiClientError> {
        self.api
            .delete_with_version(&format!("guilds/{guild}/channels/{channel}"), version)
            .await
    }
    pub async fn reorder_channels(
        &self,
        guild: &EntityRef,
        positions: &[PositionPatch],
    ) -> Result<Vec<Channel>, ApiClientError> {
        self.api
            .patch(
                &format!("guilds/{guild}/channels"),
                &serde_json::json!({"channels": positions}),
                None,
            )
            .await
    }
    pub async fn overwrites(
        &self,
        guild: &EntityRef,
        channel: &EntityRef,
    ) -> Result<Vec<Value>, ApiClientError> {
        self.api
            .get(&format!("guilds/{guild}/channels/{channel}/overwrites"))
            .await
    }
    pub async fn set_overwrite(
        &self,
        guild: &EntityRef,
        channel: &EntityRef,
        overwrite: &Value,
    ) -> Result<Value, ApiClientError> {
        self.api
            .put(
                &format!("guilds/{guild}/channels/{channel}/overwrites"),
                overwrite,
            )
            .await
    }
    pub async fn delete_overwrite(
        &self,
        guild: &EntityRef,
        channel: &EntityRef,
        target: &EntityRef,
        kind: &str,
    ) -> Result<Value, ApiClientError> {
        self.api
            .delete(&format!(
                "guilds/{guild}/channels/{channel}/overwrites/{kind}/{target}"
            ))
            .await
    }
    pub async fn sync_channel_permissions(
        &self,
        guild: &EntityRef,
        channel: &EntityRef,
    ) -> Result<Channel, ApiClientError> {
        self.api
            .post(
                &format!("guilds/{guild}/channels/{channel}/permissions/sync"),
                &serde_json::json!({}),
            )
            .await
    }

    pub async fn messages(
        &self,
        channel: &EntityRef,
        page: MessagePage<'_>,
    ) -> Result<Vec<Message>, ApiClientError> {
        let path = {
            let mut query = form_urlencoded::Serializer::new(String::new());
            query.append_pair("limit", &page.limit.clamp(1, 100).to_string());
            if let Some(before) = page.before {
                query.append_pair("before", &before.to_string());
            }
            if let Some(after) = page.after {
                query.append_pair("after", &after.to_string());
            }
            if let Some(around) = page.around {
                query.append_pair("around", &around.to_string());
            }
            format!("channels/{channel}/messages?{}", query.finish())
        };
        self.api.get(&path).await
    }
    pub async fn send_message(
        &self,
        channel: &EntityRef,
        request: &MessageCreate,
    ) -> Result<Value, ApiClientError> {
        self.api
            .post(&format!("channels/{channel}/messages"), request)
            .await
    }
    pub async fn edit_message(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
        content: &str,
    ) -> Result<Message, ApiClientError> {
        self.api
            .patch(
                &format!("channels/{channel}/messages/{message}"),
                &serde_json::json!({"content": content}),
                None,
            )
            .await
    }
    pub async fn delete_message(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
    ) -> Result<Value, ApiClientError> {
        self.api
            .delete(&format!("channels/{channel}/messages/{message}"))
            .await
    }
    pub async fn react(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
        emoji: &str,
    ) -> Result<Value, ApiClientError> {
        self.api
            .post(
                &format!("channels/{channel}/messages/{message}/reactions"),
                &serde_json::json!({"emoji": emoji}),
            )
            .await
    }
    pub async fn remove_reaction(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
        emoji: &str,
    ) -> Result<Value, ApiClientError> {
        let encoded = form_urlencoded::byte_serialize(emoji.as_bytes()).collect::<String>();
        self.api
            .delete(&format!(
                "channels/{channel}/messages/{message}/reactions/{encoded}"
            ))
            .await
    }
    pub async fn pins(&self, channel: &EntityRef) -> Result<Vec<Message>, ApiClientError> {
        self.api.get(&format!("channels/{channel}/pins")).await
    }
    pub async fn set_pinned(
        &self,
        channel: &EntityRef,
        message: &EntityRef,
        pinned: bool,
    ) -> Result<Value, ApiClientError> {
        let path = format!("channels/{channel}/pins/{message}");
        if pinned {
            self.api.put(&path, &serde_json::json!({})).await
        } else {
            self.api.delete(&path).await
        }
    }
    pub async fn bulk_delete(
        &self,
        channel: &EntityRef,
        messages: &[EntityRef],
    ) -> Result<Value, ApiClientError> {
        self.api
            .post(
                &format!("channels/{channel}/messages/bulk-delete"),
                &serde_json::json!({"message_ids": messages}),
            )
            .await
    }
    pub async fn acknowledge(
        &self,
        channel: &EntityRef,
        message: Option<&EntityRef>,
    ) -> Result<Value, ApiClientError> {
        self.api
            .post(
                &format!("channels/{channel}/ack"),
                &serde_json::json!({"message_id": message}),
            )
            .await
    }
    pub async fn typing(&self, channel: &EntityRef) -> Result<Value, ApiClientError> {
        self.api
            .post(
                &format!("channels/{channel}/typing"),
                &serde_json::json!({}),
            )
            .await
    }

    pub async fn voice_occupancy(
        &self,
        channel: &EntityRef,
    ) -> Result<VoiceOccupancy, ApiClientError> {
        self.api
            .get(&format!("channels/{channel}/voice/occupancy"))
            .await
    }

    pub async fn update_voice_moderation(
        &self,
        guild: &EntityRef,
        user: &EntityRef,
        server_mute: Option<bool>,
        server_deaf: Option<bool>,
    ) -> Result<Value, ApiClientError> {
        self.api
            .patch(
                &format!("guilds/{guild}/members/{user}/voice"),
                &serde_json::json!({
                    "server_mute": server_mute,
                    "server_deaf": server_deaf,
                }),
                None,
            )
            .await
    }

    pub async fn disconnect_voice(
        &self,
        guild: &EntityRef,
        user: &EntityRef,
    ) -> Result<Value, ApiClientError> {
        self.api
            .delete(&format!("guilds/{guild}/members/{user}/voice"))
            .await
    }

    pub async fn move_voice(
        &self,
        guild: &EntityRef,
        user: &EntityRef,
        channel: &EntityRef,
    ) -> Result<Value, ApiClientError> {
        self.api
            .post(
                &format!("guilds/{guild}/members/{user}/voice/move"),
                &serde_json::json!({"channel_id": channel}),
            )
            .await
    }

    pub async fn active_call(&self, channel: &EntityRef) -> Result<ActiveCall, ApiClientError> {
        self.api
            .get(&format!("channels/{channel}/calls/active"))
            .await
    }

    pub async fn start_call(&self, channel: &EntityRef) -> Result<Call, ApiClientError> {
        self.api
            .post(
                &format!("channels/{channel}/calls"),
                &serde_json::json!({"ring": true}),
            )
            .await
    }

    pub async fn act_on_call(
        &self,
        call: &EntityRef,
        action: &str,
    ) -> Result<Call, ApiClientError> {
        self.api
            .post(
                &format!("calls/{call}"),
                &serde_json::json!({"action": action}),
            )
            .await
    }

    pub async fn direct_messages(&self) -> Result<Vec<Channel>, ApiClientError> {
        self.api.get("users/@me/channels").await
    }
    pub async fn open_dm(&self, handle: &str) -> Result<Channel, ApiClientError> {
        self.api
            .post("users/@me/channels", &serde_json::json!({"handle": handle}))
            .await
    }
    pub async fn relationships(&self) -> Result<Vec<Relationship>, ApiClientError> {
        self.api.get("users/@me/relationships").await
    }
    pub async fn request_friend(&self, handle: &str) -> Result<Value, ApiClientError> {
        self.api
            .post(
                "users/@me/relationships",
                &serde_json::json!({"handle": handle}),
            )
            .await
    }
    pub async fn accept_friend(&self, user: &EntityRef) -> Result<Value, ApiClientError> {
        self.api
            .put(
                &format!("users/@me/relationships/{user}"),
                &serde_json::json!({}),
            )
            .await
    }
    pub async fn remove_friend(&self, user: &EntityRef) -> Result<Value, ApiClientError> {
        self.api
            .delete(&format!("users/@me/relationships/{user}"))
            .await
    }
    pub async fn set_blocked(
        &self,
        user: &EntityRef,
        blocked: bool,
    ) -> Result<Value, ApiClientError> {
        let path = format!("users/@me/relationships/{user}/block");
        if blocked {
            self.api.put(&path, &serde_json::json!({})).await
        } else {
            self.api.delete(&path).await
        }
    }

    pub async fn create_invite(
        &self,
        guild: &EntityRef,
        request: &InviteCreate,
    ) -> Result<Value, ApiClientError> {
        self.api
            .post(&format!("guilds/{guild}/invites"), request)
            .await
    }
    pub async fn invites(&self, guild: &EntityRef) -> Result<Vec<Value>, ApiClientError> {
        self.api.get(&format!("guilds/{guild}/invites")).await
    }
    pub async fn revoke_invite(
        &self,
        _guild: &EntityRef,
        code: &str,
    ) -> Result<Value, ApiClientError> {
        self.api.delete(&format!("invites/{code}")).await
    }
    pub async fn preview_invite(&self, code: &str) -> Result<Value, ApiClientError> {
        self.api.get(&format!("invites/{code}")).await
    }
    pub async fn accept_invite(&self, code: &str) -> Result<Value, ApiClientError> {
        self.api
            .post(&format!("invites/{code}"), &serde_json::json!({}))
            .await
    }

    pub async fn members(
        &self,
        guild: &EntityRef,
        after: Option<&EntityRef>,
        limit: u16,
    ) -> Result<Vec<Member>, ApiClientError> {
        let path = {
            let mut query = form_urlencoded::Serializer::new(String::new());
            query.append_pair("limit", &limit.clamp(1, 1_000).to_string());
            if let Some(after) = after {
                query.append_pair("after", &after.to_string());
            }
            format!("guilds/{guild}/members?{}", query.finish())
        };
        self.api.get(&path).await
    }
    pub async fn moderate_member(
        &self,
        guild: &EntityRef,
        user: &EntityRef,
        patch: &Value,
    ) -> Result<Value, ApiClientError> {
        self.api
            .patch(&format!("guilds/{guild}/members/{user}"), patch, None)
            .await
    }
    pub async fn kick_member(
        &self,
        guild: &EntityRef,
        user: &EntityRef,
    ) -> Result<Value, ApiClientError> {
        self.api
            .delete(&format!("guilds/{guild}/members/{user}"))
            .await
    }
    pub async fn ban_member(
        &self,
        guild: &EntityRef,
        user: &EntityRef,
        request: &Value,
    ) -> Result<Value, ApiClientError> {
        self.api
            .put(&format!("guilds/{guild}/bans/{user}"), request)
            .await
    }
    pub async fn unban_member(
        &self,
        guild: &EntityRef,
        user: &EntityRef,
    ) -> Result<Value, ApiClientError> {
        self.api
            .delete(&format!("guilds/{guild}/bans/{user}"))
            .await
    }
    pub async fn bans(
        &self,
        guild: &EntityRef,
        cursor: Option<&str>,
    ) -> Result<Value, ApiClientError> {
        let query = cursor.map_or_else(String::new, |cursor| {
            form_urlencoded::Serializer::new(String::new())
                .append_pair("cursor", cursor)
                .finish()
        });
        self.api.get(&format!("guilds/{guild}/bans?{query}")).await
    }
    pub async fn ban_instance(
        &self,
        guild: &EntityRef,
        domain: &Domain,
        request: &Value,
    ) -> Result<Value, ApiClientError> {
        self.api
            .put(&format!("guilds/{guild}/instance-bans/{domain}"), request)
            .await
    }
    pub async fn instance_bans(
        &self,
        guild: &EntityRef,
        cursor: Option<&str>,
    ) -> Result<Value, ApiClientError> {
        let query = cursor.map_or_else(String::new, |cursor| {
            form_urlencoded::Serializer::new(String::new())
                .append_pair("cursor", cursor)
                .finish()
        });
        self.api
            .get(&format!("guilds/{guild}/instance-bans?{query}"))
            .await
    }
    pub async fn unban_instance(
        &self,
        guild: &EntityRef,
        domain: &Domain,
    ) -> Result<Value, ApiClientError> {
        self.api
            .delete(&format!("guilds/{guild}/instance-bans/{domain}"))
            .await
    }
    pub async fn audit_log(
        &self,
        guild: &EntityRef,
        cursor: Option<&str>,
    ) -> Result<Value, ApiClientError> {
        let query = cursor.map_or_else(String::new, |cursor| {
            form_urlencoded::Serializer::new(String::new())
                .append_pair("cursor", cursor)
                .finish()
        });
        self.api
            .get(&format!("guilds/{guild}/audit-logs?{query}"))
            .await
    }

    pub async fn roles(&self, guild: &EntityRef) -> Result<Vec<kaede_core::Role>, ApiClientError> {
        Ok(self.guild(guild).await?.roles)
    }
    pub async fn create_role(
        &self,
        guild: &EntityRef,
        request: &RoleCreate,
    ) -> Result<kaede_core::Role, ApiClientError> {
        self.api
            .post(&format!("guilds/{guild}/roles"), request)
            .await
    }
    pub async fn update_role(
        &self,
        guild: &EntityRef,
        role: &EntityRef,
        patch: &RoleUpdate,
        version: &ResourceVersion,
    ) -> Result<kaede_core::Role, ApiClientError> {
        self.api
            .patch(
                &format!("guilds/{guild}/roles/{role}"),
                patch,
                Some(version),
            )
            .await
    }
    pub async fn delete_role(
        &self,
        guild: &EntityRef,
        role: &EntityRef,
    ) -> Result<Value, ApiClientError> {
        self.api
            .delete(&format!("guilds/{guild}/roles/{role}"))
            .await
    }
    pub async fn reorder_roles(
        &self,
        guild: &EntityRef,
        positions: &[PositionPatch],
    ) -> Result<Vec<Value>, ApiClientError> {
        self.api
            .patch(
                &format!("guilds/{guild}/roles"),
                &serde_json::json!({"roles": positions}),
                None,
            )
            .await
    }
    pub async fn assign_role(
        &self,
        guild: &EntityRef,
        user: &EntityRef,
        role: &EntityRef,
        assign: bool,
    ) -> Result<Value, ApiClientError> {
        let path = format!("guilds/{guild}/members/{user}/roles/{role}");
        if assign {
            self.api.put(&path, &serde_json::json!({})).await
        } else {
            self.api.delete(&path).await
        }
    }

    pub async fn webhooks(&self, guild: &EntityRef) -> Result<Value, ApiClientError> {
        self.api.get(&format!("guilds/{guild}/webhooks")).await
    }
    pub async fn create_webhook(
        &self,
        guild: &EntityRef,
        channel: &EntityRef,
        name: &str,
    ) -> Result<Value, ApiClientError> {
        self.api
            .post(
                &format!("guilds/{guild}/channels/{channel}/webhooks"),
                &serde_json::json!({"name": name}),
            )
            .await
    }
    pub async fn update_webhook(
        &self,
        _guild: &EntityRef,
        webhook: &EntityRef,
        patch: &Value,
    ) -> Result<Value, ApiClientError> {
        self.api
            .patch(&format!("webhooks/{webhook}"), patch, None)
            .await
    }
    pub async fn rotate_webhook_token(
        &self,
        _guild: &EntityRef,
        webhook: &EntityRef,
    ) -> Result<Value, ApiClientError> {
        self.api
            .post(
                &format!("webhooks/{webhook}/rotate"),
                &serde_json::json!({}),
            )
            .await
    }
    pub async fn delete_webhook(
        &self,
        _guild: &EntityRef,
        webhook: &EntityRef,
    ) -> Result<Value, ApiClientError> {
        self.api.delete(&format!("webhooks/{webhook}")).await
    }

    pub async fn available_emojis(&self) -> Result<Vec<CustomEmoji>, ApiClientError> {
        self.api.get("users/@me/emojis").await
    }

    pub async fn gifs(
        &self,
        query: Option<&str>,
        page: u16,
        limit: u16,
    ) -> Result<Value, ApiClientError> {
        let path = {
            let mut params = form_urlencoded::Serializer::new(String::new());
            params.append_pair("page", &page.max(1).to_string());
            params.append_pair("limit", &limit.clamp(1, 50).to_string());
            if let Some(query) = query.filter(|query| !query.trim().is_empty()) {
                params.append_pair("query", query.trim());
            }
            format!("gifs?{}", params.finish())
        };
        self.api.get(&path).await
    }

    pub async fn link_preview(&self, url: &str) -> Result<Value, ApiClientError> {
        self.api
            .post("link-previews", &serde_json::json!({"url": url}))
            .await
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct ProfilePatch {
    pub display_name: Option<String>,
    pub bio: Option<String>,
    pub custom_status: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ChannelCreate {
    pub name: String,
    #[serde(rename = "type")]
    pub kind: u8,
    pub parent_id: Option<Snowflake>,
    pub topic: Option<String>,
    #[serde(default)]
    pub rate_limit_per_user: u32,
}

#[derive(Clone, Debug, Serialize)]
pub struct PositionPatch {
    pub id: Snowflake,
    pub position: i32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_id: Option<Snowflake>,
    #[serde(default)]
    pub sync_permissions: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<ResourceVersion>,
}

#[derive(Clone, Debug, Serialize)]
pub struct RoleCreate {
    pub name: String,
    pub permissions: PermissionBits,
    pub color: u32,
    pub hoist: bool,
    pub mentionable: bool,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct RoleUpdate {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub permissions: Option<PermissionBits>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub color: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hoist: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mentionable: Option<bool>,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct MessagePage<'a> {
    pub before: Option<&'a EntityRef>,
    pub after: Option<&'a EntityRef>,
    pub around: Option<&'a EntityRef>,
    pub limit: u16,
}

#[derive(Clone, Debug, Serialize)]
pub struct MessageCreate {
    pub content: Option<String>,
    pub e2ee: Option<Value>,
    pub client_nonce: String,
    #[serde(default)]
    pub attachment_ids: Vec<Snowflake>,
    #[serde(default)]
    pub mention_user_ids: Vec<EntityRef>,
    pub referenced_message_id: Option<EntityRef>,
}

#[derive(Clone, Debug, Serialize)]
pub struct InviteCreate {
    pub channel_id: Option<EntityRef>,
    pub max_age_seconds: Option<u32>,
    pub max_uses: Option<u16>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct NotificationSettings {
    pub guild_id: Snowflake,
    pub guild_domain: Domain,
    pub level: String,
    #[serde(flatten)]
    pub extra: HashMap<String, Value>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ActiveCall {
    pub call: Option<Call>,
    #[serde(default)]
    pub joined: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct VoiceOccupancy {
    pub room: String,
    #[serde(default)]
    pub participants: Vec<VoiceOccupant>,
    pub generated_at: i64,
    #[serde(default)]
    pub stale: bool,
}

#[derive(Clone, Debug, Deserialize)]
#[allow(clippy::struct_excessive_bools)]
pub struct VoiceOccupant {
    pub identity: EntityRef,
    pub user_id: Snowflake,
    pub user_domain: Domain,
    pub room: String,
    pub guild_id: Option<Snowflake>,
    pub channel_id: Snowflake,
    pub joined_at: i64,
    #[serde(default)]
    pub self_mute: bool,
    #[serde(default)]
    pub self_deaf: bool,
    #[serde(default)]
    pub server_mute: bool,
    #[serde(default)]
    pub server_deaf: bool,
    #[serde(default)]
    pub can_speak: bool,
    #[serde(default)]
    pub can_stream: bool,
}
