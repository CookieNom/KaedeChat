from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.bots.installations import usable_guild_installation
from app.chat import permissions as permission_service
from app.chat.permissions import (
    BotGuildPermissionGrant,
    PermissionOverwrite,
    bot_guild_permission_grant,
    bot_guild_permission_grant_from_installation,
    calculate_permissions,
    get_permissions,
    permission_cache_ttl,
    require_permissions,
    resolve_permissions,
)
from app.core.permissions import ALL_PERMISSIONS, Permission
from app.db.bot_models import BotInstallation

DOMAIN = "alpha.localhost"


def resolve(
    *,
    base: Permission,
    overwrites: list[PermissionOverwrite] | None = None,
    roles: set[tuple[int, str]] | None = None,
    owner: bool = False,
    timed_out: bool = False,
) -> int:
    return resolve_permissions(
        owner=owner,
        user_id=22,
        user_domain=DOMAIN,
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids=roles or {(10, DOMAIN)},
        base_permissions=int(base),
        overwrites=overwrites or [],
        channel_type=0,
        timed_out=timed_out,
    )


def overwrite(
    target_id: int,
    target_type: str,
    *,
    allow: Permission | None = None,
    deny: Permission | None = None,
) -> PermissionOverwrite:
    return PermissionOverwrite(
        target_id,
        DOMAIN,
        target_type,
        int(allow or 0),
        int(deny or 0),
    )


def test_usable_guild_installation_requires_unrevoked_membership() -> None:
    statement = select(BotInstallation.id).where(usable_guild_installation())
    sql = str(statement.compile())

    assert "bot_installations.status" in sql
    assert "bot_installations.revoked_at IS NULL" in sql
    assert "EXISTS" in sql
    assert "guild_members" in sql


def test_owner_and_administrator_bypass_overwrites() -> None:
    deny_view = [overwrite(10, "role", deny=Permission.VIEW_CHANNEL)]
    assert resolve(base=Permission(0), overwrites=deny_view, owner=True) == ALL_PERMISSIONS
    assert resolve(base=Permission.ADMINISTRATOR, overwrites=deny_view) == ALL_PERMISSIONS


def test_overwrite_order_is_everyone_roles_then_member() -> None:
    result = resolve(
        base=Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES,
        roles={(10, DOMAIN), (11, DOMAIN)},
        overwrites=[
            overwrite(10, "role", deny=Permission.SEND_MESSAGES),
            overwrite(11, "role", allow=Permission.SEND_MESSAGES),
            overwrite(22, "member", deny=Permission.SEND_MESSAGES),
        ],
    )
    assert result & Permission.VIEW_CHANNEL
    assert not result & Permission.SEND_MESSAGES


def test_implicit_masks_remove_dependent_permissions() -> None:
    without_view = resolve(base=Permission.SEND_MESSAGES | Permission.ATTACH_FILES)
    assert without_view == 0
    without_send = resolve(base=Permission.VIEW_CHANNEL | Permission.ATTACH_FILES)
    assert without_send == Permission.VIEW_CHANNEL


def test_forum_send_dependencies_and_read_only_thread_reactions() -> None:
    forum = resolve_permissions(
        owner=False,
        user_id=22,
        user_domain=DOMAIN,
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids={(10, DOMAIN)},
        base_permissions=int(
            Permission.VIEW_CHANNEL
            | Permission.ATTACH_FILES
            | Permission.EMBED_LINKS
            | Permission.MENTION_EVERYONE
        ),
        overwrites=[],
        channel_type=15,
        timed_out=False,
    )
    assert forum == Permission.VIEW_CHANNEL

    thread = resolve_permissions(
        owner=False,
        user_id=22,
        user_domain=DOMAIN,
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids={(10, DOMAIN)},
        base_permissions=int(
            Permission.VIEW_CHANNEL
            | Permission.READ_MESSAGE_HISTORY
            | Permission.ADD_REACTIONS
            | Permission.USE_EXTERNAL_EMOJIS
            | Permission.ATTACH_FILES
        ),
        overwrites=[],
        channel_type=11,
        timed_out=False,
    )
    assert thread & Permission.USE_EXTERNAL_EMOJIS
    assert thread & Permission.ADD_REACTIONS
    assert not thread & Permission.ATTACH_FILES


def test_timeout_reduces_member_to_read_only() -> None:
    result = resolve(
        base=(
            Permission.VIEW_CHANNEL
            | Permission.READ_MESSAGE_HISTORY
            | Permission.SEND_MESSAGES
            | Permission.ADD_REACTIONS
        ),
        timed_out=True,
    )
    assert result == Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (Permission.VIEW_CHANNEL | Permission.PRIORITY_SPEAKER, Permission.VIEW_CHANNEL),
        (
            Permission.VIEW_CHANNEL | Permission.CONNECT | Permission.PRIORITY_SPEAKER,
            Permission.VIEW_CHANNEL | Permission.CONNECT,
        ),
        (
            Permission.VIEW_CHANNEL
            | Permission.CONNECT
            | Permission.SPEAK
            | Permission.PRIORITY_SPEAKER,
            Permission.VIEW_CHANNEL
            | Permission.CONNECT
            | Permission.SPEAK
            | Permission.PRIORITY_SPEAKER,
        ),
    ],
)
def test_priority_speaker_requires_connect_and_speak(
    base: Permission, expected: Permission
) -> None:
    assert (
        Permission(
            resolve_permissions(
                owner=False,
                user_id=22,
                user_domain=DOMAIN,
                everyone_role_id=10,
                everyone_role_domain=DOMAIN,
                role_ids={(10, DOMAIN)},
                base_permissions=int(base),
                overwrites=[],
                channel_type=2,
                timed_out=False,
            )
        )
        == expected
    )


def test_permission_cache_never_rounds_a_live_timeout_down_to_zero() -> None:
    evaluated_at = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
    expiring_member = SimpleNamespace(
        timeout_until=evaluated_at + timedelta(milliseconds=250),
        timeout_indefinite=False,
    )
    expired_member = SimpleNamespace(
        timeout_until=evaluated_at - timedelta(milliseconds=1),
        timeout_indefinite=False,
    )

    assert permission_cache_ttl(expiring_member, evaluated_at=evaluated_at) == 1
    assert permission_cache_ttl(expired_member, evaluated_at=evaluated_at) == 300


def test_everyone_overwrite_uses_guild_domain_for_remote_member() -> None:
    result = resolve_permissions(
        owner=False,
        user_id=22,
        user_domain="remote.localhost",
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids={(10, DOMAIN)},
        base_permissions=int(Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES),
        overwrites=[overwrite(10, "role", deny=Permission.SEND_MESSAGES)],
        channel_type=0,
        timed_out=False,
    )
    assert result == Permission.VIEW_CHANNEL


def test_voice_everyone_deny_then_inherit_restores_connect() -> None:
    base = int(Permission.VIEW_CHANNEL | Permission.CONNECT | Permission.SPEAK)
    denied = resolve_permissions(
        owner=False,
        user_id=22,
        user_domain="remote.localhost",
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids={(10, DOMAIN)},
        base_permissions=base,
        overwrites=[overwrite(10, "role", deny=Permission.CONNECT)],
        channel_type=2,
        timed_out=False,
    )
    inherited = resolve_permissions(
        owner=False,
        user_id=22,
        user_domain="remote.localhost",
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids={(10, DOMAIN)},
        base_permissions=base,
        # The middle tri-state removes the bit from both masks. The API now
        # removes the empty row, but accepting it here protects the resolver
        # while older federation events drain.
        overwrites=[overwrite(10, "role")],
        channel_type=2,
        timed_out=False,
    )

    assert denied & Permission.VIEW_CHANNEL
    assert not denied & Permission.CONNECT
    assert not denied & Permission.SPEAK
    assert inherited & Permission.CONNECT
    assert inherited & Permission.SPEAK


@pytest.mark.parametrize("channel_type", [2, 13])
def test_voice_text_chat_masks_text_dependencies_without_send_messages(
    channel_type: int,
) -> None:
    permissions = resolve_permissions(
        owner=False,
        user_id=22,
        user_domain=DOMAIN,
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids={(10, DOMAIN)},
        base_permissions=int(
            Permission.VIEW_CHANNEL
            | Permission.CONNECT
            | Permission.SPEAK
            | Permission.ATTACH_FILES
            | Permission.EMBED_LINKS
            | Permission.MENTION_EVERYONE
        ),
        overwrites=[],
        channel_type=channel_type,
        timed_out=False,
    )

    assert permissions & Permission.CONNECT
    assert permissions & Permission.SPEAK
    assert not permissions & Permission.ATTACH_FILES
    assert not permissions & Permission.EMBED_LINKS
    assert not permissions & Permission.MENTION_EVERYONE


@pytest.mark.asyncio
async def test_bot_installation_ceiling_caps_live_role_permissions() -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain=DOMAIN,
        owner_id=999,
        owner_domain=DOMAIN,
    )
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN, account_type="bot")
    member = SimpleNamespace(timeout_until=None, timeout_indefinite=False)
    live_role = SimpleNamespace(
        id=10,
        origin_domain=DOMAIN,
        permissions=int(
            Permission.VIEW_CHANNEL | Permission.CHANGE_NICKNAME | Permission.MANAGE_NICKNAMES
        ),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=member),
        scalars=AsyncMock(return_value=[live_role]),
    )
    own_nickname_only = BotGuildPermissionGrant(
        installation_id=70,
        grant_revision=3,
        granted_permissions=int(Permission.VIEW_CHANNEL | Permission.CHANGE_NICKNAME),
        channel_restrictions=(),
    )

    permissions, resolved_member = await calculate_permissions(
        session,
        guild,
        actor,
        bot_grant=own_nickname_only,
    )

    assert resolved_member is member
    assert permissions & Permission.CHANGE_NICKNAME
    assert not permissions & Permission.MANAGE_NICKNAMES


@pytest.mark.asyncio
async def test_bot_installation_ceiling_reapplies_text_and_voice_dependencies() -> None:
    text_channel = SimpleNamespace(type=0)
    text_grant = BotGuildPermissionGrant(
        installation_id=70,
        grant_revision=3,
        granted_permissions=int(Permission.VIEW_CHANNEL | Permission.ATTACH_FILES),
        channel_restrictions=(),
    )
    text_permissions = await text_grant.apply(
        SimpleNamespace(),
        int(Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES | Permission.ATTACH_FILES),
        text_channel,
    )

    assert text_permissions & Permission.VIEW_CHANNEL
    assert not text_permissions & Permission.ATTACH_FILES

    voice_channel = SimpleNamespace(type=2)
    voice_grant = BotGuildPermissionGrant(
        installation_id=70,
        grant_revision=3,
        granted_permissions=int(
            Permission.VIEW_CHANNEL | Permission.STREAM | Permission.MOVE_MEMBERS
        ),
        channel_restrictions=(),
    )
    voice_permissions = await voice_grant.apply(
        SimpleNamespace(),
        int(
            Permission.VIEW_CHANNEL
            | Permission.CONNECT
            | Permission.STREAM
            | Permission.MOVE_MEMBERS
        ),
        voice_channel,
    )

    assert voice_permissions & Permission.VIEW_CHANNEL
    assert not voice_permissions & Permission.STREAM
    assert not voice_permissions & Permission.MOVE_MEMBERS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restrictions", "expected"),
    [
        (("20@alpha.localhost",), True),
        (("99@alpha.localhost",), True),
        (("21@alpha.localhost",), False),
    ],
)
async def test_bot_installation_channel_restrictions_cap_exact_channel_permissions(
    restrictions: tuple[str, ...],
    expected: bool,
) -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain=DOMAIN,
        owner_id=999,
        owner_domain=DOMAIN,
    )
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN, account_type="bot")
    member = SimpleNamespace(timeout_until=None, timeout_indefinite=False)
    live_role = SimpleNamespace(
        id=10,
        origin_domain=DOMAIN,
        permissions=int(Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS),
    )
    channel = SimpleNamespace(
        id=20,
        origin_domain=DOMAIN,
        guild_id=10,
        guild_domain=DOMAIN,
        parent_id=99,
        parent_domain=DOMAIN,
        permissions_synced=False,
        type=0,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=member),
        scalars=AsyncMock(side_effect=[[live_role], []]),
        get=AsyncMock(
            return_value=SimpleNamespace(
                id=99,
                origin_domain=DOMAIN,
                guild_id=10,
                guild_domain=DOMAIN,
                unavailable=False,
                type=4,
                parent_id=None,
                parent_domain=None,
            )
        ),
    )
    grant = BotGuildPermissionGrant(
        installation_id=70,
        grant_revision=3,
        granted_permissions=int(Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS),
        channel_restrictions=restrictions,
    )

    permissions, _ = await calculate_permissions(
        session,
        guild,
        actor,
        channel=channel,
        bot_grant=grant,
    )

    assert bool(permissions & Permission.MANAGE_CHANNELS) is expected


@pytest.mark.asyncio
async def test_category_restricted_bot_permissions_include_child_thread() -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain=DOMAIN,
        owner_id=999,
        owner_domain=DOMAIN,
    )
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN, account_type="bot")
    member = SimpleNamespace(timeout_until=None, timeout_indefinite=False)
    live_role = SimpleNamespace(
        id=10,
        origin_domain=DOMAIN,
        permissions=int(Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS),
    )
    category = SimpleNamespace(
        id=90,
        origin_domain=DOMAIN,
        guild_id=10,
        guild_domain=DOMAIN,
        unavailable=False,
        parent_id=None,
        parent_domain=None,
        permissions_synced=False,
        type=4,
    )
    forum = SimpleNamespace(
        id=91,
        origin_domain=DOMAIN,
        guild_id=10,
        guild_domain=DOMAIN,
        unavailable=False,
        parent_id=90,
        parent_domain=DOMAIN,
        permissions_synced=False,
        type=15,
    )
    thread = SimpleNamespace(
        id=92,
        origin_domain=DOMAIN,
        guild_id=10,
        guild_domain=DOMAIN,
        unavailable=False,
        parent_id=91,
        parent_domain=DOMAIN,
        permissions_synced=False,
        type=11,
    )
    ancestors = {(91, DOMAIN): forum, (90, DOMAIN): category}
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=member),
        scalars=AsyncMock(side_effect=[[live_role], []]),
        get=AsyncMock(side_effect=lambda _model, ref, **_kwargs: ancestors.get(ref)),
    )
    grant = BotGuildPermissionGrant(
        installation_id=70,
        grant_revision=3,
        granted_permissions=int(Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS),
        channel_restrictions=(f"90@{DOMAIN}",),
    )

    permissions, _ = await calculate_permissions(
        session,
        guild,
        actor,
        channel=thread,
        bot_grant=grant,
    )

    assert permissions & Permission.MANAGE_CHANNELS


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_available", [True, False])
async def test_permission_cache_revalidates_category_restricted_thread_ancestry(
    parent_available: bool,
) -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain=DOMAIN,
        owner_id=999,
        owner_domain=DOMAIN,
        permission_generation=7,
    )
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN, account_type="bot")
    member = SimpleNamespace(member_version=4)
    category = SimpleNamespace(
        id=90,
        origin_domain=DOMAIN,
        guild_id=10,
        guild_domain=DOMAIN,
        unavailable=False,
        parent_id=None,
        parent_domain=None,
        type=4,
    )
    forum = SimpleNamespace(
        id=91,
        origin_domain=DOMAIN,
        guild_id=10,
        guild_domain=DOMAIN,
        unavailable=False,
        parent_id=90,
        parent_domain=DOMAIN,
        type=15,
    )
    thread = SimpleNamespace(
        id=92,
        origin_domain=DOMAIN,
        guild_id=10,
        guild_domain=DOMAIN,
        unavailable=False,
        parent_id=91,
        parent_domain=DOMAIN,
        type=11,
    )
    ancestors = {(91, DOMAIN): forum, (90, DOMAIN): category} if parent_available else {}
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=member),
        get=AsyncMock(side_effect=lambda _model, ref, **_kwargs: ancestors.get(ref)),
    )
    cached_permissions = int(Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS)
    redis = SimpleNamespace(get=AsyncMock(return_value=str(cached_permissions)))
    grant = BotGuildPermissionGrant(70, 3, cached_permissions, (f"90@{DOMAIN}",))

    permissions = await get_permissions(
        session,
        redis,
        guild,
        actor,
        channel=thread,
        bot_grant=grant,
    )

    assert permissions == (cached_permissions if parent_available else 0)
    if parent_available:
        redis.get.assert_awaited_once()
    else:
        redis.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_calculation_rejects_cross_guild_channel_context() -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain=DOMAIN,
        owner_id=999,
        owner_domain=DOMAIN,
    )
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN, account_type="human")
    channel = SimpleNamespace(
        id=20,
        origin_domain="other.example",
        guild_id=10,
        guild_domain="other.example",
        type=0,
    )
    session = SimpleNamespace(scalar=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await calculate_permissions(session, guild, actor, channel=channel)

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "CHANNEL_GUILD_INVALID"}
    session.scalar.assert_not_awaited()

    redis = SimpleNamespace(get=AsyncMock(return_value=str(ALL_PERMISSIONS)))
    with pytest.raises(HTTPException) as cached_caught:
        await get_permissions(session, redis, guild, actor, channel=channel)

    assert cached_caught.value.detail == {"code": "CHANNEL_GUILD_INVALID"}
    redis.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_active_bot_installations_fail_to_zero_ceiling() -> None:
    guild = SimpleNamespace(id=10, origin_domain=DOMAIN)
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN, account_type="bot")
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[SimpleNamespace(id=70), SimpleNamespace(id=71)])
    )

    grant = await bot_guild_permission_grant(session, guild, actor)

    assert grant == BotGuildPermissionGrant(None, 0, 0, ())
    statement = session.scalars.await_args.args[0]
    sql = str(statement.compile())
    assert "bot_applications.origin_domain" in sql
    assert "bot_application_targets.runtime_fingerprint IS NOT NULL" in sql
    assert "bot_application_targets.runtime_manifest_generation" in sql
    assert "bot_application_targets.runtime_revocation_generation" in sql
    assert "bot_application_targets.runtime_status" in sql
    assert "bot_application_targets.runtime_target_allowed IS true" in sql


@pytest.mark.asyncio
async def test_disabled_bot_actor_has_zero_installation_ceiling() -> None:
    guild = SimpleNamespace(id=10, origin_domain=DOMAIN)
    actor = SimpleNamespace(
        id=22,
        origin_domain=DOMAIN,
        account_type="bot",
        disabled_at=datetime.now(UTC),
    )
    session = SimpleNamespace(scalars=AsyncMock())

    grant = await bot_guild_permission_grant(session, guild, actor)

    assert grant == BotGuildPermissionGrant(None, 0, 0, ())
    session.scalars.assert_not_awaited()


def test_bot_grant_cache_identity_is_stable_across_restriction_order() -> None:
    common = {
        "id": 70,
        "grant_revision": 3,
        "granted_permissions": 8,
    }
    first = bot_guild_permission_grant_from_installation(
        SimpleNamespace(
            **common,
            channel_restrictions=["21@a.example", "20@a.example"],
        )
    )
    second = bot_guild_permission_grant_from_installation(
        SimpleNamespace(
            **common,
            channel_restrictions=["20@a.example", "21@a.example"],
        )
    )

    assert first.cache_identity() == second.cache_identity()


@pytest.mark.asyncio
async def test_timeout_denial_explains_expiry_and_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    expiry = datetime.now(UTC) + timedelta(hours=1)
    guild = SimpleNamespace(id=10, origin_domain=DOMAIN)
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN)
    member = SimpleNamespace(
        timeout_until=expiry,
        timeout_indefinite=False,
        timeout_reason="Repeated spam",
    )
    session = AsyncMock()
    session.get.return_value = member
    monkeypatch.setattr(permission_service, "get_permissions", AsyncMock(return_value=0))

    with pytest.raises(HTTPException) as caught:
        await require_permissions(
            session,
            AsyncMock(),
            guild,
            actor,
            Permission.SEND_MESSAGES,
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {
        "code": "MEMBER_TIMED_OUT",
        "message": "You are currently timed out in this guild.",
        "timeout_until": expiry.isoformat(),
        "timeout_indefinite": False,
        "reason": "Repeated spam",
    }


@pytest.mark.asyncio
async def test_ordinary_permission_denial_does_not_leak_timeout_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain=DOMAIN)
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN)
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        timeout_until=None,
        timeout_indefinite=False,
        timeout_reason=None,
    )
    monkeypatch.setattr(permission_service, "get_permissions", AsyncMock(return_value=0))

    with pytest.raises(HTTPException) as caught:
        await require_permissions(
            session,
            AsyncMock(),
            guild,
            actor,
            Permission.SEND_MESSAGES,
        )

    assert caught.value.detail == {
        "code": "MISSING_PERMISSIONS",
        "permissions": str(int(Permission.SEND_MESSAGES)),
        "message": "You do not have permission to perform this action.",
    }


@pytest.mark.asyncio
async def test_active_timeout_does_not_replace_preserved_read_permission_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain=DOMAIN)
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN)
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        timeout_until=datetime.now(UTC) + timedelta(hours=1),
        timeout_indefinite=False,
        timeout_reason="Private moderation context",
    )
    monkeypatch.setattr(permission_service, "get_permissions", AsyncMock(return_value=0))

    with pytest.raises(HTTPException) as caught:
        await require_permissions(
            session,
            AsyncMock(),
            guild,
            actor,
            Permission.VIEW_CHANNEL,
        )

    assert caught.value.detail == {
        "code": "MISSING_PERMISSIONS",
        "permissions": str(int(Permission.VIEW_CHANNEL)),
        "message": "You do not have permission to perform this action.",
    }
