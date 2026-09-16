from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text

from app.api.onboarding import safe_role
from app.chat.onboarding import (
    OnboardingAnswers,
    OnboardingConfig,
    needs_rules,
    selected_onboarding,
)
from app.chat.permissions import calculate_permissions
from app.core.permissions import Permission


def config():
    return OnboardingConfig.model_validate(
        {
            "enabled": True,
            "revision": 3,
            "rules_revision": 2,
            "rules": ["Be kind."],
            "default_channel_ids": ["11@home.test"],
            "questions": [
                {
                    "id": "interests",
                    "title": "What brings you here?",
                    "required": True,
                    "options": [
                        {
                            "id": "games",
                            "title": "Games",
                            "role_ids": ["20@home.test"],
                            "channel_ids": ["12@home.test"],
                        },
                        {"id": "art", "title": "Art"},
                    ],
                }
            ],
        }
    )


def test_answers_require_current_revision_and_valid_required_choices():
    setup = config()
    valid = OnboardingAnswers(revision=3, accept_rules=True, answers={"interests": ["games"]})
    assert selected_onboarding(setup, valid) == ({"20@home.test"}, {"11@home.test", "12@home.test"})
    for changes in (
        {"revision": 2},
        {"answers": {}},
        {"answers": {"interests": ["unknown"]}},
        {"answers": {"interests": ["games", "art"]}},
        {"completed_tasks": ["fake"]},
    ):
        with pytest.raises(HTTPException):
            selected_onboarding(setup, valid.model_copy(update=changes))
    assert needs_rules(setup.model_dump(), {"rules_revision": 1})
    assert not needs_rules(setup.model_dump(), {"rules_revision": 2})
    assert not needs_rules({"enabled": False, "rules": ["New rules"]}, {})


def test_config_rejects_ambiguous_answers_and_blank_rules():
    setup = config().model_dump()
    setup["questions"][0]["options"].append(setup["questions"][0]["options"][0])
    with pytest.raises(ValidationError):
        OnboardingConfig.model_validate(setup)
    with pytest.raises(ValidationError):
        OnboardingConfig(rules=["   "])


@pytest.mark.asyncio
async def test_self_selected_roles_cannot_grant_admin_or_cross_guild_access():
    guild = SimpleNamespace(id=10, origin_domain="home.test")
    role = SimpleNamespace(
        id=20,
        origin_domain="home.test",
        guild_id=10,
        guild_domain="home.test",
        managed=False,
        permissions=0,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=role), scalar=AsyncMock(return_value=None))
    assert await safe_role(session, guild, "20@home.test") is role
    for field, value in [
        ("permissions", int(Permission.ADMINISTRATOR)),
        ("managed", True),
        ("guild_id", 99),
    ]:
        old = getattr(role, field)
        setattr(role, field, value)
        with pytest.raises(HTTPException):
            await safe_role(session, guild, "20@home.test")
        setattr(role, field, old)


@pytest.mark.asyncio
async def test_self_selected_role_overrides_allow_participation_but_not_moderation(postgres_schema):
    # Exercise the real override query, including categories and composite identities.
    await postgres_schema.execute(
        text(
            "CREATE TABLE channel_overwrites (channel_id bigint, channel_domain text, "
            "guild_id bigint, guild_domain text, target_id bigint, target_domain text, "
            "target_type text, allow bigint)"
        )
    )
    role = SimpleNamespace(
        id=20,
        origin_domain="home.test",
        guild_id=10,
        guild_domain="home.test",
        managed=False,
        permissions=0,
    )
    guild = SimpleNamespace(id=10, origin_domain="home.test")
    session = SimpleNamespace(get=AsyncMock(return_value=role), scalar=postgres_schema.scalar)
    await postgres_schema.execute(
        text(
            "INSERT INTO channel_overwrites VALUES "
            "(30, 'home.test', 10, 'home.test', 20, 'home.test', 'role', :participate), "
            "(31, 'home.test', 10, 'home.test', 20, 'home.test', 'member', :moderate), "
            "(32, 'other.test', 10, 'other.test', 20, 'other.test', 'role', :moderate)"
        ),
        {"participate": int(Permission.SEND_MESSAGES), "moderate": int(Permission.MANAGE_MESSAGES)},
    )
    assert await safe_role(session, guild, "20@home.test") is role
    # An override on any channel/category for this role disqualifies it.
    await postgres_schema.execute(
        text(
            "INSERT INTO channel_overwrites VALUES "
            "(33, 'home.test', 10, 'home.test', 20, 'home.test', 'role', :moderate)"
        ),
        {"moderate": int(Permission.MANAGE_MESSAGES)},
    )
    with pytest.raises(HTTPException) as error:
        await safe_role(session, guild, "20@home.test")
    assert error.value.detail["code"] == "ONBOARDING_INVALID_ROLE"


@pytest.mark.asyncio
async def test_pending_rules_keep_read_access_but_block_text_reactions_and_voice():
    guild = SimpleNamespace(
        id=10,
        origin_domain="home.test",
        owner_id=1,
        owner_domain="home.test",
        onboarding=config().model_dump(),
    )
    actor = SimpleNamespace(id=2, origin_domain="home.test", account_type="human")
    member = SimpleNamespace(timeout_until=None, timeout_indefinite=False, onboarding_state={})
    allowed = (
        Permission.VIEW_CHANNEL
        | Permission.READ_MESSAGE_HISTORY
        | Permission.SEND_MESSAGES
        | Permission.ADD_REACTIONS
        | Permission.CONNECT
        | Permission.SPEAK
    )
    role = SimpleNamespace(id=10, origin_domain="home.test", permissions=int(allowed))
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=member), scalars=AsyncMock(return_value=[role])
    )
    permissions, _ = await calculate_permissions(session, guild, actor)
    assert permissions & Permission.VIEW_CHANNEL
    assert permissions & Permission.READ_MESSAGE_HISTORY
    assert not permissions & (
        Permission.SEND_MESSAGES | Permission.ADD_REACTIONS | Permission.CONNECT | Permission.SPEAK
    )
    member.onboarding_state = {"rules_revision": 2}
    permissions, _ = await calculate_permissions(session, guild, actor)
    assert permissions & allowed == allowed
    member.onboarding_state = {}
    actor.id = guild.owner_id
    permissions, _ = await calculate_permissions(session, guild, actor)
    assert permissions & Permission.ADMINISTRATOR


@pytest.mark.asyncio
async def test_onboarding_migration_preserves_members_and_defaults(postgres_schema):
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    migration = import_module("migrations.versions.f71d9a3b2c80_guild_onboarding")
    await postgres_schema.execute(text("CREATE TABLE guilds (id bigint PRIMARY KEY)"))
    await postgres_schema.execute(text("CREATE TABLE guild_members (user_id bigint PRIMARY KEY)"))
    await postgres_schema.execute(text("INSERT INTO guilds VALUES (10)"))
    await postgres_schema.execute(text("INSERT INTO guild_members VALUES (2)"))

    def apply(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, direction)()

    await postgres_schema.run_sync(apply, "upgrade")
    assert (await postgres_schema.execute(text("SELECT id, onboarding FROM guilds"))).one() == (
        10,
        {},
    )
    assert (
        await postgres_schema.execute(text("SELECT user_id, onboarding_state FROM guild_members"))
    ).one() == (2, {})
    await postgres_schema.execute(
        text("UPDATE guild_members SET onboarding_state = '{\"rules_revision\": 2}'")
    )
    assert (
        await postgres_schema.scalar(
            text("SELECT onboarding_state->>'rules_revision' FROM guild_members")
        )
        == "2"
    )
    await postgres_schema.run_sync(apply, "downgrade")
    assert await postgres_schema.scalar(text("SELECT user_id FROM guild_members")) == 2


@pytest.mark.asyncio
async def test_completion_grants_each_role_once_and_preserves_existing_roles(monkeypatch):
    from unittest.mock import Mock

    from app.api import onboarding as endpoint
    from app.core.types import EntityRef
    from app.db.models import MemberRole

    setup = config()
    setup.questions[0].options[0].role_ids.append("20")
    guild = SimpleNamespace(id=10, origin_domain="home.test", onboarding=setup.model_dump())
    actor = SimpleNamespace(id=2, origin_domain="home.test")
    member = SimpleNamespace(onboarding_state={}, temporary=True, member_version=1)
    role = SimpleNamespace(id=20, origin_domain="home.test")
    unrelated = MemberRole(
        guild_id=10,
        guild_domain="home.test",
        user_id=2,
        user_domain="home.test",
        role_id=21,
        role_domain="home.test",
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[unrelated]),
        add=Mock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        endpoint, "proxy_authenticated_guild_management", AsyncMock(return_value=(False, None))
    )
    monkeypatch.setattr(endpoint, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(endpoint, "membership", AsyncMock(return_value=member))
    monkeypatch.setattr(endpoint, "safe_role", AsyncMock(return_value=role))
    for name in (
        "render_member_update",
        "queue_guild_mutation",
        "wake_queued_guild_federation",
        "publish_e2ee_policy_updates",
        "publish_dispatch",
        "response_for",
    ):
        monkeypatch.setattr(endpoint, name, AsyncMock(return_value={}))

    async def complete(answers):
        return await endpoint.complete_onboarding(
            EntityRef("10"),
            answers,
            SimpleNamespace(user=actor),
            session,
            AsyncMock(),
            SimpleNamespace(),
        )

    valid = OnboardingAnswers(revision=3, accept_rules=True, answers={"interests": ["games"]})
    with pytest.raises(HTTPException, match="403"):
        await complete(valid.model_copy(update={"accept_rules": False}))
    session.add.assert_not_called()
    await complete(valid)
    session.add.assert_called_once()
    assert member.onboarding_state["granted_role_ids"] == ["20@home.test"]
    assert member.onboarding_state["rules_revision"] == 2
    assert not member.temporary
    granted = session.add.call_args.args[0]
    session.scalars.return_value = [unrelated, granted]
    session.add.reset_mock()
    await complete(valid)
    session.add.assert_not_called()
    setup.questions[0].required = False
    guild.onboarding = setup.model_dump()
    await complete(valid.model_copy(update={"answers": {}}))
    session.delete.assert_awaited_once_with(granted)
    assert member.onboarding_state["granted_role_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin,friend,allowed", [(False, False, False), (True, False, True), (False, True, True)]
)
async def test_pending_rules_block_unsolicited_member_dms_but_exempt_friends_and_admins(
    monkeypatch, admin, friend, allowed
):
    from unittest.mock import Mock

    from app.chat import permissions, privacy

    actor = SimpleNamespace(id=2, origin_domain="home.test")
    recipient = SimpleNamespace(id=3, origin_domain="home.test")
    guild = SimpleNamespace(onboarding=config().model_dump())
    member = SimpleNamespace(onboarding_state={})
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(dm_privacy="everyone")),
        execute=AsyncMock(return_value=SimpleNamespace(all=Mock(return_value=[(guild, member)]))),
    )
    monkeypatch.setattr(privacy, "blocked_between", AsyncMock(return_value=False))
    monkeypatch.setattr(
        privacy,
        "relationship",
        AsyncMock(return_value=SimpleNamespace(type="friend") if friend else None),
    )
    monkeypatch.setattr(
        permissions,
        "calculate_permissions",
        AsyncMock(
            return_value=(Permission.ADMINISTRATOR if admin else Permission.VIEW_CHANNEL, member)
        ),
    )
    assert await privacy.can_direct_message(session, actor, recipient) is allowed
