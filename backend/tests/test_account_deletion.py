from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.api import account_deletion as api
from app.auth.account_status import account_is_banned
from app.auth.deletion import erase_message_body
from app.auth.schemas import MfaSetupRequest
from app.db.models import User


@pytest.fixture
def deletion_request(monkeypatch):
    user = User(
        id=7,
        origin_domain="alpha.localhost",
        is_local=True,
        username="reserved",
        email="reserved@example.test",
        password_hash="old-hash",
        password_kdf_version=2,
        password_auth_salt=bytes(16),
        e2ee_vault_salt=bytes(16),
        profile_version=1,
        disabled_at=None,
        deleted_at=None,
        content_deletion=None,
    )
    session = AsyncMock()
    session.scalar.side_effect = [user, None, None]
    for name in ("lock_current_session", "verify_submitted_password", "verify_mfa_code"):
        monkeypatch.setattr(api, name, AsyncMock(return_value=True))
    monkeypatch.setattr(api, "submitted_password_protocol_matches", lambda *_: True)
    monkeypatch.setattr(api, "client_ip", lambda *_: "127.0.0.1")
    monkeypatch.setattr(api, "mfa_attempt_locked", AsyncMock(return_value=False))
    for name in (
        "record_mfa_verification_failure",
        "clear_mfa_account_failures",
        "revoke_user_sessions",
        "enqueue_best_effort",
    ):
        monkeypatch.setattr(api, name, AsyncMock())
    monkeypatch.setattr(
        "app.federation.relationships.queue_profile_updates", AsyncMock(return_value=set())
    )
    return user, session


async def call_delete(user, session, *, account=True, code=None):
    request = Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": "/api/v1/users/@me" + ("" if account else "/content"),
            "headers": [],
        }
    )
    return await api.request_deletion(
        request,
        MfaSetupRequest(password="a" * 43, password_kdf_version=2, current_code=code),
        SimpleNamespace(user=user),
        session,
        AsyncMock(),
        SimpleNamespace(domain=user.origin_domain),
    )


@pytest.mark.asyncio
async def test_delete_reserves_identity_erases_credentials_and_revokes_sessions(deletion_request):
    user, session = deletion_request
    result = await call_delete(user, session)
    assert result == {"status": "pending"}
    assert (user.email, user.username) == ("reserved@example.test", "reserved")
    assert user.password_hash is user.password_auth_salt is user.e2ee_vault_salt is None
    assert user.password_kdf_version is None
    assert account_is_banned(user)
    # Clearing an administrative ban must never resurrect a deleted identity.
    user.disabled_at = None
    assert account_is_banned(user)
    api.revoke_user_sessions.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["password", "mfa", "owner", "pending", "session"])
async def test_guards_prevent_destructive_changes(deletion_request, guard):
    user, session = deletion_request
    if guard == "password":
        api.verify_submitted_password.return_value = False
    elif guard == "mfa":
        user.totp_secret_encrypted = b"secret"
    elif guard == "owner":
        session.scalar.side_effect = [user, 99]
    elif guard == "pending":
        user.content_deletion = {"status": "pending"}
    else:
        api.lock_current_session.return_value = False
    with pytest.raises(HTTPException):
        await call_delete(user, session)
    session.commit.assert_not_awaited()
    assert user.deleted_at is None
    assert user.password_hash == "old-hash"
    api.enqueue_best_effort.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_only_keeps_credentials_and_account(deletion_request):
    user, session = deletion_request
    await call_delete(user, session, account=False)
    assert user.deleted_at is None
    assert user.password_hash == "old-hash"
    assert user.content_deletion["status"] == "pending"
    api.revoke_user_sessions.assert_not_awaited()


def test_message_scrub_removes_rich_and_encrypted_bodies():
    message = SimpleNamespace(
        content="private",
        e2ee={"ciphertext": "secret"},
        embeds=[{"description": "private"}],
        components=[{}],
        sticker_items=[{}],
        forward_snapshot={"content": "private"},
        poll_result={},
        mention_user_refs=[{}],
        mention_role_refs=[{}],
        mention_everyone=True,
    )
    erase_message_body(message)
    assert (
        message.content is message.e2ee is message.forward_snapshot is message.poll_result is None
    )
    assert message.embeds == message.components == message.sticker_items == []
    assert message.mention_user_refs == message.mention_role_refs == []
    assert not message.mention_everyone


@pytest.mark.asyncio
async def test_tombstone_database_reserves_username_email_and_survives_expiry(migrate_to):
    connection = await migrate_to("a6e2d4f80931")
    await connection.execute(
        text(
            "INSERT INTO instances (domain, is_self, current_key_id, encrypted_private_key, "
            "private_key_nonce) VALUES ('alpha.localhost', true, 'test', 'x', 'x')"
        )
    )
    # Use the real model/constraints; a tombstone holds no password material.
    async with AsyncSession(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    ) as session:
        user = User(
            id=7,
            origin_domain="alpha.localhost",
            is_local=True,
            username="reserved",
            email="reserved@example.test",
            deleted_at=datetime.now(UTC),
            disabled_at=datetime.now(UTC),
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session.add(user)
        await session.flush()
        assert await session.scalar(select(User.password_hash).where(User.id == 7)) is None
        from app.tasks import purge_unverified_accounts_in_session

        assert (
            await purge_unverified_accounts_in_session(
                session, SimpleNamespace(email_backend="smtp", verification_ttl_hours=24)
            )
            == 0
        )
        from sqlalchemy.exc import IntegrityError

        for username, email in [
            ("reserved", "other@example.test"),
            ("another", "RESERVED@example.test"),
        ]:
            with pytest.raises(IntegrityError), session.no_autoflush:
                async with session.begin_nested():
                    session.add(
                        User(
                            id=8,
                            origin_domain="alpha.localhost",
                            is_local=True,
                            username=username,
                            email=email,
                            deleted_at=datetime.now(UTC),
                        )
                    )
                    await session.flush()


@pytest.mark.asyncio
async def test_cleanup_resumes_and_preserves_other_authors_and_new_content(migrate_to, monkeypatch):
    from datetime import timedelta

    from app.auth.deletion import erase_content_batch
    from app.db import models as m

    connection = await migrate_to("a6e2d4f80931")
    monkeypatch.setattr(
        "app.api.channels.queue_dm_authority_mutation", AsyncMock(return_value=set())
    )
    monkeypatch.setattr("app.api.channels.publish_channel_dispatch", AsyncMock())
    monkeypatch.setattr("app.core.task_wake.enqueue_best_effort", AsyncMock())
    remote_delete = AsyncMock(side_effect=HTTPException(502, "Remote server offline"))
    monkeypatch.setattr("app.api.channels.proxy_remote_dm_message_operation", remote_delete)
    cutoff = datetime.now(UTC)
    async with AsyncSession(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    ) as session:
        session.add(
            m.Instance(
                domain="alpha.localhost",
                is_self=True,
                current_key_id="test",
                encrypted_private_key=b"x",
                private_key_nonce=b"x",
            )
        )
        await session.flush()
        users = [
            m.User(
                id=id,
                origin_domain="alpha.localhost",
                is_local=True,
                username=f"user{id}",
                password_hash="hash",
                password_kdf_version=2,
                password_auth_salt=bytes(16),
                e2ee_vault_salt=bytes(16),
            )
            for id in (7, 8)
        ]
        session.add_all(users)
        await session.flush()
        user = users[0]
        session.add(m.UserSettings(user_id=7, user_domain="alpha.localhost"))
        user.content_deletion = {
            "status": "pending",
            "before": cutoff.isoformat(),
            "remote_failures": 0,
        }
        session.add(m.Channel(id=20, origin_domain="alpha.localhost", type=1, created_floor_id=20))
        session.add(
            m.DMConversation(
                id=20,
                origin_domain="alpha.localhost",
                authority_domain="alpha.localhost",
                pair_key="a" * 64,
                type="direct",
            )
        )
        await session.flush()
        session.add_all(
            [
                m.DMParticipant(
                    conversation_id=20,
                    conversation_domain="alpha.localhost",
                    user_id=id,
                    user_domain="alpha.localhost",
                )
                for id in (7, 8)
            ]
        )
        for id, author, created in [
            (30, 7, cutoff - timedelta(seconds=1)),
            (31, 8, cutoff - timedelta(seconds=1)),
            (32, 7, cutoff + timedelta(seconds=1)),
        ]:
            session.add(
                m.Message(
                    id=id,
                    origin_domain="alpha.localhost",
                    channel_id=20,
                    channel_domain="alpha.localhost",
                    author_id=author,
                    author_domain="alpha.localhost",
                    content="private text",
                    embeds=[{"description": "private embed"}],
                    created_at=created,
                )
            )
        session.add(m.Instance(domain="remote.test", is_self=False))
        await session.flush()
        session.add(m.Channel(id=21, origin_domain="remote.test", type=1, created_floor_id=21))
        session.add(
            m.DMConversation(
                id=21,
                origin_domain="remote.test",
                authority_domain="remote.test",
                pair_key="b" * 64,
                type="direct",
            )
        )
        await session.flush()
        session.add(
            m.Message(
                id=33,
                origin_domain="remote.test",
                channel_id=21,
                channel_domain="remote.test",
                author_id=7,
                author_domain="alpha.localhost",
                content="remote private text",
                created_at=cutoff - timedelta(seconds=1),
            )
        )
        await session.commit()
        settings = SimpleNamespace(domain="alpha.localhost", access_token_ttl_seconds=900)
        await erase_content_batch(session, AsyncMock(), settings, user)
        assert user.content_deletion["status"] == "pending"
        old = await session.get(m.Message, (30, "alpha.localhost"))
        assert old.content is None and old.embeds == [] and old.deleted_at is not None
        await erase_content_batch(session, AsyncMock(), settings, user)
        assert user.content_deletion["status"] == "complete"
        remote_delete.assert_awaited_once()
        assert user.content_deletion["remote_failures"] == 1
        assert (await session.get(m.Message, (33, "remote.test"))).content is None
        for id in (31, 32):
            retained = await session.get(m.Message, (id, "alpha.localhost"))
            assert retained.content == "private text" and retained.deleted_at is None

        # Quarantined federation replicas must not strand private account cleanup.
        guild = m.Guild(
            id=40,
            origin_domain="remote.test",
            name="Unavailable guild",
            owner_id=8,
            owner_domain="alpha.localhost",
            unavailable=True,
            sync_status="failed",
        )
        session.add(guild)
        await session.flush()
        session.add_all(
            m.GuildMember(
                guild_id=40,
                guild_domain="remote.test",
                user_id=id,
                user_domain="alpha.localhost",
                joined_at=cutoff,
            )
            for id in (7, 8)
        )
        await session.flush()
        from app.api.guild_lifecycle import _locked_guild
        from app.core.types import EntityRef

        with pytest.raises(HTTPException):
            await _locked_guild(session, settings, EntityRef("40@remote.test"))
        leave = {"type": "guild.leave.request"}
        build_leave = AsyncMock(return_value=leave)
        queue_leave = AsyncMock()
        monkeypatch.setattr("app.api.guild_lifecycle.build_envelope", build_leave)
        monkeypatch.setattr("app.api.guild_lifecycle.queue_event", queue_leave)

        user.deleted_at = cutoff
        user.disabled_at = cutoff
        user.password_hash = user.password_kdf_version = None
        user.password_auth_salt = user.e2ee_vault_salt = None
        user.content_deletion = {
            "status": "pending",
            "before": cutoff.isoformat(),
            "remote_failures": 0,
        }
        await session.commit()
        await erase_content_batch(session, AsyncMock(), settings, user)
        await erase_content_batch(session, AsyncMock(), settings, user)
        assert user.content_deletion["status"] == "complete"
        assert (await session.get(m.Message, (32, "alpha.localhost"))).deleted_at is not None
        assert await session.get(m.UserSettings, (7, "alpha.localhost")) is None
        assert (await session.get(m.Message, (31, "alpha.localhost"))).content == "private text"
        assert await session.get(m.GuildMember, (40, "remote.test", 7, "alpha.localhost")) is None
        assert (
            await session.get(m.GuildMember, (40, "remote.test", 8, "alpha.localhost")) is not None
        )
        build_leave.assert_awaited_once()
        assert build_leave.await_args.args[2] == "guild.leave.request"
        queue_leave.assert_awaited_once_with(session, settings, "remote.test", leave)
