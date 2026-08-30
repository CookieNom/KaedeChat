from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from .audio import FFmpegAudioSource
from .media_urls import validate_signed_media_url
from .models import MISSING, MissingType
from .refs import EntityRef, User, canonical_federation_domain
from .wire import (
    strict_payload_bool,
    strict_payload_decimal_int,
    strict_payload_int,
    strict_payload_number,
    strict_payload_sha256,
    strict_payload_string,
)

if TYPE_CHECKING:
    from .client import Client
    from .voice import VoiceClient

SOUNDBOARD_MAX_BYTES = 512 * 1024
SOUNDBOARD_MAX_DURATION_MS = 5_200
_MAX_VERSION = 2_147_483_647
_SOUNDBOARD_CONTENT_TYPES = frozenset({"audio/mpeg", "audio/ogg"})


def _optional_soundboard_ref(
    payload: dict[str, Any],
    id_key: str,
    domain_key: str,
    label: str,
) -> EntityRef | None:
    id_present = id_key in payload
    domain_present = domain_key in payload
    if not id_present and not domain_present:
        # Legacy payloads omitted both nullable reference fields.
        return None
    if id_present != domain_present:
        raise ValueError(f"soundboard response has an incomplete {label} reference")
    raw_id = payload[id_key]
    raw_domain = payload[domain_key]
    if raw_id is None and raw_domain is None:
        return None
    if raw_id is None or raw_domain is None:
        raise ValueError(f"soundboard response has an incomplete {label} reference")
    return EntityRef.from_wire(raw_id, raw_domain)


def _validate_soundboard_download_url(
    location: str,
    authority_domain: str,
    media_origin: str,
) -> str:
    """Bind a bearer download to its media authority's signed HTTPS origin."""

    try:
        canonical_federation_domain(authority_domain)
        return validate_signed_media_url(location, media_origin)
    except ValueError as exc:
        raise RuntimeError(
            "soundboard play URL is outside the guild authority's signed HTTPS media origin"
        ) from exc


@dataclass(slots=True)
class SoundboardSound:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef | None
    name: str
    media_hash: str
    content_type: str
    volume: float
    duration_ms: int
    emoji_ref: EntityRef | None = None
    emoji_name: str | None = None
    available: bool = True
    creator_ref: EntityRef | None = None
    version: int = 1

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> SoundboardSound:
        ref = EntityRef.from_wire(payload["id"], payload["origin_domain"])
        guild_ref = _optional_soundboard_ref(
            payload, "guild_id", "guild_domain", "guild"
        )
        emoji_ref = _optional_soundboard_ref(
            payload, "emoji_id", "emoji_domain", "emoji"
        )
        creator_ref = _optional_soundboard_ref(
            payload, "created_by_id", "created_by_domain", "creator"
        )
        if guild_ref is not None and guild_ref.domain != ref.domain:
            raise ValueError(
                "soundboard response does not belong to the requested resource: "
                "sound and guild authorities conflict"
            )
        if emoji_ref is not None and (
            guild_ref is None or emoji_ref.domain != guild_ref.domain
        ):
            raise ValueError(
                "soundboard response does not belong to the requested resource: "
                "emoji and guild authorities conflict"
            )
        emoji_name = (
            strict_payload_string(
                payload["emoji_name"],
                "soundboard emoji_name",
                minimum=1,
                maximum=64,
            )
            if payload.get("emoji_name") is not None
            else None
        )
        if emoji_ref is not None and emoji_name is not None:
            raise ValueError(
                "soundboard emoji ref and emoji_name are mutually exclusive"
            )
        media_hash = strict_payload_sha256(
            payload["media_hash"], "soundboard media_hash"
        )
        content_type = strict_payload_string(
            payload["content_type"], "soundboard content_type"
        )
        if content_type not in _SOUNDBOARD_CONTENT_TYPES:
            raise ValueError("soundboard content_type is invalid")
        if "version" in payload:
            version = strict_payload_decimal_int(
                payload["version"],
                "soundboard version",
                minimum=1,
                maximum=_MAX_VERSION,
            )
        else:
            # Legacy bot/Gateway payloads predate the explicit revision field.
            version = 1
        raw_user = payload.get("user")
        if raw_user is not None:
            if not isinstance(raw_user, dict):
                raise ValueError("soundboard creator user is invalid")
            creator = User.from_payload(raw_user)
            if creator_ref is None or creator.ref != creator_ref:
                raise ValueError(
                    "soundboard creator user conflicts with created_by ref"
                )
        return cls(
            client=client,
            target=target,
            ref=ref,
            guild_ref=guild_ref,
            name=strict_payload_string(
                payload["name"],
                "soundboard name",
                minimum=2,
                maximum=32,
            ),
            media_hash=media_hash,
            content_type=content_type,
            volume=strict_payload_number(
                payload.get("volume", 1),
                "soundboard volume",
                minimum=0,
                maximum=1,
            ),
            duration_ms=strict_payload_int(
                payload["duration_ms"],
                "soundboard duration_ms",
                minimum=1,
                maximum=SOUNDBOARD_MAX_DURATION_MS,
            ),
            emoji_ref=emoji_ref,
            emoji_name=emoji_name,
            available=strict_payload_bool(payload, "available", default=True),
            creator_ref=creator_ref,
            version=version,
        )

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        volume: float | MissingType = MISSING,
        emoji: EntityRef | str | None | MissingType = MISSING,
        reason: str | None = None,
    ) -> SoundboardSound:
        if self.guild_ref is None:
            raise RuntimeError("default soundboard sounds cannot be edited")
        updated = await self.client.edit_soundboard_sound(
            self.guild_ref,
            self.ref,
            target=self.target,
            name=name,
            volume=volume,
            emoji=emoji,
            reason=reason,
        )
        return updated

    async def delete(self, *, reason: str | None = None) -> None:
        if self.guild_ref is None:
            raise RuntimeError("default soundboard sounds cannot be deleted")
        await self.client.delete_soundboard_sound(
            self.guild_ref,
            self.ref,
            target=self.target,
            reason=reason,
        )

    async def play(self, voice: VoiceClient, *, volume: float | None = None) -> None:
        if (
            voice.grant.guild_ref is None
            or voice.grant.bot_installation_revision is None
        ):
            raise RuntimeError(
                "soundboard playback requires a current guild voice installation grant"
            )
        source_guild = str(self.guild_ref) if self.guild_ref is not None else "default"
        resources = {
            "operation": "soundboard.play",
            "sound_ref": str(self.ref),
            "sound_version": str(self.version),
            "source_guild_ref": source_guild,
            "target_channel_ref": str(voice.grant.channel_ref),
            "target_guild_ref": str(voice.grant.guild_ref),
            "target_installation_revision": str(voice.grant.bot_installation_revision),
            "volume": "default" if volume is None else float(volume).hex(),
        }
        payload: dict[str, object] = {
            "sound_id": str(self.ref),
            "sound_version": str(self.version),
            "volume": volume,
            "actor_intent": await self.client._federated_actor_intent(
                action="soundboard.play",
                audience=self.client._authority_target(self.ref),
                runtime_target=voice.target,
                resources=resources,
            ),
        }
        if self.guild_ref is not None:
            payload["source_guild_id"] = str(self.guild_ref)
        raw = await self.client.request(
            "POST",
            f"/api/v1/bots/channels/{voice.grant.channel_ref}/soundboard-playback-grants",
            target=voice.target,
            json=payload,
        )
        if not isinstance(raw, dict):
            raise RuntimeError("soundboard play grant is invalid")
        location = _validate_soundboard_download_url(
            str(raw.get("download_url", "")),
            str(raw.get("media_authority", "")),
            str(raw.get("media_origin", "")),
        )
        async with (
            httpx.AsyncClient(
                timeout=15, follow_redirects=False, trust_env=False
            ) as media_client,
            media_client.stream("GET", location) as response,
        ):
            if response.is_redirect:
                raise RuntimeError("soundboard play URL redirected unexpectedly")
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError:
                    raise RuntimeError(
                        "soundboard response had an invalid size"
                    ) from None
                if not 0 <= declared_size <= SOUNDBOARD_MAX_BYTES:
                    raise RuntimeError("soundboard response exceeded its size limit")
            media_bytes = bytearray()
            async for chunk in response.aiter_bytes():
                media_bytes.extend(chunk[: SOUNDBOARD_MAX_BYTES + 1 - len(media_bytes)])
                if len(media_bytes) > SOUNDBOARD_MAX_BYTES:
                    raise RuntimeError("soundboard response exceeded its size limit")
        data = bytes(media_bytes)
        if hashlib.sha256(data).hexdigest() != self.media_hash:
            raise RuntimeError("soundboard response failed its integrity check")
        await voice.play(
            FFmpegAudioSource(data),
            volume=float(raw.get("effective_volume", 1)),
        )
