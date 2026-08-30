from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar, cast
from urllib.parse import quote, urljoin, urlsplit

import httpx
from websockets.asyncio.client import connect

from ._encoding import encode_base64url as _b64
from .applications import ApplicationAsset, ApplicationAssetKind, ApplicationEmoji
from .automod import (
    AutoModAction,
    AutoModEventType,
    AutoModExecution,
    AutoModRule,
    AutoModTriggerMetadata,
    AutoModTriggerType,
    validate_actions,
    validate_rule_configuration,
)
from .e2ee import (
    BOT_E2EE_CAPABILITIES,
    MLS_SUITE,
    BotE2EEControlPage,
    BotE2EEDevice,
    BotE2EEDeviceChallenge,
    BotE2EEDeviceInventory,
    BotE2EEKeyPackageResult,
    BotE2EEParticipationStatus,
    E2EEProtocolError,
    E2EEProvider,
    InteractionE2EEContext,
    WebhookE2EEControlPage,
    WebhookE2EEDevice,
    WebhookE2EEDeviceChallenge,
    WebhookE2EEDeviceInventory,
    WebhookE2EEForumProposal,
    WebhookE2EEKeyPackageResult,
    WebhookE2EEParticipationStatus,
    bot_key_package_upload_input,
    bot_mls_credential,
    build_disclosed_forward_snapshot,
    build_encrypted_forward_snapshot,
    decrypt_interaction,
    decrypt_message,
    encrypt_message,
    encrypted_forward_snapshot_digest,
    process_e2ee_control,
    require_real_e2ee_provider,
    webhook_key_package_upload_input,
    webhook_mls_credential,
)
from .embeds import Embed, serialize_embeds
from .errors import ApiError, Forbidden, NotFound, RateLimited
from .intents import Intents
from .models import (
    MISSING,
    ActiveCall,
    ApplicationCommandPermissions,
    ApplicationCommandPermissionsUpdateEvent,
    Attachment,
    AuditLogEntry,
    Ban,
    Call,
    Channel,
    ChannelDeleteEvent,
    ChannelInfoEvent,
    ChannelOverwrite,
    ChannelPinsUpdateEvent,
    ChannelPositionUpdate,
    DMOpenRejectedEvent,
    Emoji,
    EmojiDeleteEvent,
    EmojisUpdateEvent,
    ForwardedMessageReference,
    Guild,
    GuildDeleteEvent,
    GuildMembersChunkEvent,
    InstanceBan,
    Interaction,
    Invite,
    InviteTargetUsers,
    InviteTargetUsersJobStatus,
    Member,
    MemberRemoveEvent,
    Message,
    MessageBulkDeleteEvent,
    MessageDeleteEvent,
    MessagePinPage,
    MessageSearchPage,
    MissingType,
    PinEvent,
    PollVoteEvent,
    PresenceEvent,
    RawEvent,
    ReactionClearEvent,
    ReactionEmoji,
    ReactionEvent,
    ReadyEvent,
    Role,
    RoleDeleteEvent,
    ScheduledEvent,
    ScheduledEventEntityType,
    ScheduledEventRecurrenceRule,
    ScheduledEventStatus,
    ScheduledEventUser,
    ScheduledEventUserEvent,
    SoundboardSoundDeleteEvent,
    SoundboardSoundsUpdateEvent,
    StageInstance,
    StageVoiceState,
    Sticker,
    StickerDeleteEvent,
    StickersUpdateEvent,
    ThreadDeleteEvent,
    ThreadListSyncEvent,
    ThreadMember,
    ThreadMembersUpdateEvent,
    ThreadMemberUpdateEvent,
    ThreadPage,
    TrackerBoard,
    TrackerBoardUpdateEvent,
    TrackerLane,
    TrackerLaneDeleteEvent,
    TrackerTask,
    TrackerTaskDeleteEvent,
    TypingEvent,
    VoiceChannelEffectEvent,
    VoiceChannelInfo,
    VoiceChannelStartTimeEvent,
    VoiceChannelStatusEvent,
    VoiceOccupancy,
    VoiceStateEvent,
    Webhook,
)
from .moderation import BulkBanResult, PruneEstimate, PruneResult
from .polls import Poll
from .refs import EntityRef, User, canonical_federation_domain
from .soundboard import SoundboardSound
from .state import WorkerState, canonical_application_home, canonical_target_origin
from .ui import View
from .voice import (
    LiveKitTransport,
    VoiceClient,
    VoiceE2EEContext,
    VoiceGrant,
    VoiceRegion,
    VoiceTransport,
)
from .wire import optional_payload_bool, strict_payload_bool

Handler = Callable[..., Awaitable[None]]
Check = Callable[[object], bool]
T = TypeVar("T")


def _regular_message_allowed_mentions(
    value: Mapping[str, object] | None,
    *,
    reply_author: bool,
) -> dict[str, object]:
    """Normalize Discord's ordinary-message notification defaults."""

    rendered: dict[str, object] = (
        dict(value) if value is not None else {"parse": ["everyone", "roles", "users"]}
    )
    rendered.setdefault("parse", [])
    rendered.setdefault("users", [])
    rendered.setdefault("roles", [])
    rendered.setdefault("replied_user", False)
    raw_parse = rendered["parse"]
    if (
        isinstance(raw_parse, Sequence)
        and not isinstance(raw_parse, (str, bytes))
        and all(isinstance(item, str) for item in raw_parse)
    ):
        rendered["parse"] = sorted(raw_parse)
    if reply_author:
        if value is not None and rendered["replied_user"] is not True:
            raise ValueError(
                "replied_user_ref requires allowed_mentions.replied_user=true"
            )
        rendered["replied_user"] = True
    return rendered


_EVENT_ALIASES = {
    "MESSAGE": "MESSAGE_CREATE",
    "MESSAGE_EDIT": "MESSAGE_UPDATE",
    "REACTION_ADD": "MESSAGE_REACTION_ADD",
    "REACTION_REMOVE": "MESSAGE_REACTION_REMOVE",
    "MEMBER_JOIN": "GUILD_MEMBER_ADD",
    "MEMBER_UPDATE": "GUILD_MEMBER_UPDATE",
    "MEMBER_REMOVE": "GUILD_MEMBER_REMOVE",
    "GUILD_JOIN": "GUILD_CREATE",
    "GUILD_REMOVE": "GUILD_DELETE",
    "ROLE_CREATE": "GUILD_ROLE_CREATE",
    "ROLE_UPDATE": "GUILD_ROLE_UPDATE",
    "ROLE_DELETE": "GUILD_ROLE_DELETE",
    "INTERACTION": "INTERACTION_CREATE",
    "TYPING": "TYPING_START",
    "PRESENCE": "PRESENCE_UPDATE",
    "VOICE_STATE": "VOICE_STATE_UPDATE",
}


def _voice_attachment_metadata(
    *,
    content_type: str,
    encryption_mode: str,
    duration_secs: float | None,
    waveform: bytes | str | None,
) -> tuple[float | None, str | None]:
    """Normalize the shared Discord voice-message upload metadata contract."""

    if (duration_secs is None) != (waveform is None):
        raise ValueError("voice uploads require both duration_secs and waveform")
    if duration_secs is None or waveform is None:
        return None, None
    if isinstance(duration_secs, bool) or not 0 < duration_secs <= 1_200:
        raise ValueError("voice-message duration must be between 0 and 1200 seconds")
    if not content_type.lower().startswith("audio/") or encryption_mode != "plaintext":
        raise ValueError("voice metadata requires a plaintext audio attachment")
    if isinstance(waveform, bytes):
        samples = waveform
        rendered = base64.b64encode(samples).decode()
    else:
        try:
            samples = base64.b64decode(waveform, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("waveform must be canonical base64") from None
        rendered = waveform
        if base64.b64encode(samples).decode() != rendered:
            raise ValueError("waveform must be canonical base64")
    if not 1 <= len(samples) <= 256:
        raise ValueError("waveform must contain 1 to 256 byte samples")
    return duration_secs, rendered


def event_name(value: str) -> str:
    normalized = value.removeprefix("on_").upper()
    return _EVENT_ALIASES.get(normalized, normalized)


def _guild_ref_from_topic(topic: str | None) -> EntityRef | None:
    if topic is None or not topic.startswith("guild:"):
        return None
    parts = topic.split(":", 2)
    if len(parts) != 3 or not parts[1]:
        return None
    try:
        return EntityRef.parse(f"{parts[2]}@{parts[1]}")
    except ValueError:
        return None


def _target_authority(target: str) -> str:
    authority = urlsplit(target).hostname
    if authority is None:
        raise ValueError("target has no authority")
    return canonical_federation_domain(authority)


def _optional_asserted_ref(
    payload: Mapping[str, object],
    *,
    ref_key: str,
    id_key: str,
    domain_key: str,
    label: str,
) -> EntityRef | None:
    """Parse compatible qualified-ref and ID/domain aliases without guessing."""

    has_ref = ref_key in payload
    has_id = id_key in payload
    has_domain = domain_key in payload
    if has_id != has_domain:
        raise ValueError(f"{label} reference is incomplete")
    parsed_ref = EntityRef.parse(payload[ref_key]) if has_ref else None
    paired_ref = (
        EntityRef.from_wire(payload[id_key], payload[domain_key]) if has_id else None
    )
    if parsed_ref is not None and paired_ref is not None and parsed_ref != paired_ref:
        raise ValueError(f"{label} reference aliases conflict")
    return parsed_ref or paired_ref


def _gateway_event_authority(target: str, topic: str | None) -> str:
    authority = _target_authority(target)
    topic_ref = _guild_ref_from_topic(topic)
    if topic is not None and (topic_ref is None or topic_ref.domain != authority):
        raise ValueError("Gateway event changed its subscribed guild authority")
    return authority


def _gateway_guild_scope(
    target: str,
    topic: str | None,
    payload: Mapping[str, object],
) -> EntityRef:
    """Bind guild event lineage to both its connection and subscription topic."""

    topic_ref = _guild_ref_from_topic(topic)
    raw_guild_id = payload.get("guild_id")
    raw_guild_domain = payload.get("guild_domain")
    if (raw_guild_id is None) != (raw_guild_domain is None):
        raise ValueError("Gateway event guild reference is incomplete")
    payload_ref = (
        EntityRef.from_wire(raw_guild_id, raw_guild_domain)
        if raw_guild_id is not None
        else None
    )
    guild_ref = payload_ref or topic_ref
    if (
        guild_ref is None
        or guild_ref.domain != _gateway_event_authority(target, topic)
        or (topic_ref is not None and topic_ref != guild_ref)
    ):
        raise ValueError("Gateway event changed its subscribed guild authority")
    return guild_ref


def _gateway_utf8_size(value: str, *, field: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc


def _gateway_presence_activities(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)) or len(value) > 16:
        raise ValueError("presence activities must be a bounded array")
    normalized: list[dict[str, object]] = []
    for raw in value:
        if (
            not isinstance(raw, Mapping)
            or not {"name", "type"} <= set(raw)
            or not set(raw) <= {"name", "state", "type", "url"}
        ):
            raise ValueError("presence activity contains unsupported fields")
        name = raw.get("name")
        activity_type = raw.get("type")
        if (
            not isinstance(name, str)
            or not name
            or "\x00" in name
            or _gateway_utf8_size(name, field="presence activity name") > 128
            or isinstance(activity_type, bool)
            or not isinstance(activity_type, int)
            or activity_type not in range(6)
        ):
            raise ValueError("presence activity is invalid")
        rendered: dict[str, object] = {"name": name, "type": activity_type}
        state = raw.get("state")
        if state is not None:
            if (
                not isinstance(state, str)
                or not state
                or "\x00" in state
                or _gateway_utf8_size(state, field="presence activity state") > 128
            ):
                raise ValueError("presence activity state is invalid")
            rendered["state"] = state
        elif "state" in raw:
            rendered["state"] = None
        url = raw.get("url")
        if url is not None:
            if (
                not isinstance(url, str)
                or not url
                or "\x00" in url
                or _gateway_utf8_size(url, field="presence streaming URL") > 2048
            ):
                raise ValueError("presence streaming URL is invalid")
            try:
                parsed = urlsplit(url)
                port = parsed.port
            except ValueError:
                raise ValueError("presence streaming URL is invalid") from None
            if (
                activity_type != 1
                or parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or port not in {None, 443}
                or parsed.hostname
                not in {
                    "twitch.tv",
                    "www.twitch.tv",
                    "youtube.com",
                    "www.youtube.com",
                }
            ):
                raise ValueError("presence streaming URL is invalid")
            rendered["url"] = url
        elif "url" in raw:
            rendered["url"] = None
        normalized.append(rendered)
    return normalized


def _gateway_presence_since(value: object) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= (1 << 53) - 1
    ):
        raise ValueError("presence since must be a non-negative millisecond timestamp")
    return value


def _provided_fields(**values: object) -> dict[str, object]:
    return {
        name: value
        for name, value in values.items()
        if not isinstance(value, MissingType)
    }


_EXPRESSION_CUSTOM_EMOJI_RE = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):"
    r"(?P<id>[1-9][0-9]{0,18})@(?P<domain>[A-Za-z0-9.-]{1,253})>"
)


def _expression_partial_emoji_token(
    value: object,
    *,
    default_domain: str,
) -> str | None:
    if not isinstance(value, Mapping):
        return None
    raw_id = value.get("id")
    name = value.get("name")
    animated = value.get("animated", False)
    if (
        raw_id is None
        or not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9_]{2,32}", name) is None
        or not isinstance(animated, bool)
    ):
        return None
    try:
        reference = EntityRef.parse(raw_id, default_domain=default_domain)
    except ValueError:
        return None
    prefix = "a" if animated else ""
    return f"<{prefix}:{name}:{reference}>"


def _expression_projection(
    body: Mapping[str, object],
    *,
    default_domain: str,
) -> dict[str, tuple[list[str], list[str]]]:
    forwarded = body.get("forwarded_message_id") is not None
    e2ee = body.get("e2ee")
    if forwarded and isinstance(e2ee, Mapping):
        return {}
    tokens: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, str):
            tokens.update(
                match.group(0) for match in _EXPRESSION_CUSTOM_EMOJI_RE.finditer(value)
            )
            return
        if isinstance(value, Mapping):
            partial = _expression_partial_emoji_token(
                value.get("emoji"),
                default_domain=default_domain,
            )
            if partial is not None:
                tokens.add(partial)
            for nested in value.values():
                visit(nested)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for nested in value:
                visit(nested)

    fields = ("content",) if forwarded else ("content", "components", "poll")
    for field in fields:
        visit(body.get(field))
    if isinstance(e2ee, Mapping) and "rich_payload_digest" in e2ee:
        visit(e2ee.get("message_custom_emoji_refs", []))

    raw_sticker_refs: object = [] if forwarded else body.get("sticker_ids", [])
    if isinstance(e2ee, Mapping) and "rich_payload_digest" in e2ee:
        raw_sticker_refs = e2ee.get("message_sticker_refs", [])
    if not isinstance(raw_sticker_refs, Sequence) or isinstance(
        raw_sticker_refs, (str, bytes)
    ):
        raise ValueError("expression sticker projection is invalid")

    projections: dict[str, tuple[list[str], list[str]]] = {}
    for token in sorted(tokens):
        match = _EXPRESSION_CUSTOM_EMOJI_RE.fullmatch(token)
        if match is None:
            raise ValueError("expression emoji projection is invalid")
        authority = match.group("domain")
        source_tokens, source_stickers = projections.setdefault(authority, ([], []))
        source_tokens.append(token)
    seen_stickers: set[str] = set()
    for raw_reference in raw_sticker_refs:
        reference = EntityRef.parse(raw_reference, default_domain=default_domain)
        rendered = str(reference)
        if rendered in seen_stickers:
            raise ValueError("expression sticker projection contains duplicates")
        seen_stickers.add(rendered)
        source_tokens, source_stickers = projections.setdefault(
            reference.domain, ([], [])
        )
        source_stickers.append(rendered)
    for source_tokens, source_stickers in projections.values():
        source_tokens.sort()
        source_stickers.sort()
    return dict(sorted(projections.items()))


def _expression_intent_resources(
    *,
    source_authority: str,
    target_guild_ref: str,
    target_channel_ref: str,
    target_message_ref: str | None,
    operation: str,
    operation_id: str,
    emoji_tokens: list[str],
    sticker_refs: list[str],
    authorization_nonce: str,
) -> dict[str, str]:
    projection = json.dumps(
        {"emoji_tokens": emoji_tokens, "sticker_refs": sticker_refs},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "authorization_nonce": authorization_nonce,
        "expression_projection_sha256": hashlib.sha256(projection).hexdigest(),
        "operation": operation,
        "operation_id": operation_id,
        "source_authority": source_authority,
        "target_channel_ref": target_channel_ref,
        "target_guild_ref": target_guild_ref,
        "target_message_ref": target_message_ref or "none",
    }


def _validate_voice_channel_options(
    channel_type: int,
    *,
    bitrate: object,
    user_limit: object,
    video_quality_mode: object,
) -> None:
    """Validate Discord's type-specific Voice and Stage SDK inputs."""

    if isinstance(channel_type, bool) or not isinstance(channel_type, int):
        raise TypeError("channel type must be an integer")
    for label, value in (
        ("bitrate", bitrate),
        ("user_limit", user_limit),
        ("video_quality_mode", video_quality_mode),
    ):
        if not isinstance(value, MissingType) and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise TypeError(f"{label} must be an integer")
    if channel_type not in {2, 13}:
        return
    max_bitrate = 64_000 if channel_type == 13 else 384_000
    max_users = 10_000 if channel_type == 13 else 99
    if not isinstance(bitrate, MissingType):
        checked_bitrate = cast(int, bitrate)
        if not 8_000 <= checked_bitrate <= max_bitrate:
            raise ValueError(f"bitrate must be between 8000 and {max_bitrate}")
    if not isinstance(user_limit, MissingType):
        checked_user_limit = cast(int, user_limit)
        if not 0 <= checked_user_limit <= max_users:
            raise ValueError(f"user_limit must be between 0 and {max_users}")
    if not isinstance(video_quality_mode, MissingType) and video_quality_mode not in {
        1,
        2,
    }:
        raise ValueError("video_quality_mode must be 1 or 2")


def _bounded_entity_refs(
    context: str,
    label: str,
    values: object,
    *,
    maximum: int,
) -> tuple[EntityRef, ...]:
    if (
        isinstance(values, (str, bytes))
        or not isinstance(values, Sequence)
        or any(not isinstance(item, EntityRef) for item in values)
    ):
        raise ValueError(f"{context} {label} must contain entity references")
    refs = cast(tuple[EntityRef, ...], tuple(values))
    if len(refs) > maximum or len(refs) != len(set(refs)):
        raise ValueError(
            f"{context} {label} must be unique and contain at most {maximum} items"
        )
    return refs


def _search_entity_refs(
    label: str,
    values: object,
    *,
    maximum: int,
) -> tuple[EntityRef, ...]:
    return _bounded_entity_refs("message search", label, values, maximum=maximum)


def _invite_management_code(guild: EntityRef, code: str) -> str:
    if not isinstance(code, str):
        raise TypeError("invite code must be a string")
    bare_code, separator, authority = code.rpartition("@")
    if not separator:
        bare_code = code
        authority = ""
    if len(bare_code) != 8 or not bare_code.isascii() or not bare_code.isalnum():
        raise ValueError("invite code is invalid")
    if separator and authority != guild.domain:
        raise ValueError("invite code authority does not match the guild")
    return code


def _normalized_human_handle(handle: str) -> str:
    if not isinstance(handle, str):
        raise TypeError("user handle must be a string")
    username, separator, raw_domain = handle.strip().lower().rpartition("@")
    username = username.removeprefix("@")
    if not separator or not username:
        raise ValueError("user handle must be username@domain")
    domain = canonical_federation_domain(raw_domain.rstrip("."))
    return f"{username}@{domain}"


def _scoped_invite(
    client: Client,
    target: str,
    guild: EntityRef,
    payload: dict[str, Any],
    *,
    channel: EntityRef | None = None,
) -> Invite:
    """Bind an invite response to the exact requested guild/channel authority."""

    invite = Invite.from_payload(client, target, payload)
    if invite.guild_ref != guild or (
        channel is not None and invite.channel_id != channel.id
    ):
        raise ValueError("invite response does not belong to the requested resource")
    return invite


def _scoped_resource_response(
    resource: T,
    *,
    scope: EntityRef | str,
    resource_ref: EntityRef,
    lineage_refs: Sequence[EntityRef] = (),
    expected_ref: EntityRef | None = None,
    label: str,
) -> T:
    """Reject a response that moves a scoped resource into another namespace."""

    scope_domain = scope.domain if isinstance(scope, EntityRef) else scope
    if (
        resource_ref.domain != scope_domain
        or (expected_ref is not None and resource_ref != expected_ref)
        or any(ref.domain != scope_domain for ref in lineage_refs)
    ):
        raise ValueError(f"{label} response does not belong to the requested resource")
    return resource


def _scoped_resource_list(
    resources: Sequence[T],
    *,
    resource_ref: Callable[[T], EntityRef],
    label: str,
) -> list[T]:
    """Keep authority-scoped collection responses duplicate-free."""

    refs = [resource_ref(resource) for resource in resources]
    if len(refs) != len(set(refs)):
        raise ValueError(f"{label} response contains duplicate resources")
    return list(resources)


def _scoped_attachment_response(
    client: Client,
    target: str,
    scope: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
    expected_channel: EntityRef | None | MissingType = MISSING,
    label: str,
) -> Attachment:
    attachment = Attachment.from_payload(client, target, payload)
    if (
        not isinstance(expected_channel, MissingType)
        and attachment.channel_ref != expected_channel
    ):
        raise ValueError(f"{label} response changed the requested channel")
    lineage = (attachment.channel_ref,) if attachment.channel_ref is not None else ()
    return _scoped_resource_response(
        attachment,
        scope=scope,
        resource_ref=attachment.ref,
        lineage_refs=lineage,
        expected_ref=expected_ref,
        label=label,
    )


def _scheduled_event_response(
    client: Client,
    target: str,
    guild: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
    expected_channel: EntityRef | None | MissingType = MISSING,
) -> ScheduledEvent:
    event = ScheduledEvent.from_payload(client, target, payload)
    lineage = [event.guild_ref]
    if event.channel_ref is not None:
        lineage.append(event.channel_ref)
    if event.entity_ref is not None:
        lineage.append(event.entity_ref)
    if (
        event.guild_ref != guild
        or (
            not isinstance(expected_channel, MissingType)
            and event.channel_ref != expected_channel
        )
        or (event.creator is not None and event.creator.ref != event.creator_ref)
    ):
        raise ValueError("scheduled event response changed its requested lineage")
    return _scoped_resource_response(
        event,
        scope=guild,
        resource_ref=event.ref,
        lineage_refs=lineage,
        expected_ref=expected_ref,
        label="scheduled event",
    )


def _auto_mod_action_channels(
    actions: Sequence[AutoModAction],
) -> tuple[EntityRef, ...]:
    return tuple(
        action.channel_ref for action in actions if action.channel_ref is not None
    )


def _auto_mod_rule_response(
    client: Client,
    target: str,
    guild: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
) -> AutoModRule:
    rule = AutoModRule.from_payload(client, target, payload)
    lineage = [
        *rule.exempt_roles,
        *rule.exempt_channels,
        *_auto_mod_action_channels(rule.actions),
    ]
    if rule.guild_ref != guild:
        raise ValueError("AutoMod response changed the requested guild")
    return _scoped_resource_response(
        rule,
        scope=guild,
        resource_ref=rule.ref,
        lineage_refs=lineage,
        expected_ref=expected_ref,
        label="AutoMod rule",
    )


def _webhook_response(
    client: Client,
    target: str,
    scope: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
    expected_guild: EntityRef | None = None,
    expected_channel: EntityRef | None = None,
) -> Webhook:
    webhook = Webhook.from_payload(client, target, payload)
    if (
        expected_guild is not None
        and webhook.guild_ref != expected_guild
        or expected_channel is not None
        and webhook.channel_ref != expected_channel
    ):
        raise ValueError("webhook response changed the requested guild or channel")
    return _scoped_resource_response(
        webhook,
        scope=scope,
        resource_ref=webhook.ref,
        lineage_refs=(webhook.guild_ref, webhook.channel_ref),
        expected_ref=expected_ref,
        label="webhook",
    )


def _webhook_message_response(
    client: Client,
    target: str,
    webhook: EntityRef,
    payload: dict[str, Any],
    *,
    token: str,
    expected_ref: EntityRef | None = None,
    expected_channel: EntityRef | None = None,
    e2ee_device_id: str | None = None,
    response_channel_is_thread: bool = False,
) -> Message:
    """Bind token lifecycle only after exact webhook/message lineage checks."""

    message = Message.from_payload(client, target, payload)
    if (
        message.webhook_ref != webhook
        or message.ref.domain != webhook.domain
        or message.channel_ref.domain != webhook.domain
        or (expected_ref is not None and message.ref != expected_ref)
        or (expected_channel is not None and message.channel_ref != expected_channel)
    ):
        raise ValueError("webhook message response changed its requested lineage")
    return message.bind_webhook_lifecycle(
        webhook.id,
        token,
        thread_id=message.channel_ref
        if response_channel_is_thread
        else expected_channel,
        e2ee_device_id=e2ee_device_id,
    )


_ANNOUNCEMENT_FOLLOW_FIELDS = frozenset(
    {
        "id",
        "ref",
        "source_channel_id",
        "source_channel_domain",
        "target_channel_id",
        "target_channel_domain",
        "creator_id",
        "creator_domain",
        "active",
        "federated",
        "generation",
        "lifecycle_state",
        "name",
        "avatar_hash",
        "created_at",
        "updated_at",
    }
)


def _announcement_follow_response(
    payload: object,
    *,
    source: EntityRef,
    target: EntityRef | None = None,
    require_active: bool | None = None,
) -> dict[str, Any]:
    """Validate the stable target-authority-owned follower identity."""

    if not isinstance(payload, dict) or set(payload) != _ANNOUNCEMENT_FOLLOW_FIELDS:
        raise ValueError("announcement follow response has an invalid shape")
    follow_id = EntityRef.from_wire(payload["id"], payload["target_channel_domain"])
    follow_ref = EntityRef.parse(payload["ref"])
    source_ref = EntityRef.from_wire(
        payload["source_channel_id"], payload["source_channel_domain"]
    )
    target_ref = EntityRef.from_wire(
        payload["target_channel_id"], payload["target_channel_domain"]
    )
    EntityRef.from_wire(payload["creator_id"], payload["creator_domain"])
    if (
        follow_ref != follow_id
        or source_ref != source
        or (target is not None and target_ref != target)
    ):
        raise ValueError("announcement follow response changed its requested lineage")
    active = payload["active"]
    federated = payload["federated"]
    lifecycle = payload["lifecycle_state"]
    generation = payload["generation"]
    if (
        type(active) is not bool
        or type(federated) is not bool
        or lifecycle not in {"pending", "accepted", "active", "revoked"}
        or active is not (lifecycle == "active")
        or (require_active is not None and active is not require_active)
    ):
        raise ValueError("announcement follow response has an invalid lifecycle")
    if federated:
        if not isinstance(generation, str):
            raise ValueError("announcement follow response has an invalid generation")
        parsed_generation = EntityRef.from_wire(generation, target_ref.domain).id
        if str(parsed_generation) != generation:
            raise ValueError("announcement follow response has an invalid generation")
    elif generation is not None or lifecycle != "active":
        raise ValueError("local announcement follow response has federation state")
    for field_name, maximum in (("name", 80), ("avatar_hash", 128)):
        value = payload[field_name]
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError("announcement follow response has invalid metadata")
    try:
        created_at = datetime.fromisoformat(payload["created_at"])
        updated_at = datetime.fromisoformat(payload["updated_at"])
    except (TypeError, ValueError):
        raise ValueError(
            "announcement follow response has invalid timestamps"
        ) from None
    if (
        created_at.tzinfo is None
        or updated_at.tzinfo is None
        or updated_at < created_at
    ):
        raise ValueError("announcement follow response has invalid timestamps")
    return cast(dict[str, Any], payload)


def _announcement_follow_page(
    payload: object,
    *,
    source: EntityRef,
) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) > 10_000:
        raise ValueError("announcement follow page has an invalid shape")
    result: list[dict[str, Any]] = []
    previous: tuple[int, str] | None = None
    for raw in payload:
        item = _announcement_follow_response(
            raw,
            source=source,
            require_active=True,
        )
        ref = EntityRef.parse(item["ref"])
        ordering = (ref.id, ref.domain)
        if previous is not None and ordering <= previous:
            raise ValueError(
                "announcement follow page contains duplicate or unordered entries"
            )
        previous = ordering
        result.append(item)
    return result


def _webhook_token_ref(origin: str, webhook_id: int) -> EntityRef:
    return EntityRef(webhook_id, _target_authority(origin))


def _stage_instance_response(
    client: Client,
    target: str,
    channel: EntityRef,
    payload: dict[str, Any],
) -> StageInstance:
    stage = StageInstance.from_payload(client, target, payload)
    lineage = [stage.guild_ref, stage.channel_ref]
    if stage.scheduled_event_ref is not None:
        lineage.append(stage.scheduled_event_ref)
    if stage.channel_ref != channel:
        raise ValueError("Stage response changed the requested channel")
    return _scoped_resource_response(
        stage,
        scope=channel,
        resource_ref=stage.ref,
        lineage_refs=lineage,
        label="Stage instance",
    )


def _stage_voice_state_response(
    payload: dict[str, Any],
    *,
    guild: EntityRef,
    expected_user: EntityRef,
) -> StageVoiceState:
    """Bind a Stage voice-state response to its requested actor and authority."""

    state = StageVoiceState.from_payload(payload)
    if (
        state.guild_ref != guild
        or state.channel_ref.domain != guild.domain
        or state.user_ref != expected_user
    ):
        raise ValueError("Stage voice-state response changed its requested lineage")
    return state


def _tracker_lane_response(
    client: Client,
    target: str,
    channel: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
) -> TrackerLane:
    lane = TrackerLane.from_payload(client, target, payload)
    if lane.channel_ref != channel:
        raise ValueError("tracker lane response changed the requested channel")
    return _scoped_resource_response(
        lane,
        scope=channel,
        resource_ref=lane.ref,
        lineage_refs=(lane.channel_ref,),
        expected_ref=expected_ref,
        label="tracker lane",
    )


def _tracker_task_response(
    client: Client,
    target: str,
    channel: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
    expected_lane: EntityRef | None = None,
) -> TrackerTask:
    task = TrackerTask.from_payload(client, target, payload)
    if task.channel_ref != channel or (
        expected_lane is not None and task.lane_ref != expected_lane
    ):
        raise ValueError("tracker task response changed its requested lineage")
    return _scoped_resource_response(
        task,
        scope=channel,
        resource_ref=task.ref,
        lineage_refs=(task.channel_ref, task.lane_ref),
        expected_ref=expected_ref,
        label="tracker task",
    )


def _tracker_board_response(
    client: Client,
    target: str,
    channel: EntityRef,
    payload: dict[str, Any],
) -> TrackerBoard:
    board = TrackerBoard.from_payload(client, target, payload)
    if board.channel_ref != channel:
        raise ValueError("tracker response changed the requested channel")
    lanes = [
        _scoped_resource_response(
            lane,
            scope=channel,
            resource_ref=lane.ref,
            lineage_refs=(lane.channel_ref,),
            label="tracker lane",
        )
        for lane in board.lanes
        if lane.channel_ref == channel
    ]
    if len(lanes) != len(board.lanes):
        raise ValueError("tracker lane response changed the requested channel")
    tasks = [
        _scoped_resource_response(
            task,
            scope=channel,
            resource_ref=task.ref,
            lineage_refs=(task.channel_ref, task.lane_ref),
            label="tracker task",
        )
        for task in board.tasks
        if task.channel_ref == channel
    ]
    if len(tasks) != len(board.tasks):
        raise ValueError("tracker task response changed the requested channel")
    board.lanes = _scoped_resource_list(
        lanes,
        resource_ref=lambda lane: lane.ref,
        label="tracker lane",
    )
    board.tasks = _scoped_resource_list(
        tasks,
        resource_ref=lambda task: task.ref,
        label="tracker task",
    )
    lane_refs = {lane.ref for lane in board.lanes}
    if any(task.lane_ref not in lane_refs for task in board.tasks):
        raise ValueError("tracker task response references an unknown lane")
    return board


def _application_asset_response(
    client: Client,
    target: str,
    application: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
) -> ApplicationAsset:
    asset = ApplicationAsset.from_payload(client, target, payload)
    if asset.application_ref != application:
        raise ValueError("application asset response changed the requested application")
    return _scoped_resource_response(
        asset,
        scope=application,
        resource_ref=asset.ref,
        expected_ref=expected_ref,
        label="application asset",
    )


def _application_emoji_response(
    client: Client,
    target: str,
    application: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
) -> ApplicationEmoji:
    emoji = ApplicationEmoji.from_payload(client, target, payload)
    if emoji.application_ref != application:
        raise ValueError("application emoji response changed the requested application")
    return _scoped_resource_response(
        emoji,
        scope=application,
        resource_ref=emoji.ref,
        expected_ref=expected_ref,
        label="application emoji",
    )


def _emoji_response(
    client: Client,
    target: str,
    guild: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
) -> Emoji:
    emoji = Emoji.from_payload(client, target, payload)
    if emoji.guild_ref != guild:
        raise ValueError("emoji response changed the requested guild")
    return _scoped_resource_response(
        emoji,
        scope=guild,
        resource_ref=emoji.ref,
        lineage_refs=emoji.roles,
        expected_ref=expected_ref,
        label="emoji",
    )


def _sticker_response(
    client: Client,
    target: str,
    guild: EntityRef,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
) -> Sticker:
    sticker = Sticker.from_payload(client, target, payload)
    if sticker.guild_ref != guild:
        raise ValueError("sticker response changed the requested guild")
    return _scoped_resource_response(
        sticker,
        scope=guild,
        resource_ref=sticker.ref,
        expected_ref=expected_ref,
        label="sticker",
    )


def _soundboard_response(
    client: Client,
    target: str,
    scope: EntityRef | str,
    payload: dict[str, Any],
    *,
    expected_ref: EntityRef | None = None,
    default: bool = False,
) -> SoundboardSound:
    sound = SoundboardSound.from_payload(client, target, payload)
    if (default and sound.guild_ref is not None) or (
        not default and (not isinstance(scope, EntityRef) or sound.guild_ref != scope)
    ):
        raise ValueError("soundboard response changed the requested guild")
    lineage = (sound.emoji_ref,) if sound.emoji_ref is not None else ()
    return _scoped_resource_response(
        sound,
        scope=scope,
        resource_ref=sound.ref,
        lineage_refs=lineage,
        expected_ref=expected_ref,
        label="soundboard sound",
    )


def _search_string_filters(
    label: str,
    values: object,
    *,
    maximum: int,
    item_limit: int,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    if (
        isinstance(values, (str, bytes))
        or not isinstance(values, Sequence)
        or any(not isinstance(item, str) for item in values)
    ):
        raise ValueError(f"message search {label} must contain strings")
    strings = cast(tuple[str, ...], tuple(values))
    if (
        len(strings) > maximum
        or len(strings) != len(set(strings))
        or any(not item.strip() or len(item) > item_limit for item in strings)
        or (allowed is not None and any(item not in allowed for item in strings))
    ):
        raise ValueError(f"message search {label} is invalid")
    return strings


_MESSAGE_SEARCH_HAS_FILTERS = frozenset(
    {
        "image",
        "sound",
        "video",
        "file",
        "sticker",
        "embed",
        "link",
        "poll",
        "snapshot",
        "-image",
        "-sound",
        "-video",
        "-file",
        "-sticker",
        "-embed",
        "-link",
        "-poll",
        "-snapshot",
    }
)
_MESSAGE_SEARCH_AUTHOR_FILTERS = frozenset(
    {"user", "bot", "webhook", "-user", "-bot", "-webhook"}
)
_MESSAGE_SEARCH_EMBED_TYPES = frozenset({"image", "video", "gif", "sound", "article"})


def _canonical_search_hostnames(values: object) -> tuple[str, ...]:
    hostnames = _search_string_filters(
        "link hostnames",
        values,
        maximum=100,
        item_limit=256,
    )
    normalized: list[str] = []
    for value in hostnames:
        candidate = value.strip().rstrip(".").lower()
        try:
            parsed = urlsplit(f"//{candidate}")
            port = parsed.port
        except ValueError as exc:
            raise ValueError("message search link hostnames are invalid") from exc
        if (
            not candidate
            or parsed.hostname != candidate
            or port is not None
            or parsed.username is not None
        ):
            raise ValueError("message search link hostnames are invalid")
        normalized.append(candidate)
    if len(normalized) != len(set(normalized)):
        raise ValueError("message search link hostnames must be unique")
    return tuple(normalized)


def _canonical_search_extensions(values: object) -> tuple[str, ...]:
    extensions = _search_string_filters(
        "attachment extensions",
        values,
        maximum=100,
        item_limit=256,
    )
    normalized = tuple(value.strip().removeprefix(".").lower() for value in extensions)
    if any(
        not value
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in value
        )
        for value in normalized
    ) or len(normalized) != len(set(normalized)):
        raise ValueError("message search attachment extensions are invalid")
    return normalized


def _require_media_response_success(
    response: httpx.Response,
    *,
    operation: Literal["download", "upload"],
) -> None:
    """Translate object-store failures without disclosing presigned bearer URLs."""

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ApiError(
            502,
            f"MEDIA_{operation.upper()}_FAILED",
            f"Object storage {operation} failed",
            {"upstream_status": exc.response.status_code},
        ) from None


def _wire_forum_tags(
    value: list[dict[str, Any]] | MissingType,
) -> list[dict[str, Any]] | MissingType:
    if isinstance(value, MissingType):
        return value
    rendered: list[dict[str, Any]] = []
    for tag in value:
        item = dict(tag)
        for key in ("id", "emoji_id"):
            if key in item and item[key] is not None:
                item[key] = str(item[key])
        rendered.append(item)
    return rendered


def _wire_forum_emoji(
    value: dict[str, Any] | None | MissingType,
) -> dict[str, Any] | None | MissingType:
    if value is None or isinstance(value, MissingType):
        return value
    rendered = dict(value)
    if "emoji_id" in rendered and rendered["emoji_id"] is not None:
        rendered["emoji_id"] = str(rendered["emoji_id"])
    return rendered


def _version_headers(version: str | None) -> dict[str, str]:
    if not version:
        raise ValueError("the current resource version is required for this update")
    return {"If-Match": version}


def _audit_headers(reason: str | None) -> dict[str, str] | None:
    if reason is None:
        return None
    cleaned = reason.strip()
    if not cleaned:
        return None
    if len(cleaned) > 512:
        raise ValueError("audit log reasons cannot exceed 512 characters")
    return {"X-Audit-Log-Reason": cleaned}


def _merge_headers(*groups: dict[str, str] | None) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    for group in groups:
        if group is not None:
            merged.update(group)
    return merged or None


def _installation_headers(installation_id: int | None) -> dict[str, str] | None:
    if installation_id is None:
        return None
    return {"X-Kaede-Bot-Installation": str(installation_id)}


def _dm_installation_headers(
    installation_ref: EntityRef,
    installation_type: Literal["guild", "user"],
) -> dict[str, str]:
    if installation_ref.domain is None:
        raise ValueError("a DM installation reference must be qualified")
    return {
        "X-Kaede-Bot-Installation": str(installation_ref),
        "X-Kaede-Bot-Installation-Type": installation_type,
    }


def _dm_runtime_headers(
    installation_ref: EntityRef,
    installation_type: Literal["guild", "user"],
) -> dict[str, str]:
    """Keep the qualified source proof separate from legacy numeric install IDs."""

    if installation_ref.domain is None:
        raise ValueError("a DM installation reference must be qualified")
    return {
        "X-Kaede-Bot-Source-Installation": str(installation_ref),
        "X-Kaede-Bot-Installation-Type": installation_type,
    }


def _gateway_runtime_binding(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact grant projected onto a private Gateway event."""

    installation_id = payload.get("bot_installation_id")
    grant_id = payload.get("bot_dm_capability_id")
    if installation_id is not None and grant_id is not None:
        raise ValueError("Gateway event combines installation and DM grants")
    if grant_id is None:
        return {
            "installation_id": (
                int(str(installation_id)) if installation_id is not None else None
            )
        }
    revision = payload.get("bot_dm_capability_revision")
    installation_ref = payload.get(
        "bot_installation_ref",
        payload.get("installation_ref"),
    )
    installation_type = payload.get(
        "bot_installation_type",
        payload.get("installation_type"),
    )
    if (
        not isinstance(grant_id, str)
        or not grant_id.startswith("kbdg_")
        or revision is None
        or not isinstance(installation_ref, str)
        or installation_type not in {"guild", "user"}
    ):
        raise ValueError("Gateway event has an incomplete DM capability binding")
    return {
        "dm_capability_id": grant_id,
        "dm_capability_revision": int(str(revision)),
        "installation_ref": EntityRef.parse(installation_ref),
        "installation_type": cast(Literal["guild", "user"], installation_type),
    }


def _wire_refs(refs: Sequence[EntityRef], *, name: str, maximum: int) -> list[str]:
    rendered = [str(item) for item in refs]
    if len(rendered) > maximum:
        raise ValueError(f"{name} cannot contain more than {maximum} entries")
    if len(rendered) != len(set(rendered)):
        raise ValueError(f"{name} must be unique")
    return rendered


def _expression_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_]{2,32}", value) is None:
        raise ValueError(
            "expression names must contain 2 to 32 letters, numbers, or underscores"
        )
    return value


def _sticker_name(value: str) -> str:
    cleaned = value.strip()
    if not 2 <= len(cleaned) <= 30:
        raise ValueError("sticker names must contain 2 to 30 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError("sticker names cannot contain control characters")
    return cleaned


def _stage_topic(value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 120:
        raise ValueError("Stage topics must contain between 1 and 120 characters")
    return cleaned


def _sticker_description(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not 2 <= len(cleaned) <= 100:
        raise ValueError("sticker descriptions must contain 2 to 100 characters")
    return cleaned


def _sticker_tags(values: Sequence[str]) -> list[str]:
    if len(values) > 10:
        raise ValueError("stickers cannot have more than 10 tags")
    cleaned = [item.strip() for item in values]
    if any(not item or len(item) > 100 or "," in item for item in cleaned):
        raise ValueError(
            "sticker tags must contain 1 to 100 characters and cannot contain commas"
        )
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("sticker tags must be unique")
    if len(",".join(cleaned)) > 200:
        raise ValueError("sticker tags cannot exceed 200 serialized characters")
    return cleaned


_APPLICATION_ASSET_KINDS = {
    "icon",
    "cover",
    "store",
    "achievement",
    "activity",
    "other",
}

DM_CAPABILITY_REFRESH_WINDOW_SECONDS = 60.0
_DMCapabilityKey = tuple[EntityRef, str]
_RuntimeTokenKey = tuple[str, str | None, int | None, bool]


@dataclass(frozen=True, slots=True)
class _DMCapabilityContext:
    installation_ref: EntityRef
    installation_type: Literal["guild", "user"]
    grant_id: str
    revision: int
    expires_at: float
    target: str
    lineage_ref: EntityRef | None = None


@dataclass(frozen=True, slots=True)
class _InteractionLifecycleGrant:
    headers: dict[str, str]
    expires_at: float
    installation_revision: int | None
    channel_ref: EntityRef


@dataclass(frozen=True, slots=True)
class _InteractionResponseIdentity:
    interaction_ref: EntityRef
    response_ref: EntityRef
    application_ref: EntityRef
    channel_ref: EntityRef | None
    sequence: int
    revision: int
    response_type: int
    ephemeral: bool


@dataclass(frozen=True, slots=True)
class _PollVoterScope:
    channel_ref: EntityRef
    message_ref: EntityRef
    answer_id: int
    target: str


def _dm_capability_headers(context: _DMCapabilityContext) -> dict[str, str]:
    return _dm_runtime_headers(
        context.installation_ref,
        context.installation_type,
    ) | {"X-Kaede-Bot-DM-Capability": context.grant_id}


def _require_call_dm_capability_context(
    call: Call,
    context: _DMCapabilityContext | None,
) -> None:
    """Bind a call response to the exact capability used for its request."""

    observed = (
        call.dm_capability_id,
        call.dm_capability_revision,
        call.installation_ref,
        call.installation_type,
    )
    expected = (
        (
            context.grant_id,
            context.revision,
            context.installation_ref,
            context.installation_type,
        )
        if context is not None
        else (None, None, None, None)
    )
    if observed != expected or (
        context is not None and canonical_target_origin(call.target) != context.target
    ):
        raise ValueError("call response changed its DM capability lineage")


def _dm_capability_error_is_terminal(error: ApiError) -> bool:
    """Only C's explicit exact-lineage fence authorizes local lease teardown."""

    return error.code == "BOT_DM_GRANT_FENCED"


@dataclass(frozen=True, slots=True)
class BotIdentity:
    user: User
    application_ref: EntityRef
    worker_id: int
    scopes: frozenset[str]
    intents: frozenset[str]
    token_expires_at: datetime

    @classmethod
    def from_payload(cls, payload: object) -> BotIdentity:
        if not isinstance(payload, dict) or not isinstance(payload.get("user"), dict):
            raise E2EEProtocolError("bot identity response is invalid")
        try:
            application_ref = EntityRef.parse(payload["application_ref"])
            worker_raw = payload["worker_id"]
            if (
                not isinstance(worker_raw, str)
                or not worker_raw.isascii()
                or not worker_raw.isdecimal()
                or worker_raw.startswith("0")
            ):
                raise ValueError
            worker_id = int(worker_raw)
            expires_raw = payload["token_expires_at"]
            if not isinstance(expires_raw, str):
                raise ValueError
            token_expires_at = datetime.fromisoformat(
                expires_raw.replace("Z", "+00:00")
            )
            if token_expires_at.tzinfo is None:
                raise ValueError
            scopes = payload["scopes"]
            intents = payload["intents"]
            if (
                not isinstance(scopes, list)
                or any(not isinstance(item, str) for item in scopes)
                or len(scopes) != len(set(scopes))
                or not isinstance(intents, list)
                or any(not isinstance(item, str) for item in intents)
                or len(intents) != len(set(intents))
            ):
                raise ValueError
            user = User.from_payload(cast(dict[str, object], payload["user"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise E2EEProtocolError("bot identity response is invalid") from exc
        return cls(
            user=user,
            application_ref=application_ref,
            worker_id=worker_id,
            scopes=frozenset(cast(list[str], scopes)),
            intents=frozenset(cast(list[str], intents)),
            token_expires_at=token_expires_at.astimezone(UTC),
        )


class Client:
    def __init__(
        self,
        *,
        worker_state: WorkerState,
        intents: Intents | None = None,
        e2ee_device_id: str | None = None,
    ):
        self.worker_state = worker_state
        self.intents = intents or Intents.default()
        self._targets: dict[str, httpx.AsyncClient] = {}
        self._tokens: dict[_RuntimeTokenKey, tuple[str, float]] = {}
        self._bot_user_refs: dict[str, EntityRef] = {}
        self._actor_runtime_revisions: dict[str, dict[str, str]] = {}
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._commands: list[dict[str, Any]] = []
        self._views: dict[EntityRef, View] = {}
        self._response_views: dict[EntityRef, View] = {}
        self._persistent_views: dict[str, View] = {}
        self._cursors: dict[str, dict[str, int]] = defaultdict(dict)
        self._waiters: dict[str, list[tuple[asyncio.Future[object], Check | None]]] = (
            defaultdict(list)
        )
        self._stopping = False
        self._sockets: set[Any] = set()
        self._gateway_sockets: dict[str, Any] = {}
        self._cursor_lock = asyncio.Lock()
        self._gateway_tasks: dict[str, asyncio.Task[None]] = {}
        self._dm_gateway_tasks: dict[_DMCapabilityKey, asyncio.Task[None]] = {}
        self._explicit_targets: set[str] = set()
        self._discovered_targets: set[str] = set()
        self._discovery_task: asyncio.Task[None] | None = None
        self._interaction_e2ee_contexts: dict[EntityRef, InteractionE2EEContext] = {}
        self._voice_e2ee_contexts: dict[EntityRef, VoiceE2EEContext] = {}
        self._voice_clients: dict[str, VoiceClient] = {}
        self._e2ee_control_checkpoints = (
            self.worker_state.load_e2ee_control_checkpoints()
        )
        self._interaction_lifecycle_grants: dict[
            EntityRef, _InteractionLifecycleGrant
        ] = {}
        self._interaction_response_identities: dict[
            tuple[EntityRef, str, int | None], _InteractionResponseIdentity
        ] = {}
        self._poll_voter_cursors: dict[int, tuple[EntityRef, _PollVoterScope]] = {}
        self._e2ee_device_id: str | None = None
        self._dm_capabilities: dict[_DMCapabilityKey, _DMCapabilityContext] = {}
        self._dm_default_capabilities: dict[EntityRef, str] = {}
        self._dm_capability_locks: dict[_DMCapabilityKey, asyncio.Lock] = {}
        self._dm_refresh_tasks: dict[_DMCapabilityKey, asyncio.Task[None]] = {}
        self._capability_targets: dict[str, set[_DMCapabilityKey]] = defaultdict(set)
        self._started = False
        self._starting = False
        self._application_home = canonical_application_home(
            f"https://{self.worker_state.application_ref.domain}",
            self.worker_state.application_ref,
        )
        for target, cursors in self.worker_state.load_cursors().items():
            self._cursors[target].update(cursors)
        self.set_e2ee_device(e2ee_device_id)

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def event(self, function: Handler) -> Handler:
        self._handlers[event_name(function.__name__)].append(function)
        return function

    def listen(self, name: str | None = None) -> Callable[[Handler], Handler]:
        def decorator(function: Handler) -> Handler:
            self._handlers[event_name(name or function.__name__)].append(function)
            return function

        return decorator

    def remove_listener(self, function: Handler, name: str | None = None) -> None:
        listeners = self._handlers.get(event_name(name or function.__name__), [])
        if function in listeners:
            listeners.remove(function)

    def set_interaction_e2ee_context(self, context: InteractionE2EEContext) -> None:
        """Register current verified MLS state for message and interaction dispatch."""

        previous = self._interaction_e2ee_contexts.get(context.channel_ref)
        if previous is not None and previous is not context:
            previous.invalidate()
        self._interaction_e2ee_contexts[context.channel_ref] = context

    def set_message_e2ee_context(self, context: InteractionE2EEContext) -> None:
        """Register the same channel MLS state for automatic rich messages."""

        self.set_interaction_e2ee_context(context)

    def remove_interaction_e2ee_context(self, channel: EntityRef) -> None:
        context = self._interaction_e2ee_contexts.pop(channel, None)
        if context is not None:
            context.invalidate()

    def set_voice_e2ee_context(self, context: VoiceE2EEContext) -> None:
        """Register verified MLS state for an authority-initiated voice move."""

        previous = self._voice_e2ee_contexts.get(context.channel_ref)
        if previous is not None and previous is not context:
            previous.invalidate()
        self._voice_e2ee_contexts[context.channel_ref] = context

    def remove_voice_e2ee_context(self, channel: EntityRef) -> None:
        context = self._voice_e2ee_contexts.pop(channel, None)
        if context is not None:
            context.invalidate()

    def _register_voice_client(self, voice: VoiceClient) -> None:
        self._voice_clients[voice.grant.connection_id] = voice
        if voice.e2ee_context is not None:
            self.set_voice_e2ee_context(voice.e2ee_context)

    def _forget_voice_client(self, voice: VoiceClient) -> None:
        if self._voice_clients.get(voice.grant.connection_id) is voice:
            self._voice_clients.pop(voice.grant.connection_id, None)

    def remove_message_e2ee_context(self, channel: EntityRef) -> None:
        self.remove_interaction_e2ee_context(channel)

    @property
    def e2ee_device_id(self) -> str | None:
        """Return the worker device sent on encrypted REST reads and writes."""

        return self._e2ee_device_id

    def set_e2ee_device(self, device: BotE2EEDevice | str | None) -> None:
        """Select the exact registered worker device for encrypted REST calls."""

        protocol_id = (
            device.protocol_id if isinstance(device, BotE2EEDevice) else device
        )
        if (
            protocol_id is not None
            and re.fullmatch(r"kbe_[A-Za-z0-9_-]{43}", protocol_id) is None
        ):
            raise ValueError("bot E2EE device ID is invalid")
        if (
            isinstance(device, BotE2EEDevice)
            and device.worker_id != self.worker_state.worker_id
        ):
            raise ValueError("bot E2EE device belongs to a different worker")
        self._e2ee_device_id = protocol_id

    def _e2ee_device_headers(self) -> dict[str, str]:
        return (
            {"X-Kaede-E2EE-Device": self._e2ee_device_id}
            if self._e2ee_device_id is not None
            else {}
        )

    def add_view(
        self,
        view: View,
        *,
        message_id: EntityRef | None = None,
        response_id: int | EntityRef | None = None,
        target: str | None = None,
        timeout_editor: Callable[[View], Awaitable[None]] | None = None,
    ) -> None:
        """Register a component view for dispatch and persistent restart recovery."""

        if not isinstance(view, View):
            raise TypeError("view must be a View")
        if not view.rows:
            raise ValueError("a registered view requires at least one component row")
        if message_id is not None and response_id is not None:
            raise ValueError(
                "a view may target a message or an ephemeral response, not both"
            )
        if message_id is not None:
            previous = self._views.get(message_id)
            self._views[message_id] = view
            if previous is not None and previous is not view:
                self._stop_view_if_unregistered(previous)
        if response_id is not None:
            response_ref = self._interaction_authority_ref(response_id, target=target)
            previous = self._response_views.get(response_ref)
            self._response_views[response_ref] = view
            if previous is not None and previous is not view:
                self._stop_view_if_unregistered(previous)
        if message_id is None and response_id is None and not view.is_persistent:
            raise ValueError("a message-less registered view must be persistent")
        if view.is_persistent:
            for custom_id in view.custom_ids:
                previous = self._persistent_views.get(custom_id)
                self._persistent_views[custom_id] = view
                if previous is not None and previous is not view:
                    self._stop_view_if_unregistered(previous)
        origin = None
        if target is not None:
            origin = self._target(target)
        elif len(self._targets) == 1:
            origin = next(iter(self._targets))

        async def timeout_error(error: Exception) -> None:
            if origin is not None:
                await self._report_handler_error("VIEW_TIMEOUT", origin, error)

        async def timeout_action() -> None:
            if timeout_editor is not None:
                await timeout_editor(view)

        view._start_listening(
            lambda: self._remove_view_instance(view),
            timeout_error,
            timeout_action if timeout_editor is not None else None,
        )

    def _view_timeout_editor(
        self,
        path: str,
        *,
        target: str,
        view_version: int | None,
        installation_id: int | None = None,
        channel_ref: EntityRef | None = None,
        dm_capability_id: str | None = None,
    ) -> Callable[[View], Awaitable[None]]:
        """Build the authoritative edit used by ``disable_on_timeout``."""

        async def edit(timed_out_view: View) -> None:
            body: dict[str, Any] = {
                "components": timed_out_view.to_components(),
            }
            if view_version is not None and view_version > 0:
                body["view_version"] = view_version
            await self.request(
                "PATCH",
                path,
                target=target,
                json=body,
                headers=(
                    await self._runtime_grant_headers(
                        channel_ref,
                        installation_id=installation_id,
                        dm_capability_id=dm_capability_id,
                    )
                    if channel_ref is not None
                    else _installation_headers(installation_id)
                ),
            )

        return edit

    def _view_is_registered(self, view: View) -> bool:
        return (
            any(item is view for item in self._views.values())
            or any(item is view for item in self._response_views.values())
            or any(item is view for item in self._persistent_views.values())
        )

    def _stop_view_if_unregistered(self, view: View) -> None:
        if not self._view_is_registered(view):
            view.stop()

    def _remove_view_instance(self, view: View) -> None:
        for message_id, registered in tuple(self._views.items()):
            if registered is view:
                self._views.pop(message_id, None)
        for response_id, registered in tuple(self._response_views.items()):
            if registered is view:
                self._response_views.pop(response_id, None)
        for custom_id, registered in tuple(self._persistent_views.items()):
            if registered is view:
                self._persistent_views.pop(custom_id, None)

    def remove_view(self, message_id: EntityRef) -> None:
        view = self._views.pop(message_id, None)
        if view is not None:
            self._stop_view_if_unregistered(view)

    def remove_response_view(
        self,
        response_id: int | EntityRef,
        *,
        target: str | None = None,
    ) -> None:
        response_ref = self._interaction_authority_ref(response_id, target=target)
        view = self._response_views.pop(response_ref, None)
        if view is not None:
            self._stop_view_if_unregistered(view)

    async def wait_for(
        self,
        name: str,
        *,
        check: Check | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - public discord.py-compatible API
    ) -> object:
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        key = event_name(name)
        waiter = (future, check)
        self._waiters[key].append(waiter)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            if waiter in self._waiters.get(key, []):
                self._waiters[key].remove(waiter)

    def command(
        self,
        *,
        name: str | None = None,
        description: str = "",
        type: str = "chat_input",
        name_localizations: dict[str, str] | None = None,
        description_localizations: dict[str, str] | None = None,
        default_member_permissions: list[str] | Literal["0"] | None = None,
        nsfw: bool = False,
        contexts: list[str] | None = None,
        integration_types: list[str] | None = None,
        options: list[dict[str, Any]] | None = None,
    ) -> Callable[[Handler], Handler]:
        def decorator(function: Handler) -> Handler:
            command_name = name or function.__name__.lower()
            self._commands.append(
                {
                    "name": command_name,
                    "type": type,
                    "description": description,
                    "name_localizations": name_localizations or {},
                    "description_localizations": description_localizations or {},
                    "default_member_permissions": (
                        []
                        if default_member_permissions is None
                        else default_member_permissions
                    ),
                    "nsfw": nsfw,
                    "contexts": (
                        ["guild", "bot_dm", "private_channel"]
                        if contexts is None
                        else contexts
                    ),
                    "integration_types": (
                        ["guild_install"]
                        if integration_types is None
                        else integration_types
                    ),
                    "options": [] if options is None else options,
                }
            )
            self._handlers[f"COMMAND:{command_name}"].append(function)
            return function

        return decorator

    async def sync_commands(
        self,
        *,
        application_home: str,
        control_token: str,
        guild: EntityRef | None = None,
    ) -> None:
        origin = canonical_application_home(
            application_home, self.worker_state.application_ref
        )
        commands = self._commands
        suffix = "commands"
        if guild is not None:
            suffix = f"guilds/{guild}/commands"
            commands = [
                command
                | {
                    "contexts": ["guild"],
                    "integration_types": ["guild_install"],
                }
                for command in commands
            ]
        async with httpx.AsyncClient(
            base_url=origin,
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            response = await http.put(
                f"/api/v1/bot-control/applications/{self.worker_state.application_ref}/{suffix}",
                headers={"Authorization": f"BotControl {control_token}"},
                json={"commands": commands},
            )
            response.raise_for_status()

    async def add_target(self, base_url: str) -> str:
        origin = canonical_target_origin(base_url)
        if origin not in self._targets:
            self._targets[origin] = httpx.AsyncClient(base_url=origin, timeout=30)
            await self._token(origin, force=True)
        return origin

    def _worker_assertion(
        self,
        origin: str,
        path: str,
        *,
        dm_capability: _DMCapabilityContext | None = None,
    ) -> dict[str, object]:
        now = int(time.time())
        expiry = now + 60
        nonce = secrets.token_urlsafe(24)
        audience = f"{origin}{path}"
        if dm_capability is None:
            assertion = (
                f"kaede-worker-assertion-v1\n{self.worker_state.application_ref}\n"
                f"{self.worker_state.worker_id}\n{audience}\n{now}\n{expiry}\n{nonce}"
            ).encode()
        else:
            assertion = (
                f"kaede-worker-assertion-v2\n{self.worker_state.application_ref}\n"
                f"{self.worker_state.worker_id}\n{audience}\n{now}\n{expiry}\n{nonce}\n"
                f"{dm_capability.grant_id}\n{dm_capability.revision}"
            ).encode()
        payload: dict[str, object] = {
            "application_ref": str(self.worker_state.application_ref),
            "worker_id": self.worker_state.worker_id,
            "audience": audience,
            "issued_at": now,
            "expires_at": expiry,
            "nonce": nonce,
            "signature": self._sign(assertion),
        }
        if dm_capability is not None:
            payload.update(
                {
                    "dm_capability_grant_id": dm_capability.grant_id,
                    "dm_capability_revision": dm_capability.revision,
                }
            )
        return payload

    async def discover_targets(
        self,
        *,
        application_home: str | None = None,
    ) -> tuple[list[str], int]:
        """Read roster-free, target-authority-signed installation locations."""

        home = canonical_application_home(
            application_home or f"https://{self.worker_state.application_ref.domain}",
            self.worker_state.application_ref,
        )
        self._application_home = home
        path = "/api/v1/bot-workers/targets"
        async with httpx.AsyncClient(
            base_url=home,
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            response = await http.post(path, json=self._worker_assertion(home, path))
        await self._raise(response)
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or payload.get("application_ref") != str(self.worker_state.application_ref)
            or not isinstance(payload.get("targets"), list)
        ):
            raise ApiError(
                502, "BOT_TARGET_DISCOVERY_INVALID", "Invalid target discovery response"
            )
        origins: list[str] = []
        raw_targets = payload["targets"]
        if len(raw_targets) > 100:
            raise ApiError(
                502, "BOT_TARGET_DISCOVERY_INVALID", "Too many discovered targets"
            )
        for target in raw_targets:
            if not isinstance(target, dict):
                raise ApiError(
                    502,
                    "BOT_TARGET_DISCOVERY_INVALID",
                    "Invalid target discovery response",
                )
            domain = target.get("domain")
            raw_origin = target.get("origin")
            generation = target.get("generation")
            install_types = target.get("install_types")
            if (
                not isinstance(domain, str)
                or not isinstance(raw_origin, str)
                or not isinstance(generation, str)
                or not generation.isascii()
                or not generation.isdecimal()
                or int(generation) < 1
                or not isinstance(install_types, list)
                or not install_types
                or len(install_types) > 2
                or not set(install_types) <= {"guild_install", "user_install"}
            ):
                raise ApiError(
                    502,
                    "BOT_TARGET_DISCOVERY_INVALID",
                    "Invalid target discovery response",
                )
            origin = canonical_target_origin(raw_origin)
            if urlsplit(origin).hostname != domain:
                raise ApiError(
                    502,
                    "BOT_TARGET_DISCOVERY_INVALID",
                    "Target origin does not match its signed authority",
                )
            origins.append(origin)
        if len(origins) != len(set(origins)):
            raise ApiError(
                502, "BOT_TARGET_DISCOVERY_INVALID", "Duplicate discovered target"
            )
        poll_after = payload.get("poll_after_seconds", 30)
        if not isinstance(poll_after, int) or not 5 <= poll_after <= 300:
            raise ApiError(
                502,
                "BOT_TARGET_DISCOVERY_INVALID",
                "Invalid target discovery interval",
            )
        return origins, poll_after

    def _target(self, target: str | None) -> str:
        if target is None:
            if len(self._targets) != 1:
                raise ValueError(
                    "target is required when the client has zero or multiple instances"
                )
            return next(iter(self._targets))
        return canonical_target_origin(target)

    def _application_home_target(self, target: str | None = None) -> str:
        return canonical_application_home(
            self._application_home if target is None else target,
            self.worker_state.application_ref,
        )

    def _authority_target(self, resource: EntityRef, target: str | None = None) -> str:
        """Resolve a qualified resource directly to its authoritative instance."""

        explicit = canonical_target_origin(target) if target is not None else None
        if explicit is not None and urlsplit(explicit).hostname == resource.domain:
            return explicit
        configured = [
            origin
            for origin in self._targets
            if urlsplit(origin).hostname == resource.domain
        ]
        if len(configured) == 1:
            return configured[0]
        if len(configured) > 1:
            raise ValueError(
                f"multiple bot targets are configured for authority {resource.domain}"
            )
        return canonical_target_origin(f"https://{resource.domain}")

    @staticmethod
    def _require_same_authority(
        anchor: EntityRef,
        *resources: EntityRef,
        label: str = "resource",
    ) -> None:
        """Reject nested resources that do not belong to an endpoint's authority."""

        if any(
            not isinstance(resource, EntityRef) or resource.domain != anchor.domain
            for resource in resources
        ):
            raise ValueError(f"{label} must use the {anchor.domain} authority")

    def _webhook_token_context(
        self,
        webhook: int | EntityRef,
        target: str | None,
        *resources: EntityRef,
    ) -> tuple[int, str, EntityRef]:
        """Bind a token-only webhook request to one qualified authority."""

        if isinstance(webhook, EntityRef):
            self._require_same_authority(
                webhook,
                *resources,
                label="webhook resource",
            )
            origin = self._authority_target(webhook, target)
            return webhook.id, origin, webhook
        if type(webhook) is not int:
            raise TypeError("webhook must be an integer ID or EntityRef")
        if resources:
            self._require_same_authority(
                resources[0],
                *resources[1:],
                label="webhook resource",
            )
            origin = self._authority_target(resources[0], target)
        else:
            origin = self._target(target)
        return webhook, origin, _webhook_token_ref(origin, webhook)

    def _resource_target(
        self, resource: EntityRef | None, target: str | None = None
    ) -> str:
        """Resolve an optional qualified resource, otherwise require a target."""

        if resource is None:
            return self._target(target)
        return self._authority_target(resource, target)

    @staticmethod
    def _authority_ref_from_path(path: str) -> EntityRef | None:
        prefix = "/api/v1/bots/"
        if not path.startswith(prefix):
            return None
        parts = path[len(prefix) :].split("/", 2)
        if len(parts) < 2 or parts[0] not in {
            "attachments",
            "channels",
            "guilds",
            "stage-instances",
            "users",
        }:
            return None
        try:
            return EntityRef.parse(parts[1])
        except ValueError:
            return None

    @staticmethod
    def _is_application_home_path(path: str) -> bool:
        return (
            path.startswith("/api/v1/bots/applications/@me/")
            or path.startswith("/api/v1/bots/dm-capabilities")
            or path.startswith("/api/v1/bots/e2ee/devices")
            or path == "/api/v1/bots/dms"
            or path == "/api/v1/bots/@me"
        )

    def _request_target(self, path: str, target: str | None = None) -> str:
        if self._is_application_home_path(path):
            return self._application_home_target(target)
        resource = self._authority_ref_from_path(path)
        if resource is not None:
            return self._authority_target(resource, target)
        return self._target(target)

    def _interaction_authority_ref(
        self,
        value: int | EntityRef,
        *,
        target: str | None,
    ) -> EntityRef:
        """Qualify an authority-local interaction resource without guessing."""

        if isinstance(value, EntityRef):
            if target is not None:
                origin = canonical_target_origin(target)
                if urlsplit(origin).hostname != value.domain:
                    raise ValueError(
                        "interaction resource authority conflicts with target"
                    )
            return value
        origin = self._target(target)
        authority = urlsplit(origin).hostname
        if authority is None:
            raise ValueError("interaction target has no canonical authority")
        return EntityRef(value, authority)

    def _interaction_event_channel(
        self,
        interaction_ref: EntityRef,
    ) -> EntityRef | None:
        lifecycle = self._interaction_lifecycle_grants.get(interaction_ref)
        if lifecycle is None or lifecycle.expires_at <= time.time():
            return None
        return lifecycle.channel_ref

    def _bind_interaction_response_dict(
        self,
        payload: object,
        interaction_id: int,
        *,
        target: str,
        sequence_kind: Literal["original", "followup", "original_or_source"],
        expected_response_id: int | None = None,
        expected_callback_type: int | None = None,
        slot_kind: Literal["original", "followup"] | None = None,
        created: bool = False,
        mutated: bool = False,
    ) -> dict[str, Any]:
        """Validate a private/non-materialized interaction response projection.

        Modern authorities emit the complete response identity and monotonic
        sequence/revision fence. Older authorities emitted only the response ID
        plus message data; that reference-only shape remains readable, but any
        modern identity field opts the whole payload into the strict contract.
        """

        if not isinstance(payload, dict):
            raise ValueError("interaction response payload is invalid")
        authority = _target_authority(target)
        interaction_ref = self._interaction_authority_ref(
            interaction_id,
            target=target,
        )
        try:
            response_ref = EntityRef.from_wire(payload["id"], authority)
        except (KeyError, ValueError) as exc:
            raise ValueError("interaction response ID is invalid") from exc
        if expected_response_id is not None and response_ref.id != expected_response_id:
            raise ValueError("interaction response changed the requested response")

        raw_interaction_id = payload.get("interaction_id")
        if raw_interaction_id is not None:
            try:
                asserted_interaction = EntityRef.from_wire(
                    raw_interaction_id,
                    authority,
                )
            except ValueError as exc:
                raise ValueError("interaction response parent is invalid") from exc
            if asserted_interaction != interaction_ref:
                raise ValueError("interaction response changed its interaction")
        raw_interaction_ref = payload.get("interaction_ref")
        if raw_interaction_ref is not None:
            try:
                asserted_interaction_ref = EntityRef.parse(raw_interaction_ref)
            except ValueError as exc:
                raise ValueError("interaction response parent is invalid") from exc
            if asserted_interaction_ref != interaction_ref:
                raise ValueError("interaction response changed its interaction")
        raw_interaction_domain = payload.get("interaction_domain")
        if raw_interaction_domain is not None and (
            raw_interaction_id is None
            or EntityRef.from_wire(raw_interaction_id, raw_interaction_domain)
            != interaction_ref
        ):
            raise ValueError("interaction response parent aliases conflict")

        asserted_application = _optional_asserted_ref(
            payload,
            ref_key="application_ref",
            id_key="application_id",
            domain_key="application_domain",
            label="interaction response application",
        )
        if (
            asserted_application is not None
            and asserted_application != self.worker_state.application_ref
        ):
            raise ValueError("interaction response changed its application")
        asserted_channel = _optional_asserted_ref(
            payload,
            ref_key="channel_ref",
            id_key="channel_id",
            domain_key="channel_domain",
            label="interaction response channel",
        )
        event_channel = self._interaction_event_channel(interaction_ref)
        if asserted_channel is not None and (
            asserted_channel.domain != authority
            or (event_channel is not None and asserted_channel != event_channel)
        ):
            raise ValueError("interaction response changed its channel authority")

        modern_fields = {"response_ref", "sequence", "revision"}
        if not modern_fields & payload.keys():
            raw_response_id = payload.get("response_id")
            if raw_response_id is not None:
                try:
                    response_id_ref = EntityRef.from_wire(raw_response_id, authority)
                except ValueError as exc:
                    raise ValueError(
                        "interaction response reference is invalid"
                    ) from exc
                if response_id_ref != response_ref:
                    raise ValueError("interaction response reference aliases conflict")
            if "ephemeral" in payload:
                strict_payload_bool(payload, "ephemeral", default=False)
            raw_response_type = payload.get("response_type")
            if raw_response_type is not None and (
                type(raw_response_type) is not int
                or raw_response_type not in {1, 4, 5, 6, 7, 8, 9, 10}
            ):
                raise ValueError("interaction response type is invalid")
            if (
                expected_callback_type is not None
                and raw_response_type is not None
                and raw_response_type
                not in (
                    {4, 7} if expected_callback_type == 7 else {expected_callback_type}
                )
            ):
                raise ValueError("interaction callback message type is invalid")
            return payload

        required = {
            "id",
            "interaction_id",
            "response_id",
            "response_ref",
            "sequence",
            "revision",
            "ephemeral",
            "response_type",
        }
        if not required <= payload.keys():
            raise ValueError("interaction response identity is incomplete")
        try:
            asserted_response_ref = EntityRef.parse(payload["response_ref"])
            response_id_ref = EntityRef.from_wire(payload["response_id"], authority)
        except ValueError as exc:
            raise ValueError("interaction response reference is invalid") from exc
        if asserted_response_ref != response_ref or response_id_ref != response_ref:
            raise ValueError("interaction response reference aliases conflict")
        raw_sequence = payload["sequence"]
        raw_revision = payload["revision"]
        raw_response_type = payload["response_type"]
        if type(raw_sequence) is not int or not 0 <= raw_sequence <= (1 << 63) - 1:
            raise ValueError("interaction response sequence is invalid")
        try:
            revision = EntityRef.from_wire(raw_revision, authority).id
        except ValueError as exc:
            raise ValueError("interaction response revision is invalid") from exc
        if type(raw_response_type) is not int or raw_response_type not in {
            1,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
        }:
            raise ValueError("interaction response type is invalid")
        ephemeral = strict_payload_bool(payload, "ephemeral", default=False)
        if ephemeral and asserted_channel is None:
            raise ValueError("ephemeral interaction response has no channel authority")
        if sequence_kind == "original" and raw_sequence != 0:
            raise ValueError("original interaction response sequence is invalid")
        if sequence_kind == "followup" and raw_sequence <= 0:
            raise ValueError("follow-up interaction response sequence is invalid")
        if expected_callback_type is not None and raw_response_type not in (
            {4, 7} if expected_callback_type == 7 else {expected_callback_type}
        ):
            raise ValueError("interaction callback message type is invalid")
        if created and revision != 1:
            raise ValueError("new interaction response revision is invalid")

        identity = _InteractionResponseIdentity(
            interaction_ref=interaction_ref,
            response_ref=response_ref,
            application_ref=self.worker_state.application_ref,
            channel_ref=asserted_channel or event_channel,
            sequence=raw_sequence,
            revision=revision,
            response_type=raw_response_type,
            ephemeral=ephemeral,
        )
        if slot_kind is not None:
            slot_response_id = response_ref.id if slot_kind == "followup" else None
            slot = (interaction_ref, slot_kind, slot_response_id)
            previous = self._interaction_response_identities.get(slot)
            if sequence_kind == "original_or_source" and (
                previous is None or previous.response_ref != response_ref
            ):
                # Type-7 updates may project the exact private source response
                # instead of the current interaction's newly-created response.
                # Validate its full fence above, but do not relabel that source
                # as this interaction's original lifecycle identity.
                return payload
            if previous is not None:
                stable_previous = (
                    previous.interaction_ref,
                    previous.response_ref,
                    previous.application_ref,
                    previous.sequence,
                    previous.response_type,
                    previous.ephemeral,
                )
                stable_current = (
                    identity.interaction_ref,
                    identity.response_ref,
                    identity.application_ref,
                    identity.sequence,
                    identity.response_type,
                    identity.ephemeral,
                )
                if stable_current != stable_previous or (
                    previous.channel_ref is not None
                    and identity.channel_ref is not None
                    and previous.channel_ref != identity.channel_ref
                ):
                    raise ValueError("interaction response identity changed")
                if identity.revision < previous.revision or (
                    mutated and identity.revision <= previous.revision
                ):
                    raise ValueError("interaction response revision did not advance")
                if identity.channel_ref is None:
                    identity = _InteractionResponseIdentity(
                        interaction_ref=identity.interaction_ref,
                        response_ref=identity.response_ref,
                        application_ref=identity.application_ref,
                        channel_ref=previous.channel_ref,
                        sequence=identity.sequence,
                        revision=identity.revision,
                        response_type=identity.response_type,
                        ephemeral=identity.ephemeral,
                    )
            if len(self._interaction_response_identities) >= 4096 and slot not in (
                self._interaction_response_identities
            ):
                self._interaction_response_identities.pop(
                    next(iter(self._interaction_response_identities))
                )
            self._interaction_response_identities[slot] = identity
        return payload

    @staticmethod
    def _interaction_id_from_path(path: str) -> int | None:
        matched = re.match(r"^/api/v1/bots/interactions/([1-9][0-9]*)(?:/|$)", path)
        if matched is None:
            return None
        parsed = int(matched.group(1))
        return parsed if parsed <= (1 << 63) - 1 else None

    def _remember_interaction_lifecycle_grant(
        self,
        data: dict[str, Any],
        *,
        target: str,
    ) -> None:
        interaction_id = self._interaction_id_from_path(
            f"/api/v1/bots/interactions/{data.get('id')}"
        )
        token = data.get("token")
        raw_expiry = data.get("expires_at")
        if (
            interaction_id is None
            or not isinstance(token, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None
            or not isinstance(raw_expiry, str)
        ):
            raise ValueError("interaction event has an invalid lifecycle grant")
        raw_interaction_ref = data.get("interaction_ref")
        raw_channel_ref = data.get("channel_ref")
        try:
            interaction_ref = EntityRef.parse(raw_interaction_ref)
            channel_ref = EntityRef.parse(raw_channel_ref)
        except ValueError as exc:
            raise ValueError(
                "interaction event has an invalid authority reference"
            ) from exc
        authority = urlsplit(canonical_target_origin(target)).hostname
        if (
            not isinstance(raw_interaction_ref, str)
            or not isinstance(raw_channel_ref, str)
            or interaction_ref.id != interaction_id
            or interaction_ref.domain != channel_ref.domain
            or interaction_ref.domain != authority
        ):
            raise ValueError("interaction event authority conflicts with its target")
        try:
            expiry = datetime.fromisoformat(raw_expiry)
        except ValueError as exc:
            raise ValueError("interaction event has an invalid expiry") from exc
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise ValueError("interaction event lifecycle grant is expired")
        headers = {"X-Kaede-Interaction-Token": token}
        integration_type = data.get("integration_type")
        raw_revision = data.get("installation_revision")
        if (
            integration_type not in {"guild_install", "user_install", "dm_capability"}
            or not isinstance(raw_revision, str)
            or not raw_revision.isascii()
            or not raw_revision.isdecimal()
            or raw_revision.startswith("0")
        ):
            raise ValueError("interaction event has an invalid installation revision")
        installation_revision = int(raw_revision)
        standard_installation = data.get("installation_id")
        user_installation = data.get("user_installation_id")
        if integration_type == "dm_capability":
            grant_id = data.get("bot_dm_capability_id")
            revision = data.get("bot_dm_capability_revision")
            installation_ref = data.get("installation_ref")
            installation_type = data.get("installation_type")
            if (
                standard_installation is not None
                or user_installation is not None
                or not isinstance(grant_id, str)
                or re.fullmatch(r"kbdg_[A-Za-z0-9_-]{43}", grant_id) is None
                or not isinstance(revision, str)
                or not revision.isascii()
                or not revision.isdecimal()
                or revision.startswith("0")
                or revision != raw_revision
                or not isinstance(installation_ref, str)
                or installation_type not in {"guild", "user"}
            ):
                raise ValueError(
                    "interaction event has an invalid DM capability lineage"
                )
            parsed_installation = EntityRef.parse(installation_ref)
            headers.update(
                _dm_runtime_headers(
                    parsed_installation,
                    cast(Literal["guild", "user"], installation_type),
                )
            )
            headers["X-Kaede-Bot-DM-Capability"] = grant_id
        else:
            if any(
                data.get(field) is not None
                for field in (
                    "bot_dm_capability_id",
                    "bot_dm_capability_revision",
                    "installation_ref",
                    "installation_type",
                )
            ):
                raise ValueError(
                    "interaction event combines incompatible lifecycle grants"
                )
            selected = (
                standard_installation
                if integration_type == "guild_install"
                else user_installation
            )
            other = (
                user_installation
                if integration_type == "guild_install"
                else standard_installation
            )
            if (
                other is not None
                or not isinstance(selected, str)
                or not selected.isascii()
                or not selected.isdecimal()
                or selected.startswith("0")
            ):
                raise ValueError(
                    "interaction event has an invalid installation lineage"
                )
            if integration_type == "guild_install":
                headers["X-Kaede-Bot-Installation"] = selected
        now = time.time()
        for expired_id, grant in tuple(self._interaction_lifecycle_grants.items()):
            if grant.expires_at <= now:
                self._interaction_lifecycle_grants.pop(expired_id, None)
        if len(self._interaction_lifecycle_grants) >= 4096:
            oldest = min(
                self._interaction_lifecycle_grants,
                key=lambda item: self._interaction_lifecycle_grants[item].expires_at,
            )
            self._interaction_lifecycle_grants.pop(oldest, None)
        self._interaction_lifecycle_grants[interaction_ref] = (
            _InteractionLifecycleGrant(
                headers=headers,
                expires_at=expiry.timestamp(),
                installation_revision=installation_revision,
                channel_ref=channel_ref,
            )
        )

    def _interaction_lifecycle_headers_for_path(
        self,
        path: str,
        *,
        origin: str,
    ) -> dict[str, str]:
        interaction_id = self._interaction_id_from_path(path)
        if interaction_id is None:
            return {}
        authority = urlsplit(canonical_target_origin(origin)).hostname
        if authority is None:
            raise ValueError("interaction target has no canonical authority")
        interaction_ref = EntityRef(interaction_id, authority)
        stored = self._interaction_lifecycle_grants.get(interaction_ref)
        if stored is None:
            raise ValueError("interaction lifecycle request has no trusted event token")
        if stored.expires_at <= time.time():
            self._interaction_lifecycle_grants.pop(interaction_ref, None)
            raise ValueError("interaction lifecycle token has expired")
        return dict(stored.headers)

    async def _dm_capability_headers_for_path(self, path: str) -> dict[str, str]:
        channel_ref = self._authority_ref_from_path(path)
        if channel_ref is None or "/channels/" not in path:
            return {}
        grant_id = self._dm_default_capabilities.get(channel_ref)
        if grant_id is None:
            return {}
        key = (channel_ref, grant_id)
        context = self._dm_capabilities.get(key)
        if context is None:
            return {}
        if context.expires_at <= time.time() + DM_CAPABILITY_REFRESH_WINDOW_SECONDS:
            context = await self._refresh_dm_capability(key)
        return _dm_capability_headers(context)

    async def _dm_capability_headers_for_grant(
        self,
        channel_ref: EntityRef,
        grant_id: str | None,
    ) -> dict[str, str]:
        context = await self._dm_capability_context_for_grant(channel_ref, grant_id)
        if context is None:
            return {}
        return _dm_capability_headers(context)

    async def _dm_capability_context_for_grant(
        self,
        channel_ref: EntityRef,
        grant_id: str | None,
    ) -> _DMCapabilityContext | None:
        if grant_id is None:
            grant_id = self._dm_default_capabilities.get(channel_ref)
            if grant_id is None:
                return None
        key = (channel_ref, grant_id)
        context = self._dm_capabilities.get(key)
        if context is None:
            # Threads inherit the parent DM capability, but their qualified
            # channel ref differs from the conversation row used to refresh
            # the lease. Resolve only an exact grant on the same authority;
            # the server still verifies parent/conversation lineage.
            matches = [
                (candidate_key, candidate)
                for candidate_key, candidate in self._dm_capabilities.items()
                if candidate.grant_id == grant_id
                and urlsplit(candidate.target).hostname == channel_ref.domain
            ]
            if len(matches) == 1:
                key, context = matches[0]
        if context is None:
            raise ApiError(
                401,
                "BOT_DM_GRANT_REQUIRED",
                "The call's exact DM capability is no longer available",
            )
        if context.expires_at <= time.time() + DM_CAPABILITY_REFRESH_WINDOW_SECONDS:
            context = await self._refresh_dm_capability(key)
        return context

    async def _runtime_grant_headers(
        self,
        channel_ref: EntityRef,
        *,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> dict[str, str]:
        """Resolve one exact DM grant or the legacy guild installation header."""

        if installation_id is not None and dm_capability_id is not None:
            raise ValueError(
                "installation and DM capability grants are mutually exclusive"
            )
        if installation_id is not None:
            grant_headers = dict(_installation_headers(installation_id) or {})
        else:
            grant_headers = await self._dm_capability_headers_for_grant(
                channel_ref,
                dm_capability_id,
            )
        return grant_headers | self._e2ee_device_headers()

    def _default_dm_capability_id(self, channel_ref: EntityRef) -> str | None:
        return self._dm_default_capabilities.get(channel_ref)

    async def _drop_dm_capability(self, key: _DMCapabilityKey) -> None:
        channel_ref, grant_id = key
        context = self._dm_capabilities.pop(key, None)
        self._dm_capability_locks.pop(key, None)
        task = self._dm_refresh_tasks.pop(key, None)
        current_task = asyncio.current_task()
        if task is not None and task is not current_task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        gateway_task = self._dm_gateway_tasks.pop(key, None)
        cancel_gateway_current = gateway_task is current_task
        if gateway_task is not None and gateway_task is not current_task:
            gateway_task.cancel()
            with suppress(asyncio.CancelledError):
                await gateway_task
        if context is None:
            return
        for token_key in tuple(self._tokens):
            if token_key[0] == context.target and token_key[1] == grant_id:
                self._tokens.pop(token_key, None)
        if self._dm_default_capabilities.get(channel_ref) == grant_id:
            replacements = sorted(
                candidate_grant
                for candidate_channel, candidate_grant in self._dm_capabilities
                if candidate_channel == channel_ref
            )
            if replacements:
                self._dm_default_capabilities[channel_ref] = replacements[0]
            else:
                self._dm_default_capabilities.pop(channel_ref, None)
        capability_keys = self._capability_targets.get(context.target)
        if capability_keys is not None:
            capability_keys.discard(key)
            if not capability_keys:
                self._capability_targets.pop(context.target, None)
                await self._remove_discovered_target(context.target)
        if cancel_gateway_current and current_task is not None:
            current_task.cancel()

    async def _register_dm_capability(
        self,
        channel: Channel,
        context: _DMCapabilityContext,
    ) -> None:
        key = (channel.ref, context.grant_id)
        previous = self._dm_capabilities.get(key)
        if previous is not None and previous.target != context.target:
            raise ApiError(
                502,
                "BOT_DM_GRANT_INVALID",
                "The refreshed DM capability changed conversation authority",
            )
        self._dm_capabilities[key] = context
        self._dm_default_capabilities[channel.ref] = context.grant_id
        self._capability_targets[context.target].add(key)
        if previous is not None and previous.revision != context.revision:
            for token_key in tuple(self._tokens):
                if token_key[0] == context.target and token_key[1] == context.grant_id:
                    self._tokens.pop(token_key, None)
            gateway_task = self._dm_gateway_tasks.get(key)
            if gateway_task is not None and gateway_task is not asyncio.current_task():
                self._dm_gateway_tasks.pop(key, None)
                gateway_task.cancel()
        if not self._started:
            return
        if context.target not in self._targets:
            self._targets[context.target] = httpx.AsyncClient(
                base_url=context.target,
                timeout=30,
            )
        self._ensure_dm_gateway_task(key)
        task = self._dm_refresh_tasks.get(key)
        if task is None or task.done():
            self._dm_refresh_tasks[key] = asyncio.create_task(
                self._dm_capability_refresh_loop(key),
                name=f"kaede-dm-capability:{channel.ref}:{context.grant_id}",
            )

    async def _refresh_dm_capability(
        self,
        key: _DMCapabilityKey,
        *,
        force: bool = False,
    ) -> _DMCapabilityContext:
        channel_ref, grant_id = key
        lock = self._dm_capability_locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = self._dm_capabilities.get(key)
            if current is None:
                raise ApiError(
                    401,
                    "BOT_DM_GRANT_REQUIRED",
                    "The DM installation capability is no longer available",
                )
            if (
                not force
                and current.expires_at
                > time.time() + DM_CAPABILITY_REFRESH_WINDOW_SECONDS
            ):
                return current
            raw = await self.request(
                "POST",
                f"/api/v1/bots/dm-capabilities/{grant_id}/refresh",
                target=self._application_home,
            )
            channel, refreshed = self._parse_dm_capability_response(
                raw,
                origin=self._application_home,
                expected_grant_id=grant_id,
                expected_channel_ref=channel_ref,
                expected_context=current,
            )
            await self._register_dm_capability(channel, refreshed)
            return refreshed

    async def _dm_capability_refresh_loop(self, key: _DMCapabilityKey) -> None:
        channel_ref, _grant_id = key
        while self._started:
            context = self._dm_capabilities.get(key)
            if context is None:
                return
            refresh_at = context.expires_at - DM_CAPABILITY_REFRESH_WINDOW_SECONDS
            await asyncio.sleep(max(0.05, refresh_at - time.time()))
            try:
                await self._refresh_dm_capability(key, force=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                context = self._dm_capabilities.get(key)
                if context is None:
                    return
                if isinstance(exc, ApiError) and _dm_capability_error_is_terminal(exc):
                    await self._drop_dm_capability(key)
                    return
                await self.dispatch(
                    "DM_CAPABILITY_ERROR",
                    {
                        "channel_ref": str(channel_ref),
                        "authority": context.target,
                        "error": str(exc),
                    },
                    target=context.target,
                )
                remaining = context.expires_at - time.time()
                if remaining <= 0:
                    await self._drop_dm_capability(key)
                    return
                await asyncio.sleep(min(5.0, max(0.05, remaining)))

    async def _bootstrap_dm_capabilities(self) -> None:
        """Restore opaque DM leases from application home after a restart."""

        after: str | None = None
        seen: set[str] = set()
        cursors: set[str] = set()
        while True:
            raw_page = await self.request(
                "GET",
                "/api/v1/bots/dm-capabilities",
                target=self._application_home,
                params={
                    "limit": 100,
                    **({"after": after} if after is not None else {}),
                },
            )
            if not isinstance(raw_page, dict) or not isinstance(
                raw_page.get("items"), list
            ):
                raise ApiError(
                    502,
                    "BOT_DM_GRANT_INVALID",
                    "The application home returned an invalid capability list",
                )
            items = raw_page["items"]
            if len(items) > 100:
                raise ApiError(
                    502,
                    "BOT_DM_GRANT_INVALID",
                    "The application home returned too many capabilities",
                )
            page_keys: list[_DMCapabilityKey] = []
            for item in items:
                channel, context = self._parse_dm_capability_response(
                    item,
                    origin=self._application_home,
                    allow_expired=True,
                )
                if context.grant_id in seen:
                    raise ApiError(
                        502,
                        "BOT_DM_GRANT_INVALID",
                        "The application home repeated a DM capability",
                    )
                seen.add(context.grant_id)
                await self._register_dm_capability(channel, context)
                page_keys.append((channel.ref, context.grant_id))
            for key in page_keys:
                try:
                    await self._refresh_dm_capability(key, force=True)
                except ApiError as exc:
                    retained = self._dm_capabilities.get(key)
                    terminal = _dm_capability_error_is_terminal(exc)
                    expired = retained is None or retained.expires_at <= time.time()
                    if terminal or expired:
                        await self._drop_dm_capability(key)
                    if not terminal and expired:
                        raise
            next_after = raw_page.get("next_after")
            if next_after is None:
                return
            if (
                not isinstance(next_after, str)
                or not next_after.startswith("kbdg_")
                or next_after in cursors
            ):
                raise ApiError(
                    502,
                    "BOT_DM_GRANT_INVALID",
                    "The application home returned an invalid capability cursor",
                )
            cursors.add(next_after)
            after = next_after

    async def _clear_dm_capability_state(self) -> None:
        for key in tuple(self._dm_capabilities):
            await self._drop_dm_capability(key)
        self._dm_default_capabilities.clear()
        self._capability_targets.clear()

    async def _reconcile_dm_capabilities_for_target(self, target: str) -> None:
        """Refresh exact leases after a Gateway 4009 authorization change."""

        capability_keys = tuple(
            sorted(
                self._capability_targets.get(target, ()),
                key=lambda item: (str(item[0]), item[1]),
            )
        )
        for key in capability_keys:
            try:
                await self._refresh_dm_capability(key, force=True)
            except ApiError as exc:
                if _dm_capability_error_is_terminal(exc):
                    await self._drop_dm_capability(key)
                    continue
                context = self._dm_capabilities.get(key)
                if context is not None and context.expires_at <= time.time():
                    await self._drop_dm_capability(key)

    def _sign(self, payload: bytes) -> str:
        return _b64(self.worker_state.private_key.sign(payload))

    def _dm_capability_for_headers(
        self,
        origin: str,
        headers: Mapping[str, str],
    ) -> _DMCapabilityContext | None:
        grant_id = headers.get("X-Kaede-Bot-DM-Capability")
        if grant_id is None:
            return None
        matches = [
            context
            for context in self._dm_capabilities.values()
            if context.grant_id == grant_id and context.target == origin
        ]
        if len(matches) != 1:
            raise ApiError(
                401,
                "BOT_DM_GRANT_REQUIRED",
                "The exact DM capability is unavailable for this authority",
            )
        context = matches[0]
        expected = _dm_runtime_headers(
            context.installation_ref,
            context.installation_type,
        )
        if any(headers.get(name) != value for name, value in expected.items()):
            raise ApiError(
                401,
                "BOT_DM_GRANT_INVALID",
                "The DM capability source installation does not match",
            )
        return context

    @staticmethod
    def _token_key(
        origin: str,
        *,
        application_home: bool,
        dm_capability: _DMCapabilityContext | None,
    ) -> _RuntimeTokenKey:
        return (
            origin,
            dm_capability.grant_id if dm_capability is not None else None,
            dm_capability.revision if dm_capability is not None else None,
            application_home,
        )

    async def _token(
        self,
        origin: str,
        *,
        force: bool = False,
        application_home: bool = False,
        dm_capability: _DMCapabilityContext | None = None,
    ) -> str:
        if application_home and dm_capability is not None:
            raise ValueError("application-home tokens cannot carry a DM capability")
        key = self._token_key(
            origin,
            application_home=application_home,
            dm_capability=dm_capability,
        )
        cached = self._tokens.get(key)
        if cached and cached[1] - 30 > time.time() and not force:
            return cached[0]
        path = (
            "/api/v1/bot-workers/home-token"
            if application_home
            else "/api/v1/bots/token"
        )
        response = await self._targets[origin].post(
            path,
            json=self._worker_assertion(
                origin,
                path,
                dm_capability=dm_capability,
            ),
        )
        await self._raise(response)
        data = response.json()
        try:
            access_token = data["access_token"]
            expires_in = int(data["expires_in"])
            if not isinstance(access_token, str) or not access_token or expires_in <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError(
                502,
                "BOT_TOKEN_RESPONSE_INVALID",
                "Target returned an invalid bot token",
            ) from exc
        actor_metadata_fields = {
            "bot_user_ref",
            "worker_generation",
            "runtime_target",
            "runtime_manifest_generation",
            "runtime_revocation_generation",
            "runtime_access_revocation_generation",
        }
        supplied_actor_metadata = actor_metadata_fields & data.keys()
        if supplied_actor_metadata and supplied_actor_metadata != actor_metadata_fields:
            raise ApiError(
                502,
                "BOT_TOKEN_RESPONSE_INVALID",
                "Target returned incomplete bot actor metadata",
            )
        if supplied_actor_metadata and not application_home:
            try:
                bot_user_ref = EntityRef.parse(data["bot_user_ref"])
                runtime_revision = {
                    "worker_generation": str(int(data["worker_generation"])),
                    "runtime_target": str(data["runtime_target"]),
                    "runtime_manifest_generation": str(
                        int(data["runtime_manifest_generation"])
                    ),
                    "runtime_revocation_generation": str(
                        int(data["runtime_revocation_generation"])
                    ),
                    "runtime_access_revocation_generation": str(
                        int(data["runtime_access_revocation_generation"])
                    ),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiError(
                    502,
                    "BOT_TOKEN_RESPONSE_INVALID",
                    "Target returned invalid bot actor metadata",
                ) from exc
            if bot_user_ref.domain != self.worker_state.application_ref.domain:
                raise ApiError(
                    502,
                    "BOT_TOKEN_RESPONSE_INVALID",
                    "Target returned a bot identity from the wrong authority",
                )
            runtime_domain = urlsplit(origin).hostname
            numeric_revisions = [
                value
                for key, value in runtime_revision.items()
                if key != "runtime_target"
            ]
            if (
                runtime_domain is None
                or runtime_revision["runtime_target"] != runtime_domain
                or any(
                    not value.isascii()
                    or not value.isdecimal()
                    or (len(value) > 1 and value.startswith("0"))
                    for value in numeric_revisions
                )
                or runtime_revision["worker_generation"] == "0"
                or runtime_revision["runtime_manifest_generation"] == "0"
                or runtime_revision["runtime_revocation_generation"] == "0"
            ):
                raise ApiError(
                    502,
                    "BOT_TOKEN_RESPONSE_INVALID",
                    "Target returned an invalid runtime revision",
                )
            self._bot_user_refs[origin] = bot_user_ref
            self._actor_runtime_revisions[origin] = runtime_revision
        self._tokens[key] = (
            access_token,
            time.time() + expires_in,
        )
        return access_token

    def _proof_headers(self, method: str, target: str, token: str) -> dict[str, str]:
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        payload = (
            f"kaede-dpop-v1\n{method.upper()}\n{target}\n{timestamp}\n{nonce}\n{digest}"
        ).encode()
        return {
            "Authorization": f"Bot {token}",
            "X-Kaede-Bot-Timestamp": str(timestamp),
            "X-Kaede-Bot-Nonce": nonce,
            "X-Kaede-Bot-Proof": self._sign(payload),
        }

    async def _federated_actor_intent(
        self,
        *,
        action: str,
        audience: str,
        runtime_target: str,
        resources: Mapping[str, str],
    ) -> dict[str, object]:
        """Sign a narrow relayable action without exposing the bot token."""

        if audience not in self._targets:
            await self.add_target(audience)
        if audience not in self._bot_user_refs:
            await self._token(audience)
        if runtime_target not in self._targets:
            await self.add_target(runtime_target)
        if runtime_target not in self._actor_runtime_revisions:
            await self._token(runtime_target)
        actor_ref = self._bot_user_refs.get(audience)
        runtime_revision = self._actor_runtime_revisions.get(runtime_target)
        if actor_ref is None or runtime_revision is None:
            raise ApiError(
                502, "BOT_TOKEN_RESPONSE_INVALID", "Bot identity is unavailable"
            )
        issued_at = int(time.time())
        intent: dict[str, object] = {
            "version": 1,
            "action": action,
            "audience": urlsplit(audience).hostname or audience,
            "application_ref": str(self.worker_state.application_ref),
            "actor_ref": str(actor_ref),
            "worker_id": str(self.worker_state.worker_id),
            **runtime_revision,
            "resources": dict(sorted(resources.items())),
            "issued_at": issued_at,
            "expires_at": issued_at + 120,
            "nonce": secrets.token_urlsafe(24),
        }
        encoded = json.dumps(
            intent, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        intent["signature"] = self._sign(
            b"kaede-federated-actor-intent-v1\n" + encoded.encode("utf-8")
        )
        return intent

    async def _message_expression_actor_intents(
        self,
        channel: EntityRef,
        origin: str,
        body: Mapping[str, object],
        *,
        operation: Literal["message.create", "message.edit", "reaction.add"],
        operation_id: str,
        target_message_ref: EntityRef | None,
        installation_id: int | None,
    ) -> dict[str, dict[str, object]]:
        target_domain = channel.domain or urlsplit(origin).hostname
        if target_domain is None:
            raise ValueError("expression target authority is unavailable")
        projections = _expression_projection(body, default_domain=target_domain)
        if not projections:
            return {}
        channel_model = await self.fetch_channel(
            channel,
            target=origin,
            installation_id=installation_id,
        )
        guild_ref = channel_model.guild_ref
        if guild_ref is None:
            return {}
        intents: dict[str, dict[str, object]] = {}
        for source_authority, (emoji_tokens, sticker_refs) in projections.items():
            authorization_nonce = secrets.token_urlsafe(24)
            source_origin = self._authority_target(EntityRef(1, source_authority))
            intents[source_authority] = await self._federated_actor_intent(
                action="expression.use.authorize",
                audience=source_origin,
                runtime_target=origin,
                resources=_expression_intent_resources(
                    source_authority=source_authority,
                    target_guild_ref=str(guild_ref),
                    target_channel_ref=str(channel),
                    target_message_ref=(
                        str(target_message_ref)
                        if target_message_ref is not None
                        else None
                    ),
                    operation=operation,
                    operation_id=operation_id,
                    emoji_tokens=emoji_tokens,
                    sticker_refs=sticker_refs,
                    authorization_nonce=authorization_nonce,
                ),
            )
        return intents

    async def request(
        self,
        method: str,
        path: str,
        *,
        target: str | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        origin = self._request_target(path, target)
        application_home = self._is_application_home_path(path)
        if origin not in self._targets:
            if application_home:
                self._targets[origin] = httpx.AsyncClient(base_url=origin, timeout=30)
            else:
                await self.add_target(origin)
        signed_target = path
        if params:
            signed_target = f"{path}?{httpx.QueryParams(params)}"
        force_token = False
        for attempt in range(3):
            request_headers = await self._dm_capability_headers_for_path(path)
            request_headers.update(headers or {})
            request_headers.update(
                self._interaction_lifecycle_headers_for_path(path, origin=origin)
            )
            dm_capability = self._dm_capability_for_headers(origin, request_headers)
            token = await self._token(
                origin,
                force=force_token,
                application_home=application_home,
                dm_capability=dm_capability,
            )
            request_headers.update(self._proof_headers(method, signed_target, token))
            response = await self._targets[origin].request(
                method,
                path,
                json=json,
                params=params,
                headers=request_headers,
            )
            if response.status_code == 401 and attempt == 0:
                force_token = True
                continue
            force_token = False
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(
                    min(
                        30.0,
                        max(0.05, float(response.headers.get("Retry-After", "1"))),
                    )
                )
                continue
            await self._raise(response)
            return None if response.status_code == 204 else response.json()
        raise ApiError(
            503, "BOT_REQUEST_RETRY_EXHAUSTED", "Bot request retries were exhausted"
        )

    async def _redirect_location(
        self,
        path: str,
        *,
        target: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Resolve an authenticated API redirect without forwarding bot proofs.

        Media is served through short-lived object-storage URLs. The bot token
        and proof headers are used only against the Kaede origin and are never
        copied to the redirected host.
        """

        origin = self._request_target(path, target)
        if origin not in self._targets:
            self._targets[origin] = httpx.AsyncClient(base_url=origin, timeout=30)
        force_token = False
        for attempt in range(3):
            request_headers = dict(headers or {})
            request_headers.update(
                self._interaction_lifecycle_headers_for_path(path, origin=origin)
            )
            dm_capability = self._dm_capability_for_headers(origin, request_headers)
            token = await self._token(
                origin,
                force=force_token,
                dm_capability=dm_capability,
            )
            response = await self._targets[origin].get(
                path,
                headers=self._proof_headers("GET", path, token) | request_headers,
                follow_redirects=False,
            )
            if response.status_code == 401 and attempt == 0:
                force_token = True
                continue
            force_token = False
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(
                    min(
                        30.0,
                        max(0.05, float(response.headers.get("Retry-After", "1"))),
                    )
                )
                continue

            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise ApiError(
                        502, "MEDIA_REDIRECT_INVALID", "Media redirect is missing"
                    )
                resolved = urljoin(origin, location)
                from .media_urls import (
                    MediaURLValidationError,
                    media_url_origin,
                    validate_signed_media_url,
                    validate_target_media_url,
                )

                try:
                    signed_origin = response.headers.get("X-Kaede-Media-Origin")
                    validated = (
                        validate_signed_media_url(resolved, signed_origin)
                        if signed_origin
                        else validate_target_media_url(resolved, origin)
                    )
                    return validated, signed_origin or media_url_origin(validated)
                except MediaURLValidationError:
                    raise ApiError(
                        502,
                        "MEDIA_REDIRECT_INVALID",
                        "Media redirect is not on this instance's safe HTTPS media host",
                    ) from None
            await self._raise(response)
            raise ApiError(
                502, "MEDIA_REDIRECT_INVALID", "Media endpoint did not redirect"
            )
        raise ApiError(
            503, "BOT_REQUEST_RETRY_EXHAUSTED", "Bot request retries were exhausted"
        )

    async def _raise(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            body = response.json()
            detail = body.get("detail", {}) if isinstance(body, dict) else {}
        except ValueError:
            detail = {}
        code = (
            str(detail.get("code", "KAEDE_API_ERROR"))
            if isinstance(detail, dict)
            else "KAEDE_API_ERROR"
        )
        message = (
            str(detail.get("message") or code.replace("_", " ").title())
            if isinstance(detail, dict)
            else code.replace("_", " ").title()
        )
        if response.status_code == 429:
            raise RateLimited(
                429,
                code,
                message,
                float(response.headers.get("Retry-After", "1")),
                detail,
            )
        error_type = (
            Forbidden
            if response.status_code == 403
            else NotFound
            if response.status_code == 404
            else ApiError
        )
        raise error_type(response.status_code, code, message, detail)

    async def fetch_bot_identity(self, *, target: str | None = None) -> BotIdentity:
        """Return the authenticated application, worker, and effective grants."""

        identity = BotIdentity.from_payload(
            await self.request("GET", "/api/v1/bots/@me", target=target)
        )
        if (
            identity.application_ref != self.worker_state.application_ref
            or identity.worker_id != self.worker_state.worker_id
            or not identity.user.bot
        ):
            raise E2EEProtocolError("bot identity does not match this worker")
        return identity

    @staticmethod
    def _bot_e2ee_provider_material(
        worker_state: WorkerState,
        provider: E2EEProvider,
    ) -> tuple[E2EEProvider, bytes, bytes]:
        real_provider = require_real_e2ee_provider(provider)
        identity_key = real_provider.public_identity_key()
        if len(identity_key) != 32:
            raise E2EEProtocolError(
                "bot E2EE provider returned an invalid identity key"
            )
        credential = bot_mls_credential(
            worker_state.application_ref,
            worker_state.worker_id,
            identity_key,
        )
        return real_provider, identity_key, credential

    async def create_e2ee_device_challenge(
        self,
        provider: E2EEProvider,
        *,
        target: str | None = None,
    ) -> BotE2EEDeviceChallenge:
        """Create a one-use application-home challenge for this MLS provider."""

        _provider, identity_key, credential = self._bot_e2ee_provider_material(
            self.worker_state,
            provider,
        )
        challenge = BotE2EEDeviceChallenge.from_payload(
            await self.request(
                "POST",
                "/api/v1/bots/e2ee/devices/challenge",
                target=target,
                json={
                    "identity_key": _b64(identity_key),
                    "credential_digest": _b64(hashlib.sha256(credential).digest()),
                },
            )
        )
        if (
            challenge.application_ref != self.worker_state.application_ref
            or challenge.worker_id != self.worker_state.worker_id
        ):
            raise E2EEProtocolError("bot E2EE challenge does not match this worker")
        return challenge

    async def complete_e2ee_device_registration(
        self,
        provider: E2EEProvider,
        challenge: BotE2EEDeviceChallenge,
        *,
        capabilities: Sequence[str] = ("e2ee-mls/1", "e2ee-media/1"),
        target: str | None = None,
    ) -> BotE2EEDevice:
        """Prove possession of the MLS identity key and register the device."""

        real_provider, identity_key, credential = self._bot_e2ee_provider_material(
            self.worker_state,
            provider,
        )
        if (
            challenge.application_ref != self.worker_state.application_ref
            or challenge.worker_id != self.worker_state.worker_id
        ):
            raise ValueError("bot E2EE challenge belongs to a different worker")
        normalized_capabilities = tuple(sorted(set(capabilities)))
        if (
            len(normalized_capabilities) != len(capabilities)
            or not set(normalized_capabilities) <= BOT_E2EE_CAPABILITIES
            or "e2ee-mls/1" not in normalized_capabilities
        ):
            raise ValueError("bot E2EE capabilities are invalid")
        signature = real_provider.sign(challenge.signing_input)
        if len(signature) != 64:
            raise E2EEProtocolError("bot E2EE provider returned an invalid signature")
        device = BotE2EEDevice.from_payload(
            await self.request(
                "POST",
                "/api/v1/bots/e2ee/devices",
                target=target,
                json={
                    "challenge_id": challenge.challenge_id,
                    "identity_key": _b64(identity_key),
                    "credential": _b64(credential),
                    "signature": _b64(signature),
                    "capabilities": list(normalized_capabilities),
                },
            )
        )
        if (
            device.source_ref.domain != self.worker_state.application_ref.domain
            or device.worker_id != self.worker_state.worker_id
            or not hmac.compare_digest(device.identity_key, identity_key)
            or not hmac.compare_digest(device.credential, credential)
            or device.capabilities != frozenset(normalized_capabilities)
        ):
            raise E2EEProtocolError(
                "registered bot E2EE device does not match this worker"
            )
        self.set_e2ee_device(device)
        return device

    async def register_e2ee_device(
        self,
        provider: E2EEProvider,
        *,
        capabilities: Sequence[str] = ("e2ee-mls/1", "e2ee-media/1"),
        target: str | None = None,
    ) -> BotE2EEDevice:
        """Run the complete challenge/proof registration lifecycle."""

        challenge = await self.create_e2ee_device_challenge(provider, target=target)
        return await self.complete_e2ee_device_registration(
            provider,
            challenge,
            capabilities=capabilities,
            target=target,
        )

    async def e2ee_devices(
        self,
        *,
        target: str | None = None,
    ) -> BotE2EEDeviceInventory:
        """List trusted application devices and unclaimed KeyPackage inventory."""

        return BotE2EEDeviceInventory.from_payload(
            await self.request(
                "GET",
                "/api/v1/bots/e2ee/devices",
                target=target,
            )
        )

    async def upload_e2ee_key_packages(
        self,
        provider: E2EEProvider,
        device: BotE2EEDevice,
        *,
        count: int,
        expires_at: datetime | None = None,
        target: str | None = None,
    ) -> BotE2EEKeyPackageResult:
        """Generate and upload one signed batch of public MLS KeyPackages."""

        if isinstance(count, bool) or not 1 <= count <= 50:
            raise ValueError("key-package batches must contain 1 to 50 packages")
        real_provider, identity_key, _credential = self._bot_e2ee_provider_material(
            self.worker_state,
            provider,
        )
        if device.worker_id != self.worker_state.worker_id or not hmac.compare_digest(
            device.identity_key, identity_key
        ):
            raise ValueError("bot E2EE device does not belong to this worker provider")
        expiry = (expires_at or (datetime.now(UTC) + timedelta(days=7))).astimezone(UTC)
        if expiry <= datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("key-package expiry must be more than five minutes away")
        packages = tuple(real_provider.generate_key_package() for _ in range(count))
        if any(not 1 <= len(item) <= 32 * 1024 for item in packages):
            raise E2EEProtocolError("bot E2EE provider returned an invalid KeyPackage")
        signing_input = bot_key_package_upload_input(
            protocol_id=device.protocol_id,
            generation=device.generation,
            cipher_suite=MLS_SUITE,
            expires_at=expiry,
            package_hashes=(hashlib.sha256(item).digest() for item in packages),
        )
        signature = real_provider.sign(signing_input)
        if len(signature) != 64:
            raise E2EEProtocolError("bot E2EE provider returned an invalid signature")
        result = BotE2EEKeyPackageResult.from_payload(
            await self.request(
                "POST",
                f"/api/v1/bots/e2ee/devices/{device.protocol_id}/key-packages",
                target=target,
                json={
                    "cipher_suite": MLS_SUITE,
                    "expires_at": expiry.isoformat(),
                    "packages": [_b64(item) for item in packages],
                    "signature": _b64(signature),
                },
            )
        )
        if result.device_id != device.protocol_id:
            raise E2EEProtocolError("key-package response changed the device identity")
        return result

    async def replenish_e2ee_key_packages(
        self,
        provider: E2EEProvider,
        *,
        minimum_available: int = 20,
        desired_available: int = 50,
        expires_at: datetime | None = None,
        target: str | None = None,
    ) -> BotE2EEDevice:
        """Register this worker if needed and maintain a public KeyPackage pool."""

        if (
            isinstance(minimum_available, bool)
            or isinstance(desired_available, bool)
            or not 0 <= minimum_available <= desired_available <= 100
        ):
            raise ValueError("bot E2EE KeyPackage inventory bounds are invalid")
        provider = require_real_e2ee_provider(provider)
        identity_key = provider.public_identity_key()
        inventory = await self.e2ee_devices(target=target)
        matching = [
            item
            for item in inventory.devices
            if item.worker_id == self.worker_state.worker_id
            and hmac.compare_digest(item.identity_key, identity_key)
        ]
        if len(matching) > 1:
            raise E2EEProtocolError(
                "application home returned duplicate worker devices"
            )
        device = (
            matching[0]
            if matching
            else await self.register_e2ee_device(provider, target=target)
        )
        available = device.available_key_packages
        if available >= minimum_available:
            self.set_e2ee_device(device)
            return device
        while available < desired_available:
            batch = min(50, desired_available - available)
            uploaded = await self.upload_e2ee_key_packages(
                provider,
                device,
                count=batch,
                expires_at=expires_at,
                target=target,
            )
            if (
                uploaded.accepted != batch
                or uploaded.available_key_packages <= available
            ):
                raise E2EEProtocolError(
                    "application home did not accept the KeyPackage batch"
                )
            available = uploaded.available_key_packages
        replenished = BotE2EEDevice(
            source_ref=device.source_ref,
            protocol_id=device.protocol_id,
            worker_id=device.worker_id,
            identity_key=device.identity_key,
            credential=device.credential,
            capabilities=device.capabilities,
            generation=device.generation,
            available_key_packages=available,
        )
        self.set_e2ee_device(replenished)
        return replenished

    async def revoke_e2ee_device(
        self,
        protocol_id: str,
        *,
        target: str | None = None,
    ) -> None:
        """Revoke one worker device and trigger room rekeying at every authority."""

        if re.fullmatch(r"kbe_[A-Za-z0-9_-]{43}", protocol_id) is None:
            raise ValueError("bot E2EE device ID is invalid")
        await self.request(
            "DELETE",
            f"/api/v1/bots/e2ee/devices/{protocol_id}",
            target=target,
        )
        if self._e2ee_device_id == protocol_id:
            self._e2ee_device_id = None

    async def e2ee_participation(
        self,
        channel: EntityRef,
        *,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        target: str | None = None,
    ) -> BotE2EEParticipationStatus:
        """Return exact verified-device admission and MLS history-floor state."""

        origin = self._authority_target(channel, target)
        status = BotE2EEParticipationStatus.from_payload(
            await self.request(
                "GET",
                f"/api/v1/bots/channels/{channel}/e2ee/participation",
                target=origin,
                headers=await self._runtime_grant_headers(
                    channel,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                ),
            )
        )
        if (
            status.application_ref != self.worker_state.application_ref
            or status.channel_ref != channel
        ):
            raise E2EEProtocolError("bot E2EE participation identity is invalid")
        return status

    def _e2ee_control_checkpoint_key(self, target: str, channel: EntityRef) -> str:
        if self._e2ee_device_id is None:
            raise E2EEProtocolError("an exact bot E2EE device must be selected")
        return f"{canonical_target_origin(target)}|{self._e2ee_device_id}|{channel}"

    async def _fetch_e2ee_control_log(
        self,
        channel: EntityRef,
        *,
        headers: dict[str, str],
        after: str | None,
        limit: int,
        target: str,
    ) -> BotE2EEControlPage:
        if not 1 <= limit <= 25:
            raise ValueError("bot E2EE control-log limit must be between 1 and 25")
        params: dict[str, object] = {"limit": limit}
        if after is not None:
            cursor = EntityRef.parse(after)
            if cursor.domain != channel.domain:
                raise E2EEProtocolError("bot E2EE control cursor authority is invalid")
            params["after"] = after
        page = BotE2EEControlPage.from_payload(
            await self.request(
                "GET",
                f"/api/v1/bots/channels/{channel}/e2ee/control-log",
                target=target,
                headers=headers,
                params=params,
            )
        )
        if (
            page.application_ref != self.worker_state.application_ref
            or page.channel_ref != channel
            or page.device_id != self._e2ee_device_id
        ):
            raise E2EEProtocolError("bot E2EE control-log identity was substituted")
        previous = EntityRef.parse(after) if after is not None else None
        for control in page.controls:
            if previous is not None and (control.ref.id, control.ref.domain) <= (
                previous.id,
                previous.domain,
            ):
                raise E2EEProtocolError("bot E2EE control log did not advance")
            previous = control.ref
        if page.next_after is not None and (
            not page.controls or page.next_after != page.controls[-1].cursor
        ):
            raise E2EEProtocolError("bot E2EE control-log page cursor is invalid")
        return page

    async def fetch_e2ee_control_log(
        self,
        channel: EntityRef,
        *,
        after: str | None = None,
        limit: int = 25,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        target: str | None = None,
    ) -> BotE2EEControlPage:
        """Fetch one durable control page for this worker's selected device."""

        origin = self._authority_target(channel, target)
        return await self._fetch_e2ee_control_log(
            channel,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
            after=after,
            limit=limit,
            target=origin,
        )

    async def _sync_e2ee_control_log(
        self,
        context: InteractionE2EEContext,
        *,
        headers: dict[str, str],
        target: str,
    ) -> str | None:
        key = self._e2ee_control_checkpoint_key(target, context.channel_ref)
        current_digest = hashlib.sha256(context.provider.export_state()).hexdigest()
        checkpoint = self._e2ee_control_checkpoints.get(key)
        after = (
            checkpoint[0]
            if checkpoint is not None and checkpoint[1] == current_digest
            else None
        )
        seen: set[str] = set()
        for _page_number in range(256):
            page = await self._fetch_e2ee_control_log(
                context.channel_ref,
                headers=headers,
                after=after,
                limit=25,
                target=target,
            )
            for control in page.controls:
                process_e2ee_control(context, control)
                # Bind the cursor to the provider state produced by that exact
                # successful control. A crash can replay, but never skip, MLS
                # state that was not durably checkpointed by the worker.
                state_digest = hashlib.sha256(
                    context.provider.export_state()
                ).hexdigest()
                after = control.cursor
                self._e2ee_control_checkpoints[key] = (after, state_digest)
                self.worker_state.save_e2ee_control_checkpoints(
                    self._e2ee_control_checkpoints
                )
            if page.next_after is None:
                return after
            if page.next_after in seen or page.next_after != after:
                raise E2EEProtocolError("bot E2EE control-log cursor did not advance")
            seen.add(page.next_after)
        raise E2EEProtocolError("bot E2EE control log exceeded its recovery bound")

    async def sync_e2ee_control_log(
        self,
        context: InteractionE2EEContext,
        *,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        target: str | None = None,
    ) -> str | None:
        """Recover every missed Welcome/Commit before encrypted processing."""

        origin = self._authority_target(context.channel_ref, target)
        return await self._sync_e2ee_control_log(
            context,
            headers=await self._runtime_grant_headers(
                context.channel_ref,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
            target=origin,
        )

    async def fetch_user(self, ref: EntityRef, *, target: str | None = None) -> User:
        origin = self._authority_target(ref, target)
        return User.from_payload(
            await self.request("GET", f"/api/v1/bots/users/{ref}", target=origin)
        )

    async def fetch_guilds(self, *, target: str | None = None) -> list[Guild]:
        origin = self._target(target)
        raw = await self.request("GET", "/api/v1/bots/guilds", target=origin)
        results = [Guild.from_payload(self, origin, item) for item in raw]
        authority = urlsplit(origin).hostname
        if authority is None or any(item.ref.domain != authority for item in results):
            raise ValueError("guild list response changed its target authority")
        return results

    def _guild_response(
        self,
        origin: str,
        raw: dict[str, Any],
        *,
        expected: EntityRef,
    ) -> Guild:
        result = Guild.from_payload(self, origin, raw)
        if result.ref != expected:
            raise ValueError("guild response changed the requested guild")
        return result

    async def fetch_guild(
        self, guild: EntityRef, *, target: str | None = None
    ) -> Guild:
        origin = self._authority_target(guild, target)
        raw = await self.request("GET", f"/api/v1/bots/guilds/{guild}", target=origin)
        return self._guild_response(origin, raw, expected=guild)

    async def fetch_channels(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Channel]:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/channels", target=origin
        )
        return [Channel.from_payload(self, origin, item) for item in raw]

    async def fetch_channel(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Channel:
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        return Channel.from_payload(self, origin, raw).bind_runtime(
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )

    async def fetch_tracker(
        self, channel: EntityRef, *, target: str | None = None
    ) -> TrackerBoard:
        """Fetch a task-tracker board, including its ordered lanes and tasks."""

        origin = self._authority_target(channel, target)
        raw = await self.request(
            "GET", f"/api/v1/bots/channels/{channel}/tracker", target=origin
        )
        return _tracker_board_response(self, origin, channel, raw)

    async def edit_tracker(
        self,
        channel: EntityRef,
        *,
        key_prefix: str,
        target: str | None = None,
        version: str | None,
    ) -> TrackerBoard:
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/channels/{channel}/tracker",
            target=origin,
            json={"key_prefix": key_prefix},
            headers=_version_headers(version),
        )
        return _tracker_board_response(self, origin, channel, raw)

    async def create_tracker_lane(
        self,
        channel: EntityRef,
        name: str,
        *,
        target: str | None = None,
        color: int = 0,
        kind: str = "custom",
        completed: bool = False,
        position: int | None = None,
    ) -> TrackerLane:
        origin = self._authority_target(channel, target)
        body: dict[str, object] = {
            "name": name,
            "color": color,
            "kind": kind,
            "completed": completed,
        }
        if position is not None:
            body["position"] = position
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/tracker/lanes",
            target=origin,
            json=body,
        )
        return _tracker_lane_response(self, origin, channel, raw)

    async def edit_tracker_lane(
        self,
        channel: EntityRef,
        lane: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
        name: str | MissingType = MISSING,
        color: int | MissingType = MISSING,
        kind: str | MissingType = MISSING,
        completed: bool | MissingType = MISSING,
    ) -> TrackerLane:
        self._require_same_authority(channel, lane, label="tracker lane")
        body = _provided_fields(
            name=name,
            color=color,
            kind=kind,
            completed=completed,
        )
        if not body:
            raise ValueError("at least one tracker lane field is required")
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/channels/{channel}/tracker/lanes/{lane}",
            target=origin,
            json=body,
            headers=_version_headers(version),
        )
        return _tracker_lane_response(
            self,
            origin,
            channel,
            raw,
            expected_ref=lane,
        )

    async def move_tracker_lane(
        self,
        channel: EntityRef,
        lane: EntityRef,
        position: int,
        *,
        target: str | None = None,
        version: str | None,
    ) -> TrackerLane:
        self._require_same_authority(channel, lane, label="tracker lane")
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/tracker/lanes/{lane}/move",
            target=origin,
            json={"position": position},
            headers=_version_headers(version),
        )
        return _tracker_lane_response(
            self,
            origin,
            channel,
            raw,
            expected_ref=lane,
        )

    async def delete_tracker_lane(
        self,
        channel: EntityRef,
        lane: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
    ) -> None:
        self._require_same_authority(channel, lane, label="tracker lane")
        origin = self._authority_target(channel, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/tracker/lanes/{lane}",
            target=origin,
            headers=_version_headers(version),
        )

    async def create_tracker_task(
        self,
        channel: EntityRef,
        lane: EntityRef,
        title: str,
        *,
        target: str | None = None,
        description: str | None = None,
        priority: str = "none",
        position: int | None = None,
        due_at: datetime | None = None,
        assignee: EntityRef | None = None,
        client_nonce: str | None = None,
    ) -> TrackerTask:
        self._require_same_authority(channel, lane, label="tracker lane")
        origin = self._authority_target(channel, target)
        body: dict[str, object] = {
            "lane_id": str(lane),
            "title": title,
            "description": description,
            "priority": priority,
            "due_at": due_at.isoformat() if due_at is not None else None,
            "assignee_id": str(assignee) if assignee is not None else None,
        }
        if position is not None:
            body["position"] = position
        if client_nonce is not None:
            body["client_nonce"] = client_nonce
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/tracker/tasks",
            target=origin,
            json=body,
        )
        return _tracker_task_response(
            self,
            origin,
            channel,
            raw,
            expected_lane=lane,
        )

    async def edit_tracker_task(
        self,
        channel: EntityRef,
        task: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
        title: str | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        priority: str | MissingType = MISSING,
        due_at: datetime | None | MissingType = MISSING,
        assignee: EntityRef | None | MissingType = MISSING,
    ) -> TrackerTask:
        self._require_same_authority(channel, task, label="tracker task")
        body = _provided_fields(
            title=title,
            description=description,
            priority=priority,
            due_at=(due_at.isoformat() if isinstance(due_at, datetime) else due_at),
            assignee_id=(
                str(assignee) if isinstance(assignee, EntityRef) else assignee
            ),
        )
        if not body:
            raise ValueError("at least one tracker task field is required")
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/channels/{channel}/tracker/tasks/{task}",
            target=origin,
            json=body,
            headers=_version_headers(version),
        )
        return _tracker_task_response(
            self,
            origin,
            channel,
            raw,
            expected_ref=task,
        )

    async def move_tracker_task(
        self,
        channel: EntityRef,
        task: EntityRef,
        lane: EntityRef,
        position: int,
        *,
        target: str | None = None,
        version: str | None,
    ) -> TrackerTask:
        self._require_same_authority(channel, task, lane, label="tracker task or lane")
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/tracker/tasks/{task}/move",
            target=origin,
            json={"lane_id": str(lane), "position": position},
            headers=_version_headers(version),
        )
        return _tracker_task_response(
            self,
            origin,
            channel,
            raw,
            expected_ref=task,
            expected_lane=lane,
        )

    async def delete_tracker_task(
        self,
        channel: EntityRef,
        task: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
    ) -> None:
        self._require_same_authority(channel, task, label="tracker task")
        origin = self._authority_target(channel, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/tracker/tasks/{task}",
            target=origin,
            headers=_version_headers(version),
        )

    def _thread_page(
        self,
        raw: dict[str, Any],
        *,
        target: str,
        default_domain: str,
        dm_capability_id: str | None = None,
    ) -> ThreadPage:
        threads = [
            Channel.from_payload(self, target, item).bind_runtime(
                dm_capability_id=dm_capability_id
            )
            for item in raw.get("threads", [])
            if isinstance(item, dict)
        ]
        domains = {thread.ref.id: thread.ref.domain for thread in threads}
        members: list[ThreadMember] = []
        for item in raw.get("members", []):
            if not isinstance(item, dict):
                continue
            thread_id = item.get("id", item.get("thread_id"))
            thread_domain = item.get("thread_domain")
            if not isinstance(thread_domain, str) and thread_id is not None:
                thread_domain = domains.get(int(thread_id), default_domain)
            normalized = dict(item)
            if isinstance(thread_domain, str):
                normalized["thread_domain"] = thread_domain
            members.append(
                ThreadMember.from_payload(
                    normalized,
                    default_domain=default_domain,
                    client=self,
                    target=target,
                )
            )
        return ThreadPage(
            threads=threads,
            members=members,
            has_more=bool(raw.get("has_more", False)),
            next_cursor=(
                str(raw["next_cursor"]) if raw.get("next_cursor") is not None else None
            ),
        )

    async def start_thread(
        self,
        channel: EntityRef,
        name: str,
        *,
        target: str | None = None,
        reason: str | None = None,
        type: int | None = None,
        content: str | None = None,
        e2ee: dict[str, Any] | None = None,
        attachment_ids: Sequence[int] = (),
        sticker_ids: Sequence[EntityRef] = (),
        embeds: Sequence[Embed] = (),
        view: View | None = None,
        poll: Poll | None = None,
        reply_to: EntityRef | None = None,
        mention_user_ids: Sequence[EntityRef] = (),
        forward: EntityRef | None = None,
        tts: bool = False,
        voice_message: bool = False,
        applied_tag_ids: Sequence[int] = (),
        auto_archive_duration: int | None = None,
        rate_limit_per_user: int | None = None,
        invitable: bool | None = None,
        client_nonce: str | None = None,
        starter_reservation_nonce: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Channel:
        if starter_reservation_nonce is not None and any(
            (
                content is not None,
                e2ee is not None,
                bool(attachment_ids),
                bool(sticker_ids),
                bool(embeds),
                view is not None,
                poll is not None,
                reply_to is not None,
                bool(mention_user_ids),
                forward is not None,
                tts,
                voice_message,
                client_nonce is not None,
            )
        ):
            raise ValueError("an encrypted forum reservation cannot include a starter")
        if content is not None and e2ee is not None:
            raise ValueError(
                "a thread starter cannot contain plaintext and E2EE content"
            )
        if e2ee is not None and (
            embeds or sticker_ids or view is not None or poll is not None
        ):
            raise ValueError(
                "raw encrypted thread starters must carry rich fields inside their envelope; "
                "use a starter reservation followed by claim_encrypted_forum_starter for "
                "automatic rich encryption"
            )
        if voice_message and (
            tts
            or content is not None
            or e2ee is not None
            or embeds
            or view is not None
            or poll is not None
            or forward is not None
            or mention_user_ids
            or len(attachment_ids) != 1
            or sticker_ids
        ):
            raise ValueError(
                "a voice-message thread starter requires exactly one audio attachment "
                "and cannot include content, rich content, forwarding, mentions, or "
                "encrypted content"
            )
        if forward is not None and (
            content is not None
            or e2ee is not None
            or attachment_ids
            or sticker_ids
            or embeds
            or view is not None
            or poll is not None
            or reply_to is not None
            or mention_user_ids
            or voice_message
        ):
            raise ValueError("a forwarded thread starter cannot include another body")
        if type is not None and type not in {10, 11, 12}:
            raise ValueError("thread type must be 10, 11, or 12")
        origin = self._authority_target(channel, target)
        body: dict[str, Any] = {"name": name}
        for key, value in (
            ("type", type),
            ("auto_archive_duration", auto_archive_duration),
            ("rate_limit_per_user", rate_limit_per_user),
            ("invitable", invitable),
        ):
            if value is not None:
                body[key] = value
        if applied_tag_ids:
            body["applied_tag_ids"] = [str(item) for item in applied_tag_ids]
        if starter_reservation_nonce is not None:
            body["starter_reservation_nonce"] = starter_reservation_nonce
        if any(
            (
                content is not None,
                e2ee is not None,
                bool(attachment_ids),
                bool(sticker_ids),
                bool(embeds),
                view is not None,
                poll is not None,
                reply_to is not None,
                bool(mention_user_ids),
                forward is not None,
                tts,
                voice_message,
                client_nonce is not None,
            )
        ):
            starter: dict[str, Any] = {}
            if content is not None:
                starter["content"] = content
            if e2ee is not None:
                starter["e2ee"] = e2ee
            if attachment_ids:
                starter["attachment_ids"] = [str(item) for item in attachment_ids]
            if sticker_ids:
                if len(sticker_ids) > 3 or len(set(sticker_ids)) != len(sticker_ids):
                    raise ValueError(
                        "sticker_ids must contain at most three unique stickers"
                    )
                starter["sticker_ids"] = [str(item) for item in sticker_ids]
            if embeds:
                starter["embeds"] = serialize_embeds(embeds)
            if view is not None:
                starter["components"] = view.to_components()
                starter["view_persistent"] = view.is_persistent
                if view.timeout is not None:
                    starter["view_timeout_seconds"] = max(1, int(view.timeout))
            if poll is not None:
                starter["poll"] = poll.to_dict()
            if reply_to is not None:
                starter["referenced_message_id"] = str(reply_to)
            if mention_user_ids:
                starter["mention_user_ids"] = [str(item) for item in mention_user_ids]
            if forward is not None:
                starter["forwarded_message_id"] = str(forward)
            if tts:
                starter["tts"] = True
            if voice_message:
                starter["voice_message"] = True
            if client_nonce is not None:
                starter["client_nonce"] = client_nonce
            body["message"] = starter
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/threads",
            target=origin,
            json=body,
            headers=_merge_headers(
                await self._runtime_grant_headers(
                    channel,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                ),
                _audit_headers(reason),
            ),
        )
        thread = Channel.from_payload(self, origin, raw).bind_runtime(
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )
        if view is not None and view.rows and thread.starter_message_ref is not None:
            starter_message = thread.starter_message
            starter_channel = (
                starter_message.channel_ref
                if starter_message is not None
                else thread.ref
            )
            starter_version = (
                starter_message.view_version if starter_message is not None else None
            )
            self.add_view(
                view,
                message_id=thread.starter_message_ref,
                target=origin,
                timeout_editor=self._view_timeout_editor(
                    (
                        f"/api/v1/bots/channels/{starter_channel}/messages/"
                        f"{thread.starter_message_ref}"
                    ),
                    target=origin,
                    view_version=starter_version,
                    installation_id=installation_id,
                    channel_ref=channel,
                    dm_capability_id=dm_capability_id,
                ),
            )
        return thread

    async def start_thread_from_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        name: str,
        *,
        target: str | None = None,
        reason: str | None = None,
        auto_archive_duration: int | None = None,
        rate_limit_per_user: int | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Channel:
        origin = self._authority_target(channel, target)
        body: dict[str, Any] = {"name": name}
        if auto_archive_duration is not None:
            body["auto_archive_duration"] = auto_archive_duration
        if rate_limit_per_user is not None:
            body["rate_limit_per_user"] = rate_limit_per_user
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/messages/{message}/threads",
            target=origin,
            json=body,
            headers=_merge_headers(
                await self._runtime_grant_headers(
                    channel,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                ),
                _audit_headers(reason),
            ),
        )
        return Channel.from_payload(self, origin, raw).bind_runtime(
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )

    async def fetch_threads(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        archived: bool = False,
        include_archived: bool = False,
        before: datetime | None = None,
        cursor: str | None = None,
        limit: int = 50,
        tag_id: int | None = None,
        tag_ids: list[int] | None = None,
        query: str | None = None,
        sort_order: int | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> ThreadPage:
        if tag_id is not None and tag_ids is not None:
            raise ValueError("use tag_id or tag_ids, not both")
        if before is not None and cursor is not None:
            raise ValueError("use before or cursor, not both")
        if archived and include_archived:
            raise ValueError("include_archived cannot be combined with archived")
        origin = self._authority_target(channel, target)
        params: dict[str, Any] = {
            "limit": min(100, max(1, limit)),
        }
        if include_archived:
            params["include_archived"] = "true"
        else:
            params["archived"] = str(archived).lower()
        if before is not None:
            params["before"] = before.isoformat()
        if cursor is not None:
            params["cursor"] = cursor
        selected_tags = (
            tag_ids if tag_ids is not None else ([tag_id] if tag_id is not None else [])
        )
        if selected_tags:
            params["tag_id"] = [str(item) for item in selected_tags]
        if query is not None:
            params["query"] = query
        if sort_order is not None:
            params["sort_order"] = sort_order
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/threads",
            target=origin,
            params=params,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        return self._thread_page(
            raw,
            target=origin,
            default_domain=channel.domain,
            dm_capability_id=dm_capability_id,
        )

    async def active_threads(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
    ) -> ThreadPage:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/threads/active",
            target=origin,
            headers=self._e2ee_device_headers(),
        )
        return self._thread_page(raw, target=origin, default_domain=guild.domain)

    async def edit_thread(
        self,
        thread: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
        name: str | MissingType = MISSING,
        archived: bool | MissingType = MISSING,
        locked: bool | MissingType = MISSING,
        invitable: bool | MissingType = MISSING,
        auto_archive_duration: int | MissingType = MISSING,
        rate_limit_per_user: int | MissingType = MISSING,
        applied_tag_ids: list[int] | MissingType = MISSING,
        pinned: bool | MissingType = MISSING,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Channel:
        origin = self._authority_target(thread, target)
        body = _provided_fields(
            name=name,
            archived=archived,
            locked=locked,
            invitable=invitable,
            auto_archive_duration=auto_archive_duration,
            rate_limit_per_user=rate_limit_per_user,
            pinned=pinned,
        )
        if not isinstance(applied_tag_ids, MissingType):
            body["applied_tag_ids"] = [str(item) for item in applied_tag_ids]
        if not body:
            raise ValueError("at least one thread field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/channels/{thread}",
            target=origin,
            json=body,
            headers=_merge_headers(
                await self._runtime_grant_headers(
                    thread,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                ),
                _audit_headers(reason),
            ),
        )
        return Channel.from_payload(self, origin, raw).bind_runtime(
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )

    async def delete_thread(
        self,
        thread: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Channel:
        origin = self._authority_target(thread, target)
        raw = await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{thread}",
            target=origin,
            headers=_merge_headers(
                await self._runtime_grant_headers(
                    thread,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                ),
                _audit_headers(reason),
            ),
        )
        return Channel.from_payload(self, origin, raw).bind_runtime(
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )

    async def join_thread(
        self,
        thread: EntityRef,
        *,
        target: str | None = None,
        flags: int = 0,
        notification_level: str = "inherit",
        dm_capability_id: str | None = None,
    ) -> None:
        if flags < 0:
            raise ValueError("thread member flags cannot be negative")
        if notification_level not in {"inherit", "all", "mentions", "none"}:
            raise ValueError("unsupported thread notification level")
        origin = self._authority_target(thread, target)
        await self.request(
            "PUT",
            f"/api/v1/bots/channels/{thread}/thread-members/@me",
            target=origin,
            json={"flags": flags, "notification_level": notification_level},
            headers=await self._dm_capability_headers_for_grant(
                thread,
                dm_capability_id,
            ),
        )

    async def leave_thread(
        self,
        thread: EntityRef,
        *,
        target: str | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(thread, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{thread}/thread-members/@me",
            target=origin,
            headers=await self._dm_capability_headers_for_grant(
                thread,
                dm_capability_id,
            ),
        )

    async def add_thread_member(
        self,
        thread: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(thread, target)
        await self.request(
            "PUT",
            f"/api/v1/bots/channels/{thread}/thread-members/{user}",
            target=origin,
            headers=await self._dm_capability_headers_for_grant(
                thread,
                dm_capability_id,
            ),
        )

    async def remove_thread_member(
        self,
        thread: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(thread, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{thread}/thread-members/{user}",
            target=origin,
            headers=await self._dm_capability_headers_for_grant(
                thread,
                dm_capability_id,
            ),
        )

    async def thread_members(
        self,
        thread: EntityRef,
        *,
        target: str | None = None,
        after: EntityRef | None = None,
        limit: int = 100,
        with_member: bool = False,
        dm_capability_id: str | None = None,
    ) -> list[ThreadMember]:
        origin = self._authority_target(thread, target)
        params: dict[str, Any] = {
            "limit": min(100, max(1, limit)),
            "with_member": str(with_member).lower(),
        }
        if after is not None:
            params["after"] = str(after)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{thread}/thread-members",
            target=origin,
            params=params,
            headers=await self._dm_capability_headers_for_grant(
                thread,
                dm_capability_id,
            ),
        )
        items = raw.get("members", []) if isinstance(raw, dict) else raw
        return [
            ThreadMember.from_payload(
                item,
                default_domain=thread.domain,
                default_thread=thread,
                client=self,
                target=origin,
            )
            for item in items
            if isinstance(item, dict)
        ]

    async def fetch_thread_member(
        self,
        thread: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        with_member: bool = False,
        dm_capability_id: str | None = None,
    ) -> ThreadMember:
        origin = self._authority_target(thread, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{thread}/thread-members/{user}",
            target=origin,
            params={"with_member": str(with_member).lower()},
            headers=await self._dm_capability_headers_for_grant(
                thread,
                dm_capability_id,
            ),
        )
        return ThreadMember.from_payload(
            raw,
            default_domain=thread.domain,
            default_thread=thread,
            client=self,
            target=origin,
        )

    async def fetch_members(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        limit: int = 100,
        after: EntityRef | None = None,
        query: str | None = None,
    ) -> list[Member]:
        origin = self._authority_target(guild, target)
        params: dict[str, Any] = {"limit": min(1000, max(1, limit))}
        if after is not None:
            params["after"] = str(after)
        if query is not None:
            params["query"] = query
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/members",
            target=origin,
            params=params,
        )
        return [Member.from_payload(self, origin, item) for item in raw]

    async def fetch_roles(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Role]:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/roles", target=origin
        )
        return [Role.from_payload(self, origin, item) for item in raw]

    async def edit_guild(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
        reason: str | None = None,
        name: str | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        federated_history_policy: str | MissingType = MISSING,
    ) -> Guild:
        origin = self._authority_target(guild, target)
        body = _provided_fields(
            name=name,
            description=description,
            federated_history_policy=federated_history_policy,
        )
        if not body:
            raise ValueError("at least one guild field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}",
            target=origin,
            json=body,
            headers=_merge_headers(_version_headers(version), _audit_headers(reason)),
        )
        return self._guild_response(origin, raw, expected=guild)

    async def create_channel(
        self,
        guild: EntityRef,
        name: str,
        *,
        target: str | None = None,
        reason: str | None = None,
        type: int = 0,
        topic: str | None = None,
        parent_id: int | None = None,
        rate_limit_per_user: int = 0,
        bitrate: int | MissingType = MISSING,
        user_limit: int | MissingType = MISSING,
        rtc_region: str | None | MissingType = MISSING,
        video_quality_mode: int | MissingType = MISSING,
        default_thread_rate_limit_per_user: int | MissingType = MISSING,
        default_auto_archive_duration: int | MissingType = MISSING,
        available_tags: list[dict[str, Any]] | MissingType = MISSING,
        default_reaction_emoji: dict[str, Any] | None | MissingType = MISSING,
        default_sort_order: int | None | MissingType = MISSING,
        default_forum_layout: int | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        e2ee_required: bool | MissingType = MISSING,
        tracker_key_prefix: str | MissingType = MISSING,
    ) -> Channel:
        _validate_voice_channel_options(
            type,
            bitrate=bitrate,
            user_limit=user_limit,
            video_quality_mode=video_quality_mode,
        )
        origin = self._authority_target(guild, target)
        body: dict[str, object] = {
            "name": name,
            "type": type,
            "topic": topic,
            "parent_id": str(parent_id) if parent_id is not None else None,
            "rate_limit_per_user": rate_limit_per_user,
        }
        body.update(
            _provided_fields(
                default_thread_rate_limit_per_user=default_thread_rate_limit_per_user,
                default_auto_archive_duration=default_auto_archive_duration,
                available_tags=_wire_forum_tags(available_tags),
                default_reaction_emoji=_wire_forum_emoji(default_reaction_emoji),
                default_sort_order=default_sort_order,
                default_forum_layout=default_forum_layout,
                flags=flags,
                e2ee_required=e2ee_required,
                tracker_key_prefix=tracker_key_prefix,
                bitrate=bitrate,
                user_limit=user_limit,
                rtc_region=rtc_region,
                video_quality_mode=video_quality_mode,
            )
        )
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/channels",
            target=origin,
            json=body,
            headers=_audit_headers(reason),
        )
        return Channel.from_payload(self, origin, raw)

    async def edit_channel(
        self,
        guild: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
        reason: str | None = None,
        name: str | MissingType = MISSING,
        topic: str | None | MissingType = MISSING,
        parent_id: int | None | MissingType = MISSING,
        rate_limit_per_user: int | MissingType = MISSING,
        bitrate: int | MissingType = MISSING,
        user_limit: int | MissingType = MISSING,
        rtc_region: str | None | MissingType = MISSING,
        video_quality_mode: int | MissingType = MISSING,
        default_thread_rate_limit_per_user: int | MissingType = MISSING,
        default_auto_archive_duration: int | MissingType = MISSING,
        available_tags: list[dict[str, Any]] | MissingType = MISSING,
        default_reaction_emoji: dict[str, Any] | None | MissingType = MISSING,
        default_sort_order: int | None | MissingType = MISSING,
        default_forum_layout: int | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        e2ee_required: bool | MissingType = MISSING,
        federated_history_policy: str | MissingType = MISSING,
        sync_permissions: bool | MissingType = MISSING,
        channel_type: int | None = None,
    ) -> Channel:
        self._require_same_authority(guild, channel, label="channel")
        if channel_type is not None:
            _validate_voice_channel_options(
                channel_type,
                bitrate=bitrate,
                user_limit=user_limit,
                video_quality_mode=video_quality_mode,
            )
        elif not isinstance(video_quality_mode, MissingType) and (
            isinstance(video_quality_mode, bool)
            or not isinstance(video_quality_mode, int)
            or video_quality_mode not in {1, 2}
        ):
            raise ValueError("video_quality_mode must be 1 or 2")
        origin = self._authority_target(guild, target)
        body = _provided_fields(
            name=name,
            topic=topic,
            parent_id=(str(parent_id) if isinstance(parent_id, int) else parent_id),
            rate_limit_per_user=rate_limit_per_user,
            bitrate=bitrate,
            user_limit=user_limit,
            rtc_region=rtc_region,
            video_quality_mode=video_quality_mode,
            default_thread_rate_limit_per_user=default_thread_rate_limit_per_user,
            default_auto_archive_duration=default_auto_archive_duration,
            available_tags=_wire_forum_tags(available_tags),
            default_reaction_emoji=_wire_forum_emoji(default_reaction_emoji),
            default_sort_order=default_sort_order,
            default_forum_layout=default_forum_layout,
            flags=flags,
            e2ee_required=e2ee_required,
            federated_history_policy=federated_history_policy,
            sync_permissions=sync_permissions,
        )
        if not body:
            raise ValueError("at least one channel field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}",
            target=origin,
            json=body,
            headers=_merge_headers(_version_headers(version), _audit_headers(reason)),
        )
        return Channel.from_payload(self, origin, raw)

    async def set_voice_channel_status(
        self,
        guild: EntityRef,
        channel: EntityRef,
        status: str | None,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._require_same_authority(guild, channel, label="channel")
        origin = self._authority_target(guild, target)
        normalized = status.strip() if status is not None else None
        normalized = normalized or None
        if normalized is not None and len(normalized) > 500:
            raise ValueError("voice channel status cannot exceed 500 characters")
        await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}/voice-status",
            target=origin,
            json={"status": normalized},
            headers=_audit_headers(reason),
        )

    async def reorder_channels(
        self,
        guild: EntityRef,
        positions: Sequence[
            ChannelPositionUpdate
            | tuple[EntityRef, int | None]
            | tuple[EntityRef, int | None, int | None]
            | tuple[EntityRef, int | None, int | None, bool | None]
            | tuple[EntityRef, int | None, int | None, bool | None, int | None]
        ],
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        if not 1 <= len(positions) <= 500:
            raise ValueError("channel positions must contain 1 to 500 entries")
        rows: list[dict[str, object]] = []
        channels: list[EntityRef] = []
        seen_channels: set[EntityRef] = set()
        for entry in positions:
            channel: object
            position: object
            parent_id: object
            lock_permissions: object
            flags: object
            if isinstance(entry, ChannelPositionUpdate):
                channel = entry.channel
                position = entry.position
                parent_id = entry.parent_id
                lock_permissions = entry.lock_permissions
                flags = entry.flags
            else:
                raw_entry = cast(tuple[object, ...], entry)
                if len(raw_entry) not in {2, 3, 4, 5}:
                    raise ValueError(
                        "channel position entries must contain 2 to 5 values"
                    )
                channel, position = raw_entry[:2]
                parent_id = raw_entry[2] if len(raw_entry) >= 3 else MISSING
                lock_permissions = raw_entry[3] if len(raw_entry) >= 4 else MISSING
                flags = raw_entry[4] if len(raw_entry) == 5 else MISSING
            if not isinstance(channel, EntityRef):
                raise TypeError("channel position channel must be an EntityRef")
            if (
                position is MISSING
                and parent_id is MISSING
                and lock_permissions is MISSING
                and flags is MISSING
            ):
                raise ValueError("at least one channel position field is required")
            if position is not MISSING:
                if position is not None and (
                    isinstance(position, bool)
                    or not isinstance(position, int)
                    or position < 0
                ):
                    raise ValueError(
                        "channel position must be a non-negative integer or null"
                    )
            if channel in seen_channels:
                raise ValueError("channel position channels must be unique")
            seen_channels.add(channel)
            channels.append(channel)
            row: dict[str, object] = {"id": str(channel.id)}
            if position is not MISSING:
                row["position"] = position
            if parent_id is not MISSING:
                if parent_id is not None and (
                    isinstance(parent_id, bool)
                    or not isinstance(parent_id, int)
                    or parent_id < 1
                ):
                    raise ValueError(
                        "channel parent ID must be a positive integer or null"
                    )
                row["parent_id"] = str(parent_id) if parent_id is not None else None
            if lock_permissions is not MISSING:
                if lock_permissions is not None and not isinstance(
                    lock_permissions, bool
                ):
                    raise TypeError("lock_permissions must be a boolean or null")
                if parent_id is None and lock_permissions:
                    raise ValueError("permissions cannot be locked without a category")
                row["lock_permissions"] = lock_permissions
            if flags is not MISSING:
                if flags is not None and (
                    isinstance(flags, bool)
                    or not isinstance(flags, int)
                    or flags not in {0, 16}
                ):
                    raise ValueError("channel position flags must be 0, 16, or null")
                row["flags"] = flags
            rows.append(row)
        self._require_same_authority(guild, *channels, label="channel")
        origin = self._authority_target(guild, target)
        await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/channels",
            target=origin,
            json={"channels": rows},
            headers=_audit_headers(reason),
        )

    async def delete_channel(
        self,
        guild: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> Channel:
        self._require_same_authority(guild, channel, label="channel")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}",
            target=origin,
            headers=_audit_headers(reason),
        )
        return Channel.from_payload(self, origin, raw)

    async def fetch_channel_overwrites(
        self,
        guild: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
    ) -> list[ChannelOverwrite]:
        self._require_same_authority(guild, channel, label="channel")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}/overwrites",
            target=origin,
        )
        return [ChannelOverwrite.from_payload(item) for item in raw]

    async def set_channel_overwrite(
        self,
        guild: EntityRef,
        channel: EntityRef,
        overwrite_target: EntityRef,
        target_type: str,
        *,
        target: str | None = None,
        allow: int = 0,
        deny: int = 0,
        reason: str | None = None,
    ) -> ChannelOverwrite:
        if target_type not in {"role", "member"}:
            raise ValueError("target_type must be 'role' or 'member'")
        if allow < 0 or deny < 0:
            raise ValueError("permission masks must be non-negative")
        if allow & deny:
            raise ValueError("allow and deny permission masks cannot overlap")
        self._require_same_authority(guild, channel, label="channel")
        if target_type == "role":
            self._require_same_authority(
                guild, overwrite_target, label="overwrite role"
            )
        origin = self._authority_target(guild, target)
        await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}/overwrites",
            target=origin,
            json={
                "target_id": str(overwrite_target),
                "target_type": target_type,
                "allow": str(allow),
                "deny": str(deny),
            },
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )
        return ChannelOverwrite(overwrite_target, target_type, allow, deny)

    async def delete_channel_overwrite(
        self,
        guild: EntityRef,
        channel: EntityRef,
        overwrite_target: EntityRef,
        target_type: str,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        if target_type not in {"role", "member"}:
            raise ValueError("target_type must be 'role' or 'member'")
        self._require_same_authority(guild, channel, label="channel")
        if target_type == "role":
            self._require_same_authority(
                guild, overwrite_target, label="overwrite role"
            )
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}/overwrites/"
            f"{target_type}/{overwrite_target}",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def sync_channel_permissions(
        self,
        guild: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> Channel:
        self._require_same_authority(guild, channel, label="channel")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}/permissions/sync",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )
        return Channel.from_payload(self, origin, raw)

    async def create_role(
        self,
        guild: EntityRef,
        name: str,
        *,
        target: str | None = None,
        reason: str | None = None,
        permissions: int = 0,
        color: int = 0,
        hoist: bool = False,
        mentionable: bool = False,
    ) -> Role:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/roles",
            target=origin,
            json={
                "name": name,
                "permissions": str(permissions),
                "color": color,
                "hoist": hoist,
                "mentionable": mentionable,
            },
            headers=_audit_headers(reason),
        )
        return Role.from_payload(self, origin, raw)

    async def edit_role(
        self,
        guild: EntityRef,
        role: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
        reason: str | None = None,
        name: str | MissingType = MISSING,
        permissions: int | MissingType = MISSING,
        color: int | MissingType = MISSING,
        hoist: bool | MissingType = MISSING,
        mentionable: bool | MissingType = MISSING,
    ) -> Role:
        self._require_same_authority(guild, role, label="role")
        origin = self._authority_target(guild, target)
        body = _provided_fields(
            name=name,
            permissions=(
                str(permissions) if isinstance(permissions, int) else permissions
            ),
            color=color,
            hoist=hoist,
            mentionable=mentionable,
        )
        if not body:
            raise ValueError("at least one role field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/roles/{role}",
            target=origin,
            json=body,
            headers=_merge_headers(_version_headers(version), _audit_headers(reason)),
        )
        return Role.from_payload(self, origin, raw)

    async def reorder_roles(
        self,
        guild: EntityRef,
        positions: list[tuple[Role, int]],
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> list[Role]:
        self._require_same_authority(
            guild, *(role.ref for role, _ in positions), label="role"
        )
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/roles",
            target=origin,
            json={
                "roles": [
                    {
                        "id": str(role.ref.id),
                        "position": position,
                        "version": _version_headers(role.version)["If-Match"],
                    }
                    for role, position in positions
                ]
            },
            headers=_audit_headers(reason),
        )
        return [Role.from_payload(self, origin, item) for item in raw]

    async def delete_role(
        self,
        guild: EntityRef,
        role: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._require_same_authority(guild, role, label="role")
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/roles/{role}",
            target=origin,
            headers=_audit_headers(reason),
        )

    async def add_member_role(
        self,
        guild: EntityRef,
        user: EntityRef,
        role: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._require_same_authority(guild, role, label="role")
        origin = self._authority_target(guild, target)
        await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/members/{user}/roles/{role}",
            target=origin,
            headers=_audit_headers(reason),
        )

    async def remove_member_role(
        self,
        guild: EntityRef,
        user: EntityRef,
        role: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._require_same_authority(guild, role, label="role")
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/members/{user}/roles/{role}",
            target=origin,
            headers=_audit_headers(reason),
        )

    async def set_member_roles(
        self,
        guild: EntityRef,
        user: EntityRef,
        roles: list[EntityRef],
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> Member:
        self._require_same_authority(guild, *roles, label="role")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/members/{user}/roles",
            target=origin,
            json={"role_ids": [str(role) for role in roles]},
            headers=_audit_headers(reason),
        )
        return Member.from_payload(self, origin, raw)

    async def upload_attachment(
        self,
        channel: EntityRef,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        encryption_mode: str = "plaintext",
        encryption_protocol: str | None = None,
        duration_secs: float | None = None,
        waveform: bytes | str | None = None,
    ) -> Attachment:
        duration_secs, waveform_payload = _voice_attachment_metadata(
            content_type=content_type,
            encryption_mode=encryption_mode,
            duration_secs=duration_secs,
            waveform=waveform,
        )
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/attachments",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": encryption_mode,
                "encryption_protocol": encryption_protocol,
                "duration_secs": duration_secs,
                "waveform": waveform_payload,
            },
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        attachment = Attachment.from_payload(self, origin, raw).bind_runtime(
            channel_ref=channel,
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )
        await self._put_upload_ticket(attachment, data, content_type=content_type)
        return attachment

    async def fetch_attachment(
        self,
        attachment: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        channel_ref: EntityRef | None = None,
        dm_capability_id: str | None = None,
    ) -> Attachment:
        origin = self._authority_target(attachment, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/attachments/{attachment}",
            target=origin,
            headers=(
                await self._runtime_grant_headers(
                    channel_ref,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                )
                if channel_ref is not None
                else dict(_installation_headers(installation_id) or {})
                | self._e2ee_device_headers()
            ),
        )
        attachment_model = Attachment.from_payload(self, origin, raw)
        if channel_ref is None:
            return attachment_model
        return attachment_model.bind_runtime(
            channel_ref=channel_ref,
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )

    async def download_attachment(
        self,
        attachment: EntityRef,
        *,
        variant: str = "original",
        target: str | None = None,
        max_bytes: int | None = None,
        installation_id: int | None = None,
        channel_ref: EntityRef | None = None,
        dm_capability_id: str | None = None,
    ) -> bytes:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        if variant not in {
            "original",
            "thumbnail_128",
            "thumbnail_512",
            "thumbnail_1024",
            "poster",
        }:
            raise ValueError("unsupported attachment variant")
        origin = self._authority_target(attachment, target)
        return await self._download_attachment_path(
            f"/api/v1/bots/attachments/{attachment}/{variant}",
            target=origin,
            max_bytes=max_bytes,
            installation_id=installation_id,
            headers=(
                await self._runtime_grant_headers(
                    channel_ref,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                )
                if channel_ref is not None
                else self._e2ee_device_headers()
            ),
        )

    async def _download_attachment_path(
        self,
        path: str,
        *,
        target: str | None,
        max_bytes: int | None,
        installation_id: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        origin = self._request_target(path, target)
        location, media_origin = await self._redirect_location(
            path,
            target=origin,
            headers=dict(_installation_headers(installation_id) or {})
            | (headers or {}),
        )
        from .media_urls import (
            MediaURLValidationError,
            validate_signed_media_url,
        )

        try:
            location = validate_signed_media_url(location, media_origin)
        except MediaURLValidationError:
            raise ApiError(
                502,
                "MEDIA_REDIRECT_INVALID",
                "Media download is not on this instance's safe HTTPS media host",
            ) from None
        async with (
            httpx.AsyncClient(
                timeout=60,
                follow_redirects=False,
                trust_env=False,
            ) as media_client,
            media_client.stream("GET", location) as response,
        ):
            if response.is_redirect:
                redirected = response.headers.get("Location")
                if redirected:
                    try:
                        validate_signed_media_url(
                            urljoin(location, redirected),
                            media_origin,
                        )
                    except MediaURLValidationError:
                        raise ApiError(
                            502,
                            "MEDIA_REDIRECT_INVALID",
                            "Object storage redirected outside this instance's media host",
                        ) from None
                raise ApiError(
                    502,
                    "MEDIA_REDIRECT_INVALID",
                    "Object storage redirected unexpectedly",
                )
            _require_media_response_success(response, operation="download")
            declared = response.headers.get("Content-Length")
            if max_bytes is not None and declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError:
                    raise ApiError(
                        502,
                        "MEDIA_RESPONSE_INVALID",
                        "Object storage returned an invalid content length",
                    ) from None
                if declared_size > max_bytes:
                    raise ValueError("attachment exceeds max_bytes")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if max_bytes is None:
                    body.extend(chunk)
                    continue
                # Retain at most the single byte needed to prove the
                # configured limit was exceeded, then close the stream.
                body.extend(chunk[: max_bytes + 1 - len(body)])
                if len(body) > max_bytes:
                    raise ValueError("attachment exceeds max_bytes")
            return bytes(body)

    async def _put_upload_ticket(
        self,
        attachment: Attachment,
        data: bytes,
        *,
        content_type: str,
    ) -> None:
        if not attachment.upload_url:
            raise ApiError(502, "UPLOAD_TICKET_INVALID", "Upload ticket has no URL")
        from .media_urls import (
            MediaURLValidationError,
            media_url_origin,
            validate_signed_media_url,
            validate_target_media_url,
        )

        try:
            if attachment.media_origin is not None:
                upload_origin = attachment.media_origin
                upload_url = validate_signed_media_url(
                    attachment.upload_url,
                    upload_origin,
                )
            else:
                upload_url = validate_target_media_url(
                    attachment.upload_url,
                    attachment.target,
                )
                upload_origin = media_url_origin(upload_url)
        except MediaURLValidationError:
            raise ApiError(
                502,
                "UPLOAD_TICKET_INVALID",
                "Upload URL is not on this instance's safe HTTPS media host",
            ) from None
        async with httpx.AsyncClient(
            timeout=60, follow_redirects=False, trust_env=False
        ) as upload_client:
            response = await upload_client.put(
                upload_url,
                content=data,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(data)),
                },
            )
        if response.is_redirect:
            redirected = response.headers.get("Location")
            if redirected:
                try:
                    validate_signed_media_url(
                        urljoin(upload_url, redirected),
                        upload_origin,
                    )
                except MediaURLValidationError:
                    raise ApiError(
                        502,
                        "UPLOAD_REDIRECT_REJECTED",
                        "Upload URL redirected outside this instance's media host",
                    ) from None
            raise ApiError(
                502, "UPLOAD_REDIRECT_REJECTED", "Upload URL redirected unexpectedly"
            )
        _require_media_response_success(response, operation="upload")

    async def create_application_asset_ticket(
        self,
        *,
        filename: str,
        content_type: str,
        size: int,
        target: str | None = None,
    ) -> Attachment:
        if size < 1:
            raise ValueError("application asset uploads cannot be empty")
        origin = self._application_home_target(target)
        raw = await self.request(
            "POST",
            "/api/v1/bots/applications/@me/assets/tickets",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": size,
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        return _scoped_attachment_response(
            self,
            origin,
            self.worker_state.application_ref,
            raw,
            expected_channel=None,
            label="application asset attachment",
        )

    async def upload_application_asset(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
    ) -> Attachment:
        ticket = await self.create_application_asset_ticket(
            filename=filename,
            content_type=content_type,
            size=len(data),
            target=target,
        )
        await self._put_upload_ticket(ticket, data, content_type=content_type)
        return ticket

    async def application_assets(
        self, *, target: str | None = None
    ) -> list[ApplicationAsset]:
        origin = self._application_home_target(target)
        raw = await self.request(
            "GET", "/api/v1/bots/applications/@me/assets", target=origin
        )
        assets = [
            _application_asset_response(
                self,
                origin,
                self.worker_state.application_ref,
                item,
            )
            for item in raw
        ]
        return _scoped_resource_list(
            assets,
            resource_ref=lambda asset: asset.ref,
            label="application asset",
        )

    async def fetch_application_asset(
        self, asset_id: int, *, target: str | None = None
    ) -> ApplicationAsset:
        origin = self._application_home_target(target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/applications/@me/assets/{asset_id}",
            target=origin,
        )
        return _application_asset_response(
            self,
            origin,
            self.worker_state.application_ref,
            raw,
            expected_ref=EntityRef(
                asset_id,
                self.worker_state.application_ref.domain,
            ),
        )

    async def edit_application_asset(
        self,
        asset_id: int,
        *,
        target: str | None = None,
        kind: ApplicationAssetKind | MissingType = MISSING,
        name: str | MissingType = MISSING,
    ) -> ApplicationAsset:
        body: dict[str, object] = {}
        if not isinstance(kind, MissingType):
            if kind not in _APPLICATION_ASSET_KINDS:
                raise ValueError("unsupported application asset kind")
            body["kind"] = kind
        if not isinstance(name, MissingType):
            cleaned_name = name.strip()
            if not 1 <= len(cleaned_name) <= 100:
                raise ValueError(
                    "application asset names must contain 1 to 100 characters"
                )
            body["name"] = cleaned_name
        if not body:
            raise ValueError("at least one application asset field is required")
        origin = self._application_home_target(target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/applications/@me/assets/{asset_id}",
            target=origin,
            json=body,
        )
        return _application_asset_response(
            self,
            origin,
            self.worker_state.application_ref,
            raw,
            expected_ref=EntityRef(
                asset_id,
                self.worker_state.application_ref.domain,
            ),
        )

    async def commit_application_asset(
        self,
        attachment: EntityRef,
        kind: ApplicationAssetKind,
        name: str,
        *,
        target: str | None = None,
    ) -> ApplicationAsset | Attachment:
        if kind not in _APPLICATION_ASSET_KINDS:
            raise ValueError("unsupported application asset kind")
        cleaned_name = name.strip()
        if not 1 <= len(cleaned_name) <= 100:
            raise ValueError("application asset names must contain 1 to 100 characters")
        self._require_same_authority(
            self.worker_state.application_ref,
            attachment,
            label="application asset attachment",
        )
        origin = self._application_home_target(target)
        raw = await self.request(
            "POST",
            "/api/v1/bots/applications/@me/assets",
            target=origin,
            json={
                "attachment_id": str(attachment.id),
                "kind": kind,
                "name": cleaned_name,
            },
        )
        if isinstance(raw, dict) and raw.get("application_ref") is not None:
            return _application_asset_response(
                self,
                origin,
                self.worker_state.application_ref,
                raw,
            )
        processing = raw.get("attachment") if isinstance(raw, dict) else None
        if isinstance(processing, dict):
            return _scoped_attachment_response(
                self,
                origin,
                self.worker_state.application_ref,
                processing,
                expected_ref=attachment,
                expected_channel=None,
                label="application asset attachment",
            )
        raise ApiError(
            502,
            "APPLICATION_ASSET_RESPONSE_INVALID",
            "Application asset creation returned an invalid response",
        )

    async def delete_application_asset(
        self, asset_id: int, *, target: str | None = None
    ) -> None:
        origin = self._application_home_target(target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/applications/@me/assets/{asset_id}",
            target=origin,
        )

    async def create_application_emoji_ticket(
        self,
        *,
        filename: str,
        content_type: str,
        size: int,
        target: str | None = None,
    ) -> Attachment:
        if not 1 <= size <= 256 * 1024:
            raise ValueError(
                "application emoji uploads must be between 1 and 262144 bytes"
            )
        origin = self._application_home_target(target)
        raw = await self.request(
            "POST",
            "/api/v1/bots/applications/@me/emojis/tickets",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": size,
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        return _scoped_attachment_response(
            self,
            origin,
            self.worker_state.application_ref,
            raw,
            expected_channel=None,
            label="application emoji attachment",
        )

    async def upload_application_emoji(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
    ) -> Attachment:
        ticket = await self.create_application_emoji_ticket(
            filename=filename,
            content_type=content_type,
            size=len(data),
            target=target,
        )
        await self._put_upload_ticket(ticket, data, content_type=content_type)
        return ticket

    async def application_emojis(
        self, *, target: str | None = None
    ) -> list[ApplicationEmoji]:
        origin = self._application_home_target(target)
        raw = await self.request(
            "GET", "/api/v1/bots/applications/@me/emojis", target=origin
        )
        emojis = [
            _application_emoji_response(
                self,
                origin,
                self.worker_state.application_ref,
                item,
            )
            for item in raw
        ]
        return _scoped_resource_list(
            emojis,
            resource_ref=lambda emoji: emoji.ref,
            label="application emoji",
        )

    async def fetch_application_emoji(
        self, emoji_id: int, *, target: str | None = None
    ) -> ApplicationEmoji:
        origin = self._application_home_target(target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/applications/@me/emojis/{emoji_id}",
            target=origin,
        )
        return _application_emoji_response(
            self,
            origin,
            self.worker_state.application_ref,
            raw,
            expected_ref=EntityRef(
                emoji_id,
                self.worker_state.application_ref.domain,
            ),
        )

    async def commit_application_emoji(
        self,
        attachment: EntityRef,
        name: str,
        *,
        target: str | None = None,
    ) -> ApplicationEmoji | Attachment:
        _expression_name(name)
        self._require_same_authority(
            self.worker_state.application_ref,
            attachment,
            label="application emoji attachment",
        )
        origin = self._application_home_target(target)
        raw = await self.request(
            "POST",
            "/api/v1/bots/applications/@me/emojis",
            target=origin,
            json={"attachment_id": str(attachment.id), "name": name},
        )
        if isinstance(raw, dict) and raw.get("application_ref") is not None:
            return _application_emoji_response(
                self,
                origin,
                self.worker_state.application_ref,
                raw,
            )
        processing = raw.get("attachment") if isinstance(raw, dict) else None
        if isinstance(processing, dict):
            return _scoped_attachment_response(
                self,
                origin,
                self.worker_state.application_ref,
                processing,
                expected_ref=attachment,
                expected_channel=None,
                label="application emoji attachment",
            )
        raise ApiError(
            502,
            "APPLICATION_EMOJI_RESPONSE_INVALID",
            "Application emoji creation returned an invalid response",
        )

    async def edit_application_emoji(
        self,
        emoji_id: int,
        *,
        target: str | None = None,
        name: str,
    ) -> ApplicationEmoji:
        body: dict[str, object] = {"name": _expression_name(name)}
        origin = self._application_home_target(target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/applications/@me/emojis/{emoji_id}",
            target=origin,
            json=body,
        )
        return _application_emoji_response(
            self,
            origin,
            self.worker_state.application_ref,
            raw,
            expected_ref=EntityRef(
                emoji_id,
                self.worker_state.application_ref.domain,
            ),
        )

    async def delete_application_emoji(
        self, emoji_id: int, *, target: str | None = None
    ) -> None:
        origin = self._application_home_target(target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/applications/@me/emojis/{emoji_id}",
            target=origin,
        )

    async def _request_dm_open(
        self,
        handle: str,
        installation_ref: EntityRef,
        installation_type: Literal["guild", "user"],
    ) -> tuple[Channel, _DMCapabilityContext]:
        origin = self._application_home_target()
        expected_handle = _normalized_human_handle(handle)
        raw = await self.request(
            "POST",
            "/api/v1/bots/dms",
            target=origin,
            json={"handle": expected_handle},
            headers=_dm_installation_headers(installation_ref, installation_type),
        )
        if isinstance(raw, dict) and raw.get("status") == "queued":
            raise ApiError(
                502,
                "BOT_DM_GRANT_INVALID",
                "Bot DM opens must complete while their installation proof is fresh",
            )
        channel, context = self._parse_dm_capability_response(raw, origin=origin)
        raw_channel = raw.get("channel", raw) if isinstance(raw, dict) else None
        raw_recipients = (
            raw_channel.get("recipients") if isinstance(raw_channel, dict) else None
        )
        if (
            not isinstance(raw_channel, dict)
            or not isinstance(raw_channel.get("type"), int)
            or isinstance(raw_channel.get("type"), bool)
            or raw_channel["type"] != 1
            or raw_channel.get("guild_ref") is not None
            or raw_channel.get("guild_id") is not None
            or raw_channel.get("guild_domain") is not None
            or not isinstance(raw_recipients, list)
            or len(raw_recipients) != 1
            or not isinstance(raw_recipients[0], dict)
            or channel.type != 1
            or channel.guild_ref is not None
            or channel.conversation_type not in {None, "direct"}
            or len(channel.recipients) != 1
            or _normalized_human_handle(channel.recipients[0].handle) != expected_handle
        ):
            raise ApiError(
                502,
                "BOT_DM_GRANT_INVALID",
                "The application home changed the requested DM recipient",
            )
        if (
            context.installation_ref != installation_ref
            or context.installation_type != installation_type
        ):
            raise ApiError(
                502,
                "BOT_DM_GRANT_INVALID",
                "The application home changed the requested installation lineage",
            )
        return channel, context

    def _parse_dm_capability_response(
        self,
        raw: object,
        *,
        origin: str,
        expected_grant_id: str | None = None,
        expected_channel_ref: EntityRef | None = None,
        expected_context: _DMCapabilityContext | None = None,
        allow_expired: bool = False,
    ) -> tuple[Channel, _DMCapabilityContext]:
        if not isinstance(raw, dict):
            raise ApiError(
                502,
                "BOT_DM_GRANT_INVALID",
                "The application home returned an invalid DM capability",
            )
        raw_channel = raw.get("channel", raw)
        if not isinstance(raw_channel, dict):
            raise ApiError(
                502,
                "BOT_DM_GRANT_INVALID",
                "The application home omitted the DM capability channel",
            )
        channel = Channel.from_payload(self, origin, raw_channel)
        capability_id = raw.get("grant_id", raw.get("bot_dm_capability_id"))
        capability_revision = raw.get("revision", raw.get("bot_dm_capability_revision"))
        lineage_ref_raw = raw.get(
            "lineage_ref",
            raw.get(
                "bot_dm_capability_lineage_ref",
                raw_channel.get("bot_dm_capability_lineage_ref"),
            ),
        )
        expires_at = raw.get("expires_at", raw.get("bot_dm_capability_expires_at"))
        installation_ref_raw = raw.get(
            "installation_ref", raw.get("bot_installation_ref")
        )
        installation_type = raw.get(
            "installation_type", raw.get("bot_installation_type")
        )
        authority_origin_raw = raw.get("authority_origin", channel.target)
        asserted_channel_ref_raw = raw.get("channel_ref")
        if (
            not isinstance(capability_id, str)
            or not isinstance(capability_revision, (int, str))
            or not str(capability_revision).isascii()
            or not str(capability_revision).isdecimal()
            or int(capability_revision) < 1
            or not isinstance(expires_at, str)
            or not isinstance(lineage_ref_raw, str)
            or not isinstance(installation_ref_raw, str)
            or installation_type not in {"guild", "user"}
            or not isinstance(authority_origin_raw, str)
            or (
                asserted_channel_ref_raw is not None
                and not isinstance(asserted_channel_ref_raw, str)
            )
        ):
            raise ApiError(
                502,
                "BOT_DM_GRANT_INVALID",
                "The application home omitted the DM capability binding",
            )
        expiry = datetime.fromisoformat(expires_at)
        if expiry.tzinfo is None:
            raise ApiError(
                502, "BOT_DM_GRANT_INVALID", "The DM capability expiry is invalid"
            )
        if not allow_expired and expiry.timestamp() <= time.time():
            raise ApiError(
                502, "BOT_DM_GRANT_INVALID", "The DM capability is already expired"
            )
        try:
            installation_ref = EntityRef.parse(installation_ref_raw)
            lineage_ref = EntityRef.parse(lineage_ref_raw)
            authority_origin = canonical_target_origin(authority_origin_raw)
            asserted_channel_ref = (
                EntityRef.parse(asserted_channel_ref_raw)
                if isinstance(asserted_channel_ref_raw, str)
                else None
            )
        except ValueError:
            raise ApiError(
                502,
                "BOT_DM_GRANT_INVALID",
                "The DM capability authority binding is invalid",
            ) from None
        if (
            installation_ref.domain is None
            or (
                asserted_channel_ref is not None and asserted_channel_ref != channel.ref
            )
            or lineage_ref.domain != channel.ref.domain
            or urlsplit(authority_origin).hostname != channel.ref.domain
            or channel.target != authority_origin
            or (expected_grant_id is not None and capability_id != expected_grant_id)
            or (
                expected_channel_ref is not None and channel.ref != expected_channel_ref
            )
            or (
                expected_context is not None
                and (
                    installation_ref != expected_context.installation_ref
                    or installation_type != expected_context.installation_type
                    or lineage_ref != expected_context.lineage_ref
                    or authority_origin != expected_context.target
                    or int(capability_revision) < expected_context.revision
                )
            )
        ):
            raise ApiError(
                502,
                "BOT_DM_GRANT_INVALID",
                "The DM capability changed immutable conversation lineage",
            )
        typed_installation_type = cast(Literal["guild", "user"], installation_type)
        channel.bind_runtime(
            dm_capability_id=capability_id,
            dm_capability_revision=int(capability_revision),
            installation_ref=installation_ref,
            installation_type=typed_installation_type,
            reject_unasserted=True,
        )
        return channel, _DMCapabilityContext(
            installation_ref=installation_ref,
            installation_type=typed_installation_type,
            grant_id=capability_id,
            lineage_ref=lineage_ref,
            revision=int(capability_revision),
            expires_at=expiry.timestamp(),
            target=authority_origin,
        )

    async def open_dm(
        self,
        handle: str,
        *,
        installation_ref: EntityRef | None = None,
        installation_type: Literal["guild", "user"] = "guild",
        installation_id: int | None = None,
        target: str | None = None,
    ) -> Channel:
        # DM federation must originate at the bot/application authority. A
        # Guild convenience object may carry a remote guild target, but that
        # instance cannot sign as the application-home bot identity.
        if installation_ref is None:
            if installation_id is None or target is None:
                raise ValueError("a qualified installation_ref is required")
            authority = urlsplit(canonical_target_origin(target)).hostname
            if authority is None:
                raise ValueError("the legacy installation target has no authority")
            installation_ref = EntityRef(installation_id, authority)
        elif installation_id is not None:
            raise ValueError(
                "installation_ref and installation_id are mutually exclusive"
            )
        if installation_ref.domain is None:
            raise ValueError("a qualified installation_ref is required")
        channel, context = await self._request_dm_open(
            handle,
            installation_ref,
            installation_type,
        )
        await self._register_dm_capability(channel, context)
        return channel

    async def create_invite(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
        channel_id: int | None = None,
        max_uses: int | None = None,
        max_age_seconds: int | None = 86_400,
        temporary: bool = False,
        unique: bool = False,
        target_type: Literal["stream"] | None = None,
        target_user_id: EntityRef | None = None,
        scheduled_event_id: EntityRef | None = None,
        role_ids: Sequence[EntityRef] = (),
        target_user_ids: Sequence[EntityRef] = (),
    ) -> Invite:
        if channel_id is not None:
            if isinstance(channel_id, bool) or not isinstance(channel_id, int):
                raise TypeError("channel_id must be an integer")
            if channel_id < 0:
                raise ValueError("channel_id must be non-negative")
        if max_uses is not None:
            if isinstance(max_uses, bool) or not isinstance(max_uses, int):
                raise TypeError("max_uses must be an integer")
            if not 1 <= max_uses <= 100:
                raise ValueError("max_uses must be between 1 and 100")
        if max_age_seconds is not None:
            if isinstance(max_age_seconds, bool) or not isinstance(
                max_age_seconds, int
            ):
                raise TypeError("max_age_seconds must be an integer")
            if not 60 <= max_age_seconds <= 604_800:
                raise ValueError("max_age_seconds must be between 60 and 604800")
        if not isinstance(temporary, bool):
            raise TypeError("temporary must be a boolean")
        if not isinstance(unique, bool):
            raise TypeError("unique must be a boolean")
        if target_type not in {None, "stream"}:
            raise ValueError("unsupported invite target_type")
        if target_type == "stream":
            target_fields_match = target_user_id is not None
        else:
            target_fields_match = target_user_id is None
        if not target_fields_match:
            raise ValueError("invite target fields must match target_type")
        role_refs = _bounded_entity_refs(
            "invite",
            "role_ids",
            role_ids,
            maximum=100,
        )
        target_user_refs = _bounded_entity_refs(
            "invite",
            "target_user_ids",
            target_user_ids,
            maximum=1_000,
        )
        self._require_same_authority(guild, *role_refs, label="invite role")
        if scheduled_event_id is not None:
            self._require_same_authority(
                guild, scheduled_event_id, label="scheduled event"
            )
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/invites",
            target=origin,
            json={
                "channel_id": str(channel_id) if channel_id is not None else None,
                "max_uses": max_uses,
                "max_age_seconds": max_age_seconds,
                "temporary": temporary,
                "unique": unique,
                "target_type": target_type,
                "target_user_id": (
                    str(target_user_id) if target_user_id is not None else None
                ),
                "scheduled_event_id": (
                    str(scheduled_event_id) if scheduled_event_id is not None else None
                ),
                "role_ids": [str(role_ref) for role_ref in role_refs],
                "target_user_ids": [str(user_ref) for user_ref in target_user_refs],
            },
            headers=_audit_headers(reason),
        )
        return _scoped_invite(self, origin, guild, raw)

    async def scheduled_events(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        with_user_count: bool = False,
    ) -> list[ScheduledEvent]:
        if not isinstance(with_user_count, bool):
            raise TypeError("with_user_count must be a boolean")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/scheduled-events",
            target=origin,
            params={"with_user_count": with_user_count},
        )
        events = [_scheduled_event_response(self, origin, guild, item) for item in raw]
        return _scoped_resource_list(
            events,
            resource_ref=lambda event: event.ref,
            label="scheduled event",
        )

    async def fetch_scheduled_event(
        self,
        guild: EntityRef,
        event: EntityRef,
        *,
        target: str | None = None,
        with_user_count: bool = False,
    ) -> ScheduledEvent:
        if not isinstance(with_user_count, bool):
            raise TypeError("with_user_count must be a boolean")
        self._require_same_authority(guild, event, label="scheduled event")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/scheduled-events/{event}",
            target=origin,
            params={"with_user_count": with_user_count},
        )
        return _scheduled_event_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=event,
        )

    async def create_scheduled_event(
        self,
        guild: EntityRef,
        name: str,
        scheduled_start_time: datetime,
        *,
        entity_type: Literal[1, 2, 3],
        target: str | None = None,
        channel: EntityRef | None = None,
        location: str | None = None,
        scheduled_end_time: datetime | None = None,
        description: str | None = None,
        recurrence_rule: ScheduledEventRecurrenceRule | None = None,
        reason: str | None = None,
    ) -> ScheduledEvent:
        cleaned_name = name.strip()
        if not 1 <= len(cleaned_name) <= 100:
            raise ValueError(
                "scheduled event names must be between 1 and 100 characters"
            )
        if isinstance(entity_type, bool) or entity_type not in {1, 2, 3}:
            raise ValueError(
                "entity_type must be 1 (stage), 2 (voice), or 3 (external)"
            )
        if scheduled_start_time.tzinfo is None:
            raise ValueError("scheduled_start_time must include a timezone")
        if scheduled_end_time is not None and scheduled_end_time.tzinfo is None:
            raise ValueError("scheduled_end_time must include a timezone")
        if (
            scheduled_end_time is not None
            and scheduled_end_time <= scheduled_start_time
        ):
            raise ValueError(
                "scheduled_end_time must be later than scheduled_start_time"
            )
        cleaned_location = location.strip() if location is not None else None
        if cleaned_location is not None and not 1 <= len(cleaned_location) <= 100:
            raise ValueError(
                "scheduled event locations must be between 1 and 100 characters"
            )
        if entity_type in {1, 2}:
            if channel is None or location is not None:
                raise ValueError(
                    "stage and voice events require channel and do not accept location"
                )
        elif channel is not None or not cleaned_location or scheduled_end_time is None:
            raise ValueError(
                "external events require location and scheduled_end_time without a channel"
            )
        cleaned_description = description.strip() if description is not None else None
        if (
            cleaned_description is not None
            and not 1 <= len(cleaned_description) <= 1000
        ):
            raise ValueError(
                "scheduled event descriptions must be between 1 and 1000 characters"
            )
        if recurrence_rule is not None:
            if not isinstance(recurrence_rule, ScheduledEventRecurrenceRule):
                raise TypeError(
                    "recurrence_rule must be a ScheduledEventRecurrenceRule"
                )
            if recurrence_rule.start != scheduled_start_time:
                raise ValueError("recurrence start must match scheduled_start_time")
        if channel is not None:
            self._require_same_authority(guild, channel, label="event channel")
        origin = self._authority_target(guild, target)
        body: dict[str, object] = {
            "channel_id": str(channel) if channel is not None else None,
            "entity_metadata": (
                {"location": cleaned_location} if cleaned_location is not None else None
            ),
            "name": cleaned_name,
            "privacy_level": 2,
            "scheduled_start_time": scheduled_start_time.isoformat(),
            "scheduled_end_time": (
                scheduled_end_time.isoformat()
                if scheduled_end_time is not None
                else None
            ),
            "description": cleaned_description,
            "entity_type": entity_type,
        }
        if recurrence_rule is not None:
            body["recurrence_rule"] = recurrence_rule.to_dict()
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/scheduled-events",
            target=origin,
            json=body,
            headers=_audit_headers(reason),
        )
        return _scheduled_event_response(
            self,
            origin,
            guild,
            raw,
            expected_channel=channel,
        )

    async def edit_scheduled_event(
        self,
        guild: EntityRef,
        event: EntityRef,
        *,
        target: str | None = None,
        name: str | MissingType = MISSING,
        channel: EntityRef | None | MissingType = MISSING,
        location: str | None | MissingType = MISSING,
        scheduled_start_time: datetime | MissingType = MISSING,
        scheduled_end_time: datetime | None | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        entity_type: ScheduledEventEntityType | MissingType = MISSING,
        status: ScheduledEventStatus | MissingType = MISSING,
        recurrence_rule: ScheduledEventRecurrenceRule | None | MissingType = MISSING,
        reason: str | None = None,
    ) -> ScheduledEvent:
        self._require_same_authority(guild, event, label="scheduled event")
        if isinstance(channel, EntityRef):
            self._require_same_authority(guild, channel, label="event channel")
        body: dict[str, object] = {}
        if not isinstance(name, MissingType):
            cleaned_name = name.strip()
            if not 1 <= len(cleaned_name) <= 100:
                raise ValueError(
                    "scheduled event names must be between 1 and 100 characters"
                )
            body["name"] = cleaned_name
        if not isinstance(channel, MissingType):
            body["channel_id"] = str(channel) if channel is not None else None
        if not isinstance(location, MissingType):
            if location is None:
                body["entity_metadata"] = None
            else:
                cleaned_location = location.strip()
                if not 1 <= len(cleaned_location) <= 100:
                    raise ValueError(
                        "scheduled event locations must be between 1 and 100 characters"
                    )
                body["entity_metadata"] = {"location": cleaned_location}
        if not isinstance(scheduled_start_time, MissingType):
            if scheduled_start_time.tzinfo is None:
                raise ValueError("scheduled_start_time must include a timezone")
            body["scheduled_start_time"] = scheduled_start_time.isoformat()
        if not isinstance(scheduled_end_time, MissingType):
            if scheduled_end_time is not None and scheduled_end_time.tzinfo is None:
                raise ValueError("scheduled_end_time must include a timezone")
            body["scheduled_end_time"] = (
                scheduled_end_time.isoformat()
                if scheduled_end_time is not None
                else None
            )
        if not isinstance(description, MissingType):
            if description is None:
                body["description"] = None
            else:
                cleaned_description = description.strip()
                if not 1 <= len(cleaned_description) <= 1000:
                    raise ValueError(
                        "scheduled event descriptions must be between 1 and 1000 characters"
                    )
                body["description"] = cleaned_description
        if not isinstance(entity_type, MissingType):
            if isinstance(entity_type, bool) or entity_type not in {1, 2, 3}:
                raise ValueError(
                    "entity_type must be 1 (stage), 2 (voice), or 3 (external)"
                )
            body["entity_type"] = entity_type
        if not isinstance(status, MissingType):
            if isinstance(status, bool) or status not in {1, 2, 3, 4}:
                raise ValueError("scheduled event status must be between 1 and 4")
            body["status"] = status
        if not isinstance(recurrence_rule, MissingType):
            if recurrence_rule is not None and not isinstance(
                recurrence_rule, ScheduledEventRecurrenceRule
            ):
                raise TypeError(
                    "recurrence_rule must be a ScheduledEventRecurrenceRule or None"
                )
            if (
                recurrence_rule is not None
                and not isinstance(scheduled_start_time, MissingType)
                and recurrence_rule.start != scheduled_start_time
            ):
                raise ValueError("recurrence start must match scheduled_start_time")
            body["recurrence_rule"] = (
                recurrence_rule.to_dict() if recurrence_rule is not None else None
            )
        if not body:
            raise ValueError("at least one scheduled event field is required")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/scheduled-events/{event}",
            target=origin,
            json=body,
            headers=_audit_headers(reason),
        )
        return _scheduled_event_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=event,
            expected_channel=channel,
        )

    async def delete_scheduled_event(
        self,
        guild: EntityRef,
        event: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._require_same_authority(guild, event, label="scheduled event")
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/scheduled-events/{event}",
            target=origin,
            headers=_audit_headers(reason),
        )

    async def upload_scheduled_event_image(
        self,
        guild: EntityRef,
        event: EntityRef,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
        reason: str | None = None,
        scan_attempts: int = 45,
    ) -> ScheduledEvent:
        if not data:
            raise ValueError("scheduled event cover uploads cannot be empty")
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("scheduled event cover images can be at most 10 MiB")
        if scan_attempts < 1:
            raise ValueError("scan_attempts must be positive")
        self._require_same_authority(guild, event, label="scheduled event")
        origin = self._authority_target(guild, target)
        raw_ticket = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/scheduled-events/{event}/image/tickets",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        ticket = Attachment.from_payload(self, origin, raw_ticket)
        self._require_same_authority(
            guild, ticket.ref, label="scheduled event attachment"
        )
        await self._put_upload_ticket(ticket, data, content_type=content_type)
        for attempt in range(scan_attempts):
            raw = await self.request(
                "PUT",
                f"/api/v1/bots/guilds/{guild}/scheduled-events/{event}/image",
                target=origin,
                json={"attachment_id": str(ticket.ref.id)},
                headers=_audit_headers(reason),
            )
            if isinstance(raw, dict) and raw.get("guild_id") is not None:
                return _scheduled_event_response(
                    self,
                    origin,
                    guild,
                    raw,
                    expected_ref=event,
                )
            processing = raw.get("attachment") if isinstance(raw, dict) else None
            if not isinstance(processing, dict):
                raise ApiError(
                    502,
                    "SCHEDULED_EVENT_IMAGE_RESPONSE_INVALID",
                    "Scheduled event cover processing returned an invalid response",
                )
            attachment = Attachment.from_payload(self, origin, processing)
            if attachment.scan_status in {"infected", "rejected", "failed"}:
                raise ApiError(
                    422,
                    "SCHEDULED_EVENT_IMAGE_REJECTED",
                    "The scheduled event cover did not pass media safety processing",
                )
            if attempt + 1 < scan_attempts:
                await asyncio.sleep(1)
        raise ApiError(
            504,
            "SCHEDULED_EVENT_IMAGE_PROCESSING_TIMEOUT",
            "Scheduled event cover processing is taking longer than expected",
        )

    async def delete_scheduled_event_image(
        self,
        guild: EntityRef,
        event: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> ScheduledEvent:
        self._require_same_authority(guild, event, label="scheduled event")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/scheduled-events/{event}/image",
            target=origin,
            headers=_audit_headers(reason),
        )
        return _scheduled_event_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=event,
        )

    async def scheduled_event_users(
        self,
        guild: EntityRef,
        event: EntityRef,
        *,
        target: str | None = None,
        limit: int = 100,
        before: EntityRef | None = None,
        after: EntityRef | None = None,
        with_member: bool = False,
    ) -> list[ScheduledEventUser]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not isinstance(with_member, bool):
            raise TypeError("with_member must be a boolean")
        self._require_same_authority(guild, event, label="scheduled event")
        origin = self._authority_target(guild, target)
        params: dict[str, object] = {
            "limit": limit,
            "with_member": with_member,
        }
        if before is not None:
            params["before"] = str(before)
        elif after is not None:
            params["after"] = str(after)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/scheduled-events/{event}/users",
            target=origin,
            params=params,
        )
        users = [ScheduledEventUser.from_payload(self, origin, item) for item in raw]
        if any(
            item.event_ref != event
            or (
                item.member is not None
                and (
                    item.member.guild_ref != guild
                    or item.member.user.ref != item.user.ref
                )
            )
            for item in users
        ):
            raise ValueError(
                "scheduled event user response changed the requested lineage"
            )
        user_refs = [item.user.ref for item in users]
        if len(user_refs) != len(set(user_refs)):
            raise ValueError("scheduled event user response contains duplicate users")
        return users

    async def invites(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Invite]:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/invites", target=origin
        )
        return [_scoped_invite(self, origin, guild, item) for item in raw]

    async def command_permissions(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
    ) -> list[ApplicationCommandPermissions]:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/applications/@me/guilds/{guild}/commands/permissions",
            target=origin,
        )
        if not isinstance(raw, list) or len(raw) > 131:
            raise ValueError("command permission response must be a bounded list")
        scopes = [
            ApplicationCommandPermissions.from_payload(
                item,
                target=origin,
                expected_application_ref=self.worker_state.application_ref,
                expected_guild_ref=guild,
            )
            for item in raw
        ]
        if len({item.ref for item in scopes}) != len(scopes):
            raise ValueError("command permission response repeated a scope")
        return scopes

    async def command_permission(
        self,
        guild: EntityRef,
        command: EntityRef,
        *,
        target: str | None = None,
    ) -> ApplicationCommandPermissions:
        if command.domain is None:
            raise ValueError("a qualified application command ref is required")
        if command.domain != self.worker_state.application_ref.domain:
            raise ValueError("application command authority does not match this bot")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/applications/@me/guilds/{guild}/commands/{command}/permissions",
            target=origin,
        )
        scope = ApplicationCommandPermissions.from_payload(
            raw,
            target=origin,
            expected_application_ref=self.worker_state.application_ref,
            expected_guild_ref=guild,
        )
        if scope.command_ref != command:
            raise ValueError(
                "command permission response changed the requested command"
            )
        return scope

    async def channel_invites(
        self,
        guild: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
    ) -> list[Invite]:
        self._require_same_authority(guild, channel, label="invite channel")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}/invites",
            target=origin,
        )
        return [
            _scoped_invite(self, origin, guild, item, channel=channel) for item in raw
        ]

    async def fetch_invite(
        self,
        guild: EntityRef,
        code: str,
        *,
        target: str | None = None,
    ) -> Invite:
        managed_code = _invite_management_code(guild, code)
        expected_code = (
            managed_code.rpartition("@")[0] if "@" in managed_code else managed_code
        )
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/invites/{managed_code}",
            target=origin,
        )
        invite = _scoped_invite(self, origin, guild, raw)
        if invite.code != expected_code:
            raise ValueError("invite response changed the requested code")
        return invite

    async def revoke_invite(
        self,
        guild: EntityRef,
        code: str,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> Invite:
        managed_code = _invite_management_code(guild, code)
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/invites/{managed_code}",
            target=origin,
            headers=_audit_headers(reason),
        )
        return _scoped_invite(self, origin, guild, raw)

    async def fetch_invite_target_users(
        self,
        guild: EntityRef,
        code: str,
        *,
        target: str | None = None,
    ) -> InviteTargetUsers:
        managed_code = _invite_management_code(guild, code)
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/invites/{managed_code}/target-users",
            target=origin,
        )
        return InviteTargetUsers.from_payload(raw)

    async def update_invite_target_users(
        self,
        guild: EntityRef,
        code: str,
        users: Sequence[EntityRef],
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> InviteTargetUsersJobStatus:
        managed_code = _invite_management_code(guild, code)
        user_refs = _bounded_entity_refs(
            "invite",
            "target_user_ids",
            users,
            maximum=1_000,
        )
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/invites/{managed_code}/target-users",
            target=origin,
            json={"target_user_ids": [str(user_ref) for user_ref in user_refs]},
            headers=_audit_headers(reason),
        )
        return InviteTargetUsersJobStatus.from_payload(raw)

    async def fetch_invite_target_users_job_status(
        self,
        guild: EntityRef,
        code: str,
        *,
        target: str | None = None,
    ) -> InviteTargetUsersJobStatus:
        managed_code = _invite_management_code(guild, code)
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/invites/{managed_code}/target-users/job-status",
            target=origin,
        )
        return InviteTargetUsersJobStatus.from_payload(raw)

    async def create_webhook(
        self,
        guild: EntityRef,
        channel: EntityRef,
        name: str,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> Webhook:
        self._require_same_authority(guild, channel, label="webhook channel")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}/webhooks",
            target=origin,
            json={"name": name},
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )
        return _webhook_response(
            self,
            origin,
            guild,
            raw,
            expected_guild=guild,
            expected_channel=channel,
        )

    async def webhooks(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Webhook]:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/webhooks", target=origin
        )
        hooks = [
            _webhook_response(
                self,
                origin,
                guild,
                item,
                expected_guild=guild,
            )
            for item in raw
        ]
        return _scoped_resource_list(
            hooks,
            resource_ref=lambda webhook: webhook.ref,
            label="webhook",
        )

    async def channel_webhooks(
        self,
        guild: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
    ) -> list[Webhook]:
        self._require_same_authority(guild, channel, label="webhook channel")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}/webhooks",
            target=origin,
        )
        hooks = [
            _webhook_response(
                self,
                origin,
                guild,
                item,
                expected_guild=guild,
                expected_channel=channel,
            )
            for item in raw
        ]
        return _scoped_resource_list(
            hooks,
            resource_ref=lambda webhook: webhook.ref,
            label="webhook",
        )

    async def fetch_webhook(
        self,
        guild: EntityRef,
        webhook_id: int,
        *,
        target: str | None = None,
    ) -> Webhook:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}",
            target=origin,
        )
        return _webhook_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(webhook_id, guild.domain),
            expected_guild=guild,
        )

    async def edit_webhook(
        self,
        guild: EntityRef,
        webhook_id: int,
        *,
        target: str | None = None,
        name: str | MissingType = MISSING,
        avatar_hash: str | None | MissingType = MISSING,
        channel: EntityRef | MissingType = MISSING,
        reason: str | None = None,
    ) -> Webhook:
        if isinstance(channel, EntityRef):
            self._require_same_authority(guild, channel, label="webhook channel")
        origin = self._authority_target(guild, target)
        body = _provided_fields(
            name=name,
            avatar_hash=avatar_hash,
            channel_id=str(channel)
            if not isinstance(channel, MissingType)
            else MISSING,
        )
        if not body:
            raise ValueError("at least one webhook field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}",
            target=origin,
            json=body,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )
        return _webhook_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(webhook_id, guild.domain),
            expected_guild=guild,
            expected_channel=channel if isinstance(channel, EntityRef) else None,
        )

    async def upload_webhook_avatar(
        self,
        guild: EntityRef,
        webhook_id: int,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
        reason: str | None = None,
        scan_attempts: int = 45,
    ) -> Webhook:
        if not data:
            raise ValueError("webhook avatar uploads cannot be empty")
        if scan_attempts < 1:
            raise ValueError("scan_attempts must be positive")
        origin = self._authority_target(guild, target)
        raw_ticket = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}/avatar/tickets",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        ticket = Attachment.from_payload(self, origin, raw_ticket)
        self._require_same_authority(guild, ticket.ref, label="webhook attachment")
        await self._put_upload_ticket(ticket, data, content_type=content_type)
        for attempt in range(scan_attempts):
            raw = await self.request(
                "PUT",
                f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}/avatar",
                target=origin,
                json={"attachment_id": str(ticket.ref.id)},
                headers={"X-Audit-Log-Reason": reason} if reason else None,
            )
            if isinstance(raw, dict) and raw.get("guild_id") is not None:
                return _webhook_response(
                    self,
                    origin,
                    guild,
                    raw,
                    expected_ref=EntityRef(webhook_id, guild.domain),
                    expected_guild=guild,
                )
            processing = raw.get("attachment") if isinstance(raw, dict) else None
            if not isinstance(processing, dict):
                raise ApiError(
                    502,
                    "WEBHOOK_AVATAR_RESPONSE_INVALID",
                    "Webhook avatar processing returned an invalid response",
                )
            attachment = Attachment.from_payload(self, origin, processing)
            if attachment.scan_status in {"infected", "rejected", "failed"}:
                raise ApiError(
                    422,
                    "WEBHOOK_AVATAR_REJECTED",
                    "The webhook avatar did not pass media safety processing",
                )
            if attempt + 1 < scan_attempts:
                await asyncio.sleep(1)
        raise ApiError(
            504,
            "WEBHOOK_AVATAR_PROCESSING_TIMEOUT",
            "Webhook avatar processing is taking longer than expected",
        )

    async def delete_webhook_avatar(
        self,
        guild: EntityRef,
        webhook_id: int,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> Webhook:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}/avatar",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )
        return _webhook_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(webhook_id, guild.domain),
            expected_guild=guild,
        )

    async def rotate_webhook(
        self,
        guild: EntityRef,
        webhook_id: int,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> Webhook:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}/rotate",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )
        return _webhook_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(webhook_id, guild.domain),
            expected_guild=guild,
        )

    async def delete_webhook(
        self,
        guild: EntityRef,
        webhook_id: int,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def fetch_webhook_with_token(
        self,
        webhook_id: int | EntityRef,
        token: str,
        *,
        target: str | None = None,
    ) -> Webhook:
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
        )
        raw = await self.request(
            "GET",
            f"/api/v1/webhooks/{resolved_id}/{token}",
            target=origin,
        )
        webhook = _webhook_response(
            self,
            origin,
            webhook_ref,
            raw,
            expected_ref=webhook_ref,
        )
        webhook.token = token
        return webhook

    async def edit_webhook_with_token(
        self,
        webhook_id: int | EntityRef,
        token: str,
        *,
        target: str | None = None,
        name: str | MissingType = MISSING,
        clear_avatar: bool = False,
    ) -> Webhook:
        body: dict[str, object | None] = {}
        if not isinstance(name, MissingType):
            body["name"] = name
        if clear_avatar:
            body["avatar_hash"] = None
        if not body:
            raise ValueError("at least one webhook field is required")
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
        )
        raw = await self.request(
            "PATCH",
            f"/api/v1/webhooks/{resolved_id}/{token}",
            target=origin,
            json=body,
        )
        webhook = _webhook_response(
            self,
            origin,
            webhook_ref,
            raw,
            expected_ref=webhook_ref,
        )
        webhook.token = token
        return webhook

    async def delete_webhook_with_token(
        self,
        webhook_id: int | EntityRef,
        token: str,
        *,
        target: str | None = None,
    ) -> None:
        resolved_id, origin, _webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
        )
        await self.request(
            "DELETE",
            f"/api/v1/webhooks/{resolved_id}/{token}",
            target=origin,
        )

    async def upload_webhook_avatar_with_token(
        self,
        webhook_id: int | EntityRef,
        token: str,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
        scan_attempts: int = 45,
    ) -> Webhook:
        if not data:
            raise ValueError("webhook avatar uploads cannot be empty")
        if scan_attempts < 1:
            raise ValueError("scan_attempts must be positive")
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
        )
        raw_ticket = await self.request(
            "POST",
            f"/api/v1/webhooks/{resolved_id}/{token}/avatar/tickets",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        ticket = Attachment.from_payload(self, origin, raw_ticket)
        self._require_same_authority(
            webhook_ref,
            ticket.ref,
            label="webhook attachment",
        )
        await self._put_upload_ticket(ticket, data, content_type=content_type)
        for attempt in range(scan_attempts):
            raw = await self.request(
                "PUT",
                f"/api/v1/webhooks/{resolved_id}/{token}/avatar",
                target=origin,
                json={"attachment_id": str(ticket.ref.id)},
            )
            if isinstance(raw, dict) and raw.get("guild_id") is not None:
                webhook = _webhook_response(
                    self,
                    origin,
                    webhook_ref,
                    raw,
                    expected_ref=webhook_ref,
                )
                webhook.token = token
                return webhook
            processing = raw.get("attachment") if isinstance(raw, dict) else None
            if not isinstance(processing, dict):
                raise ApiError(
                    502,
                    "WEBHOOK_AVATAR_RESPONSE_INVALID",
                    "Webhook avatar processing returned an invalid response",
                )
            attachment = Attachment.from_payload(self, origin, processing)
            if attachment.scan_status in {"infected", "rejected", "failed"}:
                raise ApiError(
                    422,
                    "WEBHOOK_AVATAR_REJECTED",
                    "The webhook avatar did not pass media safety processing",
                )
            if attempt + 1 < scan_attempts:
                await asyncio.sleep(1)
        raise ApiError(
            504,
            "WEBHOOK_AVATAR_PROCESSING_TIMEOUT",
            "Webhook avatar processing is taking longer than expected",
        )

    async def delete_webhook_avatar_with_token(
        self,
        webhook_id: int | EntityRef,
        token: str,
        *,
        target: str | None = None,
    ) -> Webhook:
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
        )
        raw = await self.request(
            "DELETE",
            f"/api/v1/webhooks/{resolved_id}/{token}/avatar",
            target=origin,
        )
        webhook = _webhook_response(
            self,
            origin,
            webhook_ref,
            raw,
            expected_ref=webhook_ref,
        )
        webhook.token = token
        return webhook

    @staticmethod
    def _webhook_e2ee_provider_material(
        webhook_ref: EntityRef,
        provider: E2EEProvider,
    ) -> tuple[E2EEProvider, bytes, bytes]:
        real_provider = require_real_e2ee_provider(provider)
        identity_key = real_provider.public_identity_key()
        if len(identity_key) != 32:
            raise E2EEProtocolError(
                "webhook E2EE provider returned an invalid identity key"
            )
        return (
            real_provider,
            identity_key,
            webhook_mls_credential(webhook_ref, identity_key),
        )

    async def create_webhook_e2ee_device_challenge(
        self,
        webhook_ref: EntityRef,
        token: str,
        provider: E2EEProvider,
        *,
        target: str | None = None,
    ) -> WebhookE2EEDeviceChallenge:
        """Create one token-bound, one-use webhook device proof challenge."""

        _provider, identity_key, credential = self._webhook_e2ee_provider_material(
            webhook_ref, provider
        )
        origin = self._authority_target(webhook_ref, target)
        challenge = WebhookE2EEDeviceChallenge.from_payload(
            await self.request(
                "POST",
                f"/api/v1/webhooks/{webhook_ref.id}/{token}/e2ee/devices/challenge",
                target=origin,
                json={
                    "identity_key": _b64(identity_key),
                    "credential_digest": _b64(hashlib.sha256(credential).digest()),
                },
            )
        )
        if challenge.webhook_ref != webhook_ref:
            raise E2EEProtocolError("webhook E2EE challenge identity was substituted")
        return challenge

    async def complete_webhook_e2ee_device_registration(
        self,
        webhook_ref: EntityRef,
        token: str,
        provider: E2EEProvider,
        challenge: WebhookE2EEDeviceChallenge,
        *,
        capabilities: Sequence[str] = ("e2ee-mls/1", "e2ee-media/1"),
        target: str | None = None,
    ) -> WebhookE2EEDevice:
        """Prove the exact MLS identity and register a webhook automation device."""

        if challenge.webhook_ref != webhook_ref:
            raise ValueError("webhook E2EE challenge belongs to another webhook")
        real_provider, identity_key, credential = self._webhook_e2ee_provider_material(
            webhook_ref, provider
        )
        normalized = tuple(sorted(set(capabilities)))
        if (
            len(normalized) != len(capabilities)
            or not set(normalized) <= BOT_E2EE_CAPABILITIES
            or "e2ee-mls/1" not in normalized
        ):
            raise ValueError("webhook E2EE capabilities are invalid")
        signature = real_provider.sign(challenge.signing_input)
        if len(signature) != 64:
            raise E2EEProtocolError(
                "webhook E2EE provider returned an invalid signature"
            )
        origin = self._authority_target(webhook_ref, target)
        device = WebhookE2EEDevice.from_payload(
            await self.request(
                "POST",
                f"/api/v1/webhooks/{webhook_ref.id}/{token}/e2ee/devices",
                target=origin,
                json={
                    "challenge_id": challenge.challenge_id,
                    "identity_key": _b64(identity_key),
                    "credential": _b64(credential),
                    "signature": _b64(signature),
                    "capabilities": list(normalized),
                },
            )
        )
        if (
            device.webhook_ref != webhook_ref
            or not hmac.compare_digest(device.identity_key, identity_key)
            or not hmac.compare_digest(device.credential, credential)
            or device.capabilities != frozenset(normalized)
        ):
            raise E2EEProtocolError("registered webhook E2EE device was substituted")
        return device

    async def register_webhook_e2ee_device(
        self,
        webhook_ref: EntityRef,
        token: str,
        provider: E2EEProvider,
        *,
        capabilities: Sequence[str] = ("e2ee-mls/1", "e2ee-media/1"),
        target: str | None = None,
    ) -> WebhookE2EEDevice:
        challenge = await self.create_webhook_e2ee_device_challenge(
            webhook_ref, token, provider, target=target
        )
        return await self.complete_webhook_e2ee_device_registration(
            webhook_ref,
            token,
            provider,
            challenge,
            capabilities=capabilities,
            target=target,
        )

    async def webhook_e2ee_devices(
        self,
        webhook_ref: EntityRef,
        token: str,
        *,
        target: str | None = None,
    ) -> WebhookE2EEDeviceInventory:
        origin = self._authority_target(webhook_ref, target)
        inventory = WebhookE2EEDeviceInventory.from_payload(
            await self.request(
                "GET",
                f"/api/v1/webhooks/{webhook_ref.id}/{token}/e2ee/devices",
                target=origin,
            )
        )
        if inventory.webhook_ref != webhook_ref:
            raise E2EEProtocolError("webhook E2EE inventory identity was substituted")
        return inventory

    async def upload_webhook_e2ee_key_packages(
        self,
        webhook_ref: EntityRef,
        token: str,
        provider: E2EEProvider,
        device: WebhookE2EEDevice,
        *,
        count: int,
        expires_at: datetime | None = None,
        target: str | None = None,
    ) -> WebhookE2EEKeyPackageResult:
        if isinstance(count, bool) or not 1 <= count <= 50:
            raise ValueError("key-package batches must contain 1 to 50 packages")
        real_provider, identity_key, _credential = self._webhook_e2ee_provider_material(
            webhook_ref, provider
        )
        if device.webhook_ref != webhook_ref or not hmac.compare_digest(
            device.identity_key, identity_key
        ):
            raise ValueError("webhook E2EE device does not belong to this provider")
        expiry = (expires_at or (datetime.now(UTC) + timedelta(days=7))).astimezone(UTC)
        if expiry <= datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("key-package expiry must be more than five minutes away")
        packages = tuple(real_provider.generate_key_package() for _ in range(count))
        if any(not 1 <= len(item) <= 32 * 1024 for item in packages):
            raise E2EEProtocolError(
                "webhook E2EE provider returned an invalid KeyPackage"
            )
        signing_input = webhook_key_package_upload_input(
            protocol_id=device.protocol_id,
            generation=device.generation,
            cipher_suite=MLS_SUITE,
            expires_at=expiry,
            package_hashes=(hashlib.sha256(item).digest() for item in packages),
        )
        signature = real_provider.sign(signing_input)
        if len(signature) != 64:
            raise E2EEProtocolError(
                "webhook E2EE provider returned an invalid signature"
            )
        result = WebhookE2EEKeyPackageResult.from_payload(
            await self.request(
                "POST",
                f"/api/v1/webhooks/{webhook_ref.id}/{token}/e2ee/devices/"
                f"{device.protocol_id}/key-packages",
                target=self._authority_target(webhook_ref, target),
                json={
                    "cipher_suite": MLS_SUITE,
                    "expires_at": expiry.isoformat(),
                    "packages": [_b64(item) for item in packages],
                    "signature": _b64(signature),
                },
            )
        )
        if result.device_id != device.protocol_id:
            raise E2EEProtocolError("webhook key-package identity was substituted")
        return result

    async def replenish_webhook_e2ee_key_packages(
        self,
        webhook_ref: EntityRef,
        token: str,
        provider: E2EEProvider,
        *,
        minimum_available: int = 20,
        desired_available: int = 50,
        expires_at: datetime | None = None,
        target: str | None = None,
    ) -> WebhookE2EEDevice:
        if (
            isinstance(minimum_available, bool)
            or isinstance(desired_available, bool)
            or not 0 <= minimum_available <= desired_available <= 100
        ):
            raise ValueError("webhook E2EE KeyPackage inventory bounds are invalid")
        provider = require_real_e2ee_provider(provider)
        identity_key = provider.public_identity_key()
        inventory = await self.webhook_e2ee_devices(webhook_ref, token, target=target)
        matching = [
            item
            for item in inventory.devices
            if hmac.compare_digest(item.identity_key, identity_key)
        ]
        if len(matching) > 1:
            raise E2EEProtocolError("webhook authority returned duplicate devices")
        device = (
            matching[0]
            if matching
            else await self.register_webhook_e2ee_device(
                webhook_ref, token, provider, target=target
            )
        )
        available = device.available_key_packages
        if available >= minimum_available:
            return device
        while available < desired_available:
            batch = min(50, desired_available - available)
            uploaded = await self.upload_webhook_e2ee_key_packages(
                webhook_ref,
                token,
                provider,
                device,
                count=batch,
                expires_at=expires_at,
                target=target,
            )
            if (
                uploaded.accepted != batch
                or uploaded.available_key_packages <= available
            ):
                raise E2EEProtocolError(
                    "webhook authority did not accept the KeyPackage batch"
                )
            available = uploaded.available_key_packages
        return WebhookE2EEDevice(
            device.webhook_ref,
            device.author_ref,
            device.protocol_id,
            device.identity_key,
            device.credential,
            device.capabilities,
            device.generation,
            available,
        )

    async def webhook_e2ee_participation(
        self,
        guild_ref: EntityRef,
        webhook_ref: EntityRef,
        channel_ref: EntityRef,
        *,
        target: str | None = None,
    ) -> WebhookE2EEParticipationStatus:
        self._require_same_authority(
            guild_ref,
            webhook_ref,
            channel_ref,
            label="webhook E2EE resource",
        )
        origin = self._authority_target(guild_ref, target)
        status = WebhookE2EEParticipationStatus.from_payload(
            await self.request(
                "GET",
                f"/api/v1/bots/guilds/{guild_ref}/webhooks/{webhook_ref.id}/"
                f"e2ee/channels/{channel_ref}",
                target=origin,
            )
        )
        if status.webhook_ref != webhook_ref or status.channel_ref != channel_ref:
            raise E2EEProtocolError(
                "webhook E2EE participation identity was substituted"
            )
        return status

    async def set_webhook_e2ee_participation(
        self,
        guild_ref: EntityRef,
        webhook_ref: EntityRef,
        channel_ref: EntityRef,
        enabled: bool,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> WebhookE2EEParticipationStatus:
        self._require_same_authority(
            guild_ref,
            webhook_ref,
            channel_ref,
            label="webhook E2EE resource",
        )
        origin = self._authority_target(guild_ref, target)
        raw = await self.request(
            "PUT" if enabled else "DELETE",
            f"/api/v1/bots/guilds/{guild_ref}/webhooks/{webhook_ref.id}/"
            f"e2ee/channels/{channel_ref}",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )
        status = WebhookE2EEParticipationStatus.from_payload(raw)
        if status.webhook_ref != webhook_ref or status.channel_ref != channel_ref:
            raise E2EEProtocolError(
                "webhook E2EE participation identity was substituted"
            )
        return status

    async def fetch_webhook_e2ee_control_log(
        self,
        webhook_ref: EntityRef,
        token: str,
        channel_ref: EntityRef,
        device_id: str,
        *,
        after: str | None = None,
        limit: int = 25,
        target: str | None = None,
    ) -> WebhookE2EEControlPage:
        self._require_same_authority(
            webhook_ref,
            channel_ref,
            label="webhook E2EE resource",
        )
        if not 1 <= limit <= 25:
            raise ValueError("webhook E2EE control-log limit must be between 1 and 25")
        origin = self._authority_target(channel_ref, target)
        params: dict[str, object] = {"limit": limit}
        if after is not None:
            cursor = EntityRef.parse(after)
            if cursor.domain != channel_ref.domain:
                raise E2EEProtocolError(
                    "webhook E2EE control cursor authority is invalid"
                )
            params["after"] = after
        page = WebhookE2EEControlPage.from_payload(
            await self.request(
                "GET",
                f"/api/v1/webhooks/{webhook_ref.id}/{token}/e2ee/channels/"
                f"{channel_ref}/control-log",
                target=origin,
                headers={"X-Kaede-E2EE-Device": device_id},
                params=params,
            )
        )
        if (
            page.webhook_ref != webhook_ref
            or page.channel_ref != channel_ref
            or page.device_id != device_id
        ):
            raise E2EEProtocolError("webhook E2EE control-log identity was substituted")
        return page

    async def sync_webhook_e2ee_control_log(
        self,
        webhook_ref: EntityRef,
        token: str,
        device_id: str,
        context: InteractionE2EEContext,
        *,
        after: str | None = None,
        target: str | None = None,
    ) -> str | None:
        """Apply every authority control after the caller's durable cursor."""

        seen: set[str] = set()
        for _page_number in range(256):
            page = await self.fetch_webhook_e2ee_control_log(
                webhook_ref,
                token,
                context.channel_ref,
                device_id,
                after=after,
                target=target,
            )
            for control in page.controls:
                process_e2ee_control(context, control)
                after = control.cursor
            if page.next_after is None:
                return after
            if page.next_after in seen or page.next_after != after:
                raise E2EEProtocolError(
                    "webhook E2EE control-log cursor did not advance"
                )
            seen.add(page.next_after)
        raise E2EEProtocolError("webhook E2EE control log exceeded its recovery bound")

    async def create_webhook_encrypted_forum_reservation(
        self,
        webhook_ref: EntityRef,
        token: str,
        *,
        name: str,
        client_nonce: str,
        device_id: str,
        applied_tag_ids: Sequence[int] = (),
        target: str | None = None,
    ) -> Channel:
        """Reserve an empty forum child before its client-side MLS group exists."""

        origin = self._authority_target(webhook_ref, target)
        raw = await self.request(
            "POST",
            f"/api/v1/webhooks/{webhook_ref.id}/{token}/e2ee/forum-reservations",
            target=origin,
            headers={"X-Kaede-E2EE-Device": device_id},
            json={
                "name": name,
                "applied_tag_ids": [str(item) for item in applied_tag_ids],
                "client_nonce": client_nonce,
            },
        )
        if not isinstance(raw, dict):
            raise E2EEProtocolError("webhook forum reservation response is invalid")
        reservation = raw.get("starter_reservation")
        webhook_e2ee = raw.get("webhook_e2ee")
        channel = Channel.from_payload(self, origin, raw)
        if (
            channel.ref.domain != webhook_ref.domain
            or channel.type not in {10, 11, 12}
            or not channel.e2ee_required
            or channel.encryption_mode != "plaintext"
            or channel.encryption_state != "plaintext"
            or channel.starter_message is not None
            or not isinstance(reservation, dict)
            or reservation != {"client_nonce": client_nonce, "claimed": False}
            or not isinstance(webhook_e2ee, dict)
            or webhook_e2ee.get("device_id") != device_id
            or webhook_e2ee.get("status") not in {"pending", "active"}
        ):
            raise E2EEProtocolError("webhook forum reservation was substituted")
        return channel

    async def propose_webhook_encrypted_forum_room(
        self,
        webhook_ref: EntityRef,
        token: str,
        thread_ref: EntityRef,
        device: WebhookE2EEDevice,
        provider: E2EEProvider,
        operation_id: str,
        *,
        target: str | None = None,
    ) -> WebhookE2EEForumProposal:
        """Claim and authenticate every member KeyPackage for a reserved child."""

        self._require_same_authority(
            webhook_ref,
            thread_ref,
            label="webhook E2EE resource",
        )
        real_provider, identity_key, credential = self._webhook_e2ee_provider_material(
            webhook_ref, provider
        )
        if (
            device.webhook_ref != webhook_ref
            or not hmac.compare_digest(device.identity_key, identity_key)
            or not hmac.compare_digest(device.credential, credential)
        ):
            raise ValueError("webhook E2EE device does not belong to this provider")
        proposal = WebhookE2EEForumProposal.from_payload(
            await self.request(
                "POST",
                f"/api/v1/webhooks/{webhook_ref.id}/{token}/e2ee/channels/"
                f"{thread_ref}/propose",
                target=self._authority_target(thread_ref, target),
                headers={"X-Kaede-E2EE-Device": device.protocol_id},
                json={
                    "operation_id": operation_id,
                    "sender_device_id": device.protocol_id,
                },
            )
        )
        if proposal.operation_id != operation_id or any(
            item.device_id == device.protocol_id for item in proposal.key_packages
        ):
            raise E2EEProtocolError("webhook forum MLS proposal lineage is invalid")
        for package in proposal.key_packages:
            package.verify(real_provider)
        return proposal

    async def activate_webhook_encrypted_forum_room(
        self,
        webhook_ref: EntityRef,
        token: str,
        thread_ref: EntityRef,
        device: WebhookE2EEDevice,
        proposal: WebhookE2EEForumProposal,
        *,
        commit: bytes,
        welcome: bytes,
        target: str | None = None,
    ) -> Channel:
        """Commit the authenticated MLS group at the forum authority."""

        self._require_same_authority(
            webhook_ref,
            thread_ref,
            label="webhook E2EE resource",
        )
        if not commit or not welcome:
            raise ValueError("webhook forum MLS activation controls are required")
        origin = self._authority_target(thread_ref, target)
        raw = await self.request(
            "POST",
            f"/api/v1/webhooks/{webhook_ref.id}/{token}/e2ee/channels/"
            f"{thread_ref}/activate",
            target=origin,
            headers={"X-Kaede-E2EE-Device": device.protocol_id},
            json={
                "operation_id": proposal.operation_id,
                "sender_device_id": device.protocol_id,
                "policy_generation": str(proposal.policy_generation),
                "epoch": "1",
                "group_id": _b64(proposal.group_id),
                "commit": _b64(commit),
                "welcome": _b64(welcome),
                "prepared_vault_revision": str(device.generation),
                "prepared_vault_digest": _b64(
                    hashlib.sha256(device.credential).digest()
                ),
            },
        )
        if not isinstance(raw, dict):
            raise E2EEProtocolError("webhook forum MLS activation response is invalid")
        channel = Channel.from_payload(self, origin, raw)
        if (
            channel.ref != thread_ref
            or raw.get("operation_id") != proposal.operation_id
            or raw.get("operation_status") != "committed"
            or channel.encryption_mode != "e2ee"
            or channel.encryption_state != "active"
            or channel.encryption_policy_generation != proposal.policy_generation
            or channel.encryption_group_id != _b64(proposal.group_id)
            or channel.encryption_epoch != 1
        ):
            raise E2EEProtocolError("webhook forum MLS activation was substituted")
        return channel

    async def claim_webhook_encrypted_forum_starter(
        self,
        webhook_ref: EntityRef,
        token: str,
        thread_ref: EntityRef,
        device_id: str,
        *,
        client_nonce: str,
        e2ee: Mapping[str, object],
        attachment_ids: Sequence[int] = (),
        username: str | None = None,
        avatar_url: str | None = None,
        target: str | None = None,
    ) -> Message:
        """Consume the exact reservation with its first encrypted message."""

        self._require_same_authority(
            webhook_ref,
            thread_ref,
            label="webhook E2EE resource",
        )
        origin = self._authority_target(thread_ref, target)
        raw = await self.request(
            "POST",
            f"/api/v1/webhooks/{webhook_ref.id}/{token}/e2ee/channels/"
            f"{thread_ref}/starter",
            target=origin,
            headers={"X-Kaede-E2EE-Device": device_id},
            json={
                "e2ee": dict(e2ee),
                "client_nonce": client_nonce,
                "attachment_ids": [str(item) for item in attachment_ids],
                "username": username,
                "avatar_url": avatar_url,
            },
        )
        try:
            message = _webhook_message_response(
                self,
                origin,
                webhook_ref,
                raw,
                token=token,
                expected_ref=thread_ref,
                expected_channel=thread_ref,
                e2ee_device_id=device_id,
            )
        except (KeyError, TypeError, ValueError):
            raise E2EEProtocolError("webhook forum starter identity was substituted")
        return message

    async def execute_webhook(
        self,
        webhook_id: int | EntityRef,
        token: str,
        content: str | None = None,
        *,
        target: str | None = None,
        embeds: list[Embed] | None = None,
        view: View | None = None,
        poll: Poll | None = None,
        username: str | None = None,
        avatar_url: str | None = None,
        wait: bool = True,
        thread_id: EntityRef | None = None,
        thread_name: str | None = None,
        applied_tag_ids: Sequence[int] = (),
        attachment_ids: Sequence[int] = (),
        sticker_ids: Sequence[EntityRef] = (),
        flags: int = 0,
        tts: bool = False,
        allowed_mentions: dict[str, Any] | None = None,
        e2ee: Mapping[str, object] | None = None,
        e2ee_device_id: str | None = None,
        with_components: bool | None = None,
        idempotency_key: str | None = None,
    ) -> Message | None:
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
            *((thread_id,) if thread_id is not None else ()),
        )
        body: dict[str, Any] = {
            "content": content,
            "embeds": serialize_embeds(embeds or []),
            "username": username,
            "avatar_url": avatar_url,
            "attachment_ids": [str(item) for item in attachment_ids],
            "sticker_ids": [str(item) for item in sticker_ids],
            "flags": flags,
            "tts": tts,
        }
        if (e2ee is None) != (e2ee_device_id is None):
            raise ValueError("encrypted webhook execution requires its exact device")
        if e2ee is not None:
            if any(
                (
                    content is not None,
                    bool(embeds),
                    view is not None,
                    poll is not None,
                    bool(sticker_ids),
                    tts,
                )
            ):
                raise ValueError(
                    "encrypted webhook content must be carried only in e2ee"
                )
            body["e2ee"] = dict(e2ee)
        if thread_name is not None:
            body["thread_name"] = thread_name
        if applied_tag_ids:
            body["applied_tags"] = [str(item) for item in applied_tag_ids]
        if allowed_mentions is not None:
            body["allowed_mentions"] = dict(allowed_mentions)
        if view is not None:
            body["components"] = view.to_components()
        if poll is not None:
            body["poll"] = poll.to_dict()
        params: dict[str, Any] = {
            "wait": wait,
            "with_components": view is not None
            if with_components is None
            else with_components,
        }
        if thread_id is not None:
            params["thread_id"] = str(thread_id)
        raw = await self.request(
            "POST",
            f"/api/v1/webhooks/{resolved_id}/{token}",
            target=origin,
            params=params,
            json=body,
            headers={
                **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
                **(
                    {"X-Kaede-E2EE-Device": e2ee_device_id}
                    if e2ee_device_id is not None
                    else {}
                ),
            }
            or None,
        )
        return (
            _webhook_message_response(
                self,
                origin,
                webhook_ref,
                raw,
                token=token,
                expected_channel=thread_id,
                e2ee_device_id=e2ee_device_id,
                response_channel_is_thread=thread_name is not None,
            )
            if wait
            else None
        )

    async def upload_webhook_attachment(
        self,
        webhook_id: int | EntityRef,
        token: str,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
        channel_ref: EntityRef | None = None,
        e2ee_device_id: str | None = None,
        encryption_protocol: str | None = None,
        duration_secs: float | None = None,
        waveform: str | None = None,
    ) -> Attachment:
        if (e2ee_device_id is None) != (encryption_protocol is None):
            raise ValueError("encrypted webhook uploads require a device and protocol")
        if encryption_protocol is not None and encryption_protocol != "kaede-file-v1":
            raise ValueError("unsupported webhook attachment encryption protocol")
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
            *((channel_ref,) if channel_ref is not None else ()),
        )
        raw = await self.request(
            "POST",
            f"/api/v1/webhooks/{resolved_id}/{token}/attachments",
            target=origin,
            params={"channel_id": str(channel_ref)}
            if channel_ref is not None
            else None,
            headers=(
                {"X-Kaede-E2EE-Device": e2ee_device_id}
                if e2ee_device_id is not None
                else None
            ),
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "e2ee"
                if e2ee_device_id is not None
                else "plaintext",
                "encryption_protocol": encryption_protocol,
                "duration_secs": duration_secs,
                "waveform": waveform,
            },
        )
        attachment = Attachment.from_payload(self, origin, raw)
        self._require_same_authority(
            webhook_ref,
            attachment.ref,
            label="webhook attachment",
        )
        await self._put_upload_ticket(attachment, data, content_type=content_type)
        return attachment

    async def execute_slack_webhook(
        self,
        webhook_id: int | EntityRef,
        token: str,
        payload: dict[str, Any],
        *,
        target: str | None = None,
        wait: bool = True,
        thread_id: EntityRef | None = None,
    ) -> Message | None:
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
            *((thread_id,) if thread_id is not None else ()),
        )
        raw = await self.request(
            "POST",
            f"/api/v1/webhooks/{resolved_id}/{token}/slack",
            target=origin,
            params={
                "wait": wait,
                **({"thread_id": str(thread_id)} if thread_id is not None else {}),
            },
            json=dict(payload),
        )
        return (
            _webhook_message_response(
                self,
                origin,
                webhook_ref,
                raw,
                token=token,
                expected_channel=thread_id,
            )
            if wait
            else None
        )

    async def execute_github_webhook(
        self,
        webhook_id: int | EntityRef,
        token: str,
        event: str,
        payload: dict[str, Any],
        *,
        target: str | None = None,
        wait: bool = True,
        thread_id: EntityRef | None = None,
        delivery_id: str | None = None,
    ) -> Message | None:
        if not event.strip() or len(event) > 64:
            raise ValueError(
                "GitHub event names must contain between 1 and 64 characters"
            )
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
            *((thread_id,) if thread_id is not None else ()),
        )
        headers = {"X-GitHub-Event": event.casefold()}
        if delivery_id is not None:
            if not 1 <= len(delivery_id) <= 128:
                raise ValueError(
                    "GitHub delivery IDs must contain between 1 and 128 characters"
                )
            headers["X-GitHub-Delivery"] = delivery_id
        raw = await self.request(
            "POST",
            f"/api/v1/webhooks/{resolved_id}/{token}/github",
            target=origin,
            params={
                "wait": wait,
                **({"thread_id": str(thread_id)} if thread_id is not None else {}),
            },
            headers=headers,
            json=dict(payload),
        )
        return (
            _webhook_message_response(
                self,
                origin,
                webhook_ref,
                raw,
                token=token,
                expected_channel=thread_id,
            )
            if wait
            else None
        )

    async def fetch_webhook_message(
        self,
        webhook_id: int | EntityRef,
        token: str,
        message: EntityRef,
        *,
        target: str | None = None,
        thread_id: EntityRef | None = None,
        e2ee_device_id: str | None = None,
    ) -> Message:
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
            message,
            *((thread_id,) if thread_id is not None else ()),
        )
        raw = await self.request(
            "GET",
            f"/api/v1/webhooks/{resolved_id}/{token}/messages/{message}",
            target=origin,
            params={"thread_id": str(thread_id)} if thread_id is not None else None,
        )
        return _webhook_message_response(
            self,
            origin,
            webhook_ref,
            raw,
            token=token,
            expected_ref=message,
            expected_channel=thread_id,
            e2ee_device_id=e2ee_device_id,
        )

    async def edit_webhook_message(
        self,
        webhook_id: int | EntityRef,
        token: str,
        message: EntityRef,
        *,
        target: str | None = None,
        content: str | None | MissingType = MISSING,
        embeds: list[Embed] | None | MissingType = MISSING,
        view: View | None | MissingType = MISSING,
        components: Sequence[dict[str, Any]] | None | MissingType = MISSING,
        attachment_ids: Sequence[int] | None | MissingType = MISSING,
        flags: int | None | MissingType = MISSING,
        allowed_mentions: dict[str, Any] | None | MissingType = MISSING,
        e2ee: Mapping[str, object] | None | MissingType = MISSING,
        e2ee_device_id: str | None = None,
        with_components: bool | None = None,
        thread_id: EntityRef | None = None,
    ) -> Message:
        resolved_id, origin, webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
            message,
            *((thread_id,) if thread_id is not None else ()),
        )
        body: dict[str, Any] = {}
        if not isinstance(content, MissingType):
            body["content"] = content
        if not isinstance(embeds, MissingType):
            body["embeds"] = serialize_embeds(embeds) if embeds is not None else None
        if not isinstance(view, MissingType):
            body["components"] = view.to_components() if view is not None else None
        if not isinstance(components, MissingType):
            if not isinstance(view, MissingType):
                raise ValueError("view and raw components are mutually exclusive")
            body["components"] = list(components) if components is not None else None
        if not isinstance(attachment_ids, MissingType):
            body["attachment_ids"] = (
                [str(item) for item in attachment_ids]
                if attachment_ids is not None
                else None
            )
        if not isinstance(flags, MissingType):
            body["flags"] = flags
        if not isinstance(allowed_mentions, MissingType):
            body["allowed_mentions"] = allowed_mentions
        if not isinstance(e2ee, MissingType):
            body["e2ee"] = dict(e2ee) if e2ee is not None else None
        encrypted_edit = not isinstance(e2ee, MissingType) and e2ee is not None
        if encrypted_edit != (e2ee_device_id is not None):
            raise ValueError("encrypted webhook edits require their exact device")
        if encrypted_edit and any(
            not isinstance(item, MissingType)
            for item in (content, embeds, view, components, allowed_mentions)
        ):
            raise ValueError("encrypted webhook content must be carried only in e2ee")
        include_components = (
            not isinstance(view, MissingType) or not isinstance(components, MissingType)
            if with_components is None
            else with_components
        )
        raw = await self.request(
            "PATCH",
            f"/api/v1/webhooks/{resolved_id}/{token}/messages/{message}",
            target=origin,
            params={
                "with_components": include_components,
                **({"thread_id": str(thread_id)} if thread_id is not None else {}),
            },
            json=body,
            headers=(
                {"X-Kaede-E2EE-Device": e2ee_device_id}
                if e2ee_device_id is not None
                else None
            ),
        )
        return _webhook_message_response(
            self,
            origin,
            webhook_ref,
            raw,
            token=token,
            expected_ref=message,
            expected_channel=thread_id,
            e2ee_device_id=e2ee_device_id,
        )

    async def delete_webhook_message(
        self,
        webhook_id: int | EntityRef,
        token: str,
        message: EntityRef,
        *,
        target: str | None = None,
        thread_id: EntityRef | None = None,
    ) -> None:
        resolved_id, origin, _webhook_ref = self._webhook_token_context(
            webhook_id,
            target,
            message,
            *((thread_id,) if thread_id is not None else ()),
        )
        await self.request(
            "DELETE",
            f"/api/v1/webhooks/{resolved_id}/{token}/messages/{message}",
            target=origin,
            params={"thread_id": str(thread_id)} if thread_id is not None else None,
        )

    async def upload_guild_asset(
        self,
        guild: EntityRef,
        kind: Literal["icon", "banner"],
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
    ) -> Attachment:
        if kind not in {"icon", "banner"}:
            raise ValueError("guild asset kind must be icon or banner")
        if not data:
            raise ValueError("guild asset uploads cannot be empty")
        if content_type not in {"image/gif", "image/jpeg", "image/png", "image/webp"}:
            raise ValueError("guild assets must use a supported image type")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/assets/{kind}",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        attachment = Attachment.from_payload(self, origin, raw)
        await self._put_upload_ticket(attachment, data, content_type=content_type)
        return attachment

    async def commit_guild_asset(
        self,
        guild: EntityRef,
        kind: Literal["icon", "banner"],
        attachment: EntityRef,
        *,
        target: str | None = None,
    ) -> Attachment:
        if kind not in {"icon", "banner"}:
            raise ValueError("guild asset kind must be icon or banner")
        self._require_same_authority(guild, attachment, label="guild asset attachment")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/assets/{kind}",
            target=origin,
            json={"attachment_id": str(attachment.id)},
        )
        return Attachment.from_payload(self, origin, raw)

    async def delete_guild_asset(
        self,
        guild: EntityRef,
        kind: Literal["icon", "banner"],
        *,
        target: str | None = None,
    ) -> Guild:
        if kind not in {"icon", "banner"}:
            raise ValueError("guild asset kind must be icon or banner")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/assets/{kind}",
            target=origin,
        )
        return self._guild_response(origin, raw, expected=guild)

    async def upload_role_icon(
        self,
        guild: EntityRef,
        role: EntityRef,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
    ) -> Attachment:
        if not data or len(data) > 256 * 1024:
            raise ValueError("role icon images must be between 1 and 262144 bytes")
        if content_type not in {"image/gif", "image/jpeg", "image/png", "image/webp"}:
            raise ValueError("role icons must use a supported image type")
        self._require_same_authority(guild, role, label="role")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/roles/{role}/icon",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        attachment = Attachment.from_payload(self, origin, raw)
        await self._put_upload_ticket(attachment, data, content_type=content_type)
        return attachment

    async def commit_role_icon(
        self,
        guild: EntityRef,
        role: EntityRef,
        attachment: EntityRef,
        *,
        target: str | None = None,
    ) -> Role | Attachment:
        self._require_same_authority(guild, role, label="role")
        self._require_same_authority(guild, attachment, label="role icon attachment")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/roles/{role}/icon",
            target=origin,
            json={"attachment_id": str(attachment.id)},
        )
        if isinstance(raw, dict) and raw.get("guild_id") is not None:
            return Role.from_payload(self, origin, raw)
        return Attachment.from_payload(self, origin, raw)

    async def delete_role_icon(
        self,
        guild: EntityRef,
        role: EntityRef,
        *,
        target: str | None = None,
    ) -> Role:
        self._require_same_authority(guild, role, label="role")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/roles/{role}/icon",
            target=origin,
        )
        return Role.from_payload(self, origin, raw)

    async def emojis(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Emoji]:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/emojis", target=origin
        )
        emojis = [_emoji_response(self, origin, guild, item) for item in raw]
        return _scoped_resource_list(
            emojis,
            resource_ref=lambda emoji: emoji.ref,
            label="emoji",
        )

    async def upload_emoji(
        self,
        guild: EntityRef,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
    ) -> Attachment:
        if not data or len(data) > 256 * 1024:
            raise ValueError("guild emoji images must be between 1 and 262144 bytes")
        if content_type not in {"image/gif", "image/jpeg", "image/png", "image/webp"}:
            raise ValueError("guild emoji data must use a supported image type")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/emojis/tickets",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        attachment = _scoped_attachment_response(
            self,
            origin,
            guild,
            raw,
            label="emoji attachment",
        )
        await self._put_upload_ticket(attachment, data, content_type=content_type)
        return attachment

    async def commit_emoji(
        self,
        guild: EntityRef,
        attachment: EntityRef,
        name: str,
        *,
        roles: Sequence[EntityRef] = (),
        target: str | None = None,
        reason: str | None = None,
    ) -> Emoji | Attachment:
        _expression_name(name)
        role_ids = _wire_refs(roles, name="emoji role restrictions", maximum=100)
        self._require_same_authority(guild, attachment, label="emoji attachment")
        self._require_same_authority(guild, *roles, label="emoji role restriction")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/emojis",
            target=origin,
            json={
                "attachment_id": str(attachment.id),
                "name": name,
                "role_ids": role_ids,
            },
            headers=_audit_headers(reason),
        )
        if isinstance(raw, dict) and raw.get("guild_id") is not None:
            return _emoji_response(self, origin, guild, raw)
        return _scoped_attachment_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=attachment,
            label="emoji attachment",
        )

    async def fetch_emoji(
        self,
        guild: EntityRef,
        emoji_id: int,
        *,
        target: str | None = None,
    ) -> Emoji:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/emojis/{emoji_id}",
            target=origin,
        )
        return _emoji_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(emoji_id, guild.domain),
        )

    async def edit_emoji(
        self,
        guild: EntityRef,
        emoji_id: int,
        *,
        target: str | None = None,
        name: str | MissingType = MISSING,
        roles: Sequence[EntityRef] | MissingType = MISSING,
        reason: str | None = None,
    ) -> Emoji:
        body: dict[str, object] = {}
        if not isinstance(name, MissingType):
            body["name"] = _expression_name(name)
        if not isinstance(roles, MissingType):
            body["role_ids"] = _wire_refs(
                roles, name="emoji role restrictions", maximum=100
            )
            self._require_same_authority(guild, *roles, label="emoji role restriction")
        if not body:
            raise ValueError("at least one emoji field is required")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/emojis/{emoji_id}",
            target=origin,
            json=body,
            headers=_audit_headers(reason),
        )
        return _emoji_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(emoji_id, guild.domain),
        )

    async def delete_emoji(
        self,
        guild: EntityRef,
        emoji_id: int,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/emojis/{emoji_id}",
            target=origin,
            headers=_audit_headers(reason),
        )

    async def stickers(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Sticker]:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/stickers", target=origin
        )
        stickers = [_sticker_response(self, origin, guild, item) for item in raw]
        return _scoped_resource_list(
            stickers,
            resource_ref=lambda sticker: sticker.ref,
            label="sticker",
        )

    async def upload_sticker(
        self,
        guild: EntityRef,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        crop: dict[str, float] | None = None,
        remove_background: bool = False,
        target: str | None = None,
    ) -> Attachment:
        if not data or len(data) > 512 * 1024:
            raise ValueError("guild sticker files must be between 1 and 524288 bytes")
        if content_type not in {"image/png", "image/gif"}:
            raise ValueError("guild sticker data must be PNG, APNG, or GIF")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/stickers/tickets",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
                "crop": crop,
                "remove_background": remove_background,
            },
        )
        attachment = _scoped_attachment_response(
            self,
            origin,
            guild,
            raw,
            label="sticker attachment",
        )
        await self._put_upload_ticket(attachment, data, content_type=content_type)
        return attachment

    async def commit_sticker(
        self,
        guild: EntityRef,
        attachment: EntityRef,
        name: str,
        *,
        description: str | None = None,
        tags: Sequence[str] = (),
        target: str | None = None,
        reason: str | None = None,
    ) -> Sticker | Attachment:
        name = _sticker_name(name)
        description = _sticker_description(description)
        self._require_same_authority(guild, attachment, label="sticker attachment")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/stickers",
            target=origin,
            json={
                "attachment_id": str(attachment.id),
                "name": name,
                "description": description,
                "tags": _sticker_tags(tags),
            },
            headers=_audit_headers(reason),
        )
        if isinstance(raw, dict) and raw.get("guild_id") is not None:
            return _sticker_response(self, origin, guild, raw)
        return _scoped_attachment_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=attachment,
            label="sticker attachment",
        )

    async def fetch_sticker(
        self,
        guild: EntityRef,
        sticker_id: int,
        *,
        target: str | None = None,
    ) -> Sticker:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/stickers/{sticker_id}",
            target=origin,
        )
        return _sticker_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(sticker_id, guild.domain),
        )

    async def edit_sticker(
        self,
        guild: EntityRef,
        sticker_id: int,
        *,
        target: str | None = None,
        name: str | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        tags: Sequence[str] | MissingType = MISSING,
        reason: str | None = None,
    ) -> Sticker:
        body: dict[str, object] = {}
        if not isinstance(name, MissingType):
            body["name"] = _sticker_name(name)
        if not isinstance(description, MissingType):
            body["description"] = _sticker_description(description)
        if not isinstance(tags, MissingType):
            if not tags:
                raise ValueError("sticker edits require at least one tag")
            body["tags"] = _sticker_tags(tags)
        if not body:
            raise ValueError("at least one sticker field is required")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/stickers/{sticker_id}",
            target=origin,
            json=body,
            headers=_audit_headers(reason),
        )
        return _sticker_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(sticker_id, guild.domain),
        )

    async def delete_sticker(
        self,
        guild: EntityRef,
        sticker_id: int,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/stickers/{sticker_id}",
            target=origin,
            headers=_audit_headers(reason),
        )

    async def soundboard_sounds(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[SoundboardSound]:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/soundboard-sounds", target=origin
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise RuntimeError("soundboard response is invalid")
        sounds = [
            _soundboard_response(self, origin, guild, item) for item in raw["items"]
        ]
        return _scoped_resource_list(
            sounds,
            resource_ref=lambda sound: sound.ref,
            label="soundboard sound",
        )

    async def default_soundboard_sounds(
        self,
        *,
        target: str | None = None,
    ) -> list[SoundboardSound]:
        origin = self._target(target)
        raw = await self.request(
            "GET",
            "/api/v1/bots/soundboard-default-sounds",
            target=origin,
        )
        if not isinstance(raw, list):
            raise RuntimeError("default soundboard response is invalid")
        sounds = [
            _soundboard_response(
                self,
                origin,
                _target_authority(origin),
                item,
                default=True,
            )
            for item in raw
        ]
        return _scoped_resource_list(
            sounds,
            resource_ref=lambda sound: sound.ref,
            label="default soundboard sound",
        )

    async def fetch_soundboard_sound(
        self,
        guild: EntityRef,
        sound: EntityRef,
        *,
        target: str | None = None,
    ) -> SoundboardSound:
        self._require_same_authority(guild, sound, label="soundboard sound")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/soundboard-sounds/{sound}",
            target=origin,
        )
        return _soundboard_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=sound,
        )

    async def upload_soundboard_sound(
        self,
        guild: EntityRef,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
    ) -> Attachment:
        if not data or len(data) > 512 * 1024:
            raise ValueError("soundboard audio must be between 1 and 524288 bytes")
        if content_type not in {"audio/mpeg", "audio/ogg"}:
            raise ValueError("soundboard audio must be audio/mpeg or audio/ogg")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/soundboard-sounds/tickets",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        attachment = _scoped_attachment_response(
            self,
            origin,
            guild,
            raw,
            label="soundboard attachment",
        )
        await self._put_upload_ticket(attachment, data, content_type=content_type)
        return attachment

    async def commit_soundboard_sound(
        self,
        guild: EntityRef,
        attachment: EntityRef,
        name: str,
        *,
        volume: float = 1,
        emoji: EntityRef | str | None = None,
        target: str | None = None,
        reason: str | None = None,
    ) -> SoundboardSound | Attachment:
        self._require_same_authority(guild, attachment, label="soundboard attachment")
        if isinstance(emoji, EntityRef):
            self._require_same_authority(guild, emoji, label="soundboard emoji")
        origin = self._authority_target(guild, target)
        body: dict[str, object] = {
            "attachment_id": str(attachment.id),
            "name": name,
            "volume": volume,
            "emoji_id": str(emoji.id) if isinstance(emoji, EntityRef) else None,
            "emoji_name": emoji if isinstance(emoji, str) else None,
        }
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/soundboard-sounds",
            target=origin,
            json=body,
            headers=_audit_headers(reason),
        )
        if isinstance(raw, dict) and raw.get("duration_ms") is not None:
            return _soundboard_response(self, origin, guild, raw)
        return _scoped_attachment_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=attachment,
            label="soundboard attachment",
        )

    async def edit_soundboard_sound(
        self,
        guild: EntityRef,
        sound: EntityRef,
        *,
        target: str | None = None,
        name: str | MissingType = MISSING,
        volume: float | MissingType = MISSING,
        emoji: EntityRef | str | None | MissingType = MISSING,
        reason: str | None = None,
    ) -> SoundboardSound:
        self._require_same_authority(guild, sound, label="soundboard sound")
        if isinstance(emoji, EntityRef):
            self._require_same_authority(guild, emoji, label="soundboard emoji")
        origin = self._authority_target(guild, target)
        body = _provided_fields(name=name, volume=volume)
        if not isinstance(emoji, MissingType):
            body["emoji_id"] = str(emoji.id) if isinstance(emoji, EntityRef) else None
            body["emoji_name"] = emoji if isinstance(emoji, str) else None
        if not body:
            raise ValueError("at least one soundboard field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/soundboard-sounds/{sound}",
            target=origin,
            json=body,
            headers=_audit_headers(reason),
        )
        return _soundboard_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=sound,
        )

    async def delete_soundboard_sound(
        self,
        guild: EntityRef,
        sound: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._require_same_authority(guild, sound, label="soundboard sound")
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/soundboard-sounds/{sound}",
            target=origin,
            headers=_audit_headers(reason),
        )

    async def set_voice_moderation(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        server_mute: bool | None = None,
        server_deaf: bool | None = None,
        reason: str | None = None,
    ) -> None:
        if server_mute is None and server_deaf is None:
            raise ValueError("server_mute or server_deaf is required")
        origin = self._authority_target(guild, target)
        await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/members/{user}/voice",
            target=origin,
            json={"server_mute": server_mute, "server_deaf": server_deaf},
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def disconnect_voice(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/members/{user}/voice",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def move_voice(
        self,
        guild: EntityRef,
        user: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._require_same_authority(guild, channel, label="voice channel")
        origin = self._authority_target(guild, target)
        await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/members/{user}/voice/move",
            target=origin,
            json={"channel_id": str(channel)},
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def search_guild_messages(
        self,
        guild: EntityRef,
        query: str = "",
        *,
        target: str | None = None,
        channels: Sequence[EntityRef] = (),
        authors: Sequence[EntityRef] = (),
        mentions: Sequence[EntityRef] = (),
        mention_roles: Sequence[EntityRef] = (),
        mention_everyone: bool | None = None,
        replied_to_users: Sequence[EntityRef] = (),
        replied_to_messages: Sequence[EntityRef] = (),
        has_content: Sequence[str] = (),
        embed_types: Sequence[str] = (),
        embed_providers: Sequence[str] = (),
        link_hostnames: Sequence[str] = (),
        attachment_filenames: Sequence[str] = (),
        attachment_extensions: Sequence[str] = (),
        max_id: EntityRef | None = None,
        min_id: EntityRef | None = None,
        before: datetime | None = None,
        after: datetime | None = None,
        pinned: bool | None = None,
        author_type: Literal["user", "bot", "webhook"] | None = None,
        author_types: Sequence[str] = (),
        sort: Literal["relevance", "newest", "oldest"] = "newest",
        cursor: str | None = None,
        limit: int = 25,
        slop: int = 2,
        include_nsfw: bool = False,
    ) -> MessageSearchPage:
        """Search plaintext guild history at the guild's direct authority.

        Filter limits and signed ``author_types``/``has_content`` values mirror
        Discord's Search Guild Messages contract. End-to-end encrypted channels
        are deliberately excluded because their plaintext is never indexed.
        """

        if not isinstance(query, str) or len(query) > 1_024:
            raise ValueError(
                "message search query must be a string of at most 1024 characters"
            )
        if type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("message search limit must be between 1 and 25")
        if type(slop) is not int or not 0 <= slop <= 100:
            raise ValueError("message search slop must be between 0 and 100")
        if type(pinned) is not bool and pinned is not None:
            raise ValueError("message search pinned must be a boolean or None")
        if type(mention_everyone) is not bool and mention_everyone is not None:
            raise ValueError(
                "message search mention_everyone must be a boolean or None"
            )
        if type(include_nsfw) is not bool:
            raise ValueError("message search include_nsfw must be a boolean")
        if author_type not in {None, "user", "bot", "webhook"}:
            raise ValueError("message search author type is invalid")
        if sort not in {"relevance", "newest", "oldest"}:
            raise ValueError("message search sort is invalid")
        channel_refs = _search_entity_refs("channels", channels, maximum=500)
        author_refs = _search_entity_refs("authors", authors, maximum=100)
        mention_refs = _search_entity_refs("mentions", mentions, maximum=100)
        role_refs = _search_entity_refs("mention roles", mention_roles, maximum=100)
        replied_user_refs = _search_entity_refs(
            "replied-to users", replied_to_users, maximum=100
        )
        replied_message_refs = _search_entity_refs(
            "replied-to messages", replied_to_messages, maximum=100
        )
        content_filters = _search_string_filters(
            "content filters",
            has_content,
            maximum=18,
            item_limit=16,
            allowed=_MESSAGE_SEARCH_HAS_FILTERS,
        )
        checked_embed_types = _search_string_filters(
            "embed types",
            embed_types,
            maximum=5,
            item_limit=16,
            allowed=_MESSAGE_SEARCH_EMBED_TYPES,
        )
        checked_embed_providers = _search_string_filters(
            "embed providers", embed_providers, maximum=100, item_limit=256
        )
        checked_hostnames = _canonical_search_hostnames(link_hostnames)
        checked_filenames = tuple(
            value.strip().casefold()
            for value in _search_string_filters(
                "attachment filenames",
                attachment_filenames,
                maximum=100,
                item_limit=1_024,
            )
        )
        checked_extensions = _canonical_search_extensions(attachment_extensions)
        checked_author_types = _search_string_filters(
            "author types",
            author_types,
            maximum=6,
            item_limit=8,
            allowed=_MESSAGE_SEARCH_AUTHOR_FILTERS,
        )
        if author_type is not None and checked_author_types:
            raise ValueError(
                "message search author_type and author_types are mutually exclusive"
            )
        for values, label in (
            (content_filters, "content"),
            (checked_author_types, "author type"),
        ):
            bases = {value.removeprefix("-") for value in values}
            if any(value in values and f"-{value}" in values for value in bases):
                raise ValueError(f"message search {label} filters conflict")
        for bound, label in ((max_id, "max_id"), (min_id, "min_id")):
            if bound is not None and not isinstance(bound, EntityRef):
                raise ValueError(f"message search {label} must be an entity reference")
        if max_id is not None and min_id is not None and min_id.id >= max_id.id:
            raise ValueError("message search min_id must be less than max_id")
        if any(
            ref.domain != guild.domain
            for ref in (*channel_refs, *role_refs, *replied_message_refs)
        ):
            raise ValueError(
                "message search channel, role, and message filters must use guild authority"
            )
        if any(
            bound is not None and bound.domain != guild.domain
            for bound in (max_id, min_id)
        ):
            raise ValueError("message search id bounds must use guild authority")
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in (before, after)
        ):
            raise ValueError("message search timestamps must include a timezone")
        if before is not None and after is not None and after >= before:
            raise ValueError("message search after must be earlier than before")
        if cursor is not None and len(cursor) > 512:
            raise ValueError("message search cursor is too large")
        filters: dict[str, object] = {
            "channel_ids": [str(item) for item in channel_refs],
            "authors": [str(item) for item in author_refs],
            "mentions": [str(item) for item in mention_refs],
            "mentions_role_ids": [str(item) for item in role_refs],
            "mention_everyone": mention_everyone,
            "replied_to_user_ids": [str(item) for item in replied_user_refs],
            "replied_to_message_ids": [str(item) for item in replied_message_refs],
            "has": list(content_filters),
            "embed_types": list(checked_embed_types),
            "embed_providers": list(checked_embed_providers),
            "link_hostnames": list(checked_hostnames),
            "attachment_filenames": list(checked_filenames),
            "attachment_extensions": list(checked_extensions),
            "max_id": str(max_id) if max_id is not None else None,
            "min_id": str(min_id) if min_id is not None else None,
            "before": before.isoformat() if before is not None else None,
            "after": after.isoformat() if after is not None else None,
            "pinned": pinned,
            "author_type": author_type,
            "author_types": list(checked_author_types),
        }
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/messages/search",
            target=origin,
            json={
                "query": query,
                "scope": "guild",
                "scope_ref": str(guild),
                "filters": filters,
                "sort": sort,
                "cursor": cursor,
                "limit": limit,
                "slop": slop,
                "include_nsfw": include_nsfw,
            },
        )
        page = MessageSearchPage.from_payload(self, origin, raw)
        if any(item.guild.ref != guild for item in page.results) or any(
            item.domain != guild.domain for item in page.encrypted_channel_refs
        ):
            raise ValueError("message search response changed the requested guild")
        return page

    async def _ordinary_message_e2ee_lineage(
        self,
        channel: EntityRef,
        *,
        origin: str,
        installation_id: int | None,
        dm_capability_id: str | None,
    ) -> tuple[
        InteractionE2EEContext,
        BotIdentity,
        Literal["guild_install", "dm_capability"],
        EntityRef,
        int,
    ]:
        """Resolve one exact live grant before encrypting an ordinary message."""

        context = self._interaction_e2ee_contexts.get(channel)
        if context is None:
            raise E2EEProtocolError(
                "no current MLS context is registered for the channel"
            )
        if self.e2ee_device_id is None:
            raise E2EEProtocolError("an exact bot E2EE device must be selected")
        runtime_headers = await self._runtime_grant_headers(
            channel,
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )
        await self._sync_e2ee_control_log(
            context,
            headers=runtime_headers,
            target=origin,
        )
        identity = await self.fetch_bot_identity()
        if installation_id is not None:
            channel_model = await self.fetch_channel(
                channel,
                target=origin,
                installation_id=installation_id,
            )
            if channel_model.guild_ref is None:
                raise E2EEProtocolError(
                    "a guild installation cannot encrypt a DM message"
                )
            guild = await self.fetch_guild(channel_model.guild_ref, target=origin)
            if (
                guild.installation_id != installation_id
                or guild.installation_revision is None
                or guild.installation_revision < 1
            ):
                raise E2EEProtocolError("guild installation lineage is stale")
            return (
                context,
                identity,
                "guild_install",
                EntityRef(installation_id, channel_model.guild_ref.domain),
                guild.installation_revision,
            )
        grant_id = dm_capability_id or self._default_dm_capability_id(channel)
        if grant_id is None:
            raise E2EEProtocolError("encrypted DM messages require an exact capability")
        matches = [
            candidate
            for candidate in self._dm_capabilities.values()
            if candidate.grant_id == grant_id
            and urlsplit(candidate.target).hostname == channel.domain
        ]
        if len(matches) != 1 or matches[0].lineage_ref is None:
            raise E2EEProtocolError("encrypted DM capability lineage is unavailable")
        capability = matches[0]
        lineage_ref = capability.lineage_ref
        if lineage_ref is None:
            raise E2EEProtocolError("encrypted DM capability lineage is unavailable")
        return (
            context,
            identity,
            "dm_capability",
            lineage_ref,
            capability.revision,
        )

    async def send_message(
        self,
        channel: EntityRef,
        content: str | None = None,
        *,
        target: str | None = None,
        reply_to: EntityRef | None = None,
        attachment_ids: list[int] | None = None,
        attachment_manifests: Sequence[Mapping[str, object]] = (),
        sticker_ids: Sequence[EntityRef] = (),
        stickers: Sequence[Sticker] = (),
        mention_user_ids: Sequence[EntityRef] = (),
        resolved_mention_user_ids: Sequence[EntityRef] | None = None,
        allowed_mentions: Mapping[str, object] | None = None,
        replied_user_ref: EntityRef | None = None,
        e2ee: dict[str, Any] | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        embeds: list[Embed] | None = None,
        view: View | None = None,
        poll: Poll | None = None,
        forward: EntityRef | Message | None = None,
        forward_attachments: Sequence[Attachment] = (),
        allow_plaintext_forward_disclosure: bool = False,
        tts: bool = False,
        voice_message: bool = False,
        flags: int = 0,
        client_nonce: str | None = None,
        _starter_claim: bool = False,
    ) -> Message:
        origin = self._authority_target(channel, target)
        automatic_e2ee = e2ee is None and channel in self._interaction_e2ee_contexts
        explicit_e2ee = e2ee is not None
        destination_domain = channel.domain or urlsplit(origin).hostname
        if destination_domain is None:
            raise E2EEProtocolError("destination channel authority is unavailable")
        if replied_user_ref is not None and reply_to is None:
            raise ValueError("replied_user_ref requires reply_to")
        if explicit_e2ee and (
            allowed_mentions is not None or replied_user_ref is not None
        ):
            raise ValueError(
                "an explicit encrypted envelope already binds its allowed mentions"
            )
        mention_policy = _regular_message_allowed_mentions(
            allowed_mentions,
            reply_author=replied_user_ref is not None,
        )
        if forward_attachments:
            if not isinstance(forward, Message) or automatic_e2ee or e2ee is not None:
                raise ValueError(
                    "forward_attachments are only for explicit encrypted-to-plaintext disclosure"
                )
            prepared_attachment_ids = [item.ref.id for item in forward_attachments]
            if any(
                item.ref.domain != destination_domain for item in forward_attachments
            ):
                raise ValueError(
                    "forward attachments must belong to the destination authority"
                )
            if attachment_ids is not None and attachment_ids != prepared_attachment_ids:
                raise ValueError(
                    "forward attachment IDs do not match the prepared uploads"
                )
            attachment_ids = prepared_attachment_ids
        effective_client_nonce = client_nonce
        if forward is not None and effective_client_nonce is None:
            effective_client_nonce = f"forward-{secrets.token_urlsafe(18)}"
        if voice_message and (
            tts
            or content is not None
            or embeds
            or view is not None
            or poll is not None
            or forward is not None
            or len(attachment_ids or []) != 1
            or sticker_ids
        ):
            raise ValueError(
                "a voice message requires exactly one audio attachment and cannot "
                "include content, rich content, forwarding, or encrypted content"
            )
        public_flags = (1 << 2) | (1 << 12) | (1 << 13) | (1 << 15)
        if flags < 0 or flags & ~public_flags:
            raise ValueError("message flags contain server-owned or unsupported bits")
        if flags & (1 << 13) and not voice_message:
            raise ValueError("the voice-message flag requires voice_message=True")
        if len(sticker_ids) > 3 or len(set(sticker_ids)) != len(sticker_ids):
            raise ValueError("sticker_ids must contain at most three unique stickers")
        if stickers and sticker_ids:
            raise ValueError("use stickers or sticker_ids, not both")
        if len(stickers) > 3 or len({item.ref for item in stickers}) != len(stickers):
            raise ValueError("stickers must contain at most three unique stickers")
        if len(mention_user_ids) > 100 or len(set(mention_user_ids)) != len(
            mention_user_ids
        ):
            raise ValueError("mention_user_ids must contain unique references")
        resolved_mentions = (
            list(mention_user_ids)
            if resolved_mention_user_ids is None
            else list(resolved_mention_user_ids)
        )
        if len(resolved_mentions) > 5_000 or len(set(resolved_mentions)) != len(
            resolved_mentions
        ):
            raise ValueError("resolved_mention_user_ids must contain unique references")
        manifests = [dict(item) for item in attachment_manifests]
        if manifests and len(manifests) != len(attachment_ids or []):
            raise ValueError("attachment manifests must match every attachment ID")
        selected_sticker_ids = [item.ref for item in stickers] or list(sticker_ids)
        body: dict[str, Any] = {
            "content": content,
            "attachment_ids": attachment_ids or [],
            "sticker_ids": [str(item) for item in selected_sticker_ids],
            "mention_user_ids": [str(item) for item in resolved_mentions],
            "embeds": serialize_embeds(embeds or []),
            "voice_message": voice_message,
            "flags": flags,
        }
        if (
            not automatic_e2ee
            and not explicit_e2ee
            and (allowed_mentions is not None or replied_user_ref is not None)
        ):
            body["allowed_mentions"] = mention_policy
        if effective_client_nonce is not None:
            body["client_nonce"] = effective_client_nonce
        if _starter_claim and effective_client_nonce is None:
            raise ValueError(
                "an encrypted forum starter claim requires its reservation nonce"
            )
        if tts:
            body["tts"] = True
        if reply_to is not None:
            body["referenced_message_id"] = str(reply_to)
        if view is not None:
            body["components"] = view.to_components()
            body["view_persistent"] = view.is_persistent
            if view.timeout is not None:
                body["view_timeout_seconds"] = max(1, int(view.timeout))
        if poll is not None:
            body["poll"] = poll.to_dict()
        forwarded_message = forward if isinstance(forward, Message) else None
        if (
            forwarded_message is not None
            and isinstance(forwarded_message.e2ee, dict)
            and not automatic_e2ee
            and e2ee is None
            and not allow_plaintext_forward_disclosure
        ):
            raise ValueError(
                "forwarding decrypted E2EE content to plaintext requires explicit disclosure consent"
            )
        if forward is not None:
            body["forwarded_message_id"] = str(
                forward.ref if isinstance(forward, Message) else forward
            )
        if forwarded_message is not None:
            source_origin = self._authority_target(
                forwarded_message.channel_ref,
                forwarded_message.target,
            )
            source_headers = await self._runtime_grant_headers(
                forwarded_message.channel_ref,
                installation_id=forwarded_message.bot_installation_id,
                dm_capability_id=forwarded_message.dm_capability_id,
            )
            proof_response = await self.request(
                "POST",
                (
                    f"/api/v1/bots/channels/{forwarded_message.channel_ref}/messages/"
                    f"{forwarded_message.ref}/forward-authorize"
                ),
                target=source_origin,
                json={
                    "destination_channel_id": f"{channel.id}@{destination_domain}",
                    "destination_encryption_mode": (
                        "e2ee" if automatic_e2ee or e2ee is not None else "plaintext"
                    ),
                    "client_nonce": effective_client_nonce,
                },
                headers=source_headers,
            )
            authorization = proof_response.get("authorization")
            if not isinstance(authorization, Mapping):
                raise E2EEProtocolError(
                    "source authority returned an invalid forward proof"
                )
            body["forward_source_proof"] = dict(authorization)
            if (
                isinstance(forwarded_message.e2ee, dict)
                and not automatic_e2ee
                and e2ee is None
            ):
                body["forward_snapshot"] = build_disclosed_forward_snapshot(
                    forwarded_message,
                    forward_attachments,
                )
        authored_rich_data: dict[str, object] | None = None
        encrypted_result: dict[str, object] | None = None
        if automatic_e2ee:
            if sticker_ids:
                raise ValueError(
                    "automatic encrypted sticker messages require Sticker objects"
                )
            if attachment_ids and not manifests:
                raise ValueError(
                    "automatic encrypted attachments require authenticated manifests"
                )
            if isinstance(forward, EntityRef):
                raise ValueError(
                    "automatic encrypted forwarding requires the decrypted source Message"
                )
            sticker_items: list[dict[str, object]] = []
            for sticker in stickers:
                if sticker.media_hash is None:
                    raise ValueError(
                        "encrypted stickers require canonical media metadata"
                    )
                sticker_items.append(
                    {
                        "id": str(sticker.ref.id),
                        "origin_domain": sticker.ref.domain,
                        "name": sticker.name,
                        "format_type": 2 if sticker.animated else 1,
                        "media_hash": sticker.media_hash,
                    }
                )
            forward_snapshot = (
                build_encrypted_forward_snapshot(
                    forwarded_message,
                    attachment_manifests=manifests,
                )
                if forwarded_message is not None
                else None
            )
            forward_source_projection_digest = (
                (
                    cast(str, forwarded_message.e2ee["forward_projection_digest"])
                    if isinstance(forwarded_message.e2ee, dict)
                    else encrypted_forward_snapshot_digest(forward_snapshot)
                )
                if forwarded_message is not None and forward_snapshot is not None
                else None
            )
            rich_flags = flags | ((1 << 13) if voice_message else 0)
            authored_rich_data = {
                "content": content,
                "embeds": serialize_embeds(embeds or []),
                "components": view.to_components() if view is not None else [],
                "poll": poll.to_dict() if poll is not None else None,
                "sticker_items": sticker_items,
                "tts": tts,
                "voice_message": voice_message,
                "flags": rich_flags,
                "attachments": manifests,
                "allowed_mentions": mention_policy,
                "forward_snapshot": forward_snapshot,
            }
            (
                e2ee_context,
                identity,
                integration_type,
                installation_ref,
                installation_revision,
            ) = await self._ordinary_message_e2ee_lineage(
                channel,
                origin=origin,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            )
            encrypted_mention_refs = list(resolved_mentions)
            if (
                replied_user_ref is not None
                and replied_user_ref not in encrypted_mention_refs
            ):
                encrypted_mention_refs.append(replied_user_ref)
            encrypted = encrypt_message(
                e2ee_context,
                authored_rich_data,
                author_ref=identity.user.ref,
                sender_device_id=cast(str, self.e2ee_device_id),
                application_ref=identity.application_ref,
                interaction_integration_type=integration_type,
                interaction_installation_ref=installation_ref,
                interaction_installation_revision=installation_revision,
                view_version=1 if view is not None and view.rows else 0,
                view_persistent=bool(view and view.is_persistent),
                view_timeout_seconds=(
                    max(1, int(view.timeout))
                    if view is not None and view.timeout is not None
                    else 900
                ),
                mention_refs=encrypted_mention_refs,
                replied_user_ref=replied_user_ref,
                referenced_message_ref=reply_to,
                forwarded_message_ref=(
                    forwarded_message.ref if forwarded_message is not None else None
                ),
                forwarded_channel_ref=(
                    forwarded_message.channel_ref
                    if forwarded_message is not None
                    else None
                ),
                forward_source_projection_digest=(forward_source_projection_digest),
            )
            e2ee = encrypted.envelope
            encrypted_result = encrypted.envelope
            body["flags"] = rich_flags
        if e2ee is not None:
            body["content"] = None
            body["e2ee"] = e2ee
            body["embeds"] = []
            body["components"] = []
            body.pop("poll", None)
            body.pop("allowed_mentions", None)
            body["sticker_ids"] = []
        if _expression_projection(body, default_domain=destination_domain):
            if effective_client_nonce is None:
                effective_client_nonce = secrets.token_urlsafe(24)
                body["client_nonce"] = effective_client_nonce
            body[
                "expression_actor_intents"
            ] = await self._message_expression_actor_intents(
                channel,
                origin,
                body,
                operation="message.create",
                operation_id=effective_client_nonce,
                target_message_ref=None,
                installation_id=installation_id,
            )
        raw = await self.request(
            "POST",
            (
                f"/api/v1/bots/channels/{channel}/starter"
                if _starter_claim
                else f"/api/v1/bots/channels/{channel}/messages"
            ),
            target=origin,
            json=body,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        message = Message.from_payload(self, origin, raw).bind_runtime(
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )
        if encrypted_result is not None:
            if message.e2ee != encrypted_result or authored_rich_data is None:
                raise E2EEProtocolError(
                    "message authority modified the encrypted envelope"
                )
            self._apply_authored_rich_message(message, authored_rich_data)
        if view is not None and view.rows:
            self.add_view(
                view,
                message_id=message.ref,
                target=origin,
                timeout_editor=self._view_timeout_editor(
                    f"/api/v1/bots/channels/{message.channel_ref}/messages/{message.ref}",
                    target=origin,
                    view_version=message.view_version,
                    installation_id=installation_id,
                    channel_ref=channel,
                    dm_capability_id=dm_capability_id,
                ),
            )
        return message

    async def claim_encrypted_forum_starter(
        self,
        thread: EntityRef,
        client_nonce: str,
        content: str | None = None,
        *,
        target: str | None = None,
        attachment_ids: list[int] | None = None,
        attachment_manifests: Sequence[Mapping[str, object]] = (),
        stickers: Sequence[Sticker] = (),
        mention_user_ids: Sequence[EntityRef] = (),
        resolved_mention_user_ids: Sequence[EntityRef] | None = None,
        allowed_mentions: Mapping[str, object] | None = None,
        e2ee: dict[str, Any] | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        embeds: list[Embed] | None = None,
        view: View | None = None,
        poll: Poll | None = None,
        forward: EntityRef | Message | None = None,
        tts: bool = False,
        voice_message: bool = False,
        flags: int = 0,
    ) -> Message:
        """Claim the reserved first message only after the child MLS group is active."""

        origin = self._authority_target(thread, target)
        return await self.send_message(
            thread,
            content,
            target=origin,
            attachment_ids=attachment_ids,
            attachment_manifests=attachment_manifests,
            stickers=stickers,
            mention_user_ids=mention_user_ids,
            resolved_mention_user_ids=resolved_mention_user_ids,
            allowed_mentions=allowed_mentions,
            e2ee=e2ee,
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
            embeds=embeds,
            view=view,
            poll=poll,
            forward=forward,
            tts=tts,
            voice_message=voice_message,
            flags=flags,
            client_nonce=client_nonce,
            _starter_claim=True,
        )

    async def send_sticker(
        self,
        channel: EntityRef,
        sticker: Sticker,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Message:
        """Send a sticker using the ordinary permission-checked message API."""
        origin = self._authority_target(channel, target)
        return await self.send_message(
            channel,
            stickers=[sticker],
            target=origin,
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )

    async def history(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        before: EntityRef | None = None,
        after: EntityRef | None = None,
        around: EntityRef | None = None,
        limit: int = 50,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> list[Message]:
        origin = self._authority_target(channel, target)
        params: dict[str, Any] = {"limit": min(100, max(1, limit))}
        for name, value in (("before", before), ("after", after), ("around", around)):
            if value is not None:
                params[name] = str(value)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/messages",
            target=origin,
            params=params,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        messages = [
            Message.from_payload(self, origin, item).bind_runtime(
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            )
            for item in raw
        ]
        context = self._interaction_e2ee_contexts.get(channel)
        encrypted_messages = [
            item
            for item in messages
            if isinstance(item.e2ee, dict) and "rich_payload_digest" in item.e2ee
        ]
        if encrypted_messages:
            if context is None:
                raise E2EEProtocolError(
                    "encrypted rich history requires a current MLS context"
                )
            await self._sync_e2ee_control_log(
                context,
                headers=await self._runtime_grant_headers(
                    channel,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                ),
                target=origin,
            )
            for item in reversed(encrypted_messages):
                self._apply_decrypted_rich_message(
                    item,
                    decrypt_message(item, context),
                )
        return messages

    async def edit_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        content: str | None | MissingType = MISSING,
        *,
        target: str | None = None,
        embeds: list[Embed] | MissingType = MISSING,
        view: View | MissingType = MISSING,
        components: Sequence[dict[str, Any]] | MissingType = MISSING,
        allowed_mentions: Mapping[str, object] | MissingType = MISSING,
        e2ee: dict[str, Any] | MissingType = MISSING,
        attachment_ids: Sequence[int] | MissingType = MISSING,
        attachment_manifests: Sequence[Mapping[str, object]] | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        view_version: int | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Message:
        origin = self._authority_target(channel, target)
        authored_rich_data: dict[str, object] | None = None
        encrypted_result: dict[str, object] | None = None
        if not isinstance(view, MissingType) and not isinstance(
            components, MissingType
        ):
            raise ValueError("use view or components, not both")
        automatic_e2ee = (
            isinstance(e2ee, MissingType) and channel in self._interaction_e2ee_contexts
        )
        requested_view = view
        if automatic_e2ee:
            current_page = await self.history(
                channel,
                target=origin,
                around=message,
                limit=1,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            )
            current = next((item for item in current_page if item.ref == message), None)
            if current is None:
                raise NotFound(404, "MESSAGE_NOT_FOUND", "Message not found")
            if current.poll is not None:
                raise ValueError("encrypted poll messages cannot be edited")
            current_envelope = current.e2ee
            if not isinstance(current_envelope, dict):
                raise E2EEProtocolError("the current message is not encrypted rich-v2")
            revision_raw = current_envelope.get("message_revision")
            if not isinstance(revision_raw, str) or not revision_raw.isdecimal():
                raise E2EEProtocolError("the current encrypted revision is invalid")
            if not isinstance(content, MissingType):
                merged_content = content
            else:
                merged_content = current.content
            if not isinstance(embeds, MissingType):
                merged_embeds = serialize_embeds(embeds)
            else:
                merged_embeds = [dict(item) for item in current.embeds]
            if not isinstance(view, MissingType):
                merged_components = view.to_components()
                merged_persistent = view.is_persistent
                merged_timeout = (
                    max(1, int(view.timeout)) if view.timeout is not None else 900
                )
            elif not isinstance(components, MissingType):
                merged_components = [dict(item) for item in components]
                merged_persistent = current.view_persistent
                merged_timeout = 900
            else:
                merged_components = [dict(item) for item in current.components]
                merged_persistent = current.view_persistent
                merged_timeout = 900
            if isinstance(attachment_manifests, MissingType):
                merged_manifests = [
                    dict(item.encrypted_manifest)
                    for item in current.attachments
                    if item.encrypted_manifest is not None
                ]
            else:
                merged_manifests = [dict(item) for item in attachment_manifests]
            merged_attachment_ids = (
                [item.ref.id for item in current.attachments]
                if isinstance(attachment_ids, MissingType)
                else list(attachment_ids)
            )
            if len(merged_manifests) != len(merged_attachment_ids):
                raise ValueError(
                    "encrypted edits require a manifest for every retained attachment"
                )
            mutable_flags = (
                current.flags & ((1 << 2) | (1 << 15))
                if isinstance(flags, MissingType)
                else flags
            )
            rich_flags = int(mutable_flags) | (
                (1 << 13) if current.flags & (1 << 13) else 0
            )
            merged_allowed_mentions = _regular_message_allowed_mentions(
                (
                    current.allowed_mentions
                    if isinstance(allowed_mentions, MissingType)
                    else allowed_mentions
                ),
                reply_author=False,
            )
            current_replied_user_ref = (
                EntityRef.parse(cast(str, current_envelope["message_replied_user_ref"]))
                if current_envelope.get("message_replied_user_ref") is not None
                else None
            )
            merged_replied_user_ref = current_replied_user_ref
            if merged_allowed_mentions["replied_user"] is True:
                if merged_replied_user_ref is None:
                    referenced = current.referenced_message
                    merged_replied_user_ref = (
                        referenced.author_ref if referenced is not None else None
                    )
                if merged_replied_user_ref is None:
                    raise ValueError(
                        "encrypted reply mentions require the referenced author projection"
                    )
            else:
                merged_replied_user_ref = None
            authored_rich_data = {
                "content": merged_content,
                "embeds": merged_embeds,
                "components": merged_components,
                "poll": None,
                "sticker_items": [dict(item) for item in current.sticker_items],
                "tts": current.tts,
                "voice_message": bool(current.flags & (1 << 13)),
                "flags": rich_flags,
                "attachments": merged_manifests,
                "allowed_mentions": merged_allowed_mentions,
                "forward_snapshot": (
                    dict(current.forward_snapshot)
                    if current.forward_snapshot is not None
                    else None
                ),
            }
            (
                e2ee_context,
                identity,
                integration_type,
                installation_ref,
                installation_revision,
            ) = await self._ordinary_message_e2ee_lineage(
                channel,
                origin=origin,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            )
            encrypted = encrypt_message(
                e2ee_context,
                authored_rich_data,
                author_ref=identity.user.ref,
                sender_device_id=cast(str, self.e2ee_device_id),
                message_ref=message,
                message_revision=int(revision_raw) + 1,
                application_ref=identity.application_ref,
                interaction_integration_type=integration_type,
                interaction_installation_ref=installation_ref,
                interaction_installation_revision=installation_revision,
                view_version=(
                    current.view_version + 1
                    if current.view_version > 0 or merged_components
                    else 0
                ),
                view_persistent=merged_persistent if merged_components else False,
                view_timeout_seconds=merged_timeout,
                mention_refs=current.mention_user_refs,
                replied_user_ref=merged_replied_user_ref,
                referenced_message_ref=current.referenced_message_ref,
                forwarded_message_ref=current.forwarded_message_ref,
                forwarded_channel_ref=current.forwarded_channel_ref,
                forward_source_projection_digest=cast(
                    str | None,
                    current_envelope.get("forward_source_projection_digest"),
                ),
            )
            e2ee = encrypted.envelope
            encrypted_result = encrypted.envelope
            content = MISSING
            embeds = MISSING
            view = MISSING
            components = MISSING
            allowed_mentions = MISSING
            attachment_ids = merged_attachment_ids
            flags = int(mutable_flags)
        if not isinstance(e2ee, MissingType) and any(
            not isinstance(value, MissingType)
            for value in (content, embeds, view, components, allowed_mentions)
        ):
            raise ValueError(
                "an encrypted edit cannot contain plaintext or rich fields"
            )
        body: dict[str, Any] = {}
        if not isinstance(content, MissingType):
            body["content"] = content
        if not isinstance(embeds, MissingType):
            body["embeds"] = serialize_embeds(embeds)
        if not isinstance(components, MissingType):
            body["components"] = [dict(item) for item in components]
        if not isinstance(allowed_mentions, MissingType):
            body["allowed_mentions"] = _regular_message_allowed_mentions(
                allowed_mentions,
                reply_author=False,
            )
        if not isinstance(e2ee, MissingType):
            body["e2ee"] = e2ee
        if not isinstance(attachment_ids, MissingType):
            body["attachment_ids"] = [str(item) for item in attachment_ids]
        if not isinstance(flags, MissingType):
            body["flags"] = flags
        if not isinstance(view, MissingType):
            body["components"] = view.to_components()
            body["view_persistent"] = view.is_persistent
            if view.is_components_v2:
                body["flags"] = int(body.get("flags", 0)) | (1 << 15)
            if view.timeout is not None:
                body["view_timeout_seconds"] = max(1, int(view.timeout))
            if view_version is not None:
                body["view_version"] = view_version
        if not body:
            raise ValueError("at least one message field is required")
        if _expression_projection(body, default_domain=channel.domain):
            operation_id = hashlib.sha256(
                f"message.edit\n{message}".encode()
            ).hexdigest()
            body[
                "expression_actor_intents"
            ] = await self._message_expression_actor_intents(
                channel,
                origin,
                body,
                operation="message.edit",
                operation_id=operation_id,
                target_message_ref=message,
                installation_id=installation_id,
            )
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/channels/{channel}/messages/{message}",
            target=origin,
            json=body,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        rendered = Message.from_payload(self, origin, raw).bind_runtime(
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )
        if encrypted_result is not None:
            if rendered.e2ee != encrypted_result or authored_rich_data is None:
                raise E2EEProtocolError(
                    "message authority modified the encrypted envelope"
                )
            self._apply_authored_rich_message(rendered, authored_rich_data)
        if not isinstance(requested_view, MissingType):
            view = requested_view
        if not isinstance(view, MissingType):
            if view.rows:
                self.add_view(
                    view,
                    message_id=rendered.ref,
                    target=origin,
                    timeout_editor=self._view_timeout_editor(
                        (
                            f"/api/v1/bots/channels/{rendered.channel_ref}/messages/{rendered.ref}"
                        ),
                        target=origin,
                        view_version=rendered.view_version,
                        installation_id=installation_id,
                        channel_ref=channel,
                        dm_capability_id=dm_capability_id,
                    ),
                )
            else:
                self.remove_view(rendered.ref)
        return rendered

    async def delete_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/messages/{message}",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )

    async def bulk_delete_messages(
        self,
        channel: EntityRef,
        messages: list[EntityRef],
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/messages/bulk-delete",
            target=origin,
            json={"message_ids": [str(item) for item in messages]},
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )

    async def add_reaction(
        self,
        channel: EntityRef,
        message: EntityRef,
        emoji: str,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        expression_actor_intents: dict[str, dict[str, object]] = {}
        expression_body: dict[str, object] = {"content": emoji}
        if _expression_projection(expression_body, default_domain=channel.domain):
            operation_id = hashlib.sha256(
                f"reaction.add\n{message}\n{emoji}".encode()
            ).hexdigest()
            expression_actor_intents = await self._message_expression_actor_intents(
                channel,
                origin,
                expression_body,
                operation="reaction.add",
                operation_id=operation_id,
                target_message_ref=message,
                installation_id=installation_id,
            )
        encoded_emoji = quote(emoji, safe="")
        request_headers = await self._runtime_grant_headers(
            channel,
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )
        if not expression_actor_intents:
            await self.request(
                "PUT",
                f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/"
                f"{encoded_emoji}/@me",
                target=origin,
                headers=request_headers,
            )
            return
        await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions",
            target=origin,
            json={
                "emoji": emoji,
                "expression_actor_intents": expression_actor_intents,
            },
            headers=request_headers,
        )

    async def remove_reaction(
        self,
        channel: EntityRef,
        message: EntityRef,
        emoji: str,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/"
            f"{quote(emoji, safe='')}/@me",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )

    async def remove_user_reaction(
        self,
        channel: EntityRef,
        message: EntityRef,
        user: EntityRef,
        emoji: str,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/"
            f"{quote(emoji, safe='')}/{user}",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )

    async def clear_reactions(
        self,
        channel: EntityRef,
        message: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )

    async def clear_reaction(
        self,
        channel: EntityRef,
        message: EntityRef,
        emoji: str,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/"
            f"{quote(emoji, safe='')}",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )

    async def add_poll_vote(
        self,
        channel: EntityRef,
        message: EntityRef,
        answer_id: int,
        *,
        target: str | None = None,
        installation_id: int | None = None,
    ) -> None:
        raise Forbidden(
            403,
            "BOT_POLL_VOTE_UNSUPPORTED",
            "Applications cannot vote on polls",
        )

    async def remove_poll_vote(
        self,
        channel: EntityRef,
        message: EntityRef,
        answer_id: int,
        *,
        target: str | None = None,
        installation_id: int | None = None,
    ) -> None:
        raise Forbidden(
            403,
            "BOT_POLL_VOTE_UNSUPPORTED",
            "Applications cannot remove poll votes",
        )

    async def poll_voters(
        self,
        channel: EntityRef,
        message: EntityRef,
        answer_id: int,
        *,
        target: str | None = None,
        after: EntityRef | None = None,
        limit: int = 50,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> tuple[list[User], EntityRef | None]:
        if message.domain != channel.domain:
            raise ValueError("poll message authority conflicts with its channel")
        if type(answer_id) is not int or not 1 <= answer_id <= 10:
            raise ValueError("poll answer_id must be an integer between 1 and 10")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("poll voter limit must be an integer between 1 and 100")
        if after is not None and not isinstance(after, EntityRef):
            raise TypeError("poll voter cursor must be an EntityRef")
        origin = self._authority_target(channel, target)
        scope = _PollVoterScope(channel, message, answer_id, origin)
        if after is not None:
            issued = self._poll_voter_cursors.get(id(after))
            if issued is not None and issued[0] is after and issued[1] != scope:
                raise ValueError("poll voter cursor belongs to a different poll")
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = str(after)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/messages/{message}/polls/answers/{answer_id}",
            target=origin,
            params=params,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        if not isinstance(raw, dict) or set(raw) != {"users", "next_after"}:
            raise ValueError("poll voter page response is invalid")
        raw_users = raw["users"]
        if (
            not isinstance(raw_users, list)
            or len(raw_users) > limit
            or any(not isinstance(item, dict) for item in raw_users)
        ):
            raise ValueError("poll voter page response is invalid")
        users = [User.from_payload(cast(dict[str, object], item)) for item in raw_users]
        refs = [user.ref for user in users]
        keys = [(ref.id, ref.domain) for ref in refs]
        if len(refs) != len(set(refs)):
            raise ValueError("poll voter page contains duplicate users")
        if any(current <= previous for previous, current in zip(keys, keys[1:])):
            raise ValueError("poll voter page is not strictly ordered")
        after_key = (after.id, after.domain) if after is not None else None
        if after_key is not None and any(key <= after_key for key in keys):
            raise ValueError("poll voter page did not advance beyond its cursor")
        raw_cursor = raw["next_after"]
        if raw_cursor is None:
            cursor = None
        else:
            try:
                cursor = EntityRef.parse(raw_cursor)
            except ValueError as exc:
                raise ValueError("poll voter page cursor is invalid") from exc
            if len(users) != limit or cursor != refs[-1]:
                raise ValueError("poll voter page cursor does not match its final user")
            if after_key is not None and (cursor.id, cursor.domain) <= after_key:
                raise ValueError("poll voter page cursor did not advance")
            if len(self._poll_voter_cursors) >= 4096:
                self._poll_voter_cursors.pop(next(iter(self._poll_voter_cursors)))
            self._poll_voter_cursors[id(cursor)] = (cursor, scope)
        return users, cursor

    async def finalize_poll(
        self,
        channel: EntityRef,
        message: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Message:
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/messages/{message}/polls/expire",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        rendered = Message.from_payload(self, origin, raw).bind_runtime(
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )
        if rendered.ref != message or rendered.channel_ref != channel:
            raise ValueError("poll finalization response changed the requested message")
        return rendered

    async def resolve_forwarded_message(
        self,
        channel: EntityRef,
        message: EntityRef | Message,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Message | ForwardedMessageReference:
        destination = message if isinstance(message, Message) else None
        message_ref = destination.ref if destination is not None else message
        if destination is not None:
            if destination.channel_ref != channel:
                raise ValueError(
                    "forward destination does not belong to the requested channel"
                )
            installation_id = installation_id or destination.bot_installation_id
            dm_capability_id = dm_capability_id or destination.dm_capability_id
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/messages/{message_ref}/forwarded",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        if not isinstance(raw, dict):
            raise ValueError("forwarded-message response is invalid")
        if set(raw) == {"source_channel_ref", "source_message_ref"}:
            reference = ForwardedMessageReference.from_payload(raw)
            resolved: Message | ForwardedMessageReference = reference
            source_channel = reference.source_channel_ref
            source_message = reference.source_message_ref
        else:
            try:
                source_channel = EntityRef.parse(raw["source_channel_ref"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("forwarded-message response is invalid") from None
            rendered = Message.from_payload(self, origin, raw)
            if rendered.channel_ref != source_channel:
                raise ValueError(
                    "forwarded-message response changed its source channel"
                )
            resolved = rendered
            source_message = rendered.ref
        if destination is not None and (
            destination.forwarded_channel_ref != source_channel
            or destination.forwarded_message_ref != source_message
        ):
            raise ValueError(
                "forwarded-message response changed its saved source lineage"
            )
        return resolved

    async def follow_announcement_channel(
        self,
        source: EntityRef,
        target_channel: EntityRef,
        *,
        target: str | None = None,
    ) -> dict[str, Any]:
        origin = self._authority_target(source, target)
        source_runtime_target = self._authority_target(source)
        runtime_target = self._authority_target(target_channel)
        body: dict[str, object] = {"target_channel_id": str(target_channel)}
        actor_intents: dict[str, dict[str, object]] = {}
        resources = {
            "source_channel": str(source),
            "target_channel": str(target_channel),
        }
        for receiver in {source_runtime_target, runtime_target}:
            receiver_authority = urlsplit(receiver).hostname or receiver
            if receiver_authority == (urlsplit(origin).hostname or origin):
                continue
            actor_intents[receiver_authority] = await self._federated_actor_intent(
                action="announcement.follow.create",
                audience=receiver,
                runtime_target=receiver,
                resources=resources,
            )
        if actor_intents:
            body["actor_intents"] = actor_intents
        return _announcement_follow_response(
            await self.request(
                "POST",
                f"/api/v1/bots/channels/{source}/followers",
                target=origin,
                json=body,
            ),
            source=source,
            target=target_channel,
            require_active=True,
        )

    async def announcement_follows(
        self, source: EntityRef, *, target: str | None = None
    ) -> list[dict[str, Any]]:
        origin = self._authority_target(source, target)
        source_runtime_target = self._authority_target(source)
        headers: dict[str, str] | None = None
        if (urlsplit(origin).hostname or origin) != (
            urlsplit(source_runtime_target).hostname or source_runtime_target
        ):
            source_authority = (
                urlsplit(source_runtime_target).hostname or source_runtime_target
            )
            headers = {
                "X-Kaede-Actor-Intents": json.dumps(
                    {
                        source_authority: await self._federated_actor_intent(
                            action="announcement.follow.list",
                            audience=source_runtime_target,
                            runtime_target=source_runtime_target,
                            resources={"source_channel": str(source)},
                        )
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            }
        return _announcement_follow_page(
            await self.request(
                "GET",
                f"/api/v1/bots/channels/{source}/followers",
                target=origin,
                headers=headers,
            ),
            source=source,
        )

    async def delete_announcement_follow(
        self,
        source: EntityRef,
        follow_id: int | EntityRef,
        *,
        target: str | None = None,
    ) -> None:
        origin = self._authority_target(source, target)
        source_runtime_target = self._authority_target(source)
        follows = await self.announcement_follows(source, target=origin)
        if isinstance(follow_id, EntityRef):
            matches = [item for item in follows if item.get("ref") == str(follow_id)]
        else:
            matches = [
                item for item in follows if str(item.get("id")) == str(follow_id)
            ]
        if not matches:
            raise ApiError(
                404, "CHANNEL_FOLLOW_NOT_FOUND", "Announcement follow not found"
            )
        if len(matches) > 1:
            raise ApiError(
                409,
                "CHANNEL_FOLLOW_REF_REQUIRED",
                "A qualified announcement follow reference is required",
            )
        raw_follow = matches[0]
        try:
            validated_follow = _announcement_follow_response(
                raw_follow,
                source=source,
                require_active=True,
            )
            target_ref = EntityRef.from_wire(
                validated_follow["target_channel_id"],
                validated_follow["target_channel_domain"],
            )
            resolved_follow_ref = EntityRef.parse(validated_follow["ref"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError(
                502,
                "FEDERATED_ANNOUNCEMENT_FOLLOW_INVALID",
                "Source returned an invalid announcement follow",
            ) from exc
        runtime_target = self._authority_target(target_ref)
        headers: dict[str, str] | None = None
        actor_intents: dict[str, dict[str, object]] = {}
        resources = {
            "source_channel": str(source),
            "follow_id": str(resolved_follow_ref),
        }
        for receiver in {source_runtime_target, runtime_target}:
            receiver_authority = urlsplit(receiver).hostname or receiver
            if receiver_authority == (urlsplit(origin).hostname or origin):
                continue
            actor_intents[receiver_authority] = await self._federated_actor_intent(
                action="announcement.follow.delete",
                audience=receiver,
                runtime_target=receiver,
                resources=resources,
            )
        if actor_intents:
            headers = {
                "X-Kaede-Actor-Intents": json.dumps(
                    actor_intents,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            }
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{source}/followers/{resolved_follow_ref}",
            target=origin,
            headers=headers,
        )

    async def crosspost_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> Message:
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/messages/{message}/crosspost",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        rendered = Message.from_payload(self, origin, raw).bind_runtime(
            installation_id=installation_id,
            dm_capability_id=dm_capability_id,
        )
        if rendered.ref != message or rendered.channel_ref != channel:
            raise ValueError("crosspost response changed the requested message")
        return rendered

    async def interaction_callback(
        self,
        interaction_id: int,
        callback_type: int,
        data: dict[str, Any] | None = None,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        user_installation: bool = False,
    ) -> Message | dict[str, Any] | None:
        if type(callback_type) is not int or callback_type not in {4, 5, 6, 7, 8, 9}:
            raise ValueError("unsupported interaction callback type")
        if data is not None and not isinstance(data, dict):
            raise TypeError("interaction callback data must be an object")
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/interactions/{interaction_id}/callback",
            target=origin,
            params={"with_response": "true"},
            json={"type": callback_type, "data": data or {}},
            headers=self._e2ee_device_headers(),
        )
        if raw is None:
            return None

        if not isinstance(raw, dict):
            raise ValueError("interaction callback response is invalid")

        callback_payload = raw
        response_message_ephemeral: bool | None = None
        if {"interaction", "resource"} & raw.keys():
            if set(raw) != {"interaction", "resource"}:
                raise ValueError("interaction callback response wrapper is invalid")
            interaction_metadata = raw["interaction"]
            resource = raw["resource"]
            if not isinstance(interaction_metadata, dict) or not isinstance(
                resource, dict
            ):
                raise ValueError("interaction callback response wrapper is invalid")
            required_metadata = {
                "id",
                "type",
                "response_message_loading",
                "response_message_ephemeral",
            }
            allowed_metadata = required_metadata | {"response_message_id"}
            if (
                not required_metadata <= interaction_metadata.keys()
                or not set(interaction_metadata) <= allowed_metadata
            ):
                raise ValueError("interaction callback metadata is invalid")
            if interaction_metadata.get("id") != str(interaction_id):
                raise ValueError(
                    "interaction callback response changed its interaction"
                )
            interaction_type = interaction_metadata.get("type")
            if type(interaction_type) is not int or interaction_type not in {
                2,
                3,
                4,
                5,
            }:
                raise ValueError("interaction callback interaction type is invalid")
            loading = strict_payload_bool(
                interaction_metadata,
                "response_message_loading",
                default=False,
            )
            response_message_ephemeral = strict_payload_bool(
                interaction_metadata,
                "response_message_ephemeral",
                default=False,
            )
            if loading is not (callback_type == 5):
                raise ValueError("interaction callback loading state is invalid")
            resource_type = resource.get("type")
            if type(resource_type) is not int or resource_type != callback_type:
                raise ValueError("interaction callback response type is invalid")
            if callback_type in {5, 6}:
                if set(resource) != {"type"} or "response_message_id" in (
                    interaction_metadata
                ):
                    raise ValueError(
                        "interaction callback message reference is invalid"
                    )
                return None
            if callback_type in {4, 7}:
                if set(resource) != {"type", "message"}:
                    raise ValueError("interaction callback message resource is invalid")
                resource_message = resource.get("message")
                if not isinstance(resource_message, dict):
                    raise ValueError(
                        "interaction callback response is missing its message"
                    )
                callback_payload = resource_message
                raw_message_id = callback_payload.get("id")
                if (
                    not isinstance(raw_message_id, str)
                    or interaction_metadata.get("response_message_id") != raw_message_id
                ):
                    raise ValueError(
                        "interaction callback message reference is invalid"
                    )
            else:
                # Autocomplete and modal callbacks do not return a Message. Keep
                # their full callback-response object available to low-level users.
                if set(resource) != {"type"} or "response_message_id" in (
                    interaction_metadata
                ):
                    raise ValueError("interaction callback resource is invalid")
                return raw

        if callback_type in {5, 6}:
            # A legacy authority may return a reference body for an otherwise
            # body-less acknowledgement. Validate its identity before hiding it.
            self._bind_interaction_response_dict(
                callback_payload,
                interaction_id,
                target=origin,
                sequence_kind="original",
                expected_callback_type=callback_type,
                slot_kind="original",
                created=True,
            )
            return None
        if callback_type in {8, 9}:
            return callback_payload
        if callback_payload.get("origin_domain") is not None:
            if response_message_ephemeral is True:
                raise ValueError("ephemeral callback materialized a channel message")
            return self._bind_interaction_message(
                Message.from_payload(self, origin, callback_payload),
                interaction_id,
                target=origin,
                kind="original",
                installation_id=installation_id,
                user_installation=user_installation,
            )
        bound = self._bind_interaction_response_dict(
            callback_payload,
            interaction_id,
            target=origin,
            sequence_kind=("original" if callback_type == 4 else "original_or_source"),
            expected_callback_type=callback_type,
            slot_kind="original",
            created=callback_type == 4,
        )
        if response_message_ephemeral is not None and (
            strict_payload_bool(bound, "ephemeral", default=False)
            is not response_message_ephemeral
        ):
            raise ValueError("interaction callback ephemeral state conflicts")
        return bound

    def _bind_interaction_message(
        self,
        message: Message,
        interaction_id: int,
        *,
        target: str,
        kind: Literal["original", "followup"],
        response_id: int | None = None,
        installation_id: int | None = None,
        user_installation: bool = False,
    ) -> Message:
        """Attach trusted, client-side lifecycle authority to one response.

        Interaction endpoints authenticate through the interaction record, not a
        generic channel grant.  The private binding keeps later ``Message``
        conveniences on those endpoints and never accepts lifecycle authority
        from an untrusted response payload.
        """

        if installation_id is not None and user_installation:
            raise ValueError("an interaction cannot combine guild and user installs")
        authority = _target_authority(target)
        if message.ref.domain != authority or message.channel_ref.domain != authority:
            raise ValueError("interaction response changed its authority")
        if (
            message.application_ref is not None
            and message.application_ref != self.worker_state.application_ref
        ):
            raise ValueError("interaction response changed its application")
        if user_installation and (
            message.bot_installation_id is not None
            or message.dm_capability_id is not None
            or message.installation_ref is not None
        ):
            raise ValueError(
                "user-install response contains a conflicting runtime grant"
            )
        if installation_id is not None and message.dm_capability_id is not None:
            raise ValueError("guild response contains a conflicting DM capability")
        interaction_ref = self._interaction_authority_ref(interaction_id, target=target)
        lifecycle = self._interaction_lifecycle_grants.get(interaction_ref)
        active_lifecycle = (
            lifecycle
            if lifecycle is not None and lifecycle.expires_at > time.time()
            else None
        )
        asserted_dm = (
            active_lifecycle.headers.get("X-Kaede-Bot-DM-Capability")
            if active_lifecycle is not None
            else None
        )
        asserted_revision = (
            active_lifecycle.installation_revision
            if active_lifecycle is not None
            else None
        )
        if (
            active_lifecycle is not None
            and message.channel_ref != active_lifecycle.channel_ref
        ):
            raise ValueError("interaction response changed its event channel")
        if (
            installation_id is None
            and not user_installation
            and asserted_dm is None
            and (
                message.bot_installation_id is not None
                or message.dm_capability_id is not None
                or message.installation_ref is not None
            )
        ):
            raise ValueError("interaction response grant was not asserted by its event")
        if asserted_dm is not None:
            headers = active_lifecycle.headers if active_lifecycle is not None else {}
            if (
                message.bot_installation_id is not None
                or message.dm_capability_id != asserted_dm
                or message.dm_capability_revision != asserted_revision
                or message.installation_ref is None
                or str(message.installation_ref)
                != headers.get("X-Kaede-Bot-Source-Installation")
                or message.installation_type
                != headers.get("X-Kaede-Bot-Installation-Type")
            ):
                raise ValueError(
                    "DM interaction response grant conflicts with its event"
                )
        if installation_id is not None:
            message.bind_runtime(installation_id=installation_id)
        response_id = response_id or message.interaction_response_id
        slot = (
            interaction_ref,
            kind,
            response_id if kind == "followup" else None,
        )
        known_identity = self._interaction_response_identities.get(slot)
        if known_identity is not None:
            if response_id not in {None, known_identity.response_ref.id}:
                raise ValueError("interaction response identity changed")
            response_id = known_identity.response_ref.id
        return message.bind_interaction_lifecycle(
            interaction_id,
            kind=kind,
            response_id=response_id,
            user_installation=user_installation,
        )

    async def upload_interaction_attachment(
        self,
        interaction_id: int,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
        encryption_mode: Literal["plaintext", "e2ee"] = "plaintext",
        encryption_protocol: str | None = None,
        duration_secs: float | None = None,
        waveform: bytes | str | None = None,
    ) -> Attachment:
        """Upload media for an interaction response under its room policy.

        The returned attachment is staged until its ID is included in an
        initial response, an edit, or a follow-up for the same interaction.
        """

        if not data:
            raise ValueError("interaction attachment uploads cannot be empty")
        if encryption_mode == "e2ee":
            if self._e2ee_device_id is None:
                raise ValueError(
                    "an encrypted upload requires a configured bot E2EE device"
                )
            encryption_protocol = encryption_protocol or "kaede-file-v1"
            if encryption_protocol != "kaede-file-v1":
                raise ValueError("unsupported encrypted attachment protocol")
        elif encryption_protocol is not None:
            raise ValueError(
                "plaintext attachments cannot declare an encryption protocol"
            )
        duration_secs, waveform_payload = _voice_attachment_metadata(
            content_type=content_type,
            encryption_mode=encryption_mode,
            duration_secs=duration_secs,
            waveform=waveform,
        )
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/interactions/{interaction_id}/attachments",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": encryption_mode,
                "encryption_protocol": encryption_protocol,
                **(
                    {"duration_secs": duration_secs, "waveform": waveform_payload}
                    if waveform_payload is not None
                    else {}
                ),
            },
            headers=self._e2ee_device_headers(),
        )
        attachment = Attachment.from_payload(self, origin, raw)
        await self._put_upload_ticket(attachment, data, content_type=content_type)
        return attachment

    async def fetch_interaction_input_attachment(
        self,
        interaction_id: int,
        attachment: EntityRef,
        *,
        target: str | None = None,
    ) -> Attachment:
        origin = self._target(target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/interactions/{interaction_id}/attachments/{attachment}",
            target=origin,
            headers=self._e2ee_device_headers(),
        )
        return Attachment.from_payload(self, origin, raw)

    async def download_interaction_input_attachment(
        self,
        interaction_id: int,
        attachment: EntityRef,
        *,
        variant: str = "original",
        target: str | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        if variant not in {
            "original",
            "thumbnail_128",
            "thumbnail_512",
            "thumbnail_1024",
            "poster",
        }:
            raise ValueError("unsupported attachment variant")
        return await self._download_attachment_path(
            f"/api/v1/bots/interactions/{interaction_id}/attachments/{attachment}/{variant}",
            target=target,
            max_bytes=max_bytes,
            headers=self._e2ee_device_headers(),
        )

    async def fetch_original_interaction_response(
        self,
        interaction_id: int,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        user_installation: bool = False,
    ) -> Message | dict[str, Any]:
        origin = self._target(target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/interactions/{interaction_id}/responses/@original",
            target=origin,
            headers=self._e2ee_device_headers(),
        )
        if isinstance(raw, dict) and raw.get("origin_domain") is not None:
            return self._bind_interaction_message(
                Message.from_payload(self, origin, raw),
                interaction_id,
                target=origin,
                kind="original",
                installation_id=installation_id,
                user_installation=user_installation,
            )
        return self._bind_interaction_response_dict(
            raw,
            interaction_id,
            target=origin,
            sequence_kind="original",
            slot_kind="original",
        )

    async def edit_original_interaction_response(
        self,
        interaction_id: int,
        *,
        target: str | None = None,
        content: str | None | MissingType = MISSING,
        embeds: list[Embed] | MissingType = MISSING,
        view: View | MissingType = MISSING,
        view_version: int | None = None,
        attachment_ids: Sequence[int] | MissingType = MISSING,
        poll: Poll | MissingType = MISSING,
        e2ee: dict[str, Any] | MissingType = MISSING,
        components: Sequence[dict[str, Any]] | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        allowed_mentions: Mapping[str, object] | MissingType = MISSING,
        installation_id: int | None = None,
        user_installation: bool = False,
    ) -> Message | dict[str, Any]:
        origin = self._target(target)
        if not isinstance(e2ee, MissingType) and any(
            not isinstance(value, MissingType)
            for value in (
                content,
                embeds,
                view,
                poll,
                components,
                allowed_mentions,
            )
        ):
            raise ValueError(
                "an encrypted interaction edit cannot contain plaintext or rich fields"
            )
        body: dict[str, Any] = {}
        if not isinstance(content, MissingType):
            body["content"] = content
        if not isinstance(embeds, MissingType):
            body["embeds"] = serialize_embeds(embeds)
        if not isinstance(attachment_ids, MissingType):
            body["attachment_ids"] = [str(item) for item in attachment_ids]
        if not isinstance(poll, MissingType):
            body["poll"] = poll.to_dict()
        if not isinstance(e2ee, MissingType):
            body["e2ee"] = e2ee
        if not isinstance(flags, MissingType):
            body["flags"] = flags
        if not isinstance(allowed_mentions, MissingType):
            body["allowed_mentions"] = dict(allowed_mentions)
        if not isinstance(components, MissingType):
            if not isinstance(view, MissingType):
                raise ValueError("view and raw components are mutually exclusive")
            rendered_components = list(components)
            body["components"] = rendered_components
            if any(item.get("type") != 1 for item in rendered_components):
                body["flags"] = int(body.get("flags", 0)) | (1 << 15)
        if not isinstance(view, MissingType):
            body["components"] = view.to_components()
            body["view_persistent"] = view.is_persistent
            if view.is_components_v2:
                body["flags"] = int(body.get("flags", 0)) | (1 << 15)
            if view.timeout is not None:
                body["view_timeout_seconds"] = max(1, int(view.timeout))
            if view_version is not None:
                body["view_version"] = view_version
        elif not isinstance(components, MissingType) and view_version is not None:
            body["view_version"] = view_version
        elif view_version is not None:
            raise ValueError("view_version requires a components update")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/interactions/{interaction_id}/responses/@original",
            target=origin,
            json=body,
            headers=self._e2ee_device_headers(),
        )
        if isinstance(raw, dict) and raw.get("origin_domain") is not None:
            rendered = self._bind_interaction_message(
                Message.from_payload(self, origin, raw),
                interaction_id,
                target=origin,
                kind="original",
                installation_id=installation_id,
                user_installation=user_installation,
            )
            if not isinstance(view, MissingType):
                if view.rows:
                    self.add_view(
                        view,
                        message_id=rendered.ref,
                        target=origin,
                        timeout_editor=self._view_timeout_editor(
                            (
                                f"/api/v1/bots/interactions/{interaction_id}/responses/@original"
                            ),
                            target=origin,
                            view_version=rendered.view_version,
                        ),
                    )
                else:
                    self.remove_view(rendered.ref)
            return rendered
        bound = self._bind_interaction_response_dict(
            raw,
            interaction_id,
            target=origin,
            sequence_kind="original_or_source",
            slot_kind="original",
            mutated=True,
        )
        if not isinstance(view, MissingType) and bound.get("id") is not None:
            response_id = EntityRef.from_wire(bound["id"], _target_authority(origin)).id
            if view.rows:
                self.add_view(
                    view,
                    response_id=response_id,
                    target=origin,
                    timeout_editor=self._view_timeout_editor(
                        f"/api/v1/bots/interactions/{interaction_id}/responses/@original",
                        target=origin,
                        view_version=int(bound.get("view_version", 0) or 0),
                    ),
                )
            else:
                self.remove_response_view(response_id, target=origin)
        return bound

    async def finalize_interaction_poll(
        self,
        interaction_id: int,
        *,
        response_id: int | None = None,
        target: str | None = None,
        installation_id: int | None = None,
        user_installation: bool = False,
    ) -> Message | dict[str, Any]:
        """End an isolated private response poll authored by this application."""

        response_ref = str(response_id) if response_id is not None else "@original"
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/interactions/{interaction_id}/responses/{response_ref}/polls/expire",
            target=origin,
            headers=self._e2ee_device_headers(),
        )
        if not isinstance(raw, dict):
            raise ApiError(
                502,
                "INTERACTION_POLL_RESPONSE_INVALID",
                "The interaction poll endpoint returned an invalid response.",
            )
        if raw.get("origin_domain") is not None:
            return self._bind_interaction_message(
                Message.from_payload(self, origin, raw),
                interaction_id,
                target=origin,
                kind="followup" if response_id is not None else "original",
                response_id=response_id,
                installation_id=installation_id,
                user_installation=user_installation,
            )
        return self._bind_interaction_response_dict(
            raw,
            interaction_id,
            target=origin,
            sequence_kind="followup" if response_id is not None else "original",
            expected_response_id=response_id,
            slot_kind="followup" if response_id is not None else "original",
            mutated=True,
        )

    async def delete_original_interaction_response(
        self, interaction_id: int, *, target: str | None = None
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/interactions/{interaction_id}/responses/@original",
            target=target,
        )

    async def create_interaction_followup(
        self,
        interaction_id: int,
        content: str | None = None,
        *,
        target: str | None = None,
        embeds: list[Embed] | None = None,
        view: View | None = None,
        poll: Poll | None = None,
        ephemeral: bool = False,
        attachment_ids: Sequence[int] = (),
        e2ee: dict[str, Any] | None = None,
        tts: bool = False,
        allowed_mentions: Mapping[str, object] | None = None,
        voice_message: bool = False,
        flags: int = 0,
        components: Sequence[dict[str, Any]] | None = None,
        installation_id: int | None = None,
        user_installation: bool = False,
    ) -> Message | dict[str, Any]:
        origin = self._target(target)
        voice_message = voice_message or bool(flags & (1 << 13))
        if voice_message and (
            tts
            or content is not None
            or embeds
            or view is not None
            or poll is not None
            or components
            or len(attachment_ids) != 1
        ):
            raise ValueError(
                "a voice response requires one audio attachment and no text or rich content"
            )
        if components is not None and any(item.get("type") != 1 for item in components):
            flags |= 1 << 15
        if view is not None and view.is_components_v2:
            flags |= 1 << 15
        if e2ee is not None and any(
            value is not None
            for value in (content, embeds, view, poll, allowed_mentions, components)
        ):
            raise ValueError(
                "an encrypted interaction follow-up cannot contain plaintext or rich fields"
            )
        message: dict[str, Any] = {
            "content": content,
            "embeds": serialize_embeds(embeds or []),
            "attachment_ids": [str(item) for item in attachment_ids],
        }
        if tts:
            message["tts"] = True
        if voice_message:
            message["voice_message"] = True
        effective_flags = flags | ((1 << 13) if voice_message else 0)
        if effective_flags:
            message["flags"] = effective_flags
        if allowed_mentions is not None:
            message["allowed_mentions"] = dict(allowed_mentions)
        if components is not None:
            if view is not None:
                raise ValueError("view and raw components are mutually exclusive")
            message["components"] = list(components)
        if view is not None:
            message["components"] = view.to_components()
            message["view_persistent"] = view.is_persistent
            if view.timeout is not None:
                message["view_timeout_seconds"] = max(1, int(view.timeout))
        if poll is not None:
            message["poll"] = poll.to_dict()
        if e2ee is not None:
            message["content"] = None
            message["e2ee"] = e2ee
        raw = await self.request(
            "POST",
            f"/api/v1/bots/interactions/{interaction_id}/followups",
            target=origin,
            json={"message": message, "ephemeral": ephemeral},
            headers=self._e2ee_device_headers(),
        )
        if isinstance(raw, dict) and raw.get("origin_domain") is not None:
            rendered = self._bind_interaction_message(
                Message.from_payload(self, origin, raw),
                interaction_id,
                target=origin,
                kind="followup",
                response_id=(
                    int(raw["response_id"])
                    if raw.get("response_id") is not None
                    else None
                ),
                installation_id=installation_id,
                user_installation=user_installation,
            )
            if view is not None and view.rows:
                response_id = rendered.interaction_response_id
                self.add_view(
                    view,
                    message_id=rendered.ref,
                    target=origin,
                    timeout_editor=(
                        self._view_timeout_editor(
                            (
                                f"/api/v1/bots/interactions/{interaction_id}/followups/{response_id}"
                            ),
                            target=origin,
                            view_version=rendered.view_version,
                        )
                        if response_id is not None
                        else None
                    ),
                )
            return rendered
        bound = self._bind_interaction_response_dict(
            raw,
            interaction_id,
            target=origin,
            sequence_kind="followup",
            slot_kind="followup",
            created=True,
        )
        if view is not None and view.rows and bound.get("id") is not None:
            response_id = EntityRef.from_wire(bound["id"], _target_authority(origin)).id
            self.add_view(
                view,
                response_id=response_id,
                target=origin,
                timeout_editor=self._view_timeout_editor(
                    (
                        f"/api/v1/bots/interactions/{interaction_id}/followups/{response_id}"
                    ),
                    target=origin,
                    view_version=int(bound.get("view_version", 0) or 0),
                ),
            )
        return bound

    async def edit_interaction_followup(
        self,
        interaction_id: int,
        followup_id: int,
        *,
        target: str | None = None,
        content: str | None | MissingType = MISSING,
        embeds: list[Embed] | MissingType = MISSING,
        view: View | MissingType = MISSING,
        view_version: int | None = None,
        attachment_ids: Sequence[int] | MissingType = MISSING,
        e2ee: dict[str, Any] | MissingType = MISSING,
        components: Sequence[dict[str, Any]] | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        allowed_mentions: Mapping[str, object] | MissingType = MISSING,
        installation_id: int | None = None,
        user_installation: bool = False,
    ) -> Message | dict[str, Any]:
        origin = self._target(target)
        if not isinstance(e2ee, MissingType) and any(
            not isinstance(value, MissingType)
            for value in (content, embeds, view, components, allowed_mentions)
        ):
            raise ValueError(
                "an encrypted interaction edit cannot contain plaintext or rich fields"
            )
        body: dict[str, Any] = {}
        if not isinstance(content, MissingType):
            body["content"] = content
        if not isinstance(embeds, MissingType):
            body["embeds"] = serialize_embeds(embeds)
        if not isinstance(attachment_ids, MissingType):
            body["attachment_ids"] = [str(item) for item in attachment_ids]
        if not isinstance(e2ee, MissingType):
            body["e2ee"] = e2ee
        if not isinstance(flags, MissingType):
            body["flags"] = flags
        if not isinstance(allowed_mentions, MissingType):
            body["allowed_mentions"] = dict(allowed_mentions)
        if not isinstance(components, MissingType):
            if not isinstance(view, MissingType):
                raise ValueError("view and raw components are mutually exclusive")
            rendered_components = list(components)
            body["components"] = rendered_components
            if any(item.get("type") != 1 for item in rendered_components):
                body["flags"] = int(body.get("flags", 0)) | (1 << 15)
        if not isinstance(view, MissingType):
            body["components"] = view.to_components()
            body["view_persistent"] = view.is_persistent
            if view.is_components_v2:
                body["flags"] = int(body.get("flags", 0)) | (1 << 15)
            if view.timeout is not None:
                body["view_timeout_seconds"] = max(1, int(view.timeout))
            if view_version is not None:
                body["view_version"] = view_version
        elif not isinstance(components, MissingType) and view_version is not None:
            body["view_version"] = view_version
        elif view_version is not None:
            raise ValueError("view_version requires a components update")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/interactions/{interaction_id}/followups/{followup_id}",
            target=origin,
            json=body,
            headers=self._e2ee_device_headers(),
        )
        if isinstance(raw, dict) and raw.get("origin_domain") is not None:
            rendered = self._bind_interaction_message(
                Message.from_payload(self, origin, raw),
                interaction_id,
                target=origin,
                kind="followup",
                response_id=followup_id,
                installation_id=installation_id,
                user_installation=user_installation,
            )
            if not isinstance(view, MissingType):
                if view.rows:
                    self.add_view(
                        view,
                        message_id=rendered.ref,
                        target=origin,
                        timeout_editor=self._view_timeout_editor(
                            (
                                f"/api/v1/bots/interactions/{interaction_id}/followups/{followup_id}"
                            ),
                            target=origin,
                            view_version=rendered.view_version,
                        ),
                    )
                else:
                    self.remove_view(rendered.ref)
            return rendered
        bound = self._bind_interaction_response_dict(
            raw,
            interaction_id,
            target=origin,
            sequence_kind="followup",
            expected_response_id=followup_id,
            slot_kind="followup",
            mutated=True,
        )
        if not isinstance(view, MissingType) and bound.get("id") is not None:
            response_id = EntityRef.from_wire(bound["id"], _target_authority(origin)).id
            if view.rows:
                self.add_view(
                    view,
                    response_id=response_id,
                    target=origin,
                    timeout_editor=self._view_timeout_editor(
                        (
                            f"/api/v1/bots/interactions/{interaction_id}/followups/{followup_id}"
                        ),
                        target=origin,
                        view_version=int(bound.get("view_version", 0) or 0),
                    ),
                )
            else:
                self.remove_response_view(response_id, target=origin)
        return bound

    async def fetch_interaction_followup(
        self,
        interaction_id: int,
        followup_id: int,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        user_installation: bool = False,
    ) -> Message | dict[str, Any]:
        origin = self._target(target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/interactions/{interaction_id}/followups/{followup_id}",
            target=origin,
            headers=self._e2ee_device_headers(),
        )
        if isinstance(raw, dict) and raw.get("origin_domain") is not None:
            return self._bind_interaction_message(
                Message.from_payload(self, origin, raw),
                interaction_id,
                target=origin,
                kind="followup",
                response_id=followup_id,
                installation_id=installation_id,
                user_installation=user_installation,
            )
        return self._bind_interaction_response_dict(
            raw,
            interaction_id,
            target=origin,
            sequence_kind="followup",
            expected_response_id=followup_id,
            slot_kind="followup",
        )

    async def delete_interaction_followup(
        self,
        interaction_id: int,
        followup_id: int,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/interactions/{interaction_id}/followups/{followup_id}",
            target=target,
        )

    async def reaction_users(
        self,
        channel: EntityRef,
        message: EntityRef,
        emoji: str,
        *,
        target: str | None = None,
        after: EntityRef | None = None,
        limit: int = 50,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> tuple[list[User], int, EntityRef | None]:
        origin = self._authority_target(channel, target)
        params: dict[str, Any] = {"limit": min(100, max(1, limit))}
        if after is not None:
            params["after"] = str(after)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/"
            f"{quote(emoji, safe='')}",
            target=origin,
            params=params,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        next_after = raw.get("next_after")
        return (
            [User.from_payload(item) for item in raw.get("items", [])],
            int(raw.get("total", 0)),
            EntityRef.parse(next_after) if isinstance(next_after, str) else None,
        )

    async def pins(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> list[Message]:
        messages: list[Message] = []
        before: datetime | None = None
        seen: set[EntityRef] = set()
        for _ in range(5):
            page = await self.pin_page(
                channel,
                target=target,
                before=before,
                limit=50,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            )
            for item in page.items:
                if item.message.ref in seen:
                    raise ValueError("message pins pagination repeated a message")
                seen.add(item.message.ref)
                messages.append(item.message)
            if not page.has_more:
                return messages
            if not page.items:
                raise ValueError("message pins pagination did not advance")
            next_before = page.items[-1].pinned_at
            if before is not None and next_before >= before:
                raise ValueError("message pins pagination did not advance")
            before = next_before
        raise ValueError("message pins response exceeds the 250-pin channel limit")

    async def pin_page(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        before: datetime | None = None,
        limit: int = 50,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> MessagePinPage:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if before is not None and before.tzinfo is None:
            raise ValueError("before must include a timezone")
        origin = self._authority_target(channel, target)
        params: dict[str, Any] = {"limit": limit}
        if before is not None:
            params["before"] = before.isoformat()
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/messages/pins",
            target=origin,
            params=params,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )
        page = MessagePinPage.from_payload(
            self,
            origin,
            raw,
            channel_ref=channel,
        )
        for item in page.items:
            item.message.bind_runtime(
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            )
        return page

    async def pin_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "PUT",
            f"/api/v1/bots/channels/{channel}/messages/pins/{message}",
            target=origin,
            headers=dict(
                await self._runtime_grant_headers(
                    channel,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                )
            )
            | dict(_audit_headers(reason) or {}),
        )

    async def unpin_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/messages/pins/{message}",
            target=origin,
            headers=dict(
                await self._runtime_grant_headers(
                    channel,
                    installation_id=installation_id,
                    dm_capability_id=dm_capability_id,
                )
            )
            | dict(_audit_headers(reason) or {}),
        )

    async def trigger_typing(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/typing",
            target=origin,
            headers=await self._runtime_grant_headers(
                channel,
                installation_id=installation_id,
                dm_capability_id=dm_capability_id,
            ),
        )

    async def edit_member(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        nickname: str | None | MissingType = MISSING,
        timeout_until: datetime | None | MissingType = MISSING,
        timeout_indefinite: bool | MissingType = MISSING,
        reason: str | None = None,
    ) -> Member:
        origin = self._authority_target(guild, target)
        body: dict[str, Any] = {}
        if not isinstance(nickname, MissingType):
            body["nickname"] = nickname
        if not isinstance(timeout_until, MissingType):
            body["timeout_until"] = (
                timeout_until.isoformat() if timeout_until is not None else None
            )
        if not isinstance(timeout_indefinite, MissingType):
            body["timeout_indefinite"] = timeout_indefinite
        if not body:
            raise ValueError("at least one member field is required")
        headers = {"X-Audit-Log-Reason": reason} if reason else None
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/members/{user}",
            target=origin,
            json=body,
            headers=headers,
        )
        return Member.from_payload(self, origin, raw)

    async def kick_member(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/members/{user}",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def ban_member(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
        delete_message_seconds: int = 0,
        expires_at: datetime | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/bans/{user}",
            target=origin,
            json={
                "reason": reason,
                "delete_message_seconds": delete_message_seconds,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def unban_member(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/bans/{user}",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def bans(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        after: EntityRef | None = None,
        limit: int = 50,
    ) -> list[Ban]:
        origin = self._authority_target(guild, target)
        params: dict[str, Any] = {"limit": min(1000, max(1, limit))}
        if after is not None:
            params["after"] = str(after)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/bans",
            target=origin,
            params=params,
        )
        return [Ban.from_payload(self, origin, item) for item in raw]

    async def instance_bans(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> list[InstanceBan]:
        origin = self._authority_target(guild, target)
        params: dict[str, Any] = {"limit": min(1000, max(1, limit))}
        if after is not None:
            params["after"] = after
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/instance-bans",
            target=origin,
            params=params,
        )
        return [InstanceBan.from_payload(self, origin, item) for item in raw]

    async def ban_instance(
        self,
        guild: EntityRef,
        instance_domain: str,
        *,
        target: str | None = None,
        reason: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/instance-bans/{instance_domain}",
            target=origin,
            json={
                "reason": reason,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def unban_instance(
        self,
        guild: EntityRef,
        instance_domain: str,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/instance-bans/{instance_domain}",
            target=origin,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def auto_mod_rules(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[AutoModRule]:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/auto-moderation/rules",
            target=origin,
        )
        rules = [_auto_mod_rule_response(self, origin, guild, item) for item in raw]
        return _scoped_resource_list(
            rules,
            resource_ref=lambda rule: rule.ref,
            label="AutoMod rule",
        )

    async def fetch_auto_mod_rule(
        self,
        guild: EntityRef,
        rule_id: int,
        *,
        target: str | None = None,
    ) -> AutoModRule:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/auto-moderation/rules/{rule_id}",
            target=origin,
        )
        return _auto_mod_rule_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(rule_id, guild.domain),
        )

    async def create_auto_mod_rule(
        self,
        guild: EntityRef,
        name: str,
        trigger_type: AutoModTriggerType,
        actions: Sequence[AutoModAction],
        *,
        target: str | None = None,
        event_type: AutoModEventType = "message_send",
        trigger_metadata: AutoModTriggerMetadata | None = None,
        enabled: bool = False,
        exempt_roles: Sequence[EntityRef] = (),
        exempt_channels: Sequence[EntityRef] = (),
        reason: str | None = None,
    ) -> AutoModRule:
        cleaned_name = name.strip()
        if not 1 <= len(cleaned_name) <= 100:
            raise ValueError("AutoMod rule names must contain 1 to 100 characters")
        metadata = trigger_metadata or AutoModTriggerMetadata()
        action_items = tuple(actions)
        role_items = tuple(exempt_roles)
        channel_items = tuple(exempt_channels)
        validate_rule_configuration(
            event_type,
            trigger_type,
            metadata,
            action_items,
            role_items,
            channel_items,
        )
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        self._require_same_authority(
            guild,
            *_auto_mod_action_channels(action_items),
            label="AutoMod action channel",
        )
        self._require_same_authority(guild, *role_items, label="exempt role")
        self._require_same_authority(guild, *channel_items, label="exempt channel")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/auto-moderation/rules",
            target=origin,
            json={
                "name": cleaned_name,
                "event_type": event_type,
                "trigger_type": trigger_type,
                "trigger_metadata": metadata.to_dict(),
                "actions": [action.to_dict() for action in action_items],
                "enabled": enabled,
                "exempt_roles": _wire_refs(role_items, name="exempt roles", maximum=20),
                "exempt_channels": _wire_refs(
                    channel_items, name="exempt channels", maximum=50
                ),
            },
            headers=_audit_headers(reason),
        )
        return _auto_mod_rule_response(self, origin, guild, raw)

    async def edit_auto_mod_rule(
        self,
        guild: EntityRef,
        rule_id: int,
        *,
        target: str | None = None,
        name: str | MissingType = MISSING,
        event_type: AutoModEventType | MissingType = MISSING,
        trigger_metadata: AutoModTriggerMetadata | MissingType = MISSING,
        actions: Sequence[AutoModAction] | MissingType = MISSING,
        enabled: bool | MissingType = MISSING,
        exempt_roles: Sequence[EntityRef] | MissingType = MISSING,
        exempt_channels: Sequence[EntityRef] | MissingType = MISSING,
        reason: str | None = None,
    ) -> AutoModRule:
        body: dict[str, object] = {}
        if not isinstance(name, MissingType):
            cleaned_name = name.strip()
            if not 1 <= len(cleaned_name) <= 100:
                raise ValueError("AutoMod rule names must contain 1 to 100 characters")
            body["name"] = cleaned_name
        if not isinstance(event_type, MissingType):
            if event_type not in {"message_send", "member_update"}:
                raise ValueError("unsupported AutoMod event type")
            body["event_type"] = event_type
        if not isinstance(trigger_metadata, MissingType):
            body["trigger_metadata"] = trigger_metadata.to_dict()
        if not isinstance(actions, MissingType):
            action_items = tuple(actions)
            validate_actions(action_items)
            self._require_same_authority(
                guild,
                *_auto_mod_action_channels(action_items),
                label="AutoMod action channel",
            )
            body["actions"] = [action.to_dict() for action in action_items]
        if not isinstance(enabled, MissingType):
            if not isinstance(enabled, bool):
                raise TypeError("enabled must be a boolean")
            body["enabled"] = enabled
        if not isinstance(exempt_roles, MissingType):
            body["exempt_roles"] = _wire_refs(
                exempt_roles, name="exempt roles", maximum=20
            )
            self._require_same_authority(guild, *exempt_roles, label="exempt role")
        if not isinstance(exempt_channels, MissingType):
            body["exempt_channels"] = _wire_refs(
                exempt_channels, name="exempt channels", maximum=50
            )
            self._require_same_authority(
                guild, *exempt_channels, label="exempt channel"
            )
        if not body:
            raise ValueError("at least one AutoMod rule field is required")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/auto-moderation/rules/{rule_id}",
            target=origin,
            json=body,
            headers=_audit_headers(reason),
        )
        return _auto_mod_rule_response(
            self,
            origin,
            guild,
            raw,
            expected_ref=EntityRef(rule_id, guild.domain),
        )

    async def delete_auto_mod_rule(
        self,
        guild: EntityRef,
        rule_id: int,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(guild, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/auto-moderation/rules/{rule_id}",
            target=origin,
            headers=_audit_headers(reason),
        )

    async def estimate_prune(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        days: int = 7,
        include_roles: Sequence[EntityRef] = (),
    ) -> PruneEstimate:
        if isinstance(days, bool) or not isinstance(days, int):
            raise TypeError("days must be an integer")
        if not 1 <= days <= 30:
            raise ValueError("days must be between 1 and 30")
        roles = _wire_refs(include_roles, name="included roles", maximum=100)
        self._require_same_authority(guild, *include_roles, label="included role")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/prune/estimate",
            target=origin,
            params={"days": days, "include_roles": roles},
        )
        estimate = PruneEstimate.from_payload(raw)
        if estimate.days != days:
            raise ValueError("prune estimate changed the requested day window")
        return estimate

    async def prune_members(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        days: int = 7,
        include_roles: Sequence[EntityRef] = (),
        compute_prune_count: bool = True,
        reason: str | None = None,
    ) -> PruneResult:
        if isinstance(days, bool) or not isinstance(days, int):
            raise TypeError("days must be an integer")
        if not 1 <= days <= 30:
            raise ValueError("days must be between 1 and 30")
        if not isinstance(compute_prune_count, bool):
            raise TypeError("compute_prune_count must be a boolean")
        role_ids = _wire_refs(include_roles, name="included roles", maximum=100)
        self._require_same_authority(guild, *include_roles, label="included role")
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/prune",
            target=origin,
            json={
                "days": days,
                "include_roles": role_ids,
                "compute_prune_count": compute_prune_count,
            },
            headers=_audit_headers(reason),
        )
        result = PruneResult.from_payload(raw)
        if (
            result.guild_ref != guild
            or result.days != days
            or (result.pruned is not None) is not compute_prune_count
        ):
            raise ValueError("prune response changed its requested scope")
        return result

    async def bulk_ban_members(
        self,
        guild: EntityRef,
        users: Sequence[EntityRef],
        *,
        target: str | None = None,
        delete_message_seconds: int = 0,
        reason: str | None = None,
    ) -> BulkBanResult:
        requested_users = tuple(users)
        user_ids = _wire_refs(
            requested_users,
            name="bulk ban users",
            maximum=200,
        )
        if not user_ids:
            raise ValueError("bulk bans require at least one user")
        if isinstance(delete_message_seconds, bool) or not isinstance(
            delete_message_seconds, int
        ):
            raise TypeError("delete_message_seconds must be an integer")
        if not 0 <= delete_message_seconds <= 604_800:
            raise ValueError("delete_message_seconds must be between 0 and 604800")
        headers = _audit_headers(reason)
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/bulk-bans",
            target=origin,
            json={
                "user_ids": user_ids,
                "delete_message_seconds": delete_message_seconds,
                "reason": headers["X-Audit-Log-Reason"] if headers else None,
            },
            headers=headers,
        )
        result = BulkBanResult.from_payload(raw)
        if set(result.banned_users) | set(result.failed_users) != set(requested_users):
            raise ValueError("bulk ban response changed its requested user partition")
        return result

    async def fetch_audit_logs(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        before: int | None = None,
        after: int | None = None,
        user: EntityRef | None = None,
        action_type: int | None = None,
        target_type: str | None = None,
        limit: int = 50,
    ) -> list[AuditLogEntry]:
        if not 1 <= limit <= 100:
            raise ValueError("audit log page limit must be between 1 and 100")
        if before is not None and after is not None:
            raise ValueError("choose either before or after for audit logs, not both")
        params: dict[str, Any] = {"limit": limit}
        if before is not None:
            params["before"] = str(before)
        if after is not None:
            params["after"] = str(after)
        if user is not None:
            params["user_id"] = str(user)
        if action_type is not None:
            params["action_type"] = action_type
        if target_type is not None:
            params["target_type"] = target_type
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/audit-logs",
            target=origin,
            params=params,
        )
        return [AuditLogEntry.from_payload(item) for item in raw]

    async def audit_logs(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        before: int | None = None,
        after: int | None = None,
        user: EntityRef | None = None,
        action_type: int | None = None,
        target_type: str | None = None,
        page_size: int = 100,
        limit: int | None = None,
    ) -> AsyncIterator[AuditLogEntry]:
        if not 1 <= page_size <= 100:
            raise ValueError("audit log page size must be between 1 and 100")
        if limit is not None and limit < 0:
            raise ValueError("audit log limit must be non-negative")
        if before is not None and after is not None:
            raise ValueError("choose either before or after for audit logs, not both")
        remaining = limit
        cursor = after if after is not None else before
        ascending = after is not None
        while remaining is None or remaining > 0:
            request_limit = (
                page_size if remaining is None else min(page_size, remaining)
            )
            page = await self.fetch_audit_logs(
                guild,
                target=target,
                before=None if ascending else cursor,
                after=cursor if ascending else None,
                user=user,
                action_type=action_type,
                target_type=target_type,
                limit=request_limit,
            )
            if not page:
                return
            for entry in page:
                yield entry
            if remaining is not None:
                remaining -= len(page)
            next_cursor = page[-1].id
            cursor_did_not_advance = cursor is not None and (
                next_cursor <= cursor if ascending else next_cursor >= cursor
            )
            if len(page) < request_limit or cursor_did_not_advance:
                return
            cursor = next_cursor

    async def create_stage_instance(
        self,
        channel: EntityRef,
        topic: str,
        *,
        target: str | None = None,
        privacy_level: Literal[2] = 2,
        send_start_notification: bool = False,
        scheduled_event: EntityRef | None = None,
        reason: str | None = None,
    ) -> StageInstance:
        if isinstance(privacy_level, bool) or privacy_level != 2:
            raise ValueError("guild-only is the only supported Stage privacy level")
        if scheduled_event is not None:
            self._require_same_authority(
                channel, scheduled_event, label="scheduled event"
            )
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "POST",
            "/api/v1/bots/stage-instances",
            target=origin,
            json={
                "channel_id": str(channel),
                "topic": _stage_topic(topic),
                "privacy_level": privacy_level,
                "send_start_notification": send_start_notification,
                "guild_scheduled_event_id": (
                    str(scheduled_event) if scheduled_event is not None else None
                ),
            },
            headers=_audit_headers(reason),
        )
        stage = _stage_instance_response(self, origin, channel, raw)
        if stage.scheduled_event_ref != scheduled_event:
            raise ValueError("Stage response changed the requested scheduled event")
        return stage

    async def fetch_stage_instance(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
    ) -> StageInstance:
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/stage-instances/{channel}",
            target=origin,
        )
        return _stage_instance_response(self, origin, channel, raw)

    async def edit_stage_instance(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        topic: str | MissingType = MISSING,
        privacy_level: Literal[2] | MissingType = MISSING,
        reason: str | None = None,
    ) -> StageInstance:
        body: dict[str, object] = {}
        if not isinstance(topic, MissingType):
            body["topic"] = _stage_topic(topic)
        if not isinstance(privacy_level, MissingType):
            if isinstance(privacy_level, bool) or privacy_level != 2:
                raise ValueError("guild-only is the only supported Stage privacy level")
            body["privacy_level"] = privacy_level
        if not body:
            raise ValueError("at least one Stage instance field is required")
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/stage-instances/{channel}",
            target=origin,
            json=body,
            headers=_audit_headers(reason),
        )
        return _stage_instance_response(self, origin, channel, raw)

    async def delete_stage_instance(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        origin = self._authority_target(channel, target)
        await self.request(
            "DELETE",
            f"/api/v1/bots/stage-instances/{channel}",
            target=origin,
            headers=_audit_headers(reason),
        )

    async def fetch_current_stage_voice_state(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
    ) -> StageVoiceState:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/voice-states/@me",
            target=origin,
        )
        expected_user = self._bot_user_refs.get(origin)
        if expected_user is None:
            raise ValueError(
                "Stage voice-state response has no authenticated bot lineage"
            )
        return _stage_voice_state_response(
            raw,
            guild=guild,
            expected_user=expected_user,
        )

    async def fetch_stage_voice_state(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
    ) -> StageVoiceState:
        origin = self._authority_target(guild, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/voice-states/{user}",
            target=origin,
        )
        return _stage_voice_state_response(
            raw,
            guild=guild,
            expected_user=user,
        )

    async def update_current_stage_voice_state(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        channel: EntityRef | None = None,
        suppress: bool | MissingType = MISSING,
        request_to_speak_at: datetime | None | MissingType = MISSING,
    ) -> None:
        if channel is not None:
            self._require_same_authority(guild, channel, label="stage channel")
        body: dict[str, object] = {}
        if channel is not None:
            body["channel_id"] = str(channel)
        if not isinstance(suppress, MissingType):
            body["suppress"] = suppress
        if not isinstance(request_to_speak_at, MissingType):
            if request_to_speak_at is not None and request_to_speak_at.tzinfo is None:
                raise ValueError("request_to_speak_at requires a timezone")
            body["request_to_speak_timestamp"] = (
                request_to_speak_at.astimezone(UTC).isoformat()
                if request_to_speak_at is not None
                else None
            )
        if set(body) <= {"channel_id"}:
            raise ValueError("suppress or request_to_speak_at is required")
        origin = self._authority_target(guild, target)
        await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/voice-states/@me",
            target=origin,
            json=body,
        )

    async def request_to_speak(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        channel: EntityRef | None = None,
        requested_at: datetime | None = None,
    ) -> None:
        await self.update_current_stage_voice_state(
            guild,
            target=target,
            channel=channel,
            request_to_speak_at=requested_at or datetime.now(UTC),
        )

    async def clear_request_to_speak(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        channel: EntityRef | None = None,
    ) -> None:
        await self.update_current_stage_voice_state(
            guild,
            target=target,
            channel=channel,
            request_to_speak_at=None,
        )

    async def set_stage_suppressed(
        self,
        guild: EntityRef,
        suppress: bool,
        *,
        target: str | None = None,
        channel: EntityRef | None = None,
    ) -> None:
        await self.update_current_stage_voice_state(
            guild,
            target=target,
            channel=channel,
            suppress=suppress,
        )

    async def update_stage_user_voice_state(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        channel: EntityRef,
        suppress: bool,
        target: str | None = None,
    ) -> None:
        self._require_same_authority(guild, channel, label="stage channel")
        origin = self._authority_target(guild, target)
        await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/voice-states/{user}",
            target=origin,
            json={"channel_id": str(channel), "suppress": suppress},
        )

    async def promote_stage_speaker(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        channel: EntityRef,
        target: str | None = None,
    ) -> None:
        await self.update_stage_user_voice_state(
            guild,
            user,
            channel=channel,
            suppress=False,
            target=target,
        )

    async def move_stage_user_to_audience(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        channel: EntityRef,
        target: str | None = None,
    ) -> None:
        await self.update_stage_user_voice_state(
            guild,
            user,
            channel=channel,
            suppress=True,
            target=target,
        )

    async def voice_occupancy(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        dm_capability_id: str | None = None,
    ) -> VoiceOccupancy:
        origin = self._authority_target(channel, target)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/voice/occupancy",
            target=origin,
            headers=await self._dm_capability_headers_for_grant(
                channel,
                dm_capability_id,
            ),
        )
        participants = raw.get("participants", raw.get("occupants", []))
        return VoiceOccupancy(
            channel_ref=channel,
            participants=tuple(item for item in participants if isinstance(item, dict)),
            generated_at=(
                int(raw["generated_at"])
                if raw.get("generated_at") is not None
                else None
            ),
        )

    async def start_call(
        self,
        channel: EntityRef,
        *,
        ring: bool = True,
        target: str | None = None,
        dm_capability_id: str | None = None,
    ) -> Call:
        if type(ring) is not bool:
            raise TypeError("call ring must be a boolean")
        origin = self._authority_target(channel, target)
        selected_dm_capability_id = dm_capability_id or self._default_dm_capability_id(
            channel
        )
        dm_context = await self._dm_capability_context_for_grant(
            channel,
            selected_dm_capability_id,
        )
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/calls",
            target=origin,
            json={"ring": ring},
            headers=_dm_capability_headers(dm_context)
            if dm_context is not None
            else {},
        )
        call = Call.from_payload(
            self,
            origin,
            raw,
            fallback_dm_capability_id=selected_dm_capability_id,
        )
        _require_call_dm_capability_context(call, dm_context)
        if call.channel_ref != channel or call.state != "ringing":
            raise ValueError("call response does not match the requested channel")
        return call

    async def active_call(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        dm_capability_id: str | None = None,
    ) -> ActiveCall:
        origin = self._authority_target(channel, target)
        selected_dm_capability_id = dm_capability_id or self._default_dm_capability_id(
            channel
        )
        dm_context = await self._dm_capability_context_for_grant(
            channel,
            selected_dm_capability_id,
        )
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/calls/active",
            target=origin,
            headers=_dm_capability_headers(dm_context)
            if dm_context is not None
            else {},
        )
        joined = raw.get("joined", False)
        if type(joined) is not bool:
            raise ValueError("active call response has an invalid joined state")
        call = raw.get("call")
        parsed_call = (
            Call.from_payload(
                self,
                origin,
                call,
                fallback_dm_capability_id=selected_dm_capability_id,
            )
            if isinstance(call, dict)
            else None
        )
        if call is not None and parsed_call is None:
            raise ValueError("active call response has an invalid call")
        if parsed_call is not None:
            _require_call_dm_capability_context(parsed_call, dm_context)
        if parsed_call is not None and (
            parsed_call.channel_ref != channel or parsed_call.state == "ended"
        ):
            raise ValueError(
                "active call response does not match the requested channel"
            )
        if parsed_call is None and joined:
            raise ValueError("active call response is joined without an active call")
        return ActiveCall(
            call=parsed_call,
            joined=joined,
        )

    async def act_call(
        self,
        channel: EntityRef,
        call: EntityRef,
        action: Literal["accept", "decline", "end"],
        *,
        target: str | None = None,
        dm_capability_id: str | None = None,
    ) -> Call:
        self._require_same_authority(channel, call, label="call")
        origin = self._authority_target(channel, target)
        selected_dm_capability_id = dm_capability_id or self._default_dm_capability_id(
            channel
        )
        dm_context = await self._dm_capability_context_for_grant(
            channel,
            selected_dm_capability_id,
        )
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/calls/{call}",
            target=origin,
            json={"action": action},
            headers=_dm_capability_headers(dm_context)
            if dm_context is not None
            else {},
        )
        updated = Call.from_payload(
            self,
            origin,
            raw,
            fallback_dm_capability_id=selected_dm_capability_id,
        )
        _require_call_dm_capability_context(updated, dm_context)
        expected_state = "active" if action == "accept" else "ended"
        if (
            updated.ref != call
            or updated.channel_ref != channel
            or updated.state != expected_state
        ):
            raise ValueError("call action response does not match the requested call")
        return updated

    async def voice_regions(
        self,
        guild: EntityRef | None = None,
        *,
        target: str | None = None,
    ) -> list[VoiceRegion]:
        path = (
            "/api/v1/bots/voice/regions"
            if guild is None
            else f"/api/v1/bots/guilds/{guild}/voice/regions"
        )
        origin = self._resource_target(guild, target)
        raw = await self.request("GET", path, target=origin)
        if not isinstance(raw, list):
            raise ValueError("voice region response must be a list")
        if any(not isinstance(item, dict) for item in raw):
            raise ValueError("voice region response contains an invalid item")
        return [VoiceRegion.from_payload(item) for item in raw]

    async def connect_voice(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        listen: bool = False,
        speak: bool = False,
        stream: bool = False,
        takeover: bool = False,
        transport: VoiceTransport | None = None,
        e2ee_context: VoiceE2EEContext | None = None,
        call: EntityRef | Call | None = None,
        dm_capability_id: str | None = None,
    ) -> VoiceClient:
        call_model = call if isinstance(call, Call) else None
        call_ref: EntityRef | None = call.ref if isinstance(call, Call) else call
        if (
            call_ref is not None
            and call_ref.domain is not None
            and channel.domain is not None
            and call_ref.domain != channel.domain
        ):
            raise ValueError("call and channel authorities must match")
        origin = self._authority_target(channel, target)
        connection_id = secrets.token_urlsafe(32)
        request_body: dict[str, object] = {
            "connection_id": connection_id,
            "takeover": takeover,
            "listen": listen,
            "speak": speak,
            "stream": stream,
        }
        if e2ee_context is not None:
            if e2ee_context.channel_ref != channel:
                raise ValueError("voice E2EE context does not match the channel")
            request_body["sender_device_id"] = e2ee_context.device_id
        token_path = (
            f"/api/v1/bots/channels/{channel}/voice/token"
            if call_ref is None
            else f"/api/v1/bots/channels/{channel}/calls/{call_ref}/voice/token"
        )
        selected_dm_capability_id = (
            call_model.dm_capability_id
            if call_model is not None
            else dm_capability_id or self._default_dm_capability_id(channel)
        )
        runtime_headers = await self._dm_capability_headers_for_grant(
            channel,
            selected_dm_capability_id,
        )
        raw = await self.request(
            "POST",
            token_path,
            target=origin,
            json=request_body,
            headers=runtime_headers,
        )

        async def release_reservation(generation: object) -> None:
            if isinstance(generation, bool):
                return
            if isinstance(generation, int):
                parsed_generation = generation
            elif isinstance(generation, str):
                try:
                    parsed_generation = int(generation)
                except ValueError:
                    return
            else:
                return
            if parsed_generation < 0:
                return
            with suppress(Exception):
                await self.request(
                    "DELETE",
                    f"/api/v1/bots/channels/{channel}/voice",
                    target=origin,
                    json={
                        "connection_id": connection_id,
                        "generation": parsed_generation,
                    },
                    headers=runtime_headers,
                )

        try:
            grant = VoiceGrant.from_payload(raw)
        except Exception:
            # Token issuance reserves the bot's account before returning the
            # grant. Even a malformed or untrusted response must not strand
            # that reservation when it includes the server generation needed
            # for a conditional release.
            await release_reservation(
                raw.get("generation") if isinstance(raw, dict) else None
            )
            raise
        if (
            grant.connection_id != connection_id
            or grant.channel_ref != channel
            or urlsplit(grant.url).hostname != channel.domain
        ):
            await release_reservation(grant.generation)
            raise ApiError(
                502, "VOICE_GRANT_INVALID", "Voice grant did not match the request"
            )
        voice = VoiceClient(
            self,
            origin,
            grant,
            transport or LiveKitTransport(),
            e2ee_context=e2ee_context,
            runtime_headers=runtime_headers,
        )
        try:
            await voice.connect()
        except BaseException:
            # The server reserves the bot's account/room before minting the
            # capability. A transport failure must relinquish that reservation
            # immediately instead of making retries wait for its TTL. Cleanup
            # is best effort so the original connection error remains useful.
            with suppress(Exception):
                await voice.transport.disconnect()
            await release_reservation(grant.generation)
            raise
        self._register_voice_client(voice)
        return voice

    def _event_model(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        target: str,
        topic: str | None,
        sequence: int,
    ) -> object:
        if event_type == "INTERACTION_CREATE":
            interaction = Interaction.from_payload(self, target, data)
            self._remember_interaction_lifecycle_grant(data, target=target)
            return interaction
        if event_type in {"MESSAGE_CREATE", "MESSAGE_UPDATE"} and "created_at" in data:
            return Message.from_payload(self, target, data)
        if event_type == "MESSAGE_DELETE":
            return MessageDeleteEvent(
                target,
                EntityRef.from_wire(data["id"], data["origin_domain"]),
                EntityRef.from_wire(data["channel_id"], data["channel_domain"]),
            )
        if event_type == "MESSAGE_DELETE_BULK":
            topic_guild_ref = _guild_ref_from_topic(topic)
            channel_domain = data.get("channel_domain") or (
                topic_guild_ref.domain if topic_guild_ref is not None else ""
            )
            guild_ref = (
                EntityRef.from_wire(data["guild_id"], data["guild_domain"])
                if data.get("guild_id") is not None
                and data.get("guild_domain") is not None
                else topic_guild_ref
            )
            return MessageBulkDeleteEvent(
                target=target,
                message_refs=tuple(
                    EntityRef.from_wire(
                        item["id"], item.get("origin_domain") or channel_domain
                    )
                    for item in data.get("ids", [])
                    if isinstance(item, dict) and item.get("id") is not None
                ),
                channel_ref=EntityRef.from_wire(data["channel_id"], channel_domain),
                guild_ref=guild_ref,
            )
        if event_type in {"MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"}:
            raw_emoji_details = data.get("emoji")
            emoji_details = (
                ReactionEmoji.from_payload(raw_emoji_details)
                if isinstance(raw_emoji_details, dict)
                else None
            )
            raw_emoji = data.get("reaction")
            if raw_emoji is None and emoji_details is not None:
                raw_emoji = emoji_details.token
            if not isinstance(raw_emoji, str) or not raw_emoji:
                raise ValueError("reaction event emoji is invalid")
            if emoji_details is not None and emoji_details.token != raw_emoji:
                raise ValueError("reaction event emoji projections conflict")
            raw_burst_colors = data.get("burst_colors", [])
            if not isinstance(raw_burst_colors, list) or not all(
                isinstance(item, str) for item in raw_burst_colors
            ):
                raise ValueError("reaction event burst colors are invalid")
            raw_reaction_type = data.get("type", 0)
            if (
                isinstance(raw_reaction_type, bool)
                or not isinstance(raw_reaction_type, int)
                or raw_reaction_type not in {0, 1}
            ):
                raise ValueError("reaction event type is invalid")
            return ReactionEvent(
                target=target,
                message_ref=EntityRef.from_wire(
                    data.get("message_id", data.get("id")),
                    data.get("message_domain", data.get("origin_domain")),
                ),
                channel_ref=EntityRef.from_wire(
                    data["channel_id"], data["channel_domain"]
                ),
                user_ref=EntityRef.from_wire(data["user_id"], data["user_domain"]),
                emoji=raw_emoji,
                guild_ref=_optional_asserted_ref(
                    data,
                    ref_key="guild_ref",
                    id_key="guild_id",
                    domain_key="guild_domain",
                    label="reaction guild",
                ),
                emoji_details=emoji_details,
                message_author_ref=_optional_asserted_ref(
                    data,
                    ref_key="message_author_ref",
                    id_key="message_author_id",
                    domain_key="message_author_domain",
                    label="reaction message author",
                ),
                burst=strict_payload_bool(data, "burst", default=False),
                burst_colors=tuple(raw_burst_colors),
                reaction_type=raw_reaction_type,
            )
        if event_type in {
            "MESSAGE_REACTION_REMOVE_ALL",
            "MESSAGE_REACTION_REMOVE_EMOJI",
        }:
            topic_guild_ref = _guild_ref_from_topic(topic)
            raw_clear_details = data.get("emoji")
            clear_details = (
                ReactionEmoji.from_payload(raw_clear_details)
                if isinstance(raw_clear_details, dict)
                else None
            )
            raw_clear_emoji = data.get("reaction")
            if raw_clear_emoji is None and isinstance(raw_clear_details, str):
                raw_clear_emoji = raw_clear_details
            if raw_clear_emoji is None and clear_details is not None:
                raw_clear_emoji = clear_details.token
            if raw_clear_emoji is not None and (
                not isinstance(raw_clear_emoji, str) or not raw_clear_emoji
            ):
                raise ValueError("reaction clear emoji is invalid")
            if clear_details is not None and clear_details.token != raw_clear_emoji:
                raise ValueError("reaction clear emoji projections conflict")
            if (
                event_type == "MESSAGE_REACTION_REMOVE_EMOJI"
                and raw_clear_emoji is None
            ):
                raise ValueError("reaction clear emoji is missing")
            payload_guild_ref = _optional_asserted_ref(
                data,
                ref_key="guild_ref",
                id_key="guild_id",
                domain_key="guild_domain",
                label="reaction clear guild",
            )
            if (
                payload_guild_ref is not None
                and topic_guild_ref is not None
                and payload_guild_ref != topic_guild_ref
            ):
                raise ValueError("reaction clear guild conflicts with its topic")
            return ReactionClearEvent(
                target=target,
                message_ref=EntityRef.from_wire(
                    data["message_id"], data["message_domain"]
                ),
                channel_ref=EntityRef.from_wire(
                    data["channel_id"], data["channel_domain"]
                ),
                guild_ref=payload_guild_ref or topic_guild_ref,
                emoji=raw_clear_emoji,
                emoji_details=clear_details,
            )
        if event_type in {"MESSAGE_POLL_VOTE_ADD", "MESSAGE_POLL_VOTE_REMOVE"}:
            topic_guild_ref = _guild_ref_from_topic(topic)
            payload_guild_ref = _optional_asserted_ref(
                data,
                ref_key="guild_ref",
                id_key="guild_id",
                domain_key="guild_domain",
                label="poll vote guild",
            )
            if (
                payload_guild_ref is not None
                and topic_guild_ref is not None
                and payload_guild_ref != topic_guild_ref
            ):
                raise ValueError("poll vote guild conflicts with its topic")
            answer_id = data.get("answer_id")
            if (
                isinstance(answer_id, bool)
                or not isinstance(answer_id, int)
                or answer_id < 1
            ):
                raise ValueError("poll vote answer is invalid")
            return PollVoteEvent(
                target=target,
                message_ref=EntityRef.from_wire(
                    data["message_id"], data["message_domain"]
                ),
                channel_ref=EntityRef.from_wire(
                    data["channel_id"], data["channel_domain"]
                ),
                user_ref=EntityRef.from_wire(data["user_id"], data["user_domain"]),
                answer_id=answer_id,
                added=event_type == "MESSAGE_POLL_VOTE_ADD",
                guild_ref=payload_guild_ref or topic_guild_ref,
            )
        if event_type == "MESSAGE_PIN_UPDATE":
            return PinEvent(
                target,
                EntityRef.from_wire(data["id"], data["origin_domain"]),
                EntityRef.from_wire(data["channel_id"], data["channel_domain"]),
                bool(data["pinned"]),
            )
        if event_type == "CHANNEL_PINS_UPDATE":
            topic_guild_ref = _guild_ref_from_topic(topic)
            channel_domain = data.get("channel_domain") or (
                topic_guild_ref.domain if topic_guild_ref is not None else ""
            )
            raw_message_id = data.get("message_id")
            raw_message_domain = data.get("message_domain")
            raw_pinned = data.get("pinned")
            if (raw_message_id is None) != (raw_message_domain is None):
                raise ValueError("channel pins update message reference is incomplete")
            if raw_pinned is not None and type(raw_pinned) is not bool:
                raise ValueError("channel pins update state is invalid")
            if raw_pinned is not None and raw_message_id is None:
                raise ValueError("channel pins update state has no message reference")
            return ChannelPinsUpdateEvent(
                target=target,
                channel_ref=EntityRef.from_wire(data["channel_id"], channel_domain),
                guild_ref=(
                    EntityRef.from_wire(data["guild_id"], data["guild_domain"])
                    if data.get("guild_id") is not None
                    and data.get("guild_domain") is not None
                    else topic_guild_ref
                ),
                last_pin_at=(
                    datetime.fromisoformat(str(data["last_pin_timestamp"]))
                    if data.get("last_pin_timestamp") is not None
                    else None
                ),
                message_ref=(
                    EntityRef.from_wire(raw_message_id, raw_message_domain)
                    if raw_message_id is not None
                    else None
                ),
                pinned=raw_pinned,
            )
        if event_type == "READY":
            return ReadyEvent(
                target,
                EntityRef.parse(data["application_ref"]),
                int(data["worker_id"]),
                tuple(data.get("installations") or ()),
                tuple(str(item) for item in data.get("intents") or ()),
                tuple(data.get("user_installations") or ()),
                tuple(data.get("dm_capabilities") or ()),
            )
        if event_type == "APPLICATION_COMMAND_PERMISSIONS_UPDATE":
            topic_guild_ref = _guild_ref_from_topic(topic)
            if topic_guild_ref is None:
                return RawEvent(target, event_type, data, topic, sequence)
            raw_guild_id = data.get("guild_id")
            raw_guild_domain = data.get("guild_domain")
            if (raw_guild_id is None) != (raw_guild_domain is None):
                raise ValueError("application command guild reference is incomplete")
            guild_ref = (
                EntityRef.from_wire(raw_guild_id, raw_guild_domain)
                if raw_guild_id is not None
                else topic_guild_ref
            )
            raw_application_id = data.get("application_id")
            raw_application_domain = data.get("application_domain")
            if (raw_application_id is None) != (raw_application_domain is None):
                raise ValueError(
                    "application command application reference is incomplete"
                )
            application_ref = (
                EntityRef.parse(data["application_ref"])
                if data.get("application_ref") is not None
                else (
                    EntityRef.from_wire(raw_application_id, raw_application_domain)
                    if raw_application_id is not None
                    else self.worker_state.application_ref
                )
            )
            raw_command_ref = data.get("command_ref")
            legacy_command_id = data.get("id")
            command_ref = None
            if raw_command_ref is not None:
                command_ref = EntityRef.parse(raw_command_ref)
            elif "command_ref" not in data and legacy_command_id is not None:
                command_ref = EntityRef.from_wire(
                    legacy_command_id,
                    data.get("origin_domain") or application_ref.domain,
                )
            return ApplicationCommandPermissionsUpdateEvent(
                target=target,
                application_ref=application_ref,
                guild_ref=guild_ref,
                command_ref=command_ref,
                permissions=tuple(
                    item
                    for item in data.get("permissions", [])
                    if isinstance(item, dict)
                ),
                raw=data,
            )
        if event_type in {
            "AUTO_MODERATION_RULE_CREATE",
            "AUTO_MODERATION_RULE_UPDATE",
            "AUTO_MODERATION_RULE_DELETE",
        }:
            guild_ref = _gateway_guild_scope(target, topic, data)
            return _auto_mod_rule_response(self, target, guild_ref, data)
        if event_type == "AUTO_MODERATION_ACTION_EXECUTION":
            guild_ref = _gateway_guild_scope(target, topic, data)
            execution = AutoModExecution.from_payload(data)
            if execution.guild_ref != guild_ref:
                raise ValueError("AutoMod execution changed its subscribed guild")
            lineage = [execution.guild_ref]
            for reference in (
                execution.channel_ref,
                execution.alert_system_message_ref,
                execution.action.action.channel_ref,
            ):
                if reference is not None:
                    lineage.append(reference)
            return _scoped_resource_response(
                execution,
                scope=guild_ref,
                resource_ref=execution.rule_ref,
                lineage_refs=lineage,
                label="AutoMod execution",
            )
        if event_type in {
            "GUILD_SOUNDBOARD_SOUND_CREATE",
            "GUILD_SOUNDBOARD_SOUND_UPDATE",
        }:
            guild_ref = _gateway_guild_scope(target, topic, data)
            return _soundboard_response(self, target, guild_ref, data)
        if event_type == "GUILD_SOUNDBOARD_SOUND_DELETE":
            guild_ref = _gateway_guild_scope(target, topic, data)
            deleted_sound = SoundboardSoundDeleteEvent(
                target=target,
                sound_ref=EntityRef.from_wire(data["id"], data["origin_domain"]),
                guild_ref=guild_ref,
            )
            return _scoped_resource_response(
                deleted_sound,
                scope=guild_ref,
                resource_ref=deleted_sound.sound_ref,
                lineage_refs=(deleted_sound.guild_ref,),
                label="soundboard sound",
            )
        if event_type == "GUILD_SOUNDBOARD_SOUNDS_UPDATE":
            guild_ref = _gateway_guild_scope(target, topic, data)
            sounds = [
                _soundboard_response(self, target, guild_ref, item)
                for item in data.get("sounds", data.get("soundboard_sounds", []))
                if isinstance(item, dict)
            ]
            return SoundboardSoundsUpdateEvent(
                target=target,
                guild_ref=guild_ref,
                sounds=tuple(
                    _scoped_resource_list(
                        sounds,
                        resource_ref=lambda sound: sound.ref,
                        label="soundboard sound",
                    )
                ),
            )
        if event_type == "GUILD_MEMBERS_PRUNED":
            guild_ref = _gateway_guild_scope(target, topic, data)
            result = PruneResult.from_payload(data)
            if result.guild_ref != guild_ref:
                raise ValueError("prune event changed its subscribed guild")
            return result
        if event_type in {
            "GUILD_SCHEDULED_EVENT_CREATE",
            "GUILD_SCHEDULED_EVENT_UPDATE",
            "GUILD_SCHEDULED_EVENT_DELETE",
        }:
            guild_ref = _gateway_guild_scope(target, topic, data)
            return _scheduled_event_response(self, target, guild_ref, data)
        if event_type in {
            "GUILD_SCHEDULED_EVENT_USER_ADD",
            "GUILD_SCHEDULED_EVENT_USER_REMOVE",
        }:
            guild_ref = _gateway_guild_scope(target, topic, data)
            scheduled_guild_domain = guild_ref.domain
            event_domain = (
                data.get("guild_scheduled_event_domain") or scheduled_guild_domain
            )
            subscribed = ScheduledEventUserEvent(
                target=target,
                guild_ref=guild_ref,
                event_ref=EntityRef.from_wire(
                    data["guild_scheduled_event_id"], event_domain
                ),
                user_ref=EntityRef.from_wire(data["user_id"], data["user_domain"]),
                added=event_type == "GUILD_SCHEDULED_EVENT_USER_ADD",
            )
            return _scoped_resource_response(
                subscribed,
                scope=guild_ref,
                resource_ref=subscribed.event_ref,
                lineage_refs=(subscribed.guild_ref,),
                label="scheduled event subscription",
            )
        if event_type in {
            "STAGE_INSTANCE_CREATE",
            "STAGE_INSTANCE_UPDATE",
            "STAGE_INSTANCE_DELETE",
        }:
            guild_ref = _gateway_guild_scope(target, topic, data)
            channel_ref = EntityRef.from_wire(
                data["channel_id"], data["channel_domain"]
            )
            stage = _stage_instance_response(self, target, channel_ref, data)
            if stage.guild_ref != guild_ref:
                raise ValueError("Stage event changed its subscribed guild")
            return stage
        if event_type in {"INVITE_CREATE", "INVITE_DELETE"}:
            return Invite.from_payload(self, target, data)
        if event_type in {"GUILD_CREATE", "GUILD_UPDATE"} and "name" in data:
            return Guild.from_payload(self, target, data)
        if event_type == "GUILD_DELETE":
            return GuildDeleteEvent(
                target,
                EntityRef.from_wire(data["id"], data["origin_domain"]),
            )
        if event_type in {"THREAD_CREATE", "THREAD_UPDATE"} and "type" in data:
            return Channel.from_payload(self, target, data)
        if event_type == "THREAD_DELETE":
            return ThreadDeleteEvent(
                target=target,
                thread_ref=EntityRef.from_wire(data["id"], data["origin_domain"]),
                guild_ref=EntityRef.from_wire(data["guild_id"], data["guild_domain"]),
                parent_ref=EntityRef.from_wire(
                    data["parent_id"], data["parent_domain"]
                ),
                type=int(data["type"]),
            )
        if event_type == "THREAD_LIST_SYNC":
            guild_ref = (
                EntityRef.from_wire(data["guild_id"], data["guild_domain"])
                if data.get("guild_id") is not None
                and data.get("guild_domain") is not None
                else None
            )
            raw_channel_ids = data.get("channel_ids")
            runtime_binding = _gateway_runtime_binding(data)
            threads = tuple(
                Channel.from_payload(self, target, item).bind_runtime(
                    **runtime_binding,
                    reject_unasserted=True,
                )
                for item in data.get("threads", [])
                if isinstance(item, dict)
            )
            domains = {thread.ref.id: thread.ref.domain for thread in threads}
            default_domain = (
                guild_ref.domain
                if guild_ref is not None
                else next(iter(domains.values()), urlsplit(target).hostname or "")
            )
            members: list[ThreadMember] = []
            for item in data.get("members", []):
                if not isinstance(item, dict):
                    continue
                normalized = dict(item)
                thread_id = normalized.get("id", normalized.get("thread_id"))
                if (
                    not isinstance(normalized.get("thread_domain"), str)
                    and thread_id is not None
                ):
                    normalized["thread_domain"] = domains.get(
                        int(thread_id), default_domain
                    )
                members.append(
                    ThreadMember.from_payload(
                        normalized,
                        default_domain=default_domain,
                        client=self,
                        target=target,
                    )
                )
            return ThreadListSyncEvent(
                target=target,
                guild_ref=guild_ref,
                channel_refs=(
                    tuple(
                        EntityRef.from_wire(channel_id, guild_ref.domain)
                        if "@" not in str(channel_id) and guild_ref is not None
                        else EntityRef.parse(
                            str(channel_id),
                            default_domain=default_domain,
                        )
                        for channel_id in raw_channel_ids
                    )
                    if isinstance(raw_channel_ids, list)
                    else None
                ),
                threads=threads,
                members=tuple(members),
            )
        if event_type == "THREAD_MEMBER_UPDATE":
            thread_ref = EntityRef.from_wire(data["id"], data["thread_domain"])
            return ThreadMemberUpdateEvent(
                target=target,
                member=ThreadMember.from_payload(
                    data,
                    default_domain=thread_ref.domain,
                    default_thread=thread_ref,
                    client=self,
                    target=target,
                ),
            )
        if event_type == "THREAD_MEMBERS_UPDATE":
            thread_ref = EntityRef.from_wire(data["id"], data["thread_domain"])
            guild_ref = EntityRef.from_wire(data["guild_id"], data["guild_domain"])
            removed_member_refs = tuple(
                EntityRef.from_wire(item["id"], item["origin_domain"])
                for item in data.get("removed_member_refs", [])
                if isinstance(item, dict)
                and item.get("id") is not None
                and isinstance(item.get("origin_domain"), str)
            )
            if not removed_member_refs:
                removed_member_refs = tuple(
                    EntityRef.parse(item)
                    if "@" in str(item)
                    else EntityRef.from_wire(item, guild_ref.domain)
                    for item in data.get("removed_member_ids", [])
                )
            return ThreadMembersUpdateEvent(
                target=target,
                thread_ref=thread_ref,
                guild_ref=guild_ref,
                member_count=int(data.get("member_count", 0)),
                added_members=tuple(
                    ThreadMember.from_payload(
                        item,
                        default_domain=thread_ref.domain,
                        default_thread=thread_ref,
                        client=self,
                        target=target,
                    )
                    for item in data.get("added_members", [])
                    if isinstance(item, dict)
                ),
                removed_member_refs=removed_member_refs,
            )
        if event_type == "DM_OPEN_REJECTED":
            return DMOpenRejectedEvent(
                target=target,
                pair_key=str(data["pair_key"]),
                code=str(data["code"]),
                authority_domain=str(data["authority_domain"]),
            )
        if event_type in {
            "CALL_CREATE",
            "CALL_RING",
            "CALL_ACCEPT",
            "CALL_DECLINE",
            "CALL_END",
        }:
            try:
                call = Call.from_payload(self, target, data)
                context = None
                if call.dm_capability_id is not None:
                    context = self._dm_capabilities.get(
                        (call.channel_ref, call.dm_capability_id)
                    )
                    if context is None or context.expires_at <= time.time():
                        raise ValueError(
                            "call event asserted an unknown DM capability lineage"
                        )
                _require_call_dm_capability_context(call, context)
                return call
            except (KeyError, TypeError, ValueError):
                return RawEvent(target, event_type, data, topic, sequence)
        if event_type in {"CHANNEL_CREATE", "CHANNEL_UPDATE"} and "type" in data:
            return Channel.from_payload(self, target, data)
        if event_type == "CHANNEL_INFO":
            topic_guild_ref = _guild_ref_from_topic(topic)
            raw_guild_domain = data.get("guild_domain")
            if raw_guild_domain is not None and not isinstance(raw_guild_domain, str):
                return RawEvent(target, event_type, data, topic, sequence)
            guild_domain = raw_guild_domain or (
                topic_guild_ref.domain if topic_guild_ref is not None else ""
            )
            if not guild_domain or (
                topic_guild_ref is not None and guild_domain != topic_guild_ref.domain
            ):
                return RawEvent(target, event_type, data, topic, sequence)
            raw_channels = data.get("channels")
            if not isinstance(raw_channels, list):
                return RawEvent(target, event_type, data, topic, sequence)
            try:
                guild_ref = EntityRef.from_wire(data.get("guild_id"), guild_domain)
            except ValueError:
                return RawEvent(target, event_type, data, topic, sequence)
            channels: list[VoiceChannelInfo] = []
            for item in raw_channels:
                if not isinstance(item, dict):
                    return RawEvent(target, event_type, data, topic, sequence)
                raw_status = item.get("status")
                raw_start = item.get("voice_start_time")
                if (
                    raw_status is not None
                    and (
                        not isinstance(raw_status, str)
                        or not 1 <= len(raw_status) <= 500
                        or "\x00" in raw_status
                    )
                ) or (
                    raw_start is not None
                    and (
                        isinstance(raw_start, bool)
                        or not isinstance(raw_start, int)
                        or raw_start <= 0
                    )
                ):
                    return RawEvent(target, event_type, data, topic, sequence)
                try:
                    channel_ref = EntityRef.from_wire(item.get("id"), guild_domain)
                except ValueError:
                    return RawEvent(target, event_type, data, topic, sequence)
                channels.append(
                    VoiceChannelInfo(
                        channel_ref=channel_ref,
                        status=raw_status,
                        voice_start_time=raw_start,
                    )
                )
            return ChannelInfoEvent(
                target=target,
                guild_ref=guild_ref,
                channels=tuple(channels),
                raw=data,
            )
        if event_type == "CHANNEL_DELETE":
            return ChannelDeleteEvent(
                target,
                EntityRef.from_wire(data["id"], data["origin_domain"]),
                (
                    EntityRef.from_wire(data["guild_id"], data["guild_domain"])
                    if data.get("guild_id") is not None
                    and data.get("guild_domain") is not None
                    else None
                ),
            )
        if event_type == "TRACKER_BOARD_UPDATE":
            authority = _gateway_event_authority(target, topic)
            channel_ref = EntityRef.from_wire(
                data["channel_id"], data["channel_domain"]
            )
            updated = TrackerBoardUpdateEvent(
                target=target,
                channel_ref=channel_ref,
                key_prefix=str(data["key_prefix"]),
                next_task_number=int(data["next_task_number"]),
                version=(
                    str(data["version"]) if data.get("version") is not None else None
                ),
                full_refresh=bool(data.get("full_refresh", False)),
                reason=(
                    str(data["reason"]) if data.get("reason") is not None else None
                ),
            )
            return _scoped_resource_response(
                updated,
                scope=authority,
                resource_ref=channel_ref,
                label="tracker board",
            )
        if event_type in {"TRACKER_LANE_CREATE", "TRACKER_LANE_UPDATE"} and isinstance(
            data.get("lane"), dict
        ):
            authority = _gateway_event_authority(target, topic)
            channel_ref = EntityRef.from_wire(
                data["channel_id"], data["channel_domain"]
            )
            if channel_ref.domain != authority:
                raise ValueError("tracker lane event changed its target authority")
            return _tracker_lane_response(
                self,
                target,
                channel_ref,
                {**data["lane"], "board_version": data.get("board_version")},
            )
        if event_type == "TRACKER_LANE_DELETE":
            authority = _gateway_event_authority(target, topic)
            deleted_lane = TrackerLaneDeleteEvent(
                target=target,
                channel_ref=EntityRef.from_wire(
                    data["channel_id"], data["channel_domain"]
                ),
                lane_ref=EntityRef.from_wire(data["lane_id"], data["lane_domain"]),
                board_version=(
                    str(data["board_version"])
                    if data.get("board_version") is not None
                    else None
                ),
            )
            return _scoped_resource_response(
                deleted_lane,
                scope=authority,
                resource_ref=deleted_lane.lane_ref,
                lineage_refs=(deleted_lane.channel_ref,),
                label="tracker lane",
            )
        if event_type in {"TRACKER_TASK_CREATE", "TRACKER_TASK_UPDATE"} and isinstance(
            data.get("task"), dict
        ):
            authority = _gateway_event_authority(target, topic)
            channel_ref = EntityRef.from_wire(
                data["channel_id"], data["channel_domain"]
            )
            if channel_ref.domain != authority:
                raise ValueError("tracker task event changed its target authority")
            return _tracker_task_response(
                self,
                target,
                channel_ref,
                {**data["task"], "board_version": data.get("board_version")},
            )
        if event_type == "TRACKER_TASK_DELETE":
            authority = _gateway_event_authority(target, topic)
            deleted_task = TrackerTaskDeleteEvent(
                target=target,
                channel_ref=EntityRef.from_wire(
                    data["channel_id"], data["channel_domain"]
                ),
                task_ref=EntityRef.from_wire(data["task_id"], data["task_domain"]),
                board_version=(
                    str(data["board_version"])
                    if data.get("board_version") is not None
                    else None
                ),
            )
            return _scoped_resource_response(
                deleted_task,
                scope=authority,
                resource_ref=deleted_task.task_ref,
                lineage_refs=(deleted_task.channel_ref,),
                label="tracker task",
            )
        if event_type in {"GUILD_ROLE_CREATE", "GUILD_ROLE_UPDATE"} and "name" in data:
            return Role.from_payload(self, target, data)
        if event_type == "GUILD_ROLE_DELETE":
            return RoleDeleteEvent(
                target,
                EntityRef.from_wire(data["id"], data["origin_domain"]),
                EntityRef.from_wire(data["guild_id"], data["guild_domain"]),
            )
        if (
            event_type
            in {
                "GUILD_EMOJI_CREATE",
                "GUILD_EMOJI_UPDATE",
            }
            and data.get("name") is not None
        ):
            guild_ref = _gateway_guild_scope(target, topic, data)
            return _emoji_response(self, target, guild_ref, data)
        if event_type == "GUILD_EMOJI_DELETE":
            guild_ref = _gateway_guild_scope(target, topic, data)
            deleted_emoji = EmojiDeleteEvent(
                target,
                EntityRef.from_wire(data["id"], data["origin_domain"]),
                guild_ref,
            )
            return _scoped_resource_response(
                deleted_emoji,
                scope=guild_ref,
                resource_ref=deleted_emoji.emoji_ref,
                lineage_refs=(deleted_emoji.guild_ref,),
                label="emoji",
            )
        if event_type == "GUILD_EMOJIS_UPDATE":
            guild_ref = _gateway_guild_scope(target, topic, data)
            emojis = [
                _emoji_response(self, target, guild_ref, item)
                for item in data.get("emojis", [])
                if isinstance(item, dict)
            ]
            return EmojisUpdateEvent(
                target=target,
                guild_ref=guild_ref,
                emojis=tuple(
                    _scoped_resource_list(
                        emojis,
                        resource_ref=lambda emoji: emoji.ref,
                        label="emoji",
                    )
                ),
            )
        if (
            event_type
            in {
                "GUILD_STICKER_CREATE",
                "GUILD_STICKER_UPDATE",
            }
            and data.get("name") is not None
        ):
            guild_ref = _gateway_guild_scope(target, topic, data)
            return _sticker_response(self, target, guild_ref, data)
        if event_type == "GUILD_STICKER_DELETE":
            guild_ref = _gateway_guild_scope(target, topic, data)
            deleted_sticker = StickerDeleteEvent(
                target,
                EntityRef.from_wire(data["id"], data["origin_domain"]),
                guild_ref,
            )
            return _scoped_resource_response(
                deleted_sticker,
                scope=guild_ref,
                resource_ref=deleted_sticker.sticker_ref,
                lineage_refs=(deleted_sticker.guild_ref,),
                label="sticker",
            )
        if event_type == "GUILD_STICKERS_UPDATE":
            guild_ref = _gateway_guild_scope(target, topic, data)
            stickers = [
                _sticker_response(self, target, guild_ref, item)
                for item in data.get("stickers", [])
                if isinstance(item, dict)
            ]
            return StickersUpdateEvent(
                target=target,
                guild_ref=guild_ref,
                stickers=tuple(
                    _scoped_resource_list(
                        stickers,
                        resource_ref=lambda sticker: sticker.ref,
                        label="sticker",
                    )
                ),
            )
        if event_type == "GUILD_MEMBERS_CHUNK":
            allowed = {
                "guild_id",
                "guild_domain",
                "members",
                "presences",
                "chunk_index",
                "chunk_count",
                "not_found",
                "nonce",
            }
            if not set(data) <= allowed or not {
                "guild_id",
                "guild_domain",
                "members",
                "chunk_index",
                "chunk_count",
                "not_found",
            } <= set(data):
                raise ValueError("guild member chunk is invalid")
            chunk_guild_domain = data.get("guild_domain")
            topic_guild_ref = _guild_ref_from_topic(topic)
            try:
                chunk_guild_ref = EntityRef.from_wire(
                    data.get("guild_id"), chunk_guild_domain
                )
            except ValueError:
                raise ValueError("guild member chunk is invalid")
            if topic_guild_ref is not None and topic_guild_ref != chunk_guild_ref:
                raise ValueError("guild member chunk does not match its Gateway topic")
            raw_members = data.get("members")
            if (
                not isinstance(raw_members, list)
                or len(raw_members) > 1000
                or any(not isinstance(item, dict) for item in raw_members)
            ):
                raise ValueError("guild member chunk is invalid")
            chunk_members = tuple(
                Member.from_payload(self, target, item) for item in raw_members
            )
            if any(member.guild_ref != chunk_guild_ref for member in chunk_members):
                raise ValueError(
                    "guild member chunk contains a member from another guild"
                )
            raw_chunk_index = data.get("chunk_index")
            raw_chunk_count = data.get("chunk_count")
            if (
                isinstance(raw_chunk_index, bool)
                or not isinstance(raw_chunk_index, int)
                or isinstance(raw_chunk_count, bool)
                or not isinstance(raw_chunk_count, int)
                or not 1 <= raw_chunk_count <= 1_000_000
                or not 0 <= raw_chunk_index < raw_chunk_count
            ):
                raise ValueError("guild member chunk index is invalid")
            raw_not_found = data.get("not_found")
            if (
                not isinstance(raw_not_found, list)
                or len(raw_not_found) > 100
                or any(
                    not isinstance(item, str)
                    or not item
                    or "\x00" in item
                    or _gateway_utf8_size(
                        item,
                        field="guild member chunk not_found reference",
                    )
                    > 320
                    for item in raw_not_found
                )
            ):
                raise ValueError("guild member chunk not_found is invalid")
            chunk_nonce = data.get("nonce")
            if chunk_nonce is not None and (
                not isinstance(chunk_nonce, str)
                or "\x00" in chunk_nonce
                or _gateway_utf8_size(chunk_nonce, field="guild member chunk nonce")
                > 32
            ):
                raise ValueError("guild member chunk nonce is invalid")
            raw_presences = data.get("presences", [])
            if (
                not isinstance(raw_presences, list)
                or len(raw_presences) > 1000
                or any(not isinstance(item, dict) for item in raw_presences)
            ):
                raise ValueError("guild member chunk presences are invalid")
            chunk_member_refs = {member.user.ref for member in chunk_members}
            chunk_presence_refs: set[EntityRef] = set()
            chunk_presences: list[dict[str, object]] = []
            for raw_presence in raw_presences:
                if set(raw_presence) != {
                    "user",
                    "status",
                    "activities",
                    "client_status",
                }:
                    raise ValueError("guild member chunk presence is invalid")
                raw_user = raw_presence.get("user")
                chunk_status = raw_presence.get("status")
                chunk_client_status = raw_presence.get("client_status")
                if (
                    not isinstance(raw_user, dict)
                    or set(raw_user) != {"id", "origin_domain"}
                    or chunk_status not in {"online", "idle", "dnd", "offline"}
                    or not isinstance(chunk_client_status, dict)
                    or any(
                        platform not in {"desktop", "mobile", "web"}
                        or platform_status not in {"online", "idle", "dnd"}
                        for platform, platform_status in chunk_client_status.items()
                    )
                ):
                    raise ValueError("guild member chunk presence is invalid")
                try:
                    chunk_user_ref = EntityRef.from_wire(
                        raw_user.get("id"), raw_user.get("origin_domain")
                    )
                except ValueError:
                    raise ValueError("guild member chunk presence is invalid") from None
                if (
                    chunk_user_ref not in chunk_member_refs
                    or chunk_user_ref in chunk_presence_refs
                ):
                    raise ValueError(
                        "guild member chunk presence is not bound to one member"
                    )
                chunk_presence_refs.add(chunk_user_ref)
                chunk_presences.append(
                    {
                        "user": dict(raw_user),
                        "status": chunk_status,
                        "activities": _gateway_presence_activities(
                            raw_presence.get("activities")
                        ),
                        "client_status": dict(chunk_client_status),
                    }
                )
            return GuildMembersChunkEvent(
                target=target,
                guild_ref=chunk_guild_ref,
                members=chunk_members,
                presences=tuple(chunk_presences),
                chunk_index=raw_chunk_index,
                chunk_count=raw_chunk_count,
                not_found=tuple(raw_not_found),
                nonce=chunk_nonce,
            )
        if event_type in {"GUILD_MEMBER_ADD", "GUILD_MEMBER_UPDATE"} and isinstance(
            data.get("user"), dict
        ):
            return Member.from_payload(self, target, data)
        if event_type == "GUILD_MEMBER_REMOVE":
            return MemberRemoveEvent(
                target,
                EntityRef.from_wire(data["guild_id"], data["guild_domain"]),
                EntityRef.from_wire(data["user_id"], data["user_domain"]),
            )
        if event_type == "TYPING_START":
            return TypingEvent(
                target,
                EntityRef.from_wire(data["channel_id"], data["channel_domain"]),
                EntityRef.from_wire(data["user_id"], data["user_domain"]),
                int(data["timestamp"]),
            )
        if event_type == "PRESENCE_UPDATE" and all(
            key in data for key in ("user_id", "user_domain", "status")
        ):
            topic_guild_ref = _guild_ref_from_topic(topic)
            status = data.get("status")
            afk = data.get("afk", False)
            client_status = data.get("client_status", {})
            if (
                status not in {"online", "idle", "dnd", "offline"}
                or type(afk) is not bool
                or not isinstance(client_status, dict)
                or any(
                    platform not in {"desktop", "mobile", "web"}
                    or platform_status not in {"online", "idle", "dnd"}
                    for platform, platform_status in client_status.items()
                )
            ):
                raise ValueError("presence event is invalid")
            try:
                presence_user_ref = EntityRef.from_wire(
                    data.get("user_id"), data.get("user_domain")
                )
            except ValueError:
                raise ValueError("presence event is invalid") from None
            return PresenceEvent(
                target=target,
                user_ref=presence_user_ref,
                status=str(status),
                custom_status=(
                    str(data["custom_status"])
                    if data.get("custom_status") is not None
                    else None
                ),
                activities=tuple(
                    _gateway_presence_activities(data.get("activities", []))
                ),
                since=_gateway_presence_since(data.get("since")),
                afk=afk,
                client_status=dict(client_status),
                raw=data,
                guild_ref=(
                    EntityRef.from_wire(data["guild_id"], data["guild_domain"])
                    if data.get("guild_id") is not None
                    and data.get("guild_domain") is not None
                    else topic_guild_ref
                ),
            )
        if event_type == "VOICE_STATE_UPDATE":
            topic_guild_ref = _guild_ref_from_topic(topic)
            voice_guild_domain = (
                data.get("guild_domain")
                or data.get("channel_domain")
                or (topic_guild_ref.domain if topic_guild_ref is not None else None)
            )
            user_domain = data.get("user_domain")
            voice_channel_domain = data.get("channel_domain") or voice_guild_domain
            participants = data.get("participants")
            return VoiceStateEvent(
                target=target,
                guild_ref=(
                    EntityRef.from_wire(data["guild_id"], voice_guild_domain)
                    if data.get("guild_id") is not None
                    and voice_guild_domain is not None
                    else topic_guild_ref
                ),
                channel_ref=(
                    EntityRef.from_wire(data["channel_id"], voice_channel_domain)
                    if data.get("channel_id") is not None
                    and voice_channel_domain is not None
                    else None
                ),
                user_ref=(
                    EntityRef.from_wire(data["user_id"], user_domain)
                    if data.get("user_id") is not None and user_domain is not None
                    else None
                ),
                connected=optional_payload_bool(data, "connected"),
                self_mute=optional_payload_bool(data, "self_mute"),
                self_deaf=optional_payload_bool(data, "self_deaf"),
                server_mute=optional_payload_bool(
                    data,
                    "server_mute",
                    aliases=("mute",),
                ),
                server_deaf=optional_payload_bool(
                    data,
                    "server_deaf",
                    aliases=("deaf",),
                ),
                participants=tuple(
                    item for item in participants if isinstance(item, dict)
                )
                if isinstance(participants, list)
                else (),
                heartbeat=strict_payload_bool(data, "heartbeat", default=False),
                raw=data,
            )
        if event_type == "VOICE_CHANNEL_EFFECT_SEND":
            topic_guild_ref = _guild_ref_from_topic(topic)
            guild_domain = data.get("guild_domain") or (
                topic_guild_ref.domain if topic_guild_ref is not None else ""
            )
            return VoiceChannelEffectEvent(
                target=target,
                channel_ref=EntityRef.from_wire(
                    data["channel_id"],
                    data.get("channel_domain") or guild_domain,
                ),
                guild_ref=EntityRef.from_wire(data["guild_id"], guild_domain),
                user_ref=EntityRef.from_wire(data["user_id"], data["user_domain"]),
                emoji=(str(data["emoji"]) if data.get("emoji") is not None else None),
                sound_id=(
                    int(data["sound_id"]) if data.get("sound_id") is not None else None
                ),
                raw=data,
            )
        if event_type == "VOICE_CHANNEL_STATUS_UPDATE":
            topic_guild_ref = _guild_ref_from_topic(topic)
            if topic_guild_ref is None:
                return RawEvent(target, event_type, data, topic, sequence)
            return VoiceChannelStatusEvent(
                target=target,
                channel_ref=EntityRef.from_wire(
                    data["id"],
                    data.get("origin_domain") or topic_guild_ref.domain,
                ),
                guild_ref=topic_guild_ref,
                status=(
                    str(data["status"]) if data.get("status") is not None else None
                ),
            )
        if event_type == "VOICE_CHANNEL_START_TIME_UPDATE":
            topic_guild_ref = _guild_ref_from_topic(topic)
            if topic_guild_ref is None:
                return RawEvent(target, event_type, data, topic, sequence)
            raw_start_time = data.get("voice_start_time")
            if raw_start_time is not None and (
                isinstance(raw_start_time, bool) or not isinstance(raw_start_time, int)
            ):
                return RawEvent(target, event_type, data, topic, sequence)
            return VoiceChannelStartTimeEvent(
                target=target,
                channel_ref=EntityRef.from_wire(
                    data["id"],
                    data.get("origin_domain") or topic_guild_ref.domain,
                ),
                guild_ref=topic_guild_ref,
                voice_start_time=raw_start_time,
            )
        return RawEvent(target, event_type, data, topic, sequence)

    async def _report_handler_error(
        self, event_type: str, target: str, error: Exception
    ) -> None:
        if event_type == "ERROR":
            return
        payload = RawEvent(
            target,
            "ERROR",
            {"event_type": event_type, "error": error},
        )
        for handler in tuple(self._handlers.get("ERROR", [])):
            with suppress(Exception):
                await handler(payload)

    async def _handle_authoritative_voice_event(
        self,
        event_type: str,
        data: dict[str, Any],
        origin: str,
    ) -> None:
        """Apply private, connection-bound moderator controls to an active client."""

        if event_type == "VOICE_TOKEN":
            raw_grant = data.get("grant")
            if not isinstance(raw_grant, dict):
                return
            grant = VoiceGrant.from_payload(raw_grant)
            voice = self._voice_clients.get(grant.connection_id)
            if voice is None or (
                grant.channel_ref == voice.grant.channel_ref
                and grant.generation <= voice.grant.generation
            ):
                return
            if (
                voice.target != origin
                or urlsplit(grant.url).hostname != grant.channel_ref.domain
            ):
                raise E2EEProtocolError(
                    "authority voice move changed its bound media destination"
                )
            context = (
                self._voice_e2ee_contexts.get(grant.channel_ref) if grant.e2ee else None
            )
            await voice.move_to(grant, e2ee_context=context)
            return
        if event_type != "VOICE_STATE_UPDATE":
            return
        connection_id = data.get("connection_id")
        generation = data.get("generation")
        if not isinstance(connection_id, str) or type(generation) is not int:
            return
        voice = self._voice_clients.get(connection_id)
        if voice is None or generation < voice.grant.generation:
            return
        if voice.target != origin:
            raise E2EEProtocolError(
                "authority voice state changed a connection owned by another target"
            )
        if data.get("connected") is False:
            await voice.authority_disconnect()
            return
        await voice.apply_authority_state(
            generation=generation,
            server_mute=(
                bool(data["server_mute"])
                if isinstance(data.get("server_mute"), bool)
                else None
            ),
            server_deaf=(
                bool(data["server_deaf"])
                if isinstance(data.get("server_deaf"), bool)
                else None
            ),
            can_listen=(
                bool(data["can_listen"])
                if isinstance(data.get("can_listen"), bool)
                else None
            ),
            can_speak=(
                bool(data["can_speak"])
                if isinstance(data.get("can_speak"), bool)
                else None
            ),
            can_stream=(
                bool(data["can_stream"])
                if isinstance(data.get("can_stream"), bool)
                else None
            ),
            can_priority_speak=(
                bool(data["can_priority_speak"])
                if isinstance(data.get("can_priority_speak"), bool)
                else None
            ),
        )

    @staticmethod
    def _apply_authored_rich_message(
        message: Message,
        data: Mapping[str, object],
    ) -> None:
        """Project a locally authored body after exact envelope echo validation."""

        message.content = cast(str | None, data["content"])
        message.embeds = [
            dict(item) for item in cast(list[dict[str, Any]], data["embeds"])
        ]
        message.components = [
            dict(item) for item in cast(list[dict[str, Any]], data["components"])
        ]
        private_poll = data.get("poll")
        if isinstance(private_poll, Mapping):
            merged_poll = dict(private_poll)
            if isinstance(message.poll, dict):
                for key in ("expiry", "finalized_at", "results"):
                    if key in message.poll:
                        merged_poll[key] = message.poll[key]
                answer_ids = message.poll.get("answer_ids")
                answers = merged_poll.get("answers")
                if isinstance(answer_ids, list) and isinstance(answers, list):
                    merged_poll["answers"] = [
                        {**dict(answer), "answer_id": answer_id}
                        for answer, answer_id in zip(answers, answer_ids, strict=True)
                        if isinstance(answer, Mapping)
                    ]
            message.poll = merged_poll
        else:
            message.poll = None
        message.sticker_items = [
            dict(item) for item in cast(list[dict[str, Any]], data["sticker_items"])
        ]
        forward_snapshot = data.get("forward_snapshot")
        message.forward_snapshot = (
            dict(cast(Mapping[str, Any], forward_snapshot))
            if isinstance(forward_snapshot, Mapping)
            else None
        )
        message.message_snapshots = (
            [{"message": dict(message.forward_snapshot)}]
            if message.forward_snapshot is not None
            else []
        )
        message.tts = bool(data["tts"])
        manifests = [
            dict(item) for item in cast(list[dict[str, Any]], data["attachments"])
        ]
        manifest_order = {
            f"{item.get('attachment_id')}@{item.get('attachment_domain')}": index
            for index, item in enumerate(manifests)
        }
        message.attachments.sort(key=lambda item: manifest_order[str(item.ref)])
        manifest_by_ref = {
            f"{item.get('attachment_id')}@{item.get('attachment_domain')}": item
            for item in manifests
        }
        for attachment in message.attachments:
            manifest = manifest_by_ref[str(attachment.ref)]
            attachment.encrypted_manifest = manifest
            attachment.filename = str(manifest["filename"])
            attachment.content_type = str(manifest["content_type"])
            attachment.size = int(manifest["plaintext_size"])
            duration_millis = manifest.get("duration_millis")
            attachment.duration_secs = (
                int(duration_millis) / 1000 if duration_millis is not None else None
            )
            attachment.waveform = (
                str(manifest["waveform"])
                if manifest.get("waveform") is not None
                else None
            )

    @staticmethod
    def _apply_decrypted_rich_message(message: Message, decrypted: Any) -> None:
        """Project only authenticated private fields onto the ordinary model."""

        message.content = decrypted.content
        message.embeds = [dict(item) for item in decrypted.embeds]
        message.components = [dict(item) for item in decrypted.components]
        message.poll = dict(decrypted.poll) if decrypted.poll is not None else None
        message.sticker_items = [dict(item) for item in decrypted.sticker_items]
        message.forward_snapshot = (
            dict(decrypted.forward_snapshot)
            if decrypted.forward_snapshot is not None
            else None
        )
        message.message_snapshots = (
            [{"message": dict(decrypted.forward_snapshot)}]
            if decrypted.forward_snapshot is not None
            else []
        )
        message.tts = decrypted.tts
        message.allowed_mentions = dict(decrypted.allowed_mentions)
        manifest_list = [dict(item) for item in decrypted.attachments]
        manifest_order = {
            f"{item.get('attachment_id')}@{item.get('attachment_domain')}": index
            for index, item in enumerate(manifest_list)
        }
        message.attachments.sort(key=lambda item: manifest_order[str(item.ref)])
        manifests = {
            f"{item.get('attachment_id')}@{item.get('attachment_domain')}": item
            for item in manifest_list
        }
        for attachment in message.attachments:
            manifest = manifests[str(attachment.ref)]
            attachment.encrypted_manifest = manifest
            attachment.filename = str(manifest["filename"])
            attachment.content_type = str(manifest["content_type"])
            attachment.size = int(manifest["plaintext_size"])
            duration_millis = manifest.get("duration_millis")
            attachment.duration_secs = (
                int(duration_millis) / 1000 if duration_millis is not None else None
            )
            attachment.waveform = (
                str(manifest["waveform"])
                if manifest.get("waveform") is not None
                else None
            )

    async def dispatch(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        target: str | None = None,
        topic: str | None = None,
        sequence: int = 0,
    ) -> None:
        event_type = event_name(event_type)
        origin = self._target(target)
        try:
            await self._handle_authoritative_voice_event(event_type, data, origin)
        except Exception as exc:
            await self._report_handler_error(event_type, origin, exc)
        model = self._event_model(
            event_type, data, target=origin, topic=topic, sequence=sequence
        )
        if (
            isinstance(model, Message)
            and isinstance(model.e2ee, dict)
            and "rich_payload_digest" in model.e2ee
        ):
            e2ee_context = self._interaction_e2ee_contexts.get(model.channel_ref)
            if e2ee_context is None:
                raise RuntimeError(
                    "encrypted rich message dispatch requires a current MLS context"
                )
            runtime_headers = await self._runtime_grant_headers(
                model.channel_ref,
                installation_id=model.bot_installation_id,
                dm_capability_id=model.dm_capability_id,
            )
            await self._sync_e2ee_control_log(
                e2ee_context,
                headers=runtime_headers,
                target=origin,
            )
            self._apply_decrypted_rich_message(
                model,
                decrypt_message(model, e2ee_context),
            )
        if isinstance(model, Interaction) and model.encrypted_payload is not None:
            e2ee_context = self._interaction_e2ee_contexts.get(model.channel_ref)
            if e2ee_context is None:
                raise RuntimeError(
                    "encrypted interaction dispatch requires a current MLS context"
                )
            lifecycle = self._interaction_lifecycle_grants.get(model.ref)
            if lifecycle is None:
                raise E2EEProtocolError(
                    "encrypted interaction is missing its exact lifecycle grant"
                )
            runtime_headers = {
                name: value
                for name, value in lifecycle.headers.items()
                if name != "X-Kaede-Interaction-Token"
            }
            runtime_headers.update(self._e2ee_device_headers())
            await self._sync_e2ee_control_log(
                e2ee_context,
                headers=runtime_headers,
                target=origin,
            )
            decrypted = decrypt_interaction(model, e2ee_context)
            model.options = decrypted.options
            model.values = decrypted.values
            model.components = decrypted.components
            model.attachment_manifests = decrypted.attachments
        for future, check in tuple(self._waiters.get(event_type, [])):
            if future.done():
                continue
            try:
                accepted = check is None or check(model)
            except Exception as exc:
                future.set_exception(exc)
                continue
            if accepted:
                future.set_result(model)
        handlers: list[Handler] = []
        if isinstance(model, Interaction):
            if model.command is not None and model.command.get("name") is not None:
                handlers.extend(
                    self._handlers.get(f"COMMAND:{model.command['name']}", [])
                )
            if model.custom_id is not None:
                view = (
                    (
                        self._views.get(model.message_ref)
                        if model.message_ref is not None
                        else None
                    )
                    or (
                        self._response_views.get(
                            EntityRef(model.response_id, model.channel_ref.domain)
                        )
                        if model.response_id is not None
                        else None
                    )
                    or self._persistent_views.get(model.custom_id)
                )
                if view is not None:
                    try:
                        await view.dispatch(model)
                    except Exception as exc:
                        await self._report_handler_error(event_type, origin, exc)
        handlers.extend(self._handlers.get(event_type, []))
        for handler in tuple(handlers):
            try:
                await handler(model)
            except Exception as exc:
                await self._report_handler_error(event_type, origin, exc)

    async def _save_cursors(self) -> None:
        async with self._cursor_lock:
            self.worker_state.save_cursors(
                {target: dict(cursors) for target, cursors in self._cursors.items()}
            )

    @staticmethod
    def _gateway_cursor_key(
        target: str,
        dm_capability: _DMCapabilityContext | None,
    ) -> str:
        if dm_capability is None:
            return target
        return (
            f"{target}#dm-capability:{dm_capability.grant_id}:{dm_capability.revision}"
        )

    async def _gateway_once(
        self,
        target: str,
        dm_capability_key: _DMCapabilityKey | None = None,
    ) -> None:
        if target not in self._targets:
            if dm_capability_key is None:
                await self.add_target(target)
            else:
                self._targets[target] = httpx.AsyncClient(base_url=target, timeout=30)
        dm_capability: _DMCapabilityContext | None = None
        if dm_capability_key is not None:
            dm_capability = self._dm_capabilities.get(dm_capability_key)
            if dm_capability is None or dm_capability.target != target:
                raise ApiError(
                    401,
                    "BOT_DM_GRANT_REQUIRED",
                    "The Gateway DM capability is no longer available",
                )
            if (
                dm_capability.expires_at
                <= time.time() + DM_CAPABILITY_REFRESH_WINDOW_SECONDS
            ):
                dm_capability = await self._refresh_dm_capability(
                    dm_capability_key,
                )
        token = await self._token(target, dm_capability=dm_capability)
        cursor_key = self._gateway_cursor_key(target, dm_capability)
        parsed = urlsplit(target)
        uri = f"wss://{parsed.netloc}/api/v1/bots/gateway"
        async with connect(uri, max_size=1_048_576, open_timeout=15) as socket:
            self._sockets.add(socket)
            if dm_capability is None:
                self._gateway_sockets[target] = socket
            hello = json.loads(await socket.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000
            timestamp = int(time.time())
            nonce = secrets.token_urlsafe(24)
            digest = hashlib.sha256(token.encode()).hexdigest()
            proof = self._sign(
                f"kaede-dpop-v1\nGET\n/api/v1/bots/gateway\n{timestamp}\n{nonce}\n{digest}".encode()
            )
            identify: dict[str, object] = {
                "op": 2,
                "token": token,
                "timestamp": timestamp,
                "nonce": nonce,
                "proof": proof,
                "cursors": self._cursors[cursor_key],
                "intents": self.intents.names(),
            }
            if self._e2ee_device_id is not None:
                identify["e2ee_device_id"] = self._e2ee_device_id
            await socket.send(json.dumps(identify))

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(interval)
                    await socket.send(json.dumps({"op": 1}))

            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                async for encoded in socket:
                    event = json.loads(encoded)
                    if event.get("op") != 0:
                        continue
                    topic = event.get("topic")
                    sequence = event.get("s", 0)
                    await self.dispatch(
                        str(event.get("t", "")),
                        event.get("d") or {},
                        target=target,
                        topic=topic if isinstance(topic, str) else None,
                        sequence=sequence if isinstance(sequence, int) else 0,
                    )
                    # Persist only after dispatch completes. A crash in a user
                    # handler then replays the event instead of acknowledging
                    # work that the application never finished.
                    if topic and isinstance(sequence, int) and sequence > 0:
                        self._cursors[cursor_key][topic] = sequence
                        await self._save_cursors()
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                self._sockets.discard(socket)
                if self._gateway_sockets.get(target) is socket:
                    self._gateway_sockets.pop(target, None)

    async def request_channel_info(
        self,
        guild_ref: EntityRef,
        *,
        fields: Sequence[Literal["status", "voice_start_time"]] = (
            "status",
            "voice_start_time",
        ),
        target: str | None = None,
    ) -> None:
        """Request Discord-compatible ephemeral channel data over Gateway op 43."""

        guild = guild_ref
        requested = tuple(fields)
        if (
            not requested
            or len(requested) != len(set(requested))
            or any(field not in {"status", "voice_start_time"} for field in requested)
        ):
            raise ValueError(
                "channel info fields must be unique status/voice_start_time values"
            )
        origin = self._authority_target(guild, target)
        socket = self._gateway_sockets.get(origin)
        if socket is None:
            raise RuntimeError("the target Gateway is not connected")
        await socket.send(
            json.dumps(
                {
                    "op": 43,
                    "d": {"guild_id": str(guild.id), "fields": list(requested)},
                }
            )
        )

    async def update_presence(
        self,
        *,
        status: Literal["online", "idle", "dnd", "invisible", "offline"] = "online",
        activities: Sequence[Mapping[str, object]] = (),
        since: int | None = None,
        afk: bool = False,
        target: str | None = None,
    ) -> None:
        """Set this bot's documented Discord presence on connected authorities."""

        if status not in {"online", "idle", "dnd", "invisible", "offline"}:
            raise ValueError("presence status is invalid")
        if type(afk) is not bool:
            raise TypeError("presence afk must be a boolean")
        rendered_activities = _gateway_presence_activities(activities)
        rendered_since = _gateway_presence_since(since)
        targets = (
            (canonical_target_origin(target),)
            if target is not None
            else tuple(sorted(self._gateway_sockets))
        )
        if not targets:
            raise RuntimeError("no target Gateway is connected")
        payload = json.dumps(
            {
                "op": 3,
                "d": {
                    "since": rendered_since,
                    "activities": rendered_activities,
                    "status": status,
                    "afk": afk,
                },
            }
        )
        for origin in targets:
            socket = self._gateway_sockets.get(origin)
            if socket is None:
                raise RuntimeError("the target Gateway is not connected")
            await socket.send(payload)

    async def update_voice_state(
        self,
        guild_ref: EntityRef,
        channel_ref: EntityRef | None,
        *,
        self_mute: bool = False,
        self_deaf: bool = False,
        target: str | None = None,
    ) -> None:
        """Join, move, update, or disconnect bot voice through Gateway op 4."""

        if type(self_mute) is not bool or type(self_deaf) is not bool:
            raise TypeError("voice self state must use booleans")
        if channel_ref is not None and channel_ref.domain != guild_ref.domain:
            raise ValueError("voice channel and guild must share one authority")
        origin = self._authority_target(guild_ref, target)
        socket = self._gateway_sockets.get(origin)
        if socket is None:
            raise RuntimeError("the target Gateway is not connected")
        await socket.send(
            json.dumps(
                {
                    "op": 4,
                    "d": {
                        "guild_id": str(guild_ref.id),
                        "channel_id": (
                            str(channel_ref.id) if channel_ref is not None else None
                        ),
                        "self_mute": self_mute,
                        "self_deaf": self_deaf,
                    },
                }
            )
        )

    async def request_guild_members(
        self,
        guild_ref: EntityRef,
        *,
        query: str | None = None,
        limit: int = 0,
        user_ids: Sequence[EntityRef] | None = None,
        presences: bool = False,
        nonce: str | None = None,
        target: str | None = None,
    ) -> None:
        """Request one Discord-style member chunk stream from a guild authority."""

        if type(presences) is not bool:
            raise TypeError("member request presences must be a boolean")
        if nonce is not None and (
            "\x00" in nonce
            or _gateway_utf8_size(nonce, field="member request nonce") > 32
        ):
            raise ValueError("member request nonce must contain at most 32 UTF-8 bytes")
        if user_ids is not None:
            users = tuple(user_ids)
            if query is not None or limit != 0:
                raise ValueError(
                    "member request query and user_ids are mutually exclusive"
                )
            if (
                not 1 <= len(users) <= 100
                or any(not isinstance(item, EntityRef) for item in users)
                or len({str(item) for item in users}) != len(users)
            ):
                raise ValueError("member request requires 1 to 100 unique user refs")
            request: dict[str, object] = {
                "guild_id": str(guild_ref.id),
                "user_ids": [str(item) for item in users],
                "presences": presences,
            }
        else:
            rendered_query = query or ""
            if (
                query is not None
                and (
                    not isinstance(query, str)
                    or "\x00" in query
                    or len(query) > 100
                    or _gateway_utf8_size(query, field="member request query") > 400
                )
            ) or (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 0 <= limit <= 100
                or (rendered_query != "" and limit == 0)
            ):
                raise ValueError("member request query or limit is invalid")
            request = {
                "guild_id": str(guild_ref.id),
                "query": rendered_query,
                "limit": limit,
                "presences": presences,
            }
        if nonce is not None:
            request["nonce"] = nonce
        origin = self._authority_target(guild_ref, target)
        socket = self._gateway_sockets.get(origin)
        if socket is None:
            raise RuntimeError("the target Gateway is not connected")
        await socket.send(json.dumps({"op": 8, "d": request}))

    async def request_soundboard_sounds(
        self,
        guild_refs: Sequence[EntityRef],
        *,
        target: str | None = None,
    ) -> None:
        """Request guild sound sets over Discord-compatible Gateway op 31."""

        guilds = tuple(guild_refs)
        if not 1 <= len(guilds) <= 100 or len({item.id for item in guilds}) != len(
            guilds
        ):
            raise ValueError("soundboard requests require 1 to 100 unique guild IDs")
        domains = {item.domain for item in guilds}
        if len(domains) != 1:
            raise ValueError(
                "one soundboard Gateway request cannot cross target instances"
            )
        origin = self._authority_target(guilds[0], target)
        socket = self._gateway_sockets.get(origin)
        if socket is None:
            raise RuntimeError("the target Gateway is not connected")
        await socket.send(
            json.dumps(
                {
                    "op": 31,
                    "d": {"guild_ids": [str(item.id) for item in guilds]},
                }
            )
        )

    async def gateway(
        self,
        target: str,
        dm_capability_key: _DMCapabilityKey | None = None,
    ) -> None:
        target = canonical_target_origin(target)
        backoff = 1.0
        while not self._stopping:
            try:
                await self._gateway_once(target, dm_capability_key)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                close_code = getattr(exc, "code", None)
                if close_code == 4009 or getattr(exc, "status", None) == 4009:
                    for token_key in tuple(self._tokens):
                        if token_key[0] != target or token_key[3]:
                            continue
                        if dm_capability_key is None and token_key[1] is None:
                            self._tokens.pop(token_key, None)
                        elif (
                            dm_capability_key is not None
                            and token_key[1] == dm_capability_key[1]
                        ):
                            self._tokens.pop(token_key, None)
                    if dm_capability_key is None:
                        await self._reconcile_dm_capabilities_for_target(target)
                    else:
                        try:
                            await self._refresh_dm_capability(
                                dm_capability_key,
                                force=True,
                            )
                        except ApiError as refresh_error:
                            if _dm_capability_error_is_terminal(refresh_error):
                                await self._drop_dm_capability(dm_capability_key)
                await self.dispatch(
                    "GATEWAY_ERROR",
                    {"error": str(exc), "retry_in": backoff},
                    target=target,
                )
            if not self._stopping:
                await asyncio.sleep(backoff + secrets.randbelow(500) / 1000)
                backoff = min(30.0, backoff * 2)

    def _ensure_gateway_task(self, target: str) -> asyncio.Task[None]:
        task = self._gateway_tasks.get(target)
        if task is None or task.done():
            task = asyncio.create_task(
                self.gateway(target), name=f"kaede-gateway:{target}"
            )
            self._gateway_tasks[target] = task
        return task

    def _ensure_dm_gateway_task(
        self,
        key: _DMCapabilityKey,
    ) -> asyncio.Task[None]:
        context = self._dm_capabilities.get(key)
        if context is None:
            raise ApiError(
                401,
                "BOT_DM_GRANT_REQUIRED",
                "The Gateway DM capability is no longer available",
            )
        task = self._dm_gateway_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self.gateway(context.target, key),
                name=f"kaede-gateway:{context.target}:{context.grant_id}",
            )
            self._dm_gateway_tasks[key] = task
        return task

    async def _remove_discovered_target(self, target: str) -> None:
        if (
            target in self._explicit_targets
            or target in self._discovered_targets
            or self._capability_targets.get(target)
        ):
            return
        task = self._gateway_tasks.pop(target, None)
        current_task = asyncio.current_task()
        cancel_current = task is current_task
        if task is not None and not cancel_current:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        client = self._targets.pop(target, None)
        for token_key in tuple(self._tokens):
            if token_key[0] == target:
                self._tokens.pop(token_key, None)
        if client is not None:
            await client.aclose()
        if cancel_current and current_task is not None:
            # A 4009 reconciliation runs inside this target's Gateway task.
            # Finish deterministic token/client cleanup, then arrange for that
            # task to exit at its next suspension point without self-awaiting.
            current_task.cancel()

    async def _apply_discovered_targets(self, origins: Sequence[str]) -> None:
        current = set(origins)
        removed = self._discovered_targets - current
        self._discovered_targets = current
        for target in sorted(removed):
            await self._remove_discovered_target(target)
        for target in sorted(current):
            try:
                await self.add_target(target)
            except Exception as exc:
                await self.dispatch(
                    "TARGET_DISCOVERY_ERROR",
                    {"target": target, "error": str(exc)},
                    target=target,
                )
                continue
            self._ensure_gateway_task(target)

    async def _target_discovery_loop(
        self, application_home: str, poll_after: int
    ) -> None:
        while not self._stopping:
            await asyncio.sleep(poll_after)
            try:
                origins, poll_after = await self.discover_targets(
                    application_home=application_home
                )
                await self._apply_discovered_targets(origins)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.dispatch(
                    "TARGET_DISCOVERY_ERROR",
                    {"application_home": application_home, "error": str(exc)},
                    target=application_home,
                )
                poll_after = min(300, max(5, poll_after * 2))

    async def start(
        self,
        *targets: str,
        application_home: str | None = None,
        auto_discover: bool = True,
    ) -> None:
        if self._started or self._starting:
            raise RuntimeError("the client is already starting or running")
        self._starting = True
        home = canonical_application_home(
            application_home or f"https://{self.worker_state.application_ref.domain}",
            self.worker_state.application_ref,
        )
        try:
            self._application_home = home
            self._stopping = False
            identity = await self.fetch_bot_identity(target=home)
            if "dm.send" in identity.scopes:
                await self._bootstrap_dm_capabilities()
            else:
                await self._clear_dm_capability_state()
            if not targets and not auto_discover and not self._capability_targets:
                raise ValueError("at least one target instance is required")
            self._started = True
            self._explicit_targets = set(
                dict.fromkeys([await self.add_target(target) for target in targets])
            )
            for origin in sorted(self._explicit_targets):
                self._ensure_gateway_task(origin)
            for origin in sorted(self._capability_targets):
                if origin not in self._targets:
                    self._targets[origin] = httpx.AsyncClient(
                        base_url=origin, timeout=30
                    )
            for key in sorted(
                self._dm_capabilities,
                key=lambda item: (str(item[0]), item[1]),
            ):
                self._ensure_dm_gateway_task(key)
            for key in tuple(self._dm_capabilities):
                task = self._dm_refresh_tasks.get(key)
                if task is None or task.done():
                    channel_ref, grant_id = key
                    self._dm_refresh_tasks[key] = asyncio.create_task(
                        self._dm_capability_refresh_loop(key),
                        name=f"kaede-dm-capability:{channel_ref}:{grant_id}",
                    )
            if not auto_discover:
                await asyncio.gather(
                    *self._gateway_tasks.values(),
                    *self._dm_gateway_tasks.values(),
                )
                return
            origins, poll_after = await self.discover_targets(application_home=home)
            await self._apply_discovered_targets(origins)
            self._discovery_task = asyncio.create_task(
                self._target_discovery_loop(home, poll_after),
                name="kaede-target-discovery",
            )
            await self._discovery_task
        except BaseException:
            await self.close()
            raise
        finally:
            self._starting = False

    async def close(self) -> None:
        self._stopping = True
        self._started = False
        if self._discovery_task is not None:
            self._discovery_task.cancel()
        for task in self._gateway_tasks.values():
            task.cancel()
        for task in self._dm_gateway_tasks.values():
            task.cancel()
        for task in self._dm_refresh_tasks.values():
            task.cancel()
        views = {
            id(view): view
            for view in (
                *self._views.values(),
                *self._response_views.values(),
                *self._persistent_views.values(),
            )
        }
        for view in views.values():
            view.stop()
        await asyncio.gather(
            *(voice.disconnect() for voice in tuple(self._voice_clients.values())),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(socket.close() for socket in tuple(self._sockets)),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(
                task
                for task in (
                    self._discovery_task,
                    *self._gateway_tasks.values(),
                    *self._dm_gateway_tasks.values(),
                    *self._dm_refresh_tasks.values(),
                )
                if task is not None
            ),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(client.aclose() for client in self._targets.values()),
            return_exceptions=True,
        )
        self._discovery_task = None
        self._gateway_tasks.clear()
        self._dm_gateway_tasks.clear()
        self._dm_refresh_tasks.clear()
        self._targets.clear()
        self._voice_clients.clear()
        self._tokens.clear()
        self._explicit_targets.clear()
        self._discovered_targets.clear()
