import ast
import inspect
import textwrap
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import Client
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


AUTHORITY = "https://authority.example"
OTHER = "https://other.example"
GUILD = EntityRef(10, "authority.example")
CHANNEL = EntityRef(20, "authority.example")
MESSAGE = EntityRef(30, "authority.example")
USER = EntityRef(40, "authority.example")


class RequestObserved(Exception):
    pass


def client() -> Client:
    bot = Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )
    bot._targets.update(  # noqa: SLF001 - exercise routing without network setup
        {AUTHORITY: AsyncMock(), OTHER: AsyncMock()}
    )
    return bot


ResourceCall = Callable[[Client], Awaitable[object]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda bot: bot.fetch_user(USER, target=OTHER), id="user-profile"),
        pytest.param(
            lambda bot: bot.fetch_tracker(CHANNEL, target=OTHER), id="tracker"
        ),
        pytest.param(
            lambda bot: bot.delete_tracker_lane(
                CHANNEL, MESSAGE, target=OTHER, version="v1"
            ),
            id="tracker-lane-delete",
        ),
        pytest.param(
            lambda bot: bot.delete_tracker_task(
                CHANNEL, MESSAGE, target=OTHER, version="v1"
            ),
            id="tracker-task-delete",
        ),
        pytest.param(
            lambda bot: bot.fetch_threads(CHANNEL, target=OTHER), id="threads"
        ),
        pytest.param(
            lambda bot: bot.delete_thread(CHANNEL, target=OTHER), id="thread-delete"
        ),
        pytest.param(
            lambda bot: bot.join_thread(CHANNEL, target=OTHER), id="thread-join"
        ),
        pytest.param(
            lambda bot: bot.leave_thread(CHANNEL, target=OTHER), id="thread-leave"
        ),
        pytest.param(
            lambda bot: bot.fetch_members(GUILD, target=OTHER), id="guild-members"
        ),
        pytest.param(
            lambda bot: bot.create_channel(GUILD, "general", target=OTHER),
            id="guild-channel-management",
        ),
        pytest.param(
            lambda bot: bot.delete_channel(GUILD, CHANNEL, target=OTHER),
            id="channel-delete",
        ),
        pytest.param(
            lambda bot: bot.fetch_channel_overwrites(GUILD, CHANNEL, target=OTHER),
            id="channel-overwrites",
        ),
        pytest.param(
            lambda bot: bot.delete_role(GUILD, MESSAGE, target=OTHER),
            id="role-delete",
        ),
        pytest.param(
            lambda bot: bot.add_member_role(GUILD, USER, MESSAGE, target=OTHER),
            id="member-role-add",
        ),
        pytest.param(
            lambda bot: bot.upload_attachment(
                CHANNEL,
                b"x",
                filename="image.png",
                content_type="image/png",
                target=OTHER,
            ),
            id="attachment-upload",
        ),
        pytest.param(lambda bot: bot.create_invite(GUILD, target=OTHER), id="invites"),
        pytest.param(
            lambda bot: bot.channel_invites(GUILD, CHANNEL, target=OTHER),
            id="channel-invites",
        ),
        pytest.param(
            lambda bot: bot.revoke_invite(GUILD, "Abcd1234", target=OTHER),
            id="invite-revoke",
        ),
        pytest.param(
            lambda bot: bot.fetch_invite_target_users(
                GUILD,
                "Abcd1234",
                target=OTHER,
            ),
            id="invite-target-users",
        ),
        pytest.param(
            lambda bot: bot.update_invite_target_users(
                GUILD,
                "Abcd1234",
                [USER],
                target=OTHER,
            ),
            id="invite-target-users-update",
        ),
        pytest.param(
            lambda bot: bot.fetch_invite_target_users_job_status(
                GUILD,
                "Abcd1234",
                target=OTHER,
            ),
            id="invite-target-users-status",
        ),
        pytest.param(
            lambda bot: bot.create_webhook(
                GUILD, CHANNEL, "release hook", target=OTHER
            ),
            id="webhook-management",
        ),
        pytest.param(
            lambda bot: bot.delete_webhook(GUILD, 50, target=OTHER),
            id="webhook-delete",
        ),
        pytest.param(lambda bot: bot.emojis(GUILD, target=OTHER), id="emojis"),
        pytest.param(
            lambda bot: bot.delete_emoji(GUILD, 7, target=OTHER), id="emoji-delete"
        ),
        pytest.param(lambda bot: bot.stickers(GUILD, target=OTHER), id="stickers"),
        pytest.param(
            lambda bot: bot.delete_sticker(GUILD, 8, target=OTHER),
            id="sticker-delete",
        ),
        pytest.param(
            lambda bot: bot.soundboard_sounds(GUILD, target=OTHER), id="soundboard"
        ),
        pytest.param(
            lambda bot: bot.delete_soundboard_sound(GUILD, MESSAGE, target=OTHER),
            id="soundboard-delete",
        ),
        pytest.param(
            lambda bot: bot.set_voice_moderation(
                GUILD, USER, target=OTHER, server_mute=True
            ),
            id="voice-moderation",
        ),
        pytest.param(
            lambda bot: bot.disconnect_voice(GUILD, USER, target=OTHER),
            id="voice-disconnect",
        ),
        pytest.param(
            lambda bot: bot.move_voice(GUILD, USER, CHANNEL, target=OTHER),
            id="voice-move",
        ),
        pytest.param(
            lambda bot: bot.search_guild_messages(GUILD, "incident", target=OTHER),
            id="message-search",
        ),
        pytest.param(
            lambda bot: bot.send_message(CHANNEL, "hello", target=OTHER),
            id="messages",
        ),
        pytest.param(
            lambda bot: bot.delete_message(CHANNEL, MESSAGE, target=OTHER),
            id="message-delete",
        ),
        pytest.param(
            lambda bot: bot.add_reaction(CHANNEL, MESSAGE, "👋", target=OTHER),
            id="reaction-add",
        ),
        pytest.param(
            lambda bot: bot.finalize_poll(CHANNEL, MESSAGE, target=OTHER), id="polls"
        ),
        pytest.param(lambda bot: bot.pins(CHANNEL, target=OTHER), id="pins"),
        pytest.param(
            lambda bot: bot.pin_message(CHANNEL, MESSAGE, target=OTHER),
            id="pin-add",
        ),
        pytest.param(
            lambda bot: bot.trigger_typing(CHANNEL, target=OTHER), id="typing"
        ),
        pytest.param(lambda bot: bot.auto_mod_rules(GUILD, target=OTHER), id="automod"),
        pytest.param(
            lambda bot: bot.delete_auto_mod_rule(GUILD, 9, target=OTHER),
            id="automod-delete",
        ),
        pytest.param(lambda bot: bot.estimate_prune(GUILD, target=OTHER), id="prune"),
        pytest.param(
            lambda bot: bot.fetch_audit_logs(GUILD, target=OTHER), id="audit-log"
        ),
        pytest.param(
            lambda bot: bot.create_stage_instance(CHANNEL, "Town hall", target=OTHER),
            id="stage",
        ),
        pytest.param(
            lambda bot: bot.delete_stage_instance(CHANNEL, target=OTHER),
            id="stage-delete",
        ),
        pytest.param(
            lambda bot: bot.fetch_current_stage_voice_state(GUILD, target=OTHER),
            id="stage-voice-state",
        ),
        pytest.param(
            lambda bot: bot.voice_occupancy(CHANNEL, target=OTHER),
            id="voice-occupancy",
        ),
        pytest.param(
            lambda bot: bot.voice_regions(GUILD, target=OTHER), id="voice-regions"
        ),
        pytest.param(lambda bot: bot.connect_voice(CHANNEL, target=OTHER), id="voice"),
        pytest.param(
            lambda bot: bot.fetch_webhook_message(50, "token", MESSAGE, target=OTHER),
            id="webhook-message",
        ),
        pytest.param(
            lambda bot: bot.execute_webhook(
                50, "token", "hello", target=OTHER, thread_id=CHANNEL
            ),
            id="webhook-thread",
        ),
        pytest.param(
            lambda bot: bot.delete_webhook_with_token(
                EntityRef(50, "authority.example"),
                "token",
                target=OTHER,
            ),
            id="qualified-token-webhook",
        ),
    ],
)
async def test_qualified_resources_override_a_replica_target(
    invoke: ResourceCall,
) -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=RequestObserved
    )

    with pytest.raises(RequestObserved):
        await invoke(bot)

    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["target"] == AUTHORITY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(
            lambda bot: bot.delete_application_asset(7),
            id="application-asset-delete",
        ),
        pytest.param(
            lambda bot: bot.delete_application_emoji(8),
            id="application-emoji-delete",
        ),
    ],
)
async def test_application_media_deletes_always_use_application_home(
    invoke: ResourceCall,
) -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=RequestObserved
    )

    with pytest.raises(RequestObserved):
        await invoke(bot)

    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["target"] == "https://apps.example"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(
            lambda bot: bot.commit_application_asset(
                EntityRef(60, "authority.example"), "icon", "primary"
            ),
            id="application-asset-attachment",
        ),
        pytest.param(
            lambda bot: bot.commit_application_emoji(
                EntityRef(60, "authority.example"), "party"
            ),
            id="application-emoji-attachment",
        ),
        pytest.param(
            lambda bot: bot.commit_guild_asset(
                GUILD, "icon", EntityRef(60, "other.example")
            ),
            id="guild-asset-attachment",
        ),
        pytest.param(
            lambda bot: bot.commit_role_icon(
                GUILD,
                EntityRef(20, "authority.example"),
                EntityRef(60, "other.example"),
            ),
            id="role-icon-attachment",
        ),
        pytest.param(
            lambda bot: bot.commit_emoji(
                GUILD, EntityRef(60, "other.example"), "party"
            ),
            id="guild-emoji-attachment",
        ),
        pytest.param(
            lambda bot: bot.commit_sticker(
                GUILD,
                EntityRef(60, "other.example"),
                "party",
                tags=("party",),
            ),
            id="sticker-attachment",
        ),
        pytest.param(
            lambda bot: bot.commit_soundboard_sound(
                GUILD, EntityRef(60, "other.example"), "airhorn"
            ),
            id="soundboard-attachment",
        ),
        pytest.param(
            lambda bot: bot.edit_channel(
                GUILD,
                EntityRef(CHANNEL.id, "other.example"),
                version="v1",
                name="general",
            ),
            id="guild-channel",
        ),
        pytest.param(
            lambda bot: bot.edit_role(
                GUILD,
                EntityRef(MESSAGE.id, "other.example"),
                version="v1",
                name="helpers",
            ),
            id="guild-role",
        ),
        pytest.param(
            lambda bot: bot.fetch_scheduled_event(
                GUILD, EntityRef(MESSAGE.id, "other.example")
            ),
            id="scheduled-event",
        ),
        pytest.param(
            lambda bot: bot.create_webhook(
                GUILD, EntityRef(CHANNEL.id, "other.example"), "deploy"
            ),
            id="webhook-channel",
        ),
        pytest.param(
            lambda bot: bot.execute_webhook(
                EntityRef(50, "authority.example"),
                "token",
                "hello",
                thread_id=EntityRef(CHANNEL.id, "other.example"),
            ),
            id="token-webhook-thread",
        ),
        pytest.param(
            lambda bot: bot.fetch_webhook_message(
                EntityRef(50, "authority.example"),
                "token",
                EntityRef(MESSAGE.id, "other.example"),
            ),
            id="token-webhook-message",
        ),
        pytest.param(
            lambda bot: bot.upload_webhook_attachment(
                EntityRef(50, "authority.example"),
                "token",
                b"ciphertext",
                filename="secret.bin",
                content_type="application/octet-stream",
                channel_ref=EntityRef(CHANNEL.id, "other.example"),
            ),
            id="token-webhook-attachment",
        ),
        pytest.param(
            lambda bot: bot.channel_invites(
                GUILD, EntityRef(CHANNEL.id, "other.example")
            ),
            id="invite-channel",
        ),
        pytest.param(
            lambda bot: bot.move_voice(
                GUILD, USER, EntityRef(CHANNEL.id, "other.example")
            ),
            id="voice-channel",
        ),
    ],
)
async def test_nested_resources_reject_same_id_from_another_authority(
    invoke: ResourceCall,
) -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=RequestObserved
    )

    with pytest.raises(ValueError, match="authority"):
        await invoke(bot)

    bot.request.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/bots/users/40@authority.example",
        "/api/v1/bots/guilds/10@authority.example/channels",
        "/api/v1/bots/channels/20@authority.example/messages",
        "/api/v1/bots/attachments/60@authority.example/original",
        "/api/v1/bots/stage-instances/20@authority.example",
    ],
)
def test_resource_paths_bind_to_the_qualified_authority(path: str) -> None:
    bot = client()
    assert bot._request_target(path, OTHER) == AUTHORITY  # noqa: SLF001


def test_direct_generic_target_resolution_is_limited_to_unqualified_contexts() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(Client)))
    callers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name.startswith("_") or "e2ee" in node.name:
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "_target"
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "self"
            ):
                callers.add(node.name)

    # These APIs carry no qualified authority on the wire: target-scoped
    # collections, DM creation, token-only webhooks, interaction IDs, and
    # events already tied to the connection that delivered them.
    allowed = {
        "add_view",
        "default_soundboard_sounds",
        "dispatch",
        "edit_interaction_followup",
        "edit_original_interaction_response",
        "edit_webhook_with_token",
        "fetch_guilds",
        "fetch_interaction_followup",
        "fetch_interaction_input_attachment",
        "fetch_original_interaction_response",
        "finalize_interaction_poll",
        "fetch_webhook_with_token",
        "interaction_callback",
        "open_dm",
        "upload_interaction_attachment",
        "upload_webhook_attachment",
        "upload_webhook_avatar_with_token",
        "delete_webhook_avatar_with_token",
        "create_interaction_followup",
    }
    assert not callers - allowed, sorted(callers - allowed)
