import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import e2ee
from app.chat.e2ee_membership import pause_local_e2ee_for_device_change
from app.core.types import EntityRef
from app.db.models import Channel, DMConversation, DMParticipant, Instance, Message, User


@pytest.mark.parametrize("kind", ["direct", "group"])
async def test_unanimous_continuation_commits_once_and_closes_old_room(monkeypatch, kind):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable migrated PostgreSQL database")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    domain = f"dm-{uuid4().hex}.example"
    lookup = uuid4().hex * 2
    ids = [1, 2] if kind == "direct" else [1, 2, 3]
    users = [
        User(
            id=i,
            origin_domain=domain,
            username=f"user{i}",
            is_local=False,
            federation_introduced_by_domain=domain,
        )
        for i in ids
    ]
    settings = SimpleNamespace(domain=domain, voice_enabled=False)
    source = EntityRef(f"10@{domain}")
    mint = SimpleNamespace(mint=AsyncMock(return_value=20))
    monkeypatch.setattr(e2ee, "publish_dispatch", AsyncMock())

    async def vote(user, action, token=None):
        async with sessions() as session:
            response = await e2ee.dm_plaintext_continuation_consent(
                source,
                e2ee.DMEncryptionConsentRequest(action=action, request_id=token),
                SimpleNamespace(user=user),
                session,
                settings,
            )
            return response["request"]

    async def commit(user, token):
        async with sessions() as session:
            return await e2ee.commit_dm_continuation(
                source,
                e2ee.ContinuationCommit(request_id=token),
                SimpleNamespace(user=user),
                session,
                settings,
                mint,
                AsyncMock(),
            )

    try:
        async with sessions.begin() as session:
            session.add(Instance(domain=domain, is_self=False))
            await session.flush()
            session.add_all(users)
            await session.flush()
            session.add(
                Channel(
                    id=10,
                    origin_domain=domain,
                    type=1,
                    created_floor_id=10,
                    encryption_mode="e2ee",
                    encryption_state="active",
                    encryption_policy_generation=1,
                    encryption_protocol="mls10",
                    encryption_suite=e2ee.E2EE_SUITE_MLS_128,
                    encryption_epoch=1,
                    encryption_group_id="group",
                )
            )
            session.add(
                DMConversation(
                    id=10,
                    origin_domain=domain,
                    authority_domain=domain,
                    pair_key=lookup,
                    type=kind,
                    owner_id=1 if kind == "group" else None,
                    owner_domain=domain if kind == "group" else None,
                )
            )
            await session.flush()
            session.add_all(
                DMParticipant(
                    conversation_id=10, conversation_domain=domain, user_id=i, user_domain=domain
                )
                for i in ids
            )
            session.add(
                Message(
                    id=30,
                    origin_domain=domain,
                    channel_id=10,
                    channel_domain=domain,
                    author_id=1,
                    author_domain=domain,
                    e2ee={"version": 2},
                    encryption_policy_generation=1,
                    encryption_epoch=1,
                )
            )
        token = (await vote(users[0], "request"))["request_id"]
        with pytest.raises(e2ee.HTTPException, match="E2EE_DM_REQUEST_STALE"):
            await commit(users[0], token)
        await asyncio.gather(*(vote(user, "agree", token) for user in users[1:]))
        results = await asyncio.gather(*(commit(user, token) for user in users))
        assert results == [{"channel_ref": f"20@{domain}"}] * len(users)
        mint.mint.assert_awaited_once()
        async with sessions.begin() as session:
            old = await session.get(Channel, (10, domain))
            new = await session.get(Channel, (20, domain))
            assert (old.encryption_mode, old.dm_successor_id) == ("e2ee", 20)
            assert (new.encryption_mode, new.dm_previous_id) == ("plaintext", 10)
            assert await pause_local_e2ee_for_device_change(session, settings, users[0]) == []
            previous = await e2ee.previous_dm_segment(
                EntityRef(f"20@{domain}"), SimpleNamespace(user=users[0]), session, settings
            )
            assert previous["channel"]["conversation_type"] == kind
            original = await session.get(Message, (30, domain))
            assert original.content is None and original.e2ee == {"version": 2}
            if kind == "direct":
                assert (
                    await session.scalar(
                        select(DMConversation.id).where(DMConversation.pair_key == lookup)
                    )
                    == 20
                )
            session.add(
                Message(
                    id=31,
                    origin_domain=domain,
                    channel_id=20,
                    channel_domain=domain,
                    author_id=1,
                    author_domain=domain,
                    content="after agreement",
                )
            )
        async with sessions() as session:
            session.add(
                Message(
                    id=32,
                    origin_domain=domain,
                    channel_id=10,
                    channel_domain=domain,
                    author_id=1,
                    author_domain=domain,
                    e2ee={"version": 2},
                    encryption_policy_generation=1,
                    encryption_epoch=1,
                )
            )
            with pytest.raises(IntegrityError, match="DM continued"):
                await session.commit()
        if kind == "group":
            async with sessions.begin() as session:
                await session.delete(await session.get(DMParticipant, (20, domain, 3, domain)))
                await session.flush()
                assert await session.get(DMParticipant, (10, domain, 3, domain)) is None
                # Rejoining the current group must not restore its former history grant.
                session.add(
                    DMParticipant(
                        conversation_id=20,
                        conversation_domain=domain,
                        user_id=3,
                        user_domain=domain,
                    )
                )
                await session.flush()
                assert await e2ee.previous_dm_segment(
                    EntityRef(f"20@{domain}"), SimpleNamespace(user=users[-1]), session, settings
                ) == {"channel": None}
    finally:
        async with sessions.begin() as session:
            await session.execute(delete(Message).where(Message.channel_domain == domain))
            await session.execute(delete(Channel).where(Channel.origin_domain == domain))
            await session.execute(delete(User).where(User.origin_domain == domain))
            await session.execute(delete(Instance).where(Instance.domain == domain))
        await engine.dispose()
