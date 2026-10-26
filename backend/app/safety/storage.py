from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.settings import Settings
from app.media.storage import S3Storage, StorageError

PREFIX = "evidence/"
MAGIC = b"kaede-safety-v1\x00"


def encrypt(settings: Settings, data: bytes, context: str) -> bytes:
    nonce = secrets.token_bytes(12)
    return (
        MAGIC
        + nonce
        + AESGCM(settings.safety_evidence_key_bytes).encrypt(nonce, data, context.encode())
    )


def decrypt(settings: Settings, data: bytes, context: str) -> bytes:
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 28:
        raise StorageError("invalid preserved evidence envelope")
    envelope = data[len(MAGIC) :]
    try:
        return AESGCM(settings.safety_evidence_key_bytes).decrypt(
            envelope[:12], envelope[12:], context.encode()
        )
    except InvalidTag as exc:
        raise StorageError("preserved evidence authentication failed") from exc


def encrypt_metadata(settings: Settings, value: dict[str, Any], context: str) -> bytes:
    return encrypt(settings, json.dumps(value, separators=(",", ":")).encode(), context)


def decrypt_metadata(settings: Settings, data: bytes, context: str) -> dict[str, Any]:
    value = json.loads(decrypt(settings, data, context))
    if not isinstance(value, dict):
        raise StorageError("invalid preserved evidence metadata")
    return value


class EvidenceStorage:
    """Ciphertext in the existing private bucket; never a presigned media capability."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = S3Storage(settings)
        self.bucket = settings.media_attachments_bucket

    def _check_key(self, key: str) -> None:
        if not key.startswith(PREFIX) or ".." in key.split("/"):
            raise ValueError("invalid evidence object key")

    async def put(self, key: str, data: bytes) -> None:
        self._check_key(key)
        encrypted = encrypt(self.settings, data, key)
        await self.storage.put(self.bucket, key, encrypted, "application/octet-stream")
        verified = await self.get(key, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        if verified != data:
            raise StorageError("preserved evidence verification failed")

    async def get(self, key: str, *, size: int, sha256: str) -> bytes:
        self._check_key(key)
        envelope = await self.storage.get(self.bucket, key, max_bytes=size + len(MAGIC) + 28)
        data = decrypt(self.settings, envelope, key)
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise StorageError("preserved evidence integrity check failed")
        return data

    async def delete(self, key: str) -> None:
        self._check_key(key)
        await self.storage.delete(self.bucket, key)
