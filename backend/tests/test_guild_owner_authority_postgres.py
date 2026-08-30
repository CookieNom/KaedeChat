"""Opt-in PostgreSQL coverage for guild-owner attestation lock ordering.

Run against a disposable, fully migrated PostgreSQL database with::

    KAEDE_GUILD_AUTHORITY_TEST_DATABASE_URL=postgresql+asyncpg://... \
      pytest -q tests/test_guild_owner_authority_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.chat.guild_revision as guild_revision
from app.chat.guild_revision import federation_channel_state
from app.db.models import Channel, Guild, GuildMember, Instance, User
from app.federation.guilds import apply_guild_mutation_event

DATABASE_URL = os.environ.get("KAEDE_GUILD_AUTHORITY_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason=(
        "set KAEDE_GUILD_AUTHORITY_TEST_DATABASE_URL to a disposable migrated PostgreSQL database"
    ),
)


async def _cleanup(session: AsyncSession, domains: set[str]) -> None:
    await session.execute(delete(GuildMember).where(GuildMember.guild_domain.in_(domains)))
    await session.execute(delete(Guild).where(Guild.origin_domain.in_(domains)))
    await session.execute(delete(User).where(User.origin_domain.in_(domains)))
    await session.execute(delete(Instance).where(Instance.domain.in_(domains)))
    await session.commit()


async def _seed(
    session: AsyncSession,
    *,
    authority: str,
    owner_a_domain: str,
    owner_b_domain: str,
    member_domain: str,
) -> None:
    domains = {authority, owner_a_domain, owner_b_domain, member_domain}
    await _cleanup(session, domains)
    session.add_all(Instance(domain=domain, is_self=False) for domain in sorted(domains))
    await session.flush()
    owner_a = User(
        id=7,
        origin_domain=owner_a_domain,
        is_local=False,
        account_type="human",
        username="owner.a",
        federation_introduced_by_domain=authority,
    )
    owner_b = User(
        id=8,
        origin_domain=owner_b_domain,
        is_local=False,
        account_type="human",
        username="owner.b",
        federation_introduced_by_domain=authority,
    )
    member = User(
        id=9,
        origin_domain=member_domain,
        is_local=False,
        account_type="human",
        username="member",
        federation_introduced_by_domain=authority,
    )
    session.add_all([owner_a, owner_b, member])
    await session.flush()
    guild = Guild(
        id=42,
        origin_domain=authority,
        name="Guild owner lock ordering",
        owner_id=owner_a.id,
        owner_domain=owner_a.origin_domain,
    )
    joined_at = datetime.now(UTC)
    session.add(guild)
    session.add_all(
        GuildMember(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=user.id,
            user_domain=user.origin_domain,
            joined_at=joined_at,
        )
        for user in (owner_a, owner_b, member)
    )
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_operation", ["mutation", "transfer"])
async def test_mutation_and_owner_transfer_use_serialized_current_owner(
    monkeypatch: pytest.MonkeyPatch,
    first_operation: str,
) -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    authority = "guild-owner-lock-it.example"
    owner_a_domain = "guild-owner-a-it.example"
    owner_b_domain = "guild-owner-b-it.example"
    member_domain = "guild-member-it.example"
    domains = {authority, owner_a_domain, owner_b_domain, member_domain}
    settings = SimpleNamespace(domain=authority)
    first_envelope = asyncio.Event()
    release_first = asyncio.Event()
    captures: list[tuple[str, tuple[int, str], int, int]] = []

    async def capture_envelope(
        _session: AsyncSession,
        _settings: object,
        guild: Guild,
        _event_type: str,
        signer: User,
        content: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation = "transfer" if content["guild"].get("owner_id") is not None else "mutation"
        captures.append(
            (
                operation,
                (signer.id, signer.origin_domain),
                int((context or {})["seq"]),
                int(guild.snapshot_generation),
            )
        )
        if len(captures) == 1:
            first_envelope.set()
            await release_first.wait()
        return {
            "event_id": f"kcfe_{operation}_{len(captures)}",
            "actor": {"id": str(signer.id), "domain": signer.origin_domain},
            "context": context or {},
            "content": content,
        }

    monkeypatch.setattr(guild_revision, "build_guild_authority_envelope", capture_envelope)
    monkeypatch.setattr(guild_revision, "store_guild_event", lambda *args: None)
    monkeypatch.setattr(
        guild_revision,
        "remote_guild_destinations",
        AsyncMock(return_value=set()),
    )

    try:
        async with sessions() as seed_session:
            await _seed(
                seed_session,
                authority=authority,
                owner_a_domain=owner_a_domain,
                owner_b_domain=owner_b_domain,
                member_domain=member_domain,
            )

        async with sessions() as mutation_session, sessions() as transfer_session:
            # Load both identities before either transaction takes the row lock.
            mutation_guild = await mutation_session.get(Guild, (42, authority))
            transfer_guild = await transfer_session.get(Guild, (42, authority))
            mutation_actor = await mutation_session.get(User, (9, member_domain))
            transfer_actor = await transfer_session.get(User, (7, owner_a_domain))
            assert mutation_guild is not None and transfer_guild is not None
            assert mutation_actor is not None and transfer_actor is not None

            async def mutate() -> None:
                await guild_revision.queue_guild_mutation(
                    mutation_session,
                    settings,  # type: ignore[arg-type]
                    mutation_guild,
                    mutation_actor,
                    "guild.update",
                    {
                        "guild": {
                            "id": "42",
                            "origin_domain": authority,
                            "name": "Concurrent mutation",
                        }
                    },
                )
                await mutation_session.commit()

            async def transfer() -> None:
                await guild_revision.queue_guild_mutation(
                    transfer_session,
                    settings,  # type: ignore[arg-type]
                    transfer_guild,
                    transfer_actor,
                    "guild.update",
                    {
                        "guild": {
                            "id": "42",
                            "origin_domain": authority,
                            "owner_id": "8",
                            "owner_domain": owner_b_domain,
                        }
                    },
                )
                transfer_guild.owner_id = 8
                transfer_guild.owner_domain = owner_b_domain
                await transfer_session.commit()

            operations = {"mutation": mutate, "transfer": transfer}
            second_operation = "transfer" if first_operation == "mutation" else "mutation"
            first_task = asyncio.create_task(operations[first_operation]())
            await asyncio.wait_for(first_envelope.wait(), timeout=5)
            second_task = asyncio.create_task(operations[second_operation]())
            release_first.set()
            await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=10)

        owner_a_ref = (7, owner_a_domain)
        owner_b_ref = (8, owner_b_domain)
        if first_operation == "mutation":
            assert [(item[0], item[1]) for item in captures] == [
                ("mutation", owner_a_ref),
                ("transfer", owner_a_ref),
            ]
        else:
            assert [(item[0], item[1]) for item in captures] == [
                ("transfer", owner_a_ref),
                ("mutation", owner_b_ref),
            ]
        assert [item[2] for item in captures] == [1, 2]
        assert [item[3] for item in captures] == [2, 3]

        async with sessions() as verification:
            stored = await verification.scalar(
                select(Guild).where(Guild.id == 42, Guild.origin_domain == authority)
            )
            assert stored is not None
            assert (stored.owner_id, stored.owner_domain) == owner_b_ref
            assert (stored.last_event_seq, stored.next_event_seq) == (2, 3)
            assert stored.snapshot_generation == 3
    finally:
        async with sessions() as cleanup_session:
            await _cleanup(cleanup_session, domains)
        await engine.dispose()


@pytest.mark.asyncio
async def test_channel_event_round_trips_authority_channel_and_guild_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    authority = "guild-version-authority-it.example"
    owner_a_domain = "guild-version-owner-it.example"
    owner_b_domain = "guild-version-spare-it.example"
    member_domain = "guild-version-member-it.example"
    replica = "guild-version-replica-it.example"
    domains = {authority, owner_a_domain, owner_b_domain, member_domain, replica}
    captured: dict[str, Any] = {}

    async def capture_envelope(
        _session: AsyncSession,
        _settings: object,
        event_type: str,
        signer: User,
        content: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        **_kwargs: object,
    ) -> dict[str, Any]:
        captured.update(
            {
                "event_id": "kcge_version_round_trip",
                "type": event_type,
                "actor": {"id": str(signer.id), "domain": signer.origin_domain},
                "context": context or {},
                "content": content,
            }
        )
        return captured

    monkeypatch.setattr(guild_revision, "build_envelope", capture_envelope)
    monkeypatch.setattr(guild_revision, "store_guild_event", lambda *args: None)
    monkeypatch.setattr(
        guild_revision,
        "remote_guild_destinations",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        guild_revision,
        "remote_destinations_with_channel_access",
        AsyncMock(return_value=set()),
    )

    try:
        async with sessions() as session:
            await _seed(
                session,
                authority=authority,
                owner_a_domain=owner_a_domain,
                owner_b_domain=owner_b_domain,
                member_domain=member_domain,
            )
            session.add(Instance(domain=replica, is_self=False))
            channel = Channel(
                id=80,
                origin_domain=authority,
                guild_id=42,
                guild_domain=authority,
                type=0,
                name="before",
                position=0,
                created_floor_id=80,
            )
            session.add(channel)
            await session.commit()

            guild = await session.get(Guild, (42, authority))
            actor = await session.get(User, (7, owner_a_domain))
            assert guild is not None and actor is not None
            channel.name = "after"
            await guild_revision.queue_guild_mutation(
                session,
                SimpleNamespace(domain=authority),  # type: ignore[arg-type]
                guild,
                actor,
                "guild.channel.update",
                {"channel": federation_channel_state(channel)},
                channel=channel,
            )
            await session.commit()
            await session.refresh(guild)
            await session.refresh(channel)

            authority_guild_version = guild.updated_at
            authority_channel_version = channel.updated_at
            assert captured["context"]["guild_version"] == authority_guild_version.isoformat()
            assert (
                captured["content"]["channel"]["version"] == authority_channel_version.isoformat()
            )

            old_version = datetime(2026, 1, 1, tzinfo=UTC)
            await session.execute(
                update(Guild)
                .where(Guild.id == 42, Guild.origin_domain == authority)
                .values(
                    last_event_seq=0,
                    next_event_seq=1,
                    snapshot_generation=1,
                    sync_status="stale",
                    updated_at=old_version,
                )
            )
            await session.execute(
                update(Channel)
                .where(Channel.id == 80, Channel.origin_domain == authority)
                .values(name="before", updated_at=old_version)
            )
            await session.commit()

            replica_guild = await session.get(Guild, (42, authority), populate_existing=True)
            assert replica_guild is not None
            await apply_guild_mutation_event(
                session,
                SimpleNamespace(domain=replica),  # type: ignore[arg-type]
                replica_guild,
                captured,
            )
            await session.commit()
            await session.refresh(replica_guild)
            replicated_channel = await session.get(
                Channel,
                (80, authority),
                populate_existing=True,
            )
            assert replicated_channel is not None
            assert replica_guild.updated_at == authority_guild_version
            assert replicated_channel.updated_at == authority_channel_version
    finally:
        async with sessions() as cleanup_session:
            await _cleanup(cleanup_session, domains)
        await engine.dispose()
