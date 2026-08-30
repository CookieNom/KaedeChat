import base64
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.channels import require_owned_e2ee_sender_device
from app.chat.e2ee import (
    E2EE_PROTOCOL_MLS_10,
    E2EE_SUITE_MLS_128,
    MAX_E2EE_ARRAY_MEMBERS,
    MAX_E2EE_ENVELOPE_BYTES,
    MAX_E2EE_ENVELOPE_DEPTH,
    MAX_E2EE_ENVELOPE_NODES,
    MAX_E2EE_KEY_BYTES,
    MessageEncryptionPolicyError,
    validate_channel_encryption_policy,
    validate_channel_encryption_policy_transition,
    validate_e2ee_envelope,
    validate_message_encryption_policy,
)
from app.chat.e2ee_controls import (
    authority_attested_direct_dm_control,
    authority_attested_room_policy_change,
    room_policy_change_context,
)
from app.chat.schemas import MessageCreate, MessageEdit
from app.core.federation import FEDERATION_CAPABILITIES, canonical_json
from app.core.json_limits import MAX_SAFE_JSON_INTEGER, strict_json_loads
from app.db.models import Message
from app.federation.replication import (
    authoritative_dm_control,
    replicated_message_create_fingerprint,
)
from app.federation.schemas import EventEnvelope


def test_e2ee_envelope_is_opaque_versioned_and_bounded() -> None:
    envelope = {"version": 1, "suite": "future-suite", "ciphertext": "opaque"}
    assert validate_e2ee_envelope(envelope) == envelope
    with pytest.raises(ValueError, match="positive version"):
        validate_e2ee_envelope({"version": 0, "ciphertext": "opaque"})
    with pytest.raises(ValueError, match="too large"):
        validate_e2ee_envelope({"version": 1, "ciphertext": "x" * MAX_E2EE_ENVELOPE_BYTES})


def test_dm_controls_require_the_signed_conversation_authority() -> None:
    envelope = {"operation": "commit", "ciphertext": "opaque"}
    assert authoritative_dm_control(
        envelope,
        message_type=7,
        flags=4,
        message_origin="authority.test",
        event_origin="authority.test",
        conversation_authority="authority.test",
    ) == (True, True)
    # A participant can legitimately mint and sign its own ordinary messages,
    # but that is never sufficient authority for an MLS control.
    assert authoritative_dm_control(
        envelope,
        message_type=7,
        flags=4,
        message_origin="participant.test",
        event_origin="participant.test",
        conversation_authority="authority.test",
    ) == (True, False)
    assert authoritative_dm_control(
        envelope,
        message_type=0,
        flags=0,
        message_origin="authority.test",
        event_origin="authority.test",
        conversation_authority="authority.test",
    ) == (True, False)


@pytest.mark.parametrize(("operation", "apply"), [("welcome", True), ("commit", False)])
def test_remote_direct_dm_activation_and_rekey_controls_have_one_closed_shape(
    operation: str,
    apply: bool,
) -> None:
    content: dict[str, Any] = {
        "message": {
            "origin_domain": "authority.test",
            "channel_id": "11",
            "channel_domain": "authority.test",
            "author_id": "7",
            "author_domain": "participant.test",
            "message_type": 7,
            "flags": 4,
            "e2ee": {
                "operation": operation,
                "ciphertext": "opaque",
                "protocol": E2EE_PROTOCOL_MLS_10,
                "suite": E2EE_SUITE_MLS_128,
                "group_id": "g" * 43,
                "policy_generation": "2",
                "epoch": "1",
            },
        },
        "author": {"id": "7", "origin_domain": "participant.test"},
        "encryption_policy": {
            "mode": "e2ee",
            "state": "active",
            "generation": "2",
            "protocol": E2EE_PROTOCOL_MLS_10,
            "suite": E2EE_SUITE_MLS_128,
            "group_id": "g" * 43,
            "epoch": "1",
        },
        "e2ee_control": {
            "operation_id": "keo_" + "o" * 43,
            "operation_domain": "authority.test",
            "apply": apply,
        },
    }

    assert authority_attested_direct_dm_control(
        "dm.message.create",
        content,
        expected_authority="authority.test",
        actor_id="7",
        actor_domain="participant.test",
    )

    extra = deepcopy(content)
    extra["unsigned_hint"] = True
    assert not authority_attested_direct_dm_control(
        "dm.message.create",
        extra,
        expected_authority="authority.test",
        actor_id="7",
        actor_domain="participant.test",
    )

    mismatched_policy = deepcopy(content)
    mismatched_policy["encryption_policy"]["generation"] = "3"
    assert not authority_attested_direct_dm_control(
        "dm.message.create",
        mismatched_policy,
        expected_authority="authority.test",
        actor_id="7",
        actor_domain="participant.test",
    )

    # The exception must never turn the authority into a generic remote-user
    # message signer, even when an ordinary message copies control-like fields.
    content["message"]["message_type"] = 0
    assert not authority_attested_direct_dm_control(
        "dm.message.create",
        content,
        expected_authority="authority.test",
        actor_id="7",
        actor_domain="participant.test",
    )


def test_remote_room_policy_pause_binds_authority_actor_channel_and_scope() -> None:
    actor = SimpleNamespace(id=7, origin_domain="participant.test")
    channel = SimpleNamespace(
        id=11,
        origin_domain="authority.test",
        guild_id=13,
        guild_domain="authority.test",
    )
    content: dict[str, Any] = {
        "channel_id": "11",
        "channel_domain": "authority.test",
        "encryption_policy": {
            "mode": "e2ee",
            "state": "rekeying",
            "generation": "2",
            "protocol": E2EE_PROTOCOL_MLS_10,
            "suite": E2EE_SUITE_MLS_128,
            "group_id": "g" * 43,
            "epoch": "1",
        },
    }
    context = room_policy_change_context(channel, actor)

    assert authority_attested_room_policy_change(
        "e2ee.room-policy.changed",
        content,
        context,
        expected_authority="authority.test",
        actor_id="7",
        actor_domain="participant.test",
    )

    forged = deepcopy(context)
    forged["actor"]["id"] = "8"
    assert not authority_attested_room_policy_change(
        "e2ee.room-policy.changed",
        content,
        forged,
        expected_authority="authority.test",
        actor_id="7",
        actor_domain="participant.test",
    )

    downgraded = deepcopy(content)
    downgraded["encryption_policy"]["state"] = "active"
    assert not authority_attested_room_policy_change(
        "e2ee.room-policy.changed",
        downgraded,
        context,
        expected_authority="authority.test",
        actor_id="7",
        actor_domain="participant.test",
    )


def test_e2ee_envelope_has_iterative_shape_limits() -> None:
    nested: object = "ciphertext"
    for _ in range(MAX_E2EE_ENVELOPE_DEPTH + 1):
        nested = {"nested": nested}
    with pytest.raises(ValueError, match="nesting depth"):
        validate_e2ee_envelope({"version": 1, "payload": nested})

    with pytest.raises(ValueError, match="too many items"):
        validate_e2ee_envelope({"version": 1, "recipients": [None] * (MAX_E2EE_ARRAY_MEMBERS + 1)})
    with pytest.raises(ValueError, match="too many values"):
        validate_e2ee_envelope(
            {
                "version": 1,
                "payload": [[None] * 1024 for _ in range(MAX_E2EE_ENVELOPE_NODES // 1024)],
            }
        )
    with pytest.raises(ValueError, match="object key.*too large"):
        validate_e2ee_envelope({"version": 1, "x" * (MAX_E2EE_KEY_BYTES + 1): "value"})


def test_e2ee_envelope_rejects_non_json_and_database_hostile_values() -> None:
    with pytest.raises(ValueError, match="floating-point"):
        validate_e2ee_envelope({"version": 1, "value": float("nan")})
    with pytest.raises(ValueError, match="floating-point"):
        validate_e2ee_envelope({"version": 1, "value": 1.5})
    with pytest.raises(ValueError, match="supported range"):
        validate_e2ee_envelope({"version": 1, "counter": MAX_SAFE_JSON_INTEGER + 1})
    with pytest.raises(ValueError, match="NUL"):
        validate_e2ee_envelope({"version": 1, "ciphertext": "opaque\x00value"})

    cyclic: dict[str, object] = {"version": 1}
    cyclic["payload"] = cyclic
    with pytest.raises(ValueError, match="cyclic or shared"):
        validate_e2ee_envelope(cyclic)


def test_e2ee_envelope_returns_a_detached_normalized_json_copy() -> None:
    recipients: list[object] = [{"device": "one"}]
    envelope = {"version": 1, "recipients": recipients, "range": (1, 2)}
    validated = validate_e2ee_envelope(envelope)
    assert validated == {"version": 1, "recipients": [{"device": "one"}], "range": [1, 2]}

    recipients[0] = {"device": "changed"}
    assert validated is not None
    assert validated["recipients"] == [{"device": "one"}]


def test_federation_advertises_supported_e2ee_protocols() -> None:
    assert "e2ee-mls/1" in FEDERATION_CAPABILITIES
    assert "e2ee-media/1" in FEDERATION_CAPABILITIES
    assert "dm-history-page/1" in FEDERATION_CAPABILITIES


def test_federation_event_rejects_pathological_json_before_signing() -> None:
    nested: object = {}
    for _ in range(25):
        nested = {"nested": nested}
    with pytest.raises(ValidationError, match="nesting depth"):
        EventEnvelope.model_validate(
            {
                "event_id": "kcfe_0123456789abcdef",
                "origin": "alpha.localhost",
                "type": "dm.message.create",
                "ts": 42,
                "actor": {"id": "123", "domain": "alpha.localhost"},
                "context": {},
                "content": nested,
                "signatures": {"alpha.localhost": {"ed25519:test": "signature"}},
            }
        )
    with pytest.raises(ValidationError, match="floating-point"):
        EventEnvelope.model_validate(
            {
                "event_id": "kcfe_0123456789abcdef",
                "origin": "alpha.localhost",
                "type": "dm.message.create",
                "ts": 42,
                "actor": {"id": "123", "domain": "alpha.localhost"},
                "context": {},
                "content": {"ambiguous": 1.0},
                "signatures": {"alpha.localhost": {"ed25519:test": "signature"}},
            }
        )


def test_federation_json_rejects_ambiguous_or_nonportable_values() -> None:
    with pytest.raises(ValueError, match="duplicate object key"):
        strict_json_loads('{"content":{"version":1,"version":2}}')
    with pytest.raises(ValueError, match="floating-point"):
        strict_json_loads('{"content":{"value":1.25}}')
    with pytest.raises(ValueError, match="supported range"):
        canonical_json({"id": MAX_SAFE_JSON_INTEGER + 1})


def test_message_schemas_never_mix_plaintext_and_ciphertext() -> None:
    envelope = {"version": 1, "ciphertext": "opaque"}
    assert MessageCreate(e2ee=envelope).e2ee == envelope
    assert MessageEdit(e2ee=envelope).e2ee == envelope
    assert MessageEdit(e2ee=envelope, attachment_ids=["41"]).attachment_ids == [41]
    with pytest.raises(ValueError, match="plaintext and encrypted"):
        MessageCreate(content="visible", e2ee=envelope)
    with pytest.raises(ValueError, match="rich plaintext"):
        MessageEdit(content="visible", e2ee=envelope)


@pytest.mark.parametrize(
    ("mode", "content", "e2ee", "attachments", "code"),
    [
        ("plaintext", None, {"version": 1}, 0, "E2EE_NOT_ENABLED"),
        ("e2ee", "visible", None, 0, "E2EE_ENVELOPE_REQUIRED"),
        ("e2ee", None, None, 0, "E2EE_ENVELOPE_REQUIRED"),
    ],
)
def test_room_policy_rejects_mixed_mode_writes(
    mode: str,
    content: object,
    e2ee: object,
    attachments: int,
    code: str,
) -> None:
    with pytest.raises(MessageEncryptionPolicyError) as raised:
        validate_message_encryption_policy(
            mode,
            content=content,
            e2ee=e2ee,
            attachment_count=attachments,
        )
    assert raised.value.code == code


def test_room_policy_accepts_matching_plaintext_and_encrypted_writes() -> None:
    validate_message_encryption_policy(
        "e2ee",
        content=None,
        e2ee={"version": 1},
        attachment_count=1,
    )
    validate_message_encryption_policy(
        "plaintext",
        content="visible",
        e2ee=None,
    )
    validate_message_encryption_policy(
        "e2ee",
        content=None,
        e2ee={"version": 1},
    )


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def test_active_mls_room_binds_envelope_to_group_generation_and_epoch() -> None:
    envelope = validate_e2ee_envelope(
        {
            "version": 2,
            "protocol": E2EE_PROTOCOL_MLS_10,
            "suite": E2EE_SUITE_MLS_128,
            "group_id": b64url(b"group-id"),
            "policy_generation": "4",
            "epoch": "9",
            "sender_device_id": "ked_" + b64url(b"d" * 32),
            "operation": "create",
            "ciphertext": b64url(b"opaque MLS wire message"),
        }
    )
    validate_message_encryption_policy(
        "e2ee",
        content=None,
        e2ee=envelope,
        policy_generation=4,
        policy_epoch=9,
        policy_group_id=b64url(b"group-id"),
    )

    with pytest.raises(MessageEncryptionPolicyError) as raised:
        validate_message_encryption_policy(
            "e2ee",
            content=None,
            e2ee=envelope,
            policy_generation=4,
            policy_epoch=10,
            policy_group_id=b64url(b"group-id"),
        )
    assert raised.value.code == "E2EE_POLICY_CONTEXT_MISMATCH"


def test_active_mls_room_rejects_legacy_opaque_envelope() -> None:
    with pytest.raises(MessageEncryptionPolicyError) as raised:
        validate_message_encryption_policy(
            "e2ee",
            content=None,
            e2ee={"version": 1, "ciphertext": "legacy"},
            policy_generation=1,
            policy_epoch=0,
            policy_group_id=b64url(b"group-id"),
        )
    assert raised.value.code == "E2EE_MLS_ENVELOPE_REQUIRED"


def test_generated_room_policy_is_typed_and_downgrade_resistant() -> None:
    policy = validate_channel_encryption_policy(
        {
            "mode": "e2ee",
            "state": "active",
            "generation": "3",
            "protocol": E2EE_PROTOCOL_MLS_10,
            "suite": E2EE_SUITE_MLS_128,
            "group_id": "group-opaque-id",
            "epoch": "9",
        }
    )
    assert policy["generation"] == 3
    assert policy["epoch"] == 9

    with pytest.raises(ValueError, match="inconsistent"):
        validate_channel_encryption_policy(
            {
                "mode": "plaintext",
                "state": "active",
                "generation": "3",
                "protocol": E2EE_PROTOCOL_MLS_10,
                "suite": E2EE_SUITE_MLS_128,
                "group_id": "group-opaque-id",
                "epoch": "9",
            }
        )


def test_federated_room_policy_rejects_equal_generation_equivocation() -> None:
    channel = SimpleNamespace(
        encryption_mode="e2ee",
        encryption_state="active",
        encryption_policy_generation=3,
        encryption_protocol=E2EE_PROTOCOL_MLS_10,
        encryption_suite=E2EE_SUITE_MLS_128,
        encryption_group_id="group-opaque-id",
        encryption_epoch=9,
    )
    incoming = validate_channel_encryption_policy(
        {
            "mode": "e2ee",
            "state": "active",
            "generation": "3",
            "protocol": E2EE_PROTOCOL_MLS_10,
            "suite": E2EE_SUITE_MLS_128,
            "group_id": "group-opaque-id",
            "epoch": "8",
        }
    )

    with pytest.raises(ValueError, match="equivocated"):
        validate_channel_encryption_policy_transition(channel, incoming, label="channel")


@pytest.mark.parametrize(
    ("current", "incoming"),
    [
        ("proposed", "activating"),
        ("proposed", "active"),
        ("activating", "active"),
        ("activating", "failed"),
        ("active", "rekeying"),
    ],
)
def test_federated_room_policy_accepts_legitimate_same_generation_progress(
    current: str, incoming: str
) -> None:
    channel = SimpleNamespace(
        encryption_mode="e2ee",
        encryption_state=current,
        encryption_policy_generation=3,
        encryption_protocol=E2EE_PROTOCOL_MLS_10,
        encryption_suite=E2EE_SUITE_MLS_128,
        encryption_group_id="group-opaque-id",
        encryption_epoch=9,
    )
    policy = validate_channel_encryption_policy(
        {
            "mode": "e2ee",
            "state": incoming,
            "generation": "3",
            "protocol": E2EE_PROTOCOL_MLS_10,
            "suite": E2EE_SUITE_MLS_128,
            "group_id": "group-opaque-id",
            "epoch": "9",
        }
    )
    validate_channel_encryption_policy_transition(channel, policy, label="channel")


def test_federated_room_policy_rejects_same_generation_state_rollback() -> None:
    channel = SimpleNamespace(
        encryption_mode="e2ee",
        encryption_state="active",
        encryption_policy_generation=3,
        encryption_protocol=E2EE_PROTOCOL_MLS_10,
        encryption_suite=E2EE_SUITE_MLS_128,
        encryption_group_id="group-opaque-id",
        encryption_epoch=9,
    )
    incoming = validate_channel_encryption_policy(
        {
            "mode": "e2ee",
            "state": "proposed",
            "generation": "3",
            "protocol": E2EE_PROTOCOL_MLS_10,
            "suite": E2EE_SUITE_MLS_128,
            "group_id": "group-opaque-id",
            "epoch": "9",
        }
    )
    with pytest.raises(ValueError, match="equivocated"):
        validate_channel_encryption_policy_transition(channel, incoming, label="channel")


def test_database_migration_contains_message_policy_backstop() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations/versions/c82f4a1d6e90_e2ee_room_policy_guard.py"
    ).read_text()
    assert "channel_encryption_policy_consistent" in migration
    assert "CREATE TRIGGER trg_channels_encryption_transition" in migration
    assert "encrypted channel cannot be downgraded to plaintext" in migration
    assert "CREATE TRIGGER trg_messages_encryption_policy" in migration
    assert "plaintext body is forbidden in an encrypted channel" in migration
    assert "message encryption policy generation is stale" in migration


def test_plaintext_message_binds_absent_envelope_as_sql_null() -> None:
    """Prevent JSON `null` from violating the object-only DB constraint."""

    assert Message.__table__.c.e2ee.type.none_as_null is True


@pytest.mark.asyncio
async def test_local_e2ee_writes_require_an_active_device_owned_by_the_author() -> None:
    user = SimpleNamespace(id=7, origin_domain="alpha.localhost")
    session = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                user_id=7,
                user_domain="alpha.localhost",
                revoked_at=None,
            )
        )
    )
    await require_owned_e2ee_sender_device(
        session,
        user,
        {"sender_device_id": "ked_owned"},
        authority_domain="alpha.localhost",
    )

    for invalid in (
        None,
        SimpleNamespace(user_id=8, user_domain="alpha.localhost", revoked_at=None),
        SimpleNamespace(
            user_id=7,
            user_domain="alpha.localhost",
            revoked_at=datetime.now(UTC),
        ),
    ):
        session.get = AsyncMock(return_value=invalid)
        with pytest.raises(HTTPException) as caught:
            await require_owned_e2ee_sender_device(
                session,
                user,
                {"sender_device_id": "ked_invalid"},
                authority_domain="alpha.localhost",
            )
        assert caught.value.detail == {"code": "E2EE_SENDER_DEVICE_INVALID"}


def test_federated_message_replay_fingerprint_binds_opaque_ciphertext() -> None:
    shared = {
        "channel_id": 10,
        "channel_domain": "home.example",
        "author_id": 20,
        "author_domain": "home.example",
        "content": None,
        "message_type": 0,
        "flags": 0,
        "client_nonce": "nonce",
        "referenced_message_id": None,
        "referenced_message_domain": None,
        "mention_user_refs": [],
        "created_at": datetime(2026, 8, 12, tzinfo=UTC),
    }

    first = replicated_message_create_fingerprint(
        **shared,
        e2ee={"version": 1, "ciphertext": "first"},
    )
    equivocation = replicated_message_create_fingerprint(
        **shared,
        e2ee={"version": 1, "ciphertext": "changed"},
    )

    assert first != equivocation
