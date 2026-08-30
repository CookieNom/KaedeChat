from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import kaede_bot.e2ee as e2ee
from kaede_bot.e2ee import E2EEProtocolError, decrypt_file, encrypt_file


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_python_matches_shared_kaede_file_v1_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "frontend/static/protocol/kaede-file-v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    entropy = fixture["entropy"]
    material = iter(
        decode(entropy[field]) for field in ("file_id", "key", "salt", "nonce_prefix")
    )
    monkeypatch.setattr(e2ee.secrets, "token_bytes", lambda _size: next(material))
    plaintext = decode(fixture["plaintext_base64url"])

    encrypted = encrypt_file(
        plaintext,
        filename="hello.txt",
        content_type="text/plain",
        chunk_size=64 * 1024,
    )

    assert encrypted.manifest == fixture["manifest"]
    assert e2ee._b64(encrypted.ciphertext) == fixture["ciphertext_base64url"]  # noqa: SLF001
    assert decrypt_file(encrypted.ciphertext, encrypted.manifest) == plaintext


def test_kaede_file_v1_rejects_ciphertext_and_manifest_tampering() -> None:
    encrypted = encrypt_file(
        b"private attachment",
        filename="report.pdf",
        content_type="application/pdf",
        chunk_size=64 * 1024,
    )
    tampered = bytearray(encrypted.ciphertext)
    tampered[-1] ^= 1
    with pytest.raises(E2EEProtocolError, match="modified"):
        decrypt_file(bytes(tampered), encrypted.manifest)
    with pytest.raises(E2EEProtocolError, match="manifest"):
        decrypt_file(
            encrypted.ciphertext,
            dict(encrypted.manifest, plaintext_size=1),
        )
    with pytest.raises(E2EEProtocolError, match="plaintext digest"):
        decrypt_file(
            encrypted.ciphertext,
            dict(
                encrypted.manifest,
                plaintext_sha256=base64.urlsafe_b64encode(b"x" * 32)
                .rstrip(b"=")
                .decode(),
            ),
        )
