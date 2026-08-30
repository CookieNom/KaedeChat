from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app.federation.guilds as federation_guilds
from app.chat.guild_revision import build_guild_authority_envelope
from app.chat.payloads import message_payload
from app.core.federation import (
    authority_attested_guild_crosspost_actor,
    guild_crosspost_authority_event_ref,
    sign_envelope,
)
from app.core.settings import Settings
from app.db.models import Channel, Guild, GuildMember, Message, User
from app.federation.guilds import (
    apply_guild_message_event,
    apply_guild_mutation_event,
    assign_guild_sequence,
    validate_guild_snapshot,
)
from app.federation.replication import profile_from_user
from app.federation.security import validated_event_envelope


def _remote_user(user_id: int, domain: str, username: str) -> User:
    return User(
        id=user_id,
        origin_domain=domain,
        is_local=False,
        account_type="human",
        username=username,
        password_hash=None,
    )


def _guild_message_content(author: User) -> dict[str, Any]:
    return {
        "message": {
            "author_id": str(author.id),
            "author_domain": author.origin_domain,
            "flags": 0,
        },
        "author": {
            "id": str(author.id),
            "origin_domain": author.origin_domain,
            "username": author.username,
        },
    }


def _crosspost_content(author: User) -> dict[str, Any]:
    return {
        "message": {
            "id": "300",
            "origin_domain": "alpha.localhost",
            "channel_id": "200",
            "channel_domain": "alpha.localhost",
            "author_id": str(author.id),
            "author_domain": author.origin_domain,
            "message_type": 0,
            "flags": 1 << 1,
            "forwarded_message_id": "700",
            "forwarded_message_domain": "source.example",
            "forwarded_message_ref": "700@source.example",
            "forwarded_channel_id": "600",
            "forwarded_channel_domain": "source.example",
            "forward_snapshot": None,
            "message_reference": {
                "type": 0,
                "message_id": "700",
                "message_domain": "source.example",
                "channel_id": "600",
                "channel_domain": "source.example",
            },
        },
        "author": {
            "id": str(author.id),
            "origin_domain": author.origin_domain,
            "username": author.username,
        },
        "thread_starter": False,
    }


@pytest.mark.asyncio
async def test_crosspost_requires_complete_refs_and_exact_target_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _remote_user(1, "owner.example", "owner")
    # This source author is homed on the target authority but is deliberately
    # not a target-guild member. It must never become the envelope authority.
    source_author = User(
        id=2,
        origin_domain="alpha.localhost",
        username="source-author",
        is_local=True,
    )
    guild = Guild(
        id=100,
        origin_domain="alpha.localhost",
        name="Target",
        owner_id=owner.id,
        owner_domain=owner.origin_domain,
        updated_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    context = {
        "guild_id": "100",
        "guild_domain": "alpha.localhost",
        "channel_id": "200",
        "channel_domain": "alpha.localhost",
        "seq": "1",
    }
    content = _crosspost_content(source_author)
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    monkeypatch.setattr(
        "app.federation.events.self_private_key",
        AsyncMock(return_value=("ed25519:test", private_key)),
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=AssertionError("no member bypass")),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    settings = cast(Settings, SimpleNamespace(domain="alpha.localhost"))

    assert guild_crosspost_authority_event_ref(
        "guild.message.create",
        content,
        context,
        expected_authority="alpha.localhost",
    ) == (guild.id, guild.origin_domain)
    assert authority_attested_guild_crosspost_actor(
        "guild.message.create",
        content,
        context,
        expected_authority="alpha.localhost",
        expected_guild_id=guild.id,
        expected_owner=(owner.id, owner.origin_domain),
        actor=(owner.id, owner.origin_domain),
    )
    assert not authority_attested_guild_crosspost_actor(
        "guild.message.create",
        content,
        context,
        expected_authority="alpha.localhost",
        expected_guild_id=guild.id,
        expected_owner=(owner.id, owner.origin_domain),
        actor=(source_author.id, source_author.origin_domain),
    )

    with pytest.raises(ValueError, match="current guild owner"):
        await build_guild_authority_envelope(
            cast(Any, session),
            settings,
            guild,
            "guild.message.create",
            source_author,
            content,
            context=context,
        )

    envelope = await build_guild_authority_envelope(
        cast(Any, session),
        settings,
        guild,
        "guild.message.create",
        owner,
        content,
        context=context,
    )
    assert envelope["actor"] == {"id": "1", "domain": "owner.example"}

    malformed = []
    flag_only = deepcopy(content)
    flag_only["message"] = {
        "flags": 1 << 1,
        "message_type": 0,
        "origin_domain": "alpha.localhost",
        "channel_domain": "alpha.localhost",
    }
    malformed.append(flag_only)
    partial_ref = deepcopy(content)
    partial_ref["message"].pop("forwarded_channel_id")
    malformed.append(partial_ref)
    swapped_author = deepcopy(content)
    swapped_author["author"]["id"] = "3"
    malformed.append(swapped_author)
    for forged in malformed:
        assert (
            guild_crosspost_authority_event_ref(
                "guild.message.create",
                forged,
                context,
                expected_authority="alpha.localhost",
            )
            is None
        )
        with pytest.raises(ValueError, match="authority binding"):
            await build_guild_authority_envelope(
                cast(Any, session),
                settings,
                guild,
                "guild.message.create",
                owner,
                forged,
                context=context,
            )


@pytest.mark.asyncio
async def test_remote_message_authority_keeps_owner_actor_and_validates_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _remote_user(1, "owner.example", "owner")
    author = _remote_user(2, "member.example", "member")
    forged = _remote_user(3, "member.example", "forged")
    guild = Guild(
        id=100,
        origin_domain="alpha.localhost",
        name="Guild",
        owner_id=owner.id,
        owner_domain=owner.origin_domain,
        updated_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    membership = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=author.id,
        user_domain=author.origin_domain,
        joined_at=datetime.now(UTC),
    )

    async def get_model(model: object, key: object) -> object | None:
        if model is GuildMember and key == (
            guild.id,
            guild.origin_domain,
            author.id,
            author.origin_domain,
        ):
            return membership
        return None

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get_model),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    monkeypatch.setattr(
        "app.federation.events.self_private_key",
        AsyncMock(return_value=("ed25519:test", private_key)),
    )
    settings = cast(Settings, SimpleNamespace(domain=guild.origin_domain))
    context = {
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
        "seq": "1",
    }
    content = _guild_message_content(author)

    envelope = await build_guild_authority_envelope(
        cast(Any, session),
        settings,
        guild,
        "guild.message.create",
        owner,
        content,
        context=context,
    )

    assert envelope["actor"] == {"id": str(owner.id), "domain": owner.origin_domain}
    assert envelope["content"]["message"]["author_id"] == str(author.id)
    assert envelope["content"]["message"]["author_domain"] == author.origin_domain

    with pytest.raises(ValueError, match="only sign events for its own users"):
        await build_guild_authority_envelope(
            cast(Any, session),
            settings,
            guild,
            "guild.message.create",
            forged,
            content,
            context=context,
        )

    missing_author = _remote_user(4, "member.example", "missing")
    with pytest.raises(RuntimeError, match="author is not an authority member"):
        await build_guild_authority_envelope(
            cast(Any, session),
            settings,
            guild,
            "guild.message.create",
            owner,
            _guild_message_content(missing_author),
            context=context,
        )


def _replicated_message_event(
    guild: Guild,
    channel: Channel,
    author: User,
    *,
    actor: tuple[int, str],
) -> tuple[dict[str, Any], Message]:
    created_at = datetime.now(UTC)
    message = Message(
        id=300,
        origin_domain=guild.origin_domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=author.id,
        author_domain=author.origin_domain,
        content="owner-attested",
        e2ee=None,
        message_type=0,
        flags=0,
        client_nonce="owner-attested",
        referenced_message_id=None,
        referenced_message_domain=None,
        mention_user_refs=[],
        webhook_name=None,
        webhook_avatar_hash=None,
        created_at=created_at,
    )
    event = {
        "type": "guild.message.create",
        "ts": int(created_at.timestamp() * 1000),
        "actor": {"id": str(actor[0]), "domain": actor[1]},
        "context": {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "seq": "1",
        },
        "content": {
            "author": {
                "id": str(author.id),
                "origin_domain": author.origin_domain,
                "username": author.username,
            },
            "message": {
                "id": str(message.id),
                "origin_domain": message.origin_domain,
                "channel_id": str(message.channel_id),
                "channel_domain": message.channel_domain,
                "author_id": str(author.id),
                "author_domain": author.origin_domain,
                "content": message.content,
                "e2ee": None,
                "message_type": 0,
                "flags": 0,
                "client_nonce": message.client_nonce,
                "mention_user_refs": [],
                "attachments": [],
                "referenced_message_id": None,
                "referenced_message_domain": None,
                "edited_at": None,
                "deleted_at": None,
                "created_at": created_at.isoformat(),
            },
        },
    }
    return event, message


@pytest.mark.asyncio
@pytest.mark.parametrize("member_exists", [True, False])
async def test_owner_attested_remote_message_requires_semantic_author_membership(
    monkeypatch: pytest.MonkeyPatch,
    member_exists: bool,
) -> None:
    guild = Guild(
        id=100,
        origin_domain="alpha.localhost",
        name="Guild",
        owner_id=1,
        owner_domain="owner.example",
        last_event_seq=0,
        next_event_seq=1,
    )
    channel = Channel(
        id=200,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=0,
        name="general",
        created_floor_id=200,
        unavailable=False,
    )
    author = _remote_user(2, "member.example", "author")
    membership = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=author.id,
        user_domain=author.origin_domain,
        joined_at=datetime.now(UTC),
    )
    event, message = _replicated_message_event(
        guild,
        channel,
        author,
        actor=(guild.owner_id, guild.owner_domain),
    )

    async def get_model(model: object, _key: object) -> object | None:
        if model is Channel:
            return channel
        if model is GuildMember:
            return membership if member_exists else None
        if model is Message:
            return message
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[guild, message.id]),
        get=AsyncMock(side_effect=get_model),
        add=MagicMock(),
    )
    monkeypatch.setattr(
        federation_guilds,
        "resolve_delegated_profile",
        AsyncMock(return_value=author),
    )
    monkeypatch.setattr(federation_guilds, "validate_snowflake_timestamp", MagicMock())
    monkeypatch.setattr(
        federation_guilds,
        "replicate_message_attachments",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(federation_guilds, "advance_channel_cursor", AsyncMock())
    monkeypatch.setattr(federation_guilds, "apply_e2ee_control_metadata", AsyncMock())

    if not member_exists:
        with pytest.raises(ValueError, match="author is not a guild member"):
            await apply_guild_message_event(
                cast(Any, session),
                cast(Settings, SimpleNamespace(domain="replica.localhost")),
                guild,
                event,
            )
        return

    applied = await apply_guild_message_event(
        cast(Any, session),
        cast(Settings, SimpleNamespace(domain="replica.localhost")),
        guild,
        event,
    )
    assert applied is message


@pytest.mark.asyncio
async def test_proxy_commit_replica_uses_federation_author_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=100,
        origin_domain="alpha.localhost",
        name="Guild",
        owner_id=1,
        owner_domain="alpha.localhost",
        last_event_seq=0,
        next_event_seq=1,
    )
    channel = Channel(
        id=200,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=0,
        name="general",
        created_floor_id=200,
        unavailable=False,
        encryption_policy_generation=0,
    )
    author = _remote_user(2, "beta.localhost", "author")
    author.profile_version = 3
    author.e2ee_device_generation = 0
    membership = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=author.id,
        user_domain=author.origin_domain,
        joined_at=datetime.now(UTC),
    )
    event, message = _replicated_message_event(
        guild,
        channel,
        author,
        actor=(guild.owner_id, guild.owner_domain),
    )
    message.content = "<:federated_lantern:9000000000100@alpha.localhost>"
    message.encryption_policy_generation = 0
    event["type"] = "guild.message.committed"
    event["content"] = {
        # This is the exact authoritative proxy response shape: the client
        # projection embeds decimal-string revision fields, while the sibling
        # federation profile retains strict JSON integers.
        "message": message_payload(message, author, []),
        "author": profile_from_user(author),
        "thread_starter": False,
    }
    embedded_author = cast(dict[str, object], event["content"]["message"])["author"]
    assert isinstance(embedded_author, dict)
    assert embedded_author["profile_version"] == "3"
    assert cast(dict[str, object], event["content"]["author"])["profile_version"] == 3

    async def get_model(model: object, _key: object) -> object | None:
        if model is Channel:
            return channel
        if model is GuildMember:
            return membership
        if model is Message:
            return message
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[guild, message.id]),
        get=AsyncMock(side_effect=get_model),
        add=MagicMock(),
    )
    resolve_profile = AsyncMock(return_value=author)
    monkeypatch.setattr(federation_guilds, "resolve_delegated_profile", resolve_profile)
    monkeypatch.setattr(federation_guilds, "validate_snowflake_timestamp", MagicMock())
    monkeypatch.setattr(
        federation_guilds,
        "replicate_message_attachments",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(federation_guilds, "advance_channel_cursor", AsyncMock())
    monkeypatch.setattr(federation_guilds, "apply_e2ee_control_metadata", AsyncMock())

    applied = await apply_guild_message_event(
        cast(Any, session),
        cast(Settings, SimpleNamespace(domain="beta.localhost")),
        guild,
        event,
    )

    assert applied is message
    parsed_profile = resolve_profile.await_args.args[2]
    assert parsed_profile.profile_version == 3
    assert parsed_profile.e2ee_device_generation == 0


@pytest.mark.asyncio
async def test_forged_remote_message_authority_actor_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=100,
        origin_domain="alpha.localhost",
        name="Guild",
        owner_id=1,
        owner_domain="owner.example",
        last_event_seq=0,
        next_event_seq=1,
    )
    channel = Channel(
        id=200,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=0,
        name="general",
        created_floor_id=200,
        unavailable=False,
    )
    author = _remote_user(2, "member.example", "author")
    membership = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=author.id,
        user_domain=author.origin_domain,
        joined_at=datetime.now(UTC),
    )
    event, _ = _replicated_message_event(
        guild,
        channel,
        author,
        actor=(3, "forged.example"),
    )

    async def get_model(model: object, _key: object) -> object | None:
        if model is Channel:
            return channel
        if model is GuildMember:
            return membership
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=guild),
        get=AsyncMock(side_effect=get_model),
    )
    monkeypatch.setattr(
        federation_guilds,
        "resolve_delegated_profile",
        AsyncMock(return_value=author),
    )

    with pytest.raises(ValueError, match="actor does not match its author"):
        await apply_guild_message_event(
            cast(Any, session),
            cast(Settings, SimpleNamespace(domain="replica.localhost")),
            guild,
            event,
        )


def _signed_guild_update(
    private_key: Ed25519PrivateKey,
    *,
    actor: User,
    seq: int,
    guild_payload: dict[str, Any],
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "event_id": f"kcfe_owner_{seq:02d}_0123456789",
        "origin": "alpha.localhost",
        "type": "guild.update",
        "ts": int(datetime.now(UTC).timestamp() * 1000),
        "actor": {"id": str(actor.id), "domain": actor.origin_domain},
        "context": {
            "guild_id": "42",
            "guild_domain": "alpha.localhost",
            "seq": str(seq),
        },
        "content": {"guild": guild_payload},
    }
    raw["signatures"] = {"alpha.localhost": {"ed25519:test": sign_envelope(raw, private_key)}}
    return raw


@pytest.mark.asyncio
async def test_gap_validated_owner_transfer_uses_preupdate_owner_and_rejects_forgery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_a = _remote_user(7, "owner-a.example", "owner-a")
    owner_b = _remote_user(8, "owner-b.example", "owner-b")
    forged = _remote_user(9, "forged.example", "forged")
    guild = SimpleNamespace(
        id=42,
        origin_domain="alpha.localhost",
        owner_id=owner_a.id,
        owner_domain=owner_a.origin_domain,
        last_event_seq=0,
        next_event_seq=1,
        sync_status="ready",
        permission_generation=1,
        snapshot_generation=1,
        name="Before",
        description=None,
        icon_hash=None,
        banner_hash=None,
        federated_history_policy="disabled",
        history_policy_generation=1,
    )
    owner_b_membership = SimpleNamespace()
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    transfer = _signed_guild_update(
        private_key,
        actor=owner_a,
        seq=1,
        guild_payload={
            "id": "42",
            "origin_domain": "alpha.localhost",
            "owner_id": str(owner_b.id),
            "owner_domain": owner_b.origin_domain,
        },
    )
    after_transfer = _signed_guild_update(
        private_key,
        actor=owner_b,
        seq=2,
        guild_payload={
            "id": "42",
            "origin_domain": "alpha.localhost",
            "name": "After",
        },
    )
    forged_event = _signed_guild_update(
        private_key,
        actor=forged,
        seq=3,
        guild_payload={
            "id": "42",
            "origin_domain": "alpha.localhost",
            "name": "Forged",
        },
    )

    class KeySession:
        async def get(self, _model: object, _key: object) -> object:
            return SimpleNamespace(
                public_key=private_key.public_key().public_bytes_raw(),
                expired_at=None,
            )

    validation_settings = cast(
        Settings,
        SimpleNamespace(
            domain="replica.localhost",
            federation_clock_skew_seconds=300,
            federation_event_retention_days=7,
        ),
    )
    validated = [
        (
            await validated_event_envelope(
                cast(Any, KeySession()),
                validation_settings,
                "alpha.localhost",
                raw,
                allow_authority_attested_actor=True,
            )
        ).model_dump(mode="json")
        for raw in (transfer, after_transfer, forged_event)
    ]

    async def get_model(model: object, key: object) -> object | None:
        if model is User:
            return {
                (owner_a.id, owner_a.origin_domain): owner_a,
                (owner_b.id, owner_b.origin_domain): owner_b,
                (forged.id, forged.origin_domain): forged,
            }.get(key)
        if model is GuildMember and key == (
            guild.id,
            guild.origin_domain,
            owner_b.id,
            owner_b.origin_domain,
        ):
            return owner_b_membership
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=guild),
        get=AsyncMock(side_effect=get_model),
    )
    monkeypatch.setattr(
        "app.federation.history.purge_ineligible_federated_history",
        AsyncMock(return_value=0),
    )
    apply_settings = cast(Settings, SimpleNamespace(domain="replica.localhost"))

    await apply_guild_mutation_event(cast(Any, session), apply_settings, guild, validated[0])
    assert (guild.owner_id, guild.owner_domain) == (owner_b.id, owner_b.origin_domain)

    await apply_guild_mutation_event(cast(Any, session), apply_settings, guild, validated[1])
    assert guild.name == "After"

    with pytest.raises(ValueError, match="not authoritative"):
        await apply_guild_mutation_event(cast(Any, session), apply_settings, guild, validated[2])
    assert guild.name == "After"


def _remote_owner_snapshot() -> dict[str, Any]:
    return {
        "snapshot_seq": "0",
        "guild": {
            "id": "42",
            "origin_domain": "alpha.localhost",
            "name": "Remote owner",
            "owner_id": "8",
            "owner_domain": "owner.example",
            "permission_generation": "1",
        },
        "roles": [],
        "channels": [],
        "members": [
            {
                "user": {
                    "id": "8",
                    "origin_domain": "owner.example",
                    "username": "owner",
                },
                "nickname": None,
                "joined_at": "2026-08-28T00:00:00+00:00",
                "timeout_until": None,
                "member_version": "1",
            }
        ],
        "member_roles": [],
        "overwrites": [],
    }


def test_snapshot_accepts_remote_member_owner_and_rejects_nonmember_owner() -> None:
    snapshot = _remote_owner_snapshot()
    validate_guild_snapshot(
        snapshot,
        expected_origin="alpha.localhost",
        expected_guild_id=42,
    )

    forged = deepcopy(snapshot)
    forged["guild"]["owner_id"] = "9"
    with pytest.raises(ValueError, match="does not contain its owner"):
        validate_guild_snapshot(
            forged,
            expected_origin="alpha.localhost",
            expected_guild_id=42,
        )


@pytest.mark.asyncio
async def test_direct_sequence_assignment_flushes_and_refreshes_locked_guild() -> None:
    stale = SimpleNamespace(
        id=42,
        origin_domain="alpha.localhost",
        next_event_seq=1,
        last_event_seq=0,
    )
    locked = SimpleNamespace(
        id=42,
        origin_domain="alpha.localhost",
        next_event_seq=7,
        last_event_seq=6,
    )
    session = SimpleNamespace(
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=locked),
    )

    seq = await assign_guild_sequence(cast(Any, session), cast(Any, stale))

    assert seq == 7
    assert (locked.last_event_seq, locked.next_event_seq) == (7, 8)
    session.flush.assert_awaited_once()
    statement = session.scalar.await_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True
