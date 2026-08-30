from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.command_contract import command_permission_mask
from app.core.model_validation import UnambiguousInputModel
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.bot_models import ApplicationCommandPermission
from app.db.models import Channel, Guild, MemberRole, User


@dataclass(frozen=True, slots=True)
class CommandPermissionSubject:
    user_ref: tuple[int, str]
    role_refs: frozenset[tuple[int, str]]
    channel_ref: tuple[int, str] | None


class CommandPermissionEntry(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    id: EntityRef
    type: Literal["role", "user", "channel"]
    permission: bool


class CommandPermissionsPut(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    permissions: list[CommandPermissionEntry] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_targets(self) -> CommandPermissionsPut:
        keys = [(entry.type, str(entry.id)) for entry in self.permissions]
        if len(keys) != len(set(keys)):
            raise ValueError("command permission targets must be unique")
        return self


def permission_subject(
    guild: Guild,
    user: User,
    role_refs: Iterable[tuple[int, str]],
    channel: Channel | None,
) -> CommandPermissionSubject:
    """Build the exact subject used by Discord-style command overwrites.

    Threads inherit the parent channel's command permission entry.  The
    everyone role and all-channels constants are resolved during evaluation so
    callers cannot accidentally omit them.
    """

    channel_ref: tuple[int, str] | None = None
    if channel is not None:
        if channel.type in {10, 11, 12}:
            if channel.parent_id is not None and channel.parent_domain is not None:
                channel_ref = (channel.parent_id, channel.parent_domain)
        else:
            channel_ref = (channel.id, channel.origin_domain)
    return CommandPermissionSubject(
        user_ref=(user.id, user.origin_domain),
        role_refs=frozenset({(guild.id, guild.origin_domain), *role_refs}),
        channel_ref=channel_ref,
    )


def _most_specific_subject_decision(
    rows: Sequence[ApplicationCommandPermission],
    subject: CommandPermissionSubject,
    guild: Guild,
) -> bool | None:
    user_matches = [
        row
        for row in rows
        if row.target_type == "user" and (row.target_id, row.target_domain) == subject.user_ref
    ]
    if user_matches:
        return user_matches[0].permission

    everyone_ref = (guild.id, guild.origin_domain)
    assigned_role_matches = [
        row
        for row in rows
        if row.target_type == "role"
        and (row.target_id, row.target_domain) in subject.role_refs
        and (row.target_id, row.target_domain) != everyone_ref
    ]
    if assigned_role_matches:
        # Role grants compose like ordinary guild permissions: one applicable
        # role grant is sufficient. A deny matters only when no assigned role
        # explicitly grants the command.
        return any(row.permission for row in assigned_role_matches)

    everyone = next(
        (
            row
            for row in rows
            if row.target_type == "role" and (row.target_id, row.target_domain) == everyone_ref
        ),
        None,
    )
    return everyone.permission if everyone is not None else None


def _channel_decision(
    rows: Sequence[ApplicationCommandPermission],
    subject: CommandPermissionSubject,
    guild: Guild,
) -> bool | None:
    if subject.channel_ref is not None:
        exact = next(
            (
                row
                for row in rows
                if row.target_type == "channel"
                and (row.target_id, row.target_domain) == subject.channel_ref
            ),
            None,
        )
        if exact is not None:
            return exact.permission
    all_channels_ref = (guild.id - 1, guild.origin_domain)
    all_channels = next(
        (
            row
            for row in rows
            if row.target_type == "channel"
            and (row.target_id, row.target_domain) == all_channels_ref
        ),
        None,
    )
    return all_channels.permission if all_channels is not None else None


def command_permission_allowed(
    definition: dict[str, object],
    effective_permissions: int,
    rows: Sequence[ApplicationCommandPermission],
    subject: CommandPermissionSubject,
    guild: Guild,
) -> bool:
    """Evaluate default and granular command permissions at guild authority."""

    actor_permissions = Permission(effective_permissions)
    if actor_permissions & Permission.ADMINISTRATOR:
        return True

    disabled_by_default = definition.get("default_member_permissions") == "0"
    required = command_permission_mask(definition)
    default_allowed = not disabled_by_default and actor_permissions & required == required

    subject_decision = _most_specific_subject_decision(rows, subject, guild)
    channel_decision = _channel_decision(rows, subject, guild)
    decisions = [item for item in (subject_decision, channel_decision) if item is not None]
    if any(item is False for item in decisions):
        return False
    if any(item is True for item in decisions):
        return True
    return default_allowed


async def guild_permission_rows(
    session: AsyncSession,
    guild: Guild,
    application_refs: set[tuple[int, str]],
    command_ids: set[int],
) -> list[ApplicationCommandPermission]:
    if not application_refs:
        return []
    app_clauses = [
        (ApplicationCommandPermission.application_id == app_id)
        & (ApplicationCommandPermission.application_domain == app_domain)
        for app_id, app_domain in application_refs
    ]
    scope_clauses = [ApplicationCommandPermission.command_id.is_(None)]
    if command_ids:
        scope_clauses.append(ApplicationCommandPermission.command_id.in_(command_ids))
    return list(
        await session.scalars(
            select(ApplicationCommandPermission).where(
                ApplicationCommandPermission.guild_id == guild.id,
                ApplicationCommandPermission.guild_domain == guild.origin_domain,
                or_(*app_clauses),
                or_(*scope_clauses),
            )
        )
    )


async def guild_member_role_refs(
    session: AsyncSession,
    guild: Guild,
    user: User,
) -> set[tuple[int, str]]:
    rows = (
        await session.execute(
            select(MemberRole.role_id, MemberRole.role_domain).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                MemberRole.user_id == user.id,
                MemberRole.user_domain == user.origin_domain,
            )
        )
    ).all()
    return {(role_id, role_domain) for role_id, role_domain in rows}


def select_effective_rows(
    rows: Sequence[ApplicationCommandPermission],
    *,
    command_id: int,
    application_ref: tuple[int, str],
) -> list[ApplicationCommandPermission]:
    """Command-specific rows replace the application's synchronized set."""

    explicit = [row for row in rows if row.command_id == command_id]
    if explicit:
        return explicit
    return [
        row
        for row in rows
        if row.command_id is None
        and (row.application_id, row.application_domain) == application_ref
    ]


def permission_rows_by_scope(
    rows: Iterable[ApplicationCommandPermission],
) -> tuple[
    dict[int, list[ApplicationCommandPermission]],
    dict[tuple[int, str], list[ApplicationCommandPermission]],
]:
    commands: dict[int, list[ApplicationCommandPermission]] = defaultdict(list)
    applications: dict[tuple[int, str], list[ApplicationCommandPermission]] = defaultdict(list)
    for row in rows:
        if row.command_id is None:
            applications[(row.application_id, row.application_domain)].append(row)
        else:
            commands[row.command_id].append(row)
    return dict(commands), dict(applications)
