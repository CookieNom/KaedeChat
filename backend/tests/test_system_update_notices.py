from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.update_notices import UpdateNotice, deliver_update_notice
from app.chat.payloads import user_payload
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.db.bot_models import InstanceAdminGrant
from app.db.models import Channel, Instance, Message, SystemUpdateNotice, User
from app.federation.replication import profile_from_user, upsert_remote_user
from app.federation.schemas import RemoteUserProfile

from .test_settings import settings


def notice(**changes):
    return UpdateNotice.model_validate(
        {
            "revision": "a" * 40,
            "current_revision": "b" * 40,
            "maintenance": "required",
            "reasons": ["database"],
            **changes,
        }
    )


def test_federation_cannot_claim_system_account_type_or_export_local_system():
    with pytest.raises(ValidationError):
        RemoteUserProfile.model_validate(
            {
                "id": "123",
                "origin_domain": "evil.example",
                "username": "kaede_system",
                "account_type": "system",
            }
        )
    with pytest.raises(ValueError, match="cannot participate in federation"):
        profile_from_user(User(account_type="system"))
    forged = User(
        id=123,
        origin_domain="evil.example",
        is_local=False,
        account_type="system",
        username="kaede_system",
        display_name="Kaede System",
    )
    assert user_payload(forged)["account_type"] == "human"


async def test_federation_cannot_reference_local_system_as_a_human():
    cfg = settings()
    system = User(
        id=123,
        origin_domain=cfg.domain,
        is_local=True,
        account_type="system",
        username="kaede_system",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=system))
    profile = RemoteUserProfile(
        id="123", origin_domain=cfg.domain, username="kaede_system", account_type="human"
    )
    with pytest.raises(HTTPException) as error:
        await upsert_remote_user(session, cfg, profile)
    assert error.value.status_code == 404


async def test_system_dm_rejects_replies_before_privacy_or_encryption_work():
    from app.api.channels import require_dm_send
    from app.api.e2ee import room_participants
    from app.chat.channel_access import ChannelAccess

    system = User(account_type="system", is_local=True)
    owner = User(account_type="human", is_local=True)
    access = ChannelAccess(channel=Channel(type=1), guild=None, participants=[system, owner])
    session = AsyncMock()
    for call in (require_dm_send(session, access, owner), room_participants(session, None, access)):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.detail["code"] == "SYSTEM_CONVERSATION_READ_ONLY"
    session.execute.assert_not_called()


def test_notice_rejects_untrusted_text_and_never_calls_unknown_safe():
    with pytest.raises(ValidationError):
        notice(revision="$(curl attacker)")
    with pytest.raises(ValidationError):
        notice(reasons=["Click this malicious link"])
    assert (
        "could not be verified" in notice(maintenance="unknown", reasons=["unverified"]).message()
    )
    assert "Maintenance required" in notice().message()


async def test_delivery_receipts_owner_selection_and_database_locality(postgres_schema, migrate_to):
    await migrate_to("head")
    cfg = settings()
    now = datetime.now(UTC)
    base = (int(now.timestamp() * 1000) - EPOCH_MS) << (WORKER_BITS + SEQUENCE_BITS)
    snowflake = SimpleNamespace(mint=AsyncMock(side_effect=iter(range(base, base + 100))))
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        session.add_all(
            [
                Instance(
                    domain=cfg.domain,
                    is_self=True,
                    current_key_id="test",
                    encrypted_private_key=b"test",
                    private_key_nonce=b"a" * 12,
                ),
                Instance(domain="evil.example", is_self=False),
            ]
        )
        await session.flush()
        session.add_all(
            [
                User(
                    id=i,
                    origin_domain=cfg.domain,
                    username=f"owner{i}",
                    is_local=True,
                    account_type="human",
                    password_hash="test",
                    password_kdf_version=2,
                    password_auth_salt=b"a" * 16,
                    e2ee_vault_salt=b"b" * 16,
                )
                for i in range(1, 4)
            ]
        )
        await session.flush()
        session.add_all(
            [
                InstanceAdminGrant(
                    id=10, user_id=1, user_domain=cfg.domain, user_is_local=True, role="owner"
                ),
                InstanceAdminGrant(
                    id=11,
                    user_id=2,
                    user_domain=cfg.domain,
                    user_is_local=True,
                    role="owner",
                    revoked_at=now,
                ),
                InstanceAdminGrant(
                    id=12,
                    user_id=3,
                    user_domain=cfg.domain,
                    user_is_local=True,
                    role="administrator",
                ),
            ]
        )
        await session.flush()
        assert await deliver_update_notice(session, cfg, snowflake, notice()) == 1
        await session.commit()
        assert await deliver_update_notice(session, cfg, snowflake, notice()) == 0
        system = await session.scalar(select(User).where(User.account_type == "system"))
        assert system.is_local and system.password_hash is None and system.email is None
        message = await session.scalar(select(Message).where(Message.author_id == system.id))
        channel = await session.get(Channel, (message.channel_id, cfg.domain))
        assert message.e2ee is None and channel.encryption_mode == "plaintext"
        assert await session.scalar(select(func.count()).select_from(SystemUpdateNotice)) == 1
        # Removing the visible message does not allow the timer to spam a duplicate.
        message.deleted_at = now
        message.content = None
        await session.flush()
        assert await deliver_update_notice(session, cfg, snowflake, notice()) == 0
        assert await deliver_update_notice(session, cfg, snowflake, notice(revision="c" * 40)) == 1
        with pytest.raises(IntegrityError), session.no_autoflush:
            async with session.begin_nested():
                session.add(
                    User(
                        id=999,
                        origin_domain="evil.example",
                        is_local=False,
                        account_type="system",
                        username="kaede_system",
                        federation_introduced_by_domain="evil.example",
                    )
                )
                await session.flush()


async def test_even_a_session_token_cannot_authenticate_the_system_account(monkeypatch):
    from app.api import dependencies

    cfg = settings()
    system = User(id=123, origin_domain=cfg.domain, is_local=True, account_type="system")
    grant = SimpleNamespace(session_id=1, user_id=123, user_domain=cfg.domain)
    store = SimpleNamespace(get=AsyncMock(return_value=grant))
    monkeypatch.setattr(dependencies, "AccessTokenStore", lambda *_: store)
    request = SimpleNamespace(headers={"Authorization": "Bearer test-token"}, cookies={})
    session = SimpleNamespace(scalar=AsyncMock(return_value=system))
    with pytest.raises(HTTPException) as error:
        await dependencies.require_user(request, session, None, cfg)
    assert error.value.status_code == 401


async def test_federated_history_and_reactions_cannot_use_system_reference():
    from app.federation.guilds import _apply_reaction_mutation
    from app.federation.history import _ensure_history_identity

    cfg = settings()
    system = User(id=123, origin_domain=cfg.domain, is_local=True, account_type="system")
    session = SimpleNamespace(get=AsyncMock(return_value=system))
    with pytest.raises(ValueError, match="system accounts"):
        await _ensure_history_identity(
            session, cfg, 123, cfg.domain, authority_origin="evil.example"
        )
    with pytest.raises(ValueError, match="system accounts"):
        await _apply_reaction_mutation(
            session,
            None,
            "guild.reaction.add",
            {
                "message": {"id": "321", "origin_domain": "evil.example"},
                "user": {"id": "123", "origin_domain": cfg.domain},
                "emoji": "👍",
            },
            {"channel_id": "456", "channel_domain": "evil.example"},
        )
