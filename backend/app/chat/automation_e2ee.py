from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime

from app.core.base64url import decode_base64url, encode_base64url  # noqa: F401


def automation_device_protocol_id(
    *,
    namespace: str,
    prefix: str,
    principal_ref: str,
    identity_key: bytes,
) -> str:
    """Derive one stable, namespace-separated MLS automation device ID."""

    digest = hashlib.sha256(f"{namespace}\0{principal_ref}\0".encode() + identity_key).digest()
    return prefix + encode_base64url(digest)


def automation_mls_credential(
    *,
    account: str,
    credential_type: str,
    device_id: str,
    lineage: dict[str, str],
) -> bytes:
    return json.dumps(
        {
            "account": account,
            "credential_type": credential_type,
            "device_id": device_id,
            **lineage,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def automation_device_registration_input(
    *,
    namespace: str,
    principal_ref: str,
    identity_key: bytes,
    credential_digest: bytes,
    challenge: bytes,
    lineage: Iterable[str] = (),
) -> bytes:
    return b"\n".join(
        (
            namespace.encode(),
            principal_ref.encode(),
            *(item.encode() for item in lineage),
            encode_base64url(identity_key).encode(),
            encode_base64url(credential_digest).encode(),
            encode_base64url(challenge).encode(),
        )
    )


def automation_key_package_upload_input(
    *,
    namespace: str,
    protocol_id: str,
    generation: int,
    cipher_suite: str,
    expires_at: datetime,
    package_hashes: Iterable[bytes],
) -> bytes:
    hashes = sorted(encode_base64url(item) for item in package_hashes)
    return b"\n".join(
        (
            namespace.encode(),
            protocol_id.encode(),
            str(generation).encode(),
            cipher_suite.encode(),
            expires_at.astimezone(UTC).isoformat().encode(),
            ",".join(hashes).encode(),
        )
    )
