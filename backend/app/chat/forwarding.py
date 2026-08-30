from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.chat.custom_emojis import custom_emoji_refs
from app.chat.custom_stickers import validate_sticker_items
from app.chat.message_flags import (
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_IS_VOICE_MESSAGE,
)
from app.chat.rich_content import (
    MESSAGE_LAYOUT_COMPONENT_ADAPTER,
    Embed,
    validate_attachment_url_references,
    validate_embed_collection,
    validate_message_components,
)
from app.core.base64url import decode_base64url, encode_base64url
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import DOMAIN_RE
from app.core.types import EntityRef, WireSnowflake
from app.db.models import Attachment, Message
from app.media.payloads import attachment_payload

FORWARD_SNAPSHOT_FLAG_MASK = (
    (1 << 2) | MESSAGE_FLAG_IS_COMPONENTS_V2 | MESSAGE_FLAG_IS_VOICE_MESSAGE
)
FORWARDABLE_MESSAGE_TYPES = frozenset({0, 19, 20, 23})
FORWARD_SOURCE_AUTHORIZATION_EVENT = "message.forward.source.authorized"
FORWARD_SOURCE_AUTHORIZATION_TTL_SECONDS = 90
FORWARD_SOURCE_AUTHORIZATION_NONCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
CANONICAL_DIGEST_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _qualified_ref(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a qualified reference")
    try:
        reference = EntityRef(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a qualified reference") from exc
    if reference.domain is None or str(reference) != value:
        raise ValueError(f"{label} must be a qualified reference")
    return value


class ForwardSourceAuthorization(UnambiguousInputModel):
    """Source-authority proof for one immutable, client-mediated forward.

    The proof deliberately contains no author identity or decrypted E2EE body.
    A plaintext authority may include its canonical author-free snapshot; an
    encrypted authority can authenticate only the v2 semantic commitment that
    was already bound into the source MLS envelope.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    requester_ref: str
    requester_type: Literal["human", "bot"]
    source_message_ref: str
    source_channel_ref: str
    destination_channel_ref: str
    destination_encryption_mode: Literal["plaintext", "e2ee"]
    source_encryption_mode: Literal["plaintext", "e2ee"]
    source_projection_version: Literal[2]
    source_projection_digest: str = Field(min_length=43, max_length=43)
    source_created_at: datetime
    source_edited_at: datetime | None = None
    source_flags: int = Field(ge=0, le=2_147_483_647)
    source_message_type: int = Field(ge=0, le=2_147_483_647)
    source_nsfw: bool
    source_attachment_refs: list[str] = Field(default_factory=list, max_length=10)
    source_sticker_items: list[dict[str, object]] = Field(max_length=9)
    source_custom_emoji_refs: list[str] = Field(max_length=256)
    source_snapshot: dict[str, object] | None = None
    application_ref: str | None = None
    e2ee_device_id: str | None = Field(
        default=None,
        pattern=r"^(?:ked|kbe|kwe)_[A-Za-z0-9_-]{43}$",
    )
    nonce: str = Field(min_length=1, max_length=64)
    expires_at: datetime

    @field_validator(
        "version",
        "source_projection_version",
        "source_flags",
        "source_message_type",
        mode="before",
    )
    @classmethod
    def strict_integer_fields(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("forward authorization integer fields must be integers")
        return value

    @field_validator("source_nsfw", mode="before")
    @classmethod
    def strict_age_context(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("forward authorization age context must be a boolean")
        return value

    @field_validator(
        "requester_ref",
        "source_message_ref",
        "source_channel_ref",
        "destination_channel_ref",
    )
    @classmethod
    def canonical_required_ref(cls, value: str) -> str:
        return _qualified_ref(value, label="forward authorization reference")

    @field_validator("application_ref")
    @classmethod
    def canonical_optional_ref(cls, value: str | None) -> str | None:
        return (
            _qualified_ref(value, label="forward authorization application")
            if value is not None
            else None
        )

    @field_validator("source_attachment_refs")
    @classmethod
    def canonical_attachment_refs(cls, value: list[str]) -> list[str]:
        normalized = [
            _qualified_ref(item, label="forward authorization attachment") for item in value
        ]
        if normalized != sorted(set(normalized)):
            raise ValueError(
                "forward authorization attachment references must be sorted and unique"
            )
        return normalized

    @field_validator("source_projection_digest")
    @classmethod
    def canonical_projection_digest(cls, value: str) -> str:
        if CANONICAL_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("forward authorization projection digest is invalid")
        return value

    @field_validator("source_sticker_items")
    @classmethod
    def canonical_sticker_items(
        cls,
        value: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        normalized = validate_sticker_items(value, maximum=9)
        refs = [f"{item['id']}@{item['origin_domain']}" for item in normalized]
        if refs != sorted(refs) or len(refs) != len(set(refs)):
            raise ValueError("forward authorization sticker items are not canonical")
        return normalized

    @field_validator("source_custom_emoji_refs")
    @classmethod
    def canonical_custom_emoji_refs(cls, value: list[str]) -> list[str]:
        if value != sorted(value) or len(value) != len(set(value)):
            raise ValueError("forward authorization custom emoji references are not canonical")
        for token in value:
            refs = custom_emoji_refs(token)
            if len(refs) != 1 or refs[0].token != token:
                raise ValueError("forward authorization custom emoji reference is invalid")
        return value

    @field_validator("nonce")
    @classmethod
    def canonical_nonce(cls, value: str) -> str:
        if FORWARD_SOURCE_AUTHORIZATION_NONCE_RE.fullmatch(value) is None:
            raise ValueError("forward authorization nonce is invalid")
        return value

    @model_validator(mode="after")
    def consistent_source(self) -> ForwardSourceAuthorization:
        if self.source_created_at.tzinfo is None or (
            self.source_edited_at is not None and self.source_edited_at.tzinfo is None
        ):
            raise ValueError("forward authorization timestamps require a timezone")
        if self.expires_at.tzinfo is None:
            raise ValueError("forward authorization expiry requires a timezone")
        if self.source_edited_at is not None and self.source_edited_at < self.source_created_at:
            raise ValueError("forward authorization edit predates creation")
        if self.source_message_type not in FORWARDABLE_MESSAGE_TYPES:
            raise ValueError("forward authorization message type is unsupported")
        if self.source_flags & ~FORWARD_SNAPSHOT_FLAG_MASK:
            raise ValueError("forward authorization flags are unsupported")
        if self.source_encryption_mode == "plaintext":
            if self.source_snapshot is None:
                raise ValueError("plaintext forward authorization requires a snapshot")
            self.source_snapshot = validate_forward_snapshot_source_binding(
                self.source_snapshot,
                source_projection_digest=self.source_projection_digest,
                source_created_at=self.source_created_at,
                source_edited_at=self.source_edited_at,
                source_flags=self.source_flags,
                source_message_type=self.source_message_type,
            )
            if self.source_sticker_items != forward_snapshot_sticker_items(
                self.source_snapshot
            ) or self.source_custom_emoji_refs != forward_snapshot_custom_emoji_tokens(
                self.source_snapshot
            ):
                raise ValueError("plaintext forward expression projection is inconsistent")
        elif self.source_snapshot is not None:
            raise ValueError("encrypted forward authorization cannot contain plaintext")
        if self.requester_type == "human" and (
            self.application_ref is not None or self.e2ee_device_id is not None
        ):
            raise ValueError("human forward authorization cannot claim bot lineage")
        if self.requester_type == "bot" and self.application_ref is None:
            raise ValueError("bot forward authorization requires an application")
        if self.e2ee_device_id is not None and self.application_ref is None:
            raise ValueError("forward authorization device requires an application")
        return self


def validate_forward_source_authorization(
    value: object,
    *,
    expected_authority: str,
    requester_ref: str | None = None,
    destination_channel_ref: str | None = None,
    destination_encryption_mode: str | None = None,
    nonce: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Validate one exact signed-proof content projection."""

    try:
        authorization = ForwardSourceAuthorization.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("forward source authorization is invalid") from exc
    source_channel = EntityRef(authorization.source_channel_ref)
    current = now or datetime.now(UTC)
    if (
        source_channel.domain != expected_authority
        or (requester_ref is not None and authorization.requester_ref != requester_ref)
        or (
            destination_channel_ref is not None
            and authorization.destination_channel_ref != destination_channel_ref
        )
        or (
            destination_encryption_mode is not None
            and authorization.destination_encryption_mode != destination_encryption_mode
        )
        or (nonce is not None and authorization.nonce != nonce)
        or authorization.expires_at <= current
        or authorization.expires_at
        > current + timedelta(seconds=FORWARD_SOURCE_AUTHORIZATION_TTL_SECONDS + 5)
    ):
        raise ValueError("forward source authorization binding is invalid")
    return authorization.model_dump(mode="json", exclude_none=False)


def authority_attested_forward_source(
    event_type: str,
    content: object,
    context: object,
    *,
    expected_authority: str,
    actor: tuple[str, str],
    event_timestamp_ms: int | None = None,
) -> bool:
    """Recognize the only cross-origin actor shape allowed for forward proofs."""

    if event_type != FORWARD_SOURCE_AUTHORIZATION_EVENT or not isinstance(context, dict):
        return False
    try:
        authorization = validate_forward_source_authorization(
            content,
            expected_authority=expected_authority,
            requester_ref=f"{actor[0]}@{actor[1]}",
            now=(
                datetime.fromtimestamp(event_timestamp_ms / 1000, tz=UTC)
                if event_timestamp_ms is not None
                else datetime.now(UTC)
            ),
        )
    except ValueError:
        return False
    return context == {"source_channel_ref": authorization["source_channel_ref"]}


def _plaintext_sha256_b64(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("forward attachment integrity metadata is unavailable")
    return encode_base64url(bytes.fromhex(value))


def can_forward_between_age_contexts(
    source_nsfw: bool | None,
    destination_nsfw: bool | None,
) -> bool:
    """Fail closed and prevent age-restricted snapshots escaping their context."""

    return (
        source_nsfw is not None
        and destination_nsfw is not None
        and (not source_nsfw or destination_nsfw)
    )


class ForwardSnapshotAttachment(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    id: WireSnowflake
    origin_domain: str = Field(min_length=1, max_length=253)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    size: int = Field(ge=0, le=100 * 1024 * 1024)
    plaintext_sha256: str = Field(min_length=43, max_length=43)
    width: int | None = Field(default=None, ge=1, le=16_384)
    height: int | None = Field(default=None, ge=1, le=16_384)
    duration_secs: float | None = Field(default=None, gt=0, le=1_200)
    waveform: str | None = Field(default=None, min_length=4, max_length=344)
    blurhash: str | None = Field(default=None, max_length=256)
    scan_status: str
    encryption_mode: str
    encryption_protocol: str | None = Field(default=None, max_length=64)
    variants: dict[str, object] = Field(default_factory=dict)

    @field_validator("size", "width", "height", mode="before")
    @classmethod
    def strict_integer_fields(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("forward attachment dimensions and size must be integers")
        return value

    @field_validator("duration_secs", mode="before")
    @classmethod
    def strict_duration(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("forward attachment duration must be numeric")
        return value

    @field_validator("origin_domain")
    @classmethod
    def normalized_domain(cls, value: str) -> str:
        if value != value.rstrip(".").lower() or not DOMAIN_RE.fullmatch(value):
            raise ValueError("attachment origin domain must be normalized")
        return value

    @field_validator("scan_status")
    @classmethod
    def public_scan_status(cls, value: str) -> str:
        if value not in {"pending", "clean", "rejected"}:
            raise ValueError("attachment scan status is invalid")
        return value

    @field_validator("encryption_mode")
    @classmethod
    def plaintext_only(cls, value: str) -> str:
        if value != "plaintext":
            raise ValueError("a disclosed forward snapshot requires plaintext attachments")
        return value

    @field_validator("plaintext_sha256")
    @classmethod
    def canonical_plaintext_digest(cls, value: str) -> str:
        try:
            decode_base64url(value, size=32)
        except ValueError as exc:
            raise ValueError("attachment plaintext digest is invalid") from exc
        return value

    @model_validator(mode="after")
    def voice_metadata_is_complete(self) -> ForwardSnapshotAttachment:
        if (self.duration_secs is None) != (self.waveform is None):
            raise ValueError("attachment voice metadata is incomplete")
        if self.waveform is not None:
            try:
                decoded = base64.b64decode(self.waveform, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("attachment waveform is invalid") from exc
            if not 1 <= len(decoded) <= 256 or not self.content_type.startswith("audio/"):
                raise ValueError("attachment voice metadata is invalid")
        return self


def validate_forward_snapshot_metadata(snapshot: ForwardSnapshot) -> None:
    if snapshot.created_at.tzinfo is None or (
        snapshot.edited_at is not None and snapshot.edited_at.tzinfo is None
    ):
        raise ValueError("forward snapshot timestamps require a timezone")
    if snapshot.edited_at is not None and snapshot.edited_at < snapshot.created_at:
        raise ValueError("forward snapshot edit timestamp predates creation")
    if snapshot.message_type not in FORWARDABLE_MESSAGE_TYPES:
        raise ValueError("this message type cannot be forwarded")
    if snapshot.flags & ~FORWARD_SNAPSHOT_FLAG_MASK:
        raise ValueError("forward snapshot contains unsupported flags")


def normalized_forward_rich_content(
    snapshot: ForwardSnapshot,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        embeds = [Embed.model_validate(item) for item in snapshot.embeds]
        components = [
            MESSAGE_LAYOUT_COMPONENT_ADAPTER.validate_python(item) for item in snapshot.components
        ]
        validate_embed_collection(embeds)
        validate_message_components(components)
        validate_attachment_url_references(
            embeds=embeds,
            components=components,
            attachments=snapshot.attachments,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("forward snapshot rich content is invalid") from exc
    return (
        [item.model_dump(mode="json", exclude_none=True) for item in embeds],
        [item.model_dump(mode="json", exclude_none=True) for item in components],
    )


def normalized_forward_mentions(
    values: list[dict[str, str]],
) -> list[dict[str, str]]:
    seen: set[tuple[int, str]] = set()
    normalized: list[dict[str, str]] = []
    for raw in values:
        if not isinstance(raw, dict) or set(raw) != {"id", "origin_domain"}:
            raise ValueError("forward snapshot mention is invalid")
        try:
            mention_id = int(raw["id"])
        except (TypeError, ValueError) as exc:
            raise ValueError("forward snapshot mention is invalid") from exc
        domain = raw["origin_domain"]
        mention_ref = mention_id, domain
        if (
            mention_id < 1
            or str(mention_id) != raw["id"]
            or domain != domain.rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(domain)
            or mention_ref in seen
        ):
            raise ValueError("forward snapshot mention is invalid")
        seen.add(mention_ref)
        normalized.append({"id": str(mention_id), "origin_domain": domain})
    return normalized


def normalized_nested_forward_snapshots(
    values: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in values:
        nested = ForwardSnapshot.model_validate(raw)
        if nested.message_snapshots:
            raise ValueError("forward snapshot nesting exceeds one level")
        normalized.append(nested.model_dump(mode="json", exclude_none=True))
    return normalized


def require_forward_snapshot_body(snapshot: ForwardSnapshot) -> None:
    if not any(
        (
            snapshot.content is not None,
            snapshot.embeds,
            snapshot.components,
            snapshot.attachments,
            snapshot.sticker_items,
            snapshot.message_snapshots,
        )
    ):
        raise ValueError("forward snapshot has no body")


class ForwardSnapshot(UnambiguousInputModel):
    """Author-free immutable message material embedded in a forward."""

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=4_000)
    embeds: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    components: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    attachments: list[ForwardSnapshotAttachment] = Field(default_factory=list, max_length=10)
    mention_user_refs: list[dict[str, str]] = Field(default_factory=list, max_length=5_000)
    sticker_items: list[dict[str, object]] = Field(default_factory=list, max_length=3)
    message_snapshots: list[dict[str, Any]] = Field(default_factory=list, max_length=1)
    message_type: int = Field(default=0, ge=0, le=2_147_483_647)
    flags: int = Field(default=0, ge=0, le=2_147_483_647)
    created_at: datetime
    edited_at: datetime | None = None

    @field_validator("message_type", "flags", mode="before")
    @classmethod
    def strict_integer_fields(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("forward snapshot type and flags must be integers")
        return value

    @model_validator(mode="after")
    def validate_projection(self) -> ForwardSnapshot:
        validate_forward_snapshot_metadata(self)
        self.embeds, self.components = normalized_forward_rich_content(self)
        self.sticker_items = validate_sticker_items(self.sticker_items)
        self.mention_user_refs = normalized_forward_mentions(self.mention_user_refs)
        self.message_snapshots = normalized_nested_forward_snapshots(self.message_snapshots)
        require_forward_snapshot_body(self)
        return self


def build_forward_snapshot(
    source: Message,
    attachments: list[Attachment],
    *,
    poll: object | None = None,
) -> dict[str, object]:
    """Build a privacy-preserving snapshot from an already-authorized source."""

    if source.deleted_at is not None or source.e2ee is not None or poll is not None:
        raise ValueError("only live plaintext messages can be forwarded")
    for item in attachments:
        _plaintext_sha256_b64(item.content_sha256)
    snapshot = ForwardSnapshot.model_validate(
        {
            "content": source.content,
            "embeds": list(source.embeds or []),
            "components": list(source.components or []),
            "attachments": [
                attachment_payload(item, include_lifecycle=False)
                | {"plaintext_sha256": _plaintext_sha256_b64(item.content_sha256)}
                for item in attachments
            ],
            "mention_user_refs": list(source.mention_user_refs or []),
            "sticker_items": list(source.sticker_items or []),
            "message_snapshots": (
                [dict(source.forward_snapshot)] if isinstance(source.forward_snapshot, dict) else []
            ),
            "message_type": source.message_type,
            "flags": int(source.flags or 0) & FORWARD_SNAPSHOT_FLAG_MASK,
            "created_at": source.created_at,
            "edited_at": source.edited_at,
        }
    )
    return snapshot.model_dump(mode="json", exclude_none=True)


def build_forward_source_authorization_content(
    source: Message,
    attachments: list[Attachment],
    *,
    requester_ref: str,
    requester_type: Literal["human", "bot"],
    source_channel_ref: str,
    destination_channel_ref: str,
    destination_encryption_mode: Literal["plaintext", "e2ee"],
    source_nsfw: bool,
    nonce: str,
    application_ref: str | None = None,
    e2ee_device_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Build canonical proof content after the caller authorized source access."""

    if source.deleted_at is not None or source.created_at is None:
        raise ValueError("forward source is unavailable")
    if source.message_type not in FORWARDABLE_MESSAGE_TYPES:
        raise ValueError("this message type cannot be forwarded")
    source_snapshot: dict[str, object] | None
    source_sticker_items: list[dict[str, object]]
    source_custom_emoji_refs: list[str]
    source_encryption_mode: Literal["plaintext", "e2ee"]
    source_projection_digest: str | None
    if source.e2ee is None:
        source_encryption_mode = "plaintext"
        source_snapshot = build_forward_snapshot(source, attachments)
        source_projection_digest = forward_snapshot_projection_digest(source_snapshot)
        source_sticker_items = forward_snapshot_sticker_items(source_snapshot)
        source_custom_emoji_refs = forward_snapshot_custom_emoji_tokens(source_snapshot)
    else:
        source_encryption_mode = "e2ee"
        source_snapshot = None
        source_projection_digest = (
            source.e2ee.get("forward_projection_digest")
            if source.e2ee.get("forward_projection_version") == 2
            and "rich_payload_digest" in source.e2ee
            else None
        )
        if not isinstance(source_projection_digest, str):
            raise ValueError("encrypted forward source lacks a v2 semantic commitment")
        source_sticker_items = sorted(
            validate_sticker_items(
                list(source.sticker_items or []),
                maximum=9,
            ),
            key=lambda item: f"{item['id']}@{item['origin_domain']}",
        )
        raw_custom_emoji_refs = source.e2ee.get("message_custom_emoji_refs", [])
        if not isinstance(raw_custom_emoji_refs, list) or any(
            not isinstance(item, str) for item in raw_custom_emoji_refs
        ):
            raise ValueError("encrypted forward source expression routing is invalid")
        source_custom_emoji_refs = [str(item) for item in raw_custom_emoji_refs]
    current = now or datetime.now(UTC)
    authorization = ForwardSourceAuthorization.model_validate(
        {
            "version": 1,
            "requester_ref": requester_ref,
            "requester_type": requester_type,
            "source_message_ref": f"{source.id}@{source.origin_domain}",
            "source_channel_ref": source_channel_ref,
            "destination_channel_ref": destination_channel_ref,
            "destination_encryption_mode": destination_encryption_mode,
            "source_encryption_mode": source_encryption_mode,
            "source_projection_version": 2,
            "source_projection_digest": source_projection_digest,
            "source_created_at": source.created_at,
            "source_edited_at": source.edited_at,
            "source_flags": int(source.flags or 0) & FORWARD_SNAPSHOT_FLAG_MASK,
            "source_message_type": source.message_type,
            "source_nsfw": source_nsfw,
            "source_attachment_refs": sorted(
                f"{item.id}@{item.origin_domain}" for item in attachments
            ),
            "source_sticker_items": source_sticker_items,
            "source_custom_emoji_refs": source_custom_emoji_refs,
            "source_snapshot": source_snapshot,
            "application_ref": application_ref,
            "e2ee_device_id": e2ee_device_id,
            "nonce": nonce,
            "expires_at": current + timedelta(seconds=FORWARD_SOURCE_AUTHORIZATION_TTL_SECONDS),
        }
    )
    return authorization.model_dump(mode="json", exclude_none=False)


def validate_forward_snapshot(value: object) -> dict[str, object]:
    try:
        snapshot = ForwardSnapshot.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("forward snapshot is invalid") from exc
    return snapshot.model_dump(mode="json", exclude_none=True)


def forward_snapshot_sticker_items(value: object) -> list[dict[str, object]]:
    """Flatten canonical sticker routing metadata through the one-level snapshot."""

    snapshot = ForwardSnapshot.model_validate(value)
    by_ref: dict[tuple[int, str], dict[str, object]] = {}

    def collect(current: ForwardSnapshot) -> None:
        for item in current.sticker_items:
            reference = (int(str(item["id"])), str(item["origin_domain"]))
            previous = by_ref.get(reference)
            if previous is not None and previous != item:
                raise ValueError("forward snapshot sticker metadata is inconsistent")
            by_ref[reference] = dict(item)
        for nested in current.message_snapshots:
            collect(ForwardSnapshot.model_validate(nested))

    collect(snapshot)
    return [
        by_ref[reference] for reference in sorted(by_ref, key=lambda item: f"{item[0]}@{item[1]}")
    ]


def forward_snapshot_custom_emoji_tokens(value: object) -> list[str]:
    """Collect authority-routable custom emoji from visible snapshot fields."""

    snapshot = ForwardSnapshot.model_validate(value)
    tokens: set[str] = set()

    def collect_strings(raw: object) -> None:
        if isinstance(raw, str):
            tokens.update(reference.token for reference in custom_emoji_refs(raw))
        elif isinstance(raw, dict):
            for nested in raw.values():
                collect_strings(nested)
        elif isinstance(raw, list):
            for nested in raw:
                collect_strings(nested)

    def collect(current: ForwardSnapshot) -> None:
        collect_strings(current.content)
        collect_strings(current.embeds)
        collect_strings(current.components)
        for nested in current.message_snapshots:
            collect(ForwardSnapshot.model_validate(nested))

    collect(snapshot)
    result = sorted(tokens)
    if len(result) > 256:
        raise ValueError("forward snapshot has too many custom emoji references")
    return result


def validate_forward_snapshot_source_binding(
    value: object,
    *,
    source_projection_digest: object,
    source_created_at: object,
    source_edited_at: object,
    source_flags: object,
    source_message_type: object,
) -> dict[str, object]:
    """Bind metadata omitted from the transport-independent body digest."""

    snapshot = validate_forward_snapshot(value)
    try:
        snapshot_created_at = datetime.fromisoformat(str(snapshot["created_at"]))
        snapshot_edited_at = (
            datetime.fromisoformat(str(snapshot["edited_at"]))
            if snapshot.get("edited_at") is not None
            else None
        )
        expected_created_at = datetime.fromisoformat(str(source_created_at))
        expected_edited_at = (
            datetime.fromisoformat(str(source_edited_at)) if source_edited_at is not None else None
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("forward source timestamps are invalid") from exc
    if (
        not isinstance(source_projection_digest, str)
        or forward_snapshot_projection_digest(snapshot) != source_projection_digest
        or snapshot_created_at != expected_created_at
        or snapshot_edited_at != expected_edited_at
        or snapshot.get("flags") != source_flags
        or snapshot.get("message_type") != source_message_type
    ):
        raise ValueError("forward snapshot does not match its source proof")
    return snapshot


def forward_snapshot_projection_digest(value: object) -> str:
    """Digest the author-free semantic body independently of file transport."""

    projection = forward_snapshot_projection(value)
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return encode_base64url(hashlib.sha256(encoded).digest())


def forward_snapshot_attachment_semantics(
    item: ForwardSnapshotAttachment,
) -> dict[str, object]:
    return {
        "filename": item.filename,
        "content_type": item.content_type,
        "plaintext_size": item.size,
        "plaintext_sha256": item.plaintext_sha256,
        **(
            {
                "duration_millis": round(item.duration_secs * 1000),
                "waveform": item.waveform,
            }
            if item.duration_secs is not None and item.waveform is not None
            else {}
        ),
    }


def forward_snapshot_projection(value: object) -> dict[str, object]:
    """Return the canonical semantic projection shared with E2EE clients."""

    snapshot = ForwardSnapshot.model_validate(value)
    return {
        "version": 2,
        "content": snapshot.content,
        "embeds": snapshot.embeds,
        "components": snapshot.components,
        "attachments": [
            forward_snapshot_attachment_semantics(item) for item in snapshot.attachments
        ],
        "mention_user_refs": snapshot.mention_user_refs,
        "sticker_items": snapshot.sticker_items,
        "message_snapshots": [
            forward_snapshot_projection(item) for item in snapshot.message_snapshots
        ],
        "flags": snapshot.flags,
    }


def forward_snapshot_matches_attachments(
    value: object,
    attachments: list[Attachment] | list[dict[str, object]],
) -> bool:
    """Match a disclosed plaintext snapshot to exact destination uploads."""

    snapshot = ForwardSnapshot.model_validate(value)
    if len(snapshot.attachments) != len(attachments):
        return False

    def destination_projection(
        item: Attachment | dict[str, object],
    ) -> tuple[str, str, str, str, int, str, int | None, str | None]:
        if isinstance(item, Attachment):
            plaintext_digest = _plaintext_sha256_b64(item.content_sha256)
            return (
                str(item.id),
                item.origin_domain,
                item.filename,
                item.detected_content_type or item.content_type,
                item.size,
                plaintext_digest,
                round(item.duration_secs * 1000) if item.duration_secs is not None else None,
                item.waveform,
            )
        raw_hash = item.get("content_sha256")
        plaintext_digest = _plaintext_sha256_b64(raw_hash)
        raw_duration = item.get("duration_secs")
        if raw_duration is not None and (
            isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float))
        ):
            raise ValueError("forward attachment voice metadata is invalid")
        return (
            str(item.get("id")),
            str(item.get("origin_domain")),
            str(item.get("filename")),
            str(item.get("detected_content_type") or item.get("content_type")),
            int(str(item.get("size"))),
            plaintext_digest,
            round(raw_duration * 1000) if raw_duration is not None else None,
            str(item.get("waveform")) if item.get("waveform") is not None else None,
        )

    snapshot_projection = [
        (
            str(item.id),
            item.origin_domain,
            item.filename,
            item.content_type,
            item.size,
            item.plaintext_sha256,
            round(item.duration_secs * 1000) if item.duration_secs is not None else None,
            item.waveform,
        )
        for item in snapshot.attachments
    ]
    try:
        destination = [destination_projection(item) for item in attachments]
    except (TypeError, ValueError):
        return False
    if snapshot_projection != destination:
        return False
    if snapshot.message_snapshots:
        nested = ForwardSnapshot.model_validate(snapshot.message_snapshots[0])
        nested_projection = [
            (
                str(item.id),
                item.origin_domain,
                forward_snapshot_attachment_semantics(item),
            )
            for item in nested.attachments
        ]
        if nested_projection != [
            (
                str(item.id),
                item.origin_domain,
                forward_snapshot_attachment_semantics(item),
            )
            for item in snapshot.attachments
        ]:
            return False
    return True


def rebind_forward_snapshot_attachments(
    value: object,
    attachments: list[Attachment],
) -> dict[str, object]:
    """Move an authoritative plaintext snapshot onto fresh destination media."""

    snapshot = ForwardSnapshot.model_validate(value)
    if len(snapshot.attachments) != len(attachments):
        raise ValueError("forward snapshot attachment count is inconsistent")
    rebound: list[ForwardSnapshotAttachment] = []
    for source, destination in zip(snapshot.attachments, attachments, strict=True):
        destination_payload = attachment_payload(destination, include_lifecycle=False) | {
            "plaintext_sha256": _plaintext_sha256_b64(destination.content_sha256)
        }
        destination_payload["variants"] = destination_payload.get("variants") or {}
        candidate = ForwardSnapshotAttachment.model_validate(destination_payload)
        if forward_snapshot_attachment_semantics(source) != forward_snapshot_attachment_semantics(
            candidate
        ):
            raise ValueError("forward snapshot attachment bytes or metadata changed")
        rebound.append(candidate)
    if snapshot.message_snapshots:
        nested = ForwardSnapshot.model_validate(snapshot.message_snapshots[0])
        source_indices = {
            (str(item.id), item.origin_domain): index
            for index, item in enumerate(snapshot.attachments)
        }
        if len(source_indices) != len(snapshot.attachments):
            raise ValueError("forward snapshot attachment bindings are ambiguous")
        used: set[int] = set()
        nested_rebound: list[ForwardSnapshotAttachment] = []
        for item in nested.attachments:
            index = source_indices.get((str(item.id), item.origin_domain))
            if (
                index is None
                or index in used
                or forward_snapshot_attachment_semantics(item)
                != forward_snapshot_attachment_semantics(snapshot.attachments[index])
            ):
                raise ValueError("nested forward attachment binding is inconsistent")
            used.add(index)
            nested_rebound.append(rebound[index])
        if used != set(range(len(snapshot.attachments))):
            raise ValueError("nested forward snapshot omits an attachment")
        nested.attachments = nested_rebound
        snapshot.message_snapshots = [nested.model_dump(mode="json", exclude_none=True)]
    snapshot.attachments = rebound
    return snapshot.model_dump(mode="json", exclude_none=True)
