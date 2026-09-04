from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import ConfigDict, Field, ValidationError, model_validator
from redis.asyncio import Redis
from sqlalchemy import and_, delete, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.api.channels import refresh_thread_last_message_after_delete
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guilds import local_guild
from app.automod.service import AutoModPostCommit, evaluate_member_profile
from app.bots.e2ee import revoke_bot_e2ee_access
from app.bots.installations import (
    cleanup_installation_roles,
    publish_deleted_installation_roles,
    revoke_installations_for_guild_instance,
    revoke_installations_for_guild_member,
)
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.audit_access import filter_restricted_bot_audit_entries
from app.chat.audit_payloads import AuditLogEntryPayload, audit_log_payload
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import (
    build_guild_authority_envelope,
    federation_channel_state,
    guild_authority_owner,
    queue_guild_access_revocation,
    queue_guild_instance_access_revocation,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.hierarchy import (
    guild_member,
    highest_role,
    require_can_manage_member,
    role_rank,
)
from app.chat.moderation_status import guild_self_moderation_status, sanitize_timeout_reason
from app.chat.payloads import (
    ban_payload,
    channel_payload,
    instance_ban_payload,
    member_payload,
)
from app.chat.permissions import bot_guild_permission_grant, require_permissions
from app.chat.schemas import BanCreate, InstanceBanCreate, MemberUpdate
from app.chat.thread_membership import (
    RemovedThreadMembers,
    cleanup_guild_member_threads,
    publish_guild_thread_member_cleanup,
)
from app.core.model_validation import UnambiguousInputModel
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, Snowflake, validate_snowflake
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Attachment,
    AuditLogEntry,
    Ban,
    Channel,
    Guild,
    GuildInstanceBan,
    GuildMember,
    Instance,
    MemberRole,
    Message,
    Role,
    User,
)
from app.federation.client import signed_request
from app.federation.guild_management import (
    guild_management_dict_body,
    guild_management_list_body,
    proxy_remote_guild_management,
    qualified_management_ref,
    require_guild_management_status,
)
from app.federation.guild_media_deletions import queue_guild_media_delete_request
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.schemas import (
    ActorRef,
    FederationDomain,
    GuildSelfModerationStatus,
    SnowflakeString,
)
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
    validated_event_envelope,
)
from app.federation.terminal_rooms import lock_terminal_room
from app.media.service import attachments_for_messages
from app.media.tombstones import lock_media_tombstone_ref, queue_terminal_attachment_tombstone
from app.tracker.membership import clear_tracker_assignees, wake_tracker_membership_cleanup

router = APIRouter(prefix="/api/v1/guilds", tags=["moderation"])
federation_router = APIRouter(tags=["audit log federation"])


def _federated_username_parts(search: str) -> tuple[str, str] | None:
    username, separator, domain = search.strip().lstrip("@").rpartition("@")
    return (username, domain) if separator and username and domain else None


AUDIT_LOG_FEDERATION_CAPABILITY = "guild-audit-log/1"
AUDIT_LOG_FEDERATION_EVENT_TYPE = "guild.audit-log.page"
AUDIT_LOG_FEDERATION_DEADLINE_SECONDS = 15
AUDIT_LOG_FEDERATION_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AuditLogFederationQuery(UnambiguousInputModel):
    """Canonical audit filters echoed inside the authority-signed response."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=100)
    before: SnowflakeString | None = None
    after: SnowflakeString | None = None
    user: ActorRef | None = None
    action_type: int | None = Field(default=None, ge=0, le=2_147_483_647)
    target_type: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )

    @model_validator(mode="after")
    def one_cursor_direction(self) -> AuditLogFederationQuery:
        if self.before is not None and self.after is not None:
            raise ValueError("an audit log query cannot use before and after together")
        return self


class AuditLogFederationRequest(UnambiguousInputModel):
    """Short-lived, requester-bound authorization request to a guild home."""

    model_config = ConfigDict(extra="forbid")

    guild_id: SnowflakeString
    guild_domain: FederationDomain
    requester: ActorRef
    requesting_instance: FederationDomain
    request_id: str = Field(pattern=r"^kalr_[A-Za-z0-9_-]{32}$")
    issued_at: int = Field(ge=0)
    deadline: int = Field(ge=0)
    query: AuditLogFederationQuery

    @model_validator(mode="after")
    def short_lived(self) -> AuditLogFederationRequest:
        lifetime = self.deadline - self.issued_at
        if not 1 <= lifetime <= AUDIT_LOG_FEDERATION_DEADLINE_SECONDS:
            raise ValueError("audit log request deadline is outside the allowed window")
        return self


class AuditLogFederationPage(UnambiguousInputModel):
    """Authority-signed page whose exact request binding prevents substitution."""

    model_config = ConfigDict(extra="forbid")

    request: AuditLogFederationRequest
    entries: list[AuditLogEntryPayload] = Field(max_length=100)


async def queue_moderation_push(
    *,
    user_id: int,
    user_domain: str,
    guild: Guild,
    event_id: int,
    title: str,
    body: str,
) -> None:
    from app.tasks import mobile_push_activity

    await enqueue_best_effort(
        mobile_push_activity,
        user_id,
        user_domain,
        event_id,
        guild.origin_domain,
        "moderation",
        title,
        body,
        f"moderation:{guild.id}@{guild.origin_domain}:{event_id}",
    )


def future_expiry(value: datetime | None, *, code: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise HTTPException(status_code=400, detail={"code": f"{code}_REQUIRES_TIMEZONE"})
    normalized = value.astimezone(UTC)
    if normalized <= datetime.now(UTC):
        raise HTTPException(status_code=400, detail={"code": f"{code}_MUST_BE_FUTURE"})
    return normalized


def active_expiry(column: InstrumentedAttribute[datetime | None]) -> ColumnElement[bool]:
    return or_(column.is_(None), column > datetime.now(UTC))


@dataclass(slots=True)
class MemberModerationPostCommit:
    """Best-effort projections for a committed kick or ban mutation."""

    guild: Guild
    user_id: int
    user_domain: str
    member_removed: bool
    deleted_role_refs: list[tuple[int, str]]
    removed_thread_members: list[RemovedThreadMembers]
    e2ee_policy_channels: list[Channel]
    notification_title: str
    notification_body: str
    purged_local_attachments: list[Attachment] = dataclass_field(default_factory=list)
    media_delivery_wakes: set[str] = dataclass_field(default_factory=set)
    purged_threads: list[Channel] = dataclass_field(default_factory=list)

    async def publish(
        self,
        session: AsyncSession,
        redis: Redis,
        snowflake: SnowflakeGenerator,
        settings: Settings,
    ) -> None:
        """Run projections only after the owning SQL transaction commits."""

        if self.member_removed:
            await wake_tracker_membership_cleanup(self.guild)
        else:
            await wake_queued_guild_federation(self.guild)
        await publish_e2ee_policy_updates(
            session,
            redis,
            settings,
            self.e2ee_policy_channels,
        )
        if self.purged_local_attachments or self.media_delivery_wakes:
            from app.tasks import federation_deliver, media_local_purge

            for attachment in self.purged_local_attachments:
                await enqueue_best_effort(
                    media_local_purge,
                    attachment.id,
                    attachment.origin_domain,
                )
            for destination in sorted(self.media_delivery_wakes):
                await enqueue_best_effort(federation_deliver, destination)
        await publish_deleted_installation_roles(redis, self.guild, self.deleted_role_refs)
        await publish_guild_thread_member_cleanup(
            redis,
            self.guild,
            self.removed_thread_members,
        )
        await materialize_updated_at(session, *self.purged_threads)
        for thread in self.purged_threads:
            await publish_dispatch(
                redis,
                guild_topic(self.guild.origin_domain, self.guild.id),
                "THREAD_UPDATE",
                channel_payload(thread),
            )
        if self.member_removed:
            await publish_dispatch(
                redis,
                guild_topic(self.guild.origin_domain, self.guild.id),
                "GUILD_MEMBER_REMOVE",
                {
                    "guild_id": str(self.guild.id),
                    "guild_domain": self.guild.origin_domain,
                    "user_id": str(self.user_id),
                    "user_domain": self.user_domain,
                },
            )
            if self.user_domain == settings.domain:
                await publish_dispatch(
                    redis,
                    user_topic(self.user_domain, self.user_id),
                    "GUILD_DELETE",
                    {"id": str(self.guild.id), "origin_domain": self.guild.origin_domain},
                )
        await queue_moderation_push(
            user_id=self.user_id,
            user_domain=self.user_domain,
            guild=self.guild,
            event_id=await snowflake.mint(),
            title=self.notification_title,
            body=self.notification_body,
        )


@router.get("/{guild_id}/members/@me/moderation-status")
async def self_moderation_status(
    guild_id: EntityRef,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return timeout details only to the member they describe.

    Remote guild state is fetched on demand over a signed request. The reason
    is never inserted into the local guild replica or a guild-topic event.
    """

    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["self_moderation_status"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    resolved_id, resolved_domain = guild_id.resolve(settings.domain)
    guild = await session.get(Guild, (resolved_id, resolved_domain))
    member = await session.get(
        GuildMember,
        (resolved_id, resolved_domain, auth.user.id, auth.user.origin_domain),
    )
    if guild is None or member is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_A_GUILD_MEMBER"})
    if resolved_domain == settings.domain:
        return guild_self_moderation_status(member).model_dump(mode="json")
    fallback = guild_self_moderation_status(member)
    # The broad replica projection intentionally has timing but not a reason.
    # Avoid a federation round trip for the overwhelmingly common inactive
    # state and preserve quiet rolling-upgrade behavior.
    if not fallback.timed_out:
        return fallback.model_dump(mode="json")
    fallback = GuildSelfModerationStatus(
        guild_id=fallback.guild_id,
        guild_domain=fallback.guild_domain,
        timed_out=True,
        timeout_until=fallback.timeout_until,
        timeout_indefinite=fallback.timeout_indefinite,
        reason=None,
        details_available=False,
    )
    peer = await session.get(Instance, resolved_domain)
    if peer is None or "member-self-moderation/1" not in (peer.capabilities or []):
        # Rolling upgrades must converge without a user action. Queue only a
        # domain-level discovery refresh (never a user or timeout identifier),
        # deduped for an hour, and return the replica timing immediately.
        discovery_key = f"federation:self-moderation-capability-refresh:{resolved_domain}"
        if await redis.set(discovery_key, "1", ex=60 * 60, nx=True):
            # Lazy import keeps task registration out of the API import graph.
            from app.tasks import federation_self_moderation_capability_refresh

            if not await enqueue_best_effort(
                federation_self_moderation_capability_refresh,
                resolved_domain,
            ):
                await redis.delete(discovery_key)
        return fallback.model_dump(mode="json")
    try:
        upstream = await signed_request(
            session,
            settings,
            "GET",
            resolved_domain,
            f"/_kaede/v1/guilds/{resolved_id}/members/{auth.user.id}/moderation-status",
            max_response_bytes=8 * 1024,
        )
    except (FederationNetworkError, RuntimeError):
        return fallback.model_dump(mode="json")
    if upstream.status_code != 200:
        # Old/mixed-version homes and transient failures must not make the
        # timing projection disappear. The private reason remains unknown.
        return fallback.model_dump(mode="json")
    try:
        status_payload = GuildSelfModerationStatus.model_validate(
            decode_federation_response_json(upstream, max_response_bytes=8 * 1024)
        )
    except (FederationNetworkError, ValidationError):
        return fallback.model_dump(mode="json")
    if (int(status_payload.guild_id), status_payload.guild_domain) != (
        resolved_id,
        resolved_domain,
    ):
        return fallback.model_dump(mode="json")
    if status_payload.timeout_until is not None and status_payload.timeout_until <= datetime.now(
        UTC
    ):
        status_payload = GuildSelfModerationStatus(
            guild_id=status_payload.guild_id,
            guild_domain=status_payload.guild_domain,
            timed_out=False,
        )
    return status_payload.model_dump(mode="json")


@router.get("/{guild_id}/members")
async def list_members(
    guild_id: EntityRef,
    limit: int = Query(default=50, ge=1, le=1000),
    after: EntityRef | None = None,
    query: str | None = Query(default=None, min_length=1, max_length=100),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    resolved_guild_id, resolved_guild_domain = guild_id.resolve(settings.domain)
    guild = await session.scalar(
        select(Guild).where(
            Guild.id == resolved_guild_id,
            Guild.origin_domain == resolved_guild_domain,
        )
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await require_permissions(session, redis, guild, auth.user, required_permissions("member.list"))
    conditions: list[ColumnElement[bool]] = [
        GuildMember.guild_id == guild.id,
        GuildMember.guild_domain == guild.origin_domain,
    ]
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        conditions.append(
            tuple_(GuildMember.user_id, GuildMember.user_domain) > (after_id, after_domain)
        )
    if query is not None:
        search = query.strip().lstrip("@").strip()
        if search:
            handle = _federated_username_parts(search)
            if handle:
                username, domain = handle
                conditions.append(
                    and_(
                        User.username.icontains(username, autoescape=True),
                        User.origin_domain.icontains(domain, autoescape=True),
                    )
                )
            else:
                conditions.append(
                    or_(
                        User.username.icontains(search, autoescape=True),
                        User.display_name.icontains(search, autoescape=True),
                        User.origin_domain.icontains(search, autoescape=True),
                        GuildMember.nickname.icontains(search, autoescape=True),
                    )
                )
    rows = (
        await session.execute(
            select(GuildMember, User)
            .join(
                User,
                (User.id == GuildMember.user_id) & (User.origin_domain == GuildMember.user_domain),
            )
            .where(*conditions)
            .order_by(GuildMember.user_id, GuildMember.user_domain)
            .limit(limit)
        )
    ).all()
    refs = [(member.user_id, member.user_domain) for member, _ in rows]
    roles: dict[tuple[int, str], list[int]] = {ref: [] for ref in refs}
    if refs:
        assignments = await session.scalars(
            select(MemberRole).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                tuple_(MemberRole.user_id, MemberRole.user_domain).in_(refs),
            )
        )
        for assignment in assignments:
            roles[(assignment.user_id, assignment.user_domain)].append(assignment.role_id)
    return [
        member_payload(
            member,
            user,
            roles[(member.user_id, member.user_domain)],
            include_private_authority_state=guild.origin_domain == settings.domain,
        )
        for member, user in rows
    ]


@router.patch("/{guild_id}/members/{user_id}")
async def update_member(
    guild_id: EntityRef,
    user_id: EntityRef,
    payload: MemberUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "member.update",
        {
            "user_ref": qualified_management_ref(user_id, settings.domain),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": reason,
        },
    )
    if proxied is not None:
        return guild_management_dict_body(proxied, 200)

    automod_post_commit = AutoModPostCommit()
    guild = await local_guild(session, settings, guild_id, for_update=True)
    user_number, user_domain = user_id.resolve(settings.domain)
    self_update = (user_number, user_domain) == (auth.user.id, auth.user.origin_domain)
    if self_update:
        member = await guild_member(session, guild, user_number, user_domain)
        if {"timeout_until", "timeout_indefinite"} & payload.model_fields_set:
            raise HTTPException(status_code=403, detail={"code": "CANNOT_TIMEOUT_SELF"})
        await require_permissions(
            session, redis, guild, auth.user, required_permissions("member.nickname.self")
        )
    else:
        member = await require_can_manage_member(
            session, guild, auth.user, user_number, user_domain
        )
        needed = Permission(0)
        if "nickname" in payload.model_fields_set:
            needed |= Permission.MANAGE_NICKNAMES
        if {"timeout_until", "timeout_indefinite"} & payload.model_fields_set:
            needed |= Permission.MODERATE_MEMBERS
        await require_permissions(session, redis, guild, auth.user, needed)
    values = payload.model_dump(exclude_unset=True)
    timeout_changed = bool({"timeout_until", "timeout_indefinite"} & payload.model_fields_set)
    timeout = values.get("timeout_until")
    timeout_indefinite = values.get("timeout_indefinite")
    if timeout_indefinite is True and timeout is not None:
        raise HTTPException(status_code=400, detail={"code": "TIMEOUT_MODE_CONFLICT"})
    if timeout_indefinite is True:
        values["timeout_until"] = None
    elif "timeout_until" in payload.model_fields_set and timeout is None:
        values["timeout_indefinite"] = False
    if timeout is not None:
        now = datetime.now(UTC)
        if timeout.tzinfo is None:
            raise HTTPException(status_code=400, detail={"code": "TIMEOUT_REQUIRES_TIMEZONE"})
        if timeout > now + timedelta(days=28):
            raise HTTPException(status_code=400, detail={"code": "TIMEOUT_TOO_LONG"})
        if timeout <= now:
            values["timeout_until"] = None
            values["timeout_indefinite"] = False
        else:
            values["timeout_until"] = timeout.astimezone(UTC)
            values["timeout_indefinite"] = False
    if timeout_changed:
        timed_out = bool(values.get("timeout_indefinite", member.timeout_indefinite)) or bool(
            values.get("timeout_until", member.timeout_until)
        )
        values["timeout_reason"] = (
            sanitize_timeout_reason(normalize_audit_reason(reason)) if timed_out else None
        )
    changes: list[dict[str, object]] = []
    for field, value in values.items():
        old = getattr(member, field)
        if old != value:
            changes.append({"key": field, "old_value": str(old), "new_value": str(value)})
            setattr(member, field, value)
    if changes:
        member.member_version += 1
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.member.update",
            {
                "member": {
                    "user": {"id": str(user_number), "origin_domain": user_domain},
                    "nickname": member.nickname,
                    "timeout_until": (
                        member.timeout_until.isoformat() if member.timeout_until else None
                    ),
                    "timeout_indefinite": member.timeout_indefinite,
                    "member_version": str(member.member_version),
                }
            },
            snapshot_required=True,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            24,
            target_type="member",
            target_ref={"id": str(user_number), "origin_domain": user_domain},
            reason=normalize_audit_reason(reason),
            changes=changes,
        )
        target_user = await session.get(User, (user_number, user_domain))
        if target_user is None:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
        if any(item["key"] == "nickname" for item in changes):
            automod_post_commit = await evaluate_member_profile(
                session,
                settings,
                snowflake,
                guild,
                target_user,
            )
        await session.commit()
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_UPDATE",
            {
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "user_id": str(user_number),
                "user_domain": user_domain,
            },
        )
        await automod_post_commit.publish(redis)
        if timeout_changed:
            await queue_moderation_push(
                user_id=user_number,
                user_domain=user_domain,
                guild=guild,
                event_id=await snowflake.mint(),
                title="Guild timeout updated",
                body=f"Your messaging restrictions in {guild.name} changed.",
            )
    target_user = await session.scalar(
        select(User).where(User.id == user_number, User.origin_domain == user_domain)
    )
    if target_user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    roles = list(
        await session.scalars(
            select(MemberRole.role_id).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                MemberRole.user_id == user_number,
                MemberRole.user_domain == user_domain,
            )
        )
    )
    return member_payload(
        member,
        target_user,
        roles,
        include_private_authority_state=True,
    )


async def stage_kick_member(
    guild_id: EntityRef,
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
    *,
    record_kick_audit: bool,
) -> MemberModerationPostCommit:
    guild_number, guild_domain = guild_id.resolve(settings.domain)
    if guild_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await lock_terminal_room(session, "guild", guild_number, guild_domain)
    guild = await local_guild(session, settings, guild_id, for_update=True)
    user_number, user_domain = user_id.resolve(settings.domain)
    await require_permissions(session, redis, guild, auth.user, required_permissions("member.kick"))
    member = await require_can_manage_member(session, guild, auth.user, user_number, user_domain)
    target_user = await session.get(User, (user_number, user_domain))
    if target_user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    e2ee_policy_channels: list[Channel] = []
    revoked_installations = await revoke_installations_for_guild_member(
        session,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=user_number,
        user_domain=user_domain,
    )
    e2ee_policy_channels.extend(
        await revoke_bot_e2ee_access(
            session,
            redis,
            settings,
            installation_ids=(item.id for item in revoked_installations),
        )
    )
    deleted_role_refs = await cleanup_installation_roles(
        session,
        settings,
        guild,
        auth.user,
        revoked_installations,
    )
    if record_kick_audit:
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            20,
            target_type="member",
            target_ref={"id": str(user_number), "origin_domain": user_domain},
            reason=normalize_audit_reason(reason),
        )
    removed_thread_members = await cleanup_guild_member_threads(
        session,
        settings,
        guild,
        auth.user,
        [(user_number, user_domain)],
    )
    await clear_tracker_assignees(
        session,
        settings,
        guild,
        auth.user,
        [(user_number, user_domain)],
    )
    await session.delete(member)
    await queue_guild_access_revocation(
        session,
        settings,
        guild,
        user_id=user_number,
        user_domain=user_domain,
        reason="member_kicked",
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.member.remove",
        {"user": {"id": str(user_number), "origin_domain": user_domain}},
        snapshot_required=True,
        e2ee_policy_channels=e2ee_policy_channels,
        pause_e2ee=target_user.account_type != "bot",
    )
    return MemberModerationPostCommit(
        guild=guild,
        user_id=user_number,
        user_domain=user_domain,
        member_removed=True,
        deleted_role_refs=deleted_role_refs,
        removed_thread_members=removed_thread_members,
        e2ee_policy_channels=e2ee_policy_channels,
        notification_title="Removed from guild",
        notification_body=f"You were removed from {guild.name}.",
    )


async def kick_member_service(
    guild_id: EntityRef,
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
    *,
    record_kick_audit: bool,
) -> Response:
    postcommit = await stage_kick_member(
        guild_id,
        user_id,
        auth,
        session,
        redis,
        snowflake,
        settings,
        reason,
        record_kick_audit=record_kick_audit,
    )
    await session.commit()
    await postcommit.publish(session, redis, snowflake, settings)
    return Response(status_code=204)


@router.delete("/{guild_id}/members/{user_id}", status_code=204)
async def kick_member(
    guild_id: EntityRef,
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "member.kick",
        {
            "user_ref": qualified_management_ref(user_id, settings.domain),
            "reason": reason,
        },
    )
    if proxied is not None:
        require_guild_management_status(proxied, 204)
        return Response(status_code=204)

    return await kick_member_service(
        guild_id,
        user_id,
        auth,
        session,
        redis,
        snowflake,
        settings,
        reason,
        record_kick_audit=True,
    )


async def stage_ban_member(
    guild_id: EntityRef,
    user_id: EntityRef,
    payload: BanCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    header_reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> MemberModerationPostCommit:
    guild_number, guild_domain = guild_id.resolve(settings.domain)
    if guild_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await lock_terminal_room(session, "guild", guild_number, guild_domain)
    guild = await local_guild(session, settings, guild_id, for_update=True)
    user_number, user_domain = user_id.resolve(settings.domain)
    await require_permissions(session, redis, guild, auth.user, required_permissions("member.ban"))
    user = await session.scalar(
        select(User).where(User.id == user_number, User.origin_domain == user_domain)
    )
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    member = await session.scalar(
        select(GuildMember).where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_id == user_number,
            GuildMember.user_domain == user_domain,
        )
    )
    e2ee_policy_channels: list[Channel] = []
    if member is not None:
        await require_can_manage_member(session, guild, auth.user, user_number, user_domain)
    elif (user_number, user_domain) == (guild.owner_id, guild.owner_domain):
        raise HTTPException(status_code=403, detail={"code": "OWNER_IMMUNE"})
    reason = normalize_audit_reason(header_reason or payload.reason)
    expires_at = future_expiry(payload.expires_at, code="BAN_EXPIRY")
    revoked_installations = await revoke_installations_for_guild_member(
        session,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=user_number,
        user_domain=user_domain,
    )
    e2ee_policy_channels.extend(
        await revoke_bot_e2ee_access(
            session,
            redis,
            settings,
            installation_ids=(item.id for item in revoked_installations),
        )
    )
    deleted_role_refs = await cleanup_installation_roles(
        session,
        settings,
        guild,
        auth.user,
        revoked_installations,
    )
    purged_local_attachments: list[Attachment] = []
    media_delivery_wakes: set[str] = set()
    purged_threads: list[Channel] = []
    removed_thread_members = []
    await session.execute(
        pg_insert(Ban)
        .values(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=user_number,
            user_domain=user_domain,
            reason=reason,
            actor_id=auth.user.id,
            actor_domain=auth.user.origin_domain,
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=["guild_id", "guild_domain", "user_id", "user_domain"],
            set_={
                "reason": reason,
                "actor_id": auth.user.id,
                "actor_domain": auth.user.origin_domain,
                "expires_at": expires_at,
            },
        )
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.ban.add",
        {
            "user": {"id": str(user_number), "origin_domain": user_domain},
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
    if payload.delete_message_seconds:
        cutoff = datetime.now(UTC) - timedelta(seconds=payload.delete_message_seconds)
        deleted_at = datetime.now(UTC)
        channel_ids = select(Channel.id).where(
            Channel.guild_id == guild.id, Channel.guild_domain == guild.origin_domain
        )
        affected_message_statement = select(Message).where(
            Message.channel_id.in_(channel_ids),
            Message.channel_domain == guild.origin_domain,
            Message.author_id == user_number,
            Message.author_domain == user_domain,
            Message.created_at >= cutoff,
            Message.deleted_at.is_(None),
        )
        affected_refs = set(
            (
                await session.execute(
                    select(Message.id, Message.origin_domain).where(
                        Message.channel_id.in_(channel_ids),
                        Message.channel_domain == guild.origin_domain,
                        Message.author_id == user_number,
                        Message.author_domain == user_domain,
                        Message.created_at >= cutoff,
                        Message.deleted_at.is_(None),
                    )
                )
            ).tuples()
        )
        affected_attachments = await attachments_for_messages(session, affected_refs)
        for attachment in sorted(
            (item for rows in affected_attachments.values() for item in rows),
            key=lambda item: (item.origin_domain, item.id),
        ):
            await lock_media_tombstone_ref(session, attachment.id, attachment.origin_domain)
        affected_messages = list(
            await session.scalars(affected_message_statement.with_for_update())
        )
        affected_by_ref = {
            (message.id, message.origin_domain): message for message in affected_messages
        }
        for message in affected_messages:
            message.content = None
            message.e2ee = None
            message.deleted_at = deleted_at
        await session.flush()
        affected_thread_refs = {
            (message.channel_id, message.channel_domain) for message in affected_messages
        }
        if affected_thread_refs:
            affected_threads = list(
                await session.scalars(
                    select(Channel)
                    .where(
                        tuple_(Channel.id, Channel.origin_domain).in_(affected_thread_refs),
                        Channel.type.in_({10, 11, 12}),
                    )
                    .with_for_update()
                )
            )
            messages_by_thread: dict[tuple[int, str], list[Message]] = {}
            for message in affected_messages:
                messages_by_thread.setdefault(
                    (message.channel_id, message.channel_domain), []
                ).append(message)
            for thread in affected_threads:
                deleted_replies = sum(
                    (message.id, message.origin_domain)
                    != (thread.starter_message_id, thread.starter_message_domain)
                    for message in messages_by_thread.get((thread.id, thread.origin_domain), [])
                )
                thread.message_count = max(
                    0,
                    int(thread.message_count or 0) - deleted_replies,
                )
                if (thread.last_message_id, thread.last_message_domain) in {
                    (message.id, message.origin_domain)
                    for message in messages_by_thread.get((thread.id, thread.origin_domain), [])
                }:
                    await refresh_thread_last_message_after_delete(session, thread)
            purged_threads = affected_threads
        for message_ref, attachments in affected_attachments.items():
            affected_message = affected_by_ref.get(message_ref)
            if affected_message is None:
                continue
            for attachment in attachments:
                if attachment.origin_domain == settings.domain:
                    purged_local_attachments.append(attachment)
                    media_delivery_wakes.update(
                        await queue_terminal_attachment_tombstone(
                            session,
                            settings,
                            attachment,
                        )
                    )
                else:
                    destination = await queue_guild_media_delete_request(
                        session,
                        settings,
                        guild=guild,
                        message=affected_message,
                        attachment=attachment,
                        deleted_at=deleted_at,
                    )
                    if destination is not None:
                        media_delivery_wakes.add(destination)
        await session.execute(
            update(Message)
            .where(
                Message.channel_id.in_(channel_ids),
                Message.channel_domain == guild.origin_domain,
                Message.author_id == user_number,
                Message.author_domain == user_domain,
                Message.created_at >= cutoff,
                Message.deleted_at.is_(None),
            )
            .values(content=None, e2ee=None, deleted_at=deleted_at)
        )
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.message.purge",
            {
                "author": {"id": str(user_number), "origin_domain": user_domain},
                "created_after": cutoff.isoformat(),
                "deleted_at": deleted_at.isoformat(),
            },
        )
        for thread in purged_threads:
            # The purge event performs the replica-side decrement first; this
            # complete state then fences counters and the surviving cursor.
            await queue_guild_mutation(
                session,
                settings,
                guild,
                auth.user,
                "guild.channel.update",
                {"channel": federation_channel_state(thread)},
                channel=thread,
            )
    if member is not None:
        removed_thread_members = await cleanup_guild_member_threads(
            session,
            settings,
            guild,
            auth.user,
            [(user_number, user_domain)],
        )
        await clear_tracker_assignees(
            session,
            settings,
            guild,
            auth.user,
            [(user_number, user_domain)],
        )
        await session.delete(member)
        await queue_guild_access_revocation(
            session,
            settings,
            guild,
            user_id=user_number,
            user_domain=user_domain,
            reason="member_banned",
        )
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.member.remove",
            {"user": {"id": str(user_number), "origin_domain": user_domain}},
            snapshot_required=True,
            e2ee_policy_channels=e2ee_policy_channels,
            pause_e2ee=user.account_type != "bot",
        )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        22,
        target_type="user",
        target_ref={"id": str(user_number), "origin_domain": user_domain},
        reason=reason,
    )
    return MemberModerationPostCommit(
        guild=guild,
        user_id=user_number,
        user_domain=user_domain,
        member_removed=member is not None,
        deleted_role_refs=deleted_role_refs,
        removed_thread_members=removed_thread_members,
        e2ee_policy_channels=e2ee_policy_channels,
        notification_title="Banned from guild",
        notification_body=f"You were banned from {guild.name}.",
        purged_local_attachments=purged_local_attachments,
        media_delivery_wakes=media_delivery_wakes,
        purged_threads=purged_threads,
    )


@router.put("/{guild_id}/bans/{user_id}", status_code=204)
async def ban_member(
    guild_id: EntityRef,
    user_id: EntityRef,
    payload: BanCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    header_reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "member.ban",
        {
            "user_ref": qualified_management_ref(user_id, settings.domain),
            "data": payload.model_dump(mode="json"),
            "reason": header_reason,
        },
    )
    if proxied is not None:
        require_guild_management_status(proxied, 204)
        return Response(status_code=204)

    postcommit = await stage_ban_member(
        guild_id,
        user_id,
        payload,
        auth,
        session,
        redis,
        snowflake,
        settings,
        header_reason,
    )
    await session.commit()
    await postcommit.publish(session, redis, snowflake, settings)
    return Response(status_code=204)


@router.delete("/{guild_id}/bans/{user_id}", status_code=204)
async def remove_ban(
    guild_id: EntityRef,
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "member.unban",
        {
            "user_ref": qualified_management_ref(user_id, settings.domain),
            "reason": reason,
        },
    )
    if proxied is not None:
        require_guild_management_status(proxied, 204)
        return Response(status_code=204)

    guild = await local_guild(session, settings, guild_id, for_update=True)
    user_number, user_domain = user_id.resolve(settings.domain)
    await require_permissions(session, redis, guild, auth.user, required_permissions("ban.remove"))
    result = await session.execute(
        delete(Ban)
        .where(
            Ban.guild_id == guild.id,
            Ban.guild_domain == guild.origin_domain,
            Ban.user_id == user_number,
            Ban.user_domain == user_domain,
        )
        .returning(Ban.user_id)
    )
    if result.scalar_one_or_none() is not None:
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.ban.remove",
            {"user": {"id": str(user_number), "origin_domain": user_domain}},
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            23,
            target_type="user",
            target_ref={"id": str(user_number), "origin_domain": user_domain},
            reason=normalize_audit_reason(reason),
        )
        await session.commit()
        await wake_queued_guild_federation(guild)
    return Response(status_code=204)


@router.get("/{guild_id}/bans")
async def list_bans(
    guild_id: EntityRef,
    limit: int = Query(default=50, ge=1, le=1000),
    after: EntityRef | None = None,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "member.ban.list",
        {
            "limit": limit,
            "after": (
                qualified_management_ref(after, settings.domain) if after is not None else None
            ),
        },
    )
    if proxied is not None:
        return cast(list[dict[str, object]], guild_management_list_body(proxied, 200))

    guild = await local_guild(session, settings, guild_id)
    await require_permissions(session, redis, guild, auth.user, required_permissions("ban.list"))
    conditions: list[ColumnElement[bool]] = [
        Ban.guild_id == guild.id,
        Ban.guild_domain == guild.origin_domain,
        active_expiry(Ban.expires_at),
    ]
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        conditions.append(tuple_(Ban.user_id, Ban.user_domain) > (after_id, after_domain))
    rows = (
        await session.execute(
            select(Ban, User)
            .join(User, (User.id == Ban.user_id) & (User.origin_domain == Ban.user_domain))
            .where(*conditions)
            .order_by(Ban.user_id, Ban.user_domain)
            .limit(limit)
        )
    ).all()
    return [ban_payload(ban, user) for ban, user in rows]


@router.get("/{guild_id}/instance-bans")
async def list_instance_bans(
    guild_id: EntityRef,
    limit: int = Query(default=50, ge=1, le=1000),
    after: str | None = Query(default=None, max_length=253),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "instance_ban.list",
        {"limit": limit, "after": after},
    )
    if proxied is not None:
        return cast(list[dict[str, object]], guild_management_list_body(proxied, 200))

    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("instance_ban.list")
    )
    conditions: list[ColumnElement[bool]] = [
        GuildInstanceBan.guild_id == guild.id,
        GuildInstanceBan.guild_domain == guild.origin_domain,
        active_expiry(GuildInstanceBan.expires_at),
    ]
    if after is not None:
        conditions.append(GuildInstanceBan.instance_domain > normalize_domain(after))
    rows = await session.scalars(
        select(GuildInstanceBan)
        .where(*conditions)
        .order_by(GuildInstanceBan.instance_domain)
        .limit(limit)
    )
    return [instance_ban_payload(item) for item in rows]


@router.put("/{guild_id}/instance-bans/{instance_domain}", status_code=204)
async def ban_instance(
    guild_id: EntityRef,
    instance_domain: str,
    payload: InstanceBanCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    header_reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "instance_ban.put",
        {
            "instance_domain": instance_domain,
            "data": payload.model_dump(mode="json"),
            "reason": header_reason,
        },
    )
    if proxied is not None:
        require_guild_management_status(proxied, 204)
        return Response(status_code=204)

    guild = await local_guild(session, settings, guild_id, for_update=True)
    domain = normalize_domain(instance_domain)
    if domain == settings.domain:
        raise HTTPException(status_code=400, detail={"code": "CANNOT_BAN_HOME_INSTANCE"})
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("instance_ban.put")
    )
    expires_at = future_expiry(payload.expires_at, code="INSTANCE_BAN_EXPIRY")
    reason = normalize_audit_reason(header_reason or payload.reason)

    # Serialize this mass membership mutation with other guild administration.
    # The actor must outrank every affected member; a bulk action must not be a
    # hierarchy bypass around the ordinary member-ban endpoint.
    if (guild.owner_id, guild.owner_domain) != (auth.user.id, auth.user.origin_domain):
        actor_role = await highest_role(session, guild, auth.user.id, auth.user.origin_domain)
        actor_rank = role_rank(actor_role)
        affected_member = await session.scalar(
            select(GuildMember.user_id)
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
                GuildMember.user_domain == domain,
            )
            .limit(1)
        )
        # Every member implicitly has @everyone. An actor whose highest role is
        # also @everyone may not use the bulk endpoint to bypass the ordinary
        # equal-rank member hierarchy rule.
        if affected_member is not None and actor_role.id == guild.id:
            raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})
        blocking_role = await session.scalar(
            select(Role.id)
            .join(
                MemberRole,
                (MemberRole.role_id == Role.id)
                & (MemberRole.role_domain == Role.origin_domain)
                & (MemberRole.guild_id == guild.id)
                & (MemberRole.guild_domain == guild.origin_domain),
            )
            .where(
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
                MemberRole.user_domain == domain,
                or_(
                    Role.position > actor_rank[0],
                    (Role.position == actor_rank[0]) & (Role.id <= -actor_rank[1]),
                ),
            )
            .limit(1)
        )
        if blocking_role is not None:
            raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})

    # Ensure the composite FK remains valid even for a first-contact domain.
    await session.execute(
        pg_insert(Instance)
        .values(domain=domain, is_self=False, federation_mode="open")
        .on_conflict_do_nothing(index_elements=["domain"])
    )
    await session.execute(
        pg_insert(GuildInstanceBan)
        .values(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            instance_domain=domain,
            reason=reason,
            actor_id=auth.user.id,
            actor_domain=auth.user.origin_domain,
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=["guild_id", "guild_domain", "instance_domain"],
            set_={
                "reason": reason,
                "actor_id": auth.user.id,
                "actor_domain": auth.user.origin_domain,
                "created_at": datetime.now(UTC),
                "expires_at": expires_at,
            },
        )
    )
    e2ee_policy_channels: list[Channel] = []
    revoked_installations = await revoke_installations_for_guild_instance(
        session,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        instance_domain=domain,
    )
    e2ee_policy_channels.extend(
        await revoke_bot_e2ee_access(
            session,
            redis,
            settings,
            installation_ids=(item.id for item in revoked_installations),
        )
    )
    deleted_role_refs = await cleanup_installation_roles(
        session,
        settings,
        guild,
        auth.user,
        revoked_installations,
    )
    removed_rows = list(
        (
            await session.execute(
                select(GuildMember.user_id, GuildMember.user_domain, User.account_type)
                .join(
                    User,
                    (User.id == GuildMember.user_id)
                    & (User.origin_domain == GuildMember.user_domain),
                )
                .where(
                    GuildMember.guild_id == guild.id,
                    GuildMember.guild_domain == guild.origin_domain,
                    GuildMember.user_domain == domain,
                )
            )
        ).all()
    )
    removed_refs = [(int(row[0]), str(row[1])) for row in removed_rows]
    removed_human = any(str(row[2]) != "bot" for row in removed_rows)
    removed_thread_members = await cleanup_guild_member_threads(
        session,
        settings,
        guild,
        auth.user,
        removed_refs,
    )
    await clear_tracker_assignees(
        session,
        settings,
        guild,
        auth.user,
        removed_refs,
    )
    await session.execute(
        delete(GuildMember).where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_domain == domain,
        )
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.members.origin.remove",
        {"origin_domain": domain},
        snapshot_required=True,
        e2ee_policy_channels=e2ee_policy_channels,
        pause_e2ee=removed_human,
    )
    await queue_guild_instance_access_revocation(
        session,
        settings,
        guild,
        instance_domain=domain,
        reason="instance_banned",
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        25,
        target_type="instance",
        target_ref={"domain": domain},
        reason=reason,
        changes=[
            {"key": "expires_at", "old_value": None, "new_value": str(expires_at)},
            {"key": "members_removed", "old_value": None, "new_value": len(removed_refs)},
            {
                "key": "bot_installations_revoked",
                "old_value": None,
                "new_value": len(revoked_installations),
            },
            {
                "key": "bot_roles_deleted",
                "old_value": None,
                "new_value": len(deleted_role_refs),
            },
        ],
    )
    await session.commit()
    await wake_tracker_membership_cleanup(guild)
    await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
    await publish_deleted_installation_roles(redis, guild, deleted_role_refs)
    await publish_guild_thread_member_cleanup(redis, guild, removed_thread_members)
    for user_id, user_domain in removed_refs:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_REMOVE",
            {
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "user_id": str(user_id),
                "user_domain": user_domain,
            },
        )
    return Response(status_code=204)


@router.delete("/{guild_id}/instance-bans/{instance_domain}", status_code=204)
async def remove_instance_ban(
    guild_id: EntityRef,
    instance_domain: str,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "instance_ban.remove",
        {"instance_domain": instance_domain, "reason": reason},
    )
    if proxied is not None:
        require_guild_management_status(proxied, 204)
        return Response(status_code=204)

    guild = await local_guild(session, settings, guild_id, for_update=True)
    domain = normalize_domain(instance_domain)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("instance_ban.remove")
    )
    removed = await session.scalar(
        delete(GuildInstanceBan)
        .where(
            GuildInstanceBan.guild_id == guild.id,
            GuildInstanceBan.guild_domain == guild.origin_domain,
            GuildInstanceBan.instance_domain == domain,
        )
        .returning(GuildInstanceBan.instance_domain)
    )
    if removed is not None:
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            26,
            target_type="instance",
            target_ref={"domain": domain},
            reason=normalize_audit_reason(reason),
        )
        await session.commit()
    return Response(status_code=204)


def require_one_audit_log_cursor(before: int | None, after: int | None) -> None:
    if before is not None and after is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "AUDIT_LOG_CURSOR_CONFLICT",
                "message": "Choose either a before cursor or an after cursor, not both.",
            },
        )


async def query_audit_log_entries(
    session: AsyncSession,
    guild: Guild,
    *,
    limit: int,
    before: int | None,
    after: int | None,
    actor_ref: tuple[int, str] | None,
    action_type: int | None,
    target_type: str | None,
) -> list[AuditLogEntryPayload]:
    """Apply one canonical audit query at the guild's authoritative database."""

    conditions = [
        AuditLogEntry.guild_id == guild.id,
        AuditLogEntry.guild_domain == guild.origin_domain,
    ]
    if before is not None:
        conditions.append(AuditLogEntry.id < before)
    if after is not None:
        conditions.append(AuditLogEntry.id > after)
    if actor_ref is not None:
        actor_id, actor_domain = actor_ref
        conditions.extend(
            [
                AuditLogEntry.actor_id == actor_id,
                AuditLogEntry.actor_domain == actor_domain,
            ]
        )
    if action_type is not None:
        conditions.append(AuditLogEntry.action_type == action_type)
    if target_type is not None:
        conditions.append(AuditLogEntry.target_type == target_type)
    order = AuditLogEntry.id.asc() if after is not None else AuditLogEntry.id.desc()
    entries = await session.scalars(
        select(AuditLogEntry).where(*conditions).order_by(order).limit(limit)
    )
    return [audit_log_payload(entry) for entry in entries]


async def filter_audit_log_entries_for_actor(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    entries: list[AuditLogEntryPayload],
) -> list[AuditLogEntryPayload]:
    """Apply a bot installation's channel boundary without changing human logs."""

    grant = await bot_guild_permission_grant(session, guild, actor)
    if grant is None:
        return entries
    return await filter_restricted_bot_audit_entries(session, guild, grant, entries)


def require_fresh_audit_log_request(
    request: AuditLogFederationRequest,
    *,
    now: int,
    clock_skew_seconds: int,
) -> None:
    """Reject expired or implausibly future application-level RPC grants."""

    if request.issued_at > now + clock_skew_seconds or request.deadline <= now:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "KAED_FED_AUDIT_LOG_REQUEST_EXPIRED",
                "message": "The audit log authorization request has expired.",
            },
        )


def validate_audit_log_federation_page(
    page: AuditLogFederationPage,
    request: AuditLogFederationRequest,
) -> list[AuditLogEntryPayload]:
    """Verify request binding, ordering, cursor bounds, and every echoed filter."""

    if page.request.model_dump(mode="json") != request.model_dump(mode="json"):
        raise ValueError("audit log response is bound to a different request")
    if len(page.entries) > request.query.limit:
        raise ValueError("audit log response exceeds the requested page limit")

    before = int(request.query.before) if request.query.before is not None else None
    after = int(request.query.after) if request.query.after is not None else None
    expected_user = request.query.user
    seen: set[int] = set()
    previous: int | None = None
    for entry in page.entries:
        entry_id = validate_snowflake(entry.id)
        actor_id = validate_snowflake(entry.actor_id)
        if entry_id in seen:
            raise ValueError("audit log response contains a duplicate entry")
        seen.add(entry_id)
        if entry.guild_id != request.guild_id or entry.guild_domain != request.guild_domain:
            raise ValueError("audit log response contains an entry for another guild")
        if normalize_domain(entry.actor_domain) != entry.actor_domain:
            raise ValueError("audit log response contains an invalid actor domain")
        if before is not None and entry_id >= before:
            raise ValueError("audit log response violates its before cursor")
        if after is not None and entry_id <= after:
            raise ValueError("audit log response violates its after cursor")
        if previous is not None and (
            (after is not None and entry_id <= previous) or (after is None and entry_id >= previous)
        ):
            raise ValueError("audit log response has invalid entry ordering")
        previous = entry_id
        if expected_user is not None and (
            actor_id != int(expected_user.id) or entry.actor_domain != expected_user.domain
        ):
            raise ValueError("audit log response violates its moderator filter")
        if request.query.action_type is not None and entry.action_type != request.query.action_type:
            raise ValueError("audit log response violates its action filter")
        if request.query.target_type is not None and entry.target_type != request.query.target_type:
            raise ValueError("audit log response violates its target filter")
    return page.entries


async def request_remote_audit_log_page(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    requester: User,
    *,
    limit: int,
    before: int | None,
    after: int | None,
    actor_ref: tuple[int, str] | None,
    action_type: int | None,
    target_type: str | None,
) -> list[AuditLogEntryPayload]:
    """Fetch and verify one private page directly from the guild authority."""

    issued_at = int(time.time())
    request = AuditLogFederationRequest(
        guild_id=str(guild.id),
        guild_domain=guild.origin_domain,
        requester={"id": str(requester.id), "domain": requester.origin_domain},
        requesting_instance=settings.domain,
        request_id=f"kalr_{secrets.token_urlsafe(24)}",
        issued_at=issued_at,
        deadline=issued_at + AUDIT_LOG_FEDERATION_DEADLINE_SECONDS,
        query=AuditLogFederationQuery(
            limit=limit,
            before=str(before) if before is not None else None,
            after=str(after) if after is not None else None,
            user=(
                {"id": str(actor_ref[0]), "domain": actor_ref[1]} if actor_ref is not None else None
            ),
            action_type=action_type,
            target_type=target_type,
        ),
    )
    try:
        upstream = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/audit-logs",
            payload=request.model_dump(mode="json"),
            request_timeout=AUDIT_LOG_FEDERATION_DEADLINE_SECONDS,
            max_response_bytes=AUDIT_LOG_FEDERATION_MAX_RESPONSE_BYTES,
        )
    except (FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "FEDERATED_AUDIT_LOG_UNAVAILABLE",
                "message": "The guild home could not provide its audit log. Try again shortly.",
            },
        ) from None

    if upstream.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MISSING_PERMISSIONS",
                "message": "You do not have permission to view this guild's audit log.",
                "permissions": str(int(required_permissions("guild.audit.list"))),
            },
        )
    if upstream.status_code == 404:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    if upstream.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "FEDERATED_AUDIT_LOG_UNAVAILABLE",
                "message": "The guild home could not provide its audit log. Try again shortly.",
            },
        )

    try:
        raw = decode_federation_response_json(
            upstream,
            max_response_bytes=AUDIT_LOG_FEDERATION_MAX_RESPONSE_BYTES,
        )
        envelope = await validated_event_envelope(
            session,
            settings,
            guild.origin_domain,
            raw,
        )
        if envelope.type != AUDIT_LOG_FEDERATION_EVENT_TYPE:
            raise ValueError("audit log response has the wrong signed event type")
        if envelope.context != {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
        }:
            raise ValueError("audit log response has the wrong guild context")
        response_timestamp_floor = (
            request.issued_at - settings.federation_clock_skew_seconds
        ) * 1_000
        response_timestamp_ceiling = (
            request.deadline + settings.federation_clock_skew_seconds
        ) * 1_000
        if not response_timestamp_floor <= envelope.ts < response_timestamp_ceiling:
            raise ValueError("audit log response was signed outside its request window")
        if int(time.time()) >= request.deadline:
            raise ValueError("audit log response arrived after its request deadline")
        page = AuditLogFederationPage.model_validate(envelope.content)
        return validate_audit_log_federation_page(page, request)
    except (FederationNetworkError, ValidationError, TypeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "FEDERATED_AUDIT_LOG_RESPONSE_INVALID",
                "message": "The guild home returned an invalid audit log response.",
            },
        ) from None


@router.get("/{guild_id}/audit-logs", response_model_exclude_unset=True)
async def list_audit_logs(
    guild_id: EntityRef,
    limit: int = Query(default=50, ge=1, le=100),
    before: Snowflake | None = None,
    after: Snowflake | None = None,
    user_id: EntityRef | None = None,
    action_type: int | None = Query(default=None, ge=0, le=2_147_483_647),
    target_type: str | None = Query(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$"
    ),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[AuditLogEntryPayload]:
    require_one_audit_log_cursor(before, after)
    guild_id_value, guild_domain = guild_id.resolve(settings.domain)
    actor_ref = user_id.resolve(settings.domain) if user_id is not None else None
    if guild_domain == settings.domain:
        guild = await local_guild(session, settings, guild_id)
        await require_permissions(
            session, redis, guild, auth.user, required_permissions("guild.audit.list")
        )
        entries = await query_audit_log_entries(
            session,
            guild,
            limit=limit,
            before=before,
            after=after,
            actor_ref=actor_ref,
            action_type=action_type,
            target_type=target_type,
        )
        return await filter_audit_log_entries_for_actor(
            session,
            guild,
            auth.user,
            entries,
        )

    remote_guild = await session.get(Guild, (guild_id_value, guild_domain))
    member = await session.get(
        GuildMember,
        (guild_id_value, guild_domain, auth.user.id, auth.user.origin_domain),
    )
    if remote_guild is None or remote_guild.unavailable or member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return await request_remote_audit_log_page(
        session,
        settings,
        remote_guild,
        auth.user,
        limit=limit,
        before=before,
        after=after,
        actor_ref=actor_ref,
        action_type=action_type,
        target_type=target_type,
    )


@federation_router.post("/_kaede/v1/guilds/{guild_id}/audit-logs")
async def federation_guild_audit_logs(
    guild_id: Snowflake,
    payload: AuditLogFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Authorize and sign one non-replicated audit page at the guild home."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-audit-log",
        capacity=120,
        refill_per_minute=120,
    )
    if (
        payload.requesting_instance != principal.origin
        or payload.requester.domain != principal.origin
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_AUDIT_LOG_REQUESTER_MISMATCH"},
        )
    if int(payload.guild_id) != guild_id or payload.guild_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    now = int(time.time())
    require_fresh_audit_log_request(
        payload,
        now=now,
        clock_skew_seconds=settings.federation_clock_skew_seconds,
    )
    accepted = await redis.set(
        f"federation:audit-log-request:{principal.origin}:{payload.request_id}",
        "1",
        ex=settings.federation_clock_skew_seconds + AUDIT_LOG_FEDERATION_DEADLINE_SECONDS,
        nx=True,
    )
    if not accepted:
        raise HTTPException(
            status_code=409,
            detail={"code": "KAED_FED_AUDIT_LOG_REQUEST_REPLAYED"},
        )

    guild = await session.get(Guild, (guild_id, settings.domain))
    requester = await session.get(
        User,
        (int(payload.requester.id), payload.requester.domain),
    )
    if guild is None or guild.unavailable or requester is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await require_permissions(
        session,
        redis,
        guild,
        requester,
        required_permissions("guild.audit.list"),
    )
    entries = await query_audit_log_entries(
        session,
        guild,
        limit=payload.query.limit,
        before=(int(payload.query.before) if payload.query.before is not None else None),
        after=(int(payload.query.after) if payload.query.after is not None else None),
        actor_ref=(
            (int(payload.query.user.id), payload.query.user.domain)
            if payload.query.user is not None
            else None
        ),
        action_type=payload.query.action_type,
        target_type=payload.query.target_type,
    )
    entries = await filter_audit_log_entries_for_actor(
        session,
        guild,
        requester,
        entries,
    )
    try:
        owner = await guild_authority_owner(session, settings, guild)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "KAED_FED_AUDIT_LOG_SIGNER_UNAVAILABLE"},
        ) from exc
    page = AuditLogFederationPage(request=payload, entries=entries)
    return await build_guild_authority_envelope(
        session,
        settings,
        guild,
        AUDIT_LOG_FEDERATION_EVENT_TYPE,
        owner,
        page.model_dump(mode="json"),
        context={"guild_id": str(guild.id), "guild_domain": guild.origin_domain},
    )
