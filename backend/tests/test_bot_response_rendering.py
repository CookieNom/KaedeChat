from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from fastapi import HTTPException

from app.api import bots as bots_api
from app.api import threads as threads_api
from app.api.bots import render_bot_message_response
from app.api.threads import render_bot_thread_result


def principal(*, scopes: set[str]) -> SimpleNamespace:
    return SimpleNamespace(
        scopes=scopes,
        user=SimpleNamespace(id=10, origin_domain="apps.example"),
        application=SimpleNamespace(id=20, origin_domain="apps.example"),
        worker=SimpleNamespace(id=40),
    )


def installation(*, scopes: set[str]) -> SimpleNamespace:
    return SimpleNamespace(id=70, granted_scopes=sorted(scopes))


@pytest.mark.asyncio
async def test_message_response_uses_one_grant_and_own_content_exemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = principal(scopes={"messages.metadata"})
    grant = installation(scopes={"messages.metadata"})
    message = {
        "id": "90",
        "origin_domain": "guild.example",
        "author_id": "10",
        "author_domain": "apps.example",
        "content": "own response",
        "e2ee": None,
        "attachments": [{"id": "91"}],
    }
    monkeypatch.setattr(
        bots_api,
        "require_bot_channel_e2ee_access",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        bots_api,
        "bot_messages_after_history_floor",
        AsyncMock(return_value=[message]),
    )

    rendered = await render_bot_message_response(
        SimpleNamespace(),
        actor,
        SimpleNamespace(guild_id=1),
        grant,
        message,
    )

    assert rendered["content"] == "own response"
    assert rendered["attachments"] == []
    assert rendered["attachments_unavailable"] is True
    assert rendered["bot_installation_id"] == "70"


@pytest.mark.asyncio
async def test_foreign_message_response_redacts_content_and_history_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = principal(scopes={"messages.metadata", "messages.content"})
    grant = installation(scopes={"messages.metadata"})
    message = {
        "id": "90",
        "origin_domain": "guild.example",
        "author_id": "11",
        "author_domain": "guild.example",
        "content": "foreign response",
        "e2ee": None,
        "attachments": [],
    }
    monkeypatch.setattr(
        bots_api,
        "require_bot_channel_e2ee_access",
        AsyncMock(return_value=SimpleNamespace()),
    )
    floor = AsyncMock(return_value=[])
    monkeypatch.setattr(bots_api, "bot_messages_after_history_floor", floor)

    with pytest.raises(Exception) as hidden:
        await render_bot_message_response(
            SimpleNamespace(),
            actor,
            SimpleNamespace(guild_id=1),
            grant,
            message,
            e2ee_device_id="kbe_" + "d" * 43,
        )

    assert getattr(hidden.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_thread_response_binds_channel_and_starter_to_exact_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = principal(scopes={"messages.history", "attachments.read"})
    grant = installation(scopes={"messages.history"})
    thread = SimpleNamespace(id=30, origin_domain="guild.example", guild_id=1)
    session = SimpleNamespace(get=AsyncMock(return_value=thread))
    starter = {
        "id": "31",
        "origin_domain": "guild.example",
        "author_id": "10",
        "author_domain": "apps.example",
        "content": "own encrypted starter",
        "e2ee": {"ciphertext": "opaque"},
        "attachments": [{"id": "32"}],
    }
    rendered = {
        "id": "30",
        "origin_domain": "guild.example",
        "encryption_mode": "e2ee",
        "e2ee_required": True,
        "starter_message": starter,
    }
    participation = SimpleNamespace()
    require_access = AsyncMock(return_value=participation)
    floor = AsyncMock(return_value=[starter])
    monkeypatch.setattr(
        threads_api,
        "optional_bot_channel_e2ee_access",
        require_access,
    )
    monkeypatch.setattr(threads_api, "bot_messages_after_history_floor", floor)

    result = await render_bot_thread_result(
        session,
        rendered,
        actor,
        grant,
        e2ee_device_id="kbe_" + "d" * 43,
    )

    assert result["bot_installation_id"] == "70"
    assert result["starter_message"]["bot_installation_id"] == "70"
    assert result["starter_message"]["e2ee"] == {"ciphertext": "opaque"}
    assert result["starter_message"]["content"] == "own encrypted starter"
    assert result["starter_message"]["attachments"] == []
    require_access.assert_awaited_once()
    floor.assert_awaited_once_with(session, participation, [starter])


@pytest.mark.asyncio
async def test_thread_response_fails_closed_without_exact_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = principal(scopes={"messages.history", "messages.content"})
    grant = installation(scopes={"messages.history", "messages.content"})
    session = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                id=30,
                origin_domain="guild.example",
                guild_id=1,
            )
        )
    )
    rendered = {
        "id": "30",
        "origin_domain": "guild.example",
        "encryption_mode": "e2ee",
        "e2ee_required": True,
        "starter_message": {
            "id": "31",
            "origin_domain": "guild.example",
            "content": None,
            "e2ee": {"ciphertext": "opaque"},
            "attachments": [],
        },
    }
    require_access = AsyncMock()
    monkeypatch.setattr(
        threads_api,
        "optional_bot_channel_e2ee_access",
        require_access,
    )

    result = await render_bot_thread_result(session, rendered, actor, grant)

    assert result["starter_message"]["e2ee"] is None
    assert result["starter_message"]["content_unavailable"] is True
    require_access.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_redacts_room_not_joined_by_selected_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = AsyncMock(
        side_effect=HTTPException(
            status_code=409,
            detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"},
        )
    )
    monkeypatch.setattr(bots_api, "require_bot_channel_e2ee_access", denied)

    participation = await bots_api.optional_bot_channel_e2ee_access(
        SimpleNamespace(),
        SimpleNamespace(encryption_mode="e2ee", e2ee_required=True),
        installation(scopes={"channels.read"}),
        "kbe_" + "x" * 43,
        worker_id=40,
    )

    assert participation is None


@pytest.mark.asyncio
async def test_attachment_access_requires_current_exact_e2ee_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = principal(scopes={"attachments.read"})
    grant = installation(scopes={"attachments.read"})
    channel = SimpleNamespace(
        id=30,
        origin_domain="guild.example",
        encryption_mode="e2ee",
        e2ee_required=True,
    )
    message = SimpleNamespace(id=31, origin_domain="guild.example")
    attachment = SimpleNamespace(encryption_mode="e2ee")
    participation = SimpleNamespace()
    require_access = AsyncMock(return_value=participation)
    floor = AsyncMock(return_value=[{"id": "31", "origin_domain": "guild.example"}])
    monkeypatch.setattr(
        bots_api,
        "installation_for_channel",
        AsyncMock(return_value=(channel, grant)),
    )
    monkeypatch.setattr(bots_api, "require_bot_channel_e2ee_access", require_access)
    monkeypatch.setattr(bots_api, "bot_messages_after_history_floor", floor)

    await bots_api.require_bot_attachment_e2ee_access(
        SimpleNamespace(),
        SimpleNamespace(),
        actor,
        attachment,
        grant,
        "kbe_" + "d" * 43,
        message=message,
        channel=channel,
    )

    require_access.assert_awaited_once_with(
        ANY,
        channel,
        grant,
        "kbe_" + "d" * 43,
        worker_id=actor.worker.id,
    )
    floor.assert_awaited_once()


@pytest.mark.asyncio
async def test_attachment_access_rejects_revoked_e2ee_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = principal(scopes={"attachments.read"})
    grant = installation(scopes={"attachments.read"})
    channel = SimpleNamespace(
        id=30,
        origin_domain="guild.example",
        encryption_mode="e2ee",
        e2ee_required=True,
    )
    monkeypatch.setattr(
        bots_api,
        "installation_for_channel",
        AsyncMock(return_value=(channel, grant)),
    )
    monkeypatch.setattr(
        bots_api,
        "require_bot_channel_e2ee_access",
        AsyncMock(
            side_effect=HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"},
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await bots_api.require_bot_attachment_e2ee_access(
            SimpleNamespace(),
            SimpleNamespace(),
            actor,
            SimpleNamespace(encryption_mode="e2ee"),
            grant,
            "kbe_" + "r" * 43,
            message=SimpleNamespace(id=31, origin_domain="guild.example"),
            channel=channel,
        )

    assert exc.value.detail == {"code": "BOT_E2EE_PARTICIPANT_REQUIRED"}


@pytest.mark.asyncio
async def test_attachment_access_hides_messages_before_history_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = principal(scopes={"attachments.read"})
    grant = installation(scopes={"attachments.read"})
    channel = SimpleNamespace(
        id=30,
        origin_domain="guild.example",
        encryption_mode="e2ee",
        e2ee_required=True,
    )
    monkeypatch.setattr(
        bots_api,
        "installation_for_channel",
        AsyncMock(return_value=(channel, grant)),
    )
    monkeypatch.setattr(
        bots_api,
        "require_bot_channel_e2ee_access",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        bots_api,
        "bot_messages_after_history_floor",
        AsyncMock(return_value=[]),
    )

    with pytest.raises(HTTPException) as exc:
        await bots_api.require_bot_attachment_e2ee_access(
            SimpleNamespace(),
            SimpleNamespace(),
            actor,
            SimpleNamespace(encryption_mode="e2ee"),
            grant,
            "kbe_" + "d" * 43,
            message=SimpleNamespace(id=31, origin_domain="guild.example"),
            channel=channel,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == {"code": "MEDIA_NOT_FOUND"}
