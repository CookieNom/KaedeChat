import pytest

from app.chat.e2ee import MAX_E2EE_ENVELOPE_BYTES, validate_e2ee_envelope
from app.chat.schemas import MessageCreate, MessageEdit
from app.db.models import Message


def test_e2ee_envelope_is_opaque_versioned_and_bounded() -> None:
    envelope = {"version": 1, "suite": "future-suite", "ciphertext": "opaque"}
    assert validate_e2ee_envelope(envelope) == envelope
    with pytest.raises(ValueError, match="positive version"):
        validate_e2ee_envelope({"version": 0, "ciphertext": "opaque"})
    with pytest.raises(ValueError, match="too large"):
        validate_e2ee_envelope({"version": 1, "ciphertext": "x" * MAX_E2EE_ENVELOPE_BYTES})


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
