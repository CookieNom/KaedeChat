from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.responses import Response

import app.api.bot_e2ee as bot_e2ee_api
import app.api.interactions as interactions
from app.api.bot_gateway import encrypted_bot_content_event
from app.bots import e2ee as bot_e2ee_service
from app.chat.e2ee import (
    interaction_routing_contract_digest,
    validate_e2ee_envelope,
    validate_e2ee_message_projection,
    validate_interaction_routing_contract,
)
from app.core.types import EntityRef
from app.db.bot_models import BotE2EEDevice, BotE2EEParticipation, BotInstallation, BotWorker
from app.db.models import Channel
from app.federation import events as federation_events
from app.media.schemas import UploadTicketRequest


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def mls_envelope(*, bot: bool = False, epoch: int = 7) -> dict[str, object]:
    return {
        "version": 2,
        "protocol": "mls10",
        "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
        "group_id": b64url(b"room-group"),
        "policy_generation": "3",
        "epoch": str(epoch),
        "sender_device_id": ("kbe_" if bot else "ked_") + b64url(b"d" * 32),
        "operation": "create",
        "ciphertext": b64url(b"opaque interaction body"),
    }


def test_backend_matches_shared_routing_contract_vectors() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "frontend/static/protocol/interaction-routing-contract-v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    for vector in fixture["vectors"]:
        contract = validate_interaction_routing_contract(
            vector["contract"],
            callback_type=vector["callback_type"],
        )
        assert contract == vector["contract"], vector["name"]
        assert interaction_routing_contract_digest(contract) == vector["digest"]
    for vector in fixture["invalid_contracts"]:
        with pytest.raises(ValueError):
            validate_interaction_routing_contract(
                vector["contract"],
                callback_type=vector["callback_type"],
            )


def test_response_envelope_binds_exact_routing_contract_and_cannot_be_a_message() -> None:
    contract = {
        "version": 1,
        "kind": "message",
        "view_timeout_seconds": 900,
        "components": [
            {
                "type": 2,
                "custom_id": "approve",
                "disabled": False,
            }
        ],
    }
    envelope = mls_envelope(bot=True) | {
        "interaction_ref": "90@guild.example",
        "response_ref": "91@guild.example",
        "sequence": "0",
        "revision": "1",
        "callback_type": 4,
        "attachment_refs": [],
        "interaction_contract": contract,
        "interaction_contract_digest": interaction_routing_contract_digest(contract),
    }

    normalized = validate_e2ee_envelope(envelope)
    interactions.require_interaction_response_e2ee_binding(
        cast(Any, SimpleNamespace(id=90, channel_domain="guild.example")),
        normalized,
        response_id=91,
        sequence=0,
        revision=1,
        callback_type=4,
        attachment_ids=[],
    )
    with pytest.raises(ValueError, match="interaction response"):
        validate_e2ee_message_projection(
            normalized,
            message_id=91,
            message_domain="guild.example",
            edited=False,
        )

    tampered = dict(envelope) | {"interaction_contract_digest": b64url(b"x" * 32)}
    with pytest.raises(ValueError, match="routing contract digest"):
        validate_e2ee_envelope(tampered)


def encrypted_channel() -> SimpleNamespace:
    return SimpleNamespace(
        id=20,
        origin_domain="guild.example",
        type=11,
        encryption_mode="e2ee",
        encryption_state="active",
        encryption_policy_generation=3,
        encryption_epoch=7,
        encryption_group_id=b64url(b"room-group"),
    )


def installation() -> BotInstallation:
    return BotInstallation(
        id=30,
        application_id=40,
        application_domain="app.example",
        guild_id=50,
        guild_domain="guild.example",
        bot_user_id=60,
        bot_user_domain="app.example",
        installer_id=70,
        installer_domain="guild.example",
        granted_scopes=["interactions.respond", "attachments.read", "attachments.write"],
        granted_intents=["interactions"],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="participant",
        status="active",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_field", "owner_id", "guild_ref"),
    [
        ("installation_id", 30, (50, "guild.example")),
        ("dm_grant_id", 31, (None, None)),
    ],
)
async def test_reconciled_bot_participation_materializes_exact_consent_lineage(
    owner_field: str,
    owner_id: int,
    guild_ref: tuple[int | None, str | None],
) -> None:
    added: list[BotE2EEParticipation] = []
    session = SimpleNamespace(add=added.append)
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=90))
    channel = SimpleNamespace(
        id=20,
        origin_domain="channel.example",
        guild_id=guild_ref[0],
        guild_domain=guild_ref[1],
        last_message_id=19,
        last_message_domain="channel.example",
    )
    device = SimpleNamespace(
        id=80,
        application_id=40,
        application_domain="app.example",
    )
    actor = SimpleNamespace(id=70, origin_domain="user.example")

    await bot_e2ee_api._reconcile_bot_e2ee_participations(
        cast(Any, session),
        cast(Any, snowflake),
        existing=[],
        devices=[cast(Any, device)],
        channel=cast(Any, channel),
        actor=cast(Any, actor),
        owner_field=cast(Any, owner_field),
        owner_id=owner_id,
    )

    assert len(added) == 1
    participation = added[0]
    assert getattr(participation, owner_field) == owner_id
    assert (participation.application_id, participation.application_domain) == (
        device.application_id,
        device.application_domain,
    )
    assert (participation.guild_id, participation.guild_domain) == guild_ref
    assert (participation.channel_id, participation.channel_domain) == (
        channel.id,
        channel.origin_domain,
    )


@pytest.mark.asyncio
async def test_grant_participation_materializes_channel_before_commit_and_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_time = datetime(2026, 8, 29, 12, tzinfo=UTC)
    refreshed_at = old_time + timedelta(minutes=1)
    channel = SimpleNamespace(
        id=20,
        origin_domain="guild.example",
        encryption_mode="e2ee",
        encryption_state="active",
        updated_at=old_time,
    )
    guild = SimpleNamespace(id=50, origin_domain="guild.example")
    application = SimpleNamespace(id=40, origin_domain="app.example")
    bot = SimpleNamespace(id=60, origin_domain="app.example")
    app_installation = SimpleNamespace(id=30)
    actor = SimpleNamespace(id=70, origin_domain="guild.example")
    lifecycle: list[str] = []
    committed = False
    refreshed = False

    async def sync_policy(*_args: object) -> None:
        channel.encryption_state = "rekeying"
        channel.updated_at = None
        lifecycle.append("sync")

    async def refresh(value: object, *, attribute_names: tuple[str, ...]) -> None:
        nonlocal refreshed
        assert value is channel
        assert attribute_names == ("updated_at",)
        channel.updated_at = refreshed_at
        refreshed = True
        lifecycle.append("refresh")

    async def commit() -> None:
        nonlocal committed
        committed = True
        lifecycle.append("commit")

    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[]),
        flush=AsyncMock(side_effect=lambda: lifecycle.append("flush")),
        refresh=AsyncMock(side_effect=refresh),
        commit=AsyncMock(side_effect=commit),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_authorize_bot_e2ee_management",
        AsyncMock(return_value=channel),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_bot_installation_for_e2ee",
        AsyncMock(return_value=(application, bot, app_installation)),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_device_snapshot_for_installation",
        AsyncMock(return_value=(SimpleNamespace(), [])),
    )
    monkeypatch.setattr(bot_e2ee_api, "_reconcile_bot_e2ee_participations", AsyncMock())
    monkeypatch.setattr(
        bot_e2ee_api,
        "_sync_bot_participation_policy",
        AsyncMock(side_effect=sync_policy),
    )
    monkeypatch.setattr(bot_e2ee_api, "add_audit_entry", AsyncMock())
    monkeypatch.setattr(bot_e2ee_api, "publish_committed_dispatches", AsyncMock())
    monkeypatch.setattr(bot_e2ee_api, "wake_queued_guild_federation", AsyncMock())
    dispatch = AsyncMock()
    monkeypatch.setattr(bot_e2ee_api, "publish_dispatch", dispatch)
    monkeypatch.setattr(
        bot_e2ee_api,
        "_list_bot_e2ee_participation",
        AsyncMock(return_value={"devices": []}),
    )

    def render_channel(value: object) -> dict[str, object]:
        assert value is channel
        assert refreshed
        assert not committed
        lifecycle.append("render")
        return {"version": channel.updated_at.isoformat()}

    monkeypatch.setattr(bot_e2ee_api, "channel_payload", render_channel)

    rendered = await bot_e2ee_api._grant_bot_e2ee_participation(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
        cast(Any, guild),
        EntityRef("20@guild.example"),
        EntityRef("40@app.example"),
        cast(Any, actor),
        None,
    )

    assert rendered == {"devices": []}
    assert lifecycle == ["sync", "flush", "refresh", "render", "commit"]
    assert dispatch.await_args.args[3] == {"version": refreshed_at.isoformat()}


def plaintext_interaction() -> SimpleNamespace:
    return SimpleNamespace(
        id=90,
        channel_id=20,
        channel_domain="guild.example",
        encrypted_payload=None,
    )


def bot_principal() -> SimpleNamespace:
    return SimpleNamespace(worker=SimpleNamespace(id=80))


def encrypted_channel_session() -> SimpleNamespace:
    return SimpleNamespace(get=AsyncMock(return_value=encrypted_channel()))


def test_encrypted_interaction_keeps_plaintext_options_outside_authority() -> None:
    payload = interactions.InteractionCreate(
        application_ref="40@app.example",
        command_name="upload",
        encrypted_payload=mls_envelope(),
        attachment_ids=[101, 102],
    )
    modal = interactions.InteractionCreate(
        application_ref="40@app.example",
        interaction_type="modal_submit",
        custom_id="encrypted-form",
        response_id=9,
        encrypted_payload=mls_envelope(),
        attachment_ids=[103],
    )

    assert payload.options == {}
    assert payload.attachment_ids == [101, 102]
    assert modal.components == []
    javascript_snowflake = "9007199254740993"
    string_ids = interactions.InteractionCreate(
        application_ref="40@app.example",
        command_name="upload",
        encrypted_payload=mls_envelope(),
        attachment_ids=[javascript_snowflake],
    )
    assert string_ids.attachment_ids == [int(javascript_snowflake)]
    with pytest.raises(ValidationError):
        interactions.InteractionCreate(
            application_ref="40@app.example",
            command_name="upload",
            attachment_ids=[101],
        )
    with pytest.raises(ValidationError):
        interactions.InteractionCreate(
            application_ref="40@app.example",
            command_name="upload",
            encrypted_payload=mls_envelope(),
            options={"file": "101"},
        )


@pytest.mark.asyncio
async def test_plaintext_ephemeral_callback_rechecks_current_e2ee_policy() -> None:
    context = interactions.InteractionCallbackContext(
        interaction=cast(Any, plaintext_interaction()),
        request=interactions.InteractionCallback(type=4, data={"content": "plaintext"}),
        response=cast(Any, Response()),
        principal=cast(Any, bot_principal()),
        installation=installation(),
        session=cast(Any, encrypted_channel_session()),
        redis=cast(Any, SimpleNamespace()),
        snowflake=cast(Any, SimpleNamespace()),
        settings=cast(Any, SimpleNamespace()),
    )
    state = interactions.InteractionCallbackState(stored_payload={}, ephemeral=True)

    with pytest.raises(HTTPException) as exc:
        await interactions.create_callback_message(context, state, flags=64)

    assert exc.value.status_code == 409
    assert exc.value.detail == {"code": "E2EE_ENVELOPE_REQUIRED"}


@pytest.mark.asyncio
async def test_plaintext_ephemeral_followup_rechecks_current_e2ee_policy() -> None:
    context = interactions.InteractionFollowupContext(
        interaction=cast(Any, plaintext_interaction()),
        installation=installation(),
        request=interactions.InteractionFollowup(
            message=interactions.InteractionMessageCreate(content="plaintext"),
            ephemeral=True,
        ),
        response=Response(),
        principal=cast(Any, bot_principal()),
        session=cast(Any, encrypted_channel_session()),
        redis=cast(Any, SimpleNamespace()),
        snowflake=cast(Any, SimpleNamespace()),
        settings=cast(Any, SimpleNamespace()),
    )

    with pytest.raises(HTTPException) as exc:
        await interactions.prepare_interaction_followup(context)

    assert exc.value.status_code == 409
    assert exc.value.detail == {"code": "E2EE_ENVELOPE_REQUIRED"}


@pytest.mark.asyncio
async def test_plaintext_original_edit_rechecks_current_e2ee_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = plaintext_interaction()
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, installation())),
    )
    monkeypatch.setattr(
        interactions,
        "stored_interaction_response",
        AsyncMock(return_value=SimpleNamespace()),
    )

    with pytest.raises(HTTPException) as exc:
        await interactions.edit_original_interaction_response(
            interaction.id,
            interactions.InteractionResponseEdit(content="plaintext"),
            cast(Any, bot_principal()),
            cast(Any, encrypted_channel_session()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {"code": "E2EE_ENVELOPE_REQUIRED"}


@pytest.mark.asyncio
async def test_current_mls_interaction_response_requires_exact_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_participation = AsyncMock()
    monkeypatch.setattr(
        interactions,
        "require_bot_e2ee_participation",
        require_participation,
    )
    session = encrypted_channel_session()
    principal = bot_principal()
    app_installation = installation()
    envelope = mls_envelope(bot=True)

    await interactions.require_interaction_response_encryption(
        cast(Any, session),
        cast(Any, principal),
        cast(Any, plaintext_interaction()),
        app_installation,
        content=None,
        e2ee=envelope,
        attachment_count=0,
    )

    require_participation.assert_awaited_once_with(
        session,
        app_installation,
        session.get.return_value,
        envelope["sender_device_id"],
        worker_id=principal.worker.id,
    )


@pytest.mark.asyncio
async def test_response_attachment_uses_current_room_policy_after_e2ee_switch() -> None:
    app_installation = installation()
    principal = SimpleNamespace(user=SimpleNamespace(id=60, origin_domain="app.example"))
    attachment = SimpleNamespace(
        id=101,
        origin_domain="guild.example",
        bot_installation_id=app_installation.id,
        bot_user_installation_id=None,
        bot_dm_capability_id=None,
        uploader_id=principal.user.id,
        uploader_domain=principal.user.origin_domain,
        encryption_mode="e2ee",
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=encrypted_channel()),
        scalars=AsyncMock(return_value=[attachment]),
    )

    await interactions.require_owned_interaction_response_attachments(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guild.example")),
        cast(Any, principal),
        app_installation,
        cast(Any, plaintext_interaction()),
        [101],
    )

    session.get.assert_awaited_once_with(Channel, (20, "guild.example"))


@pytest.mark.asyncio
async def test_interaction_response_read_binds_selected_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_installation = installation()
    principal = bot_principal()
    participation = SimpleNamespace(
        history_floor_message_id=None,
        history_floor_message_domain=None,
    )
    require_participation = AsyncMock(
        return_value=(participation, SimpleNamespace(protocol_id="kbe_" + "d" * 43))
    )
    monkeypatch.setattr(
        interactions,
        "require_bot_e2ee_participation",
        require_participation,
    )
    session = encrypted_channel_session()
    stored = SimpleNamespace(
        message_id=None,
        message_domain=None,
        created_at=datetime.now(UTC),
    )

    await interactions.require_interaction_response_read_encryption(
        cast(Any, session),
        cast(Any, principal),
        cast(Any, plaintext_interaction()),
        app_installation,
        cast(Any, stored),
        "kbe_" + "d" * 43,
    )

    require_participation.assert_awaited_once_with(
        session,
        app_installation,
        session.get.return_value,
        "kbe_" + "d" * 43,
        worker_id=principal.worker.id,
    )


@pytest.mark.asyncio
async def test_interaction_response_read_hides_pre_floor_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    participation = SimpleNamespace(
        history_floor_message_id=200,
        history_floor_message_domain="guild.example",
    )
    monkeypatch.setattr(
        interactions,
        "require_bot_e2ee_participation",
        AsyncMock(return_value=(participation, SimpleNamespace())),
    )
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                encrypted_channel(),
                SimpleNamespace(
                    id=200,
                    origin_domain="guild.example",
                    created_at=now,
                ),
            ]
        )
    )
    stored = SimpleNamespace(
        message_id=None,
        message_domain=None,
        created_at=now - timedelta(seconds=1),
    )

    with pytest.raises(HTTPException) as exc:
        await interactions.require_interaction_response_read_encryption(
            cast(Any, session),
            cast(Any, bot_principal()),
            cast(Any, plaintext_interaction()),
            installation(),
            cast(Any, stored),
            "kbe_" + "d" * 43,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == {"code": "INTERACTION_RESPONSE_NOT_FOUND"}


@pytest.mark.asyncio
async def test_encrypted_select_uses_stored_source_without_plaintext_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 3,
                    "custom_id": "private-choice",
                    "options": [{"label": "One", "value": "one"}],
                    "min_values": 1,
                    "max_values": 1,
                }
            ],
        }
    ]
    monkeypatch.setattr(
        interactions,
        "public_component_source",
        AsyncMock(
            return_value=(
                SimpleNamespace(id=91),
                SimpleNamespace(
                    integration_type="guild_install",
                    installation_id=7,
                    installation_domain="guild.example",
                    installation_revision=3,
                ),
                source_components,
            )
        ),
    )
    resolve_entities = AsyncMock()
    monkeypatch.setattr(interactions, "resolve_component_entities", resolve_entities)
    payload = interactions.InteractionCreate(
        application_ref="40@app.example",
        interaction_type="component",
        message_ref="91@guild.example",
        custom_id="private-choice",
        encrypted_payload=mls_envelope(),
    )

    source = await interactions.resolve_component_interaction_source(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        payload,
        (40, "app.example"),
    )

    assert source.component_type == 3
    assert source.values == []
    assert source.source_component is not None
    assert source.source_component["custom_id"] == "private-choice"
    resolve_entities.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_ephemeral_select_uses_committed_routing_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = {
        "version": 1,
        "kind": "message",
        "view_timeout_seconds": 900,
        "components": [
            {
                "type": 3,
                "custom_id": "private-choice",
                "disabled": False,
                "min_values": 1,
                "max_values": 1,
                "option_value_digests": [b64url(b"d" * 32)],
            }
        ],
    }
    monkeypatch.setattr(
        interactions,
        "ephemeral_component_source",
        AsyncMock(
            return_value=(
                SimpleNamespace(id=91),
                SimpleNamespace(id=92),
                contract,
            )
        ),
    )
    payload = interactions.InteractionCreate(
        application_ref="40@app.example",
        interaction_type="component",
        response_id=91,
        view_version=1,
        custom_id="private-choice",
        encrypted_payload=mls_envelope(),
    )

    source = await interactions.resolve_component_interaction_source(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        payload,
        (40, "app.example"),
    )

    assert source.component_type == 3
    assert source.source_component == contract["components"][0]
    assert '"value"' not in json.dumps(source.source_component)


@pytest.mark.asyncio
async def test_encrypted_modal_uses_stored_schema_without_plaintext_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = {
        "version": 1,
        "kind": "modal",
        "custom_id": "private-modal",
        "components": [
            {
                "type": 18,
                "component": {
                    "type": 4,
                    "custom_id": "secret",
                    "required": True,
                    "min_length": 0,
                    "max_length": 4000,
                },
            }
        ],
    }
    response = SimpleNamespace(
        payload={"e2ee": {"interaction_contract": contract}},
    )
    parent = SimpleNamespace(
        application_id=40,
        application_domain="app.example",
        user_id=80,
        user_domain="user.example",
        channel_id=20,
        channel_domain="guild.example",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        status="responded",
    )

    class Result:
        def one_or_none(self) -> tuple[object, object]:
            return response, parent

    session = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    resolve_entities = AsyncMock()
    monkeypatch.setattr(interactions, "resolve_component_entities", resolve_entities)
    payload = interactions.InteractionCreate(
        application_ref="40@app.example",
        interaction_type="modal_submit",
        response_id=99,
        custom_id="private-modal",
        encrypted_payload=mls_envelope(),
    )

    source = await interactions.resolve_modal_interaction_source(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(channel=SimpleNamespace(id=20, origin_domain="guild.example"))),
        cast(Any, SimpleNamespace(id=80, origin_domain="user.example")),
        payload,
        (40, "app.example"),
    )

    assert source.components == []
    assert source.source_modal is not None
    assert source.source_modal["custom_id"] == "private-modal"
    resolve_entities.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_invocation_rechecks_sender_and_bot_participation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    participant = AsyncMock(return_value=True)
    sender = AsyncMock()
    monkeypatch.setattr(interactions, "has_active_bot_e2ee_participation", participant)
    monkeypatch.setattr(interactions, "require_owned_e2ee_sender_device", sender)
    channel = encrypted_channel()
    app_installation = installation()
    payload = interactions.InteractionCreate(
        application_ref="40@app.example",
        command_name="secure",
        encrypted_payload=mls_envelope(),
        attachment_ids=[101],
    )
    context = SimpleNamespace(
        session=SimpleNamespace(),
        settings=SimpleNamespace(domain="guild.example"),
        access=SimpleNamespace(channel=channel),
        actor=SimpleNamespace(id=80, origin_domain="user.example", account_type="human"),
        payload=payload,
        application=SimpleNamespace(
            installation=app_installation,
            user_installation=None,
            dm_capability=None,
        ),
    )

    assert await interactions.validate_interaction_encryption(cast(Any, context)) is True
    participant.assert_awaited_once_with(context.session, app_installation, channel)
    sender.assert_awaited_once_with(
        context.session,
        context.actor,
        payload.encrypted_payload,
        authority_domain="guild.example",
        channel=channel,
    )

    payload.encrypted_payload = mls_envelope(epoch=6)
    with pytest.raises(HTTPException) as stale:
        await interactions.validate_interaction_encryption(cast(Any, context))
    assert stale.value.detail["code"] == "E2EE_POLICY_CONTEXT_MISMATCH"


@pytest.mark.asyncio
async def test_encrypted_invocation_attachment_binds_opaque_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=101,
        upload_channel_id=20,
        upload_channel_domain="guild.example",
        message_id=None,
        message_domain=None,
        interaction_id=None,
        interaction_response_id=None,
        bot_installation_id=None,
        bot_user_installation_id=None,
        asset_binding=None,
        report_id=None,
        encryption_mode="e2ee",
    )
    monkeypatch.setattr(interactions, "lock_media_tombstone_ref", AsyncMock())
    monkeypatch.setattr(interactions, "finalize_attachment", AsyncMock(return_value=attachment))
    monkeypatch.setattr(
        interactions,
        "attachment_payload",
        lambda item, **_kwargs: {"id": str(item.id), "encryption_mode": item.encryption_mode},
    )
    interaction = SimpleNamespace(id=90, channel_id=20, channel_domain="guild.example")

    resolved, rows = await interactions.bind_invocation_attachments(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
        cast(Any, SimpleNamespace()),
        cast(Any, interaction),
        [101],
        {},
        expected_encryption_mode="e2ee",
    )

    assert rows == [attachment]
    assert attachment.interaction_id == 90
    assert resolved == {"101": {"id": "101", "encryption_mode": "e2ee"}}


@pytest.mark.asyncio
async def test_interaction_attachment_ticket_matches_encrypted_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_installation = installation()
    interaction = SimpleNamespace(
        id=90,
        status="pending",
        encrypted_payload=mls_envelope(),
        channel_id=20,
        channel_domain="guild.example",
    )
    principal = SimpleNamespace(
        application=SimpleNamespace(id=40, origin_domain="app.example"),
        user=SimpleNamespace(id=60, origin_domain="app.example"),
        worker=SimpleNamespace(id=80),
    )
    created = SimpleNamespace(id=101, upload_expires_at=None)
    create_ticket = AsyncMock(return_value=(created, "https://uploads.example/opaque"))
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, app_installation)),
    )
    monkeypatch.setattr(interactions, "enforce_keyed_rate_limit", AsyncMock())
    monkeypatch.setattr(interactions, "create_upload_ticket", create_ticket)
    require_participation = AsyncMock()
    monkeypatch.setattr(
        interactions,
        "require_bot_e2ee_participation",
        require_participation,
    )
    monkeypatch.setattr(
        interactions,
        "ticket_payload",
        lambda item, upload_url: {
            "id": str(item.id),
            "encryption_mode": "e2ee",
            "upload_url": upload_url,
            "media_origin": "https://uploads.example",
        },
    )
    channel = encrypted_channel()
    session = SimpleNamespace(get=AsyncMock(return_value=channel), commit=AsyncMock())
    request = UploadTicketRequest(
        filename="encrypted-file",
        content_type="application/octet-stream",
        size=64,
        encryption_mode="e2ee",
        encryption_protocol="kaede-file-v1",
    )

    rendered = await interactions.create_interaction_attachment_ticket(
        90,
        request,
        Response(),
        cast(Any, principal),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
        "kbe_" + b64url(b"d" * 32),
    )

    assert rendered["encryption_mode"] == "e2ee"
    assert rendered["media_origin"] == "https://uploads.example"
    assert create_ticket.await_args.kwargs["encryption_mode"] == "e2ee"
    assert create_ticket.await_args.kwargs["encryption_protocol"] == "kaede-file-v1"
    assert create_ticket.await_args.kwargs["bot_installation"] is app_installation
    assert created.upload_channel_id == channel.id
    assert created.upload_channel_domain == channel.origin_domain
    require_participation.assert_awaited_once_with(
        session,
        app_installation,
        channel,
        "kbe_" + b64url(b"d" * 32),
        worker_id=principal.worker.id,
    )
    session.commit.assert_awaited_once()


def test_gateway_fences_encrypted_interactions_like_messages() -> None:
    encrypted_channels = {(20, "guild.example")}
    event = {
        "t": "INTERACTION_CREATE",
        "d": {
            "channel_id": "20",
            "channel_domain": "guild.example",
            "encrypted_payload": mls_envelope(),
        },
    }

    assert encrypted_bot_content_event(event, encrypted_channels)


def test_bot_device_snapshot_accepts_only_one_complete_authorization_context() -> None:
    capability = bot_e2ee_api.BotE2EESnapshotRequest(
        target_domain="dm.example",
        grant_id="kbdg_" + "a" * 43,
        revision="7",
        conversation_ref="20@dm.example",
        channel_id="20",
        channel_domain="dm.example",
    )

    assert capability.conversation_ref == bot_e2ee_api.EntityRef("20@dm.example")
    with pytest.raises(ValidationError, match="exactly one complete authorization context"):
        bot_e2ee_api.BotE2EESnapshotRequest(
            target_domain="dm.example",
            user_id="80",
            user_domain="user.example",
            grant_id="kbdg_" + "a" * 43,
            revision="7",
            conversation_ref="20@dm.example",
            channel_id="20",
            channel_domain="dm.example",
        )
    with pytest.raises(ValidationError, match="capability conversation is not canonical"):
        bot_e2ee_api.BotE2EESnapshotRequest(
            target_domain="dm.example",
            grant_id="kbdg_" + "a" * 43,
            revision="7",
            conversation_ref="21@dm.example",
            channel_id="20",
            channel_domain="dm.example",
        )


@pytest.mark.asyncio
async def test_capability_authority_can_fetch_bot_device_snapshot_without_target_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grant_id = "kbdg_" + "a" * 43
    application = SimpleNamespace(id=40, origin_domain="app.example")
    bot = SimpleNamespace(id=60, origin_domain="app.example")
    capability = SimpleNamespace(
        grant_id=grant_id,
        revision=7,
        source_installation_domain="guild.example",
        application_id=40,
        application_domain="app.example",
        bot_user_id=60,
        bot_user_domain="app.example",
        authority_domain="dm.example",
        conversation_id=20,
        conversation_domain="dm.example",
        e2ee_mode="participant",
        status="active",
        revoked_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        proof={"signed": True},
    )
    proof = SimpleNamespace(
        grant_id=grant_id,
        revision="7",
        application=EntityRef("40@app.example"),
        bot_user=EntityRef("60@app.example"),
        authority_domain="dm.example",
        e2ee_mode="participant",
        status="active",
    )

    class ApplicationRow:
        def one_or_none(self) -> tuple[object, object]:
            return application, bot

    session = SimpleNamespace(
        execute=AsyncMock(return_value=ApplicationRow()),
        scalar=AsyncMock(return_value=capability),
    )
    snapshot = SimpleNamespace(model_dump=lambda **_kwargs: {"generation": "9"})
    monkeypatch.setattr(bot_e2ee_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(
        bot_e2ee_api,
        "validated_bot_dm_capability_proof",
        AsyncMock(return_value=(SimpleNamespace(), proof)),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "local_bot_e2ee_snapshot",
        AsyncMock(return_value=snapshot),
    )
    build_envelope = AsyncMock(return_value={"type": "bot.e2ee.device-snapshot"})
    monkeypatch.setattr(bot_e2ee_api, "build_envelope", build_envelope)

    rendered = await bot_e2ee_api.federation_bot_e2ee_device_snapshot(
        40,
        bot_e2ee_api.BotE2EESnapshotRequest(
            target_domain="dm.example",
            grant_id=grant_id,
            revision="7",
            conversation_ref="20@dm.example",
            channel_id="20",
            channel_domain="dm.example",
        ),
        cast(Any, SimpleNamespace(origin="dm.example", silenced=True)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="app.example")),
    )

    assert rendered == {"type": "bot.e2ee.device-snapshot"}
    session.scalar.assert_awaited_once()
    build_envelope.assert_awaited_once()


@pytest.mark.asyncio
async def test_silenced_user_context_can_fetch_bot_device_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = SimpleNamespace(id=40, origin_domain="app.example")
    bot = SimpleNamespace(id=60, origin_domain="app.example")

    class ApplicationRow:
        def one_or_none(self) -> tuple[object, object]:
            return application, bot

    target = SimpleNamespace(guild_installations=0, user_installations=1)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=ApplicationRow()),
        get=AsyncMock(return_value=target),
    )
    snapshot = SimpleNamespace(model_dump=lambda **_kwargs: {"generation": "9"})
    monkeypatch.setattr(bot_e2ee_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(
        bot_e2ee_api,
        "local_bot_e2ee_snapshot",
        AsyncMock(return_value=snapshot),
    )
    build_envelope = AsyncMock(return_value={"type": "bot.e2ee.device-snapshot"})
    monkeypatch.setattr(bot_e2ee_api, "build_envelope", build_envelope)

    rendered = await bot_e2ee_api.federation_bot_e2ee_device_snapshot(
        40,
        bot_e2ee_api.BotE2EESnapshotRequest(
            target_domain="user.example",
            user_id="80",
            user_domain="user.example",
            channel_id="20",
            channel_domain="dm.example",
        ),
        cast(Any, SimpleNamespace(origin="user.example", silenced=True)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="app.example")),
    )

    assert rendered == {"type": "bot.e2ee.device-snapshot"}
    session.get.assert_awaited_once()
    build_envelope.assert_awaited_once()


@pytest.mark.parametrize(
    ("snapshot_request", "principal", "target"),
    [
        (
            bot_e2ee_api.BotE2EESnapshotRequest(
                target_domain="guild.example",
                guild_id="10",
                guild_domain="guild.example",
                channel_id="20",
                channel_domain="guild.example",
            ),
            SimpleNamespace(origin="guild.example", silenced=False),
            SimpleNamespace(guild_installations=0, user_installations=1),
        ),
        (
            bot_e2ee_api.BotE2EESnapshotRequest(
                target_domain="user.example",
                user_id="80",
                user_domain="user.example",
                channel_id="20",
                channel_domain="dm.example",
            ),
            SimpleNamespace(origin="user.example", silenced=True),
            SimpleNamespace(guild_installations=1, user_installations=0),
        ),
    ],
)
@pytest.mark.asyncio
async def test_bot_device_snapshot_requires_matching_installation_context(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_request: bot_e2ee_api.BotE2EESnapshotRequest,
    principal: object,
    target: object,
) -> None:
    application = SimpleNamespace(id=40, origin_domain="app.example")
    bot = SimpleNamespace(id=60, origin_domain="app.example")

    class ApplicationRow:
        def one_or_none(self) -> tuple[object, object]:
            return application, bot

    session = SimpleNamespace(
        execute=AsyncMock(return_value=ApplicationRow()),
        get=AsyncMock(return_value=target),
    )
    monkeypatch.setattr(bot_e2ee_api, "enforce_federation_route_rate_limit", AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await bot_e2ee_api.federation_bot_e2ee_device_snapshot(
            40,
            snapshot_request,
            cast(Any, principal),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="app.example")),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "BOT_E2EE_APPLICATION_NOT_FOUND"}


@pytest.mark.asyncio
async def test_silenced_guild_context_cannot_fetch_bot_device_snapshot() -> None:
    with pytest.raises(HTTPException) as caught:
        await bot_e2ee_api.federation_bot_e2ee_device_snapshot(
            40,
            bot_e2ee_api.BotE2EESnapshotRequest(
                target_domain="guild.example",
                guild_id="10",
                guild_domain="guild.example",
                channel_id="20",
                channel_domain="guild.example",
            ),
            cast(Any, SimpleNamespace(origin="guild.example", silenced=True)),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="app.example")),
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {"code": "KAED_FED_INSTANCE_SILENCED"}


@pytest.mark.asyncio
async def test_capability_snapshot_request_uses_grant_not_target_user_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = SimpleNamespace(
        grant_id="kbdg_" + "a" * 43,
        revision=7,
        authority_domain="dm.example",
        conversation_id=20,
        conversation_domain="dm.example",
        application_id=40,
        application_domain="app.example",
        bot_user_id=60,
        bot_user_domain="app.example",
        status="active",
        revoked_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    remote_snapshot = SimpleNamespace(generation="9", devices=[SimpleNamespace(protocol_id="kbe")])
    fetch = AsyncMock(return_value=remote_snapshot)
    monkeypatch.setattr(bot_e2ee_api, "_fetch_remote_device_snapshot", fetch)
    monkeypatch.setattr(
        bot_e2ee_api,
        "materialize_bot_e2ee_snapshot",
        AsyncMock(return_value=[SimpleNamespace(id=1)]),
    )
    session = SimpleNamespace()
    actor = SimpleNamespace(id=80, origin_domain="user.example")
    application = SimpleNamespace(id=40, origin_domain="app.example")
    bot = SimpleNamespace(id=60, origin_domain="app.example", e2ee_device_generation=8)
    channel = SimpleNamespace(id=20, origin_domain="dm.example")

    devices = await bot_e2ee_api._device_snapshot_for_user_installation(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="dm.example")),
        cast(Any, channel),
        cast(Any, actor),
        cast(Any, application),
        cast(Any, bot),
        cast(Any, capability),
    )

    assert devices
    assert fetch.await_args.args[-1] == {
        "grant_id": capability.grant_id,
        "revision": "7",
        "conversation_ref": "20@dm.example",
    }


@pytest.mark.asyncio
async def test_bot_device_snapshot_compaction_is_destination_and_actor_scoped() -> None:
    class Rows:
        def all(self) -> list[tuple[str, str]]:
            return [("app.example", "kcfe_old")]

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(side_effect=[Rows(), SimpleNamespace(), SimpleNamespace()]),
    )
    await federation_events.discard_superseded_latest_state_event(
        cast(Any, session),
        destination="guild.example",
        event_type="e2ee.device-list.changed",
        actor_ref=(60, "app.example"),
    )

    select_statement = session.execute.await_args_list[0].args[0]
    select_params = select_statement.compile().params.values()
    assert {"guild.example", "e2ee.device-list.changed", "60", "app.example"} <= set(select_params)
    outbox_delete = str(session.execute.await_args_list[1].args[0].compile())
    orphan_delete = str(session.execute.await_args_list[2].args[0].compile())
    assert "federation_outbox.destination" in outbox_delete
    assert "NOT (EXISTS" in orphan_delete


@pytest.mark.asyncio
async def test_bot_device_generation_queues_capability_authorities_without_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(
        scalars=AsyncMock(
            side_effect=[
                ["guild.example"],
                ["dm.example", "guild.example"],
            ]
        )
    )
    paused = SimpleNamespace(id=20, origin_domain="app.example")
    monkeypatch.setattr(
        bot_e2ee_api,
        "pause_local_e2ee_for_device_change",
        AsyncMock(return_value=[paused]),
    )
    discard = AsyncMock()
    queue = AsyncMock()
    build = AsyncMock(side_effect=[{"event_id": "one"}, {"event_id": "two"}])
    monkeypatch.setattr(bot_e2ee_api, "discard_superseded_latest_state_event", discard)
    monkeypatch.setattr(bot_e2ee_api, "queue_event", queue)
    monkeypatch.setattr(bot_e2ee_api, "build_envelope", build)
    monkeypatch.setattr(bot_e2ee_api, "profile_from_user", lambda _bot: {"id": "60"})

    paused_channels, destinations = await bot_e2ee_api._queue_bot_device_generation_change(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="app.example")),
        cast(Any, SimpleNamespace(id=40, origin_domain="app.example")),
        cast(Any, SimpleNamespace(id=60, origin_domain="app.example")),
    )

    assert paused_channels == [paused]
    assert destinations == {"guild.example", "dm.example"}
    assert [call.kwargs["destination"] for call in discard.await_args_list] == [
        "dm.example",
        "guild.example",
    ]
    assert [call.args[2] for call in queue.await_args_list] == [
        "dm.example",
        "guild.example",
    ]
    assert not hasattr(session, "commit")


@pytest.mark.asyncio
async def test_equal_generation_bot_device_snapshot_requires_exact_device_set() -> None:
    workers = [
        BotWorker(
            id=100 + index,
            source_id=200 + index,
            source_domain="app.example",
            application_id=40,
            application_domain="app.example",
            name=f"worker-{index}",
            public_key=bytes([index]) * 32,
        )
        for index in (1, 2)
    ]
    devices = [
        BotE2EEDevice(
            id=300 + index,
            source_id=400 + index,
            source_domain="app.example",
            protocol_id="kbe_" + b64url(bytes([index]) * 32),
            application_id=40,
            application_domain="app.example",
            worker_id=workers[index - 1].id,
            identity_key=bytes([index + 2]) * 32,
            credential=f"credential-{index}".encode(),
            capabilities=["e2ee-mls/1"],
            generation=7,
            trust_state="trusted",
        )
        for index in (1, 2)
    ]

    class Rows:
        def tuples(self) -> list[tuple[BotE2EEDevice, BotWorker]]:
            return list(zip(devices, workers, strict=True))

    session = SimpleNamespace(execute=AsyncMock(return_value=Rows()))
    descriptors = [
        bot_e2ee_service.render_bot_e2ee_device(device, worker)
        for device, worker in zip(devices, workers, strict=True)
    ]
    snapshot = bot_e2ee_service.BotE2EEDeviceSnapshot.model_validate(
        {
            "application_id": "40",
            "application_domain": "app.example",
            "bot_user_id": "60",
            "bot_user_domain": "app.example",
            "generation": "7",
            "devices": descriptors,
        }
    )
    with pytest.raises(ValidationError, match="NUL"):
        bot_e2ee_service.BotE2EEDeviceSnapshot.model_validate(
            snapshot.model_dump(mode="json") | {"application_domain": "app.example\x00"}
        )

    accepted = await bot_e2ee_service.materialize_bot_e2ee_snapshot(
        cast(Any, session),
        cast(Any, SimpleNamespace(mint=AsyncMock())),
        cast(Any, SimpleNamespace(id=40, origin_domain="app.example")),
        snapshot,
        known_generation=7,
    )
    assert accepted == devices

    missing_device = snapshot.model_copy(update={"devices": snapshot.devices[:1]})
    with pytest.raises(ValueError, match="generation was equivocated"):
        await bot_e2ee_service.materialize_bot_e2ee_snapshot(
            cast(Any, session),
            cast(Any, SimpleNamespace(mint=AsyncMock())),
            cast(Any, SimpleNamespace(id=40, origin_domain="app.example")),
            missing_device,
            known_generation=7,
        )
