from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NoReturn,
    NotRequired,
    Protocol,
    Self,
    TypedDict,
    cast,
    runtime_checkable,
)

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ._encoding import encode_base64url as _b64
from .refs import EntityRef, canonical_federation_domain

if TYPE_CHECKING:
    from .models import Attachment, Interaction, Message

MAX_NATIVE_INPUT_BYTES = 64 * 1024 * 1024
MAX_MLS_MESSAGE_BYTES = 64 * 1024
MAX_KEY_PACKAGE_BYTES = 32 * 1024
MAX_CREDENTIAL_BYTES = 16 * 1024
MAX_EXPORTER_CONTEXT_BYTES = 4096
MLS_PROTOCOL = "mls10"
MLS_SUITE = "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519"
INTERACTION_AAD_PURPOSE = "kaede.interaction.v1"
INTERACTION_RESPONSE_AAD_PURPOSE = "kaede.interaction.response.v1"
MESSAGE_RICH_AAD_PURPOSE = "kaede.message.rich.v1"
INTERACTION_ROUTING_CONTRACT_VERSION = 1
MAX_INTERACTION_PLAINTEXT_BYTES = 64 * 1024
MAX_INTERACTION_REPLAY_ENTRIES = 2048
MAX_ENCRYPTED_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_ENCRYPTED_FILE_CHUNK_SIZE = 256 * 1024
ENCRYPTED_FILE_HEADER_SIZE = 41
ENCRYPTED_FILE_MAGIC = b"KAEF"
ENCRYPTED_FILE_INFO = b"kaede attachment content v1"
HUMAN_DEVICE_ID_RE = re.compile(r"ked_[A-Za-z0-9_-]{43}")
CONTENT_TYPE_RE = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+")
BOT_E2EE_CAPABILITIES = frozenset({"e2ee-mls/1", "e2ee-media/1"})
BOT_E2EE_DEVICE_ID_RE = re.compile(r"kbe_[A-Za-z0-9_-]{43}")
BOT_E2EE_CHALLENGE_ID_RE = re.compile(r"kbec_[A-Za-z0-9_-]{32}")
WEBHOOK_E2EE_DEVICE_ID_RE = re.compile(r"kwe_[A-Za-z0-9_-]{43}")
WEBHOOK_E2EE_CHALLENGE_ID_RE = re.compile(r"kwec_[A-Za-z0-9_-]{32}")
CUSTOM_EMOJI_ROUTING_RE = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):"
    r"(?P<id>[1-9][0-9]{0,18})@(?P<domain>[A-Za-z0-9.-]{1,253})>"
)
QUALIFIED_USER_MENTION_RE = re.compile(
    r"<@(?P<id>[1-9][0-9]{0,18})@(?P<domain>[a-z0-9.-]{1,253})>",
    re.IGNORECASE,
)
UNQUALIFIED_USER_MENTION_RE = re.compile(r"<@[1-9][0-9]{0,18}>")
QUALIFIED_ROLE_MENTION_RE = re.compile(
    r"<@&(?P<id>[1-9][0-9]{0,18})@(?P<domain>[a-z0-9.-]{1,253})>",
    re.IGNORECASE,
)
BROAD_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@(?:everyone|here)\b", re.IGNORECASE)


class E2EEUnavailableError(RuntimeError):
    """The audited OpenMLS provider is unavailable on this system."""


class E2EEProtocolError(RuntimeError):
    """The native provider rejected an MLS operation or returned invalid data."""


class EncryptedFileManifest(TypedDict):
    version: Literal[1]
    protocol: Literal["kaede-file-v1"]
    file_id: str
    key: str
    filename: str
    content_type: str
    plaintext_size: int
    ciphertext_size: int
    ciphertext_sha256: str
    plaintext_sha256: str
    chunk_size: int
    attachment_id: NotRequired[str]
    attachment_domain: NotRequired[str]


@dataclass(frozen=True, slots=True)
class EncryptedFile:
    """A canonical kaede-file-v1 ciphertext and its authenticated manifest."""

    ciphertext: bytes
    manifest: EncryptedFileManifest


def _wire_int(
    value: object,
    field_name: str,
    *,
    minimum: int = 0,
    maximum: int = (1 << 63) - 1,
) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise E2EEProtocolError(f"{field_name} is not a canonical integer")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise E2EEProtocolError(f"{field_name} is outside its valid range")
    return parsed


def _wire_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise E2EEProtocolError(f"{field_name} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise E2EEProtocolError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise E2EEProtocolError(f"{field_name} requires a timezone")
    return parsed.astimezone(UTC)


def _wire_b64(
    value: object,
    field_name: str,
    *,
    maximum: int,
    exact: int | None = None,
) -> bytes:
    if not isinstance(value, str):
        raise E2EEProtocolError(f"{field_name} is missing")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, UnicodeEncodeError) as exc:
        raise E2EEProtocolError(f"{field_name} is invalid") from exc
    if (
        not decoded
        or len(decoded) > maximum
        or (exact is not None and len(decoded) != exact)
        or _b64(decoded) != value
    ):
        raise E2EEProtocolError(f"{field_name} is invalid")
    return decoded


def _payload_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise E2EEProtocolError(f"{label} response is invalid")
    return cast(Mapping[str, object], value)


@dataclass(frozen=True, slots=True)
class BotE2EEDeviceChallenge:
    challenge_id: str
    signing_input: bytes
    expires_in: int
    application_ref: EntityRef
    worker_id: int
    domain: str

    @classmethod
    def from_payload(cls, payload: object) -> BotE2EEDeviceChallenge:
        raw = _payload_mapping(payload, "bot E2EE challenge")
        challenge_id = raw.get("challenge_id")
        if (
            not isinstance(challenge_id, str)
            or BOT_E2EE_CHALLENGE_ID_RE.fullmatch(challenge_id) is None
        ):
            raise E2EEProtocolError("bot E2EE challenge ID is invalid")
        try:
            application_ref = EntityRef.parse(raw.get("application_ref"))
        except ValueError as exc:
            raise E2EEProtocolError(
                "bot E2EE challenge application reference is invalid"
            ) from exc
        expires_in = raw.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not 1 <= expires_in <= 3600
        ):
            raise E2EEProtocolError("bot E2EE challenge expiry is invalid")
        domain = raw.get("domain")
        if not isinstance(domain, str) or domain != application_ref.domain:
            raise E2EEProtocolError("bot E2EE challenge authority is invalid")
        return cls(
            challenge_id=challenge_id,
            signing_input=_wire_b64(
                raw.get("signing_input"),
                "bot E2EE challenge signing input",
                maximum=1024,
            ),
            expires_in=expires_in,
            application_ref=application_ref,
            worker_id=_wire_int(raw.get("worker_id"), "bot E2EE challenge worker ID"),
            domain=domain,
        )


@dataclass(frozen=True, slots=True)
class BotE2EEDevice:
    source_ref: EntityRef
    protocol_id: str
    worker_id: int
    identity_key: bytes
    credential: bytes
    capabilities: frozenset[str]
    generation: int
    available_key_packages: int = 0

    @classmethod
    def from_payload(cls, payload: object) -> BotE2EEDevice:
        raw = _payload_mapping(payload, "bot E2EE device")
        protocol_id = raw.get("protocol_id")
        if (
            not isinstance(protocol_id, str)
            or BOT_E2EE_DEVICE_ID_RE.fullmatch(protocol_id) is None
        ):
            raise E2EEProtocolError("bot E2EE device ID is invalid")
        try:
            source_ref = EntityRef.from_wire(
                raw.get("source_id"), raw.get("source_domain")
            )
        except ValueError as exc:
            raise E2EEProtocolError("bot E2EE device authority is invalid") from exc
        capabilities = raw.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(item, str) for item in capabilities)
            or len(capabilities) != len(set(capabilities))
            or not set(capabilities) <= BOT_E2EE_CAPABILITIES
            or "e2ee-mls/1" not in capabilities
        ):
            raise E2EEProtocolError("bot E2EE device capabilities are invalid")
        if raw.get("trust_state", "trusted") != "trusted":
            raise E2EEProtocolError("bot E2EE device is not trusted")
        available = raw.get("available_key_packages", 0)
        if (
            isinstance(available, bool)
            or not isinstance(available, int)
            or not 0 <= available <= 1000
        ):
            raise E2EEProtocolError("bot E2EE key-package inventory is invalid")
        worker_id = _wire_int(
            raw.get("worker_id"), "bot E2EE device worker ID", minimum=1
        )
        identity_key = _wire_b64(
            raw.get("identity_key"), "bot E2EE identity key", maximum=32, exact=32
        )
        credential = _wire_b64(
            raw.get("credential"),
            "bot E2EE credential",
            maximum=MAX_CREDENTIAL_BYTES,
        )
        try:
            credential_payload = json.loads(credential)
            application_ref = EntityRef.parse(credential_payload["application_ref"])
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise E2EEProtocolError("bot E2EE credential is invalid") from exc
        expected_protocol_id = bot_device_protocol_id(
            application_ref,
            worker_id,
            identity_key,
        )
        expected_credential = bot_mls_credential(
            application_ref,
            worker_id,
            identity_key,
        )
        if protocol_id != expected_protocol_id or not hmac.compare_digest(
            credential, expected_credential
        ):
            raise E2EEProtocolError("bot E2EE credential identity is invalid")
        return cls(
            source_ref=source_ref,
            protocol_id=protocol_id,
            worker_id=worker_id,
            identity_key=identity_key,
            credential=credential,
            capabilities=frozenset(cast(list[str], capabilities)),
            generation=_wire_int(
                raw.get("generation"), "bot E2EE device generation", minimum=1
            ),
            available_key_packages=available,
        )


@dataclass(frozen=True, slots=True)
class BotE2EEDeviceInventory:
    generation: int
    devices: tuple[BotE2EEDevice, ...]

    @classmethod
    def from_payload(cls, payload: object) -> BotE2EEDeviceInventory:
        raw = _payload_mapping(payload, "bot E2EE device inventory")
        devices = raw.get("devices")
        if not isinstance(devices, list) or len(devices) > 50:
            raise E2EEProtocolError("bot E2EE device inventory is invalid")
        parsed = tuple(BotE2EEDevice.from_payload(item) for item in devices)
        if len({item.protocol_id for item in parsed}) != len(parsed):
            raise E2EEProtocolError("bot E2EE device inventory has duplicate devices")
        return cls(
            generation=_wire_int(
                raw.get("generation"), "bot E2EE inventory generation", minimum=1
            ),
            devices=parsed,
        )


@dataclass(frozen=True, slots=True)
class BotE2EEKeyPackageResult:
    device_id: str
    accepted: int
    available_key_packages: int

    @classmethod
    def from_payload(cls, payload: object) -> BotE2EEKeyPackageResult:
        raw = _payload_mapping(payload, "bot E2EE key-package upload")
        device_id = raw.get("device_id")
        if (
            not isinstance(device_id, str)
            or BOT_E2EE_DEVICE_ID_RE.fullmatch(device_id) is None
        ):
            raise E2EEProtocolError("bot E2EE key-package device ID is invalid")
        values: list[int] = []
        for name in ("accepted", "available_key_packages"):
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise E2EEProtocolError(f"bot E2EE key-package {name} is invalid")
            values.append(value)
        return cls(device_id, values[0], values[1])


def webhook_device_protocol_id(webhook_ref: EntityRef, identity_key: bytes) -> str:
    """Return the authority-stable device ID for one incoming webhook."""

    if len(identity_key) != 32:
        raise ValueError("webhook device identity is invalid")
    digest = hashlib.sha256(
        f"kaede-webhook-e2ee-device-v1\0{webhook_ref}\0".encode() + identity_key
    ).digest()
    return "kwe_" + _b64(digest)


def webhook_mls_credential(webhook_ref: EntityRef, identity_key: bytes) -> bytes:
    """Canonical MLS BasicCredential for a token-scoped automation device."""

    device_id = webhook_device_protocol_id(webhook_ref, identity_key)
    return json.dumps(
        {
            "account": f"webhook:{webhook_ref}",
            "credential_type": "kaede-webhook-device-v1",
            "device_id": device_id,
            "webhook_ref": str(webhook_ref),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def webhook_key_package_upload_input(
    *,
    protocol_id: str,
    generation: int,
    cipher_suite: str,
    expires_at: datetime,
    package_hashes: Iterable[bytes],
) -> bytes:
    """Canonical signature input accepted by the webhook package endpoint."""

    if WEBHOOK_E2EE_DEVICE_ID_RE.fullmatch(protocol_id) is None:
        raise ValueError("webhook E2EE device ID is invalid")
    if generation < 1:
        raise ValueError("webhook E2EE device generation must be positive")
    if cipher_suite != MLS_SUITE:
        raise ValueError("unsupported MLS cipher suite")
    if expires_at.tzinfo is None:
        raise ValueError("key-package expiry requires a timezone")
    hashes = sorted(_b64(item) for item in package_hashes)
    if not hashes or any(len(item) != 43 for item in hashes):
        raise ValueError("key-package hashes are invalid")
    return b"\n".join(
        (
            b"kaede-webhook-e2ee-key-packages-v1",
            protocol_id.encode(),
            str(generation).encode(),
            cipher_suite.encode(),
            expires_at.astimezone(UTC).isoformat().encode(),
            ",".join(hashes).encode(),
        )
    )


@dataclass(frozen=True, slots=True)
class WebhookE2EEDeviceChallenge:
    challenge_id: str
    signing_input: bytes
    expires_in: int
    webhook_ref: EntityRef

    @classmethod
    def from_payload(cls, payload: object) -> WebhookE2EEDeviceChallenge:
        raw = _payload_mapping(payload, "webhook E2EE challenge")
        challenge_id = raw.get("challenge_id")
        if (
            not isinstance(challenge_id, str)
            or WEBHOOK_E2EE_CHALLENGE_ID_RE.fullmatch(challenge_id) is None
        ):
            raise E2EEProtocolError("webhook E2EE challenge ID is invalid")
        try:
            webhook_ref = EntityRef.parse(raw.get("webhook_ref"))
        except ValueError as exc:
            raise E2EEProtocolError(
                "webhook E2EE challenge identity is invalid"
            ) from exc
        expires_in = raw.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not 1 <= expires_in <= 3600
        ):
            raise E2EEProtocolError("webhook E2EE challenge expiry is invalid")
        return cls(
            challenge_id,
            _wire_b64(
                raw.get("signing_input"),
                "webhook E2EE challenge signing input",
                maximum=1024,
            ),
            expires_in,
            webhook_ref,
        )


@dataclass(frozen=True, slots=True)
class WebhookE2EEDevice:
    webhook_ref: EntityRef
    author_ref: EntityRef
    protocol_id: str
    identity_key: bytes
    credential: bytes
    capabilities: frozenset[str]
    generation: int
    available_key_packages: int = 0

    @classmethod
    def from_payload(cls, payload: object) -> WebhookE2EEDevice:
        raw = _payload_mapping(payload, "webhook E2EE device")
        try:
            webhook_ref = EntityRef.parse(raw.get("webhook_ref"))
            author_ref = EntityRef.parse(raw.get("author_ref"))
        except ValueError as exc:
            raise E2EEProtocolError("webhook E2EE device identity is invalid") from exc
        protocol_id = raw.get("device_id")
        capabilities = raw.get("capabilities")
        available = raw.get("available_key_packages", 0)
        if (
            not isinstance(protocol_id, str)
            or WEBHOOK_E2EE_DEVICE_ID_RE.fullmatch(protocol_id) is None
            or not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(item, str) for item in capabilities)
            or len(capabilities) != len(set(capabilities))
            or not set(capabilities) <= BOT_E2EE_CAPABILITIES
            or "e2ee-mls/1" not in capabilities
            or raw.get("trust_state", "trusted") != "trusted"
            or isinstance(available, bool)
            or not isinstance(available, int)
            or not 0 <= available <= 1000
        ):
            raise E2EEProtocolError("webhook E2EE device response is invalid")
        identity_key = _wire_b64(
            raw.get("identity_key"), "webhook E2EE identity key", maximum=32, exact=32
        )
        credential = _wire_b64(
            raw.get("credential"),
            "webhook E2EE credential",
            maximum=MAX_CREDENTIAL_BYTES,
        )
        if protocol_id != webhook_device_protocol_id(
            webhook_ref, identity_key
        ) or not hmac.compare_digest(
            credential, webhook_mls_credential(webhook_ref, identity_key)
        ):
            raise E2EEProtocolError("webhook E2EE credential identity is invalid")
        return cls(
            webhook_ref,
            author_ref,
            protocol_id,
            identity_key,
            credential,
            frozenset(cast(list[str], capabilities)),
            _wire_int(raw.get("generation"), "webhook E2EE generation", minimum=1),
            available,
        )


@dataclass(frozen=True, slots=True)
class WebhookE2EEDeviceInventory:
    webhook_ref: EntityRef
    devices: tuple[WebhookE2EEDevice, ...]

    @classmethod
    def from_payload(cls, payload: object) -> WebhookE2EEDeviceInventory:
        raw = _payload_mapping(payload, "webhook E2EE inventory")
        try:
            webhook_ref = EntityRef.parse(raw.get("webhook_ref"))
        except ValueError as exc:
            raise E2EEProtocolError(
                "webhook E2EE inventory identity is invalid"
            ) from exc
        devices = raw.get("devices")
        if not isinstance(devices, list) or len(devices) > 50:
            raise E2EEProtocolError("webhook E2EE inventory is invalid")
        parsed = tuple(WebhookE2EEDevice.from_payload(item) for item in devices)
        if any(item.webhook_ref != webhook_ref for item in parsed) or len(
            {item.protocol_id for item in parsed}
        ) != len(parsed):
            raise E2EEProtocolError("webhook E2EE inventory lineage is invalid")
        return cls(webhook_ref, parsed)


@dataclass(frozen=True, slots=True)
class WebhookE2EEKeyPackageResult:
    device_id: str
    accepted: int
    available_key_packages: int

    @classmethod
    def from_payload(cls, payload: object) -> WebhookE2EEKeyPackageResult:
        raw = _payload_mapping(payload, "webhook E2EE key-package upload")
        device_id = raw.get("device_id")
        if (
            not isinstance(device_id, str)
            or WEBHOOK_E2EE_DEVICE_ID_RE.fullmatch(device_id) is None
        ):
            raise E2EEProtocolError("webhook E2EE key-package device ID is invalid")
        values: list[int] = []
        for name in ("accepted", "available_key_packages"):
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise E2EEProtocolError(f"webhook E2EE key-package {name} is invalid")
            values.append(value)
        return cls(device_id, values[0], values[1])


@dataclass(frozen=True, slots=True)
class WebhookE2EEForumKeyPackage:
    """One authority-claimed MLS member package for a webhook-created forum room."""

    principal_ref: EntityRef
    device_id: str
    identity_key: bytes
    credential: bytes
    key_package: bytes

    @classmethod
    def from_payload(cls, payload: object) -> WebhookE2EEForumKeyPackage:
        raw = _payload_mapping(payload, "webhook forum MLS KeyPackage")
        if set(raw) != {
            "user_id",
            "user_domain",
            "device_id",
            "identity_key",
            "credential",
            "key_package",
        }:
            raise E2EEProtocolError("webhook forum MLS KeyPackage shape is invalid")
        try:
            principal_ref = EntityRef.from_wire(
                raw.get("user_id"), raw.get("user_domain")
            )
        except ValueError as exc:
            raise E2EEProtocolError(
                "webhook forum MLS principal identity is invalid"
            ) from exc
        device_id = raw.get("device_id")
        if not isinstance(device_id, str) or not any(
            pattern.fullmatch(device_id) is not None
            for pattern in (
                HUMAN_DEVICE_ID_RE,
                BOT_E2EE_DEVICE_ID_RE,
                WEBHOOK_E2EE_DEVICE_ID_RE,
            )
        ):
            raise E2EEProtocolError("webhook forum MLS device identity is invalid")
        return cls(
            principal_ref=principal_ref,
            device_id=device_id,
            identity_key=_wire_b64(
                raw.get("identity_key"),
                "webhook forum MLS identity key",
                maximum=32,
                exact=32,
            ),
            credential=_wire_b64(
                raw.get("credential"),
                "webhook forum MLS credential",
                maximum=MAX_CREDENTIAL_BYTES,
            ),
            key_package=_wire_b64(
                raw.get("key_package"),
                "webhook forum MLS KeyPackage",
                maximum=MAX_KEY_PACKAGE_BYTES,
            ),
        )

    def verify(self, provider: E2EEProvider) -> None:
        """Bind the opaque KeyPackage to its advertised MLS credential and key."""

        credential, signature_key = provider.inspect_key_package(self.key_package)
        if not hmac.compare_digest(
            credential, self.credential
        ) or not hmac.compare_digest(signature_key, self.identity_key):
            raise E2EEProtocolError("webhook forum MLS KeyPackage was substituted")
        if self.device_id.startswith("ked_"):
            _validate_human_credential(self.credential, self.principal_ref)
            return
        try:
            parsed = json.loads(self.credential)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise E2EEProtocolError(
                "webhook forum MLS automation credential is invalid"
            ) from exc
        if self.device_id.startswith("kwe_"):
            expected = webhook_mls_credential(self.principal_ref, self.identity_key)
            if not hmac.compare_digest(self.credential, expected):
                raise E2EEProtocolError(
                    "webhook forum MLS webhook credential is invalid"
                )
            return
        if not isinstance(parsed, dict) or set(parsed) != {
            "account",
            "application_ref",
            "credential_type",
            "device_id",
            "worker_id",
        }:
            raise E2EEProtocolError("webhook forum MLS bot credential is invalid")
        try:
            application_ref = EntityRef.parse(parsed["application_ref"])
            worker_id = _wire_int(
                parsed.get("worker_id"),
                "webhook forum MLS bot worker ID",
                minimum=1,
            )
        except (KeyError, ValueError) as exc:
            raise E2EEProtocolError(
                "webhook forum MLS bot credential is invalid"
            ) from exc
        if (
            parsed.get("credential_type") != "kaede-bot-device-v2"
            or parsed.get("device_id") != self.device_id
            or parsed.get("account") != f"bot:{application_ref}:worker:{worker_id}"
            or self.device_id
            != bot_device_protocol_id(application_ref, worker_id, self.identity_key)
        ):
            raise E2EEProtocolError("webhook forum MLS bot credential is invalid")


@dataclass(frozen=True, slots=True)
class WebhookE2EEForumProposal:
    operation_id: str
    policy_generation: int
    group_id: bytes
    key_packages: tuple[WebhookE2EEForumKeyPackage, ...]

    @classmethod
    def from_payload(cls, payload: object) -> WebhookE2EEForumProposal:
        raw = _payload_mapping(payload, "webhook forum MLS proposal")
        operation_id = raw.get("operation_id")
        policy = _payload_mapping(raw.get("policy"), "webhook forum MLS policy")
        packages = raw.get("key_packages")
        if (
            set(raw) != {"operation_id", "status", "policy", "key_packages"}
            or not isinstance(operation_id, str)
            or re.fullmatch(r"keo_[A-Za-z0-9_-]{43}", operation_id) is None
            or raw.get("status") != "prepared"
            or set(policy)
            != {"mode", "state", "generation", "protocol", "suite", "group_id", "epoch"}
            or policy.get("mode") != "plaintext"
            or policy.get("state") != "proposed"
            or policy.get("protocol") != MLS_PROTOCOL
            or policy.get("suite") != MLS_SUITE
            or policy.get("epoch") is not None
            or not isinstance(packages, list)
            or not 1 <= len(packages) <= 48
        ):
            raise E2EEProtocolError("webhook forum MLS proposal is invalid")
        parsed_packages = tuple(
            WebhookE2EEForumKeyPackage.from_payload(item) for item in packages
        )
        if len({item.device_id for item in parsed_packages}) != len(parsed_packages):
            raise E2EEProtocolError("webhook forum MLS proposal repeats a device")
        return cls(
            operation_id=operation_id,
            policy_generation=_wire_int(
                policy.get("generation"),
                "webhook forum MLS policy generation",
                minimum=1,
            ),
            group_id=_wire_b64(
                policy.get("group_id"),
                "webhook forum MLS group ID",
                maximum=32,
                exact=32,
            ),
            key_packages=parsed_packages,
        )


@dataclass(frozen=True, slots=True)
class BotE2EEParticipationDevice:
    device_id: str
    status: Literal["pending", "active", "revoked"]
    consent_generation: int
    joined_epoch: int
    history_floor_message_ref: EntityRef | None


@dataclass(frozen=True, slots=True)
class BotE2EEParticipationStatus:
    application_ref: EntityRef
    channel_ref: EntityRef
    e2ee_mode: Literal["participant", "disabled"]
    devices: tuple[BotE2EEParticipationDevice, ...]
    encryption_policy: Mapping[str, object]

    @classmethod
    def from_payload(cls, payload: object) -> BotE2EEParticipationStatus:
        raw = _payload_mapping(payload, "bot E2EE participation")
        try:
            application_ref = EntityRef.parse(raw.get("application_ref"))
            channel_ref = EntityRef.parse(raw.get("channel_ref"))
        except ValueError as exc:
            raise E2EEProtocolError(
                "bot E2EE participation identity is invalid"
            ) from exc
        mode = raw.get("e2ee_mode")
        if mode not in {"participant", "disabled"}:
            raise E2EEProtocolError("bot E2EE participation mode is invalid")
        raw_devices = raw.get("devices")
        if not isinstance(raw_devices, list) or len(raw_devices) > 50:
            raise E2EEProtocolError("bot E2EE participation devices are invalid")
        devices: list[BotE2EEParticipationDevice] = []
        for item in raw_devices:
            device = _payload_mapping(item, "bot E2EE participation device")
            device_id = device.get("device_id")
            status = device.get("status")
            if (
                not isinstance(device_id, str)
                or BOT_E2EE_DEVICE_ID_RE.fullmatch(device_id) is None
                or status not in {"pending", "active", "revoked"}
            ):
                raise E2EEProtocolError("bot E2EE participation device is invalid")
            floor_raw = device.get("history_floor_message_ref")
            try:
                floor = EntityRef.parse(floor_raw) if floor_raw is not None else None
            except ValueError as exc:
                raise E2EEProtocolError(
                    "bot E2EE participation history floor is invalid"
                ) from exc
            devices.append(
                BotE2EEParticipationDevice(
                    device_id=device_id,
                    status=cast(Literal["pending", "active", "revoked"], status),
                    consent_generation=_wire_int(
                        device.get("consent_generation"),
                        "bot E2EE consent generation",
                        minimum=1,
                    ),
                    joined_epoch=_wire_int(
                        device.get("joined_epoch"),
                        "bot E2EE joined epoch",
                    ),
                    history_floor_message_ref=floor,
                )
            )
        policy = _payload_mapping(raw.get("encryption_policy"), "encryption policy")
        return cls(
            application_ref=application_ref,
            channel_ref=channel_ref,
            e2ee_mode=cast(Literal["participant", "disabled"], mode),
            devices=tuple(devices),
            encryption_policy=policy,
        )


@dataclass(frozen=True, slots=True)
class WebhookE2EEParticipationStatus:
    webhook_ref: EntityRef
    channel_ref: EntityRef
    devices: tuple[BotE2EEParticipationDevice, ...]
    encryption_policy: Mapping[str, object]

    @classmethod
    def from_payload(cls, payload: object) -> WebhookE2EEParticipationStatus:
        raw = _payload_mapping(payload, "webhook E2EE participation")
        try:
            webhook_ref = EntityRef.parse(raw.get("webhook_ref"))
            channel_ref = EntityRef.parse(raw.get("channel_ref"))
        except ValueError as exc:
            raise E2EEProtocolError(
                "webhook E2EE participation identity is invalid"
            ) from exc
        raw_devices = raw.get("devices")
        if not isinstance(raw_devices, list) or len(raw_devices) > 50:
            raise E2EEProtocolError("webhook E2EE participation devices are invalid")
        devices: list[BotE2EEParticipationDevice] = []
        for item in raw_devices:
            device = _payload_mapping(item, "webhook E2EE participation device")
            device_id = device.get("device_id")
            device_status = device.get("status")
            if (
                not isinstance(device_id, str)
                or WEBHOOK_E2EE_DEVICE_ID_RE.fullmatch(device_id) is None
                or device_status not in {"pending", "active", "revoked"}
            ):
                raise E2EEProtocolError("webhook E2EE participation device is invalid")
            floor_raw = device.get("history_floor_message_ref")
            try:
                floor = EntityRef.parse(floor_raw) if floor_raw is not None else None
            except ValueError as exc:
                raise E2EEProtocolError(
                    "webhook E2EE participation history floor is invalid"
                ) from exc
            devices.append(
                BotE2EEParticipationDevice(
                    device_id,
                    cast(Literal["pending", "active", "revoked"], device_status),
                    _wire_int(
                        device.get("consent_generation"),
                        "webhook E2EE consent generation",
                        minimum=1,
                    ),
                    _wire_int(device.get("joined_epoch"), "webhook E2EE joined epoch"),
                    floor,
                )
            )
        return cls(
            webhook_ref,
            channel_ref,
            tuple(devices),
            _payload_mapping(raw.get("encryption_policy"), "encryption policy"),
        )


@dataclass(frozen=True, slots=True)
class BotE2EEControlRecord:
    """One authority-bound Welcome or Commit from the durable control log."""

    ref: EntityRef
    channel_ref: EntityRef
    author_ref: EntityRef
    envelope: Mapping[str, object]
    policy_generation: int
    epoch: int
    apply: bool
    room_operation_id: str
    room_operation_domain: str

    @classmethod
    def from_payload(cls, payload: object) -> BotE2EEControlRecord:
        raw = _payload_mapping(payload, "bot E2EE control")
        try:
            ref = EntityRef.from_wire(raw.get("id"), raw.get("origin_domain"))
            channel_ref = EntityRef.from_wire(
                raw.get("channel_id"), raw.get("channel_domain")
            )
            author_ref = EntityRef.from_wire(
                raw.get("author_id"), raw.get("author_domain")
            )
            room_operation_authority = canonical_federation_domain(
                raw.get("room_operation_domain")
            )
        except ValueError as exc:
            raise E2EEProtocolError("bot E2EE control identity is invalid") from exc
        operation_id = raw.get("room_operation_id")
        if (
            ref.domain != channel_ref.domain
            or not isinstance(operation_id, str)
            or re.fullmatch(r"keo_[A-Za-z0-9_-]{43}", operation_id) is None
            or room_operation_authority != channel_ref.domain
        ):
            raise E2EEProtocolError("bot E2EE control authority is invalid")
        envelope = _payload_mapping(raw.get("e2ee"), "bot E2EE control envelope")
        operation = envelope.get("operation")
        apply = raw.get("apply")
        policy_generation = _wire_int(
            raw.get("encryption_policy_generation"),
            "bot E2EE control policy generation",
            minimum=1,
        )
        epoch = _wire_int(raw.get("encryption_epoch"), "bot E2EE control epoch")
        if (
            envelope.get("version") != 2
            or envelope.get("protocol") != MLS_PROTOCOL
            or envelope.get("suite") != MLS_SUITE
            or operation not in {"welcome", "commit"}
            or not isinstance(apply, bool)
            or (operation == "welcome" and not apply)
            or envelope.get("policy_generation") != str(policy_generation)
            or envelope.get("epoch") != str(epoch)
            or not isinstance(envelope.get("sender_device_id"), str)
            or (
                HUMAN_DEVICE_ID_RE.fullmatch(str(envelope.get("sender_device_id")))
                is None
                and BOT_E2EE_DEVICE_ID_RE.fullmatch(
                    str(envelope.get("sender_device_id"))
                )
                is None
                and WEBHOOK_E2EE_DEVICE_ID_RE.fullmatch(
                    str(envelope.get("sender_device_id"))
                )
                is None
            )
        ):
            raise E2EEProtocolError("bot E2EE control envelope is invalid")
        _wire_b64(envelope.get("group_id"), "MLS group ID", maximum=128)
        _wire_b64(
            envelope.get("ciphertext"),
            "MLS control ciphertext",
            maximum=MAX_MLS_MESSAGE_BYTES,
        )
        return cls(
            ref=ref,
            channel_ref=channel_ref,
            author_ref=author_ref,
            envelope=envelope,
            policy_generation=policy_generation,
            epoch=epoch,
            apply=apply,
            room_operation_id=operation_id,
            room_operation_domain=room_operation_authority,
        )

    @property
    def cursor(self) -> str:
        return str(self.ref)


@dataclass(frozen=True, slots=True)
class BotE2EEControlPage:
    application_ref: EntityRef
    channel_ref: EntityRef
    device_id: str
    controls: tuple[BotE2EEControlRecord, ...]
    next_after: str | None

    @classmethod
    def from_payload(cls, payload: object) -> BotE2EEControlPage:
        raw = _payload_mapping(payload, "bot E2EE control log")
        try:
            application_ref = EntityRef.parse(raw.get("application_ref"))
            channel_ref = EntityRef.parse(raw.get("channel_ref"))
        except ValueError as exc:
            raise E2EEProtocolError("bot E2EE control-log identity is invalid") from exc
        device_id = raw.get("device_id")
        raw_controls = raw.get("controls")
        next_after = raw.get("next_after")
        if (
            not isinstance(device_id, str)
            or BOT_E2EE_DEVICE_ID_RE.fullmatch(device_id) is None
            or not isinstance(raw_controls, list)
            or len(raw_controls) > 25
            or (next_after is not None and not isinstance(next_after, str))
        ):
            raise E2EEProtocolError("bot E2EE control-log response is invalid")
        controls = tuple(
            BotE2EEControlRecord.from_payload(item) for item in raw_controls
        )
        if any(control.channel_ref != channel_ref for control in controls):
            raise E2EEProtocolError("bot E2EE control-log channel was substituted")
        if any(
            (left.ref.id, left.ref.domain) >= (right.ref.id, right.ref.domain)
            for left, right in pairwise(controls)
        ):
            raise E2EEProtocolError("bot E2EE control log is out of order")
        if next_after is not None:
            try:
                next_ref = EntityRef.parse(next_after)
            except ValueError as exc:
                raise E2EEProtocolError(
                    "bot E2EE control-log cursor is invalid"
                ) from exc
            if next_ref.domain != channel_ref.domain:
                raise E2EEProtocolError(
                    "bot E2EE control-log cursor authority is invalid"
                )
        return cls(application_ref, channel_ref, device_id, controls, next_after)


@dataclass(frozen=True, slots=True)
class WebhookE2EEControlPage:
    webhook_ref: EntityRef
    channel_ref: EntityRef
    device_id: str
    controls: tuple[BotE2EEControlRecord, ...]
    next_after: str | None

    @classmethod
    def from_payload(cls, payload: object) -> WebhookE2EEControlPage:
        raw = _payload_mapping(payload, "webhook E2EE control log")
        try:
            webhook_ref = EntityRef.parse(raw.get("webhook_ref"))
            channel_ref = EntityRef.parse(raw.get("channel_ref"))
        except ValueError as exc:
            raise E2EEProtocolError(
                "webhook E2EE control-log identity is invalid"
            ) from exc
        device_id = raw.get("device_id")
        raw_controls = raw.get("controls")
        next_after = raw.get("next_after")
        if (
            not isinstance(device_id, str)
            or WEBHOOK_E2EE_DEVICE_ID_RE.fullmatch(device_id) is None
            or not isinstance(raw_controls, list)
            or len(raw_controls) > 25
            or (next_after is not None and not isinstance(next_after, str))
        ):
            raise E2EEProtocolError("webhook E2EE control-log response is invalid")
        controls = tuple(
            BotE2EEControlRecord.from_payload(item) for item in raw_controls
        )
        if any(control.channel_ref != channel_ref for control in controls):
            raise E2EEProtocolError("webhook E2EE control-log channel was substituted")
        if any(
            (left.ref.id, left.ref.domain) >= (right.ref.id, right.ref.domain)
            for left, right in pairwise(controls)
        ):
            raise E2EEProtocolError("webhook E2EE control log is out of order")
        if next_after is not None:
            try:
                next_ref = EntityRef.parse(next_after)
            except ValueError as exc:
                raise E2EEProtocolError(
                    "webhook E2EE control-log cursor is invalid"
                ) from exc
            if next_ref.domain != channel_ref.domain:
                raise E2EEProtocolError(
                    "webhook E2EE control-log cursor authority is invalid"
                )
        return cls(webhook_ref, channel_ref, device_id, controls, next_after)


@dataclass(frozen=True, slots=True)
class DecryptedInteractionData:
    """Strict application data authenticated by one MLS interaction envelope."""

    options: dict[str, Any]
    values: tuple[str, ...]
    components: tuple[dict[str, Any], ...]
    attachments: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class EncryptedRichMessage:
    """One canonical MLS envelope for an ordinary rich message revision."""

    context: dict[str, object]
    envelope: dict[str, object]


@dataclass(frozen=True, slots=True)
class DecryptedRichMessageData:
    """The complete authenticated rich body of an ordinary message."""

    content: str | None
    embeds: tuple[dict[str, Any], ...]
    components: tuple[dict[str, Any], ...]
    poll: dict[str, Any] | None
    sticker_items: tuple[dict[str, Any], ...]
    tts: bool
    voice_message: bool
    flags: int
    attachments: tuple[dict[str, Any], ...]
    allowed_mentions: dict[str, Any]
    forward_snapshot: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class EncryptedInteractionResponse:
    """One MLS body bound to an exact isolated interaction response revision."""

    response_id: int
    sequence: int
    revision: int
    callback_type: int
    context: dict[str, object]
    envelope: dict[str, object]


@dataclass(slots=True)
class InteractionE2EEContext:
    """Verified current MLS state used to decrypt one channel's interactions."""

    provider: E2EEProvider
    channel_ref: EntityRef
    group_id: bytes
    policy_generation: int
    epoch: int
    history_floor_message_ref: EntityRef | None = None
    _invalidated: bool = field(default=False, init=False, repr=False)
    _seen_ciphertexts: dict[bytes, int] = field(
        default_factory=dict, init=False, repr=False
    )
    _message_revisions: dict[EntityRef, tuple[int, bytes]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _message_ciphertexts: dict[bytes, EntityRef] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.provider = require_real_e2ee_provider(self.provider)
        if not isinstance(self.group_id, bytes) or not 1 <= len(self.group_id) <= 128:
            raise ValueError("interaction MLS group ID must contain 1 to 128 bytes")
        if (
            isinstance(self.policy_generation, bool)
            or not isinstance(self.policy_generation, int)
            or not 1 <= self.policy_generation <= (1 << 63) - 1
        ):
            raise ValueError("interaction E2EE policy generation is invalid")
        if (
            isinstance(self.epoch, bool)
            or not isinstance(self.epoch, int)
            or not 0 <= self.epoch <= (1 << 64) - 1
        ):
            raise ValueError("interaction MLS epoch is invalid")
        if (
            self.history_floor_message_ref is not None
            and self.history_floor_message_ref.domain != self.channel_ref.domain
        ):
            raise ValueError("message history floor has the wrong authority")

    def invalidate(self) -> None:
        self._invalidated = True
        self._seen_ciphertexts.clear()
        self._message_revisions.clear()
        self._message_ciphertexts.clear()

    def require_current(self) -> None:
        if self._invalidated:
            raise E2EEProtocolError("interaction E2EE context was invalidated")
        if self.provider.group_epoch(self.group_id) != self.epoch:
            self.invalidate()
            raise E2EEProtocolError("interaction MLS context is stale")

    def record_ciphertext(self, ciphertext: bytes, interaction_id: int) -> None:
        digest = hashlib.sha256(ciphertext).digest()
        previous = self._seen_ciphertexts.get(digest)
        if previous is not None and previous != interaction_id:
            raise E2EEProtocolError("encrypted interaction ciphertext was replayed")
        self._seen_ciphertexts[digest] = interaction_id
        while len(self._seen_ciphertexts) > MAX_INTERACTION_REPLAY_ENTRIES:
            self._seen_ciphertexts.pop(next(iter(self._seen_ciphertexts)))

    def record_message_ciphertext(
        self,
        ciphertext: bytes,
        message_ref: EntityRef,
        revision: int,
        operation: Literal["create", "edit"],
    ) -> None:
        """Fence cross-message replay, equivocation, stale revisions, and history floors."""

        if (
            self.history_floor_message_ref is not None
            and message_ref.domain == self.history_floor_message_ref.domain
            and message_ref.id < self.history_floor_message_ref.id
        ):
            raise E2EEProtocolError("encrypted message predates the bot history floor")
        digest = hashlib.sha256(ciphertext).digest()
        replayed_ref = self._message_ciphertexts.get(digest)
        if replayed_ref is not None and replayed_ref != message_ref:
            raise E2EEProtocolError("encrypted message ciphertext was replayed")
        previous = self._message_revisions.get(message_ref)
        if previous is not None:
            previous_revision, previous_digest = previous
            if revision == previous_revision:
                if not hmac.compare_digest(previous_digest, digest):
                    raise E2EEProtocolError(
                        "encrypted message revision was equivocated"
                    )
                return
            if operation != "edit" or revision != previous_revision + 1:
                raise E2EEProtocolError(
                    "encrypted message revision is stale or skipped"
                )
        elif operation == "create" and revision != 1:
            raise E2EEProtocolError("encrypted message create revision is invalid")
        elif operation == "edit" and revision <= 1:
            raise E2EEProtocolError("encrypted message edit revision is invalid")
        self._message_ciphertexts[digest] = message_ref
        self._message_revisions[message_ref] = (revision, digest)
        while len(self._message_revisions) > MAX_INTERACTION_REPLAY_ENTRIES:
            expired_ref = next(iter(self._message_revisions))
            _expired_revision, expired_digest = self._message_revisions.pop(expired_ref)
            self._message_ciphertexts.pop(expired_digest, None)


@runtime_checkable
class E2EEProvider(Protocol):
    """Cryptographic provider required by bot participant mode.

    Implementations must provide real RFC 9420 MLS behavior. The SDK's built-in
    implementation calls Kaede's existing OpenMLS C ABI; tests may inject a
    provider implementing this protocol without weakening production checks.
    """

    def export_state(self) -> bytes: ...

    def public_identity_key(self) -> bytes: ...

    def sign(self, value: bytes) -> bytes: ...

    def generate_key_package(self) -> bytes: ...

    def inspect_key_package(self, package: bytes) -> tuple[bytes, bytes]: ...

    def create_group(self, group_id: bytes) -> None: ...

    def add_members(
        self, group_id: bytes, packages: Sequence[bytes]
    ) -> tuple[bytes, bytes]: ...

    def remove_accounts(
        self, group_id: bytes, accounts: Sequence[str]
    ) -> tuple[bytes, bytes]: ...

    def merge_pending_commit(self, group_id: bytes) -> None: ...

    def join_group(self, welcome: bytes) -> bytes: ...

    def encrypt(self, group_id: bytes, plaintext: bytes, aad: bytes) -> bytes: ...

    def process(self, group_id: bytes, message: bytes) -> dict[str, object]: ...

    def group_epoch(self, group_id: bytes) -> int: ...

    def export_epoch_secret(
        self, group_id: bytes, label: str, context: bytes, length: int
    ) -> bytes: ...

    def close(self) -> None: ...


class _NativeBuffer(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("len", ctypes.c_size_t),
    ]


class _NativeLibrary(Protocol):
    kaede_e2ee_invoke: Any
    kaede_e2ee_close: Any
    kaede_e2ee_buffer_free: Any


def _decode(
    value: object,
    field: str,
    *,
    maximum: int,
    exact: int | None = None,
    allow_empty: bool = False,
) -> bytes:
    if not isinstance(value, str):
        raise E2EEProtocolError(f"native OpenMLS response omitted {field}")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, UnicodeEncodeError) as exc:
        raise E2EEProtocolError(
            f"native OpenMLS response contained invalid {field}"
        ) from exc
    if (
        (not decoded and not allow_empty)
        or len(decoded) > maximum
        or (exact is not None and len(decoded) != exact)
        or _b64(decoded) != value
    ):
        raise E2EEProtocolError(f"native OpenMLS response contained invalid {field}")
    return decoded


def _encrypted_file_content_key(raw_key: bytes, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=ENCRYPTED_FILE_INFO,
    ).derive(raw_key)


def _encrypted_file_header(
    plaintext_size: int,
    chunk_size: int,
    salt: bytes,
    nonce_prefix: bytes,
) -> bytes:
    return b"".join(
        (
            ENCRYPTED_FILE_MAGIC,
            b"\x01",
            chunk_size.to_bytes(4, "big"),
            plaintext_size.to_bytes(8, "big"),
            salt,
            nonce_prefix,
        )
    )


def _encrypted_file_chunk_context(
    header: bytes,
    nonce_prefix: bytes,
    index: int,
    count: int,
) -> tuple[bytes, bytes]:
    encoded_index = index.to_bytes(4, "big")
    return nonce_prefix + encoded_index, header + encoded_index + count.to_bytes(
        4, "big"
    )


def _encrypted_file_metadata(filename: str, content_type: str) -> tuple[str, str]:
    safe_filename = filename.strip() or "file"
    safe_content_type = content_type.strip().lower() or "application/octet-stream"
    if (
        len(safe_filename) > 255
        or any(
            ord(character) <= 0x1F or ord(character) == 0x7F
            for character in safe_filename
        )
        or len(safe_content_type) > 100
        or CONTENT_TYPE_RE.fullmatch(safe_content_type) is None
    ):
        raise ValueError("encrypted file metadata is invalid")
    return safe_filename, safe_content_type


def encrypt_file(
    plaintext: bytes,
    *,
    filename: str,
    content_type: str = "application/octet-stream",
    chunk_size: int = DEFAULT_ENCRYPTED_FILE_CHUNK_SIZE,
) -> EncryptedFile:
    """Encrypt bytes with the canonical browser/mobile kaede-file-v1 framing."""

    if (
        not isinstance(plaintext, bytes)
        or not 1 <= len(plaintext) <= MAX_ENCRYPTED_FILE_BYTES
    ):
        raise ValueError("encrypted files must contain 1 byte to 64 MiB")
    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or not 64 * 1024 <= chunk_size <= 1024 * 1024
    ):
        raise ValueError("encrypted file chunk size is invalid")
    safe_filename, safe_content_type = _encrypted_file_metadata(filename, content_type)
    file_id = secrets.token_bytes(16)
    raw_key = bytearray(secrets.token_bytes(32))
    salt = secrets.token_bytes(16)
    nonce_prefix = secrets.token_bytes(8)
    header = _encrypted_file_header(len(plaintext), chunk_size, salt, nonce_prefix)
    count = (len(plaintext) + chunk_size - 1) // chunk_size
    output = bytearray(header)
    try:
        cipher = AESGCM(_encrypted_file_content_key(bytes(raw_key), salt))
        for index in range(count):
            chunk = plaintext[index * chunk_size : (index + 1) * chunk_size]
            nonce, aad = _encrypted_file_chunk_context(
                header,
                nonce_prefix,
                index,
                count,
            )
            encrypted = cipher.encrypt(nonce, chunk, aad)
            output.extend(len(encrypted).to_bytes(4, "big"))
            output.extend(encrypted)
        ciphertext = bytes(output)
        return EncryptedFile(
            ciphertext=ciphertext,
            manifest=EncryptedFileManifest(
                version=1,
                protocol="kaede-file-v1",
                file_id=_b64(file_id),
                key=_b64(bytes(raw_key)),
                filename=safe_filename,
                content_type=safe_content_type,
                plaintext_size=len(plaintext),
                ciphertext_size=len(ciphertext),
                ciphertext_sha256=_b64(hashlib.sha256(ciphertext).digest()),
                plaintext_sha256=_b64(hashlib.sha256(plaintext).digest()),
                chunk_size=chunk_size,
            ),
        )
    finally:
        raw_key[:] = b"\0" * len(raw_key)


def decrypt_file(ciphertext: bytes, manifest: Mapping[str, object]) -> bytes:
    """Authenticate and decrypt one exact kaede-file-v1 manifest/ciphertext pair."""

    required = {
        "version",
        "protocol",
        "file_id",
        "key",
        "filename",
        "content_type",
        "plaintext_size",
        "ciphertext_size",
        "ciphertext_sha256",
        "chunk_size",
    }
    optional = {
        "attachment_id",
        "attachment_domain",
        "plaintext_sha256",
        "preview",
    }
    if (
        not isinstance(ciphertext, bytes)
        or set(manifest) - required - optional
        or not required <= set(manifest)
        or manifest.get("version") != 1
        or manifest.get("protocol") != "kaede-file-v1"
    ):
        raise E2EEProtocolError("encrypted file manifest is invalid")
    plaintext_size = manifest["plaintext_size"]
    ciphertext_size = manifest["ciphertext_size"]
    chunk_size = manifest["chunk_size"]
    if (
        isinstance(plaintext_size, bool)
        or not isinstance(plaintext_size, int)
        or not 1 <= plaintext_size <= MAX_ENCRYPTED_FILE_BYTES
        or isinstance(ciphertext_size, bool)
        or not isinstance(ciphertext_size, int)
        or isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or not 64 * 1024 <= chunk_size <= 1024 * 1024
    ):
        raise E2EEProtocolError("encrypted file sizes are invalid")
    count = (plaintext_size + chunk_size - 1) // chunk_size
    expected_ciphertext_size = plaintext_size + ENCRYPTED_FILE_HEADER_SIZE + count * 20
    if (
        ciphertext_size != expected_ciphertext_size
        or len(ciphertext) != ciphertext_size
        or len(ciphertext) < ENCRYPTED_FILE_HEADER_SIZE
    ):
        raise E2EEProtocolError("encrypted file size does not match its manifest")
    try:
        file_id = _decode(manifest["file_id"], "file ID", maximum=16, exact=16)
        raw_key = bytearray(_decode(manifest["key"], "file key", maximum=32, exact=32))
        expected_digest = _decode(
            manifest["ciphertext_sha256"],
            "file digest",
            maximum=32,
            exact=32,
        )
        expected_plaintext_digest = (
            _decode(
                manifest["plaintext_sha256"],
                "plaintext file digest",
                maximum=32,
                exact=32,
            )
            if manifest.get("plaintext_sha256") is not None
            else None
        )
        raw_filename = manifest["filename"]
        raw_content_type = manifest["content_type"]
        if not isinstance(raw_filename, str) or not isinstance(raw_content_type, str):
            raise ValueError("encrypted file metadata is invalid")
        normalized_metadata = _encrypted_file_metadata(raw_filename, raw_content_type)
        if normalized_metadata != (raw_filename, raw_content_type):
            raise ValueError("encrypted file metadata is not canonical")
    except (TypeError, ValueError, E2EEProtocolError) as exc:
        raise E2EEProtocolError("encrypted file manifest is invalid") from exc
    del file_id
    if not hmac.compare_digest(hashlib.sha256(ciphertext).digest(), expected_digest):
        raw_key[:] = b"\0" * len(raw_key)
        raise E2EEProtocolError("encrypted file was modified")
    if ciphertext[:4] != ENCRYPTED_FILE_MAGIC or ciphertext[4] != 1:
        raw_key[:] = b"\0" * len(raw_key)
        raise E2EEProtocolError("encrypted file header is invalid")
    header = ciphertext[:ENCRYPTED_FILE_HEADER_SIZE]
    header_chunk_size = int.from_bytes(header[5:9], "big")
    header_plaintext_size = int.from_bytes(header[9:17], "big")
    if header_chunk_size != chunk_size or header_plaintext_size != plaintext_size:
        raw_key[:] = b"\0" * len(raw_key)
        raise E2EEProtocolError("encrypted file header does not match its manifest")
    salt = header[17:33]
    nonce_prefix = header[33:41]
    output = bytearray()
    offset = ENCRYPTED_FILE_HEADER_SIZE
    try:
        cipher = AESGCM(_encrypted_file_content_key(bytes(raw_key), salt))
        for index in range(count):
            if offset + 4 > len(ciphertext):
                raise E2EEProtocolError("encrypted file is truncated")
            length = int.from_bytes(ciphertext[offset : offset + 4], "big")
            offset += 4
            if length < 17 or offset + length > len(ciphertext):
                raise E2EEProtocolError("encrypted file chunk is invalid")
            nonce, aad = _encrypted_file_chunk_context(
                header,
                nonce_prefix,
                index,
                count,
            )
            output.extend(
                cipher.decrypt(nonce, ciphertext[offset : offset + length], aad)
            )
            offset += length
    except InvalidTag as exc:
        output[:] = b"\0" * len(output)
        raise E2EEProtocolError("encrypted file authentication failed") from exc
    finally:
        raw_key[:] = b"\0" * len(raw_key)
    if offset != len(ciphertext) or len(output) != plaintext_size:
        output[:] = b"\0" * len(output)
        raise E2EEProtocolError("encrypted file framing is invalid")
    plaintext = bytes(output)
    output[:] = b"\0" * len(output)
    if expected_plaintext_digest is not None and not hmac.compare_digest(
        hashlib.sha256(plaintext).digest(),
        expected_plaintext_digest,
    ):
        raise E2EEProtocolError("encrypted file plaintext digest is invalid")
    return plaintext


def bot_device_protocol_id(
    application_ref: EntityRef,
    worker_id: int,
    identity_key: bytes,
) -> str:
    """Return the federation-stable protocol ID bound into a bot credential."""

    if worker_id < 1 or len(identity_key) != 32:
        raise ValueError("bot device identity is invalid")
    digest = hashlib.sha256(
        (f"kaede-bot-e2ee-device-v1\0{application_ref}\0{worker_id}\0").encode()
        + identity_key
    ).digest()
    return "kbe_" + _b64(digest)


def bot_mls_credential(
    application_ref: EntityRef,
    worker_id: int,
    identity_key: bytes,
) -> bytes:
    """Canonical MLS BasicCredential identity for one exact bot device."""

    if worker_id < 1:
        raise ValueError("worker_id must be positive")
    device_id = bot_device_protocol_id(application_ref, worker_id, identity_key)
    return json.dumps(
        {
            "account": f"bot:{application_ref}:worker:{worker_id}",
            "application_ref": str(application_ref),
            "credential_type": "kaede-bot-device-v2",
            "device_id": device_id,
            "worker_id": str(worker_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def bot_key_package_upload_input(
    *,
    protocol_id: str,
    generation: int,
    cipher_suite: str,
    expires_at: datetime,
    package_hashes: Iterable[bytes],
) -> bytes:
    """Canonical signature input accepted by the bot key-package endpoint."""

    if BOT_E2EE_DEVICE_ID_RE.fullmatch(protocol_id) is None:
        raise ValueError("bot E2EE device ID is invalid")
    if generation < 1:
        raise ValueError("bot E2EE device generation must be positive")
    if cipher_suite != MLS_SUITE:
        raise ValueError("unsupported MLS cipher suite")
    if expires_at.tzinfo is None:
        raise ValueError("key-package expiry requires a timezone")
    hashes = sorted(_b64(item) for item in package_hashes)
    if not hashes or any(len(item) != 43 for item in hashes):
        raise ValueError("key-package hashes are invalid")
    return b"\n".join(
        (
            b"kaede-bot-e2ee-key-packages-v1",
            protocol_id.encode(),
            str(generation).encode(),
            cipher_suite.encode(),
            expires_at.astimezone(UTC).isoformat().encode(),
            ",".join(hashes).encode(),
        )
    )


def _library_names() -> tuple[str, ...]:
    if sys.platform == "win32":
        return ("kaede_e2ee_ffi.dll",)
    if sys.platform == "darwin":
        return ("libkaede_e2ee_ffi.dylib",)
    return ("libkaede_e2ee_ffi.so",)


def _library_candidates(explicit: str | os.PathLike[str] | None) -> list[Path]:
    if explicit is not None:
        return [Path(explicit)]
    configured = os.environ.get("KAEDE_E2EE_LIBRARY")
    if configured:
        return [Path(configured)]
    package_root = Path(__file__).resolve().parent
    repo_root = (
        package_root.parents[2] if len(package_root.parents) >= 3 else package_root
    )
    candidates: list[Path] = []
    for name in _library_names():
        candidates.extend(
            (
                package_root / "native" / name,
                repo_root / "desktop" / "target" / "release" / name,
                repo_root / "desktop" / "target" / "debug" / name,
            )
        )
    return candidates


def _safe_library_path(candidate: Path) -> Path | None:
    try:
        resolved = candidate.expanduser().resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        return None
    if metadata.st_mode & stat.S_IWOTH:
        raise E2EEUnavailableError("refusing to load a world-writable OpenMLS library")
    return resolved


def _load_library(explicit: str | os.PathLike[str] | None = None) -> _NativeLibrary:
    errors: list[str] = []
    for candidate in _library_candidates(explicit):
        path = _safe_library_path(candidate)
        if path is None:
            continue
        try:
            library = cast(_NativeLibrary, ctypes.CDLL(str(path)))
            library.kaede_e2ee_invoke.argtypes = [
                ctypes.c_uint64,
                ctypes.POINTER(ctypes.c_uint8),
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_uint8),
                ctypes.c_size_t,
            ]
            library.kaede_e2ee_invoke.restype = _NativeBuffer
            library.kaede_e2ee_close.argtypes = [ctypes.c_uint64]
            library.kaede_e2ee_close.restype = None
            library.kaede_e2ee_buffer_free.argtypes = [_NativeBuffer]
            library.kaede_e2ee_buffer_free.restype = None
            return library
        except (AttributeError, OSError) as exc:
            errors.append(f"{path}: {exc}")
    detail = f" ({'; '.join(errors)})" if errors else ""
    raise E2EEUnavailableError(
        "Kaede's OpenMLS native library was not found; build kaede-e2ee-ffi "
        "or set KAEDE_E2EE_LIBRARY" + detail
    )


class NativeOpenMLSProvider:
    """Strict ctypes wrapper over ``desktop/crates/kaede-e2ee-ffi``."""

    def __init__(self, library: _NativeLibrary, handle: int) -> None:
        if handle <= 0:
            raise E2EEProtocolError("native OpenMLS returned an invalid handle")
        self._library = library
        self._handle = handle

    @classmethod
    def generate(
        cls,
        credential: bytes,
        *,
        library_path: str | os.PathLike[str] | None = None,
    ) -> Self:
        if not 1 <= len(credential) <= MAX_CREDENTIAL_BYTES:
            raise ValueError("MLS credentials must contain 1 to 16384 bytes")
        library = _load_library(library_path)
        result = cls._invoke_raw(
            library, 0, "generate", {"credential": _b64(credential)}
        )
        return cls(library, cls._handle_value(result))

    @classmethod
    def restore(
        cls,
        state: bytes,
        *,
        library_path: str | os.PathLike[str] | None = None,
    ) -> Self:
        if not 1 <= len(state) <= MAX_NATIVE_INPUT_BYTES:
            raise ValueError("MLS state has an invalid size")
        library = _load_library(library_path)
        result = cls._invoke_raw(library, 0, "restore", {"state": _b64(state)})
        return cls(library, cls._handle_value(result))

    @staticmethod
    def _handle_value(result: dict[str, object]) -> int:
        raw = result.get("handle")
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
            raise E2EEProtocolError("native OpenMLS returned an invalid handle")
        return int(raw)

    @staticmethod
    def _invoke_raw(
        library: _NativeLibrary,
        handle: int,
        method: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        if not method.isascii() or not method.replace("_", "").isalnum():
            raise ValueError("native OpenMLS method is invalid")
        method_bytes = method.encode()
        input_bytes = json.dumps(payload, separators=(",", ":")).encode()
        if len(input_bytes) > MAX_NATIVE_INPUT_BYTES:
            raise ValueError("native OpenMLS input is too large")
        method_buffer = (ctypes.c_uint8 * len(method_bytes)).from_buffer_copy(
            method_bytes
        )
        input_buffer = (ctypes.c_uint8 * len(input_bytes)).from_buffer_copy(input_bytes)
        returned = library.kaede_e2ee_invoke(
            handle,
            method_buffer,
            len(method_bytes),
            input_buffer,
            len(input_bytes),
        )
        try:
            if not returned.data or not 1 <= returned.len <= MAX_NATIVE_INPUT_BYTES:
                raise E2EEProtocolError("native OpenMLS returned an invalid buffer")
            raw = ctypes.string_at(returned.data, returned.len)
            try:
                envelope = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise E2EEProtocolError("native OpenMLS returned invalid JSON") from exc
            if not isinstance(envelope, dict) or not isinstance(
                envelope.get("ok"), bool
            ):
                raise E2EEProtocolError("native OpenMLS returned an invalid envelope")
            if not envelope["ok"]:
                error = envelope.get("error")
                raise E2EEProtocolError(
                    error
                    if isinstance(error, str) and error
                    else "native OpenMLS failed"
                )
            result = envelope.get("result")
            if not isinstance(result, dict):
                raise E2EEProtocolError("native OpenMLS returned an invalid result")
            return cast(dict[str, object], result)
        finally:
            library.kaede_e2ee_buffer_free(returned)

    def _invoke(
        self, method: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        if self._handle <= 0:
            raise E2EEProtocolError("native OpenMLS provider is closed")
        return self._invoke_raw(self._library, self._handle, method, payload or {})

    def export_state(self) -> bytes:
        return _decode(
            self._invoke("export_state").get("state"),
            "state",
            maximum=MAX_NATIVE_INPUT_BYTES,
        )

    def public_identity_key(self) -> bytes:
        return _decode(
            self._invoke("public_identity_key").get("bytes"),
            "identity key",
            maximum=32,
            exact=32,
        )

    def sign(self, value: bytes) -> bytes:
        if not value or len(value) > MAX_NATIVE_INPUT_BYTES:
            raise ValueError("signature input has an invalid size")
        return _decode(
            self._invoke("sign", {"input": _b64(value)}).get("bytes"),
            "signature",
            maximum=64,
            exact=64,
        )

    def generate_key_package(self) -> bytes:
        return _decode(
            self._invoke("generate_key_package").get("bytes"),
            "key package",
            maximum=MAX_KEY_PACKAGE_BYTES,
        )

    def inspect_key_package(self, package: bytes) -> tuple[bytes, bytes]:
        if not 1 <= len(package) <= MAX_KEY_PACKAGE_BYTES:
            raise ValueError("key package has an invalid size")
        result = self._invoke("inspect_key_package", {"key_package": _b64(package)})
        return (
            _decode(
                result.get("credential"), "credential", maximum=MAX_CREDENTIAL_BYTES
            ),
            _decode(result.get("signature_key"), "signature key", maximum=32, exact=32),
        )

    def create_group(self, group_id: bytes) -> None:
        if not 1 <= len(group_id) <= 128:
            raise ValueError("MLS group ID has an invalid size")
        self._invoke("create_group", {"group_id": _b64(group_id)})

    def add_members(
        self, group_id: bytes, packages: Sequence[bytes]
    ) -> tuple[bytes, bytes]:
        if not packages or len(packages) > 48:
            raise ValueError("MLS add-members requires 1 to 48 key packages")
        if any(not 1 <= len(package) <= MAX_KEY_PACKAGE_BYTES for package in packages):
            raise ValueError("key package has an invalid size")
        result = self._invoke(
            "add_members",
            {
                "group_id": _b64(group_id),
                "key_packages": [_b64(item) for item in packages],
            },
        )
        return self._pending(result)

    def remove_accounts(
        self, group_id: bytes, accounts: Sequence[str]
    ) -> tuple[bytes, bytes]:
        if not accounts or len(accounts) > 500 or any(not item for item in accounts):
            raise ValueError("MLS remove-accounts input is invalid")
        result = self._invoke(
            "remove_accounts",
            {"group_id": _b64(group_id), "accounts": list(accounts)},
        )
        return self._pending(result)

    @staticmethod
    def _pending(result: dict[str, object]) -> tuple[bytes, bytes]:
        return (
            _decode(result.get("commit"), "commit", maximum=MAX_MLS_MESSAGE_BYTES),
            _decode(
                result.get("welcome"),
                "welcome",
                maximum=MAX_MLS_MESSAGE_BYTES,
                allow_empty=True,
            ),
        )

    def merge_pending_commit(self, group_id: bytes) -> None:
        self._invoke("merge_pending_commit", {"group_id": _b64(group_id)})

    def join_group(self, welcome: bytes) -> bytes:
        if not 1 <= len(welcome) <= MAX_MLS_MESSAGE_BYTES:
            raise ValueError("MLS Welcome has an invalid size")
        return _decode(
            self._invoke("join_group", {"welcome": _b64(welcome)}).get("group_id"),
            "group ID",
            maximum=128,
        )

    def encrypt(self, group_id: bytes, plaintext: bytes, aad: bytes) -> bytes:
        if (
            not plaintext
            or len(plaintext) > MAX_NATIVE_INPUT_BYTES
            or not aad
            or len(aad) > 4096
        ):
            raise ValueError("MLS application message input is invalid")
        return _decode(
            self._invoke(
                "encrypt",
                {
                    "group_id": _b64(group_id),
                    "plaintext": _b64(plaintext),
                    "aad": _b64(aad),
                },
            ).get("bytes"),
            "ciphertext",
            maximum=MAX_NATIVE_INPUT_BYTES,
        )

    def process(self, group_id: bytes, message: bytes) -> dict[str, object]:
        if not 1 <= len(message) <= MAX_MLS_MESSAGE_BYTES:
            raise ValueError("MLS message has an invalid size")
        result = self._invoke(
            "process", {"group_id": _b64(group_id), "message": _b64(message)}
        )
        kind = result.get("kind")
        if kind not in {"application", "proposal", "commit"}:
            raise E2EEProtocolError("native OpenMLS returned an invalid message kind")
        return result

    def group_epoch(self, group_id: bytes) -> int:
        if not 1 <= len(group_id) <= 128:
            raise ValueError("MLS group ID has an invalid size")
        raw = self._invoke("group_epoch", {"group_id": _b64(group_id)}).get("epoch")
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
            raise E2EEProtocolError("native OpenMLS returned an invalid group epoch")
        epoch = int(raw)
        if epoch > (1 << 64) - 1:
            raise E2EEProtocolError("native OpenMLS returned an invalid group epoch")
        return epoch

    def export_epoch_secret(
        self,
        group_id: bytes,
        label: str,
        context: bytes,
        length: int,
    ) -> bytes:
        if not label or len(label.encode()) > 255:
            raise ValueError("MLS exporter label is invalid")
        if (
            not context
            or len(context) > MAX_EXPORTER_CONTEXT_BYTES
            or not 1 <= length <= 64
        ):
            raise ValueError("MLS exporter parameters are invalid")
        return _decode(
            self._invoke(
                "export_epoch_secret",
                {
                    "group_id": _b64(group_id),
                    "label": label,
                    "context": _b64(context),
                    "length": length,
                },
            ).get("bytes"),
            "exported secret",
            maximum=64,
            exact=length,
        )

    def close(self) -> None:
        if self._handle > 0:
            self._library.kaede_e2ee_close(self._handle)
            self._handle = 0

    def __enter__(self) -> Self:
        if self._handle <= 0:
            raise E2EEProtocolError("native OpenMLS provider is closed")
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            if getattr(self, "_handle", 0) > 0:
                self.close()
        except (AttributeError, OSError):
            # Finalizers must never surface failures during interpreter teardown.
            pass


def require_real_e2ee_provider(provider: E2EEProvider | None) -> E2EEProvider:
    if provider is None or not isinstance(provider, E2EEProvider):
        raise E2EEUnavailableError(
            "participant mode requires NativeOpenMLSProvider or an explicit real MLS provider"
        )
    return provider


def _provider_group_epoch(provider: E2EEProvider, group_id: bytes) -> int | None:
    try:
        epoch = provider.group_epoch(group_id)
    except E2EEProtocolError:
        return None
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or not 0 <= epoch <= (1 << 64) - 1
    ):
        raise E2EEProtocolError("MLS provider returned an invalid group epoch")
    return epoch


def process_e2ee_welcome(
    provider: E2EEProvider,
    group_id: bytes,
    welcome: bytes,
    *,
    expected_epoch: int,
) -> int:
    """Join a missing group or idempotently recognize an applied Welcome."""

    current_epoch = _provider_group_epoch(provider, group_id)
    if current_epoch is None:
        joined_group = provider.join_group(welcome)
        if not hmac.compare_digest(joined_group, group_id):
            raise E2EEProtocolError("MLS Welcome targets a different group")
        current_epoch = _provider_group_epoch(provider, group_id)
    if current_epoch is None or current_epoch < expected_epoch:
        raise E2EEProtocolError("MLS Welcome did not establish the expected epoch")
    return current_epoch


def process_e2ee_commit(
    provider: E2EEProvider,
    group_id: bytes,
    commit: bytes,
    *,
    expected_epoch: int,
    apply: bool,
) -> int:
    """Apply one ordered commit, tolerating only exact already-applied replay."""

    current_epoch = _provider_group_epoch(provider, group_id)
    if current_epoch is None:
        raise E2EEProtocolError("MLS commit arrived before its Welcome")
    if not apply or current_epoch >= expected_epoch:
        if current_epoch < expected_epoch:
            raise E2EEProtocolError("audit-only MLS commit precedes local group state")
        return current_epoch
    if current_epoch + 1 != expected_epoch:
        raise E2EEProtocolError("MLS control log has an epoch gap")
    result = provider.process(group_id, commit)
    if result.get("kind") != "commit":
        raise E2EEProtocolError("MLS control record is not a commit")
    applied_epoch = _provider_group_epoch(provider, group_id)
    if applied_epoch != expected_epoch:
        raise E2EEProtocolError("MLS commit did not advance to the expected epoch")
    return applied_epoch


def process_e2ee_control(
    context: InteractionE2EEContext,
    control: BotE2EEControlRecord,
) -> None:
    """Apply a durable control before decrypting Gateway interactions."""

    if context._invalidated:
        raise E2EEProtocolError("interaction E2EE context was invalidated")
    if control.channel_ref != context.channel_ref:
        raise E2EEProtocolError("MLS control belongs to a different channel")
    group_id = _wire_b64(
        control.envelope.get("group_id"),
        "MLS group ID",
        maximum=128,
    )
    ciphertext = _wire_b64(
        control.envelope.get("ciphertext"),
        "MLS control ciphertext",
        maximum=MAX_MLS_MESSAGE_BYTES,
    )
    operation = control.envelope.get("operation")
    if operation == "welcome":
        current_epoch = process_e2ee_welcome(
            context.provider,
            group_id,
            ciphertext,
            expected_epoch=control.epoch,
        )
    elif operation == "commit":
        current_epoch = process_e2ee_commit(
            context.provider,
            group_id,
            ciphertext,
            expected_epoch=control.epoch,
            apply=control.apply,
        )
    else:
        raise E2EEProtocolError("unsupported MLS control operation")
    if control.policy_generation < context.policy_generation:
        return
    if (
        control.policy_generation == context.policy_generation
        and not hmac.compare_digest(group_id, context.group_id)
    ):
        raise E2EEProtocolError("MLS policy generation was equivocated")
    context.group_id = group_id
    context.policy_generation = control.policy_generation
    context.epoch = current_epoch


def _canonical_json(value: object, *, label: str) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as exc:
        raise E2EEProtocolError(f"{label} is not canonical JSON") from exc
    if not encoded or len(encoded) > MAX_INTERACTION_PLAINTEXT_BYTES:
        raise E2EEProtocolError(f"{label} has an invalid size")
    return encoded


def _wire_integer(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise E2EEProtocolError(f"{label} is invalid")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise E2EEProtocolError(f"{label} is invalid")
    return parsed


def _optional_wire_integer(value: int | None) -> str | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= (1 << 63) - 1
    ):
        raise E2EEProtocolError("interaction identity is invalid")
    return str(value)


def _interaction_attachment_ids(interaction: Interaction) -> list[str]:
    resolved = interaction.resolved or {}
    attachments = resolved.get("attachments", {})
    if not isinstance(attachments, dict):
        raise E2EEProtocolError("resolved interaction attachments are invalid")
    result: list[str] = []
    for raw_id, raw_attachment in attachments.items():
        attachment_id = str(raw_id)
        if (
            not attachment_id.isascii()
            or not attachment_id.isdecimal()
            or attachment_id.startswith("0")
            or int(attachment_id) > (1 << 63) - 1
            or not isinstance(raw_attachment, dict)
            or str(raw_attachment.get("id")) != attachment_id
        ):
            raise E2EEProtocolError("resolved interaction attachments are invalid")
        result.append(attachment_id)
    return sorted(result, key=int)


def interaction_authenticated_context(
    interaction: Interaction,
    envelope: Mapping[str, object],
) -> dict[str, object]:
    """Build the exact context browsers authenticate and bot workers verify."""

    command = interaction.command
    command_name = command.get("name") if isinstance(command, dict) else None
    command_type = command.get("type") if isinstance(command, dict) else None
    if command_name is not None and not isinstance(command_name, str):
        raise E2EEProtocolError("interaction command name is invalid")
    if command_type is not None and command_type not in {
        "chat_input",
        "user",
        "message",
    }:
        raise E2EEProtocolError("interaction command type is invalid")
    component_type = interaction.component_type
    if isinstance(component_type, bool) or not isinstance(
        component_type, (int, str, type(None))
    ):
        raise E2EEProtocolError("interaction component type is invalid")
    command_interaction = interaction.type in {"command", "autocomplete"}
    if command_interaction != (interaction.command_id is not None):
        raise E2EEProtocolError("interaction command identity is invalid")
    return {
        "application_ref": str(interaction.application_ref),
        "attachment_ids": _interaction_attachment_ids(interaction),
        "autocomplete_generation": _optional_wire_integer(
            interaction.autocomplete_generation
        ),
        "channel_ref": str(interaction.channel_ref),
        "command_id": _optional_wire_integer(interaction.command_id),
        "command_name": command_name,
        "command_type": command_type,
        "component_type": component_type,
        "context": interaction.context,
        "custom_id": interaction.custom_id,
        "epoch": envelope.get("epoch"),
        "focused_option": interaction.focused_option,
        "group_id": envelope.get("group_id"),
        "integration_type": interaction.integration_type,
        "interaction_type": interaction.type,
        "invoker_ref": str(interaction.user.ref),
        "message_ref": str(interaction.message_ref)
        if interaction.message_ref
        else None,
        "policy_generation": envelope.get("policy_generation"),
        "response_id": _optional_wire_integer(interaction.response_id),
        "sender_device_id": envelope.get("sender_device_id"),
        "target_ref": str(interaction.target_ref) if interaction.target_ref else None,
        "view_version": _optional_wire_integer(interaction.view_version),
    }


def interaction_authenticated_data(context: Mapping[str, object]) -> bytes:
    """Serialize the public, purpose-bound MLS authenticated data."""

    return _canonical_json(
        {"context": dict(context), "purpose": INTERACTION_AAD_PURPOSE},
        label="interaction authenticated data",
    )


def interaction_response_authenticated_context(
    interaction: Interaction,
    envelope: Mapping[str, object],
) -> dict[str, object]:
    """Build the exact public context authenticated for one private response."""

    required = {
        "interaction_ref",
        "response_ref",
        "sequence",
        "revision",
        "callback_type",
        "attachment_refs",
        "group_id",
        "policy_generation",
        "epoch",
        "sender_device_id",
        "operation",
    }
    if not required <= set(envelope):
        raise E2EEProtocolError("interaction response identity is incomplete")
    interaction_ref = EntityRef(interaction.id, interaction.channel_ref.domain)
    try:
        projected_interaction = EntityRef.parse(envelope["interaction_ref"])
        response_ref = EntityRef.parse(envelope["response_ref"])
    except ValueError as exc:
        raise E2EEProtocolError("interaction response identity is invalid") from exc
    if (
        projected_interaction != interaction_ref
        or response_ref.domain != interaction_ref.domain
    ):
        raise E2EEProtocolError("interaction response authority is invalid")
    raw_attachments = envelope["attachment_refs"]
    if not isinstance(raw_attachments, list) or any(
        not isinstance(item, str) for item in raw_attachments
    ):
        raise E2EEProtocolError("interaction response attachment identity is invalid")
    attachment_refs = list(cast(list[str], raw_attachments))
    try:
        parsed_attachments = [EntityRef.parse(item) for item in attachment_refs]
    except ValueError as exc:
        raise E2EEProtocolError(
            "interaction response attachment identity is invalid"
        ) from exc
    if (
        attachment_refs != sorted(attachment_refs)
        or len(set(attachment_refs)) != len(attachment_refs)
        or any(item.domain != interaction_ref.domain for item in parsed_attachments)
    ):
        raise E2EEProtocolError("interaction response attachment identity is invalid")
    sequence = _wire_integer(
        envelope["sequence"],
        label="interaction response sequence",
        minimum=0,
        maximum=(1 << 63) - 1,
    )
    revision = _wire_integer(
        envelope["revision"],
        label="interaction response revision",
        minimum=1,
        maximum=(1 << 63) - 1,
    )
    callback_type = envelope["callback_type"]
    if isinstance(callback_type, bool) or callback_type not in {4, 7, 8, 9}:
        raise E2EEProtocolError("interaction response callback type is invalid")
    operation = envelope["operation"]
    if operation != ("create" if revision == 1 else "edit"):
        raise E2EEProtocolError("interaction response operation is invalid")
    contract_digest = envelope.get("interaction_contract_digest")
    if contract_digest is not None:
        _wire_b64(
            contract_digest,
            "interaction routing contract digest",
            maximum=32,
            exact=32,
        )
    return {
        "application_ref": str(interaction.application_ref),
        "attachment_refs": attachment_refs,
        "authority_domain": interaction_ref.domain,
        "callback_type": callback_type,
        "channel_ref": str(interaction.channel_ref),
        "epoch": envelope["epoch"],
        "group_id": envelope["group_id"],
        "interaction_ref": str(interaction_ref),
        "interaction_contract_digest": contract_digest,
        "invoker_ref": str(interaction.user.ref),
        "operation": operation,
        "policy_generation": envelope["policy_generation"],
        "response_ref": str(response_ref),
        "revision": str(revision),
        "sender_device_id": envelope["sender_device_id"],
        "sequence": str(sequence),
    }


def _rich_mapping(value: object, label: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    serializer = getattr(value, "to_dict", None)
    if callable(serializer):
        rendered = serializer()
        if isinstance(rendered, Mapping):
            return cast(Mapping[str, object], rendered)
    raise ValueError(f"{label} must be a mapping or SDK rich-content model")


def _routing_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise ValueError(f"{label} is invalid")
    return value


def _routing_int(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _routing_option_digests(value: object, *, maximum: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("interaction routing options are invalid")
    if not 1 <= len(value) <= maximum:
        raise ValueError("interaction routing options are invalid")
    option_values = [
        _routing_text(
            _rich_mapping(item, "interaction routing option").get("value"),
            "interaction routing option value",
        )
        for item in value
    ]
    if len(set(option_values)) != len(option_values):
        raise ValueError("interaction routing option values must be unique")
    return sorted(
        _b64(hashlib.sha256(item.encode("utf-8")).digest()) for item in option_values
    )


def _routing_control(value: object, *, modal: bool) -> dict[str, object] | None:
    raw = _rich_mapping(value, "interaction routing control")
    control_type = raw.get("type")
    if isinstance(control_type, bool) or not isinstance(control_type, int):
        raise ValueError("interaction routing control type is invalid")
    if control_type == 2 and raw.get("custom_id") is None:
        # Link and premium buttons do not dispatch interactions.
        return None
    allowed = {4, 19, 21, 22, 23} if modal else {2}
    allowed |= {3, 5, 6, 7, 8}
    if control_type not in allowed:
        return None
    custom_id = _routing_text(
        raw.get("custom_id"),
        "interaction routing custom ID",
    )
    if control_type == 2:
        disabled = raw.get("disabled", False)
        if not isinstance(disabled, bool):
            raise ValueError("interaction routing button state is invalid")
        return {"type": 2, "custom_id": custom_id, "disabled": disabled}
    if control_type in {3, 5, 6, 7, 8}:
        minimum = _routing_int(
            raw.get("min_values", 1),
            "interaction routing minimum values",
            minimum=0,
            maximum=25,
        )
        maximum = _routing_int(
            raw.get("max_values", 1),
            "interaction routing maximum values",
            minimum=minimum,
            maximum=25,
        )
        disabled = raw.get("disabled", False)
        if not isinstance(disabled, bool):
            raise ValueError("interaction routing select state is invalid")
        required = raw.get("required") is not False
        if modal and (disabled or required and minimum == 0):
            raise ValueError("interaction routing modal select state is invalid")
        result: dict[str, object] = {
            "type": control_type,
            "custom_id": custom_id,
            "disabled": disabled,
            "min_values": minimum,
            "max_values": maximum,
        }
        if modal:
            result["required"] = required
        if control_type == 3:
            option_digests = _routing_option_digests(raw.get("options"), maximum=25)
            if maximum > len(option_digests):
                raise ValueError("interaction routing select range is invalid")
            result["option_value_digests"] = option_digests
        if control_type == 8:
            channel_types = raw.get("channel_types", ())
            if not isinstance(channel_types, Sequence) or isinstance(
                channel_types, (str, bytes)
            ):
                raise ValueError("interaction routing channel types are invalid")
            normalized_types = [
                _routing_int(
                    item,
                    "interaction routing channel type",
                    minimum=0,
                    maximum=(1 << 31) - 1,
                )
                for item in channel_types
            ]
            if len(normalized_types) > 19 or len(normalized_types) != len(
                set(normalized_types)
            ):
                raise ValueError("interaction routing channel types are invalid")
            result["channel_types"] = normalized_types
        return result
    if control_type == 4:
        raw_min_length = raw.get("min_length")
        raw_max_length = raw.get("max_length")
        normalized_minimum = _routing_int(
            0 if raw_min_length is None else raw_min_length,
            "interaction routing minimum length",
            minimum=0,
            maximum=4000,
        )
        normalized_maximum = _routing_int(
            4000 if raw_max_length is None else raw_max_length,
            "interaction routing maximum length",
            minimum=1,
            maximum=4000,
        )
        if normalized_minimum > normalized_maximum:
            raise ValueError("interaction routing text length range is invalid")
        return {
            "type": 4,
            "custom_id": custom_id,
            "required": raw.get("required", True) is not False,
            "min_length": normalized_minimum,
            "max_length": normalized_maximum,
        }
    if control_type == 19:
        file_types = raw.get("file_types", ())
        if not isinstance(file_types, Sequence) or isinstance(file_types, (str, bytes)):
            raise ValueError("interaction routing file types are invalid")
        normalized_file_types = [
            _routing_text(item, "interaction routing file type") for item in file_types
        ]
        if len(normalized_file_types) > 10 or len(normalized_file_types) != len(
            set(normalized_file_types)
        ):
            raise ValueError("interaction routing file types are invalid")
        minimum = _routing_int(
            raw.get("min_values", 1),
            "interaction routing minimum files",
            minimum=0,
            maximum=10,
        )
        maximum = _routing_int(
            raw.get("max_values", 1),
            "interaction routing maximum files",
            minimum=1,
            maximum=10,
        )
        required = raw.get("required", True) is not False
        if minimum > maximum or required and minimum == 0:
            raise ValueError("interaction routing file range is invalid")
        return {
            "type": 19,
            "custom_id": custom_id,
            "required": required,
            "min_values": minimum,
            "max_values": maximum,
            "file_types": normalized_file_types,
        }
    if control_type in {21, 22}:
        option_digests = _routing_option_digests(raw.get("options"), maximum=10)
        result = {
            "type": control_type,
            "custom_id": custom_id,
            "required": raw.get("required", True) is not False,
            "option_value_digests": option_digests,
        }
        if control_type == 22:
            result["min_values"] = _routing_int(
                raw.get("min_values", 1),
                "interaction routing minimum choices",
                minimum=0,
                maximum=len(option_digests),
            )
            result["max_values"] = _routing_int(
                raw.get("max_values", len(option_digests)),
                "interaction routing maximum choices",
                minimum=cast(int, result["min_values"]),
                maximum=len(option_digests),
            )
            if result["required"] is True and result["min_values"] == 0:
                raise ValueError("interaction routing required choices are invalid")
        return result
    return {"type": 23, "custom_id": custom_id}


def _walk_routing_controls(value: object) -> Iterable[object]:
    raw = _rich_mapping(value, "interaction component")
    yield raw
    for key in ("components",):
        children = raw.get(key)
        if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
            for child in children:
                yield from _walk_routing_controls(child)
    for key in ("component", "accessory"):
        child = raw.get(key)
        if child is not None:
            yield from _walk_routing_controls(child)


def interaction_routing_contract(
    data: Mapping[str, object],
    *,
    callback_type: Literal[4, 7, 8, 9] | None,
) -> dict[str, object] | None:
    """Derive the minimal server-routable contract from encrypted rich data."""

    if callback_type == 8:
        return None
    if callback_type == 9:
        custom_id = _routing_text(data.get("custom_id"), "modal custom ID")
        raw_rows = data.get("components")
        if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
            raise ValueError("modal components are invalid")
        rows: list[dict[str, object]] = []
        for item in raw_rows:
            row = _rich_mapping(item, "modal row")
            row_type = row.get("type")
            if row_type == 10:
                continue
            if row_type == 1:
                raw_fields = row.get("components")
                if (
                    not isinstance(raw_fields, Sequence)
                    or isinstance(raw_fields, (str, bytes))
                    or len(raw_fields) != 1
                ):
                    raise ValueError("modal row is invalid")
                field = _routing_control(raw_fields[0], modal=True)
                if field is None:
                    raise ValueError("modal input is invalid")
                rows.append({"type": 1, "components": [field]})
            elif row_type == 18:
                field = _routing_control(row.get("component"), modal=True)
                if field is None:
                    raise ValueError("modal input is invalid")
                rows.append({"type": 18, "component": field})
            else:
                raise ValueError("modal row is invalid")
        if not 1 <= len(rows) <= 5:
            raise ValueError("modal routing contract requires one to five inputs")
        return {
            "version": INTERACTION_ROUTING_CONTRACT_VERSION,
            "kind": "modal",
            "custom_id": custom_id,
            "components": rows,
        }
    components = data.get("components", ())
    if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
        raise ValueError("message components are invalid")
    controls: list[dict[str, object]] = []
    for layout in components:
        for item in _walk_routing_controls(layout):
            control = _routing_control(item, modal=False)
            if control is not None:
                controls.append(control)
    poll_contract: dict[str, object] | None = None
    raw_poll = data.get("poll")
    if raw_poll is not None:
        poll = _rich_mapping(raw_poll, "encrypted poll")
        raw_answers = poll.get("answers")
        if (
            not isinstance(raw_answers, Sequence)
            or isinstance(raw_answers, (str, bytes))
            or not 2 <= len(raw_answers) <= 10
            or not isinstance(poll.get("allow_multiselect", False), bool)
            or poll.get("layout_type", 1) != 1
        ):
            raise ValueError("encrypted poll routing is invalid")
        for raw_answer in raw_answers:
            answer = _rich_mapping(raw_answer, "encrypted poll answer")
            if not isinstance(answer.get("poll_media"), Mapping):
                raise ValueError("encrypted poll answer is invalid")
        duration_hours = _routing_int(
            poll.get("duration", 24),
            "encrypted poll duration",
            minimum=1,
            maximum=768,
        )
        poll_contract = {
            "version": 1,
            "answer_ids": list(range(1, len(raw_answers) + 1)),
            "allow_multiselect": poll.get("allow_multiselect", False),
            "duration_seconds": duration_hours * 3600,
            "layout_type": 1,
        }
    if not controls and poll_contract is None:
        return None
    custom_ids = [cast(str, item["custom_id"]) for item in controls]
    if len(custom_ids) != len(set(custom_ids)):
        raise ValueError("interaction routing custom IDs must be unique")
    timeout = data.get("view_timeout_seconds", 900)
    contract: dict[str, object] = {
        "version": INTERACTION_ROUTING_CONTRACT_VERSION,
        "kind": "message",
        "view_timeout_seconds": _routing_int(
            timeout,
            "interaction view timeout",
            minimum=1,
            maximum=86_400,
        ),
        "components": controls,
    }
    if poll_contract is not None:
        contract["poll"] = poll_contract
    return contract


def interaction_routing_contract_digest(contract: Mapping[str, object]) -> str:
    """Return the canonical cross-language SHA-256 routing-contract digest."""

    encoded = _canonical_json(contract, label="interaction routing contract")
    return _b64(hashlib.sha256(encoded).digest())


def interaction_response_authenticated_data(context: Mapping[str, object]) -> bytes:
    return _canonical_json(
        {"context": dict(context), "purpose": INTERACTION_RESPONSE_AAD_PURPOSE},
        label="interaction response authenticated data",
    )


def interaction_response_plaintext(
    context: Mapping[str, object],
    data: Mapping[str, object],
) -> bytes:
    return _canonical_json(
        {
            "context": dict(context),
            "data": dict(data),
            "kind": "interaction_response",
            "version": 1,
        },
        label="interaction response plaintext",
    )


def encrypt_interaction_response(
    interaction: Interaction,
    context: InteractionE2EEContext,
    data: Mapping[str, object],
    *,
    callback_type: Literal[4, 7, 8, 9],
    response_id: int | None = None,
    sequence: int = 0,
    revision: int = 1,
    attachment_manifests: Mapping[str, Mapping[str, object]] | None = None,
) -> EncryptedInteractionResponse:
    """Encrypt one isolated response while binding its complete server identity."""

    if context.channel_ref != interaction.channel_ref:
        raise ValueError("interaction response E2EE context does not match the channel")
    context.require_current()
    chosen_id = (
        secrets.randbelow((1 << 63) - 1) + 1 if response_id is None else response_id
    )
    if (
        isinstance(chosen_id, bool)
        or not 1 <= chosen_id <= (1 << 63) - 1
        or isinstance(sequence, bool)
        or not 0 <= sequence <= (1 << 63) - 1
        or isinstance(revision, bool)
        or not 1 <= revision <= (1 << 63) - 1
    ):
        raise ValueError("interaction response identity is invalid")
    sender_device_id = interaction.client.e2ee_device_id
    if (
        sender_device_id is None
        or BOT_E2EE_DEVICE_ID_RE.fullmatch(sender_device_id) is None
    ):
        raise E2EEProtocolError("an exact bot E2EE device must be selected")
    authority = interaction.channel_ref.domain
    attachment_map = {
        str(key): dict(value) for key, value in (attachment_manifests or {}).items()
    }
    attachment_refs = sorted(attachment_map)
    if any(EntityRef.parse(item).domain != authority for item in attachment_refs):
        raise ValueError(
            "interaction response attachments must belong to its authority"
        )
    response_ref = EntityRef(chosen_id, authority)
    operation = "create" if revision == 1 else "edit"
    envelope: dict[str, object] = {
        "version": 2,
        "protocol": MLS_PROTOCOL,
        "suite": MLS_SUITE,
        "group_id": _b64(context.group_id),
        "policy_generation": str(context.policy_generation),
        "epoch": str(context.epoch),
        "sender_device_id": sender_device_id,
        "operation": operation,
        "interaction_ref": str(EntityRef(interaction.id, authority)),
        "response_ref": str(response_ref),
        "sequence": str(sequence),
        "revision": str(revision),
        "callback_type": callback_type,
        "attachment_refs": attachment_refs,
    }
    contract = interaction_routing_contract(data, callback_type=callback_type)
    if contract is not None:
        envelope["interaction_contract"] = contract
        envelope["interaction_contract_digest"] = interaction_routing_contract_digest(
            contract
        )
    elif callback_type == 9:
        raise ValueError("encrypted modal responses require a routing contract")
    if operation == "edit":
        envelope["target_message"] = str(response_ref)
    authenticated = interaction_response_authenticated_context(interaction, envelope)
    body = dict(data)
    if attachment_map:
        if "attachments" in body:
            raise ValueError("pass encrypted attachment manifests separately")
        body["attachments"] = attachment_map
    plaintext = interaction_response_plaintext(authenticated, body)
    aad = interaction_response_authenticated_data(authenticated)
    ciphertext = context.provider.encrypt(context.group_id, plaintext, aad)
    if not 1 <= len(ciphertext) <= MAX_MLS_MESSAGE_BYTES:
        raise E2EEProtocolError("interaction response ciphertext is invalid")
    envelope["ciphertext"] = _b64(ciphertext)
    return EncryptedInteractionResponse(
        response_id=chosen_id,
        sequence=sequence,
        revision=revision,
        callback_type=callback_type,
        context=authenticated,
        envelope=envelope,
    )


def interaction_plaintext(
    context: Mapping[str, object],
    *,
    options: Mapping[str, object] | None = None,
    values: Sequence[str] = (),
    components: Sequence[Mapping[str, object]] = (),
    attachments: Mapping[str, Mapping[str, object]] | None = None,
) -> bytes:
    """Serialize the sole accepted encrypted-interaction plaintext shape."""

    return _canonical_json(
        {
            "context": dict(context),
            "data": {
                "attachments": {
                    attachment_id: dict(manifest)
                    for attachment_id, manifest in (attachments or {}).items()
                },
                "components": [dict(item) for item in components],
                "options": dict(options or {}),
                "values": list(values),
            },
            "kind": "interaction",
            "version": 1,
        },
        label="interaction plaintext",
    )


def interaction_attachment_manifest_digest(
    attachments: Mapping[str, Mapping[str, object]],
) -> str:
    """Return the public envelope digest for the MLS-private file manifests."""

    encoded = _canonical_json(
        {
            attachment_id: dict(manifest)
            for attachment_id, manifest in attachments.items()
        },
        label="interaction attachment manifests",
    )
    return _b64(hashlib.sha256(encoded).digest())


def _validated_interaction_envelope(
    interaction: Interaction,
    context: InteractionE2EEContext,
) -> tuple[dict[str, object], bytes]:
    raw = interaction.encrypted_payload
    if not isinstance(raw, dict):
        raise E2EEProtocolError("interaction is missing its MLS envelope")
    required = {
        "version",
        "protocol",
        "suite",
        "group_id",
        "policy_generation",
        "epoch",
        "sender_device_id",
        "operation",
        "ciphertext",
    }
    optional = {"attachment_manifest_digest"}
    if set(raw) - required - optional or not required <= set(raw):
        raise E2EEProtocolError("interaction MLS envelope fields are invalid")
    if (
        raw.get("version") != 2
        or raw.get("protocol") != MLS_PROTOCOL
        or raw.get("suite") != MLS_SUITE
    ):
        raise E2EEProtocolError("interaction MLS envelope suite is unsupported")
    group_id = _decode(raw.get("group_id"), "group ID", maximum=128)
    if group_id != context.group_id:
        raise E2EEProtocolError("interaction MLS group does not match the channel")
    if (
        _wire_integer(
            raw.get("policy_generation"),
            label="interaction policy generation",
            minimum=1,
            maximum=(1 << 63) - 1,
        )
        != context.policy_generation
    ):
        raise E2EEProtocolError(
            "interaction policy generation does not match the channel"
        )
    if (
        _wire_integer(
            raw.get("epoch"),
            label="interaction MLS epoch",
            minimum=0,
            maximum=(1 << 64) - 1,
        )
        != context.epoch
    ):
        raise E2EEProtocolError("interaction MLS epoch does not match the channel")
    if (
        not isinstance(raw.get("sender_device_id"), str)
        or HUMAN_DEVICE_ID_RE.fullmatch(cast(str, raw["sender_device_id"])) is None
    ):
        raise E2EEProtocolError("interaction sender device ID is invalid")
    if raw.get("operation") != "create":
        raise E2EEProtocolError("interaction MLS operation is invalid")
    ciphertext = _decode(
        raw.get("ciphertext"),
        "interaction ciphertext",
        maximum=MAX_MLS_MESSAGE_BYTES,
    )
    return raw, ciphertext


def _validate_human_credential(value: bytes, expected_ref: EntityRef) -> None:
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EEProtocolError("interaction sender credential is invalid") from exc
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"version", "account", "nonce"}
        or parsed.get("version") != 1
        or parsed.get("account") != str(expected_ref)
        or not isinstance(parsed.get("nonce"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", cast(str, parsed["nonce"])) is None
    ):
        raise E2EEProtocolError(
            "interaction sender identity does not match the invoker"
        )


def decrypt_interaction(
    interaction: Interaction,
    context: InteractionE2EEContext,
) -> DecryptedInteractionData:
    """Authenticate, decrypt, and validate an encrypted gateway interaction."""

    if context.channel_ref != interaction.channel_ref:
        raise E2EEProtocolError("interaction E2EE context does not match the channel")
    context.require_current()
    envelope, ciphertext = _validated_interaction_envelope(interaction, context)
    authenticated_context = interaction_authenticated_context(interaction, envelope)
    expected_aad = interaction_authenticated_data(authenticated_context)
    result = context.provider.process(context.group_id, ciphertext)
    if result.get("kind") != "application":
        raise E2EEProtocolError("interaction MLS message is not application data")
    application = _decode(
        result.get("application"),
        "interaction plaintext",
        maximum=MAX_INTERACTION_PLAINTEXT_BYTES,
    )
    received_aad = _decode(
        result.get("aad"),
        "interaction authenticated data",
        maximum=4096,
    )
    if not hmac.compare_digest(received_aad, expected_aad):
        raise E2EEProtocolError("interaction authenticated context was modified")
    credential = _decode(
        result.get("credential"),
        "interaction sender credential",
        maximum=MAX_CREDENTIAL_BYTES,
    )
    _validate_human_credential(credential, interaction.user.ref)
    try:
        plaintext = json.loads(application)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EEProtocolError("interaction plaintext is invalid") from exc
    if (
        not isinstance(plaintext, dict)
        or set(plaintext) != {"version", "kind", "context", "data"}
        or plaintext.get("version") != 1
        or plaintext.get("kind") != "interaction"
        or plaintext.get("context") != authenticated_context
        or not isinstance(plaintext.get("data"), dict)
        or set(cast(dict[str, object], plaintext["data"]))
        != {"attachments", "options", "values", "components"}
    ):
        raise E2EEProtocolError("interaction plaintext context is invalid")
    expected_attachment_ids = set(
        cast(list[str], authenticated_context["attachment_ids"])
    )
    plaintext_data = cast(dict[str, object], plaintext["data"])
    manifests = _validated_attachment_manifests(
        interaction,
        envelope,
        plaintext_data["attachments"],
        expected_attachment_ids,
    )
    validated = _validate_decrypted_interaction_data(
        interaction,
        plaintext_data,
        expected_attachment_ids,
        manifests,
    )
    context.record_ciphertext(ciphertext, interaction.id)
    return validated


def _invalid_interaction(message: str) -> NoReturn:
    raise E2EEProtocolError(f"decrypted interaction is invalid: {message}")


def _canonical_attachment_id(value: object) -> str:
    rendered = str(value)
    if (
        isinstance(value, bool)
        or not rendered.isascii()
        or not rendered.isdecimal()
        or rendered.startswith("0")
        or int(rendered) > (1 << 63) - 1
    ):
        _invalid_interaction("an attachment option is invalid")
    return rendered


def _manifest_base64(value: object, *, label: str, length: int) -> None:
    if not isinstance(value, str):
        _invalid_interaction(f"an attachment manifest has an invalid {label}")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, UnicodeEncodeError):
        _invalid_interaction(f"an attachment manifest has an invalid {label}")
    if len(decoded) != length or _b64(decoded) != value:
        _invalid_interaction(f"an attachment manifest has an invalid {label}")


def _validated_attachment_manifests(
    interaction: Interaction,
    envelope: Mapping[str, object],
    value: object,
    expected_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != expected_ids:
        _invalid_interaction("attachment manifests do not match the bound capabilities")
    manifests = cast(dict[str, object], value)
    digest = envelope.get("attachment_manifest_digest")
    if not expected_ids:
        if digest is not None:
            _invalid_interaction(
                "an attachment manifest digest was supplied without files"
            )
        return {}
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest,
        interaction_attachment_manifest_digest(
            {
                attachment_id: cast(Mapping[str, object], manifest)
                for attachment_id, manifest in manifests.items()
                if isinstance(manifest, dict)
            }
        ),
    ):
        _invalid_interaction(
            "attachment manifest digest does not match the MLS plaintext"
        )
    resolved = interaction.resolved or {}
    resolved_attachments = resolved.get("attachments")
    if not isinstance(resolved_attachments, dict):
        _invalid_interaction("resolved interaction attachments are invalid")
    required = {
        "version",
        "protocol",
        "file_id",
        "key",
        "filename",
        "content_type",
        "plaintext_size",
        "ciphertext_size",
        "ciphertext_sha256",
        "chunk_size",
        "attachment_id",
        "attachment_domain",
    }
    validated: dict[str, dict[str, Any]] = {}
    for attachment_id, raw_manifest in manifests.items():
        if not isinstance(raw_manifest, dict) or set(raw_manifest) != required:
            _invalid_interaction("an attachment manifest has invalid fields")
        manifest = cast(dict[str, Any], raw_manifest)
        if manifest.get("version") != 1 or manifest.get("protocol") != "kaede-file-v1":
            _invalid_interaction("an attachment manifest has an unsupported protocol")
        if manifest.get("attachment_id") != attachment_id:
            _invalid_interaction("an attachment manifest changes its bound capability")
        attachment_domain = manifest.get("attachment_domain")
        if not isinstance(attachment_domain, str):
            _invalid_interaction("an attachment manifest has an invalid authority")
        try:
            EntityRef.parse(f"{attachment_id}@{attachment_domain}")
        except ValueError:
            _invalid_interaction("an attachment manifest has an invalid authority")
        filename = manifest.get("filename")
        content_type = manifest.get("content_type")
        if (
            not isinstance(filename, str)
            or not 1 <= len(filename) <= 255
            or filename != filename.strip()
            or any(
                ord(character) < 32 or ord(character) == 127 for character in filename
            )
            or not isinstance(content_type, str)
            or not 1 <= len(content_type) <= 100
            or content_type != content_type.lower()
            or CONTENT_TYPE_RE.fullmatch(content_type) is None
        ):
            _invalid_interaction("an attachment manifest has invalid original metadata")
        plaintext_size = manifest.get("plaintext_size")
        ciphertext_size = manifest.get("ciphertext_size")
        chunk_size = manifest.get("chunk_size")
        if (
            isinstance(plaintext_size, bool)
            or not isinstance(plaintext_size, int)
            or not 1 <= plaintext_size <= MAX_ENCRYPTED_FILE_BYTES
            or isinstance(ciphertext_size, bool)
            or not isinstance(ciphertext_size, int)
            or isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or not 64 * 1024 <= chunk_size <= 1024 * 1024
        ):
            _invalid_interaction("an attachment manifest has invalid sizes")
        chunk_count = (plaintext_size + chunk_size - 1) // chunk_size
        if ciphertext_size != plaintext_size + 41 + chunk_count * 20:
            _invalid_interaction("an attachment manifest has inconsistent framing")
        _manifest_base64(manifest.get("file_id"), label="file ID", length=16)
        _manifest_base64(manifest.get("key"), label="file key", length=32)
        _manifest_base64(
            manifest.get("ciphertext_sha256"),
            label="ciphertext digest",
            length=32,
        )
        resolved_attachment = resolved_attachments.get(attachment_id)
        if (
            not isinstance(resolved_attachment, dict)
            or str(resolved_attachment.get("id")) != attachment_id
            or resolved_attachment.get("origin_domain") != attachment_domain
            or resolved_attachment.get("filename") != "encrypted-file"
            or resolved_attachment.get("content_type") != "application/octet-stream"
            or resolved_attachment.get("encryption_mode") != "e2ee"
            or resolved_attachment.get("encryption_protocol") != "kaede-file-v1"
            or resolved_attachment.get("size") != ciphertext_size
        ):
            _invalid_interaction(
                "an attachment manifest does not match authority metadata"
            )
        validated[attachment_id] = dict(manifest)
    return validated


def _resolved_channel_type(interaction: Interaction, reference: str) -> int | None:
    resolved = interaction.resolved or {}
    channels = resolved.get("channels")
    if not isinstance(channels, dict):
        return None
    item = channels.get(reference)
    if not isinstance(item, dict):
        return None
    channel_type = item.get("type")
    return (
        channel_type
        if isinstance(channel_type, int) and not isinstance(channel_type, bool)
        else None
    )


def _validate_choice(definition: Mapping[str, object], value: object) -> None:
    choices = definition.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return
    option_type = definition.get("type")
    for choice in choices:
        if not isinstance(choice, dict) or choice.get("value") != value:
            continue
        selected = choice.get("value")
        if (
            option_type == "number"
            and not isinstance(selected, bool)
            and isinstance(selected, (int, float))
        ):
            return
        if type(selected) is type(value):
            return
    _invalid_interaction("an option is not one of the command's choices")


def _validate_numeric_option(
    definition: Mapping[str, object],
    value: object,
    *,
    integer: bool,
) -> int | float:
    valid = (
        not isinstance(value, bool)
        and (
            isinstance(value, int)
            if integer
            else isinstance(value, (int, float)) and math.isfinite(value)
        )
        and (
            -(2**53) + 1 <= cast(int, value) <= 2**53 - 1
            if integer
            else -(2**53) <= cast(int | float, value) <= 2**53
        )
    )
    if not valid:
        _invalid_interaction("a numeric option has the wrong type or range")
    number = cast(int | float, value)
    minimum = definition.get("min_value")
    maximum = definition.get("max_value")
    if isinstance(minimum, (int, float)) and number < minimum:
        _invalid_interaction("a numeric option is below its minimum")
    if isinstance(maximum, (int, float)) and number > maximum:
        _invalid_interaction("a numeric option is above its maximum")
    return number


def _attachment_matches_file_types(
    attachment_id: str,
    accepted: object,
    manifests: Mapping[str, Mapping[str, object]],
) -> bool:
    if not isinstance(accepted, list) or not accepted:
        return True
    item = manifests.get(attachment_id)
    if not isinstance(item, dict):
        return False
    filename = item.get("filename")
    content_type = item.get("content_type")
    if not isinstance(filename, str) or not isinstance(content_type, str):
        return False
    lowered_name = filename.lower()
    lowered_type = content_type.lower()
    return any(
        isinstance(value, str)
        and (
            (
                value in {"image", "video", "audio"}
                and lowered_type.startswith(f"{value}/")
            )
            or (value.startswith(".") and lowered_name.endswith(value.lower()))
        )
        for value in accepted
    )


def _validate_leaf_option(
    interaction: Interaction,
    definition: Mapping[str, object],
    value: object,
    manifests: Mapping[str, Mapping[str, object]],
) -> tuple[object, set[str]]:
    option_type = definition.get("type")
    attachments: set[str] = set()
    if option_type == "string":
        if not isinstance(value, str):
            _invalid_interaction("a string option has the wrong type")
        minimum = definition.get("min_length", 0)
        maximum = definition.get("max_length", 6000)
        if (
            not isinstance(minimum, int)
            or not isinstance(maximum, int)
            or not minimum <= len(value) <= maximum
        ):
            _invalid_interaction("a string option violates its length bounds")
        normalized: object = value
    elif option_type in ("integer", "number"):
        normalized = _validate_numeric_option(
            definition,
            value,
            integer=option_type == "integer",
        )
    elif option_type == "boolean":
        if not isinstance(value, bool):
            _invalid_interaction("a boolean option has the wrong type")
        normalized = value
    elif option_type in ("user", "channel", "role", "mentionable"):
        if not isinstance(value, str):
            _invalid_interaction("an entity option has the wrong type")
        try:
            EntityRef.parse(value)
        except ValueError:
            _invalid_interaction("an entity option is not a canonical reference")
        if option_type == "channel":
            allowed = definition.get("channel_types", [])
            resolved_type = _resolved_channel_type(interaction, value)
            if (
                isinstance(allowed, list)
                and allowed
                and resolved_type is not None
                and resolved_type not in allowed
            ):
                _invalid_interaction("a channel option has a disallowed channel type")
        normalized = value
    elif option_type == "attachment":
        attachment_id = _canonical_attachment_id(value)
        if not _attachment_matches_file_types(
            attachment_id,
            definition.get("file_types", []),
            manifests,
        ):
            _invalid_interaction(
                "an attachment does not match the command's file types"
            )
        attachments.add(attachment_id)
        normalized = attachment_id
    else:
        _invalid_interaction("the command contains an unsupported option type")
    _validate_choice(definition, normalized)
    return normalized, attachments


def _validate_option_level(
    interaction: Interaction,
    definitions: object,
    supplied: object,
    *,
    require_complete: bool,
    manifests: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], set[str]]:
    if not isinstance(definitions, list) or not isinstance(supplied, dict):
        _invalid_interaction("command options must be an object")
    declared: dict[str, dict[str, object]] = {}
    for item in definitions:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            _invalid_interaction("the command definition is invalid")
        declared[cast(str, item["name"])] = cast(dict[str, object], item)
    if len(declared) != len(definitions) or set(supplied) - set(declared):
        _invalid_interaction("command options do not match the registered definition")
    containers = {
        name: item
        for name, item in declared.items()
        if item.get("type") in {"subcommand", "subcommand_group"}
    }
    if containers:
        selected = [name for name in containers if name in supplied]
        if len(selected) != 1 or len(supplied) != 1:
            _invalid_interaction("exactly one subcommand must be selected")
        name = selected[0]
        nested, nested_attachments = _validate_option_level(
            interaction,
            containers[name].get("options", []),
            supplied[name],
            require_complete=require_complete,
            manifests=manifests,
        )
        return {name: nested}, nested_attachments
    normalized: dict[str, object] = {}
    attachments: set[str] = set()
    for name, definition in declared.items():
        if name not in supplied:
            if require_complete and definition.get("required") is True:
                _invalid_interaction("a required command option is missing")
            continue
        value, selected_attachments = _validate_leaf_option(
            interaction,
            definition,
            supplied[name],
            manifests,
        )
        normalized[name] = value
        attachments.update(selected_attachments)
    return normalized, attachments


def _source_option_digests(value: object) -> set[str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) for item in value)
        or value != sorted(value)
        or len(value) != len(set(value))
    ):
        _invalid_interaction("source option commitments are invalid")
    for item in cast(list[str], value):
        try:
            _wire_b64(
                item,
                "source option commitment",
                maximum=32,
                exact=32,
            )
        except E2EEProtocolError:
            _invalid_interaction("source option commitments are invalid")
    return set(cast(list[str], value))


def _component_values(
    source: Mapping[str, object],
    values: object,
    manifests: Mapping[str, Mapping[str, object]],
) -> tuple[tuple[str, ...], set[str]]:
    if not isinstance(values, list) or any(
        not isinstance(item, str) for item in values
    ):
        _invalid_interaction("component values must be strings")
    normalized = tuple(cast(list[str], values))
    if len(normalized) > 25 or len(normalized) != len(set(normalized)):
        _invalid_interaction("component values are duplicated or too numerous")
    component_type = source.get("type")
    if component_type == 2:
        if normalized:
            _invalid_interaction("buttons cannot submit values")
        return normalized, set()
    if component_type not in {3, 5, 6, 7, 8, 19, 22}:
        _invalid_interaction("component type is not an interactive selection")
    minimum = source.get("min_values", 1)
    maximum = source.get("max_values", 1)
    if (
        not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or not minimum <= len(normalized) <= maximum
    ):
        _invalid_interaction("component value count is out of bounds")
    if component_type in {3, 22}:
        allowed_digests = _source_option_digests(source.get("option_value_digests"))
        if allowed_digests is not None:
            supplied_digests = {
                _b64(hashlib.sha256(item.encode("utf-8")).digest())
                for item in normalized
            }
            available = supplied_digests <= allowed_digests
        else:
            options = source.get("options", [])
            allowed_values = (
                {
                    option.get("value")
                    for option in options
                    if isinstance(option, dict) and isinstance(option.get("value"), str)
                }
                if isinstance(options, list)
                else set()
            )
            available = set(normalized) <= allowed_values
        if not available:
            _invalid_interaction("component selected an unavailable value")
    if component_type in {5, 6, 7, 8}:
        try:
            for value in normalized:
                EntityRef.parse(value)
        except ValueError:
            _invalid_interaction("component selected an invalid entity")
    if component_type == 19:
        accepted = source.get("file_types", [])
        if any(
            not _attachment_matches_file_types(attachment_id, accepted, manifests)
            for attachment_id in normalized
        ):
            _invalid_interaction("a modal attachment does not match its file types")
        return normalized, set(normalized)
    return normalized, set()


def _source_modal_fields(
    source_modal: Mapping[str, object],
) -> list[Mapping[str, object]]:
    rows = source_modal.get("components")
    if not isinstance(rows, list):
        _invalid_interaction("source modal is invalid")
    fields: list[Mapping[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            _invalid_interaction("source modal is invalid")
        if row.get("type") == 10:
            continue
        if row.get("type") == 18 and isinstance(row.get("component"), dict):
            fields.append(cast(dict[str, object], row["component"]))
        elif (
            row.get("type") == 1
            and isinstance(row.get("components"), list)
            and len(cast(list[object], row["components"])) == 1
            and isinstance(cast(list[object], row["components"])[0], dict)
        ):
            fields.append(
                cast(dict[str, object], cast(list[object], row["components"])[0])
            )
        else:
            _invalid_interaction("source modal layout is invalid")
    return fields


def _submitted_modal_field(
    source: Mapping[str, object],
    submitted: object,
    manifests: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], set[str]]:
    if not isinstance(submitted, dict):
        _invalid_interaction("a modal field is invalid")
    if submitted.get("type") != source.get("type") or submitted.get(
        "custom_id"
    ) != source.get("custom_id"):
        _invalid_interaction("a modal field does not match its source")
    component_type = source.get("type")
    if component_type == 4:
        if set(submitted) != {"type", "custom_id", "value"} or not isinstance(
            submitted.get("value"), str
        ):
            _invalid_interaction("a modal text field is invalid")
        value = cast(str, submitted["value"])
        minimum = source.get("min_length", 0) or 0
        maximum = source.get("max_length", 4000) or 4000
        if source.get("required") is True and not value:
            _invalid_interaction("a required modal field is empty")
        if (
            not isinstance(minimum, int)
            or not isinstance(maximum, int)
            or not minimum <= len(value) <= maximum
        ):
            _invalid_interaction("a modal text field violates its length bounds")
        return dict(submitted), set()
    if component_type == 23:
        if set(submitted) != {"type", "custom_id", "value"} or not isinstance(
            submitted.get("value"), bool
        ):
            _invalid_interaction("a modal checkbox is invalid")
        return dict(submitted), set()
    if component_type == 21:
        if set(submitted) != {"type", "custom_id", "value"}:
            _invalid_interaction("a modal radio group is invalid")
        radio_value = submitted.get("value")
        option_digests = _source_option_digests(source.get("option_value_digests"))
        options = source.get("options", [])
        allowed_values = (
            {
                option.get("value")
                for option in options
                if isinstance(option, dict) and isinstance(option.get("value"), str)
            }
            if isinstance(options, list)
            else set()
        )
        digest_allowed = (
            option_digests is not None
            and isinstance(radio_value, str)
            and _b64(hashlib.sha256(radio_value.encode("utf-8")).digest())
            in option_digests
        )
        if (radio_value is None and source.get("required") is True) or (
            radio_value is not None
            and (
                not isinstance(radio_value, str)
                or radio_value not in allowed_values
                and not digest_allowed
            )
        ):
            _invalid_interaction("a modal radio value is invalid")
        return dict(submitted), set()
    if set(submitted) != {"type", "custom_id", "values"}:
        _invalid_interaction("a modal selection is invalid")
    normalized, attachments = _component_values(
        source,
        submitted.get("values"),
        manifests,
    )
    return {
        "type": component_type,
        "custom_id": source.get("custom_id"),
        "values": list(normalized),
    }, attachments


def _validate_modal_components(
    interaction: Interaction,
    components: object,
    manifests: Mapping[str, Mapping[str, object]],
) -> tuple[tuple[dict[str, Any], ...], set[str]]:
    source_modal = interaction.source_modal
    if (
        not isinstance(source_modal, dict)
        or source_modal.get("custom_id") != interaction.custom_id
    ):
        _invalid_interaction("source modal identity is missing")
    source_fields = _source_modal_fields(source_modal)
    if not isinstance(components, list) or len(components) != len(source_fields):
        _invalid_interaction("modal fields do not match the source modal")
    normalized_rows: list[dict[str, Any]] = []
    attachments: set[str] = set()
    for source, submitted_row in zip(source_fields, components, strict=True):
        if not isinstance(submitted_row, dict):
            _invalid_interaction("a modal row is invalid")
        if submitted_row.get("type") == 18 and isinstance(
            submitted_row.get("component"), dict
        ):
            normalized, selected = _submitted_modal_field(
                source,
                submitted_row["component"],
                manifests,
            )
            row: dict[str, Any] = {"type": 18, "component": normalized}
            if "id" in submitted_row:
                row["id"] = submitted_row["id"]
        elif (
            submitted_row.get("type") == 1
            and isinstance(submitted_row.get("components"), list)
            and len(cast(list[object], submitted_row["components"])) == 1
        ):
            normalized, selected = _submitted_modal_field(
                source,
                cast(list[object], submitted_row["components"])[0],
                manifests,
            )
            row = {"type": 1, "components": [normalized]}
        else:
            _invalid_interaction("a modal row does not match the source modal")
        normalized_rows.append(row)
        attachments.update(selected)
    return tuple(normalized_rows), attachments


def _validate_decrypted_interaction_data(
    interaction: Interaction,
    data: Mapping[str, object],
    expected_attachments: set[str],
    manifests: dict[str, dict[str, Any]],
) -> DecryptedInteractionData:
    options = data.get("options")
    values = data.get("values")
    components = data.get("components")
    selected_attachments: set[str] = set()
    if interaction.type in {"command", "autocomplete"}:
        if not isinstance(options, dict) or values != [] or components != []:
            _invalid_interaction("command data fields are invalid")
        if not isinstance(interaction.command, dict):
            _invalid_interaction("command definition is missing")
        normalized_options, selected_attachments = _validate_option_level(
            interaction,
            interaction.command.get("options", []),
            options,
            require_complete=interaction.type == "command",
            manifests=manifests,
        )
        normalized_values: tuple[str, ...] = ()
        normalized_components: tuple[dict[str, Any], ...] = ()
    elif interaction.type == "component":
        if (
            options != {}
            or components != []
            or not isinstance(interaction.source_component, dict)
        ):
            _invalid_interaction("component data fields are invalid")
        if (
            interaction.source_component.get("custom_id") != interaction.custom_id
            or interaction.source_component.get("type") != interaction.component_type
        ):
            _invalid_interaction("source component identity does not match the event")
        normalized_values, selected_attachments = _component_values(
            interaction.source_component,
            values,
            manifests,
        )
        normalized_options = {}
        normalized_components = ()
    elif interaction.type == "modal_submit":
        if options != {} or values != []:
            _invalid_interaction("modal data fields are invalid")
        normalized_components, selected_attachments = _validate_modal_components(
            interaction,
            components,
            manifests,
        )
        normalized_options = {}
        normalized_values = ()
    else:
        _invalid_interaction("interaction type is unsupported")
    if selected_attachments != expected_attachments:
        _invalid_interaction(
            "attachment references do not match the bound capabilities"
        )
    return DecryptedInteractionData(
        normalized_options,
        normalized_values,
        normalized_components,
        manifests,
    )


_MESSAGE_RICH_DATA_FIELDS = frozenset(
    {
        "content",
        "embeds",
        "components",
        "poll",
        "sticker_items",
        "tts",
        "voice_message",
        "flags",
        "attachments",
        "allowed_mentions",
        "forward_snapshot",
    }
)
_MESSAGE_FILE_MANIFEST_FIELDS = frozenset(
    {
        "version",
        "protocol",
        "file_id",
        "key",
        "filename",
        "content_type",
        "plaintext_size",
        "ciphertext_size",
        "ciphertext_sha256",
        "plaintext_sha256",
        "chunk_size",
        "attachment_id",
        "attachment_domain",
    }
)
_MESSAGE_FILE_MANIFEST_VOICE_FIELDS = frozenset({"duration_millis", "waveform"})
_FORWARD_SNAPSHOT_FIELDS = frozenset(
    {
        "content",
        "embeds",
        "components",
        "attachments",
        "mention_user_refs",
        "sticker_items",
        "message_snapshots",
        "message_type",
        "flags",
        "created_at",
        "edited_at",
    }
)
_FORWARDABLE_MESSAGE_TYPES = frozenset({0, 19, 20, 23})
_FORWARD_SNAPSHOT_FLAG_MASK = (1 << 2) | (1 << 13) | (1 << 15)


def _message_file_manifest(value: Mapping[str, object]) -> dict[str, object]:
    fields = set(value)
    if (
        fields
        not in {
            _MESSAGE_FILE_MANIFEST_FIELDS,
            _MESSAGE_FILE_MANIFEST_FIELDS | _MESSAGE_FILE_MANIFEST_VOICE_FIELDS,
        }
        or value.get("version") != 1
        or value.get("protocol") != "kaede-file-v1"
    ):
        raise ValueError("encrypted attachment manifest fields are invalid")
    filename = value.get("filename")
    content_type = value.get("content_type")
    plaintext_size = value.get("plaintext_size")
    ciphertext_size = value.get("ciphertext_size")
    chunk_size = value.get("chunk_size")
    if (
        not isinstance(filename, str)
        or not 1 <= len(filename) <= 255
        or filename != filename.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        or not isinstance(content_type, str)
        or not 1 <= len(content_type) <= 100
        or content_type != content_type.lower()
        or CONTENT_TYPE_RE.fullmatch(content_type) is None
        or isinstance(plaintext_size, bool)
        or not isinstance(plaintext_size, int)
        or not 1 <= plaintext_size <= MAX_ENCRYPTED_FILE_BYTES
        or isinstance(ciphertext_size, bool)
        or not isinstance(ciphertext_size, int)
        or isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or not 64 * 1024 <= chunk_size <= 1024 * 1024
    ):
        raise ValueError("encrypted attachment manifest metadata is invalid")
    chunks = (plaintext_size + chunk_size - 1) // chunk_size
    if ciphertext_size != plaintext_size + ENCRYPTED_FILE_HEADER_SIZE + chunks * 20:
        raise ValueError("encrypted attachment manifest framing is invalid")
    for name, length in (
        ("file_id", 16),
        ("key", 32),
        ("ciphertext_sha256", 32),
        ("plaintext_sha256", 32),
    ):
        raw = value.get(name)
        try:
            decoded = _decode(raw, f"encrypted attachment {name}", maximum=length)
        except E2EEProtocolError as exc:
            raise ValueError(
                "encrypted attachment manifest cryptography is invalid"
            ) from exc
        if len(decoded) != length:
            raise ValueError("encrypted attachment manifest cryptography is invalid")
    attachment_id = value.get("attachment_id")
    attachment_domain = value.get("attachment_domain")
    try:
        attachment_ref = EntityRef.parse(f"{attachment_id}@{attachment_domain}")
    except ValueError as exc:
        raise ValueError("encrypted attachment manifest identity is invalid") from exc
    if (
        str(attachment_id) != str(attachment_ref.id)
        or attachment_domain != attachment_ref.domain
    ):
        raise ValueError("encrypted attachment manifest identity is not canonical")
    duration_millis = value.get("duration_millis")
    waveform = value.get("waveform")
    if (duration_millis is None) != (waveform is None):
        raise ValueError("encrypted voice metadata is incomplete")
    if duration_millis is not None:
        if (
            isinstance(duration_millis, bool)
            or not isinstance(duration_millis, int)
            or not 1 <= duration_millis <= 1_200_000
            or not isinstance(waveform, str)
            or not 4 <= len(waveform) <= 344
        ):
            raise ValueError("encrypted voice metadata is invalid")
        try:
            samples = base64.b64decode(waveform, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("encrypted voice waveform is invalid") from exc
        if (
            not 1 <= len(samples) <= 256
            or base64.b64encode(samples).decode() != waveform
        ):
            raise ValueError("encrypted voice waveform is invalid")
    return dict(value)


def _stable_file_manifest(value: Mapping[str, object]) -> dict[str, object]:
    manifest = _message_file_manifest(value)
    return {
        "filename": manifest["filename"],
        "content_type": manifest["content_type"],
        "plaintext_size": manifest["plaintext_size"],
        "plaintext_sha256": manifest["plaintext_sha256"],
        **(
            {
                "duration_millis": manifest["duration_millis"],
                "waveform": manifest["waveform"],
            }
            if "duration_millis" in manifest and "waveform" in manifest
            else {}
        ),
    }


def _stable_forward_attachment(value: Mapping[str, object]) -> dict[str, object]:
    """Strip either encrypted-manifest or plaintext-snapshot transport bindings."""

    if value.get("protocol") == "kaede-file-v1":
        return _stable_file_manifest(value)
    required = {
        "id",
        "origin_domain",
        "filename",
        "content_type",
        "size",
        "plaintext_sha256",
    }
    if not required <= set(value):
        raise ValueError("forward snapshot attachment fields are invalid")
    filename = value.get("filename")
    content_type = value.get("content_type")
    size = value.get("size")
    plaintext_sha256 = value.get("plaintext_sha256")
    if (
        not isinstance(filename, str)
        or not 1 <= len(filename) <= 255
        or not isinstance(content_type, str)
        or not 1 <= len(content_type) <= 100
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 <= size <= MAX_ENCRYPTED_FILE_BYTES
    ):
        raise ValueError("forward snapshot attachment metadata is invalid")
    try:
        if (
            len(
                _decode(
                    plaintext_sha256,
                    "forward attachment plaintext digest",
                    maximum=32,
                )
            )
            != 32
        ):
            raise ValueError("forward attachment plaintext digest is invalid")
    except E2EEProtocolError as exc:
        raise ValueError("forward attachment plaintext digest is invalid") from exc
    duration_secs = value.get("duration_secs")
    waveform = value.get("waveform")
    if (duration_secs is None) != (waveform is None):
        raise ValueError("forward voice metadata is incomplete")
    voice: dict[str, object] = {}
    if duration_secs is not None:
        if (
            isinstance(duration_secs, bool)
            or not isinstance(duration_secs, (int, float))
            or not 0 < duration_secs <= 1_200
            or not isinstance(waveform, str)
        ):
            raise ValueError("forward voice metadata is invalid")
        voice = {
            "duration_millis": round(float(duration_secs) * 1000),
            "waveform": waveform,
        }
    return {
        "filename": filename,
        "content_type": content_type,
        "plaintext_size": size,
        "plaintext_sha256": plaintext_sha256,
        **voice,
    }


def _canonical_message_mentions(values: Sequence[EntityRef | str]) -> list[str]:
    mentions = sorted(
        str(item if isinstance(item, EntityRef) else EntityRef.parse(item))
        for item in values
    )
    if len(mentions) > 5_000 or len(mentions) != len(set(mentions)):
        raise ValueError("encrypted message mention references are invalid")
    return mentions


def _message_allowed_mentions(value: object) -> dict[str, object]:
    """Normalize the notification policy carried only inside rich ciphertext."""

    if not isinstance(value, Mapping) or set(value) != {
        "parse",
        "users",
        "roles",
        "replied_user",
    }:
        raise ValueError("encrypted message allowed mentions are invalid")
    raw_parse = value.get("parse")
    if (
        not isinstance(raw_parse, Sequence)
        or isinstance(raw_parse, (str, bytes))
        or any(item not in {"everyone", "roles", "users"} for item in raw_parse)
    ):
        raise ValueError("encrypted message allowed mentions are invalid")
    parse = list(cast(Sequence[str], raw_parse))
    if parse != sorted(parse) or len(parse) != len(set(parse)):
        raise ValueError("encrypted message allowed mention parsing is not canonical")
    raw_users = value.get("users")
    raw_roles = value.get("roles")
    if (
        not isinstance(raw_users, Sequence)
        or isinstance(raw_users, (str, bytes))
        or not isinstance(raw_roles, Sequence)
        or isinstance(raw_roles, (str, bytes))
    ):
        raise ValueError("encrypted message allowed mention references are invalid")
    users = _canonical_message_mentions(cast(Sequence[EntityRef | str], raw_users))
    roles = _canonical_message_mentions(cast(Sequence[EntityRef | str], raw_roles))
    if len(users) > 100 or len(roles) > 100:
        raise ValueError("encrypted message allowed mentions are too large")
    if ("users" in parse and users) or ("roles" in parse and roles):
        raise ValueError("encrypted message allowed mention policy overlaps")
    replied_user = value.get("replied_user")
    if not isinstance(replied_user, bool):
        raise ValueError("encrypted message reply mention policy is invalid")
    return {
        "parse": parse,
        "users": users,
        "roles": roles,
        "replied_user": replied_user,
    }


def _message_mention_texts(data: Mapping[str, object]) -> list[str]:
    """Return only fields whose Discord-compatible rendering can notify."""

    texts: list[str] = []
    content = data.get("content")
    if isinstance(content, str):
        texts.append(content)

    def walk_component(value: object) -> None:
        if isinstance(value, Mapping):
            if value.get("type") == 10 and isinstance(value.get("content"), str):
                texts.append(cast(str, value["content"]))
            for nested in value.values():
                walk_component(nested)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for nested in value:
                walk_component(nested)

    walk_component(data.get("components"))
    return texts


def _message_mention_intent(
    data: Mapping[str, object],
) -> tuple[list[str], list[str], bool]:
    policy = _message_allowed_mentions(data.get("allowed_mentions"))
    texts = _message_mention_texts(data)
    if any(UNQUALIFIED_USER_MENTION_RE.search(text) is not None for text in texts):
        raise ValueError("encrypted user mention tokens must be origin-qualified")
    visible_users = {
        str(EntityRef.parse(f"{match.group('id')}@{match.group('domain').lower()}"))
        for text in texts
        for match in QUALIFIED_USER_MENTION_RE.finditer(text)
    }
    visible_roles = {
        str(EntityRef.parse(f"{match.group('id')}@{match.group('domain').lower()}"))
        for text in texts
        for match in QUALIFIED_ROLE_MENTION_RE.finditer(text)
    }
    parse = set(cast(list[str], policy["parse"]))
    explicit_users = set(cast(list[str], policy["users"]))
    explicit_roles = set(cast(list[str], policy["roles"]))
    users = visible_users if "users" in parse else visible_users & explicit_users
    roles = visible_roles if "roles" in parse else visible_roles & explicit_roles
    everyone = "everyone" in parse and any(
        BROAD_MENTION_RE.search(text) is not None for text in texts
    )
    return sorted(users), sorted(roles), everyone


def message_sticker_routing_refs(data: Mapping[str, object]) -> list[str]:
    """Return the sorted routing union for outer and forwarded stickers."""

    refs: set[str] = set()

    def collect_items(raw_items: object) -> None:
        if not isinstance(raw_items, list) or len(raw_items) > 3:
            raise ValueError("encrypted message sticker items are invalid")
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise ValueError("encrypted message sticker items are invalid")
            try:
                ref = EntityRef.parse(f"{raw.get('id')}@{raw.get('origin_domain')}")
            except ValueError as exc:
                raise ValueError("encrypted message sticker items are invalid") from exc
            if (
                str(raw.get("id")) != str(ref.id)
                or raw.get("origin_domain") != ref.domain
            ):
                raise ValueError("encrypted message sticker items are invalid")
            refs.add(str(ref))

    def collect_snapshot(raw: object, *, nested: bool = False) -> None:
        if not isinstance(raw, Mapping):
            raise ValueError("encrypted message forward snapshot is invalid")
        collect_items(raw.get("sticker_items"))
        snapshots = raw.get("message_snapshots")
        if not isinstance(snapshots, list) or len(snapshots) > (0 if nested else 1):
            raise ValueError("encrypted message forward snapshot is invalid")
        for item in snapshots:
            collect_snapshot(item, nested=True)

    collect_items(data.get("sticker_items"))
    forward_snapshot = data.get("forward_snapshot")
    if forward_snapshot is not None:
        collect_snapshot(forward_snapshot)
    result = sorted(refs)
    if len(result) > 9:
        raise ValueError("encrypted message sticker items are invalid")
    return result


def _message_custom_emoji_refs(data: Mapping[str, object]) -> list[str]:
    """Collect canonical custom-emoji tokens from every encrypted rich field."""

    refs: set[str] = set()

    def add_token(value: str) -> None:
        for match in CUSTOM_EMOJI_ROUTING_RE.finditer(value):
            ref = EntityRef.parse(f"{match.group('id')}@{match.group('domain')}")
            if ref.domain != match.group("domain"):
                raise ValueError(
                    "encrypted message custom emoji domain is not canonical"
                )
            prefix = "a" if match.group("animated") else ""
            refs.add(f"<{prefix}:{match.group('name')}:{ref}>")

    def walk(value: object) -> None:
        if isinstance(value, str):
            add_token(value)
            return
        if isinstance(value, Mapping):
            raw_id = value.get("id")
            raw_name = value.get("name")
            raw_animated = value.get("animated", False)
            if (
                isinstance(raw_id, str)
                and "@" in raw_id
                and isinstance(raw_name, str)
                and re.fullmatch(r"[A-Za-z0-9_]{2,32}", raw_name) is not None
                and isinstance(raw_animated, bool)
            ):
                try:
                    ref = EntityRef.parse(raw_id)
                except ValueError as exc:
                    raise ValueError(
                        "encrypted message custom emoji is invalid"
                    ) from exc
                if str(ref) != raw_id:
                    raise ValueError("encrypted message custom emoji is not canonical")
                prefix = "a" if raw_animated else ""
                refs.add(f"<{prefix}:{raw_name}:{ref}>")
            for nested in value.values():
                walk(nested)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for nested in value:
                walk(nested)

    walk(data)
    result = sorted(refs)
    if len(result) > 256:
        raise ValueError("encrypted message has too many custom emoji references")
    return result


def _message_rich_data(value: Mapping[str, object]) -> dict[str, object]:
    """Copy and bound the canonical rich plaintext without weakening its shape."""

    if set(value) != _MESSAGE_RICH_DATA_FIELDS:
        raise ValueError("encrypted rich message data fields are invalid")
    content = value.get("content")
    if content is not None and (
        not isinstance(content, str) or not 1 <= len(content) <= 4000
    ):
        raise ValueError("encrypted rich message content is invalid")
    collections: dict[str, list[dict[str, object]]] = {}
    for field_name, maximum in (
        ("embeds", 10),
        ("components", 40),
        ("sticker_items", 3),
    ):
        raw_items = value.get(field_name)
        if (
            not isinstance(raw_items, Sequence)
            or isinstance(raw_items, (str, bytes))
            or len(raw_items) > maximum
            or any(not isinstance(item, Mapping) for item in raw_items)
        ):
            raise ValueError(f"encrypted rich message {field_name} are invalid")
        collections[field_name] = [
            dict(cast(Mapping[str, object], item)) for item in raw_items
        ]
    raw_attachments = value.get("attachments")
    if (
        not isinstance(raw_attachments, Sequence)
        or isinstance(raw_attachments, (str, bytes))
        or len(raw_attachments) > 10
        or any(not isinstance(item, Mapping) for item in raw_attachments)
    ):
        raise ValueError("encrypted rich message attachments are invalid")
    collections["attachments"] = [
        _message_file_manifest(cast(Mapping[str, object], item))
        for item in raw_attachments
    ]
    poll = value.get("poll")
    if poll is not None and not isinstance(poll, Mapping):
        raise ValueError("encrypted rich message poll is invalid")
    forward_snapshot = value.get("forward_snapshot")
    if forward_snapshot is not None and not isinstance(forward_snapshot, Mapping):
        raise ValueError("encrypted rich message forward snapshot is invalid")
    tts = value.get("tts")
    voice_message = value.get("voice_message")
    flags = value.get("flags")
    if (
        not isinstance(tts, bool)
        or not isinstance(voice_message, bool)
        or tts
        and voice_message
        or isinstance(flags, bool)
        or not isinstance(flags, int)
        or not 0 <= flags <= 2_147_483_647
        or voice_message
        and len(collections["attachments"]) != 1
    ):
        raise ValueError("encrypted rich message delivery metadata is invalid")
    if voice_message and collections["attachments"][0].get("duration_millis") is None:
        raise ValueError("encrypted voice message is missing authenticated metadata")
    allowed_mentions = _message_allowed_mentions(value.get("allowed_mentions"))
    return {
        "content": content,
        "embeds": collections["embeds"],
        "components": collections["components"],
        "poll": dict(cast(Mapping[str, object], poll)) if poll is not None else None,
        "sticker_items": collections["sticker_items"],
        "tts": tts,
        "voice_message": voice_message,
        "flags": flags,
        "attachments": collections["attachments"],
        "allowed_mentions": allowed_mentions,
        "forward_snapshot": (
            dict(cast(Mapping[str, object], forward_snapshot))
            if forward_snapshot is not None
            else None
        ),
    }


def message_rich_payload_digest(data: Mapping[str, object]) -> str:
    normalized = _message_rich_data(data)
    return _b64(
        hashlib.sha256(_canonical_json(normalized, label="rich message data")).digest()
    )


def message_attachment_manifest_digest(
    attachments: Sequence[Mapping[str, object]],
) -> str:
    return _b64(
        hashlib.sha256(
            _canonical_json(
                [dict(item) for item in attachments],
                label="message attachment manifests",
            )
        ).digest()
    )


def _forward_projection_from_snapshot(value: Mapping[str, object]) -> dict[str, object]:
    if set(value) not in {
        _FORWARD_SNAPSHOT_FIELDS,
        _FORWARD_SNAPSHOT_FIELDS - {"edited_at"},
    }:
        raise ValueError("encrypted forward snapshot fields are invalid")
    content = value.get("content")
    embeds = value.get("embeds")
    components = value.get("components")
    attachments = value.get("attachments")
    mentions = value.get("mention_user_refs")
    stickers = value.get("sticker_items")
    nested = value.get("message_snapshots")
    message_type = value.get("message_type")
    flags = value.get("flags")
    created_at = value.get("created_at")
    edited_at = value.get("edited_at")
    if content is not None and (
        not isinstance(content, str) or not 1 <= len(content) <= 4_000
    ):
        raise ValueError("encrypted forward snapshot content is invalid")
    collections = (
        (embeds, 10, "embeds"),
        (components, 40, "components"),
        (stickers, 3, "sticker items"),
    )
    if any(
        not isinstance(items, list)
        or len(items) > maximum
        or any(not isinstance(item, Mapping) for item in items)
        for items, maximum, _label in collections
    ):
        raise ValueError("encrypted forward snapshot rich content is invalid")
    if (
        not isinstance(attachments, list)
        or len(attachments) > 10
        or any(not isinstance(item, Mapping) for item in attachments)
        or not isinstance(mentions, list)
        or len(mentions) > 5_000
        or not isinstance(nested, list)
        or len(nested) > 1
        or any(not isinstance(item, Mapping) for item in nested)
        or isinstance(message_type, bool)
        or message_type not in _FORWARDABLE_MESSAGE_TYPES
        or isinstance(flags, bool)
        or not isinstance(flags, int)
        or flags & ~_FORWARD_SNAPSHOT_FLAG_MASK
        or not isinstance(created_at, str)
        or edited_at is not None
        and not isinstance(edited_at, str)
    ):
        raise ValueError("encrypted forward snapshot metadata is invalid")
    try:
        created = datetime.fromisoformat(created_at)
        edited = (
            datetime.fromisoformat(edited_at) if isinstance(edited_at, str) else None
        )
    except ValueError as exc:
        raise ValueError("encrypted forward snapshot timestamps are invalid") from exc
    if (
        created.tzinfo is None
        or edited is not None
        and (edited.tzinfo is None or edited < created)
    ):
        raise ValueError("encrypted forward snapshot timestamps are invalid")
    normalized_mentions: list[dict[str, str]] = []
    mention_strings: list[str] = []
    for raw in cast(list[object], mentions):
        if not isinstance(raw, Mapping) or set(raw) != {"id", "origin_domain"}:
            raise ValueError("encrypted forward snapshot mentions are invalid")
        try:
            ref = EntityRef.parse(f"{raw.get('id')}@{raw.get('origin_domain')}")
        except ValueError as exc:
            raise ValueError("encrypted forward snapshot mentions are invalid") from exc
        if str(raw.get("id")) != str(ref.id) or raw.get("origin_domain") != ref.domain:
            raise ValueError("encrypted forward snapshot mentions are invalid")
        mention_strings.append(str(ref))
        normalized_mentions.append({"id": str(ref.id), "origin_domain": ref.domain})
    if mention_strings != sorted(mention_strings) or len(mention_strings) != len(
        set(mention_strings)
    ):
        raise ValueError("encrypted forward snapshot mentions are invalid")
    if nested and cast(Mapping[str, object], nested[0]).get("message_snapshots"):
        raise ValueError("encrypted forward snapshot nesting exceeds one level")
    return {
        "version": 2,
        "content": content,
        "embeds": [
            dict(cast(Mapping[str, object], item))
            for item in cast(list[object], embeds)
        ],
        "components": [
            dict(cast(Mapping[str, object], item))
            for item in cast(list[object], components)
        ],
        "attachments": [
            _stable_forward_attachment(cast(Mapping[str, object], item))
            for item in cast(list[object], attachments)
        ],
        "mention_user_refs": normalized_mentions,
        "sticker_items": [
            dict(cast(Mapping[str, object], item))
            for item in cast(list[object], stickers)
        ],
        "message_snapshots": [
            _forward_projection_from_snapshot(cast(Mapping[str, object], item))
            for item in cast(list[object], nested)
        ],
        "flags": flags,
    }


def message_forward_projection_digest(
    data: Mapping[str, object],
    mention_refs: Sequence[EntityRef | str] = (),
) -> str | None:
    """Commit to the source-authenticated author-free body used by a forward."""

    normalized = _message_rich_data(data)
    if normalized["poll"] is not None:
        return None
    mentions = _canonical_message_mentions(mention_refs)
    projection = {
        "version": 2,
        "content": normalized["content"],
        "embeds": normalized["embeds"],
        "components": normalized["components"],
        "attachments": [
            _stable_file_manifest(item)
            for item in cast(list[dict[str, object]], normalized["attachments"])
        ],
        "mention_user_refs": [
            {
                "id": str(EntityRef.parse(item).id),
                "origin_domain": EntityRef.parse(item).domain,
            }
            for item in mentions
        ],
        "sticker_items": normalized["sticker_items"],
        "message_snapshots": (
            [
                _forward_projection_from_snapshot(
                    cast(Mapping[str, object], normalized["forward_snapshot"])
                )
            ]
            if normalized["forward_snapshot"] is not None
            else []
        ),
        "flags": cast(int, normalized["flags"]) & _FORWARD_SNAPSHOT_FLAG_MASK,
    }
    return _b64(
        hashlib.sha256(_canonical_json(projection, label="forward projection")).digest()
    )


def encrypted_forward_snapshot_digest(value: Mapping[str, object]) -> str:
    projection = _forward_projection_from_snapshot(value)
    return _b64(
        hashlib.sha256(_canonical_json(projection, label="forward projection")).digest()
    )


def _rebind_nested_forward_snapshot_attachments(
    snapshot: Mapping[str, object],
    message: Message,
    destination_bindings: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Replace prior-room file bindings while preserving committed file semantics."""

    rebound = dict(snapshot)
    raw_attachments = rebound.get("attachments")
    if not isinstance(raw_attachments, list) or any(
        not isinstance(item, Mapping) for item in raw_attachments
    ):
        raise ValueError("nested forward snapshot attachments are invalid")
    source_indices = {
        str(attachment.ref): index
        for index, attachment in enumerate(message.attachments)
    }
    if len(source_indices) != len(message.attachments):
        raise ValueError("source forward attachment bindings are ambiguous")
    used: set[int] = set()
    replacements: list[dict[str, object]] = []
    for raw in cast(list[Mapping[str, object]], raw_attachments):
        attachment_id = raw.get("attachment_id", raw.get("id"))
        attachment_domain = raw.get("attachment_domain", raw.get("origin_domain"))
        try:
            source_ref = EntityRef.parse(f"{attachment_id}@{attachment_domain}")
        except ValueError as exc:
            raise ValueError("nested forward attachment binding is invalid") from exc
        index = source_indices.get(str(source_ref))
        if index is None or index in used or index >= len(destination_bindings):
            raise ValueError(
                "nested forward attachment is not bound to the source message"
            )
        replacement = dict(destination_bindings[index])
        if _stable_forward_attachment(raw) != _stable_forward_attachment(replacement):
            raise ValueError("nested forward attachment bytes were substituted")
        used.add(index)
        replacements.append(replacement)
    if used != set(range(len(message.attachments))):
        raise ValueError("nested forward snapshot omits a source attachment")
    rebound["attachments"] = replacements
    return rebound


def build_disclosed_forward_snapshot(
    message: Message,
    destination_attachments: Sequence[Attachment],
) -> dict[str, object]:
    """Build the explicit E2EE-to-plaintext disclosure bound to fresh uploads."""

    if (
        message.deleted_at is not None
        or message.created_at is None
        or message.poll is not None
        or message.message_type not in _FORWARDABLE_MESSAGE_TYPES
        or not isinstance(message.e2ee, dict)
        or not isinstance(message.e2ee.get("forward_projection_digest"), str)
        or len(destination_attachments) != len(message.attachments)
    ):
        raise ValueError("this encrypted message cannot be disclosed as a forward")
    bindings: list[dict[str, object]] = []
    for source, destination in zip(
        message.attachments,
        destination_attachments,
        strict=True,
    ):
        if (
            source.encrypted_manifest is None
            or destination.encryption_mode != "plaintext"
        ):
            raise ValueError(
                "disclosed forward attachments require fresh plaintext uploads"
            )
        source_manifest = _message_file_manifest(source.encrypted_manifest)
        binding: dict[str, object] = {
            "id": str(destination.ref.id),
            "origin_domain": destination.ref.domain,
            "filename": destination.filename,
            "content_type": destination.content_type,
            "size": destination.size,
            "plaintext_sha256": source_manifest["plaintext_sha256"],
            "width": destination.width,
            "height": destination.height,
            "duration_secs": destination.duration_secs,
            "waveform": destination.waveform,
            "blurhash": destination.blurhash,
            "scan_status": destination.scan_status,
            "encryption_mode": "plaintext",
            "encryption_protocol": None,
            "variants": dict(destination.variants),
        }
        if _stable_forward_attachment(binding) != _stable_file_manifest(
            source_manifest
        ):
            raise ValueError(
                "disclosed forward attachment bytes or metadata were substituted"
            )
        bindings.append(binding)
    nested = (
        [
            _rebind_nested_forward_snapshot_attachments(
                message.forward_snapshot,
                message,
                bindings,
            )
        ]
        if message.forward_snapshot is not None
        else []
    )
    snapshot: dict[str, object] = {
        "content": message.content,
        "embeds": [dict(item) for item in message.embeds],
        "components": [dict(item) for item in message.components],
        "attachments": bindings,
        "mention_user_refs": [
            {"id": str(item.id), "origin_domain": item.domain}
            for item in sorted(message.mention_user_refs, key=str)
        ],
        "sticker_items": [dict(item) for item in message.sticker_items],
        "message_snapshots": nested,
        "message_type": message.message_type,
        "flags": message.flags & _FORWARD_SNAPSHOT_FLAG_MASK,
        "created_at": message.created_at.astimezone(UTC).isoformat(),
        "edited_at": (
            message.edited_at.astimezone(UTC).isoformat()
            if message.edited_at is not None
            else None
        ),
    }
    if not any(
        (
            snapshot["content"] is not None,
            snapshot["embeds"],
            snapshot["components"],
            snapshot["attachments"],
            snapshot["sticker_items"],
            snapshot["message_snapshots"],
        )
    ):
        raise ValueError("forward snapshot has no body")
    if (
        encrypted_forward_snapshot_digest(snapshot)
        != message.e2ee["forward_projection_digest"]
    ):
        raise ValueError("decrypted source does not match its forward commitment")
    return snapshot


def build_encrypted_forward_snapshot(
    message: Message,
    *,
    attachment_manifests: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Build an author-free snapshot after the source was locally decrypted."""

    if (
        message.deleted_at is not None
        or message.created_at is None
        or message.poll is not None
    ):
        raise ValueError("this message cannot be forwarded")
    if message.message_type not in _FORWARDABLE_MESSAGE_TYPES:
        raise ValueError("this message type cannot be forwarded")
    encrypted_source = isinstance(message.e2ee, dict)
    if encrypted_source and not isinstance(
        cast(dict[str, object], message.e2ee).get("forward_projection_digest"),
        str,
    ):
        raise ValueError("the encrypted source is not safely forwardable")
    manifests = [_message_file_manifest(item) for item in attachment_manifests]
    if len(manifests) != len(message.attachments):
        raise ValueError("forward attachments must be re-uploaded for the destination")
    destination_semantics = [_stable_file_manifest(item) for item in manifests]
    if encrypted_source:
        source_manifests = [
            _message_file_manifest(attachment.encrypted_manifest)
            for attachment in message.attachments
            if attachment.encrypted_manifest is not None
        ]
        if (
            len(source_manifests) != len(message.attachments)
            or [_stable_file_manifest(item) for item in source_manifests]
            != destination_semantics
        ):
            raise ValueError(
                "forward attachments must preserve the decrypted source bytes"
            )
    else:
        for attachment, semantic in zip(
            message.attachments,
            destination_semantics,
            strict=True,
        ):
            expected_voice = (
                {
                    "duration_millis": round(attachment.duration_secs * 1000),
                    "waveform": attachment.waveform,
                }
                if attachment.duration_secs is not None
                and attachment.waveform is not None
                else {}
            )
            if attachment.encryption_mode != "plaintext" or semantic != {
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "plaintext_size": attachment.size,
                # The source authority compares this caller-supplied digest to
                # its stored plaintext hash before admitting the forward.
                "plaintext_sha256": semantic["plaintext_sha256"],
                **expected_voice,
            }:
                raise ValueError(
                    "forward attachments must preserve the plaintext source metadata"
                )
    nested = (
        [
            _rebind_nested_forward_snapshot_attachments(
                message.forward_snapshot,
                message,
                manifests,
            )
        ]
        if message.forward_snapshot is not None
        else []
    )
    if nested and nested[0].get("message_snapshots"):
        raise ValueError("forward snapshot nesting exceeds one level")
    snapshot: dict[str, object] = {
        "content": message.content,
        "embeds": [dict(item) for item in message.embeds],
        "components": [dict(item) for item in message.components],
        "attachments": manifests,
        "mention_user_refs": [
            {"id": str(item.id), "origin_domain": item.domain}
            for item in sorted(message.mention_user_refs, key=str)
        ],
        "sticker_items": [dict(item) for item in message.sticker_items],
        "message_snapshots": nested,
        "message_type": message.message_type,
        "flags": message.flags & _FORWARD_SNAPSHOT_FLAG_MASK,
        "created_at": message.created_at.astimezone(UTC).isoformat(),
        "edited_at": (
            message.edited_at.astimezone(UTC).isoformat()
            if message.edited_at is not None
            else None
        ),
    }
    if not any(
        (
            snapshot["content"] is not None,
            snapshot["embeds"],
            snapshot["components"],
            snapshot["attachments"],
            snapshot["sticker_items"],
            snapshot["message_snapshots"],
        )
    ):
        raise ValueError("forward snapshot has no body")
    if encrypted_source:
        source_digest = cast(
            str, cast(dict[str, object], message.e2ee)["forward_projection_digest"]
        )
        if encrypted_forward_snapshot_digest(snapshot) != source_digest:
            raise ValueError("decrypted source does not match its forward commitment")
    return snapshot


def _message_attachment_refs(
    attachments: Sequence[Mapping[str, object]],
) -> list[str]:
    refs: list[str] = []
    for manifest in attachments:
        attachment_id = manifest.get("attachment_id")
        attachment_domain = manifest.get("attachment_domain")
        try:
            ref = EntityRef.parse(f"{attachment_id}@{attachment_domain}")
        except ValueError as exc:
            raise ValueError(
                "encrypted attachment manifest identity is invalid"
            ) from exc
        if str(attachment_id) != str(ref.id) or attachment_domain != ref.domain:
            raise ValueError("encrypted attachment manifest identity is not canonical")
        refs.append(str(ref))
    if len(refs) != len(set(refs)):
        raise ValueError("encrypted attachment manifests contain duplicate identities")
    return sorted(refs)


def message_rich_authenticated_context(
    channel_ref: EntityRef,
    envelope: Mapping[str, object],
) -> dict[str, object]:
    """Build the exact cross-language ordinary-message MLS context."""

    return {
        "application_ref": envelope.get("application_ref"),
        "attachment_manifest_digest": envelope.get("attachment_manifest_digest"),
        "author_ref": envelope.get("author_ref"),
        "channel_ref": str(channel_ref),
        "epoch": envelope.get("epoch"),
        "group_id": envelope.get("group_id"),
        "interaction_contract_digest": envelope.get("interaction_contract_digest"),
        "interaction_installation_ref": envelope.get("interaction_installation_ref"),
        "interaction_installation_revision": envelope.get(
            "interaction_installation_revision"
        ),
        "interaction_integration_type": envelope.get("interaction_integration_type"),
        "message_attachment_refs": envelope.get("message_attachment_refs"),
        "message_custom_emoji_refs": envelope.get("message_custom_emoji_refs"),
        "message_mention_everyone": envelope.get("message_mention_everyone"),
        "message_mention_refs": envelope.get("message_mention_refs"),
        "message_mention_role_refs": envelope.get("message_mention_role_refs"),
        "message_mention_user_refs": envelope.get("message_mention_user_refs"),
        "message_replied_user_ref": envelope.get("message_replied_user_ref"),
        "message_sticker_refs": envelope.get("message_sticker_refs"),
        "message_flags": envelope.get("message_flags"),
        "message_revision": envelope.get("message_revision"),
        "operation": envelope.get("operation"),
        "policy_generation": envelope.get("policy_generation"),
        "referenced_message_ref": envelope.get("referenced_message_ref"),
        "rich_payload_digest": envelope.get("rich_payload_digest"),
        "sender_device_id": envelope.get("sender_device_id"),
        "target_message": envelope.get("target_message"),
        "tts": envelope.get("tts"),
        "view_persistent": envelope.get("view_persistent"),
        "view_version": envelope.get("view_version"),
        "voice_message": envelope.get("voice_message"),
        "forwarded_message_ref": envelope.get("forwarded_message_ref"),
        "forwarded_channel_ref": envelope.get("forwarded_channel_ref"),
        "forward_snapshot_digest": envelope.get("forward_snapshot_digest"),
        "forward_projection_version": envelope.get("forward_projection_version"),
        "forward_projection_digest": envelope.get("forward_projection_digest"),
        "forward_source_projection_digest": envelope.get(
            "forward_source_projection_digest"
        ),
        "forwarded_created_at": envelope.get("forwarded_created_at"),
        "forwarded_edited_at": envelope.get("forwarded_edited_at"),
        "forwarded_flags": envelope.get("forwarded_flags"),
        "forwarded_message_type": envelope.get("forwarded_message_type"),
    }


def message_rich_authenticated_data(context: Mapping[str, object]) -> bytes:
    return _canonical_json(
        {"context": dict(context), "purpose": MESSAGE_RICH_AAD_PURPOSE},
        label="rich message authenticated data",
    )


def message_rich_plaintext(
    context: Mapping[str, object],
    data: Mapping[str, object],
) -> bytes:
    return _canonical_json(
        {
            "context": dict(context),
            "data": _message_rich_data(data),
            "kind": "message",
            "version": 2,
        },
        label="rich message plaintext",
    )


def encrypt_message(
    context: InteractionE2EEContext,
    data: Mapping[str, object],
    *,
    author_ref: EntityRef,
    sender_device_id: str,
    message_ref: EntityRef | None = None,
    message_revision: int = 1,
    application_ref: EntityRef | None = None,
    interaction_integration_type: Literal[
        "guild_install", "user_install", "dm_capability"
    ]
    | None = None,
    interaction_installation_ref: EntityRef | None = None,
    interaction_installation_revision: int | None = None,
    view_version: int = 0,
    view_persistent: bool = False,
    view_timeout_seconds: int = 900,
    mention_refs: Sequence[EntityRef | str] = (),
    replied_user_ref: EntityRef | None = None,
    referenced_message_ref: EntityRef | None = None,
    forwarded_message_ref: EntityRef | None = None,
    forwarded_channel_ref: EntityRef | None = None,
    forward_source_projection_digest: str | None = None,
) -> EncryptedRichMessage:
    """Encrypt a full ordinary message body with exact routable public bindings."""

    context.require_current()
    normalized = _message_rich_data(data)
    operation: Literal["create", "edit"] = (
        "edit" if message_ref is not None else "create"
    )
    if (
        isinstance(message_revision, bool)
        or not 1 <= message_revision <= (1 << 63) - 1
        or operation == "create"
        and message_revision != 1
        or operation == "edit"
        and message_revision <= 1
    ):
        raise ValueError("encrypted message revision does not match its operation")
    if sender_device_id.startswith("kbe_"):
        if BOT_E2EE_DEVICE_ID_RE.fullmatch(sender_device_id) is None:
            raise ValueError("encrypted bot sender device is invalid")
    elif sender_device_id.startswith("kwe_"):
        if WEBHOOK_E2EE_DEVICE_ID_RE.fullmatch(sender_device_id) is None:
            raise ValueError("encrypted webhook sender device is invalid")
    elif HUMAN_DEVICE_ID_RE.fullmatch(sender_device_id) is None:
        raise ValueError("encrypted human sender device is invalid")
    lineage = (
        application_ref,
        interaction_integration_type,
        interaction_installation_ref,
        interaction_installation_revision,
    )
    if any(item is not None for item in lineage) and any(
        item is None for item in lineage
    ):
        raise ValueError("encrypted message installation lineage is incomplete")
    if interaction_installation_revision is not None and (
        isinstance(interaction_installation_revision, bool)
        or not 1 <= interaction_installation_revision <= (1 << 63) - 1
    ):
        raise ValueError("encrypted message installation revision is invalid")
    attachments = cast(list[dict[str, object]], normalized["attachments"])
    attachment_refs = _message_attachment_refs(attachments)
    message_mention_refs = _canonical_message_mentions(mention_refs)
    (
        message_mention_user_refs,
        message_mention_role_refs,
        message_mention_everyone,
    ) = _message_mention_intent(normalized)
    allowed_mentions = cast(dict[str, object], normalized["allowed_mentions"])
    reply_notifications = cast(bool, allowed_mentions["replied_user"])
    if reply_notifications != (replied_user_ref is not None) or (
        replied_user_ref is not None and referenced_message_ref is None
    ):
        raise ValueError("encrypted reply mention routing is incomplete")
    required_recipients = {
        *message_mention_user_refs,
        *([str(replied_user_ref)] if replied_user_ref is not None else []),
    }
    if not required_recipients <= set(message_mention_refs) or (
        not message_mention_role_refs
        and not message_mention_everyone
        and required_recipients != set(message_mention_refs)
    ):
        raise ValueError("encrypted resolved mention routing is invalid")
    message_sticker_refs = message_sticker_routing_refs(normalized)
    message_custom_emoji_refs = _message_custom_emoji_refs(normalized)
    contract_input = {**normalized, "view_timeout_seconds": view_timeout_seconds}
    contract = interaction_routing_contract(contract_input, callback_type=None)
    forward_snapshot = cast(dict[str, object] | None, normalized["forward_snapshot"])
    if (
        (forwarded_message_ref is None) != (forwarded_channel_ref is None)
        or (forwarded_message_ref is None) != (forward_snapshot is None)
        or (forwarded_message_ref is None) != (forward_source_projection_digest is None)
    ):
        raise ValueError("encrypted forward lineage is incomplete")
    forward_projection_digest = message_forward_projection_digest(
        normalized,
        message_mention_refs,
    )
    if forward_snapshot is not None and (
        encrypted_forward_snapshot_digest(forward_snapshot)
        != forward_source_projection_digest
    ):
        raise ValueError("encrypted forward snapshot does not match its source digest")
    has_controls = bool(contract and contract.get("components"))
    if has_controls and application_ref is None:
        raise ValueError("encrypted interactive messages require application lineage")
    if has_controls:
        if isinstance(view_version, bool) or not 1 <= view_version <= (1 << 63) - 1:
            raise ValueError("encrypted message view version is invalid")
    elif (operation == "create" and view_version != 0) or view_persistent:
        raise ValueError("encrypted message has view metadata without controls")
    envelope: dict[str, object] = {
        "version": 2,
        "protocol": MLS_PROTOCOL,
        "suite": MLS_SUITE,
        "group_id": _b64(context.group_id),
        "policy_generation": str(context.policy_generation),
        "epoch": str(context.epoch),
        "sender_device_id": sender_device_id,
        "operation": operation,
        "author_ref": str(author_ref),
        "message_revision": str(message_revision),
        "message_attachment_refs": attachment_refs,
        "message_custom_emoji_refs": message_custom_emoji_refs,
        "message_mention_everyone": message_mention_everyone,
        "message_mention_refs": message_mention_refs,
        "message_mention_role_refs": message_mention_role_refs,
        "message_mention_user_refs": message_mention_user_refs,
        "message_replied_user_ref": (
            str(replied_user_ref) if replied_user_ref is not None else None
        ),
        "message_sticker_refs": message_sticker_refs,
        "referenced_message_ref": (
            str(referenced_message_ref) if referenced_message_ref is not None else None
        ),
        "rich_payload_digest": message_rich_payload_digest(normalized),
        "forward_projection_digest": forward_projection_digest,
        "forward_projection_version": 2
        if forward_projection_digest is not None
        else None,
        "application_ref": str(application_ref)
        if application_ref is not None
        else None,
        "interaction_integration_type": interaction_integration_type,
        "interaction_installation_ref": (
            str(interaction_installation_ref)
            if interaction_installation_ref is not None
            else None
        ),
        "interaction_installation_revision": (
            str(interaction_installation_revision)
            if interaction_installation_revision is not None
            else None
        ),
        "view_version": str(view_version),
        "view_persistent": view_persistent,
        "tts": normalized["tts"],
        "voice_message": normalized["voice_message"],
        "message_flags": normalized["flags"],
        "forwarded_message_ref": (
            str(forwarded_message_ref) if forwarded_message_ref is not None else None
        ),
        "forwarded_channel_ref": (
            str(forwarded_channel_ref) if forwarded_channel_ref is not None else None
        ),
        "forward_snapshot_digest": (
            _b64(
                hashlib.sha256(
                    _canonical_json(forward_snapshot, label="forward snapshot")
                ).digest()
            )
            if forward_snapshot is not None
            else None
        ),
        "forward_source_projection_digest": forward_source_projection_digest,
        "forwarded_created_at": (
            forward_snapshot.get("created_at") if forward_snapshot is not None else None
        ),
        "forwarded_edited_at": (
            forward_snapshot.get("edited_at") if forward_snapshot is not None else None
        ),
        "forwarded_flags": (
            forward_snapshot.get("flags") if forward_snapshot is not None else None
        ),
        "forwarded_message_type": (
            forward_snapshot.get("message_type")
            if forward_snapshot is not None
            else None
        ),
    }
    if message_ref is not None:
        envelope["target_message"] = str(message_ref)
    if attachments:
        envelope["attachment_manifest_digest"] = message_attachment_manifest_digest(
            attachments
        )
    if contract is not None:
        envelope["interaction_contract"] = contract
        envelope["interaction_contract_digest"] = interaction_routing_contract_digest(
            contract
        )
    authenticated_context = message_rich_authenticated_context(
        context.channel_ref, envelope
    )
    ciphertext = context.provider.encrypt(
        context.group_id,
        message_rich_plaintext(authenticated_context, normalized),
        message_rich_authenticated_data(authenticated_context),
    )
    if not 1 <= len(ciphertext) <= MAX_MLS_MESSAGE_BYTES:
        raise E2EEProtocolError("encrypted rich message ciphertext is invalid")
    envelope["ciphertext"] = _b64(ciphertext)
    return EncryptedRichMessage(authenticated_context, envelope)


def _validate_message_sender_credential(
    credential: bytes,
    *,
    sender_device_id: str,
    author_ref: EntityRef,
    application_ref: EntityRef | None,
) -> None:
    if sender_device_id.startswith("ked_"):
        _validate_human_credential(credential, author_ref)
        return
    try:
        parsed = json.loads(credential)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EEProtocolError("encrypted bot sender credential is invalid") from exc
    if not isinstance(parsed, dict) or set(parsed) != {
        "account",
        "application_ref",
        "credential_type",
        "device_id",
        "worker_id",
    }:
        raise E2EEProtocolError("encrypted bot sender credential is invalid")
    worker_id = parsed.get("worker_id")
    if (
        application_ref is None
        or parsed.get("application_ref") != str(application_ref)
        or parsed.get("credential_type") != "kaede-bot-device-v2"
        or parsed.get("device_id") != sender_device_id
        or not isinstance(worker_id, str)
        or _wire_integer(
            worker_id,
            label="encrypted bot worker ID",
            minimum=1,
            maximum=(1 << 63) - 1,
        )
        < 1
        or parsed.get("account") != f"bot:{application_ref}:worker:{worker_id}"
    ):
        raise E2EEProtocolError("encrypted bot sender credential is invalid")


def _message_poll_with_results(
    decrypted: dict[str, object] | None,
    projected: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if decrypted is None:
        if projected is not None:
            raise E2EEProtocolError(
                "encrypted poll projection has no private definition"
            )
        return None
    if projected is None or projected.get("encrypted") is not True:
        raise E2EEProtocolError(
            "encrypted poll is missing its opaque result projection"
        )
    answers = decrypted.get("answers")
    answer_ids = projected.get("answer_ids")
    if (
        not isinstance(answers, list)
        or not isinstance(answer_ids, list)
        or answer_ids != list(range(1, len(answers) + 1))
        or decrypted.get("allow_multiselect", False)
        is not projected.get("allow_multiselect")
        or decrypted.get("layout_type", 1) != projected.get("layout_type")
    ):
        raise E2EEProtocolError("encrypted poll projection was modified")
    merged = dict(decrypted)
    merged["answers"] = [
        {**cast(dict[str, Any], answer), "answer_id": answer_id}
        for answer_id, answer in zip(answer_ids, answers, strict=True)
        if isinstance(answer, dict)
    ]
    if len(cast(list[object], merged["answers"])) != len(answers):
        raise E2EEProtocolError("encrypted poll answers are invalid")
    merged["expiry"] = projected.get("expiry")
    merged["finalized_at"] = projected.get("finalized_at")
    merged["results"] = projected.get("results")
    return merged


def decrypt_message(
    message: Message,
    context: InteractionE2EEContext,
) -> DecryptedRichMessageData:
    """Decrypt an ordinary rich message and reject every public-context mismatch."""

    if context.channel_ref != message.channel_ref:
        raise E2EEProtocolError("message E2EE context does not match the channel")
    context.require_current()
    envelope = message.e2ee
    required = {
        "version",
        "protocol",
        "suite",
        "group_id",
        "policy_generation",
        "epoch",
        "sender_device_id",
        "operation",
        "ciphertext",
        "author_ref",
        "message_revision",
        "message_attachment_refs",
        "message_custom_emoji_refs",
        "message_mention_everyone",
        "message_mention_refs",
        "message_mention_role_refs",
        "message_mention_user_refs",
        "message_replied_user_ref",
        "message_sticker_refs",
        "referenced_message_ref",
        "rich_payload_digest",
        "forward_projection_digest",
        "forward_projection_version",
        "application_ref",
        "interaction_integration_type",
        "interaction_installation_ref",
        "interaction_installation_revision",
        "view_version",
        "view_persistent",
        "tts",
        "voice_message",
        "message_flags",
        "forwarded_message_ref",
        "forwarded_channel_ref",
        "forward_snapshot_digest",
        "forward_source_projection_digest",
        "forwarded_created_at",
        "forwarded_edited_at",
        "forwarded_flags",
        "forwarded_message_type",
    }
    optional = {
        "target_message",
        "attachment_manifest_digest",
        "interaction_contract",
        "interaction_contract_digest",
    }
    if (
        not isinstance(envelope, dict)
        or not required <= set(envelope)
        or set(envelope) - required - optional
        or envelope.get("version") != 2
        or envelope.get("protocol") != MLS_PROTOCOL
        or envelope.get("suite") != MLS_SUITE
    ):
        raise E2EEProtocolError("rich message MLS envelope is invalid")
    group_id = _decode(envelope.get("group_id"), "rich message group ID", maximum=128)
    if group_id != context.group_id:
        raise E2EEProtocolError("rich message MLS group does not match the channel")
    generation = _wire_integer(
        envelope.get("policy_generation"),
        label="rich message policy generation",
        minimum=1,
        maximum=(1 << 63) - 1,
    )
    epoch = _wire_integer(
        envelope.get("epoch"),
        label="rich message epoch",
        minimum=0,
        maximum=(1 << 63) - 1,
    )
    revision = _wire_integer(
        envelope.get("message_revision"),
        label="rich message revision",
        minimum=1,
        maximum=(1 << 63) - 1,
    )
    operation = envelope.get("operation")
    if generation != context.policy_generation or epoch != context.epoch:
        raise E2EEProtocolError("rich message policy context is stale")
    if (
        operation not in {"create", "edit"}
        or (
            operation == "create"
            and (revision != 1 or envelope.get("target_message") is not None)
        )
        or (
            operation == "edit"
            and (revision <= 1 or envelope.get("target_message") != str(message.ref))
        )
    ):
        raise E2EEProtocolError("rich message operation does not match its projection")
    author_ref = message.author_ref or (
        message.author.ref if message.author is not None else None
    )
    if author_ref is None or envelope.get("author_ref") != str(author_ref):
        raise E2EEProtocolError("rich message author was substituted")
    projected_application = (
        str(message.application_ref) if message.application_ref is not None else None
    )
    if envelope.get("application_ref") != projected_application:
        raise E2EEProtocolError("rich message application was substituted")
    if envelope.get("forwarded_message_ref") != (
        str(message.forwarded_message_ref)
        if message.forwarded_message_ref is not None
        else None
    ) or envelope.get("forwarded_channel_ref") != (
        str(message.forwarded_channel_ref)
        if message.forwarded_channel_ref is not None
        else None
    ):
        raise E2EEProtocolError("rich message forward lineage was substituted")
    attachment_refs = sorted(str(item.ref) for item in message.attachments)
    if envelope.get("message_attachment_refs") != attachment_refs:
        raise E2EEProtocolError("rich message attachments were substituted")
    mention_refs = sorted(str(item) for item in message.mention_user_refs)
    if envelope.get("message_mention_refs") != mention_refs:
        raise E2EEProtocolError("rich message mentions were substituted")
    if envelope.get("referenced_message_ref") != (
        str(message.referenced_message_ref)
        if message.referenced_message_ref is not None
        else None
    ):
        raise E2EEProtocolError("rich message reply reference was substituted")
    if envelope.get("tts") is not message.tts or bool(
        message.flags & (1 << 13)
    ) is not bool(envelope.get("voice_message")):
        raise E2EEProtocolError("rich message delivery markers were substituted")
    view_version = _wire_integer(
        envelope.get("view_version"),
        label="rich message view version",
        minimum=0,
        maximum=(1 << 63) - 1,
    )
    if view_version != message.view_version or envelope.get("view_persistent") is not (
        message.view_persistent
    ):
        raise E2EEProtocolError("rich message view projection was substituted")
    raw_contract = envelope.get("interaction_contract")
    raw_contract_digest = envelope.get("interaction_contract_digest")
    if (raw_contract is None) != (raw_contract_digest is None):
        raise E2EEProtocolError("rich message routing contract is incomplete")
    contract = (
        dict(cast(Mapping[str, object], raw_contract))
        if isinstance(raw_contract, Mapping)
        else None
    )
    if contract is not None and (
        not isinstance(raw_contract_digest, str)
        or not hmac.compare_digest(
            raw_contract_digest,
            interaction_routing_contract_digest(contract),
        )
    ):
        raise E2EEProtocolError("rich message routing contract was modified")
    has_controls = bool(contract and contract.get("components"))
    if has_controls:
        if (
            envelope.get("interaction_integration_type")
            != message.interaction_integration_type
            or envelope.get("interaction_installation_ref")
            != (
                str(message.interaction_installation_ref)
                if message.interaction_installation_ref is not None
                else None
            )
            or envelope.get("interaction_installation_revision")
            != (
                str(message.interaction_installation_revision)
                if message.interaction_installation_revision is not None
                else None
            )
        ):
            raise E2EEProtocolError("rich message view lineage was substituted")
    ciphertext = _decode(
        envelope.get("ciphertext"),
        "rich message ciphertext",
        maximum=MAX_MLS_MESSAGE_BYTES,
    )
    authenticated_context = message_rich_authenticated_context(
        message.channel_ref, envelope
    )
    result = context.provider.process(context.group_id, ciphertext)
    if result.get("kind") != "application":
        raise E2EEProtocolError("rich message MLS record is not application data")
    received_aad = _decode(
        result.get("aad"),
        "rich message authenticated data",
        maximum=4096,
    )
    if not hmac.compare_digest(
        received_aad,
        message_rich_authenticated_data(authenticated_context),
    ):
        raise E2EEProtocolError("rich message authenticated context was modified")
    sender_device_id = envelope.get("sender_device_id")
    if not isinstance(sender_device_id, str):
        raise E2EEProtocolError("rich message sender device is invalid")
    credential = _decode(
        result.get("credential"),
        "rich message sender credential",
        maximum=MAX_CREDENTIAL_BYTES,
    )
    _validate_message_sender_credential(
        credential,
        sender_device_id=sender_device_id,
        author_ref=author_ref,
        application_ref=message.application_ref,
    )
    application = _decode(
        result.get("application"),
        "rich message plaintext",
        maximum=MAX_INTERACTION_PLAINTEXT_BYTES,
    )
    try:
        plaintext = json.loads(application)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EEProtocolError("rich message plaintext is invalid") from exc
    if (
        not isinstance(plaintext, dict)
        or set(plaintext) != {"version", "kind", "context", "data"}
        or plaintext.get("version") != 2
        or plaintext.get("kind") != "message"
        or plaintext.get("context") != authenticated_context
        or not isinstance(plaintext.get("data"), dict)
    ):
        raise E2EEProtocolError("rich message plaintext context is invalid")
    try:
        normalized = _message_rich_data(cast(dict[str, object], plaintext["data"]))
    except ValueError as exc:
        raise E2EEProtocolError("rich message plaintext body is invalid") from exc
    expected_rich_digest = message_rich_payload_digest(normalized)
    if not isinstance(
        envelope.get("rich_payload_digest"), str
    ) or not hmac.compare_digest(
        cast(str, envelope["rich_payload_digest"]),
        expected_rich_digest,
    ):
        raise E2EEProtocolError("rich message body digest was modified")
    expected_forward_projection = message_forward_projection_digest(
        normalized,
        mention_refs,
    )
    if envelope.get(
        "forward_projection_digest"
    ) != expected_forward_projection or envelope.get("forward_projection_version") != (
        2 if expected_forward_projection is not None else None
    ):
        raise E2EEProtocolError("rich message forward projection was modified")
    try:
        expected_sticker_refs = message_sticker_routing_refs(normalized)
        expected_custom_emoji_refs = _message_custom_emoji_refs(normalized)
        (
            expected_mention_user_refs,
            expected_mention_role_refs,
            expected_mention_everyone,
        ) = _message_mention_intent(normalized)
    except ValueError as exc:
        raise E2EEProtocolError("rich message routing metadata is invalid") from exc
    if envelope.get("message_sticker_refs") != expected_sticker_refs:
        raise E2EEProtocolError("rich message stickers were substituted")
    if envelope.get("message_custom_emoji_refs") != expected_custom_emoji_refs:
        raise E2EEProtocolError("rich message custom emoji were substituted")
    if (
        envelope.get("message_mention_user_refs") != expected_mention_user_refs
        or envelope.get("message_mention_role_refs") != expected_mention_role_refs
        or envelope.get("message_mention_everyone") is not expected_mention_everyone
    ):
        raise E2EEProtocolError("rich message mention intent was substituted")
    replied_user_ref = envelope.get("message_replied_user_ref")
    allowed_mentions = cast(dict[str, object], normalized["allowed_mentions"])
    reply_notifications = cast(bool, allowed_mentions["replied_user"])
    if reply_notifications != (replied_user_ref is not None):
        raise E2EEProtocolError("rich message reply mention intent was substituted")
    if replied_user_ref is not None:
        referenced = message.referenced_message
        referenced_author_ref = None
        if referenced is not None:
            referenced_author_ref = referenced.author_ref or (
                referenced.author.ref if referenced.author is not None else None
            )
        if referenced_author_ref is None or replied_user_ref != str(
            referenced_author_ref
        ):
            raise E2EEProtocolError(
                "rich message replied-user reference was substituted"
            )
    required_recipients = {
        *expected_mention_user_refs,
        *([cast(str, replied_user_ref)] if replied_user_ref is not None else []),
    }
    if not required_recipients <= set(mention_refs) or (
        not expected_mention_role_refs
        and not expected_mention_everyone
        and required_recipients != set(mention_refs)
    ):
        raise E2EEProtocolError("rich message resolved mentions were substituted")
    manifests = cast(list[dict[str, object]], normalized["attachments"])
    try:
        manifest_refs = _message_attachment_refs(manifests)
    except ValueError as exc:
        raise E2EEProtocolError(
            "rich message attachment manifests are invalid"
        ) from exc
    manifest_digest = envelope.get("attachment_manifest_digest")
    if (
        manifest_refs != attachment_refs
        or (bool(manifests) != (manifest_digest is not None))
        or (
            manifests
            and (
                not isinstance(manifest_digest, str)
                or not hmac.compare_digest(
                    manifest_digest,
                    message_attachment_manifest_digest(manifests),
                )
            )
        )
    ):
        raise E2EEProtocolError("rich message attachment manifests were modified")
    expected_contract = interaction_routing_contract(
        {
            **normalized,
            "view_timeout_seconds": (
                contract.get("view_timeout_seconds", 900)
                if contract is not None
                else 900
            ),
        },
        callback_type=None,
    )
    if expected_contract != contract:
        raise E2EEProtocolError("rich message routing contract does not match its body")
    forward_snapshot = (
        dict(cast(Mapping[str, object], normalized["forward_snapshot"]))
        if normalized["forward_snapshot"] is not None
        else None
    )
    forward_digest = envelope.get("forward_snapshot_digest")
    if bool(forward_snapshot) != (forward_digest is not None) or (
        forward_snapshot is not None
        and (
            not isinstance(forward_digest, str)
            or not hmac.compare_digest(
                forward_digest,
                _b64(
                    hashlib.sha256(
                        _canonical_json(forward_snapshot, label="forward snapshot")
                    ).digest()
                ),
            )
        )
    ):
        raise E2EEProtocolError("rich message forward snapshot was modified")
    source_projection_digest = envelope.get("forward_source_projection_digest")
    if (forward_snapshot is None) != (source_projection_digest is None):
        raise E2EEProtocolError("rich message forward source identity is incomplete")
    if forward_snapshot is not None:
        if (
            not isinstance(source_projection_digest, str)
            or not hmac.compare_digest(
                source_projection_digest,
                encrypted_forward_snapshot_digest(forward_snapshot),
            )
            or envelope.get("forwarded_created_at")
            != forward_snapshot.get("created_at")
            or envelope.get("forwarded_edited_at") != forward_snapshot.get("edited_at")
            or envelope.get("forwarded_flags") != forward_snapshot.get("flags")
            or envelope.get("forwarded_message_type")
            != forward_snapshot.get("message_type")
        ):
            raise E2EEProtocolError(
                "rich message forward source projection was modified"
            )
    elif any(
        envelope.get(field) is not None
        for field in (
            "forwarded_created_at",
            "forwarded_edited_at",
            "forwarded_flags",
            "forwarded_message_type",
        )
    ):
        raise E2EEProtocolError("rich message forward metadata is incomplete")
    decrypted_poll = (
        dict(cast(Mapping[str, object], normalized["poll"]))
        if normalized["poll"] is not None
        else None
    )
    poll = _message_poll_with_results(decrypted_poll, message.poll)
    context.record_message_ciphertext(
        ciphertext,
        message.ref,
        revision,
        cast(Literal["create", "edit"], operation),
    )
    return DecryptedRichMessageData(
        content=cast(str | None, normalized["content"]),
        embeds=tuple(cast(list[dict[str, Any]], normalized["embeds"])),
        components=tuple(cast(list[dict[str, Any]], normalized["components"])),
        poll=poll,
        sticker_items=tuple(cast(list[dict[str, Any]], normalized["sticker_items"])),
        tts=cast(bool, normalized["tts"]),
        voice_message=cast(bool, normalized["voice_message"]),
        flags=cast(int, normalized["flags"]),
        attachments=tuple(cast(list[dict[str, Any]], normalized["attachments"])),
        allowed_mentions=dict(cast(dict[str, Any], normalized["allowed_mentions"])),
        forward_snapshot=forward_snapshot,
    )
