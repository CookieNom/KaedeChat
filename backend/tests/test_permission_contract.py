import pytest

from app.core.channel_types import GUILD_WEBHOOK_CHANNEL_TYPES
from app.core.permission_contract import PERMISSION_CONTRACT, required_permissions
from app.core.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_ALIASES,
    PERMISSION_METADATA,
    PERMISSION_SCHEMA,
    Permission,
    permission_mask_from_wire,
    permission_mask_to_wire,
)


def test_permission_metadata_is_total_and_internally_consistent() -> None:
    assert {item.permission for item in PERMISSION_METADATA} == set(Permission)
    for item in PERMISSION_METADATA:
        assert item.resource_scopes
        assert item.permission.value & ~ALL_PERMISSIONS == 0
        assert all(dependency in Permission for dependency in item.dependencies)
        assert item.danger in {"normal", "elevated", "dangerous", "critical"}


def test_endpoint_permission_contract_is_unique_known_and_nonempty() -> None:
    assert len(PERMISSION_CONTRACT) >= 30
    for operation, contract in PERMISSION_CONTRACT.items():
        assert operation == contract.operation
        assert contract.scope in {"guild", "channel"}
        if operation == "guild.expression.read":
            assert contract.permission == Permission(0)
            assert contract.conditional == (
                "guild membership is required; no expression permission is required"
            )
        else:
            assert contract.permission
        assert int(contract.permission) & ~ALL_PERMISSIONS == 0
        assert required_permissions(operation) == contract.permission
    with pytest.raises(RuntimeError, match="unknown permission contract"):
        required_permissions("unregistered.operation")


def test_announcement_follow_only_manages_the_destination_webhook() -> None:
    assert required_permissions("announcement.follow.source") == Permission.VIEW_CHANNEL
    assert required_permissions("webhook.manage") == Permission.MANAGE_WEBHOOKS
    metadata = next(
        item for item in PERMISSION_METADATA if item.permission == Permission.MANAGE_WEBHOOKS
    )
    assert set(metadata.channel_types) == GUILD_WEBHOOK_CHANNEL_TYPES == {0, 5, 15}


def test_federated_instance_bans_use_a_dedicated_critical_permission() -> None:
    assert required_permissions("instance_ban.list") == Permission.BAN_INSTANCES
    assert required_permissions("instance_ban.put") == Permission.BAN_INSTANCES
    assert required_permissions("instance_ban.remove") == Permission.BAN_INSTANCES
    metadata = next(
        item for item in PERMISSION_METADATA if item.permission == Permission.BAN_INSTANCES
    )
    assert metadata.resource_scopes == ("guild",)
    assert metadata.danger == "critical"


def test_thread_and_forum_permission_bits_are_stable() -> None:
    assert Permission.PRIORITY_SPEAKER == 1 << 8
    assert Permission.USE_APPLICATION_COMMANDS == 1 << 32
    assert Permission.MANAGE_THREADS == 1 << 34
    assert Permission.CREATE_PUBLIC_THREADS == 1 << 35
    assert Permission.CREATE_PRIVATE_THREADS == 1 << 36
    assert Permission.SEND_MESSAGES_IN_THREADS == 1 << 38
    assert Permission.PIN_MESSAGES == 1 << 51
    assert Permission.BYPASS_SLOWMODE == 1 << 52
    # Existing persisted assignments must never move to make room for parity.
    assert Permission.STREAM == 1 << 31
    assert Permission.BAN_INSTANCES == 1 << 41


def test_permission_masks_round_trip_only_under_the_kaede_v1_schema() -> None:
    mask = Permission.STREAM | Permission.USE_APPLICATION_COMMANDS
    encoded = permission_mask_to_wire(mask)

    assert encoded == {
        "schema": "kaede-permissions-v1",
        "value": str((1 << 31) | (1 << 32)),
    }
    assert PERMISSION_SCHEMA == "kaede-permissions-v1"
    assert permission_mask_from_wire(encoded) == mask


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "1"},
        {"schema": "discord-api-v10", "value": "1"},
        {"schema": "kaede-permissions-v1", "value": 1},
        {"schema": "kaede-permissions-v1", "value": "01"},
        {"schema": "kaede-permissions-v1", "value": str(1 << 63)},
        {"schema": "kaede-permissions-v1", "value": "1", "extra": True},
    ],
)
def test_permission_mask_wire_import_rejects_ambiguous_or_unknown_layouts(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="permission"):
        permission_mask_from_wire(payload)


@pytest.mark.parametrize("mask", [-1, 1 << 63, True])
def test_permission_mask_wire_export_rejects_invalid_values(mask: object) -> None:
    with pytest.raises(ValueError, match="permission"):
        permission_mask_to_wire(mask)  # type: ignore[arg-type]


def test_every_published_permission_name_and_bit_is_unchanged() -> None:
    published = {
        "CREATE_INVITE": 0,
        "KICK_MEMBERS": 1,
        "BAN_MEMBERS": 2,
        "ADMINISTRATOR": 3,
        "MANAGE_CHANNELS": 4,
        "MANAGE_GUILD": 5,
        "ADD_REACTIONS": 6,
        "VIEW_AUDIT_LOG": 7,
        "VIEW_CHANNEL": 10,
        "SEND_MESSAGES": 11,
        "MANAGE_MESSAGES": 13,
        "EMBED_LINKS": 14,
        "ATTACH_FILES": 15,
        "READ_MESSAGE_HISTORY": 16,
        "MENTION_EVERYONE": 17,
        "USE_EXTERNAL_EMOJIS": 18,
        "CONNECT": 20,
        "SPEAK": 21,
        "MUTE_MEMBERS": 22,
        "DEAFEN_MEMBERS": 23,
        "MOVE_MEMBERS": 24,
        "USE_VAD": 25,
        "CHANGE_NICKNAME": 26,
        "MANAGE_NICKNAMES": 27,
        "MANAGE_ROLES": 28,
        "MANAGE_WEBHOOKS": 29,
        "MANAGE_EMOJIS": 30,
        "STREAM": 31,
        "USE_APPLICATION_COMMANDS": 32,
        "MANAGE_THREADS": 34,
        "CREATE_PUBLIC_THREADS": 35,
        "CREATE_PRIVATE_THREADS": 36,
        "SEND_MESSAGES_IN_THREADS": 38,
        "MODERATE_MEMBERS": 40,
        "BAN_INSTANCES": 41,
        "PIN_MESSAGES": 51,
        "BYPASS_SLOWMODE": 52,
        "CREATE_TRACKER_TASKS": 53,
        "EDIT_OWN_TRACKER_TASKS": 54,
        "MANAGE_TRACKER_TASKS": 55,
        "ASSIGN_TRACKER_TASKS": 56,
        "MANAGE_TRACKER": 57,
    }
    assert {name: Permission[name].bit_length() - 1 for name in published} == published


def test_additive_discord_compatibility_bits_fill_only_published_holes() -> None:
    expected = {
        "PRIORITY_SPEAKER": 8,
        "SEND_TTS_MESSAGES": 12,
        "USE_EXTERNAL_STICKERS": 58,
        "REQUEST_TO_SPEAK": 33,
        "MANAGE_EVENTS": 39,
        "USE_SOUNDBOARD": 43,
        "CREATE_GUILD_EXPRESSIONS": 44,
        "CREATE_EVENTS": 45,
        "USE_EXTERNAL_SOUNDS": 46,
        "SEND_VOICE_MESSAGES": 47,
        "SET_VOICE_CHANNEL_STATUS": 48,
        "SEND_POLLS": 49,
        "USE_EXTERNAL_APPS": 50,
    }
    assert {
        name: permission.bit_length() - 1
        for name, permission in Permission.__members__.items()
        if name in expected
    } == expected
    reserved = {9, 19, 37, 42}
    assert ((1 << 59) - 1) ^ sum(1 << bit for bit in reserved) == ALL_PERMISSIONS
    assert all(not ALL_PERMISSIONS & (1 << bit) for bit in reserved)


def test_priority_speaker_is_scoped_to_ordinary_voice_with_speaking_dependencies() -> None:
    metadata = next(
        item for item in PERMISSION_METADATA if item.permission == Permission.PRIORITY_SPEAKER
    )

    assert metadata.channel_types == (2,)
    assert metadata.dependencies == (Permission.CONNECT, Permission.SPEAK)


def test_voice_and_stage_text_chat_permissions_match_discord_applicability() -> None:
    expected = {0, 2, 5, 10, 11, 12, 13, 15}
    for permission in (
        Permission.ADD_REACTIONS,
        Permission.SEND_TTS_MESSAGES,
        Permission.MANAGE_MESSAGES,
        Permission.EMBED_LINKS,
        Permission.ATTACH_FILES,
        Permission.READ_MESSAGE_HISTORY,
        Permission.MENTION_EVERYONE,
        Permission.USE_EXTERNAL_EMOJIS,
        Permission.USE_EXTERNAL_STICKERS,
        Permission.SEND_VOICE_MESSAGES,
        Permission.SEND_POLLS,
        Permission.BYPASS_SLOWMODE,
    ):
        metadata = next(item for item in PERMISSION_METADATA if item.permission == permission)
        assert set(metadata.channel_types) == expected
    pin_metadata = next(
        item for item in PERMISSION_METADATA if item.permission == Permission.PIN_MESSAGES
    )
    assert set(pin_metadata.channel_types) == {0, 5, 10, 11, 12, 15}

    send_messages = next(
        item for item in PERMISSION_METADATA if item.permission == Permission.SEND_MESSAGES
    )
    assert set(send_messages.channel_types) == {0, 2, 5, 13, 15}


def test_soundboard_playback_requires_voice_speaking_permissions() -> None:
    """Keep every authorization surface aligned with Discord's voice contract."""

    required = (
        Permission.VIEW_CHANNEL | Permission.CONNECT | Permission.SPEAK | Permission.USE_SOUNDBOARD
    )
    assert required_permissions("soundboard.use") == required

    use_soundboard = next(
        item for item in PERMISSION_METADATA if item.permission == Permission.USE_SOUNDBOARD
    )
    assert use_soundboard.dependencies == (
        Permission.VIEW_CHANNEL,
        Permission.CONNECT,
        Permission.SPEAK,
    )

    external_sounds = next(
        item for item in PERMISSION_METADATA if item.permission == Permission.USE_EXTERNAL_SOUNDS
    )
    assert external_sounds.dependencies == (*use_soundboard.dependencies, Permission.USE_SOUNDBOARD)


def test_manage_expressions_metadata_does_not_promise_the_separate_create_capability() -> None:
    manage = next(
        item for item in PERMISSION_METADATA if item.permission == Permission.MANAGE_EMOJIS
    )

    assert manage.label == "Manage guild expressions"
    assert manage.description == (
        "Edit and remove emoji, stickers, and soundboard sounds created by other members."
    )


def test_discord_compatibility_names_are_explicit_same_bit_aliases() -> None:
    assert Permission.CREATE_INSTANT_INVITE is Permission.CREATE_INVITE
    assert Permission.MANAGE_GUILD_EXPRESSIONS is Permission.MANAGE_EMOJIS
    assert Permission.MANAGE_AUTO_MODERATION is Permission.MANAGE_GUILD
    assert PERMISSION_ALIASES == {
        "CREATE_INSTANT_INVITE": Permission.CREATE_INVITE,
        "MANAGE_GUILD_EXPRESSIONS": Permission.MANAGE_EMOJIS,
        "MANAGE_AUTO_MODERATION": Permission.MANAGE_GUILD,
    }
    assert "CREATE_INSTANT_INVITE" not in {item.name for item in Permission}
    assert "MANAGE_GUILD_EXPRESSIONS" not in {item.name for item in Permission}
    assert "MANAGE_AUTO_MODERATION" not in {item.name for item in Permission}


def test_thread_operations_use_their_dedicated_permissions() -> None:
    assert required_permissions("forum.post.create") == (
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES
    )
    assert required_permissions("thread.create.public") == (
        Permission.VIEW_CHANNEL | Permission.CREATE_PUBLIC_THREADS
    )
    assert required_permissions("thread.create.private") == (
        Permission.VIEW_CHANNEL | Permission.CREATE_PRIVATE_THREADS
    )
    assert required_permissions("thread.message.create") == (
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES_IN_THREADS
    )
    assert required_permissions("thread.update.other") == (
        Permission.VIEW_CHANNEL | Permission.MANAGE_THREADS
    )
    assert required_permissions("thread.member.join") == Permission.VIEW_CHANNEL
    assert required_permissions("thread.member.add") == (
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES_IN_THREADS
    )
    assert required_permissions("pin.update") == (
        Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY | Permission.PIN_MESSAGES
    )
    assert required_permissions("pin.list") == Permission.VIEW_CHANNEL
    assert required_permissions("application.command.use") == (
        Permission.VIEW_CHANNEL | Permission.USE_APPLICATION_COMMANDS
    )


def test_destructive_bulk_moderation_requires_manage_guild_too() -> None:
    assert required_permissions("guild.prune") == (
        Permission.MANAGE_GUILD | Permission.KICK_MEMBERS
    )
    assert required_permissions("guild.bulk_ban") == (
        Permission.MANAGE_GUILD | Permission.BAN_MEMBERS
    )
