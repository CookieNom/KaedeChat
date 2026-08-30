from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, ConfigDict, Field, field_validator, model_validator

from app.chat.custom_emojis import canonical_reaction_emoji
from app.chat.custom_stickers import validate_sticker_items
from app.chat.e2ee import (
    interaction_routing_poll,
    validate_e2ee_envelope,
    validate_interaction_routing_contract,
)
from app.chat.expression_authorization import canonical_expression_authority_map
from app.chat.forwarding import validate_forward_snapshot
from app.chat.mention_policy import AllowedMentions
from app.chat.message_flags import (
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_IS_VOICE_MESSAGE,
    PUBLIC_MESSAGE_CREATE_FLAGS,
)
from app.chat.presence import normalize_bot_presence_activities, normalize_presence_since
from app.chat.rich_content import (
    Embed,
    MessageLayoutComponent,
    PollCreate,
    uses_components_v2,
    validate_embed_collection,
    validate_message_components,
)
from app.chat.schemas import (
    MessageBulkDelete,
    MessageEdit,
    canonical_actor_intent_authority_map,
)
from app.core.json_limits import JsonTreeLimits, validate_json_tree
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import DOMAIN_RE
from app.core.text import sanitize_single_line_text
from app.core.types import EntityRef

MAX_DATABASE_SNOWFLAKE = (1 << 63) - 1
KEY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
FEDERATION_EVENT_JSON_LIMITS = JsonTreeLimits(
    max_depth=24,
    max_nodes=16_384,
    max_object_members=1024,
    max_array_members=4096,
    max_key_bytes=256,
    max_string_bytes=1024 * 1024,
)


def _snowflake_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("snowflake must be a decimal string")
    rendered = value
    if (
        not rendered
        or not rendered.isascii()
        or not rendered.isdecimal()
        or (len(rendered) > 1 and rendered.startswith("0"))
    ):
        raise ValueError("snowflake must be a decimal string")
    parsed = int(rendered)
    if parsed > MAX_DATABASE_SNOWFLAKE:
        raise ValueError("snowflake is outside the PostgreSQL BIGINT range")
    return rendered


def _federation_domain(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("domain must be a string")
    domain = value.rstrip(".").lower()
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError("invalid federation domain")
    return domain


SnowflakeString = Annotated[str, BeforeValidator(_snowflake_string)]
FederationDomain = Annotated[str, BeforeValidator(_federation_domain)]


class ActorRef(UnambiguousInputModel):
    model_config = ConfigDict(extra="allow")

    id: SnowflakeString
    domain: FederationDomain


class DMForwardResolveFederationRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    requester: ActorRef
    source_message_ref: EntityRef


class EventEnvelope(UnambiguousInputModel):
    # Unknown optional envelope members must remain in the canonical signing input.
    # Dropping them before verification would make those members malleable in transit.
    model_config = ConfigDict(extra="allow")

    event_id: str = Field(pattern=r"^kcfe_[A-Za-z0-9_-]{16,59}$")
    origin: FederationDomain
    type: str = Field(min_length=1, max_length=100)
    ts: int = Field(ge=0)
    actor: ActorRef
    context: dict[str, Any] = Field(default_factory=dict)
    content: dict[str, Any] = Field(default_factory=dict)
    signatures: dict[str, dict[str, str]]

    @model_validator(mode="before")
    @classmethod
    def bounded_json_structure(cls, value: object) -> object:
        validate_json_tree(
            value,
            limits=FEDERATION_EVENT_JSON_LIMITS,
            label="federation event envelope",
        )
        return value

    @field_validator("signatures")
    @classmethod
    def bounded_signatures(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        if not 1 <= len(value) <= 8:
            raise ValueError("an envelope must contain between one and eight signers")
        for domain, keys in value.items():
            _federation_domain(domain)
            if not 1 <= len(keys) <= 8:
                raise ValueError("an envelope signer must contain between one and eight keys")
            for key_id, signature in keys.items():
                if not KEY_ID_RE.fullmatch(key_id) or len(signature) > 128:
                    raise ValueError("invalid envelope signature entry")
        return value


class InboxRequest(UnambiguousInputModel):
    events: list[EventEnvelope] = Field(min_length=1, max_length=100)


class InboxResult(UnambiguousInputModel):
    event_id: str
    status: Literal["accepted", "duplicate", "rejected", "retry"]
    code: str | None = None


class RemoteUserProfile(UnambiguousInputModel):
    id: SnowflakeString
    origin_domain: FederationDomain
    account_type: Literal["human", "bot"] = "human"
    username: str = Field(pattern=r"^[a-z0-9_.]{2,32}$")
    display_name: str | None = Field(default=None, max_length=100)
    avatar_hash: str | None = Field(default=None, max_length=128)
    banner_hash: str | None = Field(default=None, max_length=128)
    bio: str | None = Field(default=None, max_length=500)
    custom_status: str | None = Field(default=None, max_length=128)
    profile_version: int = Field(default=1, ge=1, le=2_147_483_647)
    e2ee_device_generation: int = Field(default=0, ge=0, le=MAX_DATABASE_SNOWFLAKE)


class ForwardSourceAuthorizeFederationRequest(UnambiguousInputModel):
    """Ask the exact source-channel authority for one short-lived proof."""

    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    source_message_ref: EntityRef
    destination_channel_ref: EntityRef
    destination_encryption_mode: Literal["plaintext", "e2ee"]
    nonce: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    application_ref: EntityRef | None = None
    e2ee_device_id: str | None = Field(
        default=None,
        pattern=r"^(?:ked|kbe|kwe)_[A-Za-z0-9_-]{43}$",
    )

    @model_validator(mode="after")
    def complete_bot_lineage(self) -> ForwardSourceAuthorizeFederationRequest:
        if (self.application_ref is None) != (self.e2ee_device_id is None):
            raise ValueError("forward source bot lineage is incomplete")
        if self.actor.account_type == "human" and self.application_ref is not None:
            raise ValueError("human forward source authorization cannot claim an application")
        if self.actor.account_type == "bot" and self.application_ref is None:
            raise ValueError("bot forward source authorization requires an application")
        return self


class E2EEKeyPackageClaimRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(pattern=r"^keo_[A-Za-z0-9_-]{43}$")
    operation_domain: FederationDomain
    channel_id: SnowflakeString
    channel_domain: FederationDomain
    claimant_id: SnowflakeString
    claimant_domain: FederationDomain
    target_id: SnowflakeString
    target_domain: FederationDomain
    excluded_device_id: str | None = Field(
        default=None,
        pattern=r"^ked_[A-Za-z0-9_-]{43}$",
    )
    # Present only when the target is an explicitly consented participant bot.
    # The guild/DM authority selects these IDs from its durable participation
    # rows; a target home never accepts client-selected device identities.
    bot_device_ids: list[str] = Field(default_factory=list, max_length=16)
    # Keep a conservative default for requests from peers upgrading from the
    # first device-claim protocol revision.
    max_devices: int = Field(default=48, ge=1, le=48)

    @field_validator("bot_device_ids")
    @classmethod
    def canonical_bot_devices(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"kbe_[A-Za-z0-9_-]{43}", item) is None for item in value
        ):
            raise ValueError("bot device IDs must be unique canonical references")
        return value


class E2EERoomProxyRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: SnowflakeString
    channel_domain: FederationDomain
    actor: RemoteUserProfile
    operation_id: str = Field(pattern=r"^keo_[A-Za-z0-9_-]{43}$")
    sender_device_id: str = Field(pattern=r"^ked_[A-Za-z0-9_-]{43}$")
    policy_generation: SnowflakeString | None = None
    epoch: SnowflakeString | None = None
    group_id: str | None = Field(
        default=None,
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    commit: str | None = Field(default=None, min_length=2, max_length=87_384)
    welcome: str | None = Field(default=None, min_length=2, max_length=87_384)
    prepared_vault_revision: SnowflakeString | None = None
    prepared_vault_digest: str | None = Field(
        default=None,
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    vault_attested: bool | None = None

    @model_validator(mode="after")
    def complete_activation(self) -> E2EERoomProxyRequest:
        activation = (
            self.policy_generation,
            self.epoch,
            self.group_id,
            self.commit,
            self.welcome,
            self.prepared_vault_revision,
            self.prepared_vault_digest,
            self.vault_attested,
        )
        if any(item is not None for item in activation) and any(
            item is None for item in activation
        ):
            raise ValueError("room activation context is incomplete")
        if self.vault_attested is False:
            raise ValueError("room activation vault attestation is invalid")
        return self


class E2EERoomOperationStatusRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: SnowflakeString
    channel_domain: FederationDomain
    actor: RemoteUserProfile
    operation_id: str = Field(pattern=r"^keo_[A-Za-z0-9_-]{43}$")


class RelationshipEventContent(UnambiguousInputModel):
    actor: RemoteUserProfile
    target: ActorRef
    request_id: str | None = Field(default=None, pattern=r"^kcr_[A-Za-z0-9_-]{16,59}$")


class DMOpenFederationRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    participants: list[RemoteUserProfile] = Field(min_length=2, max_length=2)
    bot_capability: EventEnvelope | None = None
    bot_runtime_proof: EventEnvelope | None = None

    @field_validator("participants")
    @classmethod
    def distinct_participants(cls, value: list[RemoteUserProfile]) -> list[RemoteUserProfile]:
        identities = {(item.id, item.origin_domain) for item in value}
        if len(identities) != 2:
            raise ValueError("participants must be distinct")
        return value

    @model_validator(mode="after")
    def paired_bot_proofs(self) -> DMOpenFederationRequest:
        if (self.bot_capability is None) != (self.bot_runtime_proof is None):
            raise ValueError("bot capability and application runtime proof must be paired")
        return self


class DMGroupAuthorizeRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: SnowflakeString | None = None
    conversation_domain: FederationDomain | None = None
    inviter: RemoteUserProfile
    invitee: RemoteUserProfile

    @model_validator(mode="after")
    def coherent_authority_context(self) -> DMGroupAuthorizeRequest:
        if (self.conversation_id is None) != (self.conversation_domain is None):
            raise ValueError("group authorization authority context is incomplete")
        return self


class DMGroupMutationRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "rename", "leave", "remove"]
    conversation_id: SnowflakeString
    conversation_domain: FederationDomain
    actor: RemoteUserProfile
    target: RemoteUserProfile | None = None
    name: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def coherent_mutation(self) -> DMGroupMutationRequest:
        if self.action in {"add", "remove"} and self.target is None:
            raise ValueError("member mutation requires a target")
        if self.action not in {"add", "remove"} and self.target is not None:
            raise ValueError("target is not valid for this mutation")
        if self.action == "rename":
            self.name = self.name.strip() if self.name is not None else None
            if self.name == "":
                self.name = None
        elif self.name is not None:
            raise ValueError("name is only valid for rename")
        return self


class InviteResolveRequest(UnambiguousInputModel):
    code: str = Field(pattern=r"^[A-Za-z0-9]{8}$")
    viewer_id: SnowflakeString | None = None


class PresenceFederationRequest(UnambiguousInputModel):
    """Short-lived presence projected by a user's authoritative instance."""

    model_config = ConfigDict(extra="forbid")

    user_id: SnowflakeString
    user_domain: FederationDomain
    status: Literal["online", "idle", "dnd", "offline"]
    activities: list[dict[str, object]] = Field(default_factory=list, max_length=16)
    since: int | None = Field(default=None, ge=0)
    afk: bool = False
    observed_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)

    @field_validator("activities", mode="before")
    @classmethod
    def documented_bot_activities(cls, value: object) -> list[dict[str, object]]:
        return normalize_bot_presence_activities(value)

    @field_validator("since", mode="before")
    @classmethod
    def valid_since(cls, value: object) -> int | None:
        return normalize_presence_since(value)


class GuildJoinRequest(UnambiguousInputModel):
    code: str = Field(pattern=r"^[A-Za-z0-9]{8}$")
    user: RemoteUserProfile


class GuildLeaveRequest(UnambiguousInputModel):
    user: ActorRef


class GuildSelfModerationStatus(UnambiguousInputModel):
    """Private timeout state returned only to the affected user's home."""

    model_config = ConfigDict(extra="forbid")

    guild_id: SnowflakeString
    guild_domain: FederationDomain
    timed_out: bool
    timeout_until: datetime | None = None
    timeout_indefinite: bool = False
    reason: str | None = Field(default=None, max_length=512)
    details_available: bool = True

    @field_validator("reason", mode="before")
    @classmethod
    def clean_reason(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        return sanitize_single_line_text(value, max_characters=512)

    @model_validator(mode="after")
    def consistent_timeout(self) -> GuildSelfModerationStatus:
        if self.timeout_until is not None and self.timeout_until.tzinfo is None:
            raise ValueError("timeout expiry requires a timezone")
        if not self.timed_out:
            if self.timeout_until is not None or self.timeout_indefinite or self.reason is not None:
                raise ValueError("inactive moderation status contains timeout details")
            return self
        if self.timeout_indefinite == (self.timeout_until is not None):
            raise ValueError("active timeout requires exactly one duration mode")
        return self


class GuildHistoryExportRequest(UnambiguousInputModel):
    user: ActorRef


class GuildProxyRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["message.create"]
    actor: RemoteUserProfile
    channel_id: SnowflakeString
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    e2ee: dict[str, object] | None = None
    tts: bool = False
    voice_message: bool = False
    flags: int = Field(default=0, ge=0, le=2_147_483_647)
    embeds: list[Embed] = Field(default_factory=list, max_length=10)
    components: list[MessageLayoutComponent] = Field(default_factory=list, max_length=40)
    poll: PollCreate | None = None
    # Encrypted forwards expose only the union of authority-routable sticker
    # identities across the outer body and one immutable nested snapshot.
    sticker_items: list[dict[str, object]] = Field(default_factory=list, max_length=9)
    expression_authorizations: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        max_length=16,
    )
    forwarded_message_id: EntityRef | None = None
    forwarded_channel_id: EntityRef | None = None
    forward_snapshot: dict[str, object] | None = None
    forward_source_nsfw: bool | None = None
    forward_source_proof: dict[str, object] | None = None
    application_id: EntityRef | None = None
    interaction_integration_type: (
        Literal["guild_install", "user_install", "dm_capability"] | None
    ) = None
    interaction_installation_ref: EntityRef | None = None
    interaction_installation_revision: SnowflakeString | None = None
    interaction_message_type: Literal[20, 23] | None = None
    interaction_metadata: dict[str, object] | None = None
    view_timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    view_persistent: bool = False
    client_nonce: str = Field(min_length=1, max_length=64)
    referenced_message_id: EntityRef | None = None
    allowed_mentions: AllowedMentions | None = None
    mention_user_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    attachments: list[dict[str, object]] = Field(default_factory=list, max_length=10)

    @field_validator("expression_authorizations")
    @classmethod
    def canonical_expression_authorizations(
        cls, value: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        return canonical_expression_authority_map(value)

    @model_validator(mode="after")
    def content_or_attachment(self) -> GuildProxyRequest:
        self.e2ee = validate_e2ee_envelope(self.e2ee)
        if (self.interaction_message_type is None) != (self.interaction_metadata is None):
            raise ValueError("interaction message type and metadata must be supplied together")
        if self.interaction_message_type is not None and self.application_id is None:
            raise ValueError("interaction messages require an application identity")
        encrypted_contract: dict[str, object] | None = None
        encrypted_controls: list[object] = []
        encrypted_poll: dict[str, object] | None = None
        if isinstance(self.e2ee, dict) and "rich_payload_digest" in self.e2ee:
            raw_contract = self.e2ee.get("interaction_contract")
            if raw_contract is not None:
                encrypted_contract = validate_interaction_routing_contract(
                    raw_contract,
                    callback_type=None,
                )
                raw_controls = encrypted_contract.get("components")
                if not isinstance(raw_controls, list):
                    raise ValueError("encrypted interaction controls are invalid")
                encrypted_controls = raw_controls
                encrypted_poll = interaction_routing_poll(encrypted_contract)
        if self.content is not None and self.e2ee is not None:
            raise ValueError("a proxied message cannot mix plaintext and encrypted content")
        if self.e2ee is not None and self.allowed_mentions is not None:
            raise ValueError("encrypted proxy messages carry allowed mentions inside ciphertext")
        validate_embed_collection(self.embeds)
        validate_message_components(self.components)
        components_v2 = uses_components_v2(self.components)
        if self.flags & ~PUBLIC_MESSAGE_CREATE_FLAGS:
            raise ValueError("proxied message flags contain unsupported bits")
        if self.flags & MESSAGE_FLAG_IS_VOICE_MESSAGE and not self.voice_message:
            raise ValueError("the proxied voice-message flag requires a voice-message body")
        encrypted_flags = self.e2ee.get("message_flags") if isinstance(self.e2ee, dict) else None
        encrypted_components_v2 = bool(
            isinstance(encrypted_flags, int)
            and not isinstance(encrypted_flags, bool)
            and encrypted_flags & MESSAGE_FLAG_IS_COMPONENTS_V2
        )
        if self.flags & MESSAGE_FLAG_IS_COMPONENTS_V2 and not (
            components_v2 or encrypted_components_v2
        ):
            raise ValueError("the proxied Components V2 flag requires a Components V2 body")
        encrypted_forward = bool(
            isinstance(self.e2ee, dict) and self.e2ee.get("forward_snapshot_digest") is not None
        )
        self.sticker_items = validate_sticker_items(
            self.sticker_items,
            maximum=9 if encrypted_forward else 3,
        )
        if components_v2 and (
            self.content is not None or self.embeds or self.poll is not None or self.sticker_items
        ):
            raise ValueError(
                "Components V2 messages cannot include content, embeds, polls, or stickers"
            )
        if self.e2ee is not None and (self.embeds or self.components or self.poll is not None):
            raise ValueError("an encrypted proxied message cannot contain rich plaintext")
        if self.forwarded_message_id is not None and (
            (self.e2ee is not None and not encrypted_forward)
            or self.embeds
            or self.components
            or self.poll is not None
            or self.sticker_items
            and not encrypted_forward
            or self.referenced_message_id is not None
            or self.mention_user_ids
        ):
            raise ValueError("a proxied forward can contain only an optional text note")
        if (self.forwarded_message_id is None) != (self.forwarded_channel_id is None):
            raise ValueError("a proxied forward requires complete snapshot provenance")
        if self.forward_snapshot is not None:
            self.forward_snapshot = validate_forward_snapshot(self.forward_snapshot)
        if self.forwarded_message_id is not None and (
            (self.forward_snapshot is None) == (not encrypted_forward)
        ):
            raise ValueError("a proxied forward requires exactly one snapshot transport")
        if self.forwarded_message_id is not None and self.forward_source_nsfw is None:
            raise ValueError("a proxied forward requires its authoritative age context")
        if self.forwarded_message_id is not None and self.forward_source_proof is None:
            raise ValueError("a proxied forward requires its source-authority proof")
        if self.forwarded_message_id is None and (
            self.forward_snapshot is not None
            or encrypted_forward
            or self.forward_source_nsfw is not None
            or self.forward_source_proof is not None
        ):
            raise ValueError("a proxied forward requires its authoritative age context")
        if self.forward_source_proof is not None and self.forwarded_message_id is None:
            raise ValueError("a proxied source proof requires a forward")
        if self.voice_message and (
            self.tts
            or self.content is not None
            or self.embeds
            or self.components
            or self.poll is not None
            or self.sticker_items
            or self.forwarded_message_id is not None
            or self.mention_user_ids
            or len(self.attachments) != 1
        ):
            raise ValueError(
                "a proxied voice message requires exactly one audio attachment and no copied "
                "or rich body"
            )
        has_controls = bool(self.components or encrypted_controls)
        if has_controls and self.application_id is None:
            raise ValueError("interactive components require an application identity")
        lineage = (
            self.interaction_integration_type,
            self.interaction_installation_ref,
            self.interaction_installation_revision,
        )
        if has_controls and not all(item is not None for item in lineage):
            raise ValueError("interactive components require exact installation lineage")
        if not has_controls and any(item is not None for item in lineage):
            raise ValueError("installation lineage requires interactive components")
        if (self.view_timeout_seconds is not None or self.view_persistent) and not has_controls:
            raise ValueError("view lifetime options require message components")
        if self.view_persistent and self.view_timeout_seconds is not None:
            raise ValueError("a persistent view cannot have a timeout")
        if (
            self.content is None
            and self.e2ee is None
            and not self.attachments
            and not self.embeds
            and not self.components
            and self.poll is None
            and encrypted_poll is None
            and not self.sticker_items
            and self.forwarded_message_id is None
        ):
            raise ValueError(
                "a proxied message requires content, an attachment, rich content, or a forward"
            )
        return self


class GuildPollVoteProxyRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    channel_id: SnowflakeString
    message_id: EntityRef
    answer_id: int = Field(ge=1, le=10)
    remove: bool = False


class GuildPollFinalizeProxyRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    channel_id: SnowflakeString
    message_id: EntityRef


class GuildPollVotersProxyRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    channel_id: SnowflakeString
    message_id: EntityRef
    answer_id: int = Field(ge=1, le=10)
    after: EntityRef | None = None
    limit: int = Field(default=50, ge=1, le=100)


class GuildForwardResolveRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    channel_id: SnowflakeString
    message_id: EntityRef


class AnnouncementFollowActorRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    # Bot tokens never cross an instance boundary.  The signed request carries
    # only the public application identity so the channel authority can bind
    # the actor to its mirrored application and installation grants.
    actor_application_ref: EntityRef | None = None
    actor_intent: dict[str, Any] | None = None
    actor_intents: dict[str, dict[str, object]] = Field(default_factory=dict, max_length=2)

    @field_validator("actor_intents")
    @classmethod
    def canonical_actor_intents(
        cls,
        value: dict[str, dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        canonical = canonical_actor_intent_authority_map(value)
        for intent in canonical.values():
            validate_json_tree(
                intent,
                limits=FEDERATION_EVENT_JSON_LIMITS,
                label="announcement actor intent",
            )
        return canonical

    @model_validator(mode="after")
    def one_actor_intent_encoding(self) -> AnnouncementFollowActorRequest:
        if self.actor_intent is not None and self.actor_intents:
            raise ValueError("actor_intent and actor_intents are mutually exclusive")
        return self


class AnnouncementFollowAuthorizeRequest(AnnouncementFollowActorRequest):
    source_channel_ref: EntityRef
    target_channel_id: SnowflakeString
    source_authorization: dict[str, Any] | None = None

    @field_validator("source_authorization")
    @classmethod
    def bounded_source_authorization(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            validate_json_tree(
                value,
                limits=FEDERATION_EVENT_JSON_LIMITS,
                label="announcement source authorization",
            )
        return value


class AnnouncementFollowSourceAuthorizeRequest(AnnouncementFollowActorRequest):
    """Ask the source authority to attest one announcement-channel follow."""

    target_channel_ref: EntityRef


class AnnouncementFollowAcceptRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    receipt: dict[str, Any]


class AnnouncementFollowRevokeRequest(AnnouncementFollowActorRequest):
    follow_id: SnowflakeString
    generation: SnowflakeString


class AnnouncementFollowDeactivateRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    receipt: dict[str, Any]


class AnnouncementCrosspostDeliverRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    follow_id: SnowflakeString
    generation: SnowflakeString
    source_channel_ref: EntityRef
    source_message_ref: EntityRef
    source_author: RemoteUserProfile
    source_message: dict[str, Any]
    application_ref: EntityRef | None = None
    published_at: datetime

    @field_validator("source_message")
    @classmethod
    def bounded_source_message(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_json_tree(
            value,
            limits=FEDERATION_EVENT_JSON_LIMITS,
            label="announcement source message",
        )
        return value

    @field_validator("published_at")
    @classmethod
    def aware_publication_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        return value


class AnnouncementCrosspostResolveRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    follow_id: SnowflakeString
    generation: SnowflakeString
    source_message_ref: EntityRef


class GuildPinProxyRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    channel_id: SnowflakeString
    message_id: EntityRef
    pinned: bool
    reason: str | None = Field(default=None, max_length=512)


class ChannelPinsPageProxyRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    channel_id: SnowflakeString
    before: datetime | None = None
    limit: int = Field(default=50, ge=1, le=50)

    @field_validator("before")
    @classmethod
    def timezone_cursor(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("pin cursor must include a timezone")
        return value.astimezone(UTC) if value is not None else None


class GuildReactionProxyRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    actor: RemoteUserProfile
    channel_id: SnowflakeString
    message_id: EntityRef
    emoji: str = Field(min_length=1, max_length=320)
    remove: bool = False
    expression_authorizations: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        max_length=1,
    )
    application_id: EntityRef | None = None

    @field_validator("expression_authorizations")
    @classmethod
    def canonical_expression_authorizations(
        cls, value: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        return canonical_expression_authority_map(value)

    @field_validator("emoji")
    @classmethod
    def canonical_emoji(cls, value: str) -> str:
        return canonical_reaction_emoji(value)


def _validate_message_edit_attachment_transport(
    edit: MessageEdit | None,
    attachment_refs: list[EntityRef],
    attachments: list[dict[str, Any]],
) -> None:
    """Fence authority-only attachment metadata carried with an edit."""

    if edit is None or edit.attachment_ids is None:
        if attachment_refs or attachments:
            raise ValueError("only attachment edits accept attachment federation metadata")
        return
    if len(attachment_refs) != len(edit.attachment_ids) or any(
        reference.domain is None for reference in attachment_refs
    ):
        raise ValueError("attachment edit references must be complete and qualified")
    if [reference.id for reference in attachment_refs] != [
        int(attachment_id) for attachment_id in edit.attachment_ids
    ]:
        raise ValueError("attachment edit references do not match the edit body")
    if len(attachment_refs) != len(set(attachment_refs)):
        raise ValueError("attachment edit references must be unique")
    try:
        metadata_refs = [EntityRef(f"{item['id']}@{item['origin_domain']}") for item in attachments]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("attachment edit metadata has an invalid reference") from exc
    if len(metadata_refs) != len(set(metadata_refs)) or not set(metadata_refs) <= set(
        attachment_refs
    ):
        raise ValueError("attachment edit metadata must be a unique subset of its references")


class GuildMessageOperationRequest(UnambiguousInputModel):
    """Typed authority transport for non-create message operations."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "message.edit",
        "message.delete",
        "message.bulk_delete",
        "reaction.remove_user",
        "reaction.clear",
        "announcement.crosspost",
    ]
    actor: RemoteUserProfile
    channel_id: SnowflakeString
    message_id: EntityRef | None = None
    message_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    edit: MessageEdit | None = None
    emoji: str | None = Field(default=None, min_length=1, max_length=320)
    target_user_id: EntityRef | None = None
    application_id: EntityRef | None = None
    authoritative_mention_user_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    attachment_refs: list[EntityRef] = Field(default_factory=list, max_length=10)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    expression_authorizations: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        max_length=16,
    )
    expression_sticker_items: list[dict[str, object]] = Field(
        default_factory=list,
        max_length=9,
    )

    @field_validator("expression_authorizations")
    @classmethod
    def canonical_expression_authorizations(
        cls, value: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        return canonical_expression_authority_map(value)

    @field_validator("emoji")
    @classmethod
    def canonical_emoji(cls, value: str | None) -> str | None:
        return canonical_reaction_emoji(value) if value is not None else None

    @model_validator(mode="after")
    def operation_fields(self) -> GuildMessageOperationRequest:
        self.expression_sticker_items = validate_sticker_items(
            self.expression_sticker_items,
            maximum=9,
        )
        bulk = self.operation == "message.bulk_delete"
        if bulk:
            MessageBulkDelete(message_ids=self.message_ids)
        elif self.message_id is None or self.message_ids:
            raise ValueError("message operation requires exactly one message identity")
        if (self.operation == "message.edit") != (self.edit is not None):
            raise ValueError("only message.edit accepts an edit body")
        if self.operation != "message.edit" and self.expression_authorizations:
            raise ValueError("only message.edit accepts expression authorizations")
        if self.operation != "message.edit" and self.expression_sticker_items:
            raise ValueError("only message.edit accepts expression sticker metadata")
        if self.operation != "message.edit" and (
            self.application_id is not None or self.authoritative_mention_user_ids
        ):
            raise ValueError("only message.edit accepts application mention authority")
        if self.authoritative_mention_user_ids and self.application_id is None:
            raise ValueError("authoritative mention edits require an application identity")
        if len(self.authoritative_mention_user_ids) != len(
            set(self.authoritative_mention_user_ids)
        ):
            raise ValueError("authoritative mention user IDs must be unique")
        _validate_message_edit_attachment_transport(
            self.edit,
            self.attachment_refs,
            self.attachments,
        )
        if self.operation == "reaction.remove_user":
            if self.emoji is None or self.target_user_id is None:
                raise ValueError("remove-user reaction requires emoji and target user")
        elif self.operation == "reaction.clear":
            if self.target_user_id is not None:
                raise ValueError("reaction clear does not accept a target user")
        elif self.emoji is not None or self.target_user_id is not None:
            raise ValueError("reaction fields are not valid for this operation")
        return self


class DMMessageOperationRequest(UnambiguousInputModel):
    """Typed authority transport for federated-DM message state."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "message.edit",
        "message.delete",
        "reaction.add",
        "reaction.remove",
        "reaction.list",
        "poll.vote.add",
        "poll.vote.remove",
        "poll.voters.list",
        "poll.end",
        "pin.add",
        "pin.remove",
    ]
    actor: ActorRef
    message_id: EntityRef
    edit: MessageEdit | None = None
    emoji: str | None = Field(default=None, min_length=1, max_length=320)
    answer_id: int | None = Field(default=None, ge=1, le=10)
    after: EntityRef | None = None
    limit: int = Field(default=50, ge=1, le=100)
    application_id: EntityRef | None = None
    authoritative_mention_user_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    attachment_refs: list[EntityRef] = Field(default_factory=list, max_length=10)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=10)

    @field_validator("emoji")
    @classmethod
    def canonical_emoji(cls, value: str | None) -> str | None:
        return canonical_reaction_emoji(value) if value is not None else None

    @model_validator(mode="after")
    def operation_fields(self) -> DMMessageOperationRequest:
        editing = self.operation == "message.edit"
        if editing != (self.edit is not None):
            raise ValueError("only message.edit accepts an edit body")
        if not editing and (self.application_id is not None or self.authoritative_mention_user_ids):
            raise ValueError("only message.edit accepts application mention authority")
        if self.authoritative_mention_user_ids and self.application_id is None:
            raise ValueError("authoritative mention edits require an application identity")
        if len(self.authoritative_mention_user_ids) != len(
            set(self.authoritative_mention_user_ids)
        ):
            raise ValueError("authoritative mention user IDs must be unique")
        _validate_message_edit_attachment_transport(
            self.edit,
            self.attachment_refs,
            self.attachments,
        )
        reaction = self.operation.startswith("reaction.")
        if reaction != (self.emoji is not None):
            raise ValueError("reaction operations require exactly one emoji")
        poll_answer = self.operation in {
            "poll.vote.add",
            "poll.vote.remove",
            "poll.voters.list",
        }
        if poll_answer != (self.answer_id is not None):
            raise ValueError("poll answer operations require exactly one answer")
        paginated = self.operation in {"reaction.list", "poll.voters.list"}
        if not paginated and (self.after is not None or self.limit != 50):
            raise ValueError("pagination is valid only for list operations")
        return self


class InstanceBlockPut(UnambiguousInputModel):
    domain: FederationDomain
    level: Literal["silence", "suspend"]
    include_subdomains: bool = True
    reason: str | None = Field(default=None, max_length=500)
