from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.chat.e2ee import (
    MAX_E2EE_ARRAY_MEMBERS,
    MAX_E2EE_ENVELOPE_BYTES,
    MAX_E2EE_ENVELOPE_DEPTH,
    MAX_E2EE_ENVELOPE_NODES,
    MAX_E2EE_KEY_BYTES,
    validate_e2ee_envelope,
)
from app.chat.schemas import MessageCreate, MessageEdit
from app.core.federation import FEDERATION_CAPABILITIES, canonical_json
from app.core.json_limits import MAX_SAFE_JSON_INTEGER, strict_json_loads
from app.db.models import Message
from app.federation.replication import replicated_message_create_fingerprint
from app.federation.schemas import EventEnvelope


def test_e2ee_envelope_is_opaque_versioned_and_bounded() -> None:
    envelope = {"version": 1, "suite": "future-suite", "ciphertext": "opaque"}
    assert validate_e2ee_envelope(envelope) == envelope
    with pytest.raises(ValueError, match="positive version"):
        validate_e2ee_envelope({"version": 0, "ciphertext": "opaque"})
    with pytest.raises(ValueError, match="too large"):
        validate_e2ee_envelope({"version": 1, "ciphertext": "x" * MAX_E2EE_ENVELOPE_BYTES})


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


def test_federation_advertises_transport_only_e2ee_capability() -> None:
    assert "e2ee-transport/1" in FEDERATION_CAPABILITIES
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
    with pytest.raises(ValueError, match="plaintext and encrypted"):
        MessageCreate(content="visible", e2ee=envelope)
    with pytest.raises(ValueError, match="plaintext or encrypted"):
        MessageEdit(content="visible", e2ee=envelope)


def test_plaintext_message_binds_absent_envelope_as_sql_null() -> None:
    """Prevent JSON `null` from violating the object-only DB constraint."""

    assert Message.__table__.c.e2ee.type.none_as_null is True


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
