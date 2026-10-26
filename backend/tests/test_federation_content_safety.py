from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.federation import content_safety
from app.federation.content_safety import (
    apply_safety_update,
    reconcile_safety_history,
    safety_projection,
)
from app.federation.dm_history import merge_dm_history_messages
from app.federation.dm_mutations import apply_dm_message_mutation

from .test_dm_message_mutations import (
    ACTOR_REF,
    AUTHORITY,
    common_content,
    context,
    mutation_objects,
    mutation_session,
)


def projection(message, state):
    return {
        "id": str(message.id),
        "origin_domain": message.origin_domain,
        "channel_id": str(message.channel_id),
        "channel_domain": message.channel_domain,
        "author_id": str(message.author_id),
        "author_domain": message.author_domain,
        "created_at": message.created_at.isoformat(),
        "edited_at": (message.created_at + timedelta(minutes=1)).isoformat(),
        "safety_state": state,
        "safety_locked": state is not None,
        "content": "restored text",
        "message_type": 0,
        "flags": 0,
        "attachments": [],
    }


def test_signed_safety_projection_discards_every_embedded_content_source():
    raw = {
        "id": "1",
        "safety_state": "removed",
        "content": "original",
        "e2ee": {"ciphertext": "original"},
        "attachments": [{"id": "2"}],
        "message_snapshots": [{"message": {"content": "original"}}],
        "forwarded_message_ref": "3@remote.example",
        "referenced_message": {"content": "original"},
        "embeds": [{"image": {"url": "https://remote.example/original"}}],
        "poll": {"question": "original"},
        "flags": (1 << 14) | (1 << 15),
    }
    safe = safety_projection(raw)
    assert "original" not in str(safe)
    assert safe["safety_locked"] is True
    assert safe["flags"] == 0
    assert raw["content"] == "original"
    with pytest.raises(ValueError):
        safety_projection({"safety_state": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True])
async def test_authority_can_release_pending_but_never_restore_confirmed(monkeypatch, encrypted):
    _, channel, actor, message = mutation_objects()
    channel.encryption_mode = "plaintext"
    channel.encryption_policy_generation = 0
    channel.encryption_epoch = None
    channel.encryption_group_id = None
    message.flags = 0
    message.safety_state = None
    message.e2ee = {"ciphertext": "previous secret"}
    session = SimpleNamespace(get=AsyncMock(return_value=actor))
    published = AsyncMock()
    monkeypatch.setattr(content_safety, "project_safety", published)
    monkeypatch.setattr("app.federation.replication.replicate_message_attachments", AsyncMock())
    settings = SimpleNamespace(domain="local.example")
    event_ms = int((message.created_at + timedelta(hours=1)).timestamp() * 1000)

    async def apply(raw):
        nonlocal event_ms
        event_ms += 1000
        return await apply_safety_update(
            cast(Any, session),
            cast(Any, settings),
            cast(Any, channel),
            cast(Any, message),
            raw,
            event_timestamp_ms=event_ms,
        )

    pending = projection(message, "pending_review")
    fractional_time = datetime.fromtimestamp((event_ms + 1000) / 1000, UTC) + timedelta(
        microseconds=500
    )
    pending["edited_at"] = pending["safety_updated_at"] = fractional_time.isoformat()
    await apply(pending)
    assert message.e2ee is None and message.content != "before"
    with pytest.raises(ValueError, match="protected"):
        await apply({"content": "ordinary edit without an explicit release"})
    release = projection(message, None)
    release["edited_at"] = None
    release["mention_user_refs"] = [{"id": "42", "origin_domain": "member.example"}]
    if encrypted:
        release["content"] = None
        release["e2ee"] = {
            "version": 2,
            "protocol": "mls10",
            "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
            "group_id": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "policy_generation": "1",
            "epoch": "1",
            "sender_device_id": "ked_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "operation": "create",
            "ciphertext": "AQ",
        }
    # A historical plaintext body remains valid after the room enables MLS.
    channel.encryption_mode = "e2ee"
    channel.encryption_policy_generation = 2
    channel.encryption_epoch = 9
    channel.encryption_group_id = "current-group"
    await apply(release)
    assert message.safety_state is None and message.content == release["content"]
    assert message.encryption_policy_generation == (1 if encrypted else 0)
    assert message.encryption_epoch == (1 if encrypted else None)
    assert message.edited_at is None and message.mention_user_refs == release["mention_user_refs"]
    assert published.await_args.args[-1] == "released"
    released_at = message.safety_updated_at
    old_pending = projection(message, "pending_review")
    old_pending["safety_updated_at"] = (released_at - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="regressed"):
        await apply(old_pending)
    await reconcile_safety_history(cast(Any, session), cast(Any, settings), message, old_pending)
    assert message.safety_state is None and message.safety_updated_at == released_at

    removed = projection(message, "removed")
    removed["edited_at"] = (message.created_at + timedelta(minutes=3)).isoformat()
    await apply(removed)
    with pytest.raises(ValueError):
        await apply(release)
    assert message.safety_state == "removed"


@pytest.mark.asyncio
async def test_remote_authority_legacy_delete_confirms_pending_for_departed_author(monkeypatch):
    conversation, channel, actor, message = mutation_objects()
    message.safety_state = "pending_review"
    session = mutation_session(conversation, channel, actor, message)
    get = session.get.side_effect

    async def departed(model, key, **kwargs):
        return None if model.__name__ == "DMParticipant" else await get(model, key, **kwargs)

    session.get.side_effect = departed
    published = AsyncMock()
    monkeypatch.setattr(content_safety, "project_safety", published)
    deleted_at = datetime(2026, 8, 28, 10, tzinfo=UTC)
    kwargs = dict(
        event_type="dm.message.delete",
        content=common_content(deleted_at=deleted_at.isoformat()),
        context=context(),
        event_origin=AUTHORITY,
        actor_ref=ACTOR_REF,
        event_timestamp_ms=int(deleted_at.timestamp() * 1000),
    )
    result = await apply_dm_message_mutation(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="local.example")),
        **kwargs,
    )
    assert result.render_message_update is True
    assert message.safety_state == "removed" and message.deleted_at is None
    assert published.await_args.args[-1] == "removed"
    with pytest.raises(ValueError, match="projection"):
        await apply_dm_message_mutation(
            cast(Any, session),
            cast(Any, SimpleNamespace(domain="local.example")),
            **{**kwargs, "event_origin": "attacker.example"},
        )


def test_dm_history_safety_marker_wins_without_importing_remote_body_or_identity():
    local = {
        "id": "1",
        "origin_domain": "local.example",
        "content": "local original",
        "author": {"username": "trusted"},
        "attachments": [{"id": "2"}],
    }
    remote = {
        **local,
        "content": "forged remote body",
        "author": {"username": "forged"},
        "safety_state": "pending_review",
    }
    merged = merge_dm_history_messages([remote], [local], limit=10)[0]
    assert merged["content"] != local["content"] and merged["attachments"] == []
    assert merged["author"] == local["author"]
    assert merged["safety_state"] == "pending_review"
    confirmed = {**merged, "safety_state": "removed"}
    assert merge_dm_history_messages([remote], [confirmed], limit=10)[0] == confirmed
    released = {**local, "safety_updated_at": "2026-08-28T12:00:00+00:00"}
    stale = {**remote, "safety_updated_at": "2026-08-28T11:00:00+00:00"}
    assert merge_dm_history_messages([stale], [released], limit=10)[0] == released


@pytest.mark.asyncio
async def test_history_marker_persists_removal_without_accepting_a_replacement_body(monkeypatch):
    _, _, _, message = mutation_objects()
    published = AsyncMock()
    monkeypatch.setattr(content_safety, "project_safety", published)
    raw = projection(message, "removed") | {"content": "forged replacement"}
    await reconcile_safety_history(cast(Any, None), cast(Any, None), cast(Any, message), raw)
    assert message.safety_state == "removed"
    assert message.content != "forged replacement"
    assert published.await_count == 1
    await reconcile_safety_history(
        cast(Any, None), cast(Any, None), cast(Any, message), projection(message, "pending_review")
    )
    assert message.safety_state == "removed" and published.await_count == 1


@pytest.mark.asyncio
async def test_guild_authority_safety_transition_preserves_placeholder_and_blocks_regular_delete(
    monkeypatch,
):
    from app.federation.guilds import apply_guild_mutation_event

    _, channel, actor, message = mutation_objects()
    guild = SimpleNamespace(
        id=12,
        origin_domain=AUTHORITY,
        last_event_seq=0,
        next_event_seq=1,
        sync_status="ready",
        snapshot_generation=1,
    )
    actor.origin_domain = AUTHORITY
    channel.guild_id = guild.id
    channel.guild_domain = guild.origin_domain
    channel.type = 0
    message.flags = 0

    async def get(model, _key, **_kwargs):
        return {"User": actor, "Message": message, "Channel": channel}.get(model.__name__)

    session = SimpleNamespace(scalar=AsyncMock(return_value=guild), get=AsyncMock(side_effect=get))
    published = AsyncMock()
    monkeypatch.setattr(content_safety, "project_safety", published)
    monkeypatch.setattr(
        "app.chat.payloads.message_payload",
        lambda message, *_args: safety_projection(projection(message, message.safety_state)),
    )
    timestamp = message.created_at + timedelta(minutes=2)
    event = {
        "type": "guild.message.update",
        "ts": int(timestamp.timestamp() * 1000),
        "actor": {"id": str(actor.id), "domain": actor.origin_domain},
        "context": {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "channel_id": str(channel.id),
            "channel_domain": channel.origin_domain,
            "seq": "1",
        },
        "content": {"message": projection(message, "pending_review")},
    }
    settings = SimpleNamespace(domain="local.example")
    result = await apply_guild_mutation_event(
        cast(Any, session), cast(Any, settings), cast(Any, guild), event
    )
    assert result[0] == "MESSAGE_UPDATE" and result[1]["safety_state"] == "pending_review"
    assert result[1]["attachments"] == [] and result[1]["content"] != "restored text"
    event["type"] = "guild.message.delete"
    event["context"]["seq"] = "2"
    event["content"] = {
        "message": {"id": str(message.id), "origin_domain": message.origin_domain},
        "deleted_at": timestamp.isoformat(),
    }
    with pytest.raises(ValueError, match="protected"):
        await apply_guild_mutation_event(
            cast(Any, session), cast(Any, settings), cast(Any, guild), event
        )
    assert guild.last_event_seq == 1
    event["content"]["safety_state"] = "removed"
    result = await apply_guild_mutation_event(
        cast(Any, session), cast(Any, settings), cast(Any, guild), event
    )
    assert result[0] == "MESSAGE_UPDATE" and result[1]["safety_state"] == "removed"
    assert message.deleted_at is None and guild.last_event_seq == 2
