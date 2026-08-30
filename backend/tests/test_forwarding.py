from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from app.api import bots as bots_api
from app.api import channels as channels_api
from app.chat.forwarding import (
    FORWARD_SOURCE_AUTHORIZATION_EVENT,
    authority_attested_forward_source,
    build_forward_source_authorization_content,
    forward_snapshot_custom_emoji_tokens,
    forward_snapshot_matches_attachments,
    forward_snapshot_projection_digest,
    forward_snapshot_sticker_items,
    rebind_forward_snapshot_attachments,
    validate_forward_snapshot,
    validate_forward_snapshot_source_binding,
    validate_forward_source_authorization,
)
from app.chat.schemas import MessageForwardPrepare
from app.core.federation import sign_envelope
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import Attachment, Channel, Message, User
from app.federation.forwarding import validated_forward_source_proof

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "domain": "destination.example",
        "environment": "test",
        "secret_key": VALID_KEY,
        "database_url": "postgresql+asyncpg://test:test@postgres/test",
        "dragonfly_url": "redis://dragonfly:6379/0",
        "media_s3_access_key": "GK00000000000000000000000000000000",
        "media_s3_secret_key": "0" * 64,
        "federation_peer_overrides": {
            "source.example": "http://source-api:8000",
        },
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def snapshot(*, plaintext_sha256: str) -> dict[str, object]:
    return {
        "content": "immutable body",
        "embeds": [],
        "components": [],
        "attachments": [
            {
                "id": "501",
                "origin_domain": "destination.example",
                "filename": "voice.ogg",
                "content_type": "audio/ogg",
                "size": 12,
                "plaintext_sha256": plaintext_sha256,
                "duration_secs": 1.2,
                "waveform": "AQIDBA==",
                "scan_status": "clean",
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
                "variants": {},
            }
        ],
        "mention_user_refs": [],
        "sticker_items": [],
        "message_snapshots": [],
        "message_type": 0,
        "flags": 1 << 13,
        "created_at": datetime(2026, 8, 28, tzinfo=UTC).isoformat(),
        "edited_at": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("guild_id", "guild_context"), [(10, True), (None, False)])
async def test_remote_forward_source_proof_marks_only_guild_sources(
    monkeypatch: pytest.MonkeyPatch,
    guild_id: int | None,
    guild_context: bool,
) -> None:
    response = httpx.Response(
        200,
        json={"authorization": {"type": FORWARD_SOURCE_AUTHORIZATION_EVENT}},
        request=httpx.Request("POST", "https://source.example/_kaede/v1/channels/70"),
    )
    signed = AsyncMock(return_value=response)
    monkeypatch.setattr(channels_api, "signed_request", signed)

    result = await channels_api.remote_forward_source_proof(
        cast(Any, SimpleNamespace()),
        settings(),
        requester=cast(
            Any,
            SimpleNamespace(
                id=7,
                origin_domain="destination.example",
                account_type="human",
                username="requester",
                display_name=None,
                avatar_hash=None,
                banner_hash=None,
                bio=None,
                custom_status=None,
                profile_version=1,
                e2ee_device_generation=0,
            ),
        ),
        source_message_ref=(700, "source.example"),
        source_channel=cast(
            Any,
            SimpleNamespace(
                id=70,
                origin_domain="source.example",
                guild_id=guild_id,
            ),
        ),
        destination_channel=cast(
            Any,
            SimpleNamespace(
                id=80,
                origin_domain="destination.example",
                encryption_mode="plaintext",
            ),
        ),
        nonce="human-forward-1",
    )

    assert result == {"type": FORWARD_SOURCE_AUTHORIZATION_EVENT}
    assert signed.await_args.kwargs["guild_context"] is guild_context


def test_forward_snapshot_binds_destination_plaintext_integrity() -> None:
    plaintext = b"hello world\n"
    plaintext_hex = hashlib.sha256(plaintext).hexdigest()
    plaintext_b64 = base64.urlsafe_b64encode(bytes.fromhex(plaintext_hex)).rstrip(b"=").decode()
    value = snapshot(plaintext_sha256=plaintext_b64)
    destination = [
        {
            "id": "501",
            "origin_domain": "destination.example",
            "filename": "voice.ogg",
            "content_type": "audio/ogg",
            "size": 12,
            "content_sha256": plaintext_hex,
            "duration_secs": 1.2,
            "waveform": "AQIDBA==",
        }
    ]

    assert forward_snapshot_matches_attachments(value, destination)
    assert not forward_snapshot_matches_attachments(
        value,
        [destination[0] | {"content_sha256": "00" * 32}],
    )


def test_forward_snapshot_rejects_ambiguous_integer_and_nul_text() -> None:
    plaintext_b64 = (
        base64.urlsafe_b64encode(hashlib.sha256(b"hello world\n").digest()).rstrip(b"=").decode()
    )
    value = snapshot(plaintext_sha256=plaintext_b64)
    raw_attachments = cast(list[dict[str, object]], value["attachments"])
    with pytest.raises(ValueError):
        validate_forward_snapshot(value | {"attachments": [raw_attachments[0] | {"size": True}]})
    with pytest.raises(ValueError):
        validate_forward_snapshot(value | {"content": "unsafe\x00text"})


def test_plaintext_forward_rebinds_authoritative_snapshot_to_fresh_media() -> None:
    plaintext = b"hello world\n"
    plaintext_hex = hashlib.sha256(plaintext).hexdigest()
    plaintext_b64 = base64.urlsafe_b64encode(bytes.fromhex(plaintext_hex)).rstrip(b"=").decode()
    value = snapshot(plaintext_sha256=plaintext_b64)
    destination = Attachment(
        id=999,
        origin_domain="destination.example",
        uploader_id=7,
        uploader_domain="user.example",
        filename="voice.ogg",
        content_type="audio/ogg",
        detected_content_type="audio/ogg",
        size=len(plaintext),
        object_key="attachments/999",
        duration_secs=1.2,
        waveform="AQIDBA==",
        scan_status="clean",
        encryption_mode="plaintext",
        purpose="attachment",
        content_sha256=plaintext_hex,
    )

    rebound = rebind_forward_snapshot_attachments(value, [destination])
    assert rebound["attachments"][0]["id"] == "999"  # type: ignore[index]
    assert forward_snapshot_projection_digest(rebound) == forward_snapshot_projection_digest(value)
    assert forward_snapshot_matches_attachments(rebound, [destination])

    destination.content_sha256 = "00" * 32
    with pytest.raises(ValueError, match="bytes or metadata"):
        rebind_forward_snapshot_attachments(value, [destination])


def test_plaintext_forward_rebinds_nested_snapshot_media_once() -> None:
    plaintext_hex = hashlib.sha256(b"hello world\n").hexdigest()
    plaintext_b64 = base64.urlsafe_b64encode(bytes.fromhex(plaintext_hex)).rstrip(b"=").decode()
    value = snapshot(plaintext_sha256=plaintext_b64)
    value["message_snapshots"] = [dict(value) | {"message_snapshots": []}]
    destination = Attachment(
        id=999,
        origin_domain="destination.example",
        uploader_id=7,
        uploader_domain="user.example",
        filename="voice.ogg",
        content_type="audio/ogg",
        detected_content_type="audio/ogg",
        size=12,
        object_key="attachments/999",
        duration_secs=1.2,
        waveform="AQIDBA==",
        scan_status="clean",
        encryption_mode="plaintext",
        purpose="attachment",
        content_sha256=plaintext_hex,
    )

    rebound = rebind_forward_snapshot_attachments(value, [destination])
    nested = rebound["message_snapshots"][0]  # type: ignore[index]
    assert nested["attachments"][0]["id"] == "999"
    assert forward_snapshot_matches_attachments(rebound, [destination])

    stale = dict(rebound)
    stale["message_snapshots"] = [dict(nested) | {"attachments": value["attachments"]}]
    assert not forward_snapshot_matches_attachments(stale, [destination])


def test_forward_projection_ignores_attachment_transport_but_not_plaintext() -> None:
    plaintext_digest = (
        base64.urlsafe_b64encode(hashlib.sha256(b"hello world\n").digest()).rstrip(b"=").decode()
    )
    first = snapshot(plaintext_sha256=plaintext_digest)
    rebound = snapshot(plaintext_sha256=plaintext_digest)
    attachments = rebound["attachments"]
    assert isinstance(attachments, list)
    attachment = attachments[0]
    assert isinstance(attachment, dict)
    attachment["id"] = "999"
    attachment["origin_domain"] = "remote.example"
    assert forward_snapshot_projection_digest(first) == forward_snapshot_projection_digest(rebound)

    attachment["plaintext_sha256"] = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
    assert forward_snapshot_projection_digest(first) != forward_snapshot_projection_digest(rebound)


def test_forward_expression_projection_recurses_once_and_is_source_attested() -> None:
    first_sticker = {
        "id": "77",
        "origin_domain": "source.example",
        "name": "First",
        "format_type": 1,
        "media_hash": "a" * 64,
    }
    second_sticker = {
        "id": "88",
        "origin_domain": "nested.example",
        "name": "Second",
        "format_type": 2,
        "media_hash": "b" * 64,
    }
    value = snapshot(
        plaintext_sha256=base64.urlsafe_b64encode(hashlib.sha256(b"hello world\n").digest())
        .rstrip(b"=")
        .decode()
    )
    value["content"] = "outer <:wave:7@source.example>"
    value["sticker_items"] = [first_sticker]
    value["message_snapshots"] = [
        {
            "content": "nested <a:dance:8@nested.example>",
            "embeds": [],
            "components": [],
            "attachments": [],
            "mention_user_refs": [],
            "sticker_items": [second_sticker],
            "message_snapshots": [],
            "message_type": 0,
            "flags": 0,
            "created_at": datetime(2026, 8, 27, tzinfo=UTC).isoformat(),
            "edited_at": None,
        }
    ]

    assert forward_snapshot_sticker_items(value) == [first_sticker, second_sticker]
    assert forward_snapshot_custom_emoji_tokens(value) == [
        "<:wave:7@source.example>",
        "<a:dance:8@nested.example>",
    ]

    source = source_message(encrypted=False)
    source.content = cast(str, value["content"])
    source.sticker_items = [first_sticker]
    source.forward_snapshot = cast(list[dict[str, object]], value["message_snapshots"])[0]
    authorization = build_forward_source_authorization_content(
        source,
        [],
        requester_ref="7@user.example",
        requester_type="human",
        source_channel_ref="70@source.example",
        destination_channel_ref="80@destination.example",
        destination_encryption_mode="e2ee",
        source_nsfw=False,
        nonce="expression-forward-1",
        now=datetime(2026, 8, 28, 12, 1, tzinfo=UTC),
    )
    assert authorization["source_sticker_items"] == [first_sticker, second_sticker]
    assert authorization["source_custom_emoji_refs"] == [
        "<:wave:7@source.example>",
        "<a:dance:8@nested.example>",
    ]


def test_disclosed_forward_metadata_must_match_the_signed_source_proof() -> None:
    plaintext_digest = (
        base64.urlsafe_b64encode(hashlib.sha256(b"hello world\n").digest()).rstrip(b"=").decode()
    )
    value = snapshot(plaintext_sha256=plaintext_digest)
    expected = {
        "source_projection_digest": forward_snapshot_projection_digest(value),
        "source_created_at": value["created_at"],
        "source_edited_at": None,
        "source_flags": 1 << 13,
        "source_message_type": 0,
    }
    assert validate_forward_snapshot_source_binding(value, **expected)["message_type"] == 0

    for mutation in (
        {"message_type": 19},
        {"flags": 0},
        {"created_at": datetime(2026, 8, 27, tzinfo=UTC).isoformat()},
        {"edited_at": datetime(2026, 8, 29, tzinfo=UTC).isoformat()},
    ):
        with pytest.raises(ValueError, match="source proof"):
            validate_forward_snapshot_source_binding(value | mutation, **expected)
    for mutation in ({"message_type": True}, {"flags": False}):
        with pytest.raises(ValueError, match="forward snapshot"):
            validate_forward_snapshot_source_binding(value | mutation, **expected)


def source_message(*, encrypted: bool) -> Message:
    created_at = datetime(2026, 8, 28, 12, tzinfo=UTC)
    return Message(
        id=700,
        origin_domain="source.example",
        channel_id=70,
        channel_domain="source.example",
        author_id=7,
        author_domain="user.example",
        content=None if encrypted else "authenticated source",
        e2ee=(
            {
                "version": 2,
                "rich_payload_digest": "B" * 43,
                "forward_projection_version": 2,
                "forward_projection_digest": "A" * 43,
            }
            if encrypted
            else None
        ),
        embeds=[],
        components=[],
        sticker_items=[],
        message_type=19,
        flags=0,
        created_at=created_at,
    )


def test_forward_source_authorization_is_exact_and_expiring() -> None:
    now = datetime.now(UTC)
    content = build_forward_source_authorization_content(
        source_message(encrypted=False),
        [],
        requester_ref="7@user.example",
        requester_type="human",
        source_channel_ref="70@source.example",
        destination_channel_ref="80@destination.example",
        destination_encryption_mode="plaintext",
        source_nsfw=False,
        nonce="forward-1",
        now=now,
    )

    validated = validate_forward_source_authorization(
        content,
        expected_authority="source.example",
        requester_ref="7@user.example",
        destination_channel_ref="80@destination.example",
        destination_encryption_mode="plaintext",
        nonce="forward-1",
        now=now,
    )
    assert validated["source_snapshot"] is not None
    assert authority_attested_forward_source(
        FORWARD_SOURCE_AUTHORIZATION_EVENT,
        content,
        {"source_channel_ref": "70@source.example"},
        expected_authority="source.example",
        actor=("7", "user.example"),
    )
    for mutation in (
        {"destination_channel_ref": "81@destination.example"},
        {"source_projection_digest": "C" * 43},
        {"source_message_type": 0},
    ):
        tampered = content | mutation
        try:
            validate_forward_source_authorization(
                tampered,
                expected_authority="source.example",
                requester_ref="7@user.example",
                destination_channel_ref="80@destination.example",
                destination_encryption_mode="plaintext",
                nonce="forward-1",
                now=now,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("tampered forward authorization was accepted")
    try:
        validate_forward_source_authorization(
            content,
            expected_authority="source.example",
            now=now + timedelta(seconds=91),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expired forward authorization was accepted")


def test_queued_forward_uses_signed_outer_event_time_without_extending_expiry() -> None:
    issued_at = datetime.now(UTC) - timedelta(minutes=10)
    content = build_forward_source_authorization_content(
        source_message(encrypted=False),
        [],
        requester_ref="7@user.example",
        requester_type="human",
        source_channel_ref="70@source.example",
        destination_channel_ref="80@destination.example",
        destination_encryption_mode="plaintext",
        source_nsfw=False,
        nonce="queued-forward-1",
        now=issued_at,
    )

    # A proposal durably signed while the proof was live remains retryable.
    validate_forward_source_authorization(
        content,
        expected_authority="source.example",
        now=issued_at + timedelta(seconds=30),
    )
    # A proposal first issued after the proof expired still fails closed.
    with pytest.raises(ValueError, match="binding"):
        validate_forward_source_authorization(
            content,
            expected_authority="source.example",
            now=issued_at + timedelta(seconds=91),
        )


def test_encrypted_forward_authorization_never_discloses_plaintext() -> None:
    content = build_forward_source_authorization_content(
        source_message(encrypted=True),
        [],
        requester_ref="9@app.example",
        requester_type="bot",
        source_channel_ref="70@source.example",
        destination_channel_ref="80@destination.example",
        destination_encryption_mode="e2ee",
        source_nsfw=False,
        nonce="forward-bot-1",
        application_ref="90@app.example",
        e2ee_device_id="kbe_" + "D" * 43,
        now=datetime(2026, 8, 28, 12, 1, tzinfo=UTC),
    )

    assert content["source_snapshot"] is None
    assert content["source_projection_digest"] == "A" * 43
    assert content["source_encryption_mode"] == "e2ee"


@pytest.mark.asyncio
async def test_cross_authority_forward_proof_verifies_signature_and_exact_use() -> None:
    now = datetime.now(UTC) - timedelta(minutes=10)
    content = build_forward_source_authorization_content(
        source_message(encrypted=True),
        [],
        requester_ref="9@app.example",
        requester_type="bot",
        source_channel_ref="70@source.example",
        destination_channel_ref="80@destination.example",
        destination_encryption_mode="e2ee",
        source_nsfw=False,
        nonce="cross-authority-1",
        application_ref="90@app.example",
        e2ee_device_id="kbe_" + "D" * 43,
        now=now,
    )
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    raw: dict[str, object] = {
        "event_id": "kcfe_forwardproof0001",
        "origin": "source.example",
        "type": FORWARD_SOURCE_AUTHORIZATION_EVENT,
        "ts": int(now.timestamp() * 1000),
        "actor": {"id": "9", "domain": "app.example"},
        "context": {"source_channel_ref": "70@source.example"},
        "content": content,
    }
    raw["signatures"] = {"source.example": {"ed25519:test": sign_envelope(raw, private_key)}}

    class FakeSession:
        async def get(self, _model: object, _identity: object) -> object:
            return SimpleNamespace(
                public_key=private_key.public_key().public_bytes_raw(),
                expired_at=None,
            )

    kwargs: dict[str, object] = {
        "requester_ref": "9@app.example",
        "requester_type": "bot",
        "source_message_ref": "700@source.example",
        "source_channel_ref": "70@source.example",
        "destination_channel_ref": "80@destination.example",
        "destination_encryption_mode": "e2ee",
        "nonce": "cross-authority-1",
        "application_ref": "90@app.example",
        "e2ee_device_id": "kbe_" + "D" * 43,
        "validation_time": now + timedelta(seconds=30),
    }
    validated = await validated_forward_source_proof(
        cast(Any, FakeSession()),
        settings(),
        raw,
        **cast(Any, kwargs),
    )
    assert validated["source_projection_digest"] == "A" * 43
    assert validated["source_snapshot"] is None

    with pytest.raises(ValueError, match="binding"):
        await validated_forward_source_proof(
            cast(Any, FakeSession()),
            settings(),
            raw,
            **cast(Any, kwargs | {"validation_time": None}),
        )

    for mutation in (
        {"destination_channel_ref": "81@destination.example"},
        {"nonce": "cross-authority-2"},
        {"application_ref": "91@app.example"},
        {"e2ee_device_id": "kbe_" + "E" * 43},
    ):
        with pytest.raises(ValueError, match="binding"):
            await validated_forward_source_proof(
                cast(Any, FakeSession()),
                settings(),
                raw,
                **cast(Any, kwargs | mutation),
            )

    tampered = dict(raw)
    tampered_content = dict(content)
    tampered_content["source_projection_digest"] = "B" * 43
    tampered["content"] = tampered_content
    with pytest.raises(ValueError, match="signature"):
        await validated_forward_source_proof(
            cast(Any, FakeSession()),
            settings(),
            tampered,
            **cast(Any, kwargs),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_domain", "destination_mode", "source_mode", "expected_helper"),
    (
        ("destination.example", "e2ee", "plaintext", "local"),
        ("source.example", "plaintext", "e2ee", "remote"),
    ),
)
async def test_human_forward_prepare_pins_authority_and_disclosure_contract(
    monkeypatch: pytest.MonkeyPatch,
    source_domain: str,
    destination_mode: str,
    source_mode: str,
    expected_helper: str,
) -> None:
    configured = settings()
    requester = User(
        id=7,
        origin_domain="user.example",
        is_local=False,
        username="forwarder",
        account_type="human",
    )
    source_channel = Channel(
        id=70,
        origin_domain=source_domain,
        guild_id=1,
        guild_domain=source_domain,
        type=0,
        encryption_mode=source_mode,
        created_floor_id=70,
    )
    destination_channel = Channel(
        id=80,
        origin_domain="remote-destination.example",
        guild_id=2,
        guild_domain="remote-destination.example",
        type=0,
        encryption_mode=destination_mode,
        created_floor_id=80,
    )
    source = source_message(encrypted=source_mode == "e2ee")
    source.channel_domain = source_domain
    raw_authorization = {"type": FORWARD_SOURCE_AUTHORIZATION_EVENT}
    validated = {
        "source_message_ref": f"700@{source_domain}",
        "source_channel_ref": f"70@{source_domain}",
        "source_encryption_mode": source_mode,
        "source_projection_version": 2,
        "source_projection_digest": "A" * 43,
        "source_created_at": source.created_at.isoformat(),
        "source_edited_at": None,
        "source_flags": 0,
        "source_message_type": 19,
        "source_nsfw": False,
        "source_attachment_refs": [],
        "source_snapshot": ({"content": "source"} if source_mode == "plaintext" else None),
    }
    accesses = [
        SimpleNamespace(channel=source_channel),
        SimpleNamespace(channel=destination_channel),
    ]
    session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        scalars=AsyncMock(return_value=[]),
    )
    local = AsyncMock(return_value=raw_authorization)
    remote = AsyncMock(return_value=raw_authorization)
    monkeypatch.setattr(channels_api, "load_channel_access", AsyncMock(side_effect=accesses))
    monkeypatch.setattr(channels_api, "require_channel_permissions", AsyncMock())
    monkeypatch.setattr(channels_api, "channel_message", AsyncMock(return_value=source))
    monkeypatch.setattr(channels_api, "effective_channel_nsfw", AsyncMock(return_value=False))
    monkeypatch.setattr(channels_api, "local_forward_source_proof", local)
    monkeypatch.setattr(channels_api, "remote_forward_source_proof", remote)
    verify = AsyncMock(return_value=validated)
    monkeypatch.setattr(channels_api, "validate_signed_forward_source_proof", verify)

    result = await channels_api.prepare_forward_message(
        EntityRef(f"70@{source_domain}"),
        EntityRef(f"700@{source_domain}"),
        MessageForwardPrepare.model_validate(
            {
                "destinations": [
                    {
                        "channel_id": "80@remote-destination.example",
                        "client_nonce": "human-forward-1",
                    }
                ]
            }
        ),
        cast(Any, SimpleNamespace(user=requester)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        configured,
    )

    assert result["source"] == {
        "message_ref": f"700@{source_domain}",
        "channel_ref": f"70@{source_domain}",
        "encryption_mode": source_mode,
        "projection_version": 2,
        "projection_digest": "A" * 43,
        "created_at": source.created_at.isoformat(),
        "edited_at": None,
        "flags": 0,
        "message_type": 19,
        "nsfw": False,
        "attachment_refs": [],
        "snapshot": ({"content": "source"} if source_mode == "plaintext" else None),
    }
    assert result["destinations"] == [
        {
            "channel_id": "80@remote-destination.example",
            "client_nonce": "human-forward-1",
            "encryption_mode": destination_mode,
            "requires_plaintext_disclosure": (
                source_mode == "e2ee" and destination_mode == "plaintext"
            ),
            "authorization": raw_authorization,
        }
    ]
    assert (local.await_count, remote.await_count) == (
        (1, 0) if expected_helper == "local" else (0, 1)
    )
    verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_forward_source_access_never_composes_grants_across_installations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_message(encrypted=True)
    source.channel_id = 70
    source.channel_domain = "source.example"
    source_channel = Channel(
        id=70,
        origin_domain="source.example",
        guild_id=1,
        guild_domain="source.example",
        type=0,
        encryption_mode="e2ee",
        created_floor_id=70,
    )
    exact_installation = SimpleNamespace(
        id=77,
        granted_scopes=["messages.history"],
        granted_intents=["message_content"],
    )
    participation = SimpleNamespace(history_floor_message_id=1)
    principal = SimpleNamespace(
        user=SimpleNamespace(id=9, origin_domain="apps.example"),
        application=SimpleNamespace(id=90, origin_domain="apps.example"),
        worker=SimpleNamespace(id=5),
        scopes={"messages.history", "messages.content", "attachments.read"},
        intents={"message_content"},
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=source),
        scalar=AsyncMock(return_value=True),
    )
    installation_lookup = AsyncMock(return_value=(source_channel, exact_installation))
    floor_check = AsyncMock(
        return_value=[{"id": str(source.id), "origin_domain": source.origin_domain}]
    )
    monkeypatch.setattr(bots_api, "installation_for_channel", installation_lookup)
    monkeypatch.setattr(
        bots_api,
        "require_bot_channel_e2ee_access",
        AsyncMock(return_value=participation),
    )
    monkeypatch.setattr(bots_api, "bot_messages_after_history_floor", floor_check)

    # The token's global content/attachment scopes cannot be combined with a
    # different installation; the exact source installation must grant each.
    with pytest.raises(HTTPException) as missing_content:
        await bots_api.require_bot_forward_source_access(
            cast(Any, session),
            settings(),
            cast(Any, principal),
            EntityRef("700@source.example"),
            e2ee_device_id="kbe_" + "D" * 43,
            installation_id=77,
        )
    assert missing_content.value.detail == {"code": "BOT_MESSAGE_CONTENT_REQUIRED"}

    exact_installation.granted_scopes.append("messages.content")
    principal.intents = set()
    with pytest.raises(HTTPException) as missing_content_intent:
        await bots_api.require_bot_forward_source_access(
            cast(Any, session),
            settings(),
            cast(Any, principal),
            EntityRef("700@source.example"),
            e2ee_device_id="kbe_" + "D" * 43,
            installation_id=77,
        )
    assert missing_content_intent.value.detail == {"code": "BOT_MESSAGE_CONTENT_REQUIRED"}
    principal.intents = {"message_content"}
    with pytest.raises(HTTPException) as missing_attachment:
        await bots_api.require_bot_forward_source_access(
            cast(Any, session),
            settings(),
            cast(Any, principal),
            EntityRef("700@source.example"),
            e2ee_device_id="kbe_" + "D" * 43,
            installation_id=77,
        )
    assert missing_attachment.value.detail == {"code": "BOT_ATTACHMENT_ACCESS_REQUIRED"}

    exact_installation.granted_scopes.append("attachments.read")
    resolved = await bots_api.require_bot_forward_source_access(
        cast(Any, session),
        settings(),
        cast(Any, principal),
        EntityRef("700@source.example"),
        e2ee_device_id="kbe_" + "D" * 43,
        installation_id=77,
    )
    assert resolved == (source, source_channel, exact_installation, participation)
    assert installation_lookup.await_args.args[5] == 77
    assert floor_check.await_count == 4


@pytest.mark.asyncio
async def test_bot_encrypted_forward_source_must_be_above_exact_history_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_message(encrypted=True)
    source_channel = Channel(
        id=70,
        origin_domain="source.example",
        guild_id=1,
        guild_domain="source.example",
        type=0,
        encryption_mode="e2ee",
        created_floor_id=70,
    )
    installation = SimpleNamespace(
        id=77,
        granted_scopes=["messages.history", "messages.content", "attachments.read"],
    )
    principal = SimpleNamespace(
        user=SimpleNamespace(id=9, origin_domain="apps.example"),
        application=SimpleNamespace(id=90, origin_domain="apps.example"),
        worker=SimpleNamespace(id=5),
        scopes={"messages.history", "messages.content", "attachments.read"},
    )
    session = SimpleNamespace(get=AsyncMock(return_value=source))
    monkeypatch.setattr(
        bots_api,
        "installation_for_channel",
        AsyncMock(return_value=(source_channel, installation)),
    )
    monkeypatch.setattr(
        bots_api,
        "require_bot_channel_e2ee_access",
        AsyncMock(return_value=SimpleNamespace(history_floor_message_id=800)),
    )
    monkeypatch.setattr(
        bots_api,
        "bot_messages_after_history_floor",
        AsyncMock(return_value=[]),
    )

    with pytest.raises(HTTPException) as hidden:
        await bots_api.require_bot_forward_source_access(
            cast(Any, session),
            settings(),
            cast(Any, principal),
            EntityRef("700@source.example"),
            e2ee_device_id="kbe_" + "D" * 43,
            installation_id=77,
        )
    assert hidden.value.status_code == 404
    assert hidden.value.detail == {"code": "MESSAGE_NOT_FOUND"}
