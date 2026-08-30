from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import Client
from kaede_bot.models import (
    Attachment,
    Call,
    Channel,
    Emoji,
    Guild,
    Member,
    Message,
    ReadyEvent,
    Sticker,
    Webhook,
)
from kaede_bot.refs import EntityRef, User
from kaede_bot.state import WorkerState


class StubClient:
    def _authority_target(self, ref: EntityRef, target: str) -> str:
        assert ref.domain in target
        return target


def user_payload(*, user_id: int = 8, domain: str = "users.example") -> dict[str, Any]:
    return {
        "id": str(user_id),
        "origin_domain": domain,
        "username": "remote-user",
        "display_name": "Remote User",
        "bot": False,
        "account_type": "human",
        "profile_version": "7",
        "e2ee_device_generation": "4",
        "profile_resolved": False,
    }


def _boolean_channel(overrides: dict[str, object]) -> object:
    return Channel.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://chat.example",
        {"id": "55", "origin_domain": "chat.example", **overrides},
    )


def _boolean_member(overrides: dict[str, object]) -> object:
    return Member.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://guilds.example",
        {
            "guild_id": "10",
            "guild_domain": "guilds.example",
            "user": user_payload(),
            "joined_at": "2026-08-28T12:00:00+00:00",
            **overrides,
        },
    )


def _boolean_webhook(overrides: dict[str, object]) -> object:
    return Webhook.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://guilds.example",
        {
            "id": "71",
            "guild_id": "10",
            "guild_domain": "guilds.example",
            "channel_id": "55",
            "channel_domain": "guilds.example",
            "name": "Relay",
            **overrides,
        },
    )


def _boolean_emoji(overrides: dict[str, object]) -> object:
    return Emoji.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://guilds.example",
        {
            "id": "72",
            "origin_domain": "guilds.example",
            "guild_id": "10",
            "guild_domain": "guilds.example",
            "name": "wave",
            **overrides,
        },
    )


def _boolean_sticker(overrides: dict[str, object]) -> object:
    return Sticker.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://guilds.example",
        {
            "id": "73",
            "origin_domain": "guilds.example",
            "guild_id": "10",
            "guild_domain": "guilds.example",
            "name": "wave",
            **overrides,
        },
    )


def _boolean_message(overrides: dict[str, object]) -> object:
    return Message.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "101",
            "origin_domain": "chat.example",
            "channel_id": "55",
            "channel_domain": "chat.example",
            **overrides,
        },
    )


@pytest.mark.parametrize("invalid", ["false", 0, None])
@pytest.mark.parametrize(
    ("parser", "field"),
    [
        (_boolean_channel, "nsfw"),
        (_boolean_channel, "permissions_synced"),
        (_boolean_channel, "history_truncated"),
        (_boolean_channel, "history_remote_available"),
        (_boolean_member, "temporary"),
        (_boolean_webhook, "revoked"),
        (_boolean_emoji, "animated"),
        (_boolean_emoji, "available"),
        (_boolean_sticker, "animated"),
        (_boolean_sticker, "available"),
        (_boolean_message, "content_unavailable"),
        (_boolean_message, "attachments_unavailable"),
        (_boolean_message, "tts"),
        (_boolean_message, "view_persistent"),
    ],
)
def test_new_payload_boole_reject_truthiness_coercion(
    parser: Callable[[dict[str, object]], object],
    field: str,
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        parser({field: invalid})


def test_user_retains_federated_profile_and_e2ee_generations() -> None:
    user = User.from_payload(user_payload())

    assert user.ref == EntityRef(8, "users.example")
    assert user.profile_version == 7
    assert user.e2ee_device_generation == 4
    assert user.profile_resolved is False
    assert user.account_type == "human"


def test_guild_retains_replica_policy_generations_and_health() -> None:
    guild = Guild.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://guilds.example",
        {
            "id": "10",
            "origin_domain": "guilds.example",
            "name": "Remote Guild",
            "permission_generation": "12",
            "federated_history_policy": "metadata",
            "history_policy_generation": "9",
            "sync_status": "degraded",
            "sync_error_code": "KAED_FED_HISTORY_CAPACITY",
            "capability_revision": "11",
        },
    )

    assert guild.permission_generation == 12
    assert guild.federated_history_policy == "metadata"
    assert guild.history_policy_generation == 9
    assert guild.sync_status == "degraded"
    assert guild.sync_error_code == "KAED_FED_HISTORY_CAPACITY"
    assert guild.installation_revision == 11


def test_channel_retains_dm_capability_membership_history_and_e2ee_policy() -> None:
    grant_id = "kbdg_" + "a" * 43
    channel = Channel.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "55",
            "origin_domain": "chat.example",
            "type": 3,
            "nsfw": True,
            "permissions_synced": True,
            "recipients": [user_payload()],
            "conversation_type": "group",
            "federated_history_policy": "full",
            "encryption_mode": "e2ee",
            "encryption_state": "active",
            "encryption_policy_generation": "6",
            "encryption_protocol": "mls10",
            "encryption_suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
            "encryption_group_id": "opaque-group",
            "encryption_epoch": "14",
            "encryption_activated_at": "2026-08-28T12:00:00+00:00",
            "encryption_policy": {
                "mode": "e2ee",
                "state": "active",
                "generation": "6",
                "protocol": "mls10",
                "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
                "group_id": "opaque-group",
                "epoch": "14",
            },
            "history_truncated": True,
            "history_retention": "rolling_replica_cache",
            "history_source": "chat.example",
            "history_remote_available": True,
            "oldest_available_message_ref": {
                "id": "99",
                "origin_domain": "chat.example",
            },
            "history_degraded_code": "KAED_FED_DM_HISTORY_TRUNCATED",
            "bot_dm_capability_id": grant_id,
            "bot_dm_capability_revision": "3",
            "bot_installation_ref": "60@guilds.example",
            "bot_installation_type": "guild",
        },
    )

    assert channel.recipients[0].profile_version == 7
    assert channel.type == 3
    assert channel.conversation_type == "group"
    assert channel.nsfw is True
    assert channel.permissions_synced is True
    assert channel.federated_history_policy == "full"
    assert channel.encryption_state == "active"
    assert channel.encryption_policy_generation == 6
    assert channel.encryption_protocol == "mls10"
    assert channel.encryption_group_id == "opaque-group"
    assert channel.encryption_epoch == 14
    assert channel.encryption_policy == {
        "mode": "e2ee",
        "state": "active",
        "generation": "6",
        "protocol": "mls10",
        "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
        "group_id": "opaque-group",
        "epoch": "14",
    }
    assert channel.history_truncated is True
    assert channel.history_remote_available is True
    assert channel.oldest_available_message_ref == EntityRef(99, "chat.example")
    assert channel.dm_capability_id == grant_id
    assert channel.dm_capability_revision == 3
    assert channel.installation_ref == EntityRef(60, "guilds.example")
    assert channel.installation_type == "guild"


def test_message_and_attachment_retain_exact_dm_and_encrypted_lineage() -> None:
    grant_id = "kbdg_" + "b" * 43
    message = Message.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "101",
            "origin_domain": "chat.example",
            "channel_id": "55",
            "channel_domain": "chat.example",
            "author_id": "8",
            "author_domain": "users.example",
            "author": user_payload(),
            "content": None,
            "e2ee": {
                "protocol": "mls10",
                "group_id": "opaque-group",
                "epoch": "14",
                "ciphertext": "ciphertext",
            },
            "encryption_policy_generation": "6",
            "encryption_epoch": "14",
            "message_type": 0,
            "tts": True,
            "client_nonce": "idempotency-token",
            "mention_user_refs": [{"id": "9", "origin_domain": "remote.example"}],
            "attachments": [
                {
                    "id": "201",
                    "origin_domain": "chat.example",
                    "filename": "cipher.bin",
                    "content_type": "application/octet-stream",
                    "size": 42,
                    "scan_status": "clean",
                    "encryption_mode": "e2ee",
                    "encryption_protocol": "kaede-file-v1",
                }
            ],
            "webhook_id": "71",
            "webhook": {
                "id": "71",
                "origin_domain": "chat.example",
                "ref": "71@chat.example",
                "name": "Federated Hook",
                "avatar_hash": "hash",
            },
            "published_at": "2026-08-28T12:01:00+00:00",
            "forwarded_message_ref": "88@source.example",
            "forwarded_channel_id": "77",
            "forwarded_channel_domain": "source.example",
            "forward_snapshot": {"content": "snapshot"},
            "message_reference": {
                "type": 1,
                "message_id": "88",
                "message_domain": "source.example",
            },
            "view_version": 4,
            "view_persistent": True,
            "view_expires_at": "2026-08-29T12:00:00+00:00",
            "bot_dm_capability_id": grant_id,
            "bot_dm_capability_revision": "5",
            "installation_ref": "60@guilds.example",
            "installation_type": "user",
            "created_at": "2026-08-28T12:00:00+00:00",
        },
    )

    assert message.author_ref == EntityRef(8, "users.example")
    assert message.e2ee is not None and message.e2ee["ciphertext"] == "ciphertext"
    assert message.encryption_policy_generation == 6
    assert message.encryption_epoch == 14
    assert message.tts is True
    assert message.client_nonce == "idempotency-token"
    assert message.mention_user_refs == (EntityRef(9, "remote.example"),)
    assert message.webhook_ref == EntityRef(71, "chat.example")
    assert message.webhook is not None and message.webhook["name"] == "Federated Hook"
    assert message.published_at is not None
    assert message.forwarded_message_ref == EntityRef(88, "source.example")
    assert message.forwarded_channel_ref == EntityRef(77, "source.example")
    assert message.forward_snapshot == {"content": "snapshot"}
    assert message.message_reference is not None
    assert message.view_persistent is True
    assert message.view_expires_at is not None
    assert message.dm_capability_id == grant_id
    assert message.dm_capability_revision == 5
    assert message.installation_ref == EntityRef(60, "guilds.example")
    assert message.installation_type == "user"

    attachment = message.attachments[0]
    assert isinstance(attachment, Attachment)
    assert attachment.channel_ref == message.channel_ref
    assert attachment.dm_capability_id == grant_id
    assert attachment.dm_capability_revision == 5
    assert attachment.installation_ref == EntityRef(60, "guilds.example")
    assert attachment.installation_type == "user"


def test_channel_follow_message_retains_qualified_source_references() -> None:
    payload: dict[str, Any] = {
        "id": "101",
        "origin_domain": "target.example",
        "channel_id": "55",
        "channel_domain": "target.example",
        "author_id": "8",
        "author_domain": "users.example",
        "content": "upstream-news",
        "message_type": 12,
        "message_reference": {
            "type": 0,
            "channel_id": "77",
            "channel_domain": "source.example",
            "guild_id": "66",
            "guild_domain": "source.example",
        },
    }
    message = Message.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://target.example",
        payload,
    )

    assert message.followed_channel_ref == EntityRef(77, "source.example")
    assert message.followed_guild_ref == EntityRef(66, "source.example")
    assert message.message_reference_channel_ref == message.followed_channel_ref
    assert message.message_reference_guild_ref == message.followed_guild_ref
    assert message.message_reference == payload["message_reference"]

    invalid = dict(payload)
    invalid["message_reference"] = {
        "channel_id": "77",
        "channel_domain": "source.example",
    }
    with pytest.raises(ValueError, match="qualified channel and guild references"):
        Message.from_payload(
            StubClient(),  # type: ignore[arg-type]
            "https://target.example",
            invalid,
        )


def test_message_rejects_attachment_lineage_conflicts() -> None:
    with pytest.raises(ValueError, match="conflicts with its parent"):
        Message.from_payload(
            StubClient(),  # type: ignore[arg-type]
            "https://chat.example",
            {
                "id": "101",
                "origin_domain": "chat.example",
                "channel_id": "55",
                "channel_domain": "chat.example",
                "bot_dm_capability_id": "kbdg_" + "a" * 43,
                "attachments": [
                    {
                        "id": "201",
                        "origin_domain": "chat.example",
                        "filename": "cipher.bin",
                        "content_type": "application/octet-stream",
                        "size": 42,
                        "bot_dm_capability_id": "kbdg_" + "b" * 43,
                    }
                ],
            },
        )


def test_message_rejects_attachment_lineage_not_asserted_by_parent() -> None:
    with pytest.raises(ValueError, match="conflicts with its parent"):
        Message.from_payload(
            StubClient(),  # type: ignore[arg-type]
            "https://chat.example",
            {
                "id": "101",
                "origin_domain": "chat.example",
                "channel_id": "55",
                "channel_domain": "chat.example",
                "attachments": [
                    {
                        "id": "201",
                        "origin_domain": "chat.example",
                        "filename": "forged.bin",
                        "content_type": "application/octet-stream",
                        "size": 42,
                        "bot_dm_capability_id": "kbdg_" + "b" * 43,
                    }
                ],
            },
        )


def test_owned_thread_inherits_message_authorization() -> None:
    grant_id = "kbdg_" + "a" * 43
    message = Message.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "101",
            "origin_domain": "chat.example",
            "channel_id": "55",
            "channel_domain": "chat.example",
            "bot_dm_capability_id": grant_id,
            "thread": {
                "id": "56",
                "origin_domain": "chat.example",
                "type": 11,
            },
        },
    )

    assert message.thread is not None
    assert message.thread.dm_capability_id == grant_id


def test_referenced_message_does_not_inherit_parent_authorization() -> None:
    message = Message.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "101",
            "origin_domain": "chat.example",
            "channel_id": "55",
            "channel_domain": "chat.example",
            "bot_dm_capability_id": "kbdg_" + "a" * 43,
            "referenced_message": {
                "id": "88",
                "origin_domain": "chat.example",
                "channel_id": "55",
                "channel_domain": "chat.example",
                "attachments": [],
            },
        },
    )

    assert message.referenced_message is not None
    assert message.referenced_message.dm_capability_id is None


def test_ready_event_retains_bootstrap_dm_capabilities() -> None:
    capability = {
        "grant_id": "kbdg_" + "c" * 43,
        "installation_ref": "60@guilds.example",
        "installation_type": "guild",
        "channel_ref": "55@chat.example",
        "capability_revision": "2",
        "expires_at": "2026-08-28T12:10:00+00:00",
    }

    ready = ReadyEvent(
        target="https://apps.example",
        application_ref=EntityRef(1, "apps.example"),
        worker_id=2,
        installations=(),
        dm_capabilities=(capability,),
    )

    assert ready.dm_capabilities == (capability,)

    client = Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )
    parsed = client._event_model(  # noqa: SLF001
        "READY",
        {
            "application_ref": "1@apps.example",
            "worker_id": "2",
            "dm_capabilities": [capability],
        },
        target="https://apps.example",
        topic=None,
        sequence=0,
    )

    assert isinstance(parsed, ReadyEvent)
    assert parsed.dm_capabilities == (capability,)


def call_payload() -> dict[str, object]:
    grant_id = "kbdg_" + "c" * 43
    return {
        "id": "99",
        "channel_id": "55",
        "channel_domain": "chat.example",
        "authority_domain": "chat.example",
        "room": "d.55.99",
        "state": "active",
        "caller": "7@apps.example",
        "participants": ["7@apps.example", "8@users.example"],
        "created_at": 1,
        "bot_dm_capability_id": grant_id,
        "bot_dm_capability_revision": "4",
        "bot_installation_ref": "60@guilds.example",
        "bot_installation_type": "guild",
    }


def test_call_retains_exact_rest_capability_lineage() -> None:
    grant_id = "kbdg_" + "c" * 43
    call = Call.from_payload(
        StubClient(),  # type: ignore[arg-type]
        "https://chat.example",
        call_payload(),
    )

    assert call.dm_capability_id == grant_id
    assert call.dm_capability_revision == 4
    assert call.installation_ref == EntityRef(60, "guilds.example")
    assert call.installation_type == "guild"


@pytest.mark.parametrize(
    "change",
    [
        {"authority_domain": "other.example"},
        {"channel_domain": "other.example"},
        {"room": "d.55.100"},
        {"participants": ["7@apps.example", "7@apps.example"]},
        {"created_at": True},
        {"ended_at": 2},
        {"bot_dm_capability_revision": None},
        {"bot_installation_ref": "60"},
    ],
)
def test_call_rejects_substituted_identity_and_ambiguous_wire_values(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        Call.from_payload(
            StubClient(),  # type: ignore[arg-type]
            "https://chat.example",
            call_payload() | change,
            fallback_dm_capability_id="kbdg_" + "c" * 43,
        )

    with pytest.raises(ValueError, match="requested DM capability"):
        Call.from_payload(
            StubClient(),  # type: ignore[arg-type]
            "https://chat.example",
            call_payload(),
            fallback_dm_capability_id="kbdg_" + "d" * 43,
        )


@pytest.mark.asyncio
async def test_channel_conveniences_pin_the_retained_dm_capability() -> None:
    client = StubClient()
    call = object()
    active = object()
    voice = object()
    uploaded = object()
    client.start_call = AsyncMock(return_value=call)  # type: ignore[attr-defined]
    client.active_call = AsyncMock(return_value=active)  # type: ignore[attr-defined]
    client.connect_voice = AsyncMock(return_value=voice)  # type: ignore[attr-defined]
    client.upload_attachment = AsyncMock(  # type: ignore[attr-defined]
        return_value=uploaded
    )
    grant_id = "kbdg_" + "d" * 43
    channel = Channel.from_payload(
        client,  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "55",
            "origin_domain": "chat.example",
            "type": 1,
            "conversation_type": "direct",
            "bot_dm_capability_id": grant_id,
        },
    )

    assert await channel.start_call(ring=False) is call
    assert await channel.active_call() is active
    assert await channel.connect_voice() is voice
    assert (
        await channel.upload(
            b"payload",
            filename="payload.bin",
            content_type="application/octet-stream",
        )
        is uploaded
    )

    client.start_call.assert_awaited_once_with(  # type: ignore[attr-defined]
        channel.ref,
        target=channel.target,
        ring=False,
        dm_capability_id=grant_id,
    )
    client.active_call.assert_awaited_once_with(  # type: ignore[attr-defined]
        channel.ref,
        target=channel.target,
        dm_capability_id=grant_id,
    )
    assert client.connect_voice.await_args.kwargs["dm_capability_id"] == grant_id  # type: ignore[attr-defined]
    assert client.upload_attachment.await_args.kwargs["dm_capability_id"] == grant_id  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_group_dm_app_voice_helpers_fail_before_network_io() -> None:
    client = StubClient()
    client.start_call = AsyncMock()  # type: ignore[attr-defined]
    client.active_call = AsyncMock()  # type: ignore[attr-defined]
    client.connect_voice = AsyncMock()  # type: ignore[attr-defined]
    channel = Channel.from_payload(
        client,  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "55",
            "origin_domain": "chat.example",
            "type": 3,
            "conversation_type": "group",
        },
    )

    with pytest.raises(ValueError, match="group DMs"):
        await channel.start_call()
    with pytest.raises(ValueError, match="group DMs"):
        await channel.active_call()
    with pytest.raises(ValueError, match="group DMs"):
        await channel.connect_voice()

    client.start_call.assert_not_awaited()  # type: ignore[attr-defined]
    client.active_call.assert_not_awaited()  # type: ignore[attr-defined]
    client.connect_voice.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_channel_and_message_resources_do_not_switch_to_a_new_default_grant() -> (
    None
):
    client = StubClient()
    operation_names = {
        "send_message",
        "send_sticker",
        "history",
        "pins",
        "trigger_typing",
        "voice_occupancy",
        "start_thread",
        "fetch_threads",
        "delete_thread",
        "join_thread",
        "leave_thread",
        "add_thread_member",
        "remove_thread_member",
        "thread_members",
        "fetch_thread_member",
        "edit_thread",
        "edit_message",
        "delete_message",
        "add_reaction",
        "remove_reaction",
        "remove_user_reaction",
        "clear_reactions",
        "clear_reaction",
        "reaction_users",
        "finalize_poll",
        "crosspost_message",
        "pin_message",
        "unpin_message",
        "start_thread_from_message",
    }
    for name in operation_names:
        setattr(client, name, AsyncMock(return_value=object()))

    retained_grant = "kbdg_" + "1" * 43
    replacement_grant = "kbdg_" + "2" * 43
    channel = Channel.from_payload(
        client,  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "55",
            "origin_domain": "chat.example",
            "type": 1,
            "conversation_type": "direct",
            "bot_dm_capability_id": retained_grant,
        },
    )
    # Model objects must remain pinned even after SDK discovery chooses another
    # default capability for the same conversation.
    client._dm_default_capabilities = {channel.ref: replacement_grant}  # type: ignore[attr-defined]  # noqa: SLF001

    await channel.send("hello")
    await channel.send_sticker(object())  # type: ignore[arg-type]
    await channel.history()
    await channel.pins()
    await channel.trigger_typing()
    await channel.voice_occupancy()
    await channel.start_thread("topic")
    await channel.threads()

    thread = Channel.from_payload(
        client,  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "56",
            "origin_domain": "chat.example",
            "type": 11,
            "bot_dm_capability_id": retained_grant,
        },
    )
    user_ref = EntityRef(8, "users.example")
    await thread.delete()
    await thread.join()
    await thread.leave()
    await thread.add_member(user_ref)
    await thread.remove_member(user_ref)
    await thread.members()
    await thread.fetch_member(user_ref)
    await thread.edit_thread(name="renamed")

    message = Message.from_payload(
        client,  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "101",
            "origin_domain": "chat.example",
            "channel_id": "55",
            "channel_domain": "chat.example",
            "bot_dm_capability_id": retained_grant,
        },
    )
    await message.reply("reply")
    await message.edit("edited")
    await message.delete()
    reaction_emoji = "👋"
    await message.add_reaction(reaction_emoji)
    await message.remove_reaction(reaction_emoji)
    await message.remove_user_reaction(user_ref, reaction_emoji)
    await message.clear_reactions()
    await message.clear_reaction(reaction_emoji)
    await message.reaction_users(reaction_emoji)
    await message.end_poll()
    await message.publish()
    await message.pin()
    await message.unpin()
    await message.start_thread("message topic")

    for name in operation_names:
        mock = getattr(client, name)
        assert mock.await_count > 0, name
        assert mock.await_args.kwargs["dm_capability_id"] == retained_grant, name


@pytest.mark.asyncio
async def test_attachment_conveniences_pin_channel_and_dm_capability() -> None:
    client = StubClient()
    refreshed = object()
    client.fetch_attachment = AsyncMock(  # type: ignore[attr-defined]
        return_value=refreshed
    )
    client.download_attachment = AsyncMock(  # type: ignore[attr-defined]
        return_value=b"ciphertext"
    )
    grant_id = "kbdg_" + "e" * 43
    attachment = Attachment.from_payload(
        client,  # type: ignore[arg-type]
        "https://chat.example",
        {
            "id": "201",
            "origin_domain": "chat.example",
            "filename": "cipher.bin",
            "content_type": "application/octet-stream",
            "size": 42,
            "channel_id": "55",
            "channel_domain": "chat.example",
            "bot_dm_capability_id": grant_id,
        },
    )

    assert await attachment.refresh() is refreshed
    assert await attachment.read(max_bytes=100) == b"ciphertext"
    client.fetch_attachment.assert_awaited_once_with(  # type: ignore[attr-defined]
        attachment.ref,
        target=attachment.target,
        installation_id=None,
        channel_ref=EntityRef(55, "chat.example"),
        dm_capability_id=grant_id,
    )
    client.download_attachment.assert_awaited_once_with(  # type: ignore[attr-defined]
        attachment.ref,
        variant="original",
        target=attachment.target,
        max_bytes=100,
        installation_id=None,
        channel_ref=EntityRef(55, "chat.example"),
        dm_capability_id=grant_id,
    )


@pytest.mark.asyncio
async def test_client_attachment_round_trip_preserves_exact_channel_context() -> None:
    client = Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )
    grant_id = "kbdg_" + "f" * 43
    ticket = {
        "id": "201",
        "origin_domain": "chat.example",
        "filename": "cipher.bin",
        "content_type": "application/octet-stream",
        "size": 7,
        "upload_url": "https://media.chat.example/upload",
        "media_origin": "https://media.chat.example",
        "bot_dm_capability_id": grant_id,
        "bot_dm_capability_revision": "4",
        "bot_installation_ref": "60@guilds.example",
        "bot_installation_type": "guild",
    }
    client.request = AsyncMock(side_effect=[ticket, ticket])  # type: ignore[method-assign]
    client._runtime_grant_headers = AsyncMock(return_value={})  # type: ignore[method-assign]  # noqa: SLF001
    client._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    channel_ref = EntityRef(55, "chat.example")

    uploaded = await client.upload_attachment(
        channel_ref,
        b"payload",
        filename="cipher.bin",
        content_type="application/octet-stream",
        target="https://chat.example",
        dm_capability_id=grant_id,
    )
    refreshed = await client.fetch_attachment(
        uploaded.ref,
        target=uploaded.target,
        channel_ref=uploaded.channel_ref,
        dm_capability_id=uploaded.dm_capability_id,
    )

    assert uploaded.channel_ref == channel_ref
    assert refreshed.channel_ref == channel_ref
    assert uploaded.media_origin == "https://media.chat.example"
    assert refreshed.media_origin == "https://media.chat.example"
    assert refreshed.dm_capability_id == grant_id
    assert refreshed.dm_capability_revision == 4
