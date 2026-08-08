use std::collections::{HashMap, HashSet, VecDeque};

use kaede_protocol::{EntityRef, GatewayEnvelope};
use serde::{Deserialize, de::DeserializeOwned};
use serde_json::Value;
use thiserror::Error;

use crate::{
    Call, Channel, CustomEmoji, Guild, LinkPreview, Member, Message, PendingMessage,
    PendingMessageState, Presence, ReadState, Relationship, Role, User, UserSettings, VoiceState,
};

const MAX_MESSAGES_PER_CHANNEL: usize = 2_000;

#[derive(Default)]
pub struct AppState {
    pub sequence: Option<u64>,
    pub session_id: Option<String>,
    pub current_user: Option<User>,
    pub user_settings: Option<UserSettings>,
    pub users: HashMap<EntityRef, User>,
    pub guilds: HashMap<EntityRef, Guild>,
    pub channels: HashMap<EntityRef, Channel>,
    pub roles: HashMap<EntityRef, Role>,
    pub members: HashMap<(EntityRef, EntityRef), Member>,
    pub messages: HashMap<EntityRef, Message>,
    pub link_previews: HashMap<EntityRef, LinkPreview>,
    pub pending_messages: HashMap<String, PendingMessage>,
    pub message_order: HashMap<EntityRef, VecDeque<EntityRef>>,
    pub presences: HashMap<EntityRef, Presence>,
    pub typing: HashMap<(EntityRef, EntityRef), chrono::DateTime<chrono::Utc>>,
    pub voice_states: HashMap<EntityRef, VoiceState>,
    pub calls: HashMap<EntityRef, Call>,
    pub read_states: HashMap<EntityRef, ReadState>,
    pub relationships: HashMap<EntityRef, Relationship>,
    pub emojis: HashMap<EntityRef, CustomEmoji>,
    /// Credential-free public assets resolved into account-scoped local files.
    /// The key is `<origin>/<content-hash>/<variant>`; entities continue to
    /// carry authoritative hashes so this derived cache can be discarded.
    pub public_assets: HashMap<String, String>,
    pub guild_notification_levels: HashMap<EntityRef, String>,
    pub inaccessible_channels: HashSet<EntityRef>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Reduction {
    pub changed: bool,
    pub reconcile_required: bool,
    pub purge_channels: Vec<EntityRef>,
    pub unknown_event: Option<String>,
    /// A user-facing asynchronous failure delivered by the home instance.
    /// These are not protocol/decode failures and must not trigger a full
    /// reconciliation loop.
    pub user_error: Option<String>,
}

impl AppState {
    pub fn hydrate_identity(&mut self, user: User) {
        self.users.insert(user.key(), user.clone());
        self.current_user = Some(user);
    }

    pub fn hydrate_settings(&mut self, settings: UserSettings) {
        self.user_settings = Some(settings);
    }

    pub fn hydrate_read_states(&mut self, states: impl IntoIterator<Item = ReadState>) {
        for state in states {
            self.read_states.insert(
                EntityRef::new(state.channel_id, state.channel_domain.clone()),
                state,
            );
        }
    }

    pub fn hydrate_guilds(&mut self, guilds: impl IntoIterator<Item = Guild>) {
        for mut guild in guilds {
            for channel in std::mem::take(&mut guild.channels) {
                self.channels.insert(channel.key(), channel);
            }
            for role in std::mem::take(&mut guild.roles) {
                self.roles.insert(role.key(), role);
            }
            self.guilds.insert(guild.key(), guild);
        }
    }

    pub fn hydrate_channels(&mut self, channels: impl IntoIterator<Item = Channel>) {
        for channel in channels {
            self.channels.insert(channel.key(), channel);
        }
    }

    pub fn hydrate_messages(&mut self, channel: &EntityRef, messages: Vec<Message>) {
        if self.inaccessible_channels.contains(channel) {
            return;
        }
        let order = self.message_order.entry(channel.clone()).or_default();
        for message in messages.into_iter().rev() {
            let key = message.key();
            self.messages.insert(key.clone(), message);
            if !order.contains(&key) {
                order.push_back(key);
            }
        }
        while order.len() > MAX_MESSAGES_PER_CHANNEL {
            if let Some(oldest) = order.pop_front() {
                self.messages.remove(&oldest);
            }
        }
    }

    pub fn enqueue_message(&mut self, pending: PendingMessage) {
        self.pending_messages
            .insert(pending.client_nonce.clone(), pending);
    }

    pub fn mark_message_queued(&mut self, nonce: &str) {
        if let Some(pending) = self.pending_messages.get_mut(nonce) {
            pending.state = PendingMessageState::Queued;
        }
    }

    pub fn fail_message(&mut self, nonce: &str, reason: String) {
        if let Some(pending) = self.pending_messages.get_mut(nonce) {
            pending.state = PendingMessageState::Failed;
            pending.failure_reason = Some(reason);
        }
    }

    /// Move a failed optimistic message back to the sending state without
    /// changing its nonce. The nonce is the idempotency key used by both the
    /// local API and federated delivery, so retries must never mint a new one.
    pub fn retry_message(&mut self, nonce: &str) -> Option<PendingMessage> {
        let pending = self.pending_messages.get_mut(nonce)?;
        pending.state = PendingMessageState::Sending;
        pending.failure_reason = None;
        Some(pending.clone())
    }

    #[must_use]
    pub fn channel_messages(&self, channel: &EntityRef) -> Vec<Message> {
        self.message_order
            .get(channel)
            .into_iter()
            .flatten()
            .filter_map(|key| self.messages.get(key).cloned())
            .collect()
    }

    /// Applies one ordered gateway envelope to the account state.
    ///
    /// # Errors
    ///
    /// Returns [`ReduceError`] when a recognized event carries a malformed
    /// payload. Unknown event names are preserved for forward compatibility.
    pub fn reduce(&mut self, envelope: GatewayEnvelope) -> Result<Reduction, ReduceError> {
        if let Some(sequence) = envelope.s {
            if self.sequence.is_some_and(|current| sequence != current + 1) {
                self.sequence = Some(sequence);
                return Ok(Reduction {
                    reconcile_required: true,
                    ..Reduction::default()
                });
            }
            self.sequence = Some(sequence);
        }
        let Some(event) = envelope.t.as_deref() else {
            return Ok(Reduction::default());
        };
        match event {
            "READY" => self.ready(envelope.d),
            "RESUMED" | "VOICE_TOKEN" | "FEDERATION_PEER_STATUS" => Ok(Reduction {
                changed: true,
                ..Reduction::default()
            }),
            "ATTACHMENT_UPDATE" => self.update_attachment(envelope.d),
            "DM_OPEN_REJECTED" => Self::reject_dm_open(envelope.d),
            "GUILD_MEMBERS_CHUNK" => self.upsert_member_chunk(envelope.d),
            "GUILD_MEMBER_LIST_UPDATE" => self.apply_member_list_update(envelope.d),
            "MESSAGE_CREATE" => self.upsert_message(envelope.d),
            "MESSAGE_UPDATE" => self.update_message(&envelope.d),
            "MESSAGE_DELETE" => self.delete_message(&envelope.d),
            "CHANNEL_CREATE"
            | "CHANNEL_UPDATE"
            | "CHANNEL_ACCESS_GRANTED"
            | "CHANNEL_PERMISSION_UPDATE" => self.upsert_channel(envelope.d),
            "CHANNEL_DELETE" | "CHANNEL_ACCESS_REVOKED" => self.remove_channel(&envelope.d),
            "GUILD_CREATE" | "GUILD_UPDATE" | "GUILD_AVAILABILITY_UPDATE" => {
                self.upsert_guild(envelope.d)
            }
            "GUILD_DELETE" => self.remove_guild(&envelope.d),
            "GUILD_ROLE_CREATE" | "GUILD_ROLE_UPDATE" => self.upsert_role(envelope.d),
            "GUILD_ROLE_DELETE" => self.remove_role(&envelope.d),
            "GUILD_MEMBER_ADD" | "GUILD_MEMBER_UPDATE" => self.upsert_member(envelope.d),
            "GUILD_MEMBER_REMOVE" => self.remove_member(&envelope.d),
            "PRESENCE_UPDATE" => self.upsert_presence(envelope.d),
            "TYPING_START" => self.upsert_typing(envelope.d),
            "VOICE_STATE_UPDATE" | "VOICE_CHANNEL_MOVE" => self.upsert_voice(envelope.d),
            "READ_STATE_UPDATE" => self.upsert_read_state(envelope.d),
            "USER_UPDATE" => self.upsert_user_or_relationship(envelope.d),
            "MESSAGE_SEND_REJECTED" => self.reject_message(envelope.d),
            "MESSAGE_DELIVERY_UPDATE" => self.update_delivery(envelope.d),
            "GUILD_EMOJI_CREATE" => self.upsert_emoji(envelope.d),
            "GUILD_EMOJI_DELETE" => self.remove_emoji(&envelope.d),
            "CALL_CREATE" | "CALL_RING" | "CALL_ACCEPT" | "CALL_DECLINE" => {
                self.upsert_call(envelope.d)
            }
            "CALL_END" => self.end_call(envelope.d),
            unknown => Ok(Reduction {
                unknown_event: Some(unknown.to_owned()),
                ..Reduction::default()
            }),
        }
    }

    fn upsert_typing(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        #[derive(Deserialize)]
        struct TypingStart {
            channel_id: kaede_protocol::Snowflake,
            channel_domain: kaede_protocol::Domain,
            user_id: kaede_protocol::Snowflake,
            user_domain: kaede_protocol::Domain,
        }
        let typing: TypingStart = decode(value)?;
        self.typing.insert(
            (
                EntityRef::new(typing.channel_id, typing.channel_domain),
                EntityRef::new(typing.user_id, typing.user_domain),
            ),
            chrono::Utc::now(),
        );
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn ready(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        #[derive(serde::Deserialize)]
        struct Ready {
            session_id: String,
            user: User,
            #[serde(default)]
            guilds: Vec<Guild>,
            #[serde(default)]
            dm_channels: Vec<Channel>,
            #[serde(default)]
            read_states: Vec<ReadState>,
        }
        let ready: Ready = decode(value)?;
        self.session_id = Some(ready.session_id);
        self.current_user = Some(ready.user.clone());
        self.users.insert(ready.user.key(), ready.user);
        self.hydrate_guilds(ready.guilds);
        for channel in ready.dm_channels {
            self.channels.insert(channel.key(), channel);
        }
        for read_state in ready.read_states {
            self.read_states.insert(
                EntityRef::new(read_state.channel_id, read_state.channel_domain.clone()),
                read_state,
            );
        }
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn upsert_message(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let message: Message = decode(value)?;
        let key = message.key();
        let channel = message.channel_key();
        if self.inaccessible_channels.contains(&channel) {
            return Ok(Reduction::default());
        }
        if let Some(nonce) = message.client_nonce.as_ref() {
            self.pending_messages.remove(nonce);
        }
        self.messages.insert(key.clone(), message);
        let order = self.message_order.entry(channel).or_default();
        if !order.contains(&key) {
            order.push_back(key);
        }
        while order.len() > MAX_MESSAGES_PER_CHANNEL {
            if let Some(oldest) = order.pop_front() {
                self.messages.remove(&oldest);
            }
        }
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn update_message(&mut self, value: &Value) -> Result<Reduction, ReduceError> {
        // Content edits carry a complete message payload. Pin and reaction
        // updates deliberately carry only a composite message identity plus
        // the changed relationship, so they must not invalidate the gateway
        // stream merely because they are not full Message objects.
        if let Ok(message) = serde_json::from_value::<Message>(value.clone()) {
            return self
                .upsert_message(serde_json::to_value(message).map_err(ReduceError::Payload)?);
        }

        let key = entity_ref(value)?;
        Ok(Reduction {
            changed: self.messages.contains_key(&key),
            reconcile_required: !self.messages.contains_key(&key),
            ..Reduction::default()
        })
    }

    fn reject_message(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        #[derive(serde::Deserialize)]
        struct Rejection {
            client_nonce: String,
            #[serde(default)]
            code: String,
            reason: Option<String>,
        }
        let rejection: Rejection = decode(value)?;
        let reason = rejection
            .reason
            .unwrap_or_else(|| match rejection.code.as_str() {
                "MEMBER_TIMED_OUT" => "You are timed out in this guild.".to_owned(),
                "MISSING_PERMISSIONS" | "FORBIDDEN" => {
                    "You no longer have permission to send messages here.".to_owned()
                }
                _ => "The remote instance rejected this message.".to_owned(),
            });
        self.fail_message(&rejection.client_nonce, reason);
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn update_delivery(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        #[derive(serde::Deserialize)]
        struct Delivery {
            message_id: kaede_protocol::Snowflake,
            message_domain: kaede_protocol::Domain,
            status: String,
            code: Option<String>,
        }
        let delivery: Delivery = decode(value)?;
        let key = EntityRef::new(delivery.message_id, delivery.message_domain);
        if let Some(message) = self.messages.get_mut(&key) {
            message.delivery_status = Some(delivery.status);
            if delivery.code.is_some() && message.delivery_status.as_deref() == Some("failed") {
                message.flags |= 1_u64 << 63;
            }
        }
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn update_attachment(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        #[derive(Deserialize)]
        struct Update {
            message_id: kaede_protocol::Snowflake,
            message_domain: kaede_protocol::Domain,
            attachment: crate::Attachment,
        }
        let update: Update = decode(value)?;
        let message = EntityRef::new(update.message_id, update.message_domain);
        let Some(message) = self.messages.get_mut(&message) else {
            // The message may be outside the bounded local window. Its final
            // attachment state will be present when that page is fetched.
            return Ok(Reduction::default());
        };
        let attachment = update.attachment;
        if let Some(existing) = message.attachments.iter_mut().find(|candidate| {
            candidate.id == attachment.id && candidate.origin_domain == attachment.origin_domain
        }) {
            *existing = attachment;
        } else {
            message.attachments.push(attachment);
        }
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn reject_dm_open(value: Value) -> Result<Reduction, ReduceError> {
        #[derive(Deserialize)]
        struct Rejection {
            #[allow(dead_code)]
            pair_key: String,
            code: String,
        }
        let rejection: Rejection = decode(value)?;
        let message = match rejection.code.as_str() {
            "DM_BLOCKED" => "This person is not accepting direct messages from you.",
            "DM_PRIVACY_DENIED" | "CANNOT_DM_USER" => {
                "This person’s privacy settings do not allow this direct message."
            }
            _ => "The direct-message request was rejected by the recipient’s home instance.",
        };
        Ok(Reduction {
            user_error: Some(message.to_owned()),
            ..Reduction::default()
        })
    }

    fn upsert_member_chunk(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        #[derive(Deserialize)]
        struct Chunk {
            members: Vec<Member>,
        }
        let chunk: Chunk = decode(value)?;
        for member in chunk.members {
            self.insert_member(member);
        }
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn apply_member_list_update(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        #[derive(Deserialize)]
        struct MemberList {
            ops: Vec<MemberListOperation>,
        }
        #[derive(Deserialize)]
        struct MemberListOperation {
            op: String,
            #[serde(default)]
            items: Vec<Member>,
        }
        let update: MemberList = decode(value)?;
        let mut changed = false;
        for operation in update.ops {
            if operation.op != "SYNC" {
                continue;
            }
            for member in operation.items {
                self.insert_member(member);
                changed = true;
            }
        }
        Ok(Reduction {
            changed,
            ..Reduction::default()
        })
    }

    fn insert_member(&mut self, member: Member) {
        let guild = EntityRef::new(member.guild_id, member.guild_domain.clone());
        let user = member.user.key();
        self.users.insert(user.clone(), member.user.clone());
        self.members.insert((guild, user), member);
    }

    fn delete_message(&mut self, value: &Value) -> Result<Reduction, ReduceError> {
        let key = entity_ref(value)?;
        self.messages.remove(&key);
        self.link_previews.remove(&key);
        for order in self.message_order.values_mut() {
            order.retain(|candidate| candidate != &key);
        }
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn upsert_channel(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let channel: Channel = decode(value)?;
        let key = channel.key();
        self.inaccessible_channels.remove(&key);
        self.channels.insert(key, channel);
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn remove_channel(&mut self, value: &Value) -> Result<Reduction, ReduceError> {
        let key = entity_ref(value)?;
        self.channels.remove(&key);
        self.inaccessible_channels.insert(key.clone());
        self.pending_messages
            .retain(|_, message| message.channel != key);
        if let Some(messages) = self.message_order.remove(&key) {
            for message in messages {
                self.messages.remove(&message);
                self.link_previews.remove(&message);
            }
        }
        Ok(Reduction {
            changed: true,
            purge_channels: vec![key],
            ..Reduction::default()
        })
    }

    fn upsert_guild(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let guild: Guild = decode(value)?;
        self.hydrate_guilds([guild]);
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn remove_guild(&mut self, value: &Value) -> Result<Reduction, ReduceError> {
        let key = entity_ref(value)?;
        self.guilds.remove(&key);
        let channels: Vec<_> = self
            .channels
            .values()
            .filter(|channel| channel.guild_key().as_ref() == Some(&key))
            .map(Channel::key)
            .collect();
        let mut purged = Vec::new();
        for channel in channels {
            let reduction = self.remove_channel(&serde_json::json!({
                "id": channel.id,
                "origin_domain": channel.domain,
            }))?;
            purged.extend(reduction.purge_channels);
        }
        Ok(Reduction {
            changed: true,
            purge_channels: purged,
            ..Reduction::default()
        })
    }

    fn upsert_role(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let role: Role = decode(value)?;
        self.roles.insert(role.key(), role);
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn remove_role(&mut self, value: &Value) -> Result<Reduction, ReduceError> {
        self.roles.remove(&entity_ref(value)?);
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn upsert_member(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let member: Member = decode(value)?;
        self.insert_member(member);
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn remove_member(&mut self, value: &Value) -> Result<Reduction, ReduceError> {
        let guild = nested_entity_ref(value, "guild")?;
        let user = nested_entity_ref(value, "user")?;
        self.members.remove(&(guild, user));
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn upsert_presence(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let presence: Presence = decode(value)?;
        self.presences.insert(
            EntityRef::new(presence.user_id, presence.user_domain.clone()),
            presence,
        );
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn upsert_voice(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let voice: VoiceState = decode(value)?;
        let user = EntityRef::new(voice.user_id, voice.user_domain.clone());
        if voice.channel_id.is_none() {
            self.voice_states.remove(&user);
        } else {
            self.voice_states.insert(user, voice);
        }
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn upsert_call(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let call: Call = decode(value)?;
        if call.state == "ended" {
            self.calls.remove(&call.channel_key());
        } else {
            self.calls.insert(call.channel_key(), call);
        }
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn end_call(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let call: Call = decode(value)?;
        self.calls.remove(&call.channel_key());
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn upsert_read_state(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let read: ReadState = decode(value)?;
        self.read_states.insert(
            EntityRef::new(read.channel_id, read.channel_domain.clone()),
            read,
        );
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn upsert_user_or_relationship(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        if let Some(relationship) = value.get("relationship") {
            #[derive(serde::Deserialize)]
            struct RelationshipUpdate {
                #[serde(rename = "type")]
                kind: String,
                user: User,
            }
            let update: RelationshipUpdate = decode(relationship.clone())?;
            let key = update.user.key();
            self.users.insert(key.clone(), update.user.clone());
            if update.kind == "none" {
                self.relationships.remove(&key);
            } else {
                self.relationships.insert(
                    key,
                    Relationship {
                        kind: update.kind,
                        user: update.user,
                        created_at: None,
                        updated_at: None,
                    },
                );
            }
            return Ok(Reduction {
                changed: true,
                ..Reduction::default()
            });
        }
        let user: User = decode(value)?;
        let key = user.key();
        if self
            .current_user
            .as_ref()
            .is_some_and(|current| current.key() == key)
        {
            self.current_user = Some(user.clone());
        }
        self.users.insert(key, user);
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn upsert_emoji(&mut self, value: Value) -> Result<Reduction, ReduceError> {
        let emoji: CustomEmoji = decode(value)?;
        self.emojis.insert(emoji.key(), emoji);
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }

    fn remove_emoji(&mut self, value: &Value) -> Result<Reduction, ReduceError> {
        self.emojis.remove(&entity_ref(value)?);
        Ok(Reduction {
            changed: true,
            ..Reduction::default()
        })
    }
}

fn decode<T: DeserializeOwned>(value: Value) -> Result<T, ReduceError> {
    serde_json::from_value(value).map_err(ReduceError::Payload)
}

fn entity_ref(value: &Value) -> Result<EntityRef, ReduceError> {
    let (id_name, domain_name) = if value.get("id").is_some() {
        ("id", "origin_domain")
    } else if value.get("channel_id").is_some() {
        ("channel_id", "channel_domain")
    } else if value.get("role_id").is_some() {
        ("role_id", "role_domain")
    } else {
        ("guild_id", "guild_domain")
    };
    let id = value
        .get(id_name)
        .ok_or_else(|| ReduceError::MissingField(id_name.to_owned()))?;
    let domain = value
        .get(domain_name)
        .ok_or_else(|| ReduceError::MissingField(domain_name.to_owned()))?;
    let id = serde_json::from_value(id.clone()).map_err(ReduceError::Payload)?;
    let domain = serde_json::from_value(domain.clone()).map_err(ReduceError::Payload)?;
    Ok(EntityRef::new(id, domain))
}

fn nested_entity_ref(value: &Value, name: &str) -> Result<EntityRef, ReduceError> {
    value
        .get(name)
        .ok_or_else(|| ReduceError::MissingField(name.to_owned()))
        .and_then(entity_ref)
}

#[derive(Debug, Error)]
pub enum ReduceError {
    #[error("gateway payload is invalid: {0}")]
    Payload(serde_json::Error),
    #[error("gateway payload is missing {0}")]
    MissingField(String),
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_json::json;

    fn dispatch(event: &str, sequence: u64, data: Value) -> GatewayEnvelope {
        GatewayEnvelope {
            op: 0,
            d: data,
            s: Some(sequence),
            t: Some(event.to_owned()),
        }
    }

    fn user(id: &str, domain: &str, username: &str) -> Value {
        json!({
            "id": id,
            "origin_domain": domain,
            "username": username,
            "display_name": null,
            "avatar_hash": null,
            "banner_hash": null,
            "bio": null,
            "custom_status": null
        })
    }

    fn member(id: &str, user_domain: &str) -> Value {
        json!({
            "user": user(id, user_domain, "member"),
            "guild_id": "3",
            "guild_domain": "guild.example",
            "nickname": null,
            "role_ids": [],
            "timed_out_until": null,
            "timeout_indefinite": false,
            "voice_flags": 0,
            "joined_at": null
        })
    }

    fn message() -> Value {
        json!({
            "id": "7",
            "origin_domain": "guild.example",
            "channel_id": "5",
            "channel_domain": "guild.example",
            "author_id": "9",
            "author_domain": "remote.example",
            "author": user("9", "remote.example", "author"),
            "content": "hello",
            "e2ee": null,
            "nonce": "client-nonce",
            "mentions": [],
            "attachments": [{
                "id": "11",
                "origin_domain": "guild.example",
                "filename": "photo.png",
                "content_type": "image/png",
                "size": 120,
                "scan_status": "pending",
                "width": null,
                "height": null,
                "blurhash": null,
                "variants": {}
            }],
            "flags": 0,
            "created_at": "2026-08-07T00:00:00Z",
            "edited_at": null,
            "message_type": 0,
            "referenced_message_id": null,
            "referenced_message_domain": null,
            "deleted_at": null,
            "delivery_status": null
        })
    }

    #[test]
    fn sequence_gap_requires_authoritative_reconciliation() {
        let mut state = AppState {
            sequence: Some(10),
            ..AppState::default()
        };
        let reduction = state
            .reduce(dispatch(
                "TYPING_START",
                12,
                json!({
                    "channel_id": "5",
                    "channel_domain": "home.example",
                    "user_id": "9",
                    "user_domain": "remote.example"
                }),
            ))
            .expect("valid envelope");
        assert!(reduction.reconcile_required);
        assert_eq!(state.sequence, Some(12));
    }

    #[test]
    fn typing_is_scoped_by_composite_channel_and_user() {
        let mut state = AppState::default();
        state
            .reduce(dispatch(
                "TYPING_START",
                1,
                json!({
                    "channel_id": "5",
                    "channel_domain": "home.example",
                    "user_id": "9",
                    "user_domain": "remote.example"
                }),
            ))
            .expect("typing payload");
        let channel: EntityRef = "5@home.example".parse().expect("channel reference");
        let user: EntityRef = "9@remote.example".parse().expect("user reference");
        assert!(state.typing.contains_key(&(channel, user)));
    }

    #[test]
    fn calls_are_scoped_to_their_composite_dm_channel() {
        let mut state = AppState::default();
        let call = json!({
            "id": "99",
            "channel_id": "7",
            "channel_domain": "home.example",
            "authority_domain": "remote.example",
            "room": "d.7.99",
            "state": "ringing",
            "created_at": 10,
            "ended_at": null,
            "caller": "1@home.example",
            "participants": ["1@home.example", "2@remote.example"]
        });
        state
            .reduce(dispatch("CALL_RING", 1, call.clone()))
            .expect("call payload");
        let channel = "7@home.example"
            .parse::<EntityRef>()
            .expect("channel reference");
        assert_eq!(
            state.calls.get(&channel).map(|call| call.id.get()),
            Some(99)
        );

        let mut ended = call;
        ended["state"] = json!("ended");
        ended["ended_at"] = json!(11);
        state
            .reduce(dispatch("CALL_END", 2, ended))
            .expect("terminal call payload");
        assert!(!state.calls.contains_key(&channel));
    }

    #[test]
    fn voice_leave_removes_ephemeral_occupancy() {
        let mut state = AppState::default();
        let joined = json!({
            "user_id": "5", "user_domain": "home.example",
            "guild_id": "3", "guild_domain": "home.example",
            "channel_id": "4", "channel_domain": "home.example",
            "self_mute": false, "self_deaf": false,
            "server_mute": false, "server_deaf": false
        });
        state
            .reduce(dispatch("VOICE_STATE_UPDATE", 1, joined.clone()))
            .expect("joined voice payload");
        let user = "5@home.example"
            .parse::<EntityRef>()
            .expect("user reference");
        assert!(state.voice_states.contains_key(&user));
        let mut left = joined;
        left["channel_id"] = Value::Null;
        left["channel_domain"] = Value::Null;
        state
            .reduce(dispatch("VOICE_STATE_UPDATE", 2, left))
            .expect("leave voice payload");
        assert!(!state.voice_states.contains_key(&user));
    }

    #[test]
    fn attachment_processing_updates_the_existing_message() {
        let mut state = AppState::default();
        state
            .reduce(dispatch("MESSAGE_CREATE", 1, message()))
            .expect("message payload");
        state
            .reduce(dispatch(
                "ATTACHMENT_UPDATE",
                2,
                json!({
                    "message_id": "7",
                    "message_domain": "guild.example",
                    "attachment": {
                        "id": "11",
                        "origin_domain": "guild.example",
                        "filename": "photo.png",
                        "content_type": "image/png",
                        "size": 120,
                        "scan_status": "clean",
                        "width": 640,
                        "height": 480,
                        "blurhash": "hash",
                        "variants": {"thumbnail_512": true}
                    }
                }),
            ))
            .expect("attachment update");
        let key: EntityRef = "7@guild.example".parse().expect("message reference");
        let attachment = &state.messages[&key].attachments[0];
        assert_eq!(attachment.scan_status.as_deref(), Some("clean"));
        assert_eq!(attachment.width, Some(640));
    }

    #[test]
    fn partial_pin_update_preserves_the_cached_message() {
        let mut state = AppState::default();
        state
            .reduce(dispatch("MESSAGE_CREATE", 1, message()))
            .expect("message payload");

        let reduction = state
            .reduce(dispatch(
                "MESSAGE_UPDATE",
                2,
                json!({
                    "id": "7",
                    "origin_domain": "guild.example",
                    "pinned": true
                }),
            ))
            .expect("partial pin update");

        let key: EntityRef = "7@guild.example".parse().expect("message reference");
        assert!(reduction.changed);
        assert!(!reduction.reconcile_required);
        assert_eq!(state.messages[&key].content.as_deref(), Some("hello"));
    }

    #[test]
    fn member_chunks_preserve_federated_composite_identity() {
        let mut state = AppState::default();
        state
            .reduce(dispatch(
                "GUILD_MEMBERS_CHUNK",
                1,
                json!({
                    "guild_id": "3",
                    "guild_domain": "guild.example",
                    "members": [member("9", "alpha.example"), member("9", "beta.example")],
                    "chunk_index": 0
                }),
            ))
            .expect("member chunk");
        assert_eq!(state.members.len(), 2);
        assert!(state.members.contains_key(&(
            "3@guild.example".parse().expect("guild"),
            "9@alpha.example".parse().expect("alpha user")
        )));
    }

    #[test]
    fn access_revocation_purges_loaded_and_optimistic_messages() {
        let mut state = AppState::default();
        state
            .reduce(dispatch("MESSAGE_CREATE", 1, message()))
            .expect("message payload");
        let channel: EntityRef = "5@guild.example".parse().expect("channel");
        let author: EntityRef = "9@remote.example".parse().expect("author");
        state.enqueue_message(PendingMessage {
            channel: channel.clone(),
            author,
            client_nonce: "pending".to_owned(),
            content: "pending".to_owned(),
            attachment_ids: Vec::new(),
            mention_user_ids: Vec::new(),
            referenced_message_id: None,
            created_at: chrono::Utc::now(),
            state: PendingMessageState::Sending,
            failure_reason: None,
        });
        let reduction = state
            .reduce(dispatch(
                "CHANNEL_ACCESS_REVOKED",
                2,
                json!({
                    "guild_id": "3",
                    "guild_domain": "guild.example",
                    "channel_id": "5",
                    "channel_domain": "guild.example"
                }),
            ))
            .expect("revocation");
        assert_eq!(reduction.purge_channels, vec![channel.clone()]);
        assert!(state.channel_messages(&channel).is_empty());
        assert!(state.pending_messages.is_empty());
        assert!(state.inaccessible_channels.contains(&channel));
    }

    #[test]
    fn asynchronous_dm_rejection_is_a_user_error_not_a_protocol_failure() {
        let mut state = AppState::default();
        let reduction = state
            .reduce(dispatch(
                "DM_OPEN_REJECTED",
                1,
                json!({"pair_key": "pair", "code": "DM_PRIVACY_DENIED"}),
            ))
            .expect("rejection payload");
        assert_eq!(
            reduction.user_error.as_deref(),
            Some("This person’s privacy settings do not allow this direct message.")
        );
        assert!(!reduction.reconcile_required);
    }
}
