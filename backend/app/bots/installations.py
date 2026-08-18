from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, exists, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import queue_guild_mutation
from app.core.settings import Settings
from app.db.bot_models import BotInstallation
from app.db.models import ChannelOverwrite, Guild, GuildMember, MemberRole, Role, User


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


def active_installation_exists(
    *,
    application_id: Any,
    application_domain: Any,
    bot_user_id: Any,
    bot_user_domain: Any,
) -> ColumnElement[bool]:
    """Return an existence fence for at least one usable installation."""

    return exists(
        select(BotInstallation.id).where(
            BotInstallation.application_id == application_id,
            BotInstallation.application_domain == application_domain,
            BotInstallation.bot_user_id == bot_user_id,
            BotInstallation.bot_user_domain == bot_user_domain,
            BotInstallation.status == "active",
            installation_has_membership(),
        )
    )


def _revoke(installations: list[BotInstallation]) -> list[BotInstallation]:
    revoked_at = datetime.now(UTC)
    for installation in installations:
        installation.status = "revoked"
        installation.revoked_at = revoked_at
        installation.grant_revision += 1
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
    return _revoke(installations)


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
    return _revoke(installations)
