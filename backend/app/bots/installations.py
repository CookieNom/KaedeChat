from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import and_, delete, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import queue_guild_mutation
from app.chat.postcommit import queue_postcommit_dispatch
from app.core.permissions import ALL_PERMISSIONS, Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.bot_models import (
    BotApplication,
    BotDMCapability,
    BotInstallation,
    BotUserInstallation,
)
from app.db.models import Channel, ChannelOverwrite, Guild, GuildMember, MemberRole, Role, User


def effective_installation_permissions(
    granted_permissions: int,
    live_permissions: int,
) -> int:
    """Intersect an installation ceiling with the bot member's live authority."""

    ceiling = (
        ALL_PERMISSIONS if granted_permissions & Permission.ADMINISTRATOR else granted_permissions
    )
    return int(ceiling & live_permissions)


def installation_grants_permissions(
    granted_permissions: int,
    required_permissions: int | Permission,
) -> bool:
    """Return whether an installation ceiling contains one complete mask."""

    required = Permission(required_permissions)
    return (
        Permission(effective_installation_permissions(granted_permissions, int(required)))
        & required
        == required
    )


def normalize_channel_restrictions(restrictions: Iterable[object]) -> tuple[str, ...]:
    """Return the canonical ordered set used by authorization and cache keys."""

    return tuple(sorted({str(item).strip() for item in restrictions if str(item).strip()}))


def qualified_channel_restrictions(
    restrictions: Iterable[object],
    *,
    authority_domain: str,
) -> tuple[str, ...]:
    """Render one canonical target-owned restriction set on the wire."""

    refs: set[tuple[int, str]] = set()
    for item in normalize_channel_restrictions(restrictions):
        channel_id, channel_domain = EntityRef(item).resolve(authority_domain)
        if channel_domain != authority_domain:
            raise ValueError("channel restriction belongs to a different authority")
        refs.add((channel_id, channel_domain))
    return tuple(f"{channel_id}@{channel_domain}" for channel_id, channel_domain in sorted(refs))


def _restriction_matches(
    restrictions: set[str],
    channel_id: int,
    channel_domain: str,
) -> bool:
    return bool(
        restrictions.intersection(
            {
                str(channel_id),
                f"{channel_id}@{channel_domain}",
            }
        )
    )


async def channel_restrictions_allow(
    session: AsyncSession,
    restrictions: Iterable[object],
    channel: Channel,
) -> bool:
    """Apply an installation restriction to one validated Discord hierarchy.

    Restrictions may name the channel itself, its immediate parent, or, for a
    thread/post, the category containing its parent channel. Parent traversal
    is deliberately bounded to those two Discord-defined edges, and every
    loaded ancestor must be a current channel in the exact same guild.
    """

    normalized = set(normalize_channel_restrictions(restrictions))
    if not normalized:
        return True
    if _restriction_matches(normalized, channel.id, channel.origin_domain):
        return True
    if channel.parent_id is None or channel.parent_domain is None:
        return False
    parent = await session.get(
        Channel,
        (channel.parent_id, channel.parent_domain),
        populate_existing=True,
    )
    is_thread = channel.type in {10, 11, 12}
    if (
        parent is None
        or parent.unavailable
        or (parent.guild_id, parent.guild_domain) != (channel.guild_id, channel.guild_domain)
        or parent.type not in ({0, 5, 15} if is_thread else {4})
    ):
        return False
    if _restriction_matches(normalized, parent.id, parent.origin_domain):
        return True
    if not is_thread or parent.parent_id is None or parent.parent_domain is None:
        return False
    category = await session.get(
        Channel,
        (parent.parent_id, parent.parent_domain),
        populate_existing=True,
    )
    return bool(
        category is not None
        and not category.unavailable
        and category.type == 4
        and (category.guild_id, category.guild_domain) == (channel.guild_id, channel.guild_domain)
        and _restriction_matches(normalized, category.id, category.origin_domain)
    )


async def installation_allows_channel(
    session: AsyncSession,
    installation: BotInstallation,
    channel: Channel,
) -> bool:
    """Apply an installation's exact-guild, ancestor-aware channel ceiling."""

    return bool(
        not channel.unavailable
        and (channel.guild_id, channel.guild_domain)
        == (installation.guild_id, installation.guild_domain)
        and await channel_restrictions_allow(
            session,
            installation.channel_restrictions or [],
            channel,
        )
    )


async def installation_accessible_channel(
    session: AsyncSession,
    installation: BotInstallation,
    guild: Guild,
    channel_ref: EntityRef,
) -> Channel | None:
    """Resolve one exact target-guild channel inside an installation ceiling.

    Keeping the composite guild/authority and parent-aware restriction checks in
    one helper prevents resource wrappers that start from guild scope from
    accidentally treating a numeric channel ID as sufficient authority.
    """

    if (installation.guild_id, installation.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        return None
    try:
        channel_id, channel_domain = channel_ref.resolve(guild.origin_domain)
    except ValueError:
        return None
    channel = await session.get(Channel, (channel_id, channel_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.origin_domain != guild.origin_domain
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        or not await installation_allows_channel(session, installation, channel)
    ):
        return None
    return channel


def installation_has_membership() -> ColumnElement[bool]:
    """Correlate an installation row with its current bot membership.

    ``BotInstallation.status`` is intentionally not sufficient authority on
    its own.  Membership deletion and installation revocation happen in the
    same transaction, but this predicate keeps every authorization path
    fail-closed if old data, a failed migration, or an interrupted write ever
    leaves a dangling active installation behind.
    """

    return exists(
        select(GuildMember.user_id).where(
            GuildMember.guild_id == BotInstallation.guild_id,
            GuildMember.guild_domain == BotInstallation.guild_domain,
            GuildMember.user_id == BotInstallation.bot_user_id,
            GuildMember.user_domain == BotInstallation.bot_user_domain,
        )
    )


def usable_guild_installation() -> ColumnElement[bool]:
    """Require coherent active, unrevoked guild-install authority."""

    return and_(
        BotInstallation.status == "active",
        BotInstallation.revoked_at.is_(None),
        installation_has_membership(),
    )


def bot_actor_active_installations_statement(
    guild: Guild,
    actor: User,
) -> Select[tuple[BotInstallation]]:
    """Select active runtime-ready installs for one exact guild bot identity."""

    # Kept lazy because runtime_control imports installation lifecycle helpers.
    from app.bots.runtime_control import application_runtime_projection_exists

    return (
        select(BotInstallation)
        .join(
            BotApplication,
            (BotApplication.id == BotInstallation.application_id)
            & (BotApplication.origin_domain == BotInstallation.application_domain),
        )
        .where(
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.bot_user_id == actor.id,
            BotInstallation.bot_user_domain == actor.origin_domain,
            usable_guild_installation(),
            BotApplication.status == "active",
            BotApplication.bot_user_id == actor.id,
            BotApplication.bot_user_domain == actor.origin_domain,
            or_(
                BotApplication.origin_domain == guild.origin_domain,
                application_runtime_projection_exists(guild.origin_domain),
            ),
        )
    )


def usable_user_installation(
    *,
    current_instance_domain: str,
    at: datetime | ColumnElement[Any] | None = None,
) -> ColumnElement[bool]:
    """Fence foreign user-install mirrors behind their signed authority lease.

    A user installation is owned by the installing user's home.  That row does
    not need a lease on its own authority, while every materialized foreign
    mirror fails closed when its short signed lease is absent or expired.
    """

    current = func.now() if at is None else at
    return and_(
        BotUserInstallation.status == "active",
        BotUserInstallation.revoked_at.is_(None),
        or_(
            BotUserInstallation.user_domain == current_instance_domain,
            and_(
                BotUserInstallation.authority_expires_at.is_not(None),
                BotUserInstallation.authority_expires_at > current,
            ),
        ),
    )


def user_installation_is_usable(
    installation: BotUserInstallation,
    *,
    current_instance_domain: str,
    at: datetime | None = None,
) -> bool:
    """In-memory equivalent of :func:`usable_user_installation`."""

    if installation.status != "active" or installation.revoked_at is not None:
        return False
    if installation.user_domain == current_instance_domain:
        return True
    expires_at = installation.authority_expires_at
    if expires_at is None or expires_at.tzinfo is None:
        return False
    return expires_at > (at or datetime.now(UTC))


def active_standard_installation_exists(
    *,
    application_id: Any,
    application_domain: Any,
    bot_user_id: Any,
    bot_user_domain: Any,
    current_instance_domain: str,
) -> ColumnElement[bool]:
    """Return an existence fence for a usable guild or user installation."""

    guild_installation = exists(
        select(BotInstallation.id).where(
            BotInstallation.application_id == application_id,
            BotInstallation.application_domain == application_domain,
            BotInstallation.bot_user_id == bot_user_id,
            BotInstallation.bot_user_domain == bot_user_domain,
            usable_guild_installation(),
        )
    )
    user_installation = exists(
        select(BotUserInstallation.id).where(
            BotUserInstallation.application_id == application_id,
            BotUserInstallation.application_domain == application_domain,
            usable_user_installation(current_instance_domain=current_instance_domain),
        )
    )
    return or_(guild_installation, user_installation)


def active_installation_exists(
    *,
    application_id: Any,
    application_domain: Any,
    bot_user_id: Any,
    bot_user_domain: Any,
    current_instance_domain: str,
) -> ColumnElement[bool]:
    """Return a runtime fence for a standard install or exact DM capability."""

    # Local import avoids the dm_capability -> runtime_control -> installations cycle.
    from app.bots.dm_capability import usable_dm_capability

    dm_capability = exists(
        select(BotDMCapability.id).where(
            BotDMCapability.application_id == application_id,
            BotDMCapability.application_domain == application_domain,
            BotDMCapability.bot_user_id == bot_user_id,
            BotDMCapability.bot_user_domain == bot_user_domain,
            BotDMCapability.conversation_id.is_not(None),
            usable_dm_capability(at=datetime.now(UTC)),
        )
    )
    return or_(
        active_standard_installation_exists(
            application_id=application_id,
            application_domain=application_domain,
            bot_user_id=bot_user_id,
            bot_user_domain=bot_user_domain,
            current_instance_domain=current_instance_domain,
        ),
        dm_capability,
    )


def installation_gateway_payload(installation: BotInstallation) -> dict[str, object]:
    return {
        "id": str(installation.id),
        "application_ref": (f"{installation.application_id}@{installation.application_domain}"),
        "application_id": str(installation.application_id),
        "application_domain": installation.application_domain,
        "guild_id": str(installation.guild_id),
        "guild_domain": installation.guild_domain,
        "bot_user_ref": f"{installation.bot_user_id}@{installation.bot_user_domain}",
        "scopes": list(installation.granted_scopes),
        "intents": list(installation.granted_intents),
        "granted_permissions": str(installation.granted_permissions),
        "channel_restrictions": list(
            qualified_channel_restrictions(
                installation.channel_restrictions or [],
                authority_domain=installation.guild_domain,
            )
        ),
        "grant_revision": str(installation.grant_revision),
        "status": installation.status,
    }


def queue_installation_gateway_events(
    session: AsyncSession,
    installation: BotInstallation,
    operation: str,
) -> None:
    """Queue Discord integration and Kaede installation projections atomically."""

    if operation not in {"CREATE", "UPDATE", "DELETE"}:
        raise ValueError("installation gateway operation is invalid")
    topic = guild_topic(installation.guild_domain, installation.guild_id)
    payload = installation_gateway_payload(installation)
    queue_postcommit_dispatch(session, topic, f"INTEGRATION_{operation}", payload)
    queue_postcommit_dispatch(session, topic, f"BOT_INSTALLATION_{operation}", payload)
    queue_postcommit_dispatch(
        session,
        topic,
        "GUILD_INTEGRATIONS_UPDATE",
        {
            "guild_id": str(installation.guild_id),
            "guild_domain": installation.guild_domain,
        },
    )
    permission_entries: list[dict[str, object]] = []
    if installation.role_id is not None and installation.role_domain is not None:
        permission_entries.append(
            {
                "id": str(installation.role_id),
                "origin_domain": installation.role_domain,
                "type": "role",
                "permission": operation != "DELETE",
            }
        )
    queue_postcommit_dispatch(
        session,
        topic,
        "APPLICATION_COMMAND_PERMISSIONS_UPDATE",
        {
            "application_ref": (f"{installation.application_id}@{installation.application_domain}"),
            "application_id": str(installation.application_id),
            "application_domain": installation.application_domain,
            "guild_id": str(installation.guild_id),
            "guild_domain": installation.guild_domain,
            "permissions": permission_entries,
            "granted_permissions": str(installation.granted_permissions),
            "grant_revision": str(installation.grant_revision),
        },
        audience_user_refs=(f"{installation.bot_user_id}@{installation.bot_user_domain}",),
    )


async def set_application_installations_enabled(
    session: AsyncSession,
    application: BotApplication,
    *,
    enabled: bool,
    rotate_active_grants: bool = False,
) -> list[BotInstallation]:
    """Apply one reversible effective-runtime state to local guild grants.

    Only rows without an independent terminal revocation participate. Guild
    integration projections are emitted here so home-instance moderation and
    signed remote runtime snapshots cannot drift in behavior.
    """

    current_statuses = ["suspended" if enabled else "active"]
    if enabled and rotate_active_grants:
        current_statuses.append("active")
    resulting_status = "active" if enabled else "suspended"
    guild_installations = list(
        await session.scalars(
            select(BotInstallation)
            .where(
                BotInstallation.application_id == application.id,
                BotInstallation.application_domain == application.origin_domain,
                BotInstallation.status.in_(current_statuses),
                BotInstallation.revoked_at.is_(None),
            )
            .with_for_update()
        )
    )
    for installation in guild_installations:
        installation.status = resulting_status
        installation.grant_revision += 1
        queue_installation_gateway_events(session, installation, "UPDATE")
    # User-install grants are owned by the installing user's home, which can be
    # a third authority. Application runtime status is already enforced through
    # the application/target projection and must never rewrite that grant's
    # revision or source-owned state on a runtime target.
    return guild_installations


async def transition_application_installations(
    session: AsyncSession,
    application: BotApplication,
    *,
    previous_status: str,
    next_status: Literal["active", "suspended"],
) -> list[BotInstallation]:
    """Converge grants for a reversible application-state transition."""

    if next_status == "suspended" and previous_status != "suspended":
        return await set_application_installations_enabled(session, application, enabled=False)
    if previous_status == "suspended" and next_status == "active":
        return await set_application_installations_enabled(session, application, enabled=True)
    return []


def _revoke(session: AsyncSession, installations: list[BotInstallation]) -> list[BotInstallation]:
    revoked_at = datetime.now(UTC)
    for installation in installations:
        installation.status = "revoked"
        installation.revoked_at = revoked_at
        installation.grant_revision += 1
        queue_installation_gateway_events(session, installation, "DELETE")
    return installations


async def cleanup_installation_roles(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    installations: list[BotInstallation],
) -> list[tuple[int, str]]:
    """Delete roles owned by revoked installations in the caller's transaction.

    Installation rows and the authoritative guild row are already locked by
    the caller, preserving the guild -> installation -> role lock order used
    by install/uninstall paths.  A corrupt role shared with another active
    installation or any non-target member grant is deliberately retained,
    while the target installations' references and obsolete bot grants are
    still cleared. Normal installation roles are unique and are removed
    together with their overwrites and federated role-delete event.
    """

    installation_ids = [installation.id for installation in installations]
    role_refs = {
        (installation.role_id, installation.role_domain)
        for installation in installations
        if installation.role_id is not None and installation.role_domain is not None
    }
    installation_role_grants = {
        (
            installation.bot_user_id,
            installation.bot_user_domain,
            installation.role_id,
            installation.role_domain,
        )
        for installation in installations
        if installation.role_id is not None and installation.role_domain is not None
    }
    for installation in installations:
        installation.role_id = None
        installation.role_domain = None
    if not role_refs:
        return []

    roles = list(
        await session.scalars(
            select(Role)
            .where(
                tuple_(Role.id, Role.origin_domain).in_(role_refs),
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
                Role.id != guild.id,
            )
            .order_by(Role.id, Role.origin_domain)
            .with_for_update()
        )
    )
    if not roles:
        return []

    loaded_refs = {(role.id, role.origin_domain) for role in roles}
    active_installation_grants = {
        (bot_user_id, bot_user_domain, role_id, role_domain)
        for bot_user_id, bot_user_domain, role_id, role_domain in await session.execute(
            select(
                BotInstallation.bot_user_id,
                BotInstallation.bot_user_domain,
                BotInstallation.role_id,
                BotInstallation.role_domain,
            ).where(
                tuple_(BotInstallation.role_id, BotInstallation.role_domain).in_(loaded_refs),
                BotInstallation.id.not_in(installation_ids),
                BotInstallation.status == "active",
                BotInstallation.revoked_at.is_(None),
            )
        )
        if role_id is not None and role_domain is not None
    }
    shared_refs = {(grant[2], grant[3]) for grant in active_installation_grants}
    removed_member_role_refs = {
        grant
        for grant in installation_role_grants
        if (grant[2], grant[3]) in loaded_refs and grant not in active_installation_grants
    }
    if removed_member_role_refs:
        await session.execute(
            delete(MemberRole).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                tuple_(
                    MemberRole.user_id,
                    MemberRole.user_domain,
                    MemberRole.role_id,
                    MemberRole.role_domain,
                ).in_(removed_member_role_refs),
            )
        )
    shared_refs.update(
        {
            (role_id, role_domain)
            for role_id, role_domain in await session.execute(
                select(MemberRole.role_id, MemberRole.role_domain)
                .where(
                    MemberRole.guild_id == guild.id,
                    MemberRole.guild_domain == guild.origin_domain,
                    tuple_(MemberRole.role_id, MemberRole.role_domain).in_(loaded_refs),
                )
                .distinct()
            )
        }
    )
    retained_removed_grants = sorted(
        grant for grant in removed_member_role_refs if (grant[2], grant[3]) in shared_refs
    )
    if retained_removed_grants:
        member_refs = {(grant[0], grant[1]) for grant in retained_removed_grants}
        locked_members = list(
            await session.scalars(
                select(GuildMember)
                .where(
                    GuildMember.guild_id == guild.id,
                    GuildMember.guild_domain == guild.origin_domain,
                    tuple_(GuildMember.user_id, GuildMember.user_domain).in_(member_refs),
                )
                .order_by(GuildMember.user_id, GuildMember.user_domain)
                .with_for_update()
            )
        )
        members_by_ref = {(member.user_id, member.user_domain): member for member in locked_members}
        if set(members_by_ref) != member_refs:
            raise RuntimeError("installation role grant exists without guild membership")
        for user_id, user_domain, role_id, role_domain in retained_removed_grants:
            member = members_by_ref[(user_id, user_domain)]
            member.member_version += 1
            await queue_guild_mutation(
                session,
                settings,
                guild,
                actor,
                "guild.member.role.remove",
                {
                    "user": {"id": str(user_id), "origin_domain": user_domain},
                    "role": {"id": str(role_id), "origin_domain": role_domain},
                    "member_version": str(member.member_version),
                },
                snapshot_required=True,
            )
    roles = [role for role in roles if (role.id, role.origin_domain) not in shared_refs]
    if not roles:
        return []

    guild.permission_generation += 1
    deleted_refs: list[tuple[int, str]] = []
    for role in roles:
        role_ref = (role.id, role.origin_domain)
        await queue_guild_mutation(
            session,
            settings,
            guild,
            actor,
            "guild.role.delete",
            {"role": {"id": str(role.id), "origin_domain": role.origin_domain}},
            snapshot_required=True,
        )
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.guild_id == guild.id,
                ChannelOverwrite.guild_domain == guild.origin_domain,
                ChannelOverwrite.target_id == role.id,
                ChannelOverwrite.target_domain == role.origin_domain,
                ChannelOverwrite.target_type == "role",
            )
        )
        await session.delete(role)
        deleted_refs.append(role_ref)
    return deleted_refs


async def publish_deleted_installation_roles(
    redis: Redis,
    guild: Guild,
    role_refs: list[tuple[int, str]],
) -> None:
    """Publish the best-effort projection after role cleanup commits."""

    for role_id, role_domain in role_refs:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_ROLE_DELETE",
            {
                "id": str(role_id),
                "origin_domain": role_domain,
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
            },
        )


async def revoke_installations_for_guild_member(
    session: AsyncSession,
    *,
    guild_id: int,
    guild_domain: str,
    user_id: int,
    user_domain: str,
) -> list[BotInstallation]:
    """Revoke non-revoked installations represented by a removed guild member.

    Installation rows are locked and changed inside the caller's membership
    transaction. Unbanning a bot deliberately does not reactivate them: a
    guild administrator must explicitly reinstall the application so its
    scopes, intents, permissions, and consent are reviewed again.
    """

    installations = list(
        await session.scalars(
            select(BotInstallation)
            .where(
                BotInstallation.guild_id == guild_id,
                BotInstallation.guild_domain == guild_domain,
                BotInstallation.bot_user_id == user_id,
                BotInstallation.bot_user_domain == user_domain,
                BotInstallation.status != "revoked",
            )
            .order_by(BotInstallation.id)
            .with_for_update()
        )
    )
    return _revoke(session, installations)


async def revoke_installations_for_guild_instance(
    session: AsyncSession,
    *,
    guild_id: int,
    guild_domain: str,
    instance_domain: str,
) -> list[BotInstallation]:
    """Revoke every non-revoked installation removed by an instance-wide ban.

    The authoritative guild row is already locked by the caller.  Locking the
    matching installations before deleting memberships makes the revocations,
    member deletion, instance-ban row, and federation mutation one atomic
    guild transaction.
    """

    installations = list(
        await session.scalars(
            select(BotInstallation)
            .where(
                BotInstallation.guild_id == guild_id,
                BotInstallation.guild_domain == guild_domain,
                BotInstallation.bot_user_domain == instance_domain,
                BotInstallation.status != "revoked",
            )
            .order_by(BotInstallation.id)
            .with_for_update()
        )
    )
    return _revoke(session, installations)
