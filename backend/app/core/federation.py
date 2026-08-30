from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.chat.e2ee import validate_channel_encryption_policy
from app.core.dm import (
    GROUP_DM_MEMBER_ADDED,
    GROUP_DM_MEMBER_LEFT,
    GROUP_DM_MEMBER_REMOVED,
    MAX_GROUP_DM_PARTICIPANTS,
    group_dm_key,
)
from app.core.json_limits import FEDERATION_JSON_LIMITS, validate_json_tree
from app.core.permissions import PERMISSION_SCHEMA_CAPABILITY
from app.core.settings import DOMAIN_RE

POLICY_HELD_OUTBOX_PREFIX = "held by local federation block:"
POLICY_HELD_OUTBOX_DELAY = timedelta(days=36_500)
BLOCK_POLICY_ADVISORY_NAME = "kaede-instance-blocks"
DEVELOPER_TEAM_SNAPSHOT_EVENT = "developer.team.snapshot"
DURABLE_LATEST_STATE_EVENTS = frozenset(
    {
        DEVELOPER_TEAM_SNAPSHOT_EVENT,
        "bot.application.runtime.changed",
        "bot.application.target.changed",
        "bot.dm.installation-capability",
        "e2ee.device-list.changed",
        "e2ee.room-policy.changed",
        "guild.announcement.follow.accepted",
        "guild.announcement.follow.finalized",
        "guild.announcement.follow.rejected",
        "guild.announcement.follow.revoked",
        "guild.announcement.follow.updated",
    }
)
SECURITY_CRITICAL_GUILD_EVENTS = frozenset(
    {
        "guild.access.revoked",
        "guild.instance_access.revoked",
        "guild.leave.request",
        "guild.media.delete.request",
        "guild.resync.required",
        "media.delete",
        "relationship.remove",
        "bot.application.runtime.changed",
        "bot.dm.installation-capability",
        "e2ee.device-list.changed",
        "e2ee.room-policy.changed",
        "guild.announcement.follow.accepted",
        "guild.announcement.follow.finalized",
        "guild.announcement.follow.rejected",
        "guild.announcement.follow.revoked",
        "guild.announcement.follow.updated",
    }
)
GUILD_MUTATION_EVENT_TYPES = frozenset(
    {
        "guild.update",
        "guild.channel.create",
        "guild.channel.update",
        "guild.channel.delete",
        "guild.forum.cursor.update",
        "guild.tracker.board.invalidate",
        "guild.thread.member.upsert",
        "guild.thread.member.delete",
        "guild.role.create",
        "guild.role.update",
        "guild.role.delete",
        "guild.emoji.create",
        "guild.emoji.update",
        "guild.emoji.delete",
        "guild.sticker.create",
        "guild.sticker.update",
        "guild.sticker.delete",
        "guild.stage.instance.create",
        "guild.stage.instance.update",
        "guild.stage.instance.delete",
        "guild.scheduled_event.create",
        "guild.scheduled_event.update",
        "guild.scheduled_event.delete",
        "guild.scheduled_event.user.add",
        "guild.scheduled_event.user.remove",
        "guild.soundboard.sound.create",
        "guild.soundboard.sound.update",
        "guild.soundboard.sound.delete",
        "guild.soundboard.sounds.update",
        "guild.voice_channel_status.update",
        "guild.voice_channel_start_time.update",
        "guild.automod.rule.create",
        "guild.automod.rule.update",
        "guild.automod.rule.delete",
        "guild.automod.execution",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
        "guild.member.update",
        "guild.member.profile.relay",
        "guild.member.remove",
        "guild.members.origin.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
        "guild.ban.add",
        "guild.ban.remove",
        "guild.message.update",
        "guild.message.delete",
        "guild.message.bulk_delete",
        "guild.message.purge",
        "guild.reaction.add",
        "guild.reaction.remove",
        "guild.reaction.clear",
        "guild.poll.vote.add",
        "guild.poll.vote.remove",
        "guild.poll.finalize",
        "guild.pin.add",
        "guild.pin.remove",
    }
)
AUTHORITY_ATTESTED_GUILD_OWNER_EVENT_TYPES = GUILD_MUTATION_EVENT_TYPES | {
    "announcement.crosspost.sync",
    "guild.access.revoked",
    "guild.announcement.follow.authorized",
    "guild.announcement.follow.accepted",
    "guild.announcement.follow.finalized",
    "guild.announcement.follow.rejected",
    "guild.announcement.follow.revoked",
    "guild.announcement.follow.updated",
    "guild.announcement.follow.source_authorized",
    "guild.audit-log.page",
    "guild.event.redacted",
    "guild.instance_access.revoked",
    "guild.member.add",
    "guild.message.committed",
    "guild.resync.required",
    "guild.soundboard.play",
    "guild.soundboard.query",
    "guild.soundboard.source-capability",
    "message.send_rejected",
}
FEDERATION_CAPABILITIES = (
    PERMISSION_SCHEMA_CAPABILITY,
    "dm-history-page/1",
    "e2ee-mls/1",
    "e2ee-media/1",
    "bot-direct-auth/1",
    "group-dm/1",
    "guild-audit-log/1",
    "guild-soundboard/1",
    "guild-history-sync/1",
    "guild-history-sync/2",
    "member-self-moderation/1",
    "report-forwarding/1",
    "message-search/1",
    "presence/1",
    "profile-by-ref/1",
    "request-nonce/1",
)


def block_covers_domain(block_domain: str, include_subdomains: bool, destination: str) -> bool:
    """Return whether one normalized block rule covers a normalized peer."""

    return destination == block_domain or (
        include_subdomains and destination.endswith(f".{block_domain}")
    )


def federation_event_has_guild_context(event_type: str, context: object = None) -> bool:
    """Recognize guild-scoped events whose type is shared with DM traffic."""

    if event_type.startswith("guild."):
        return True
    if not isinstance(context, dict):
        return False
    if context.get("guild_id") is not None and context.get("guild_domain") is not None:
        return True
    scope = context.get("scope")
    return isinstance(scope, dict) and scope.get("type") == "guild"


def federation_policy_holds_event(
    level: str,
    event_type: str,
    *,
    context: object = None,
) -> bool:
    """Return whether a local block must prevent this outbound delivery."""

    return level == "suspend" or (
        level == "silence" and federation_event_has_guild_context(event_type, context)
    )


def terminal_room_generation(envelope: dict[str, Any]) -> int:
    content = envelope.get("content")
    raw_generation = content.get("_terminal_generation") if isinstance(content, dict) else None
    if (
        not isinstance(raw_generation, str)
        or not raw_generation.isascii()
        or not raw_generation.isdecimal()
        or (len(raw_generation) > 1 and raw_generation.startswith("0"))
    ):
        raise ValueError("terminal-room generation is invalid")
    generation = int(raw_generation)
    if not 1 <= generation <= (1 << 63) - 1:
        raise ValueError("terminal-room generation is invalid")
    return generation


def authority_attested_terminal_guild_actor(
    event_type: object,
    content: object,
    context: object,
    *,
    expected_authority: str,
    actor_id: object,
    actor_domain: object,
) -> bool:
    """Recognize the one terminal guild control an authority may sign for its owner.

    A guild owner can live on another instance after an ownership transfer.  The
    guild authority therefore has to attest that remote identity when it emits
    the immutable deletion proof.  Keep this predicate exact: ordinary guild
    controls and non-terminal instance revocations must remain actor-home signed.
    """

    target_domain = content.get("target_domain") if isinstance(content, dict) else None
    return bool(
        event_type == "guild.instance_access.revoked"
        and isinstance(context, dict)
        and set(context) == {"guild_id", "guild_domain"}
        and context.get("guild_domain") == expected_authority
        and isinstance(content, dict)
        and set(content) == {"target_domain", "reason", "_terminal_generation"}
        and content.get("reason") == "guild_deleted"
        and isinstance(target_domain, str)
        and target_domain
        and target_domain == target_domain.rstrip(".").lower()
        and DOMAIN_RE.fullmatch(target_domain)
        and isinstance(actor_id, str)
        and actor_id.isascii()
        and actor_id.isdecimal()
        and not (len(actor_id) > 1 and actor_id.startswith("0"))
        and int(actor_id) <= (1 << 63) - 1
        and isinstance(actor_domain, str)
        and actor_domain
        and actor_domain == actor_domain.rstrip(".").lower()
        and DOMAIN_RE.fullmatch(actor_domain)
    )


def guild_authority_event_ref(
    event_type: object,
    context: object,
    *,
    expected_authority: str,
) -> tuple[int, str] | None:
    """Parse the guild identity from one closed authority-owner event family."""

    if event_type not in AUTHORITY_ATTESTED_GUILD_OWNER_EVENT_TYPES:
        return None
    if not isinstance(context, dict):
        return None
    raw_guild_id = context.get("guild_id")
    guild_domain = context.get("guild_domain")
    if (
        not isinstance(raw_guild_id, str)
        or not raw_guild_id.isascii()
        or not raw_guild_id.isdecimal()
        or (len(raw_guild_id) > 1 and raw_guild_id.startswith("0"))
        or int(raw_guild_id) > (1 << 63) - 1
        or guild_domain != expected_authority
    ):
        return None
    return int(raw_guild_id), expected_authority


def authority_attested_guild_owner_actor(
    event_type: object,
    context: object,
    *,
    expected_authority: str,
    expected_guild_id: int,
    expected_owner: tuple[int, str],
    actor: tuple[int, str],
) -> bool:
    """Bind a remote signer to the current owner of one authority-local guild."""

    event_ref = guild_authority_event_ref(
        event_type,
        context,
        expected_authority=expected_authority,
    )
    return event_ref == (expected_guild_id, expected_authority) and actor == expected_owner


def guild_message_authority_event_refs(
    event_type: object,
    content: object,
    context: object,
    *,
    expected_authority: str,
) -> tuple[int, str, int, str] | None:
    """Parse an owner-attested guild message and its semantic author."""

    if event_type != "guild.message.create":
        return None
    if not isinstance(context, dict) or context.get("guild_domain") != expected_authority:
        return None
    raw_guild_id = context.get("guild_id")
    if (
        not isinstance(raw_guild_id, str)
        or not raw_guild_id.isascii()
        or not raw_guild_id.isdecimal()
        or (len(raw_guild_id) > 1 and raw_guild_id.startswith("0"))
        or int(raw_guild_id) > (1 << 63) - 1
    ):
        return None
    if not isinstance(content, dict) or not isinstance(content.get("message"), dict):
        return None
    message = content["message"]
    raw_author_id = message.get("author_id")
    author_domain = message.get("author_domain")
    if (
        not isinstance(raw_author_id, str)
        or not raw_author_id.isascii()
        or not raw_author_id.isdecimal()
        or (len(raw_author_id) > 1 and raw_author_id.startswith("0"))
        or int(raw_author_id) > (1 << 63) - 1
        or not isinstance(author_domain, str)
        or not author_domain
        or author_domain != author_domain.rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(author_domain)
    ):
        return None
    return int(raw_guild_id), expected_authority, int(raw_author_id), author_domain


def guild_crosspost_authority_event_ref(
    event_type: object,
    content: object,
    context: object,
    *,
    expected_authority: str,
) -> tuple[int, str] | None:
    """Parse one owner-attested announcement copy message."""

    if (
        event_type != "guild.message.create"
        or not isinstance(content, dict)
        or set(content) != {"message", "author", "thread_starter"}
        or content.get("thread_starter") is not False
    ):
        return None
    message = content.get("message")
    author = content.get("author")
    if not isinstance(message, dict) or not isinstance(author, dict):
        return None
    flags = message.get("flags", 0)
    if (
        isinstance(flags, bool)
        or not isinstance(flags, int)
        or not flags & (1 << 1)
        or message.get("message_type") != 0
        or message.get("origin_domain") != expected_authority
        or message.get("channel_domain") != expected_authority
        or message.get("forward_snapshot") is not None
    ):
        return None
    if not isinstance(context, dict) or context.get("guild_domain") != expected_authority:
        return None
    raw_guild_id = context.get("guild_id")

    def canonical_id(value: object) -> int | None:
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or (len(value) > 1 and value.startswith("0"))
            or int(value) > (1 << 63) - 1
        ):
            return None
        return int(value)

    def canonical_domain(value: object) -> str | None:
        if (
            not isinstance(value, str)
            or not value
            or value != value.rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(value)
        ):
            return None
        return value

    guild_id = canonical_id(raw_guild_id)
    target_message_id = canonical_id(message.get("id"))
    target_channel_id = canonical_id(message.get("channel_id"))
    source_message_id = canonical_id(message.get("forwarded_message_id"))
    source_channel_id = canonical_id(message.get("forwarded_channel_id"))
    author_id = canonical_id(message.get("author_id"))
    source_message_domain = canonical_domain(message.get("forwarded_message_domain"))
    source_channel_domain = canonical_domain(message.get("forwarded_channel_domain"))
    author_domain = canonical_domain(message.get("author_domain"))
    if (
        guild_id is None
        or target_message_id is None
        or target_channel_id is None
        or source_message_id is None
        or source_channel_id is None
        or author_id is None
        or source_message_domain is None
        or source_channel_domain is None
        or author_domain is None
        or context.get("channel_id") != str(target_channel_id)
        or context.get("channel_domain") != expected_authority
        or author.get("id") != str(author_id)
        or author.get("origin_domain") != author_domain
        or message.get("forwarded_message_ref") != f"{source_message_id}@{source_message_domain}"
    ):
        return None
    message_reference = message.get("message_reference")
    if message_reference != {
        "type": 0,
        "message_id": str(source_message_id),
        "message_domain": source_message_domain,
        "channel_id": str(source_channel_id),
        "channel_domain": source_channel_domain,
    }:
        return None
    return guild_id, expected_authority


def authority_attested_guild_crosspost_actor(
    event_type: object,
    content: object,
    context: object,
    *,
    expected_authority: str,
    expected_guild_id: int,
    expected_owner: tuple[int, str],
    actor: tuple[int, str],
) -> bool:
    """Bind an exact announcement copy to the target guild's current owner."""

    return (
        guild_crosspost_authority_event_ref(
            event_type,
            content,
            context,
            expected_authority=expected_authority,
        )
        == (expected_guild_id, expected_authority)
        and actor == expected_owner
    )


def _authority_event_id(value: object) -> int | None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        return None
    parsed = int(value)
    return parsed if 0 < parsed <= (1 << 63) - 1 else None


def _authority_event_domain(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(value)
    ):
        return None
    return value


def _authority_profile_ref(value: object) -> tuple[int, str] | None:
    if not isinstance(value, dict):
        return None
    identifier = _authority_event_id(value.get("id"))
    domain = _authority_event_domain(value.get("origin_domain"))
    if identifier is None or domain is None:
        return None
    return identifier, domain


def _authority_participant_ref(value: object) -> tuple[int, str] | None:
    if not isinstance(value, str) or value.count("@") != 1:
        return None
    raw_id, raw_domain = value.split("@", 1)
    identifier = _authority_event_id(raw_id)
    domain = _authority_event_domain(raw_domain)
    if identifier is None or domain is None:
        return None
    return identifier, domain


def _authority_group_context_ref(
    context: object,
    expected_authority: str,
) -> tuple[int, str, int] | None:
    if not isinstance(context, dict) or set(context) != {
        "conversation_id",
        "conversation_domain",
        "state_version",
    }:
        return None
    conversation_id = _authority_event_id(context.get("conversation_id"))
    state_version = _authority_event_id(context.get("state_version"))
    if (
        conversation_id is None
        or state_version is None
        or state_version < 1
        or context.get("conversation_domain") != expected_authority
    ):
        return None
    return conversation_id, expected_authority, state_version


def _authority_group_state_ref(
    content: object,
    context: object,
    *,
    expected_authority: str,
    actor: tuple[int, str],
) -> tuple[int, str, int] | None:
    if not isinstance(content, dict) or set(content) not in (
        {"conversation", "participants"},
        {"conversation", "participants", "notice"},
    ):
        return None
    if context != {}:
        return None
    conversation = content.get("conversation")
    participants = content.get("participants")
    if (
        not isinstance(conversation, dict)
        or set(conversation)
        != {
            "id",
            "origin_domain",
            "pair_key",
            "type",
            "authority_domain",
            "owner",
            "name",
            "state_version",
            "deleted",
            "encryption_policy",
        }
        or conversation.get("type") != "group"
        or conversation.get("origin_domain") != expected_authority
        or conversation.get("authority_domain") != expected_authority
        or conversation.get("deleted") is not False
        or not isinstance(participants, list)
        or not 1 <= len(participants) <= MAX_GROUP_DM_PARTICIPANTS
    ):
        return None
    conversation_id = _authority_event_id(conversation.get("id"))
    state_version = _authority_event_id(conversation.get("state_version"))
    owner = _authority_profile_ref(conversation.get("owner"))
    name = conversation.get("name")
    if (
        conversation_id is None
        or state_version is None
        or state_version < 1
        or owner is None
        or conversation.get("pair_key") != group_dm_key(expected_authority, conversation_id)
        or (
            name is not None
            and (not isinstance(name, str) or not 1 <= len(name) <= 100 or name != name.strip())
        )
    ):
        return None
    participant_refs = [_authority_profile_ref(item) for item in participants]
    if (
        any(item is None for item in participant_refs)
        or len(set(participant_refs)) != len(participant_refs)
        or owner not in participant_refs
    ):
        return None
    policy = conversation.get("encryption_policy")
    if not isinstance(policy, dict):
        return None
    try:
        validate_channel_encryption_policy(policy)
    except ValueError:
        return None
    notice = content.get("notice")
    if notice is not None:
        if not isinstance(notice, dict) or set(notice) != {"message", "author", "target"}:
            return None
        message = notice.get("message")
        author = _authority_profile_ref(notice.get("author"))
        target = _authority_profile_ref(notice.get("target"))
        if not isinstance(message, dict) or author != actor or target is None:
            return None
        message_type = message.get("message_type")
        if (
            _authority_event_id(message.get("id")) is None
            or message.get("origin_domain") != expected_authority
            or message.get("channel_id") != str(conversation_id)
            or message.get("channel_domain") != expected_authority
            or _authority_event_id(message.get("author_id")) != actor[0]
            or message.get("author_domain") != actor[1]
            or message_type
            not in {GROUP_DM_MEMBER_ADDED, GROUP_DM_MEMBER_LEFT, GROUP_DM_MEMBER_REMOVED}
            or (message_type == GROUP_DM_MEMBER_ADDED and target not in participant_refs)
            or (message_type == GROUP_DM_MEMBER_LEFT and target != actor)
            or (message_type == GROUP_DM_MEMBER_REMOVED and target == actor)
        ):
            return None
    elif actor not in participant_refs and state_version == 1:
        # The initial snapshot has no prior membership state with which a
        # replica could bind an absent semantic actor.
        return None
    return conversation_id, expected_authority, state_version


def _authority_group_message_ref(
    content: object,
    context: object,
    *,
    expected_authority: str,
    actor: tuple[int, str],
) -> tuple[int, str, int] | None:
    context_ref = _authority_group_context_ref(context, expected_authority)
    if context_ref is None or not isinstance(content, dict):
        return None
    if not {"message", "author"} <= set(content) or not set(content) <= {
        "message",
        "author",
        "forward_source_nsfw",
        "encryption_policy",
        "e2ee_control",
    }:
        return None
    message = content.get("message")
    if not isinstance(message, dict) or _authority_profile_ref(content.get("author")) != actor:
        return None
    conversation_id, _, _ = context_ref
    if (
        _authority_event_id(message.get("id")) is None
        or message.get("origin_domain") not in {expected_authority, actor[1]}
        or message.get("channel_id") != str(conversation_id)
        or message.get("channel_domain") != expected_authority
        or _authority_event_id(message.get("author_id")) != actor[0]
        or message.get("author_domain") != actor[1]
    ):
        return None
    return context_ref


def _authority_group_call_ref(
    content: object,
    context: object,
    *,
    expected_authority: str,
    actor: tuple[int, str],
) -> tuple[int, str, int] | None:
    context_ref = _authority_group_context_ref(context, expected_authority)
    if context_ref is None or not isinstance(content, dict) or set(content) != {"call"}:
        return None
    call = content.get("call")
    if not isinstance(call, dict) or set(call) != {
        "id",
        "channel_id",
        "channel_domain",
        "authority_domain",
        "room",
        "state",
        "created_at",
        "ended_at",
        "caller",
        "participants",
    }:
        return None
    conversation_id, _, _ = context_ref
    call_id = _authority_event_id(call.get("id"))
    participants = call.get("participants")
    participant_refs = (
        [_authority_participant_ref(item) for item in participants]
        if isinstance(participants, list)
        else []
    )
    created_at = call.get("created_at")
    if (
        call_id is None
        or call.get("channel_id") != str(conversation_id)
        or call.get("channel_domain") != expected_authority
        or call.get("authority_domain") != expected_authority
        or call.get("room") != f"d.{conversation_id}.{call_id}"
        or call.get("state") != "ringing"
        or call.get("ended_at") is not None
        or isinstance(created_at, bool)
        or not isinstance(created_at, int)
        or created_at < 0
        or not 2 <= len(participant_refs) <= MAX_GROUP_DM_PARTICIPANTS
        or any(item is None for item in participant_refs)
        or len(set(participant_refs)) != len(participant_refs)
        or _authority_participant_ref(call.get("caller")) != actor
        or actor not in participant_refs
    ):
        return None
    return context_ref


def authority_attested_group_event_ref(
    event_type: object,
    content: object,
    context: object,
    *,
    expected_authority: str,
    actor_id: object,
    actor_domain: object,
) -> tuple[int, str, int] | None:
    """Parse one exact group-DM authority event carrying a remote semantic actor.

    The authority may attest a remote participant only for these three closed
    projections. Replicas still recheck current/prior membership and state
    version before applying the event.
    """

    parsed_actor_id = _authority_event_id(actor_id)
    parsed_actor_domain = _authority_event_domain(actor_domain)
    expected_authority = _authority_event_domain(expected_authority) or ""
    if (
        parsed_actor_id is None
        or parsed_actor_domain is None
        or not expected_authority
        or parsed_actor_domain == expected_authority
    ):
        return None
    actor = (parsed_actor_id, parsed_actor_domain)
    if event_type == "dm.group.state":
        return _authority_group_state_ref(
            content,
            context,
            expected_authority=expected_authority,
            actor=actor,
        )
    if event_type == "dm.group.message.committed":
        return _authority_group_message_ref(
            content,
            context,
            expected_authority=expected_authority,
            actor=actor,
        )
    if event_type == "dm.group.call.create":
        return _authority_group_call_ref(
            content,
            context,
            expected_authority=expected_authority,
            actor=actor,
        )
    return None


def terminal_room_event_ref(envelope: dict[str, Any]) -> tuple[str, int, str] | None:
    """Return the room named by an exact authoritative terminal-room event.

    These two event shapes are durable deletion controls rather than ordinary
    membership updates.  Keep this predicate deliberately strict because it
    is also used to grant old-timestamp acceptance and non-expiring delivery.
    Origin-signature, destination, and retained-route checks still run in the
    inbox processor; terminal truth must not depend on a stale owner/member
    projection at a destination that already lost room visibility.
    """

    if set(envelope) != {
        "event_id",
        "origin",
        "type",
        "ts",
        "actor",
        "context",
        "content",
        "signatures",
    }:
        return None
    event_type = envelope.get("type")
    origin = envelope.get("origin")
    content = envelope.get("content")
    context = envelope.get("context")
    signatures = envelope.get("signatures")
    if not isinstance(origin, str) or not origin or not isinstance(content, dict):
        return None
    if (
        origin != origin.rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(origin)
        or not isinstance(signatures, dict)
        or set(signatures) != {origin}
        or not isinstance(signatures.get(origin), dict)
        or len(signatures[origin]) != 1
    ):
        return None

    if event_type == "guild.instance_access.revoked":
        actor = envelope.get("actor")
        if (
            not isinstance(actor, dict)
            or not isinstance(context, dict)
            or set(actor) != {"id", "domain"}
            or not authority_attested_terminal_guild_actor(
                event_type,
                content,
                context,
                expected_authority=origin,
                actor_id=actor.get("id"),
                actor_domain=actor.get("domain"),
            )
        ):
            return None
        raw_id = context.get("guild_id")
        room_kind = "guild"
    elif event_type == "dm.group.state":
        actor = envelope.get("actor")
        actor_id = actor.get("id") if isinstance(actor, dict) else None
        actor_domain = actor.get("domain") if isinstance(actor, dict) else None
        conversation = content.get("conversation")
        participants = content.get("participants")
        if (
            not isinstance(actor, dict)
            or set(actor) != {"id", "domain"}
            or not isinstance(actor_id, str)
            or not actor_id.isascii()
            or not actor_id.isdecimal()
            or (len(actor_id) > 1 and actor_id.startswith("0"))
            or not isinstance(actor_domain, str)
            or not actor_domain
            or actor_domain != actor_domain.rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(actor_domain)
            or int(actor_id) > (1 << 63) - 1
            or not isinstance(conversation, dict)
            or set(content) != {"conversation", "participants", "_terminal_generation"}
            or set(conversation)
            != {
                "id",
                "origin_domain",
                "pair_key",
                "type",
                "authority_domain",
                "owner",
                "name",
                "state_version",
                "deleted",
                "encryption_policy",
            }
            or context != {}
            or conversation.get("deleted") is not True
            or conversation.get("type") != "group"
            or conversation.get("origin_domain") != origin
            or conversation.get("authority_domain") != origin
            or participants != []
        ):
            return None
        raw_id = conversation.get("id")
        room_kind = "group_dm"
    else:
        return None

    if (
        not isinstance(raw_id, str)
        or not raw_id.isascii()
        or not raw_id.isdecimal()
        or (len(raw_id) > 1 and raw_id.startswith("0"))
    ):
        return None
    room_id = int(raw_id)
    if not 0 <= room_id <= (1 << 63) - 1:
        return None
    try:
        terminal_room_generation(envelope)
    except ValueError:
        return None
    if room_kind == "group_dm":
        group_conversation = cast(dict[str, Any], conversation)
        owner = group_conversation.get("owner")
        name = group_conversation.get("name")
        state_version = group_conversation.get("state_version")
        encryption_policy = group_conversation.get("encryption_policy")
        if (
            group_conversation.get("pair_key") != group_dm_key(origin, room_id)
            or not isinstance(owner, dict)
            or set(owner) != {"id", "origin_domain"}
            or not isinstance(owner.get("id"), str)
            or not owner["id"].isascii()
            or not owner["id"].isdecimal()
            or (len(owner["id"]) > 1 and owner["id"].startswith("0"))
            or int(owner["id"]) > (1 << 63) - 1
            or not isinstance(owner.get("origin_domain"), str)
            or owner["origin_domain"] != owner["origin_domain"].rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(owner["origin_domain"])
            or (
                name is not None
                and (not isinstance(name, str) or not 1 <= len(name) <= 100 or name != name.strip())
            )
            or not isinstance(state_version, str)
            or not state_version.isascii()
            or not state_version.isdecimal()
            or (len(state_version) > 1 and state_version.startswith("0"))
            or not 1 <= int(state_version) <= (1 << 63) - 1
            or not isinstance(encryption_policy, dict)
            or set(encryption_policy)
            != {"mode", "state", "generation", "protocol", "suite", "group_id", "epoch"}
            or not isinstance(encryption_policy.get("generation"), str)
            or (
                encryption_policy.get("epoch") is not None
                and not isinstance(encryption_policy.get("epoch"), str)
            )
        ):
            return None
        try:
            validate_channel_encryption_policy(encryption_policy)
        except ValueError:
            return None
    return room_kind, room_id, origin


def durable_terminal_room_event(envelope: dict[str, Any]) -> bool:
    """Whether an envelope is an exact terminal-room deletion control."""

    return terminal_room_event_ref(envelope) is not None


def guild_media_delete_request_ref(
    envelope: dict[str, Any],
) -> tuple[int, str, int, str, int, str, int] | None:
    """Parse one exact authority-signed request to delete origin-owned media.

    A guild home can delete a message after the attachment's origin instance
    has left the guild.  The ordinary sequenced mutation is intentionally not
    disclosed to that former member, so this minimal removal-only control is
    addressed directly to the attachment home.  Its exact shape is used to
    grant durable delivery and old-timestamp acceptance; semantic authority is
    still bound to the recipient's retained guild/message/media route.
    """

    if (
        set(envelope)
        != {
            "event_id",
            "origin",
            "type",
            "ts",
            "actor",
            "context",
            "content",
            "signatures",
        }
        or envelope.get("type") != "guild.media.delete.request"
    ):
        return None
    origin = envelope.get("origin")
    actor = envelope.get("actor")
    context = envelope.get("context")
    content = envelope.get("content")
    signatures = envelope.get("signatures")
    if (
        not isinstance(origin, str)
        or origin != origin.rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(origin)
        or not isinstance(actor, dict)
        or set(actor) != {"id", "domain"}
        or not isinstance(actor.get("id"), str)
        or not actor["id"].isascii()
        or not actor["id"].isdecimal()
        or (len(actor["id"]) > 1 and actor["id"].startswith("0"))
        or int(actor["id"]) > (1 << 63) - 1
        or not isinstance(actor.get("domain"), str)
        or not actor["domain"]
        or actor["domain"] != actor["domain"].rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(actor["domain"])
        or context != {}
        or not isinstance(content, dict)
        or set(content) != {"guild", "message", "attachment", "deleted_at", "_deletion_generation"}
        or not isinstance(signatures, dict)
        or set(signatures) != {origin}
        or not isinstance(signatures.get(origin), dict)
        or len(signatures[origin]) != 1
    ):
        return None

    def exact_ref(raw: object, *, expected_domain: str | None = None) -> tuple[int, str] | None:
        if not isinstance(raw, dict) or set(raw) != {"id", "origin_domain"}:
            return None
        raw_id = raw.get("id")
        domain = raw.get("origin_domain")
        if (
            not isinstance(raw_id, str)
            or not raw_id.isascii()
            or not raw_id.isdecimal()
            or (len(raw_id) > 1 and raw_id.startswith("0"))
            or int(raw_id) > (1 << 63) - 1
            or not isinstance(domain, str)
            or domain != domain.rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(domain)
            or (expected_domain is not None and domain != expected_domain)
        ):
            return None
        return int(raw_id), domain

    guild_ref = exact_ref(content.get("guild"), expected_domain=origin)
    message_ref = exact_ref(content.get("message"), expected_domain=origin)
    attachment_ref = exact_ref(content.get("attachment"))
    raw_generation = content.get("_deletion_generation")
    raw_deleted_at = content.get("deleted_at")
    if (
        guild_ref is None
        or message_ref is None
        or attachment_ref is None
        or attachment_ref[1] == origin
        or not isinstance(raw_generation, str)
        or not raw_generation.isascii()
        or not raw_generation.isdecimal()
        or (len(raw_generation) > 1 and raw_generation.startswith("0"))
        or not 1 <= int(raw_generation) <= (1 << 63) - 1
        or not isinstance(raw_deleted_at, str)
    ):
        return None
    try:
        deleted_at = datetime.fromisoformat(raw_deleted_at)
    except ValueError:
        return None
    if deleted_at.tzinfo is None or deleted_at.isoformat() != raw_deleted_at:
        return None
    return (
        guild_ref[0],
        guild_ref[1],
        message_ref[0],
        message_ref[1],
        attachment_ref[0],
        attachment_ref[1],
        int(raw_generation),
    )


def durable_guild_media_delete_request(envelope: dict[str, Any]) -> bool:
    return guild_media_delete_request_ref(envelope) is not None


def authority_attested_media_delete_ref(
    envelope: dict[str, Any],
    *,
    expected_authority: str,
) -> tuple[int, str, int] | None:
    """Parse the exact removal-only tombstone shape signed by an asset home.

    The envelope actor is attribution only for this event. The attachment
    origin is the authority that can invalidate its own bytes, so a retained
    uploader reference may be remote after guild ownership or installation
    state changes. Keeping this predicate exact prevents that exception from
    admitting a remote actor for any broader event contract.
    """

    if (
        set(envelope)
        != {
            "event_id",
            "origin",
            "type",
            "ts",
            "actor",
            "context",
            "content",
            "signatures",
        }
        or envelope.get("type") != "media.delete"
        or envelope.get("origin") != expected_authority
        or envelope.get("context") != {}
    ):
        return None
    actor = envelope.get("actor")
    content = envelope.get("content")
    signatures = envelope.get("signatures")
    if (
        not isinstance(actor, dict)
        or set(actor) != {"id", "domain"}
        or not isinstance(actor.get("id"), str)
        or not actor["id"].isascii()
        or not actor["id"].isdecimal()
        or (len(actor["id"]) > 1 and actor["id"].startswith("0"))
        or int(actor["id"]) > (1 << 63) - 1
        or not isinstance(actor.get("domain"), str)
        or actor["domain"] != actor["domain"].rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(actor["domain"])
        or not isinstance(content, dict)
        or set(content) != {"attachment_id", "origin_domain", "generation"}
        or content.get("origin_domain") != expected_authority
        or not isinstance(signatures, dict)
        or set(signatures) != {expected_authority}
        or not isinstance(signatures.get(expected_authority), dict)
        or len(signatures[expected_authority]) != 1
    ):
        return None
    raw_attachment_id = content.get("attachment_id")
    raw_generation = content.get("generation")
    if (
        not isinstance(raw_attachment_id, str)
        or not raw_attachment_id.isascii()
        or not raw_attachment_id.isdecimal()
        or (len(raw_attachment_id) > 1 and raw_attachment_id.startswith("0"))
        or int(raw_attachment_id) > (1 << 63) - 1
        or not isinstance(raw_generation, str)
        or not raw_generation.isascii()
        or not raw_generation.isdecimal()
        or (len(raw_generation) > 1 and raw_generation.startswith("0"))
        or not 1 <= int(raw_generation) <= (1 << 63) - 1
    ):
        return None
    return int(raw_attachment_id), expected_authority, int(raw_generation)


def policy_held_retry_at(now: datetime | None = None) -> datetime:
    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return current + POLICY_HELD_OUTBOX_DELAY


def canonical_query(query: str) -> str:
    return urlencode(sorted(parse_qsl(query, keep_blank_values=True)))


def canonical_request_target(path: str, query: str = "") -> str:
    normalized = canonical_query(query)
    return f"{path}?{normalized}" if normalized else path


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_json(value: dict[str, Any], *, allow_floats: bool = False) -> bytes:
    validate_json_tree(
        value,
        limits=FEDERATION_JSON_LIMITS,
        label="federation JSON",
        allow_floats=allow_floats,
    )
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SigningInput:
    method: str
    request_target: str
    origin: str
    destination: str
    timestamp: int
    content_hash: str
    nonce: str | None = None

    def canonical_bytes(self) -> bytes:
        value: dict[str, Any] = {
            "content_sha256": self.content_hash,
            "destination": self.destination,
            "method": self.method.upper(),
            "origin": self.origin,
            "request_target": self.request_target,
            "ts": self.timestamp,
        }
        if self.nonce is not None:
            value["nonce"] = self.nonce
        return canonical_json(value)


def sign_request(signing_input: SigningInput, private_key: Ed25519PrivateKey) -> bytes:
    return private_key.sign(signing_input.canonical_bytes())


def verify_request(
    signing_input: SigningInput, signature: bytes, public_key: Ed25519PublicKey
) -> bool:
    try:
        public_key.verify(signature, signing_input.canonical_bytes())
    except InvalidSignature:
        return False
    return True


def envelope_signing_bytes(envelope: dict[str, Any]) -> bytes:
    return canonical_json({key: value for key, value in envelope.items() if key != "signatures"})


def sign_envelope(envelope: dict[str, Any], private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.sign(envelope_signing_bytes(envelope))).decode("ascii")


def verify_envelope(
    envelope: dict[str, Any], signature: bytes, public_key: Ed25519PublicKey
) -> bool:
    try:
        public_key.verify(signature, envelope_signing_bytes(envelope))
    except InvalidSignature:
        return False
    return True
