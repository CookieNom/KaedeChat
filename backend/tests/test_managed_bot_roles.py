from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.api import management
from app.chat.hierarchy import require_can_manage_role, require_unmanaged_role
from app.chat.payloads import role_payload
from app.chat.schemas import MemberRoleSet
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.models import Role

DOMAIN = "guild.example"


def bot_role() -> Role:
    role = Role(
        id=20,
        origin_domain=DOMAIN,
        guild_id=10,
        guild_domain=DOMAIN,
        name="Weather",
        permissions=int(Permission.SEND_MESSAGES),
        position=1,
        managed=True,
    )
    role.updated_at = datetime.now(UTC)
    return role


async def test_managed_role_permissions_remain_editable_by_owner() -> None:
    role = bot_role()
    guild = SimpleNamespace(owner_id=1, owner_domain=DOMAIN)
    actor = SimpleNamespace(id=1, origin_domain=DOMAIN)
    await require_can_manage_role(None, guild, actor, role)
    assert role_payload(role)["managed"] is True
    with pytest.raises(HTTPException, match="MANAGED_ROLE_IMMUTABLE"):
        require_unmanaged_role(role)
    role.managed = False
    require_unmanaged_role(role)


@pytest.mark.parametrize("operation", ["assign_role", "remove_role", "delete_role"])
async def test_managed_role_membership_and_deletion_rejected_at_authority(monkeypatch, operation):
    role = bot_role()
    guild = SimpleNamespace(id=10, origin_domain=DOMAIN, owner_id=1, owner_domain=DOMAIN)
    auth = SimpleNamespace(user=SimpleNamespace(id=1, origin_domain=DOMAIN))
    for name, value in {
        "proxy_remote_guild_management": None,
        "local_guild": guild,
        "require_permissions": int(Permission.MANAGE_ROLES),
        "guild_role": role,
        "require_can_assign_member_role": SimpleNamespace(),
    }.items():
        monkeypatch.setattr(management, name, AsyncMock(return_value=value))
    session = AsyncMock()
    args = {
        "guild_id": EntityRef(f"10@{DOMAIN}"),
        "role_id": EntityRef(f"20@{DOMAIN}"),
        "auth": auth,
        "session": session,
        "redis": AsyncMock(),
        "snowflake": AsyncMock(),
        "settings": SimpleNamespace(domain=DOMAIN),
    }
    if operation != "delete_role":
        args["user_id"] = EntityRef(f"30@{DOMAIN}")
    with pytest.raises(HTTPException) as denied:
        await getattr(management, operation)(**args)
    assert denied.value.detail == {"code": "MANAGED_ROLE_IMMUTABLE"}
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_managed_role_migration_backfills_only_unrevoked_installations(postgres_schema):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = import_module("migrations.versions.9e7a1c4b8d20_managed_bot_roles")
    await postgres_schema.execute(text("CREATE TABLE roles (id bigint, origin_domain text)"))
    await postgres_schema.execute(
        text(
            "CREATE TABLE bot_installations (role_id bigint, role_domain text, "
            "status text, revoked_at timestamptz)"
        )
    )
    await postgres_schema.execute(
        text("INSERT INTO roles VALUES (1, 'a'), (2, 'a'), (3, 'a'), (1, 'b')")
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO bot_installations VALUES (1, 'a', 'active', NULL), "
            "(2, 'a', 'suspended', NULL), (3, 'a', 'revoked', now())"
        )
    )

    def migrate(connection, operation):
        with Operations.context(MigrationContext.configure(connection)):
            operation()

    await postgres_schema.run_sync(migrate, migration.upgrade)
    rows = (
        await postgres_schema.execute(
            text("SELECT id, origin_domain, managed FROM roles ORDER BY origin_domain, id")
        )
    ).all()
    assert rows == [(1, "a", True), (2, "a", True), (3, "a", False), (1, "b", False)]
    await postgres_schema.run_sync(migrate, migration.downgrade)


@pytest.mark.parametrize("removing", [False, True])
async def test_bulk_role_replacement_cannot_change_managed_membership(monkeypatch, removing):
    role = bot_role()
    guild = SimpleNamespace(id=10, origin_domain=DOMAIN, owner_id=1, owner_domain=DOMAIN)
    auth = SimpleNamespace(user=SimpleNamespace(id=1, origin_domain=DOMAIN))
    for name, value in {
        "proxy_remote_guild_management": None,
        "local_guild": guild,
        "require_permissions": int(Permission.MANAGE_ROLES),
        "guild_role": role,
        "require_can_assign_member_role": SimpleNamespace(),
    }.items():
        monkeypatch.setattr(management, name, AsyncMock(return_value=value))
    rows = [(20, DOMAIN)] if removing else []
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(all=Mock(return_value=rows))
    session.get.return_value = role
    with pytest.raises(HTTPException) as denied:
        await management.replace_member_roles(
            EntityRef(f"10@{DOMAIN}"),
            EntityRef(f"30@{DOMAIN}"),
            MemberRoleSet(role_ids=[] if removing else [f"20@{DOMAIN}"]),
            auth,
            session,
            AsyncMock(),
            AsyncMock(),
            SimpleNamespace(domain=DOMAIN),
        )
    assert denied.value.detail == {"code": "MANAGED_ROLE_IMMUTABLE"}
    session.execute.assert_awaited_once()  # Only the current-role read, no mutation.
    session.commit.assert_not_awaited()
