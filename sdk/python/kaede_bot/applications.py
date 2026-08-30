from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import TYPE_CHECKING, Any, Literal, cast

from .models import MISSING, MissingType
from .refs import EntityRef
from .wire import (
    strict_payload_bool,
    strict_payload_datetime,
    strict_payload_int,
    strict_payload_sha256,
    strict_payload_string,
)

if TYPE_CHECKING:
    from .client import Client

ApplicationAssetKind = Literal[
    "icon", "cover", "store", "achievement", "activity", "other"
]
_APPLICATION_ASSET_KINDS = frozenset(
    {"icon", "cover", "store", "achievement", "activity", "other"}
)
_APPLICATION_IMAGE_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
_APPLICATION_EMOJI_NAME = re.compile(r"^[A-Za-z0-9_]{2,32}$")
_MAX_INT32 = 2_147_483_647


def _application_resource_ref(
    payload: dict[str, Any],
    application_ref: EntityRef,
    label: str,
) -> EntityRef:
    ref = EntityRef.from_wire(payload["id"], application_ref.domain)
    if "ref" in payload and EntityRef.parse(payload["ref"]) != ref:
        raise ValueError(f"{label} ref conflicts with its id and application authority")
    return ref


def _application_timestamps(
    payload: dict[str, Any],
    label: str,
) -> tuple[datetime, datetime]:
    created_at = strict_payload_datetime(payload["created_at"], f"{label} created_at")
    updated_at = strict_payload_datetime(payload["updated_at"], f"{label} updated_at")
    if updated_at < created_at:
        raise ValueError(f"{label} updated_at cannot precede created_at")
    return created_at, updated_at


def _asset_dimensions(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    width_present = "width" in payload
    height_present = "height" in payload
    if not width_present and not height_present:
        # Legacy SDK fixtures predate explicit nullable dimension fields.
        return None, None
    if width_present != height_present:
        raise ValueError("application asset dimensions are incomplete")
    width = payload["width"]
    height = payload["height"]
    if width is None and height is None:
        return None, None
    if width is None or height is None:
        raise ValueError("application asset dimensions must both be null or positive")
    return (
        strict_payload_int(
            width,
            "application asset width",
            minimum=1,
            maximum=_MAX_INT32,
        ),
        strict_payload_int(
            height,
            "application asset height",
            minimum=1,
            maximum=_MAX_INT32,
        ),
    )


@dataclass(slots=True)
class ApplicationAsset:
    client: Client
    target: str
    ref: EntityRef
    application_ref: EntityRef
    kind: ApplicationAssetKind
    name: str
    media_hash: str
    content_type: str
    width: int | None
    height: int | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> ApplicationAsset:
        application_ref = EntityRef.parse(payload["application_ref"])
        ref = _application_resource_ref(payload, application_ref, "application asset")
        kind = payload["kind"]
        if not isinstance(kind, str) or kind not in _APPLICATION_ASSET_KINDS:
            raise ValueError("application asset kind is invalid")
        name = strict_payload_string(
            payload["name"],
            "application asset name",
            minimum=1,
            maximum=100,
        )
        if not name.strip():
            raise ValueError("application asset name must not be blank")
        content_type = strict_payload_string(
            payload["content_type"], "application asset content_type"
        )
        if content_type not in _APPLICATION_IMAGE_TYPES:
            raise ValueError("application asset content_type is invalid")
        width, height = _asset_dimensions(payload)
        created_at, updated_at = _application_timestamps(payload, "application asset")
        return cls(
            client=client,
            target=target,
            ref=ref,
            application_ref=application_ref,
            kind=cast(ApplicationAssetKind, kind),
            name=name,
            media_hash=strict_payload_sha256(
                payload["media_hash"], "application asset media_hash"
            ),
            content_type=content_type,
            width=width,
            height=height,
            version=strict_payload_int(
                payload.get("version", 1),
                "application asset version",
                minimum=1,
                maximum=_MAX_INT32,
            ),
            created_at=created_at,
            updated_at=updated_at,
        )

    async def delete(self) -> None:
        await self.client.delete_application_asset(self.ref.id, target=self.target)

    async def edit(
        self,
        *,
        kind: ApplicationAssetKind | MissingType = MISSING,
        name: str | MissingType = MISSING,
    ) -> ApplicationAsset:
        return await self.client.edit_application_asset(
            self.ref.id,
            target=self.target,
            kind=kind,
            name=name,
        )


@dataclass(slots=True)
class ApplicationEmoji:
    client: Client
    target: str
    ref: EntityRef
    application_ref: EntityRef
    name: str
    media_hash: str
    animated: bool
    available: bool
    creator_ref: EntityRef
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> ApplicationEmoji:
        application_ref = EntityRef.parse(payload["application_ref"])
        ref = _application_resource_ref(payload, application_ref, "application emoji")
        name = strict_payload_string(payload["name"], "application emoji name")
        if _APPLICATION_EMOJI_NAME.fullmatch(name) is None:
            raise ValueError("application emoji name is invalid")
        created_at, updated_at = _application_timestamps(payload, "application emoji")
        return cls(
            client=client,
            target=target,
            ref=ref,
            application_ref=application_ref,
            name=name,
            media_hash=strict_payload_sha256(
                payload["media_hash"], "application emoji media_hash"
            ),
            animated=strict_payload_bool(payload, "animated", default=False),
            available=strict_payload_bool(payload, "available", default=True),
            creator_ref=EntityRef.from_wire(
                payload["creator_id"], payload["creator_domain"]
            ),
            version=strict_payload_int(
                payload.get("version", 1),
                "application emoji version",
                minimum=1,
                maximum=_MAX_INT32,
            ),
            created_at=created_at,
            updated_at=updated_at,
        )

    @property
    def token(self) -> str:
        prefix = "a" if self.animated else ""
        return f"<{prefix}:{self.name}:{self.ref}>"

    async def edit(
        self,
        *,
        name: str,
    ) -> ApplicationEmoji:
        return await self.client.edit_application_emoji(
            self.ref.id,
            target=self.target,
            name=name,
        )

    async def delete(self) -> None:
        await self.client.delete_application_emoji(self.ref.id, target=self.target)


__all__ = ["ApplicationAsset", "ApplicationAssetKind", "ApplicationEmoji"]
