from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.automod.service import rule_payload
from app.bots.installations import channel_restrictions_allow
from app.chat.audit_payloads import AuditLogChangePayload, AuditLogEntryPayload
from app.chat.permissions import BotGuildPermissionGrant
from app.core.types import EntityRef
from app.db.models import (
    AutoModRule,
    Channel,
    Guild,
    GuildScheduledEvent,
    Invite,
    StageInstance,
    Webhook,
)

ChannelRef = tuple[int, str]

_DIRECT_CHANNEL_TARGET_TYPES = frozenset({"channel", "thread", "voice_channel"})
_CHANNEL_SCOPED_TARGET_TYPES = frozenset(
    {
        *_DIRECT_CHANNEL_TARGET_TYPES,
        "auto_mod_rule",
        "channel_order",
        "invite",
        "message",
        "scheduled_event",
        "stage_instance",
        "webhook",
    }
)
_CHANNEL_VALUE_KEYS = frozenset(
    {
        "channel_id",
        "channel_ref",
        "parent_id",
        "target_channel_id",
        "target_channel_ref",
    }
)
_CHANNEL_LIST_KEYS = frozenset(
    {
        "channel_ids",
        "channel_refs",
        "exempt_channels",
    }
)


def _channel_ref(value: object, default_domain: str) -> ChannelRef:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("invalid audit channel reference")
    return EntityRef(str(value)).resolve(default_domain)


def _nested_channel_refs(
    value: object,
    default_domain: str,
) -> tuple[set[ChannelRef], bool]:
    """Extract only fields whose schema identifies them as channel references."""

    refs: set[ChannelRef] = set()
    valid = True

    def visit(item: object) -> None:
        nonlocal valid
        if isinstance(item, dict):
            for key, nested in item.items():
                if key in _CHANNEL_VALUE_KEYS:
                    if nested is None:
                        continue
                    try:
                        refs.add(_channel_ref(nested, default_domain))
                    except ValueError:
                        valid = False
                    continue
                if key in _CHANNEL_LIST_KEYS:
                    if not isinstance(nested, list):
                        valid = False
                        continue
                    for raw_ref in nested:
                        try:
                            refs.add(_channel_ref(raw_ref, default_domain))
                        except ValueError:
                            valid = False
                    continue
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return refs, valid


def _change_channel_refs(
    change: AuditLogChangePayload,
    default_domain: str,
) -> tuple[set[ChannelRef], bool]:
    rendered = change.model_dump(mode="json", exclude_unset=True)
    refs, valid = _nested_channel_refs(rendered, default_domain)
    if change.key not in _CHANNEL_VALUE_KEYS:
        return refs, valid
    for field in ("old_value", "new_value", "added", "removed"):
        raw_value = rendered.get(field)
        if raw_value is None:
            continue
        values: Iterable[object] = raw_value if isinstance(raw_value, list) else (raw_value,)
        for value in values:
            try:
                refs.add(_channel_ref(value, default_domain))
            except ValueError:
                valid = False
    return refs, valid


def _target_id(entry: AuditLogEntryPayload, default_domain: str) -> ChannelRef:
    target = entry.target_ref
    if not isinstance(target, dict):
        raise ValueError("missing audit target reference")
    raw_id = target.get("id")
    raw_domain = target.get("origin_domain", target.get("domain", default_domain))
    if not isinstance(raw_domain, str):
        raise ValueError("invalid audit target domain")
    return _channel_ref(f"{raw_id}@{raw_domain}", default_domain)


async def _resource_channel_refs(
    session: AsyncSession,
    guild: Guild,
    entry: AuditLogEntryPayload,
) -> tuple[set[ChannelRef], bool]:
    """Resolve the channel lineage of one channel-scoped audit target."""

    target_type = entry.target_type
    if target_type in _DIRECT_CHANNEL_TARGET_TYPES:
        try:
            return {_target_id(entry, guild.origin_domain)}, True
        except ValueError:
            return set(), False
    if target_type == "message":
        message_refs, valid = _nested_channel_refs(entry.target_ref, guild.origin_domain)
        return message_refs, valid and bool(message_refs)
    if target_type == "channel_order":
        return set(), True
    if target_type not in _CHANNEL_SCOPED_TARGET_TYPES:
        return set(), True

    target = entry.target_ref
    if not isinstance(target, dict):
        return set(), False
    try:
        if target_type == "invite":
            code = target.get("code")
            if not isinstance(code, str):
                return set(), False
            invite = await session.get(Invite, code)
            if invite is None or (invite.guild_id, invite.guild_domain) != (
                guild.id,
                guild.origin_domain,
            ):
                return set(), False
            refs: set[ChannelRef] = set()
            if invite.channel_id is not None and invite.channel_domain is not None:
                refs.add((invite.channel_id, invite.channel_domain))
            if invite.scheduled_event_id is not None and invite.scheduled_event_domain is not None:
                event = await session.get(
                    GuildScheduledEvent,
                    (invite.scheduled_event_id, invite.scheduled_event_domain),
                )
                if event is None:
                    return set(), False
                if event.channel_id is not None and event.channel_domain is not None:
                    refs.add((event.channel_id, event.channel_domain))
            return refs, True

        resource_id, resource_domain = _target_id(entry, guild.origin_domain)
        if target_type == "scheduled_event":
            event = await session.get(GuildScheduledEvent, (resource_id, resource_domain))
            if event is None or (event.guild_id, event.guild_domain) != (
                guild.id,
                guild.origin_domain,
            ):
                return set(), False
            refs = set()
            if event.channel_id is not None and event.channel_domain is not None:
                refs.add((event.channel_id, event.channel_domain))
            return refs, True
        if target_type == "stage_instance":
            instance = await session.get(StageInstance, (resource_id, resource_domain))
            if instance is None or (instance.guild_id, instance.guild_domain) != (
                guild.id,
                guild.origin_domain,
            ):
                return set(), False
            return {(instance.channel_id, instance.channel_domain)}, True
        if target_type == "webhook":
            webhook = await session.get(Webhook, resource_id)
            if webhook is None or (webhook.guild_id, webhook.guild_domain) != (
                guild.id,
                guild.origin_domain,
            ):
                # Announcement follower audit rows share the webhook shape but
                # are deleted with their target. Historical rows without a
                # provable channel are intentionally hidden from restricted bots.
                return set(), False
            return {(webhook.channel_id, webhook.channel_domain)}, True
        if target_type == "auto_mod_rule":
            rule = await session.get(AutoModRule, (resource_id, resource_domain))
            if rule is None or (rule.guild_id, rule.guild_domain) != (
                guild.id,
                guild.origin_domain,
            ):
                return set(), False
            rendered = await rule_payload(session, rule)
            refs, valid = _nested_channel_refs(rendered, guild.origin_domain)
            return refs, valid
    except (TypeError, ValueError):
        return set(), False
    return set(), False


async def filter_restricted_bot_audit_entries(
    session: AsyncSession,
    guild: Guild,
    grant: BotGuildPermissionGrant,
    entries: list[AuditLogEntryPayload],
) -> list[AuditLogEntryPayload]:
    """Remove channel metadata outside one bot installation's exact grant."""

    if grant.installation_id is None:
        return []
    if not grant.channel_restrictions:
        return entries

    visibility: dict[ChannelRef, bool] = {}

    async def visible(channel_ref: ChannelRef) -> bool:
        cached = visibility.get(channel_ref)
        if cached is not None:
            return cached
        channel = await session.get(Channel, channel_ref)
        allowed = bool(
            channel is not None
            and not channel.unavailable
            and (channel.guild_id, channel.guild_domain) == (guild.id, guild.origin_domain)
            and await channel_restrictions_allow(
                session,
                grant.channel_restrictions,
                channel,
            )
        )
        visibility[channel_ref] = allowed
        return allowed

    async def all_visible(refs: set[ChannelRef]) -> bool:
        for channel_ref in sorted(refs, key=lambda item: (item[1], item[0])):
            if not await visible(channel_ref):
                return False
        return True

    rendered: list[AuditLogEntryPayload] = []
    for entry in entries:
        if entry.target_type == "channel_order":
            visible_changes: list[AuditLogChangePayload] = []
            for change in entry.changes:
                try:
                    refs = {_channel_ref(change.key, guild.origin_domain)}
                    nested_refs, valid = _change_channel_refs(change, guild.origin_domain)
                    refs.update(nested_refs)
                except ValueError:
                    valid = False
                    refs = set()
                if valid and await all_visible(refs):
                    visible_changes.append(change)
            if visible_changes:
                rendered.append(entry.model_copy(update={"changes": visible_changes}))
            continue

        refs, resolved = await _resource_channel_refs(session, guild, entry)
        nested_refs, nested_valid = _nested_channel_refs(
            [change.model_dump(mode="json", exclude_unset=True) for change in entry.changes],
            guild.origin_domain,
        )
        for change in entry.changes:
            change_refs, change_valid = _change_channel_refs(change, guild.origin_domain)
            nested_refs.update(change_refs)
            nested_valid = nested_valid and change_valid
        refs.update(nested_refs)
        if not resolved or not nested_valid:
            continue
        if await all_visible(refs):
            rendered.append(entry)
    return rendered
