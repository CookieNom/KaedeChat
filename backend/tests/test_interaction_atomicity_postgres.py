"""Opt-in PostgreSQL coverage for public interaction response atomicity.

Run against a disposable, fully migrated PostgreSQL database with::

    KAEDE_INTERACTION_TEST_DATABASE_URL=postgresql+asyncpg://... \
      pytest -q tests/test_interaction_atomicity_postgres.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.responses import Response

import app.api.channels as channels
import app.api.interactions as interactions
from app.bots.auth import BotPrincipal
from app.chat.channel_access import ChannelAccess
from app.core.permissions import Permission
from app.core.snowflake import EPOCH_MS
from app.db.bot_models import (
    ApplicationCommand,
    BotApplication,
    BotApplicationTarget,
    BotInstallation,
    BotInteraction,
    BotInteractionResponse,
    BotToken,
    BotUserInstallation,
    BotWorker,
    DeveloperTeam,
    InteractionDispatchOutbox,
)
from app.db.models import Channel, Guild, GuildMember, Instance, Message, User
from app.db.partitions import ensure_message_partitions

DATABASE_URL = os.environ.get("KAEDE_INTERACTION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason=("set KAEDE_INTERACTION_TEST_DATABASE_URL to a disposable migrated PostgreSQL database"),
)


class AtomicSnowflake:
    def __init__(self, *, worker_id: int = 991, sequence: int = 100) -> None:
        self.worker_id = worker_id
        self.sequence = sequence
        self.lock = asyncio.Lock()

    async def mint(self) -> int:
        async with self.lock:
            now_ms = time.time_ns() // 1_000_000
            value = ((now_ms - EPOCH_MS) << 22) | (self.worker_id << 12) | self.sequence
            self.sequence += 1
            return value


class FailAfterMessageSnowflake:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.calls = 0

    async def mint(self) -> int:
        self.calls += 1
        if self.calls == 1:
            return self.message_id
        raise RuntimeError("injected response-id failure")


async def cleanup_domain(session: AsyncSession, domain: str) -> None:
    await session.execute(
        delete(InteractionDispatchOutbox).where(InteractionDispatchOutbox.user_domain == domain)
    )
    await session.execute(delete(Message).where(Message.origin_domain == domain))
    await session.execute(delete(Guild).where(Guild.origin_domain == domain))
    await session.execute(delete(BotApplication).where(BotApplication.origin_domain == domain))
    await session.execute(delete(DeveloperTeam).where(DeveloperTeam.origin_domain == domain))
    await session.execute(delete(User).where(User.origin_domain == domain))
    await session.execute(delete(Instance).where(Instance.domain == domain))
    await session.commit()


@pytest.mark.asyncio
async def test_public_callback_is_atomic_under_race_and_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    domain = "interaction-atomicity-it.example"
    ids = AtomicSnowflake(sequence=200)
    owner_id = await ids.mint()
    bot_id = await ids.mint()
    guild_id = await ids.mint()
    channel_id = await ids.mint()
    application_id = await ids.mint()
    command_id = await ids.mint()
    installation_id = await ids.mint()
    interaction_id = await ids.mint()
    rollback_interaction_id = await ids.mint()
    interaction_token = "t" * 43
    now = datetime.now(UTC)

    async def noop(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def empty(*args: object, **kwargs: object) -> set[str]:
        del args, kwargs
        return set()

    async def no_roles(*args: object, **kwargs: object) -> list[tuple[int, str]]:
        del args, kwargs
        return []

    async def access_for(
        session: AsyncSession,
        settings: object,
        actor: User,
        channel_ref: object,
    ) -> ChannelAccess:
        del settings, actor, channel_ref
        channel = await session.get(Channel, (channel_id, domain))
        guild = await session.get(Guild, (guild_id, domain))
        assert channel is not None and guild is not None
        return ChannelAccess(channel=channel, guild=guild, participants=[])

    async def lock_access(
        session: AsyncSession,
        settings: object,
        access: ChannelAccess,
    ) -> ChannelAccess:
        del settings, access
        channel = await session.scalar(
            select(Channel)
            .where(Channel.id == channel_id, Channel.origin_domain == domain)
            .with_for_update()
        )
        guild = await session.get(Guild, (guild_id, domain))
        assert channel is not None and guild is not None
        return ChannelAccess(channel=channel, guild=guild, participants=[])

    async def allow_permissions(*args: object, **kwargs: object) -> Permission:
        del args, kwargs
        return Permission.SEND_MESSAGES | Permission.EMBED_LINKS

    async def signer(*args: object, **kwargs: object) -> User:
        del kwargs
        return args[-1]  # type: ignore[return-value]

    monkeypatch.setattr(channels, "enforce_client_rate_limit", noop)
    monkeypatch.setattr(channels, "load_channel_access", access_for)
    monkeypatch.setattr(channels, "lock_terminal_room", noop)
    monkeypatch.setattr(channels, "lock_local_channel_mutation", lock_access)
    monkeypatch.setattr(channels, "require_channel_permissions", allow_permissions)
    monkeypatch.setattr(channels, "validate_custom_emoji_use", noop)
    monkeypatch.setattr(channels, "validate_custom_sticker_use", noop)
    monkeypatch.setattr(channels, "require_dm_send", noop)
    monkeypatch.setattr(channels, "role_mention_recipients", no_roles)
    monkeypatch.setattr(channels, "evaluate_automod_message", AsyncMock(return_value=None))
    monkeypatch.setattr(channels, "guild_mutation_signer", signer)
    monkeypatch.setattr(channels, "remote_destinations_with_channel_access", empty)
    monkeypatch.setattr(channels, "publish_channel_dispatch", noop)
    monkeypatch.setattr(channels, "enqueue_best_effort", noop)
    monkeypatch.setattr(interactions, "publish_interaction_response_event", noop)
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    settings = SimpleNamespace(domain=domain)

    principal: BotPrincipal | None = None
    callback_tasks: list[asyncio.Task[object]] = []
    try:
        async with engine.begin() as connection:
            await ensure_message_partitions(connection)
        async with sessions() as session:
            await cleanup_domain(session, domain)
            session.add(Instance(domain=domain, is_self=False))
            await session.flush()
            owner = User(
                id=owner_id,
                origin_domain=domain,
                is_local=False,
                account_type="bot",
                username="atomicity.owner",
                federation_introduced_by_domain=domain,
            )
            bot = User(
                id=bot_id,
                origin_domain=domain,
                is_local=False,
                account_type="bot",
                username="atomicity.bot",
                federation_introduced_by_domain=domain,
            )
            session.add_all([owner, bot])
            await session.flush()
            session.add(
                DeveloperTeam(
                    id=owner_id,
                    origin_domain=domain,
                    name="Atomicity integration",
                )
            )
            await session.flush()
            application = BotApplication(
                id=application_id,
                origin_domain=domain,
                team_id=owner_id,
                team_domain=domain,
                bot_user_id=bot_id,
                bot_user_domain=domain,
                name="Atomicity",
                status="active",
            )
            session.add(application)
            await session.flush()
            session.add(
                ApplicationCommand(
                    id=command_id,
                    application_id=application_id,
                    application_domain=domain,
                    name="atomicity",
                    type="chat_input",
                    definition={"description": "atomic callback"},
                    contexts=["guild"],
                    integration_types=["guild_install"],
                    generation=1,
                    state="active",
                )
            )
            await session.flush()
            guild = Guild(
                id=guild_id,
                origin_domain=domain,
                name="Interaction atomicity integration",
                owner_id=owner_id,
                owner_domain=domain,
            )
            session.add_all(
                [
                    guild,
                    GuildMember(
                        guild_id=guild_id,
                        guild_domain=domain,
                        user_id=owner_id,
                        user_domain=domain,
                        joined_at=now,
                    ),
                    GuildMember(
                        guild_id=guild_id,
                        guild_domain=domain,
                        user_id=bot_id,
                        user_domain=domain,
                        joined_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                Channel(
                    id=channel_id,
                    origin_domain=domain,
                    guild_id=guild_id,
                    guild_domain=domain,
                    type=0,
                    name="atomicity",
                    created_floor_id=channel_id,
                )
            )
            await session.flush()
            installation = BotInstallation(
                id=installation_id,
                application_id=application_id,
                application_domain=domain,
                guild_id=guild_id,
                guild_domain=domain,
                bot_user_id=bot_id,
                bot_user_domain=domain,
                installer_id=owner_id,
                installer_domain=domain,
                granted_scopes=["interactions.respond"],
                granted_intents=["interactions"],
                status="active",
            )
            session.add(installation)
            await session.flush()
            for pending_id in (interaction_id, rollback_interaction_id):
                session.add(
                    BotInteraction(
                        id=pending_id,
                        application_id=application_id,
                        application_domain=domain,
                        installation_id=installation_id,
                        installation_revision=installation.grant_revision,
                        guild_id=guild_id,
                        guild_domain=domain,
                        channel_id=channel_id,
                        channel_domain=domain,
                        user_id=owner_id,
                        user_domain=domain,
                        interaction_type="command",
                        context="guild",
                        integration_type="guild_install",
                        invocation_channel_type=0,
                        command_id=command_id,
                        command_name="atomicity",
                        command_type="chat_input",
                        payload={},
                        token_hash=hashlib.sha256(interaction_token.encode()).digest(),
                        status="pending",
                        expires_at=now + timedelta(minutes=5),
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.commit()

            worker = BotWorker(
                id=await ids.mint(),
                application_id=application_id,
                application_domain=domain,
                name="integration",
                public_key=b"x" * 32,
            )
            token = BotToken(
                id=await ids.mint(),
                token_hash=b"x" * 32,
                application_id=application_id,
                application_domain=domain,
                worker_id=worker.id,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            principal = BotPrincipal(
                user=bot,
                application=application,
                worker=worker,
                token=token,
                scopes=frozenset({"interactions.respond"}),
                intents=frozenset({"interactions"}),
                interaction_token=interaction_token,
            )

        original_bot_interaction = interactions.bot_interaction
        first_locked = asyncio.Event()
        release_first = asyncio.Event()
        first_pending = True

        async def pause_first_lock(*args: object, **kwargs: object) -> tuple[object, object]:
            nonlocal first_pending
            result = await original_bot_interaction(*args, **kwargs)  # type: ignore[arg-type]
            if first_pending:
                first_pending = False
                first_locked.set()
                await release_first.wait()
            return result

        monkeypatch.setattr(interactions, "bot_interaction", pause_first_lock)

        async def callback() -> object:
            assert principal is not None
            async with sessions() as session:
                try:
                    return await interactions.callback_interaction(
                        interaction_id,
                        interactions.InteractionCallback(
                            type=4,
                            data={"content": "race winner"},
                        ),
                        Response(),
                        principal,
                        session,
                        redis,
                        ids,
                        settings,
                        True,
                    )
                except HTTPException as exc:
                    await session.rollback()
                    return exc

        first = asyncio.create_task(callback())
        callback_tasks.append(first)
        await asyncio.wait_for(first_locked.wait(), timeout=5)
        second = asyncio.create_task(callback())
        callback_tasks.append(second)
        await asyncio.sleep(0.05)
        assert not second.done(), "the competing callback did not wait on the interaction row lock"
        release_first.set()
        results = await asyncio.gather(first, second)

        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, HTTPException)]
        assert len(successes) == 1, repr(results)
        assert len(failures) == 1
        assert failures[0].status_code == 409
        assert failures[0].detail["code"] == "INTERACTION_ALREADY_ACKNOWLEDGED"

        async with sessions() as session:
            messages = list(
                await session.scalars(
                    select(Message).where(
                        Message.origin_domain == domain,
                        Message.content == "race winner",
                    )
                )
            )
            responses = list(
                await session.scalars(
                    select(BotInteractionResponse).where(
                        BotInteractionResponse.interaction_id == interaction_id
                    )
                )
            )
            interaction = await session.get(BotInteraction, interaction_id)
            assert len(messages) == 1
            assert len(responses) == 1
            assert interaction is not None and interaction.status == "responded"
            assert responses[0].message_id == messages[0].id
            assert interaction.response_message_id == messages[0].id

        monkeypatch.setattr(interactions, "bot_interaction", original_bot_interaction)
        rollback_message_id = await ids.mint()
        failing_ids = FailAfterMessageSnowflake(rollback_message_id)
        redis.eval.reset_mock()
        assert principal is not None
        async with sessions() as session:
            with pytest.raises(RuntimeError, match="injected response-id failure"):
                await interactions.callback_interaction(
                    rollback_interaction_id,
                    interactions.InteractionCallback(
                        type=4,
                        data={"content": "must roll back"},
                    ),
                    Response(),
                    principal,
                    session,
                    redis,
                    failing_ids,
                    settings,
                    True,
                )
            await session.rollback()

        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.id == rollback_message_id, Message.origin_domain == domain)
                )
                == 0
            )
            rolled_back = await session.get(BotInteraction, rollback_interaction_id)
            assert rolled_back is not None and rolled_back.status == "pending"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BotInteractionResponse)
                    .where(BotInteractionResponse.interaction_id == rollback_interaction_id)
                )
                == 0
            )
        redis.eval.assert_not_awaited()
    finally:
        for task in callback_tasks:
            if not task.done():
                task.cancel()
        if callback_tasks:
            await asyncio.gather(*callback_tasks, return_exceptions=True)
        async with sessions() as session:
            await cleanup_domain(session, domain)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_installation", [False, True])
async def test_runtime_suspend_serializes_before_user_install_create_or_reactivation(
    monkeypatch: pytest.MonkeyPatch,
    existing_installation: bool,
) -> None:
    """A target deny holding the app lock wins over a concurrent local grant."""

    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app_domain = "user-install-race-app-it.example"
    target_domain = "user-install-race-target-it.example"
    ids = AtomicSnowflake(sequence=700 if existing_installation else 600)
    team_id = await ids.mint()
    bot_id = await ids.mint()
    user_id = await ids.mint()
    application_id = await ids.mint()
    command_id = await ids.mint()
    installation_id = await ids.mint()

    async def cleanup(session: AsyncSession) -> None:
        await session.execute(
            delete(BotApplication).where(BotApplication.origin_domain == app_domain)
        )
        await session.execute(
            delete(DeveloperTeam).where(DeveloperTeam.origin_domain == app_domain)
        )
        await session.execute(
            delete(User).where(User.origin_domain.in_([app_domain, target_domain]))
        )
        await session.execute(
            delete(Instance).where(Instance.domain.in_([app_domain, target_domain]))
        )
        await session.commit()

    async def noop(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def no_destinations(*args: object, **kwargs: object) -> set[str]:
        del args, kwargs
        return set()

    monkeypatch.setattr(interactions, "refresh_user_bot_application", noop)
    monkeypatch.setattr(
        interactions,
        "queue_application_target_snapshots_for_refs",
        no_destinations,
    )
    monkeypatch.setattr(interactions, "wake_application_target_deliveries", noop)
    monkeypatch.setattr(interactions, "publish_e2ee_policy_updates", noop)
    monkeypatch.setattr(interactions, "revoke_bot_e2ee_access", AsyncMock(return_value=[]))

    actor: User | None = None
    try:
        async with sessions() as session:
            await cleanup(session)
            session.add_all(
                [
                    Instance(domain=app_domain, is_self=False),
                    Instance(
                        domain=target_domain,
                        is_self=True,
                        current_key_id="ed25519:test",
                        encrypted_private_key=b"test-only",
                        private_key_nonce=b"n" * 12,
                    ),
                ]
            )
            await session.flush()
            actor = User(
                id=user_id,
                origin_domain=target_domain,
                is_local=True,
                account_type="human",
                username="install.race.user",
                password_hash="test-only-password-hash",
                password_kdf_version=2,
                password_auth_salt=b"a" * 16,
                e2ee_vault_salt=b"v" * 16,
            )
            bot = User(
                id=bot_id,
                origin_domain=app_domain,
                is_local=False,
                account_type="bot",
                username="install.race.bot",
                federation_introduced_by_domain=app_domain,
            )
            session.add_all([actor, bot])
            await session.flush()
            session.add(
                DeveloperTeam(
                    id=team_id,
                    origin_domain=app_domain,
                    name="Install race",
                )
            )
            await session.flush()
            session.add(
                BotApplication(
                    id=application_id,
                    origin_domain=app_domain,
                    team_id=team_id,
                    team_domain=app_domain,
                    bot_user_id=bot_id,
                    bot_user_domain=app_domain,
                    name="Install race",
                    status="active",
                    target_policy="open",
                    default_scopes=["applications.commands", "interactions.respond"],
                    default_intents=["interactions"],
                    supported_install_types=["user_install"],
                    user_install_scopes=["applications.commands", "interactions.respond"],
                    user_install_contexts=["bot_dm"],
                )
            )
            await session.flush()
            session.add_all(
                [
                    ApplicationCommand(
                        id=command_id,
                        application_id=application_id,
                        application_domain=app_domain,
                        name="race",
                        type="chat_input",
                        definition={"description": "race"},
                        contexts=["bot_dm"],
                        integration_types=["user_install"],
                        generation=1,
                        state="active",
                    ),
                    BotApplicationTarget(
                        application_id=application_id,
                        application_domain=app_domain,
                        target_domain=target_domain,
                        generation=1,
                        runtime_manifest_generation=1,
                        runtime_revocation_generation=1,
                        runtime_status="active",
                        runtime_target_allowed=True,
                    ),
                ]
            )
            if existing_installation:
                session.add(
                    BotUserInstallation(
                        id=installation_id,
                        source_id=installation_id,
                        source_domain=target_domain,
                        application_id=application_id,
                        application_domain=app_domain,
                        user_id=user_id,
                        user_domain=target_domain,
                        granted_scopes=["applications.commands", "interactions.respond"],
                        granted_intents=["interactions"],
                        contexts=["bot_dm"],
                        grant_revision=1,
                        status="suspended",
                    )
                )
            await session.commit()

        deny_locked = asyncio.Event()
        release_deny = asyncio.Event()

        async def suspend_target() -> None:
            async with sessions() as session:
                application = await session.scalar(
                    select(BotApplication)
                    .where(
                        BotApplication.id == application_id,
                        BotApplication.origin_domain == app_domain,
                    )
                    .with_for_update()
                )
                assert application is not None
                deny_locked.set()
                await release_deny.wait()
                target = await session.scalar(
                    select(BotApplicationTarget)
                    .where(
                        BotApplicationTarget.application_id == application_id,
                        BotApplicationTarget.application_domain == app_domain,
                        BotApplicationTarget.target_domain == target_domain,
                    )
                    .with_for_update()
                )
                assert target is not None
                target.runtime_status = "suspended"
                target.runtime_revocation_generation += 1
                await session.commit()

        async def install() -> object:
            assert actor is not None
            async with sessions() as session:
                try:
                    return await interactions.create_user_installation(
                        interactions.UserInstallationCreate(
                            application_ref=f"{application_id}@{app_domain}",
                            contexts=["bot_dm"],
                        ),
                        SimpleNamespace(user=actor),
                        session,
                        SimpleNamespace(),
                        ids,
                        SimpleNamespace(domain=target_domain),
                    )
                except Exception as exc:
                    await session.rollback()
                    return exc

        deny_task = asyncio.create_task(suspend_target())
        await asyncio.wait_for(deny_locked.wait(), timeout=5)
        install_task = asyncio.create_task(install())
        await asyncio.sleep(0.05)
        assert not install_task.done(), "install did not wait for the application row lock"
        release_deny.set()
        await deny_task
        result = await install_task
        assert isinstance(result, HTTPException)
        assert result.status_code == 403
        assert result.detail["code"] == "APPLICATION_TARGET_NOT_ALLOWED"

        async with sessions() as session:
            rows = list(
                await session.scalars(
                    select(BotUserInstallation).where(
                        BotUserInstallation.application_id == application_id,
                        BotUserInstallation.application_domain == app_domain,
                        BotUserInstallation.user_id == user_id,
                        BotUserInstallation.user_domain == target_domain,
                    )
                )
            )
            if existing_installation:
                assert len(rows) == 1 and rows[0].status == "suspended"
                assert rows[0].grant_revision == 1
            else:
                assert rows == []
    finally:
        async with sessions() as session:
            await cleanup(session)
        await engine.dispose()
