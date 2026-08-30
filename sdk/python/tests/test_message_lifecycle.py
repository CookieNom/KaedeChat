from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Coroutine
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import kaede_bot.client as client_module
from kaede_bot import Client, EntityRef, Interaction, Message, WorkerState


TARGET = "https://chat.example"
DEVICE_ID = "kbe_" + "d" * 43


def client(*, device_id: str | None = None) -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "lifecycle",
        ),
        e2ee_device_id=device_id,
    )


def message_payload(*, response_id: int = 81) -> dict[str, object]:
    return {
        "id": "9",
        "origin_domain": "chat.example",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "content": "response",
        "created_at": "2026-08-28T00:00:00+00:00",
        "attachments": [],
        "interaction_id": "70",
        "response_id": str(response_id),
    }


@pytest.mark.asyncio
async def test_interaction_message_uses_exact_lifecycle_for_edit_delete_and_poll() -> (
    None
):
    bot = client(device_id=DEVICE_ID)
    payload = message_payload()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[payload, payload, payload, None]
    )

    message = await bot.fetch_original_interaction_response(
        70,
        target=TARGET,
        installation_id=44,
    )
    assert isinstance(message, Message)
    edited = await message.edit(content="changed")
    ended = await message.end_poll()
    await message.delete()

    assert isinstance(edited, Message)
    assert edited.bot_installation_id == 44
    assert isinstance(ended, Message)
    calls = bot.request.await_args_list
    assert calls[0].args == (
        "GET",
        "/api/v1/bots/interactions/70/responses/@original",
    )
    assert calls[0].kwargs["headers"] == {"X-Kaede-E2EE-Device": DEVICE_ID}
    assert calls[1].args == (
        "PATCH",
        "/api/v1/bots/interactions/70/responses/@original",
    )
    assert calls[1].kwargs["json"] == {"content": "changed"}
    assert calls[2].args == (
        "POST",
        "/api/v1/bots/interactions/70/responses/@original/polls/expire",
    )
    assert calls[3].args == (
        "DELETE",
        "/api/v1/bots/interactions/70/responses/@original",
    )
    assert all("/bots/channels/" not in call.args[1] for call in calls)


@pytest.mark.asyncio
async def test_user_install_followup_is_lifecycle_only_and_sends_exact_device() -> None:
    bot = client(device_id=DEVICE_ID)
    payload = message_payload(response_id=82)
    bot.request = AsyncMock(side_effect=[payload, payload, None])  # type: ignore[method-assign]

    message = await bot.fetch_interaction_followup(
        70,
        82,
        target=TARGET,
        user_installation=True,
    )
    assert isinstance(message, Message)
    edited = await message.edit(e2ee={"protocol": "kaede-e2ee-v1"})
    await message.delete()

    assert isinstance(edited, Message)
    assert bot.request.await_args_list[0].kwargs["headers"] == {
        "X-Kaede-E2EE-Device": DEVICE_ID
    }
    assert bot.request.await_args_list[1].args == (
        "PATCH",
        "/api/v1/bots/interactions/70/followups/82",
    )
    assert bot.request.await_args_list[1].kwargs["json"] == {
        "e2ee": {"protocol": "kaede-e2ee-v1"}
    }
    assert bot.request.await_args_list[2].args == (
        "DELETE",
        "/api/v1/bots/interactions/70/followups/82",
    )
    bot.request.reset_mock()
    with pytest.raises(ValueError, match="interaction lifecycle"):
        await message.reply("must not borrow a channel grant")
    with pytest.raises(ValueError, match="interaction lifecycle"):
        await message.add_reaction("👋")
    with pytest.raises(ValueError, match="interaction lifecycle"):
        await message.start_thread("unsafe")
    bot.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_wrapper_pins_guild_or_user_install_authority() -> None:
    guild_bot = client()
    guild_bot.request = AsyncMock(return_value=message_payload())  # type: ignore[method-assign]
    guild = Interaction.from_payload(
        guild_bot,
        TARGET,
        {
            "id": "70",
            "application_ref": "1@apps.example",
            "guild_ref": "10@chat.example",
            "channel_ref": "5@chat.example",
            "installation_id": "44",
            "integration_type": "guild_install",
            "user": {
                "id": "3",
                "origin_domain": "chat.example",
                "username": "member",
            },
        },
    )
    guild_response = await guild.respond("done")
    assert isinstance(guild_response, Message)
    assert guild_response.bot_installation_id == 44

    user_bot = client()
    user_bot.request = AsyncMock(return_value=message_payload())  # type: ignore[method-assign]
    user = Interaction.from_payload(
        user_bot,
        TARGET,
        {
            "id": "70",
            "application_ref": "1@apps.example",
            "channel_ref": "5@chat.example",
            "user_installation_id": "55",
            "integration_type": "user_install",
            "user": {
                "id": "3",
                "origin_domain": "chat.example",
                "username": "member",
            },
        },
    )
    user_response = await user.respond("done")
    assert isinstance(user_response, Message)
    with pytest.raises(ValueError, match="interaction lifecycle"):
        await user_response.reply("no generic GDM authority")


@pytest.mark.asyncio
async def test_webhook_messages_keep_private_token_only_in_known_producers() -> None:
    bot = client()
    thread = EntityRef(5, "chat.example")
    payload = {
        **message_payload(),
        "webhook_id": "22",
        "webhook_domain": "chat.example",
    }
    edited_payload = {**payload, "content": "edited"}
    bot.request = AsyncMock(side_effect=[payload, edited_payload, None])  # type: ignore[method-assign]

    message = await bot.execute_webhook(
        22,
        "secret-token",
        "created",
        target=TARGET,
        thread_id=thread,
    )
    assert isinstance(message, Message)
    edited = await message.edit(content="edited", components=[])
    await edited.delete()

    calls = bot.request.await_args_list
    assert calls[1].args == (
        "PATCH",
        "/api/v1/webhooks/22/secret-token/messages/9@chat.example",
    )
    assert calls[1].kwargs["params"] == {
        "with_components": True,
        "thread_id": str(thread),
    }
    assert calls[2].args == (
        "DELETE",
        "/api/v1/webhooks/22/secret-token/messages/9@chat.example",
    )
    assert calls[2].kwargs["params"] == {"thread_id": str(thread)}

    raw = Message.from_payload(bot, TARGET, payload)
    bot.request.reset_mock()
    bot.edit_message = AsyncMock(return_value=raw)  # type: ignore[method-assign]
    await raw.edit(content="generic")
    bot.edit_message.assert_awaited_once()
    assert "secret-token" not in repr(bot.edit_message.await_args)


@pytest.mark.asyncio
async def test_forum_webhook_response_retains_created_thread_for_lifecycle() -> None:
    bot = client()
    created = {
        **message_payload(),
        "channel_id": "6",
        "webhook_id": "22",
        "webhook_domain": "chat.example",
    }
    edited = {**created, "content": "edited"}
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[created, edited, None]
    )

    message = await bot.execute_webhook(
        22,
        "secret-token",
        "starter",
        target=TARGET,
        thread_name="Release notes",
    )
    assert isinstance(message, Message)
    updated = await message.edit(content="edited")
    await updated.delete()

    assert bot.request.await_args_list[1].kwargs["params"]["thread_id"] == (
        "6@chat.example"
    )
    assert bot.request.await_args_list[2].kwargs["params"]["thread_id"] == (
        "6@chat.example"
    )


@pytest.mark.asyncio
async def test_commands_only_start_skips_dm_capability_bootstrap() -> None:
    bot = client()
    bot.fetch_bot_identity = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(scopes=frozenset({"commands.read"}))
    )
    bot._bootstrap_dm_capabilities = AsyncMock()  # type: ignore[method-assign]
    bot._clear_dm_capability_state = AsyncMock()  # type: ignore[method-assign]
    bot.add_target = AsyncMock(return_value=TARGET)  # type: ignore[method-assign]
    bot.gateway = AsyncMock()  # type: ignore[method-assign]

    await bot.start(TARGET, auto_discover=False)

    bot._bootstrap_dm_capabilities.assert_not_awaited()
    bot._clear_dm_capability_state.assert_awaited_once_with()
    await bot.close()


@pytest.mark.asyncio
async def test_gateway_identify_binds_the_selected_e2ee_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client(device_id=DEVICE_ID)
    bot._targets[TARGET] = SimpleNamespace()  # type: ignore[assignment]  # noqa: SLF001
    bot._token = AsyncMock(return_value="runtime-token")  # type: ignore[method-assign]
    sent: list[dict[str, object]] = []
    heartbeat_tasks: list[asyncio.Task[None]] = []

    original_create_task = asyncio.create_task

    def capture_task(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = original_create_task(coroutine)
        heartbeat_tasks.append(task)
        return task

    monkeypatch.setattr(client_module.asyncio, "create_task", capture_task)

    class Socket:
        async def recv(self) -> str:
            return '{"op":10,"d":{"heartbeat_interval":60000}}'

        async def send(self, encoded: str) -> None:
            sent.append(client_module.json.loads(encoded))

        def __aiter__(self) -> Socket:
            return self

        async def __anext__(self) -> str:
            raise StopAsyncIteration

    class Connection:
        async def __aenter__(self) -> Socket:
            return Socket()

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        client_module, "connect", lambda *_args, **_kwargs: Connection()
    )

    await bot._gateway_once(TARGET)  # noqa: SLF001

    assert sent[0]["op"] == 2
    assert sent[0]["e2ee_device_id"] == DEVICE_ID
    assert len(heartbeat_tasks) == 1
    assert heartbeat_tasks[0].cancelled()


@pytest.mark.asyncio
async def test_gateway_4009_evicts_the_exact_regular_token_before_retry() -> None:
    bot = client()
    bot._tokens[(TARGET, None, None, False)] = SimpleNamespace()  # type: ignore[assignment]  # noqa: SLF001
    bot._tokens[(TARGET, None, None, True)] = SimpleNamespace()  # type: ignore[assignment]  # noqa: SLF001

    class AuthorizationChanged(Exception):
        code = 4009

    bot._gateway_once = AsyncMock(  # type: ignore[method-assign]
        side_effect=AuthorizationChanged("changed")
    )
    bot._reconcile_dm_capabilities_for_target = AsyncMock()  # type: ignore[method-assign]

    async def stop_after_dispatch(*_args: object, **_kwargs: object) -> None:
        bot._stopping = True  # noqa: SLF001

    bot.dispatch = AsyncMock(side_effect=stop_after_dispatch)  # type: ignore[method-assign]

    await bot.gateway(TARGET)

    assert (TARGET, None, None, False) not in bot._tokens  # noqa: SLF001
    assert (TARGET, None, None, True) in bot._tokens  # noqa: SLF001
    bot._reconcile_dm_capabilities_for_target.assert_awaited_once_with(TARGET)
