from __future__ import annotations

import hashlib
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import delete, exists, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.automod.engine import MatchResult, evaluate_trigger
from app.automod.schemas import AutoModActionInput, AutoModRuleCreate, AutoModRuleUpdate
from app.chat.audit import add_audit_entry
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.mentions import syntactic_mention_count
from app.chat.moderation_status import member_timeout_error_detail
from app.chat.payloads import message_payload
from app.chat.postcommit import publish_committed_dispatches, queue_postcommit_dispatch
from app.core.channel_types import is_message_capable_channel_type
from app.core.permissions import BLOCKED_MEMBER_INTERACTION_PERMISSIONS, Permission
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.materialization import materialize_updated_at
from app.db.models import (
    AutoModAction,
    AutoModExecution,
    AutoModMemberBlock,
    AutoModRule,
    AutoModRuleExemptChannel,
    AutoModRuleExemptRole,
    Channel,
    Guild,
    GuildMember,
    MemberRole,
    Message,
    MessageProjection,
    Role,
    User,
)
from app.federation.replication import profile_from_user

AUTOMOD_AUDIT_CREATE = 140
AUTOMOD_AUDIT_UPDATE = 141
AUTOMOD_AUDIT_DELETE = 142
AUTOMOD_AUDIT_EXECUTE = 143
RULE_LIMITS = {
    "keyword": 6,
    "spam": 1,
    "keyword_preset": 1,
    "mention_spam": 1,
    "member_profile": 1,
}


@dataclass(slots=True)
class AutoModPostCommit:
    """Best-effort projections for AutoMod state already queued in SQL."""

    guilds: list[Guild] = dataclass_field(default_factory=list)
    dispatches: list[tuple[str, str, dict[str, Any]]] = dataclass_field(default_factory=list)

    def add_guild(self, guild: Guild) -> None:
        if all(
            (item.id, item.origin_domain) != (guild.id, guild.origin_domain) for item in self.guilds
        ):
            self.guilds.append(guild)

    def add_dispatch(self, guild: Guild, event_type: str, data: dict[str, Any]) -> None:
        self.dispatches.append((guild_topic(guild.origin_domain, guild.id), event_type, data))

    def extend(self, other: AutoModPostCommit) -> None:
        for guild in other.guilds:
            self.add_guild(guild)
        self.dispatches.extend(other.dispatches)

    async def publish(self, redis: Redis) -> None:
        # SQL/outbox state is already committed by the caller. Redis and task
        # wakes are recoverable projections and must never undo that mutation.
        for guild in self.guilds:
            await wake_queued_guild_federation(guild)
        for topic, event_type, data in self.dispatches:
            await publish_dispatch(redis, topic, event_type, data)


class AutoModMessageBlocked(HTTPException):
    """A blocked message whose durable AutoMod side effects still need committing."""

    def __init__(self, message: str, post_commit: AutoModPostCommit) -> None:
        super().__init__(
            status_code=403,
            detail={"code": "MESSAGE_BLOCKED_BY_AUTO_MOD", "message": message},
        )
        self.post_commit = post_commit


def _ref(id: int, domain: str) -> str:
    return f"{id}@{domain}"


def _active_rules_statement(
    guild: Guild,
    event_type: str,
    *,
    trigger_type: str | None = None,
    limit: int | None = None,
) -> Any:
    """Lock one coherent rule snapshot for an admitted AutoMod evaluation."""

    statement = select(AutoModRule).where(
        AutoModRule.guild_id == guild.id,
        AutoModRule.guild_domain == guild.origin_domain,
        AutoModRule.enabled.is_(True),
        AutoModRule.event_type == event_type,
    )
    if trigger_type is not None:
        statement = statement.where(AutoModRule.trigger_type == trigger_type)
    statement = statement.order_by(AutoModRule.id)
    if limit is not None:
        statement = statement.limit(limit)
    # Rule updates and deletes first lock the rule row. A shared lock keeps
    # trigger metadata, exemptions, and actions coherent for this evaluation.
    return statement.with_for_update(read=True)


async def get_rule(
    session: AsyncSession, guild: Guild, rule_id: int, *, for_update: bool = False
) -> AutoModRule:
    statement = select(AutoModRule).where(
        AutoModRule.id == rule_id,
        AutoModRule.origin_domain == guild.origin_domain,
        AutoModRule.guild_id == guild.id,
        AutoModRule.guild_domain == guild.origin_domain,
    )
    if for_update:
        statement = statement.with_for_update()
    rule = await session.scalar(statement)
    if rule is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "AUTO_MOD_RULE_NOT_FOUND", "message": "AutoMod rule not found."},
        )
    return rule


async def rule_payload(session: AsyncSession, rule: AutoModRule) -> dict[str, object]:
    actions = list(
        await session.scalars(
            select(AutoModAction)
            .where(
                AutoModAction.rule_id == rule.id,
                AutoModAction.rule_domain == rule.origin_domain,
            )
            .order_by(AutoModAction.position)
        )
    )
    exempt_roles = list(
        await session.scalars(
            select(AutoModRuleExemptRole).where(
                AutoModRuleExemptRole.rule_id == rule.id,
                AutoModRuleExemptRole.rule_domain == rule.origin_domain,
            )
        )
    )
    exempt_channels = list(
        await session.scalars(
            select(AutoModRuleExemptChannel).where(
                AutoModRuleExemptChannel.rule_id == rule.id,
                AutoModRuleExemptChannel.rule_domain == rule.origin_domain,
            )
        )
    )
    return {
        "id": str(rule.id),
        "origin_domain": rule.origin_domain,
        "guild_id": str(rule.guild_id),
        "guild_domain": rule.guild_domain,
        "name": rule.name,
        "creator_id": str(rule.creator_id),
        "creator_domain": rule.creator_domain,
        "event_type": rule.event_type,
        "trigger_type": rule.trigger_type,
        "trigger_metadata": rule.trigger_metadata,
        "actions": [
            {"type": action.action_type, "metadata": action.action_metadata} for action in actions
        ],
        "enabled": rule.enabled,
        "exempt_roles": [_ref(item.role_id, item.role_domain) for item in exempt_roles],
        "exempt_channels": [_ref(item.channel_id, item.channel_domain) for item in exempt_channels],
        "version": rule.version,
        "created_at": rule.created_at.isoformat(),
        "updated_at": rule.updated_at.isoformat(),
    }


async def _validate_refs(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    role_refs: list[EntityRef],
    channel_refs: list[EntityRef],
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    roles = [item.resolve(settings.domain) for item in role_refs]
    channels = [item.resolve(settings.domain) for item in channel_refs]
    # Bare IDs are a local-authority shorthand. Reject aliases such as
    # ``123`` plus ``123@authority`` after resolution so a federated request
    # cannot reach the database with duplicate composite keys.
    if len(roles) != len(set(roles)):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "AUTO_MOD_EXEMPT_ROLE_DUPLICATE",
                "message": "Choose each exempt role only once.",
            },
        )
    if len(channels) != len(set(channels)):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "AUTO_MOD_EXEMPT_CHANNEL_DUPLICATE",
                "message": "Choose each exempt channel only once.",
            },
        )
    if roles:
        found_roles = set(
            (
                await session.execute(
                    select(Role.id, Role.origin_domain).where(
                        Role.guild_id == guild.id,
                        Role.guild_domain == guild.origin_domain,
                        Role.origin_domain == guild.origin_domain,
                        Role.id.in_([item[0] for item in roles]),
                    )
                )
            ).tuples()
        )
        if found_roles != set(roles):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "AUTO_MOD_EXEMPT_ROLE_INVALID",
                    "message": "Every exempt role must belong to this guild.",
                },
            )
    if channels:
        found_channels = set(
            (
                await session.execute(
                    select(Channel.id, Channel.origin_domain).where(
                        Channel.guild_id == guild.id,
                        Channel.guild_domain == guild.origin_domain,
                        Channel.origin_domain == guild.origin_domain,
                        Channel.id.in_([item[0] for item in channels]),
                    )
                )
            ).tuples()
        )
        if found_channels != set(channels):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "AUTO_MOD_EXEMPT_CHANNEL_INVALID",
                    "message": "Every exempt channel must belong to this guild.",
                },
            )
    return roles, channels


async def _replace_children(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    rule: AutoModRule,
    *,
    actions: list[AutoModActionInput] | None,
    exempt_roles: list[EntityRef] | None,
    exempt_channels: list[EntityRef] | None,
) -> None:
    roles, channels = await _validate_refs(
        session,
        settings,
        guild,
        exempt_roles or [],
        exempt_channels or [],
    )
    if actions is not None:
        for action in actions:
            if action.type != "send_alert_message" or action.channel_id is None:
                continue
            channel_id, channel_domain = action.channel_id.resolve(settings.domain)
            alert_channel = await session.get(Channel, (channel_id, channel_domain))
            if alert_channel is None or (
                alert_channel.guild_id,
                alert_channel.guild_domain,
                alert_channel.type,
            ) not in {
                (guild.id, guild.origin_domain, 0),
                (guild.id, guild.origin_domain, 5),
            }:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "AUTO_MOD_ALERT_CHANNEL_INVALID",
                        "message": (
                            "AutoMod alerts must target a text or announcement channel "
                            "in this guild."
                        ),
                    },
                )
            if alert_channel.encryption_mode == "e2ee" or alert_channel.e2ee_required:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "AUTO_MOD_ALERT_CHANNEL_ENCRYPTED",
                        "message": (
                            "AutoMod alert messages are server-authored and cannot be sent to "
                            "an end-to-end encrypted channel. Choose a plaintext text or "
                            "announcement channel."
                        ),
                    },
                )
        await session.execute(
            delete(AutoModAction).where(
                AutoModAction.rule_id == rule.id,
                AutoModAction.rule_domain == rule.origin_domain,
            )
        )
        for position, action in enumerate(actions):
            session.add(
                AutoModAction(
                    rule_id=rule.id,
                    rule_domain=rule.origin_domain,
                    position=position,
                    action_type=action.type,
                    action_metadata=action.metadata(),
                )
            )
    if exempt_roles is not None:
        await session.execute(
            delete(AutoModRuleExemptRole).where(
                AutoModRuleExemptRole.rule_id == rule.id,
                AutoModRuleExemptRole.rule_domain == rule.origin_domain,
            )
        )
        for role_id, role_domain in roles:
            session.add(
                AutoModRuleExemptRole(
                    rule_id=rule.id,
                    rule_domain=rule.origin_domain,
                    role_id=role_id,
                    role_domain=role_domain,
                    guild_id=guild.id,
                    guild_domain=guild.origin_domain,
                )
            )
    if exempt_channels is not None:
        await session.execute(
            delete(AutoModRuleExemptChannel).where(
                AutoModRuleExemptChannel.rule_id == rule.id,
                AutoModRuleExemptChannel.rule_domain == rule.origin_domain,
            )
        )
        for channel_id, channel_domain in channels:
            session.add(
                AutoModRuleExemptChannel(
                    rule_id=rule.id,
                    rule_domain=rule.origin_domain,
                    channel_id=channel_id,
                    channel_domain=channel_domain,
                    guild_id=guild.id,
                    guild_domain=guild.origin_domain,
                )
            )


async def _require_rule_capacity(
    session: AsyncSession,
    guild: Guild,
    trigger_type: str,
    *,
    exclude_rule_id: int | None = None,
) -> None:
    limit = RULE_LIMITS[trigger_type]
    statement = select(AutoModRule.id).where(
        AutoModRule.guild_id == guild.id,
        AutoModRule.guild_domain == guild.origin_domain,
        AutoModRule.trigger_type == trigger_type,
    )
    if exclude_rule_id is not None:
        statement = statement.where(AutoModRule.id != exclude_rule_id)
    existing = len(list(await session.scalars(statement.limit(limit))))
    if existing >= limit:
        label = trigger_type.replace("_", " ")
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AUTO_MOD_TRIGGER_RULE_LIMIT_REACHED",
                "message": f"This guild already has the maximum number of {label} rules.",
                "trigger_type": trigger_type,
                "limit": limit,
            },
        )


async def create_rule(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    actor: User,
    payload: AutoModRuleCreate,
    *,
    redis: Redis,
    reason: str | None,
) -> AutoModRule:
    # Serialize the capacity check for this authoritative guild. Without a
    # shared row lock, concurrent human, bot, or federated creates can both
    # observe the same count and exceed Discord's per-trigger rule limits.
    await session.execute(
        select(Guild.id)
        .where(
            Guild.id == guild.id,
            Guild.origin_domain == guild.origin_domain,
        )
        .with_for_update()
    )
    await _require_rule_capacity(session, guild, payload.trigger_type)
    rule = AutoModRule(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name=payload.name,
        creator_id=actor.id,
        creator_domain=actor.origin_domain,
        event_type=payload.event_type,
        trigger_type=payload.trigger_type,
        trigger_metadata=payload.trigger_metadata.model_dump(mode="json"),
        enabled=payload.enabled,
    )
    session.add(rule)
    await session.flush()
    await _replace_children(
        session,
        settings,
        guild,
        rule,
        actions=payload.actions,
        exempt_roles=list(payload.exempt_roles),
        exempt_channels=list(payload.exempt_channels),
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        AUTOMOD_AUDIT_CREATE,
        target_type="auto_mod_rule",
        target_ref={"id": str(rule.id), "origin_domain": rule.origin_domain},
        reason=reason,
        changes=[{"key": "name", "new_value": rule.name}],
    )
    rendered = await rule_payload(session, rule)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.automod.rule.create",
        {"rule": rendered},
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "AUTO_MODERATION_RULE_CREATE",
        rendered,
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return rule


async def update_rule(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    actor: User,
    rule: AutoModRule,
    payload: AutoModRuleUpdate,
    *,
    redis: Redis,
    reason: str | None,
) -> AutoModRule:
    effective_actions = payload.actions
    if effective_actions is None:
        stored_actions = list(
            await session.scalars(
                select(AutoModAction)
                .where(
                    AutoModAction.rule_id == rule.id,
                    AutoModAction.rule_domain == rule.origin_domain,
                )
                .order_by(AutoModAction.position)
            )
        )
        effective_actions = [
            AutoModActionInput.model_validate(
                {"type": item.action_type, **(item.action_metadata or {})}
            )
            for item in stored_actions
        ]
    effective_trigger = rule.trigger_type
    effective_event = payload.event_type or rule.event_type
    effective_metadata = payload.trigger_metadata or rule.trigger_metadata
    AutoModRuleCreate.model_validate(
        {
            "name": payload.name or rule.name,
            "event_type": effective_event,
            "trigger_type": effective_trigger,
            "trigger_metadata": effective_metadata,
            "actions": [item.model_dump(mode="json") for item in effective_actions],
        }
    )
    changes: list[dict[str, object]] = []
    for field in ("name", "event_type", "enabled"):
        value = getattr(payload, field)
        if field in payload.model_fields_set and value != getattr(rule, field):
            changes.append({"key": field, "old_value": getattr(rule, field), "new_value": value})
            setattr(rule, field, value)
    if payload.trigger_metadata is not None:
        metadata = payload.trigger_metadata.model_dump(mode="json")
        changes.append(
            {"key": "trigger_metadata", "old_value": rule.trigger_metadata, "new_value": metadata}
        )
        rule.trigger_metadata = metadata
    await _replace_children(
        session,
        settings,
        guild,
        rule,
        actions=payload.actions,
        exempt_roles=(list(payload.exempt_roles) if payload.exempt_roles is not None else None),
        exempt_channels=(
            list(payload.exempt_channels) if payload.exempt_channels is not None else None
        ),
    )
    rule.version += 1
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        AUTOMOD_AUDIT_UPDATE,
        target_type="auto_mod_rule",
        target_ref={"id": str(rule.id), "origin_domain": rule.origin_domain},
        reason=reason,
        changes=changes or [{"key": "configuration", "new_value": "updated"}],
    )
    await materialize_updated_at(session, rule)
    rendered = await rule_payload(session, rule)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.automod.rule.update",
        {"rule": rendered},
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "AUTO_MODERATION_RULE_UPDATE",
        rendered,
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return rule


async def delete_rule(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    actor: User,
    rule: AutoModRule,
    *,
    redis: Redis,
    reason: str | None,
) -> None:
    rendered = await rule_payload(session, rule)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        AUTOMOD_AUDIT_DELETE,
        target_type="auto_mod_rule",
        target_ref={"id": str(rule.id), "origin_domain": rule.origin_domain},
        reason=reason,
        changes=[{"key": "name", "old_value": rule.name}],
    )
    await session.delete(rule)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.automod.rule.delete",
        {"rule": rendered},
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "AUTO_MODERATION_RULE_DELETE",
        rendered,
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)


async def _is_exempt(
    session: AsyncSession,
    guild: Guild,
    channel: Channel,
    actor: User,
    rule: AutoModRule,
    actor_permissions: int,
) -> bool:
    if (actor.id, actor.origin_domain) == (guild.owner_id, guild.owner_domain):
        return True
    if actor_permissions & (Permission.ADMINISTRATOR | Permission.MANAGE_GUILD):
        return True
    channel_exempt = await session.scalar(
        select(AutoModRuleExemptChannel.rule_id).where(
            AutoModRuleExemptChannel.rule_id == rule.id,
            AutoModRuleExemptChannel.rule_domain == rule.origin_domain,
            AutoModRuleExemptChannel.channel_id == channel.id,
            AutoModRuleExemptChannel.channel_domain == channel.origin_domain,
        )
    )
    if channel_exempt is not None:
        return True
    role_exempt = await session.scalar(
        select(AutoModRuleExemptRole.rule_id)
        .join(
            MemberRole,
            (MemberRole.role_id == AutoModRuleExemptRole.role_id)
            & (MemberRole.role_domain == AutoModRuleExemptRole.role_domain)
            & (MemberRole.guild_id == guild.id)
            & (MemberRole.guild_domain == guild.origin_domain)
            & (MemberRole.user_id == actor.id)
            & (MemberRole.user_domain == actor.origin_domain),
        )
        .where(
            AutoModRuleExemptRole.rule_id == rule.id,
            AutoModRuleExemptRole.rule_domain == rule.origin_domain,
        )
    )
    return role_exempt is not None


async def _is_member_profile_exempt(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    rule: AutoModRule,
) -> bool:
    if (actor.id, actor.origin_domain) == (guild.owner_id, guild.owner_domain):
        return True
    role_rows = list(
        (
            await session.execute(
                select(Role.id, Role.origin_domain, Role.permissions)
                .outerjoin(
                    MemberRole,
                    (MemberRole.role_id == Role.id)
                    & (MemberRole.role_domain == Role.origin_domain)
                    & (MemberRole.guild_id == guild.id)
                    & (MemberRole.guild_domain == guild.origin_domain)
                    & (MemberRole.user_id == actor.id)
                    & (MemberRole.user_domain == actor.origin_domain),
                )
                .where(
                    Role.guild_id == guild.id,
                    Role.guild_domain == guild.origin_domain,
                    ((Role.id == guild.id) | MemberRole.role_id.is_not(None)),
                )
            )
        ).tuples()
    )
    privileged = int(Permission.ADMINISTRATOR | Permission.MANAGE_GUILD)
    if any(int(permissions) & privileged for _, _, permissions in role_rows):
        return True
    role_refs = {(role_id, role_domain) for role_id, role_domain, _ in role_rows}
    if not role_refs:
        return False
    return bool(
        await session.scalar(
            select(
                exists().where(
                    AutoModRuleExemptRole.rule_id == rule.id,
                    AutoModRuleExemptRole.rule_domain == rule.origin_domain,
                    tuple_(
                        AutoModRuleExemptRole.role_id,
                        AutoModRuleExemptRole.role_domain,
                    ).in_(role_refs),
                )
            )
        )
    )


def _member_profile_values(member: GuildMember, actor: User) -> tuple[tuple[str, str], ...]:
    values = [("username", actor.username)]
    if actor.display_name:
        values.append(("display_name", actor.display_name))
    if member.nickname:
        values.append(("nickname", member.nickname))
    return tuple(values)


@dataclass(frozen=True, slots=True)
class MemberProfileMatch:
    field_name: str
    content: str
    result: MatchResult


def _member_profile_match(
    rule: AutoModRule,
    member: GuildMember,
    actor: User,
) -> MemberProfileMatch | None:
    for field_name, value in _member_profile_values(member, actor):
        match = evaluate_trigger(rule.trigger_type, rule.trigger_metadata, value)
        if match.matched:
            return MemberProfileMatch(field_name, value, match)
    return None


def _member_profile_digest(member: GuildMember, actor: User) -> str:
    value = "\0".join(value for _, value in _member_profile_values(member, actor))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _execution_dispatch(
    guild: Guild,
    rule: AutoModRule,
    actor: User,
    *,
    channel: Channel | None,
    action: AutoModAction,
    outcome: str,
    content: str,
    matched_keyword: str | None,
    matched_content: str | None,
    digest: str,
    alert_message: Message | None = None,
) -> dict[str, Any]:
    return {
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
        "channel_id": str(channel.id) if channel is not None else None,
        "channel_domain": channel.origin_domain if channel is not None else None,
        "rule_id": str(rule.id),
        "rule_domain": rule.origin_domain,
        "rule_trigger_type": rule.trigger_type,
        "user_id": str(actor.id),
        "user_domain": actor.origin_domain,
        "action": {
            "type": action.action_type,
            "metadata": dict(action.action_metadata or {}),
        },
        "outcome": outcome,
        "content": content,
        "matched_keyword": matched_keyword,
        "matched_content": matched_content,
        "alert_system_message_id": (str(alert_message.id) if alert_message is not None else None),
        "alert_system_message_domain": (
            alert_message.origin_domain if alert_message is not None else None
        ),
        "content_digest": digest,
    }


async def _queue_execution_projection(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    event: dict[str, Any],
    post_commit: AutoModPostCommit,
    *,
    channel: Channel | None,
) -> None:
    # Guild mutations are replicated to every participating member instance,
    # not only to bot runtimes holding MESSAGE_CONTENT. Preserve the semantic
    # event and integrity digest across federation without replicating the
    # triggering plaintext; bots connect directly to the guild authority and
    # receive the full local dispatch after the normal intent/scope filter.
    federated_event = dict(event)
    federated_event["content"] = ""
    federated_event["matched_content"] = None
    federated_event["content_digest"] = None
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.automod.execution",
        {"execution": federated_event},
        channel=channel,
    )
    post_commit.add_guild(guild)
    post_commit.add_dispatch(guild, "AUTO_MODERATION_ACTION_EXECUTION", event)


async def _queue_timeout_member_projection(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    member: GuildMember,
    post_commit: AutoModPostCommit,
) -> None:
    """Replicate an AutoMod timeout through the normal guild-member contract."""

    member.member_version += 1
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.member.update",
        {
            "member": {
                "user": {
                    "id": str(member.user_id),
                    "origin_domain": member.user_domain,
                },
                "nickname": member.nickname,
                "timeout_until": (
                    member.timeout_until.isoformat() if member.timeout_until is not None else None
                ),
                "timeout_indefinite": member.timeout_indefinite,
                "member_version": str(member.member_version),
            }
        },
        snapshot_required=True,
    )
    post_commit.add_guild(guild)
    post_commit.add_dispatch(
        guild,
        "GUILD_MEMBER_UPDATE",
        {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "user_id": str(member.user_id),
            "user_domain": member.user_domain,
        },
    )


async def _create_alert_message(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    rule: AutoModRule,
    actor: User,
    action: AutoModAction,
    post_commit: AutoModPostCommit,
    *,
    source_channel: Channel | None,
    evidence: dict[str, object],
) -> Message | None:
    channel_ref = (action.action_metadata or {}).get("channel_id")
    if not isinstance(channel_ref, str):
        return None
    try:
        channel_id, channel_domain = EntityRef(channel_ref).resolve(settings.domain)
    except ValueError:
        return None
    channel = await session.get(Channel, (channel_id, channel_domain))
    if (
        channel is None
        or channel.unavailable
        or not is_message_capable_channel_type(channel.type, guild_channel=True)
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        or channel.encryption_mode == "e2ee"
        or channel.e2ee_required
    ):
        return None
    creator = await session.get(User, (rule.creator_id, rule.creator_domain))
    if creator is None:
        return None
    source = (
        f" in #{source_channel.name}" if source_channel is not None and source_channel.name else ""
    )
    profile_field = evidence.get("profile_field")
    context = (
        f" The member's {str(profile_field).replace('_', ' ')} matched the profile rule."
        if isinstance(profile_field, str)
        else ""
    )
    rule_name = " ".join(rule.name.split())
    actor_name = " ".join((actor.display_name or actor.username).split())
    content = (
        f'AutoMod applied rule "{rule_name}" to '
        f"{actor_name}@{actor.origin_domain}{source}.{context}"
    )
    message = Message(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=creator.id,
        author_domain=creator.origin_domain,
        content=content,
        encryption_policy_generation=channel.encryption_policy_generation,
        encryption_epoch=channel.encryption_epoch,
        message_type=24,
        flags=4,
        mention_user_refs=[],
    )
    session.add(message)
    await session.flush()
    channel.last_message_id = message.id
    channel.last_message_domain = message.origin_domain
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            mention_user_refs=[],
        )
    )
    rendered = message_payload(message, creator, [])
    await queue_guild_mutation(
        session,
        settings,
        guild,
        creator,
        "guild.message.create",
        {"message": rendered, "author": profile_from_user(creator)},
        channel=channel,
    )
    post_commit.add_guild(guild)
    post_commit.add_dispatch(guild, "MESSAGE_CREATE", rendered)
    return message


async def evaluate_member_profile(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    actor: User,
) -> AutoModPostCommit:
    """Evaluate a member profile and persist/release Discord-style quarantine.

    The caller owns the surrounding mutation transaction and must commit before
    invoking ``AutoModPostCommit.publish``. This keeps gateway events from
    advertising rows that may still roll back.
    """

    post_commit = AutoModPostCommit()
    if guild.origin_domain != settings.domain or actor.account_type == "bot":
        return post_commit
    member = await session.scalar(
        select(GuildMember)
        .where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_id == actor.id,
            GuildMember.user_domain == actor.origin_domain,
        )
        .with_for_update()
    )
    if member is None:
        return post_commit
    rules = list(
        await session.scalars(
            _active_rules_statement(
                guild,
                "member_update",
                trigger_type="member_profile",
            )
        )
    )
    live_rule_refs = {(rule.id, rule.origin_domain) for rule in rules}
    existing_blocks = {
        (item.rule_id, item.rule_domain): item
        for item in await session.scalars(
            select(AutoModMemberBlock).where(
                AutoModMemberBlock.guild_id == guild.id,
                AutoModMemberBlock.guild_domain == guild.origin_domain,
                AutoModMemberBlock.user_id == actor.id,
                AutoModMemberBlock.user_domain == actor.origin_domain,
            )
        )
    }
    for rule_ref, block in existing_blocks.items():
        if rule_ref not in live_rule_refs:
            await session.delete(block)

    profile_digest = _member_profile_digest(member, actor)
    for rule in rules:
        rule_ref = (rule.id, rule.origin_domain)
        existing = existing_blocks.get(rule_ref)
        if await _is_member_profile_exempt(session, guild, actor, rule):
            if existing is not None:
                await session.delete(existing)
            continue
        match = _member_profile_match(rule, member, actor)
        if match is None:
            if existing is not None:
                await session.delete(existing)
            continue
        field_name = match.field_name
        match_kind = match.result.kind
        matched_keyword = match.result.keyword
        evidence: dict[str, object] = {
            "profile_field": field_name,
            "match_kind": match_kind,
            "matched_keyword": matched_keyword,
        }
        actions = list(
            await session.scalars(
                select(AutoModAction)
                .where(
                    AutoModAction.rule_id == rule.id,
                    AutoModAction.rule_domain == rule.origin_domain,
                )
                .order_by(AutoModAction.position)
            )
        )
        has_block = any(item.action_type == "block_member_interaction" for item in actions)
        if has_block:
            if existing is None:
                existing = AutoModMemberBlock(
                    rule_id=rule.id,
                    rule_domain=rule.origin_domain,
                    guild_id=guild.id,
                    guild_domain=guild.origin_domain,
                    user_id=actor.id,
                    user_domain=actor.origin_domain,
                    profile_digest=profile_digest,
                    evidence=evidence,
                )
                session.add(existing)
            else:
                existing.profile_digest = profile_digest
                existing.evidence = evidence
        elif existing is not None:
            await session.delete(existing)

        event_actions: list[tuple[AutoModAction, str, Message | None]] = []
        for action in actions:
            idempotency_material = (
                f"profile:{guild.origin_domain}:{guild.id}:{actor.origin_domain}:{actor.id}:"
                f"{rule.origin_domain}:{rule.id}:{rule.version}:{action.position}:{profile_digest}"
            )
            idempotency_key = (
                "profile:" + hashlib.sha256(idempotency_material.encode("utf-8")).hexdigest()
            )
            already_recorded = await session.scalar(
                select(AutoModExecution.id).where(
                    AutoModExecution.idempotency_key == idempotency_key
                )
            )
            if already_recorded is not None:
                continue
            alert_message: Message | None = None
            outcome = "blocked"
            if action.action_type == "send_alert_message":
                alert_message = await _create_alert_message(
                    session,
                    settings,
                    snowflake,
                    guild,
                    rule,
                    actor,
                    action,
                    post_commit,
                    source_channel=None,
                    evidence=evidence,
                )
                outcome = "alerted" if alert_message is not None else "failed"
            elif action.action_type != "block_member_interaction":
                # Schema validation prevents other member-profile actions. A
                # defensive failed record preserves observability if legacy
                # data predates that invariant.
                outcome = "failed"
            session.add(
                AutoModExecution(
                    id=await snowflake.mint(),
                    rule_id=rule.id,
                    rule_domain=rule.origin_domain,
                    action_type=action.action_type,
                    guild_id=guild.id,
                    guild_domain=guild.origin_domain,
                    channel_id=(alert_message.channel_id if alert_message is not None else None),
                    channel_domain=(
                        alert_message.channel_domain if alert_message is not None else None
                    ),
                    message_id=(alert_message.id if alert_message is not None else None),
                    message_domain=(
                        alert_message.origin_domain if alert_message is not None else None
                    ),
                    target_user_id=actor.id,
                    target_user_domain=actor.origin_domain,
                    matched_content_digest=profile_digest,
                    evidence=evidence,
                    outcome=outcome,
                    idempotency_key=idempotency_key,
                )
            )
            event_actions.append((action, outcome, alert_message))
        if not event_actions:
            continue
        creator = await session.get(User, (rule.creator_id, rule.creator_domain))
        if creator is not None:
            await add_audit_entry(
                session,
                snowflake,
                guild,
                creator,
                AUTOMOD_AUDIT_EXECUTE,
                target_type="user",
                target_ref={"id": str(actor.id), "origin_domain": actor.origin_domain},
                changes=[
                    {"key": "rule_id", "new_value": str(rule.id)},
                    {"key": "profile_field", "new_value": field_name},
                ],
            )
        for action, outcome, alert_message in event_actions:
            event = _execution_dispatch(
                guild,
                rule,
                actor,
                channel=None,
                action=action,
                outcome=outcome,
                content=match.content,
                matched_keyword=matched_keyword,
                matched_content=match.result.matched_content,
                digest=profile_digest,
                alert_message=alert_message,
            )
            await _queue_execution_projection(
                session,
                settings,
                guild,
                actor,
                event,
                post_commit,
                channel=None,
            )
    return post_commit


async def require_member_interactions_allowed(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    needed: Permission,
) -> None:
    """Reject timeout- or profile-quarantined interactive guild actions."""

    if actor.account_type == "bot" or not needed & BLOCKED_MEMBER_INTERACTION_PERMISSIONS:
        return
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
    )
    if member is None:
        return
    timeout_detail = member_timeout_error_detail(member)
    if timeout_detail is not None:
        raise HTTPException(status_code=403, detail=timeout_detail)
    rows = list(
        (
            await session.execute(
                select(AutoModMemberBlock, AutoModRule)
                .select_from(AutoModRule)
                .outerjoin(
                    AutoModMemberBlock,
                    (AutoModMemberBlock.rule_id == AutoModRule.id)
                    & (AutoModMemberBlock.rule_domain == AutoModRule.origin_domain)
                    & (AutoModMemberBlock.guild_id == guild.id)
                    & (AutoModMemberBlock.guild_domain == guild.origin_domain)
                    & (AutoModMemberBlock.user_id == actor.id)
                    & (AutoModMemberBlock.user_domain == actor.origin_domain),
                )
                .where(
                    AutoModRule.guild_id == guild.id,
                    AutoModRule.guild_domain == guild.origin_domain,
                    AutoModRule.enabled.is_(True),
                    AutoModRule.event_type == "member_update",
                    AutoModRule.trigger_type == "member_profile",
                    exists().where(
                        AutoModAction.rule_id == AutoModRule.id,
                        AutoModAction.rule_domain == AutoModRule.origin_domain,
                        AutoModAction.action_type == "block_member_interaction",
                    ),
                )
                .order_by(AutoModRule.id)
            )
        ).all()
    )
    if not rows:
        return
    for block, rule in rows:
        if await _is_member_profile_exempt(session, guild, actor, rule):
            if block is not None:
                await session.delete(block)
            continue
        match = _member_profile_match(rule, member, actor)
        if match is None:
            if block is not None:
                await session.delete(block)
            continue
        # A local profile edit is committed before its per-guild AutoMod
        # evaluation transactions to avoid an unbounded User -> Guild lock
        # fan-out. Evaluate the live rule here as well as the durable block so
        # that narrow post-commit window fails closed. The ordinary evaluator
        # still persists the block, audit entry, execution, and federation
        # projection immediately after the profile transaction.
        field_name = match.field_name
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTO_MOD_MEMBER_INTERACTION_BLOCKED",
                "message": (
                    "AutoMod has temporarily limited your interactions in this guild because "
                    f"your {field_name.replace('_', ' ')} matches the member-profile rule "
                    f'"{rule.name}". Update that profile field to interact again.'
                ),
                "rule_id": str(rule.id),
                "rule_domain": rule.origin_domain,
                "profile_field": field_name,
            },
        )


async def evaluate_message(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    channel: Channel,
    actor: User,
    content: str | None,
    *,
    mention_count: int,
    actor_permissions: int,
    commit_on_block: bool = True,
) -> AutoModPostCommit:
    """Evaluate only at the authoritative guild home before message commit."""

    post_commit = AutoModPostCommit()
    if (
        not content
        or actor.account_type == "bot"
        or guild.origin_domain != settings.domain
        or channel.encryption_mode == "e2ee"
        or channel.e2ee_required
    ):
        return post_commit
    rules = list(await session.scalars(_active_rules_statement(guild, "message_send", limit=100)))
    # Recompute at the authority from visible syntax. The caller-provided
    # projection can contain thousands of recipients expanded from one role.
    mention_count = syntactic_mention_count(
        content,
        default_domain=guild.origin_domain,
    )
    now = datetime.now(UTC)
    for rule in rules:
        if await _is_exempt(session, guild, channel, actor, rule, actor_permissions):
            continue
        evaluated_mention_count = mention_count
        if (
            rule.trigger_type == "mention_spam"
            and bool(rule.trigger_metadata.get("mention_raid_protection_enabled"))
            and mention_count
            >= max(3, int(rule.trigger_metadata.get("mention_total_limit", 50)) // 2)
        ):
            raid_key = (
                f"automod:mention-raid:{guild.origin_domain}:{guild.id}:"
                f"{rule.id}:{actor.origin_domain}:{actor.id}"
            )
            raid_count = await redis.incr(raid_key)
            await redis.expire(raid_key, 30)
            if raid_count >= 3:
                evaluated_mention_count = (
                    int(rule.trigger_metadata.get("mention_total_limit", 50)) + 1
                )
        matched = evaluate_trigger(
            rule.trigger_type,
            rule.trigger_metadata,
            content,
            mention_count=evaluated_mention_count,
        )
        if not matched.matched:
            continue
        actions = list(
            await session.scalars(
                select(AutoModAction)
                .where(
                    AutoModAction.rule_id == rule.id,
                    AutoModAction.rule_domain == rule.origin_domain,
                )
                .order_by(AutoModAction.position)
            )
        )
        blocked = False
        block_message = "This message was blocked by AutoMod."
        event_actions: list[tuple[AutoModAction, str, Message | None]] = []
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        evidence: dict[str, object] = {
            "match_kind": matched.kind,
            "matched_keyword": matched.keyword,
        }
        for action in actions:
            metadata = action.action_metadata or {}
            outcome = "failed"
            alert_message: Message | None = None
            if action.action_type in {"block_message", "block_member_interaction"}:
                blocked = True
                outcome = "blocked"
                custom = metadata.get("custom_message")
                if isinstance(custom, str):
                    block_message = custom
            elif action.action_type == "send_alert_message":
                alert_message = await _create_alert_message(
                    session,
                    settings,
                    snowflake,
                    guild,
                    rule,
                    actor,
                    action,
                    post_commit,
                    source_channel=channel,
                    evidence=evidence,
                )
                outcome = "alerted" if alert_message is not None else "failed"
            elif action.action_type == "timeout":
                duration = int(metadata.get("duration_seconds", 0))
                if duration > 0:
                    member = await session.get(
                        GuildMember,
                        (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
                        with_for_update=True,
                    )
                    if member is not None:
                        proposed = now + timedelta(seconds=min(duration, 28 * 24 * 60 * 60))
                        if not member.timeout_indefinite and (
                            member.timeout_until is None or member.timeout_until < proposed
                        ):
                            member.timeout_until = proposed
                            member.timeout_reason = f"AutoMod rule: {rule.name}"
                            await _queue_timeout_member_projection(
                                session,
                                settings,
                                guild,
                                actor,
                                member,
                                post_commit,
                            )
                        outcome = "timed_out"
            execution_id = await snowflake.mint()
            idempotency_material = (
                f"message-pending:{guild.origin_domain}:{guild.id}:{channel.origin_domain}:"
                f"{channel.id}:{actor.origin_domain}:{actor.id}:{rule.origin_domain}:"
                f"{rule.id}:{action.position}:{digest}:{execution_id}"
            )
            execution = AutoModExecution(
                id=execution_id,
                rule_id=rule.id,
                rule_domain=rule.origin_domain,
                action_type=action.action_type,
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                message_id=None,
                message_domain=None,
                target_user_id=actor.id,
                target_user_domain=actor.origin_domain,
                matched_content_digest=digest,
                evidence={
                    **evidence,
                    **(
                        {
                            "alert_message_id": str(alert_message.id),
                            "alert_message_domain": alert_message.origin_domain,
                        }
                        if alert_message is not None
                        else {}
                    ),
                },
                outcome=outcome,
                idempotency_key="message:"
                + hashlib.sha256(idempotency_material.encode("utf-8")).hexdigest(),
            )
            session.add(execution)
            event_actions.append((action, outcome, alert_message))
        creator = await session.get(User, (rule.creator_id, rule.creator_domain))
        if creator is not None:
            await add_audit_entry(
                session,
                snowflake,
                guild,
                creator,
                AUTOMOD_AUDIT_EXECUTE,
                target_type="user",
                target_ref={"id": str(actor.id), "origin_domain": actor.origin_domain},
                changes=[{"key": "rule_id", "new_value": str(rule.id)}],
            )
        for action, outcome, alert_message in event_actions:
            event = _execution_dispatch(
                guild,
                rule,
                actor,
                channel=channel,
                action=action,
                outcome=outcome,
                content=content,
                matched_keyword=matched.keyword,
                matched_content=matched.matched_content,
                digest=digest,
                alert_message=alert_message,
            )
            await _queue_execution_projection(
                session,
                settings,
                guild,
                actor,
                event,
                post_commit,
                channel=channel,
            )
        if blocked:
            if commit_on_block:
                await session.commit()
                await post_commit.publish(redis)
            raise AutoModMessageBlocked(block_message, post_commit)
    return post_commit
