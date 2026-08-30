from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import kaede_bot.client as client_module
from kaede_bot import (
    Attachment,
    Client,
    EntityRef,
    Interaction,
    Poll,
    PollAnswer,
    PollMedia,
    TextDisplay,
    View,
    WorkerState,
)
from kaede_bot.errors import ApiError

TARGET = "https://chat.example"


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def interaction(bot: Client) -> Interaction:
    return Interaction.from_payload(
        bot,
        TARGET,
        {
            "id": "90",
            "application_ref": "1@apps.example",
            "guild_ref": "10@chat.example",
            "channel_ref": "7@chat.example",
            "user": {
                "id": "3",
                "origin_domain": "chat.example",
                "username": "member",
            },
            "command": {"name": "report"},
            "options": {},
        },
    )


def upload_ticket(
    *,
    url: str = "https://media.chat.example/staged/41",
    encryption_mode: str = "plaintext",
    encryption_protocol: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "41",
        "origin_domain": "chat.example",
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size": 4,
        "scan_status": "pending",
        "encryption_mode": encryption_mode,
        "encryption_protocol": encryption_protocol,
        "purpose": "attachment",
        "upload_url": url,
        "media_origin": "https://media.chat.example",
        "upload_method": "PUT",
        "expires_at": "2026-08-27T12:15:00+00:00",
    }


@pytest.mark.asyncio
async def test_defer_and_deferred_original_poll_preserve_discord_lifecycle_shape() -> (
    None
):
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            None,
            {"id": "30", "ephemeral": True},
            {
                "id": "30",
                "ephemeral": True,
                "poll": {"results": {"is_finalized": True}},
            },
            {
                "id": "31",
                "ephemeral": True,
                "poll": {"results": {"is_finalized": True}},
            },
        ]
    )
    current = interaction(bot)
    poll = Poll(
        question=PollMedia(text="Ship it?"),
        answers=[
            PollAnswer(PollMedia(text="Yes")),
            PollAnswer(PollMedia(text="Wait")),
        ],
        duration=24,
    )

    await current.defer(ephemeral=True)
    await current.edit_original_response(poll=poll)
    ended = await current.end_original_poll()
    ended_followup = await current.end_followup_poll(31)

    assert ended["poll"]["results"]["is_finalized"] is True
    assert ended_followup["id"] == "31"
    calls = bot.request.await_args_list
    assert calls[0].args == ("POST", "/api/v1/bots/interactions/90/defer")
    assert calls[0].kwargs["json"] == {"ephemeral": True}
    assert calls[1].args == (
        "PATCH",
        "/api/v1/bots/interactions/90/responses/@original",
    )
    assert calls[1].kwargs["json"]["poll"] == poll.to_dict()
    assert calls[2].args == (
        "POST",
        "/api/v1/bots/interactions/90/responses/@original/polls/expire",
    )
    assert calls[3].args == (
        "POST",
        "/api/v1/bots/interactions/90/responses/31/polls/expire",
    )


@pytest.mark.asyncio
async def test_interaction_upload_stages_bytes_without_forwarding_bot_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client()
    bot.request = AsyncMock(return_value=upload_ticket())  # type: ignore[method-assign]
    uploaded: dict[str, Any] = {}

    class UploadResponse:
        is_redirect = False

        def raise_for_status(self) -> None:
            return None

    class UploadClient:
        def __init__(self, **kwargs: Any) -> None:
            uploaded["client_kwargs"] = kwargs

        async def __aenter__(self) -> UploadClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def put(self, url: str, **kwargs: Any) -> UploadResponse:
            uploaded["url"] = url
            uploaded["put_kwargs"] = kwargs
            return UploadResponse()

    monkeypatch.setattr(client_module.httpx, "AsyncClient", UploadClient)

    ticket = await interaction(bot).upload_attachment(
        b"data",
        filename="report.pdf",
        content_type="application/pdf",
    )

    assert isinstance(ticket, Attachment)
    assert ticket.ref == EntityRef(41, "chat.example")
    request = bot.request.await_args
    assert request is not None
    assert request.args == ("POST", "/api/v1/bots/interactions/90/attachments")
    assert request.kwargs == {
        "target": TARGET,
        "json": {
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "size": 4,
            "encryption_mode": "plaintext",
            "encryption_protocol": None,
        },
        "headers": {},
    }
    assert uploaded == {
        "client_kwargs": {
            "timeout": 60,
            "follow_redirects": False,
            "trust_env": False,
        },
        "url": "https://media.chat.example/staged/41",
        "put_kwargs": {
            "content": b"data",
            "headers": {
                "Content-Type": "application/pdf",
                "Content-Length": "4",
            },
        },
    }


@pytest.mark.asyncio
async def test_interaction_upload_rejects_unsafe_ticket_before_sending_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=upload_ticket(url="http://storage.example/staged/41")
    )

    def unexpected_client(**_: Any) -> object:
        raise AssertionError("an HTTP client must not be opened for an unsafe URL")

    monkeypatch.setattr(client_module.httpx, "AsyncClient", unexpected_client)

    with pytest.raises(ApiError, match="safe HTTPS"):
        await bot.upload_interaction_attachment(
            90,
            b"data",
            filename="report.pdf",
            content_type="application/pdf",
            target=TARGET,
        )


@pytest.mark.asyncio
async def test_encrypted_interaction_upload_binds_selected_device_and_protocol() -> (
    None
):
    device_id = "kbe_" + "d" * 43
    bot = Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        ),
        e2ee_device_id=device_id,
    )
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=upload_ticket(
            encryption_mode="e2ee",
            encryption_protocol="kaede-file-v1",
        )
    )
    bot._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]

    ticket = await bot.upload_interaction_attachment(
        90,
        b"ciphertext",
        filename="encrypted-file",
        content_type="application/octet-stream",
        target=TARGET,
        encryption_mode="e2ee",
    )

    assert ticket.encryption_mode == "e2ee"
    request = bot.request.await_args
    assert request is not None
    assert request.kwargs["headers"] == {"X-Kaede-E2EE-Device": device_id}
    assert request.kwargs["json"]["encryption_protocol"] == "kaede-file-v1"
    bot._put_upload_ticket.assert_awaited_once_with(
        ticket,
        b"ciphertext",
        content_type="application/octet-stream",
    )

    with pytest.raises(ValueError, match="rich fields"):
        await interaction(bot).send_followup(
            "plaintext",
            e2ee={"protocol": "mls10"},
            attachment_ids=[ticket.ref.id],
        )


@pytest.mark.asyncio
async def test_initial_response_includes_staged_attachment_ids() -> None:
    bot = client()
    bot.interaction_callback = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": "42", "ephemeral": True}
    )
    current = interaction(bot)

    await current.respond("Ready", attachment_ids=[41, 42], ephemeral=True)

    callback = bot.interaction_callback.await_args
    assert callback is not None
    assert callback.args == (
        90,
        4,
        {
            "content": "Ready",
            "embeds": [],
            "attachment_ids": ["41", "42"],
            "flags": 64,
        },
    )
    assert callback.kwargs == {"target": TARGET}

    await current.respond(
        e2ee={"protocol": "kaede-e2ee-v1"},
        attachment_ids=[41],
    )
    assert bot.interaction_callback.await_args.args[2] == {
        "content": None,
        "e2ee": {"protocol": "kaede-e2ee-v1"},
        "attachment_ids": ["41"],
    }
    assert bot.interaction_callback.await_count == 2


@pytest.mark.asyncio
async def test_interaction_voice_and_message_options_cover_callback_and_followup() -> (
    None
):
    bot = client()
    bot.interaction_callback = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": "42", "ephemeral": False}
    )
    bot.request = AsyncMock(return_value={"id": "43", "ephemeral": True})  # type: ignore[method-assign]
    current = interaction(bot)
    mentions = {"parse": [], "users": ["7@users.example"]}

    await current.respond(
        attachment_ids=[41],
        voice_message=True,
        allowed_mentions=mentions,
    )
    callback_body = bot.interaction_callback.await_args.args[2]
    assert callback_body["voice_message"] is True
    assert callback_body["flags"] == 1 << 13
    assert callback_body["allowed_mentions"] == mentions

    await current.send_followup(
        "hello",
        tts=True,
        allowed_mentions=mentions,
        flags=1 << 12,
    )
    followup = bot.request.await_args.kwargs["json"]["message"]
    assert followup["tts"] is True
    assert followup["flags"] == 1 << 12
    assert followup["allowed_mentions"] == mentions

    await current.respond(attachment_ids=[41], flags=1 << 13)
    assert bot.interaction_callback.await_args.args[2]["voice_message"] is True
    with pytest.raises(ValueError, match="voice response"):
        await current.respond("not voice", attachment_ids=[41], flags=1 << 13)


@pytest.mark.asyncio
async def test_interaction_voice_upload_normalizes_duration_and_waveform() -> None:
    bot = client()
    ticket_payload = upload_ticket()
    ticket_payload.update(filename="voice.ogg", content_type="audio/ogg")
    bot.request = AsyncMock(return_value=ticket_payload)  # type: ignore[method-assign]
    bot._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]

    await interaction(bot).upload_attachment(
        b"opus",
        filename="voice.ogg",
        content_type="audio/ogg",
        duration_secs=1.25,
        waveform=b"\x00\x7f\xff",
    )

    assert bot.request.await_args.kwargs["json"] == {
        "filename": "voice.ogg",
        "content_type": "audio/ogg",
        "size": 4,
        "encryption_mode": "plaintext",
        "encryption_protocol": None,
        "duration_secs": 1.25,
        "waveform": "AH//",
    }
    with pytest.raises(ValueError, match="both duration_secs and waveform"):
        await interaction(bot).upload_attachment(
            b"opus",
            filename="voice.ogg",
            content_type="audio/ogg",
            duration_secs=1,
        )


@pytest.mark.asyncio
async def test_components_v2_initial_response_uses_immediate_type_four_callback() -> (
    None
):
    bot = client()
    bot.interaction_callback = AsyncMock(return_value={"id": "42"})  # type: ignore[method-assign]
    current = interaction(bot)
    components = [{"type": 10, "content": "V2 text"}]

    await current.respond(components=components)

    assert bot.interaction_callback.await_args.args == (
        90,
        4,
        {
            "content": None,
            "embeds": [],
            "attachment_ids": [],
            "flags": 1 << 15,
            "components": components,
        },
    )

    view = View(rows=[TextDisplay("View V2")])
    await current.respond(view=view)
    assert bot.interaction_callback.await_args.args[0:2] == (90, 4)
    view_payload = bot.interaction_callback.await_args.args[2]
    assert view_payload["components"] == [{"type": 10, "content": "View V2"}]
    assert view_payload["flags"] == 1 << 15


@pytest.mark.asyncio
async def test_components_v2_and_allowed_mentions_cover_followup_edits() -> None:
    bot = client()
    bot.request = AsyncMock(return_value={"id": "72", "ephemeral": False})  # type: ignore[method-assign]
    current = interaction(bot)
    mentions = {"parse": [], "users": ["7@users.example"]}

    await current.send_followup(view=View(rows=[TextDisplay("Follow-up V2")]))
    followup = bot.request.await_args.kwargs["json"]["message"]
    assert followup["flags"] == 1 << 15
    assert followup["components"] == [{"type": 10, "content": "Follow-up V2"}]

    await current.edit_followup(
        72,
        components=[{"type": 10, "content": "Edited V2"}],
        allowed_mentions=mentions,
        view_version=2,
    )
    body = bot.request.await_args.kwargs["json"]
    assert body["flags"] == 1 << 15
    assert body["allowed_mentions"] == mentions
    assert body["view_version"] == 2


@pytest.mark.asyncio
async def test_interaction_attachment_ids_cover_edits_and_followup_lifecycle() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": "72", "ephemeral": True, "attachments": []}
    )
    current = interaction(bot)

    await current.edit_original_response(attachment_ids=[41, 42])
    await current.send_followup(
        "More",
        ephemeral=True,
        attachment_ids=[43],
    )
    await current.edit_followup(72, attachment_ids=[])
    await current.fetch_followup(72)
    await current.delete_followup(72)
    await current.delete_original_response()

    calls = bot.request.await_args_list
    assert calls[0].args == (
        "PATCH",
        "/api/v1/bots/interactions/90/responses/@original",
    )
    assert calls[0].kwargs["json"] == {"attachment_ids": ["41", "42"]}
    assert calls[1].args == (
        "POST",
        "/api/v1/bots/interactions/90/followups",
    )
    assert calls[1].kwargs["json"] == {
        "message": {
            "content": "More",
            "embeds": [],
            "attachment_ids": ["43"],
        },
        "ephemeral": True,
    }
    assert calls[2].args == (
        "PATCH",
        "/api/v1/bots/interactions/90/followups/72",
    )
    assert calls[2].kwargs["json"] == {"attachment_ids": []}
    assert calls[3].args == (
        "GET",
        "/api/v1/bots/interactions/90/followups/72",
    )
    assert calls[4].args == (
        "DELETE",
        "/api/v1/bots/interactions/90/followups/72",
    )
    assert calls[5].args == (
        "DELETE",
        "/api/v1/bots/interactions/90/responses/@original",
    )
    assert all(call.kwargs["target"] == TARGET for call in calls)


@pytest.mark.asyncio
async def test_interaction_exposes_and_reads_authority_resolved_input_attachments() -> (
    None
):
    bot = client()
    payload = upload_ticket()
    payload.pop("upload_url")
    current = Interaction.from_payload(
        bot,
        TARGET,
        {
            "id": "90",
            "application_ref": "1@apps.example",
            "channel_ref": "7@chat.example",
            "user": {
                "id": "3",
                "origin_domain": "chat.example",
                "username": "member",
            },
            "options": {"document": "41"},
            "resolved": {"attachments": {"41": payload}},
        },
    )
    assert [item.ref for item in current.input_attachments] == [
        EntityRef(41, "chat.example")
    ]

    bot.request = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    fetched = await current.fetch_input_attachment(EntityRef(41, "chat.example"))
    assert fetched.filename == "report.pdf"
    request = bot.request.await_args
    assert request is not None
    assert request.args == (
        "GET",
        "/api/v1/bots/interactions/90/attachments/41@chat.example",
    )

    bot._download_attachment_path = AsyncMock(return_value=b"data")  # type: ignore[method-assign]
    assert (
        await current.read_input_attachment(
            EntityRef(41, "chat.example"), max_bytes=1024
        )
        == b"data"
    )
    download = bot._download_attachment_path.await_args
    assert download is not None
    assert download.args == (
        "/api/v1/bots/interactions/90/attachments/41@chat.example/original",
    )
    assert download.kwargs == {"target": TARGET, "max_bytes": 1024, "headers": {}}
