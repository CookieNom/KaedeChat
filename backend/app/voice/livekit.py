from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import aiohttp
import structlog
from fastapi import HTTPException
from livekit import api
from sqlalchemy import select

from app.core.settings import Settings
from app.db.models import RTCRoomPlacement
from app.voice.rtc import (
    RoutingHints,
    configuration,
    placement_lock,
    placement_settings,
    provider_settings,
    room_settings,
    rtc_session,
    seal,
    select_endpoint,
)

log = structlog.get_logger()


class LiveKitError(RuntimeError):
    """A LiveKit control-plane operation failed."""


def publication_sources(*, can_speak: bool, can_stream: bool) -> list[str]:
    sources: list[str] = []
    if can_speak:
        sources.append("microphone")
    if can_stream:
        sources.extend(("camera", "screen_share", "screen_share_audio"))
    return sources


def mint_join_token(
    settings: Settings,
    *,
    room: str,
    identity: str,
    display_name: str,
    metadata: dict[str, object],
    can_speak: bool,
    can_stream: bool,
    can_subscribe: bool = True,
    can_publish_data: bool = False,
) -> tuple[str, datetime]:
    if settings.voice_api_key is None or settings.voice_api_secret is None:
        raise LiveKitError("LiveKit credentials are not configured")
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.voice_token_ttl_seconds)
    sources = publication_sources(can_speak=can_speak, can_stream=can_stream)
    try:
        token = (
            api.AccessToken(
                settings.voice_api_key.get_secret_value(),
                settings.voice_api_secret.get_secret_value(),
            )
            .with_identity(identity)
            .with_name(display_name)
            .with_metadata(json.dumps(metadata, separators=(",", ":"), sort_keys=True))
            .with_attributes(
                {
                    "kaede.generation": str(metadata["generation"]),
                    "kaede.user_domain": str(metadata["user_domain"]),
                }
            )
            .with_ttl(timedelta(seconds=settings.voice_token_ttl_seconds))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room,
                    can_publish=bool(sources),
                    can_subscribe=can_subscribe,
                    can_publish_data=can_publish_data,
                    can_publish_sources=sources,
                    can_update_own_metadata=False,
                )
            )
            .to_jwt()
        )
    except Exception as exc:
        raise LiveKitError("could not mint a LiveKit join token") from exc
    return token, expires_at


class LiveKitControl:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    @asynccontextmanager
    async def direct_client(
        settings: Settings,
        latency: dict[str, float] | None = None,
    ) -> AsyncIterator[Any]:
        if settings.voice_api_key is None or settings.voice_api_secret is None:
            raise LiveKitError("RTC project credentials are not configured")
        headers = {}
        if latency:
            bounded: dict[str, float] = {}
            for region, milliseconds in sorted(latency.items(), key=lambda item: item[1]):
                bounded[region] = milliseconds
                if len(json.dumps(bounded, separators=(",", ":"))) > 4096:
                    bounded.pop(region)
                    break
            headers["X-Cinnamon-Client-Latency"] = json.dumps(bounded, separators=(",", ":"))
        trace = aiohttp.TraceConfig()

        async def reject_redirect(*args: Any) -> None:
            raise LiveKitError("RTC endpoints must not redirect; check the configured URL")

        trace.on_request_redirect.append(reject_redirect)
        async with (
            aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                trace_configs=[trace],
            ) as transport,
            api.LiveKitAPI(
                url=settings.voice_livekit_url,
                api_key=settings.voice_api_key.get_secret_value(),
                api_secret=settings.voice_api_secret.get_secret_value(),
                session=transport,
            ) as client,
        ):
            yield client

    @asynccontextmanager
    async def _client(self, room: str) -> AsyncIterator[Any]:
        resolved = await room_settings(self.settings, room)
        async with self.direct_client(resolved) as client:
            yield client

    async def ensure_room(
        self,
        room: str,
        hints: RoutingHints | None = None,
        *,
        channel_region: str | None = None,
    ) -> Settings:
        try:
            async with rtc_session(self.settings) as session:
                await placement_lock(session, self.settings.domain, room)
                row = await session.get(RTCRoomPlacement, (self.settings.domain, room))
                if row is not None and not row.finished:
                    # Always use the saved project and endpoint, including after
                    # an administrator switches provider or rotates credentials.
                    resolved = await room_settings(self.settings, room)
                    async with self.direct_client(resolved) as client:
                        listed = await client.room.list_rooms(api.ListRoomsRequest(names=[room]))
                    if listed.rooms:
                        return resolved
                if row is not None:
                    await session.delete(row)
                    await session.flush()
                config = await configuration(session, self.settings)
                if (
                    config.allow_region_selection
                    and channel_region
                    and (hints is None or hints.region is None)
                    and channel_region in {r.id for r in config.regions if r.enabled}
                ):
                    hints = (hints or RoutingHints()).model_copy(update={"region": channel_region})
                resolved = self.settings
                provider = "builtin"
                # Preserve untracked built-in rooms during the first migration.
                legacy_exists = False
                if row is None and self.settings.voice_api_key and self.settings.voice_api_secret:
                    async with self.direct_client(self.settings) as client:
                        listed = await client.room.list_rooms(api.ListRoomsRequest(names=[room]))
                        legacy_exists = bool(listed.rooms)
                if not legacy_exists and config.provider == "cinnamon":
                    resolved = provider_settings(
                        self.settings, config, select_endpoint(config, hints)
                    )
                    provider = "cinnamon"
                latency = None
                if (
                    provider == "cinnamon"
                    and hints
                    and (
                        hints.region == "automatic"
                        or (hints.region is None and config.default_region is None)
                    )
                ):
                    enabled = {r.id for r in config.regions if r.enabled}
                    latency = {k: v for k, v in hints.latency.items() if k in enabled}
                if not legacy_exists:
                    async with self.direct_client(resolved, latency) as client:
                        await client.room.create_room(
                            api.CreateRoomRequest(name=room, empty_timeout=300)
                        )
                if resolved.voice_api_key is None or resolved.voice_api_secret is None:
                    raise LiveKitError("RTC project credentials are not configured")
                session.add(
                    RTCRoomPlacement(
                        domain=self.settings.domain,
                        room=room,
                        provider=provider,
                        control_url=resolved.voice_livekit_url,
                        connection_url=resolved.voice_public_url,
                        credentials=seal(
                            self.settings,
                            {
                                "api_key": resolved.voice_api_key.get_secret_value(),
                                "api_secret": resolved.voice_api_secret.get_secret_value(),
                            },
                        ),
                    )
                )
                await session.commit()
                return resolved
        except HTTPException:
            raise
        except Exception as exc:
            raise LiveKitError(
                "RTC room creation failed; check project permissions and endpoints"
            ) from exc

    async def remove_participant(self, room: str, identity: str) -> None:
        try:
            async with self._client(room) as client:
                await client.room.remove_participant(
                    api.RoomParticipantIdentity(room=room, identity=identity)
                )
        except Exception as exc:
            raise LiveKitError("LiveKit participant removal failed") from exc

    async def delete_room(self, room: str) -> None:
        """Delete a room and disconnect every participant in it."""

        try:
            async with self._client(room) as client:
                await client.room.delete_room(api.DeleteRoomRequest(room=room))
        except Exception as exc:
            raise LiveKitError("LiveKit room deletion failed") from exc

    async def list_rooms(self) -> list[Any]:
        try:
            async with rtc_session(self.settings) as session:
                rows = list(
                    await session.scalars(
                        select(RTCRoomPlacement).where(
                            RTCRoomPlacement.domain == self.settings.domain,
                            RTCRoomPlacement.finished.is_(False),
                        )
                    )
                )
            targets = [self.settings] if self.settings.voice_api_key else []
            seen = {(s.voice_livekit_url, s.voice_api_key, s.voice_api_secret) for s in targets}
            for row in rows:
                resolved = placement_settings(self.settings, row)
                key = (
                    resolved.voice_livekit_url,
                    resolved.voice_api_key,
                    resolved.voice_api_secret,
                )
                if key not in seen:
                    targets.append(resolved)
                    seen.add(key)
            rooms = {}
            successful = 0
            tracked = {row.room for row in rows}
            for target in targets:
                try:
                    async with self.direct_client(target) as client:
                        response = await client.room.list_rooms(api.ListRoomsRequest())
                        successful += 1
                        rooms.update(
                            {
                                room.name: room
                                for room in response.rooms
                                if target._rtc_provider == "builtin" or room.name in tracked
                            }
                        )
                except Exception:
                    log.warning("rtc_provider_room_listing_failed")
            if targets and not successful:
                raise LiveKitError("All configured RTC room listings failed")
            return list(rooms.values())
        except Exception as exc:
            raise LiveKitError("RTC room listing failed") from exc

    async def update_participant(
        self,
        room: str,
        identity: str,
        *,
        can_speak: bool,
        can_stream: bool,
        can_subscribe: bool,
        can_publish_data: bool,
        metadata: dict[str, object] | None = None,
    ) -> None:
        sources = publication_sources(can_speak=can_speak, can_stream=can_stream)
        source_values = [source.upper() for source in sources]
        try:
            async with self._client(room) as client:
                request = api.UpdateParticipantRequest(
                    room=room,
                    identity=identity,
                    permission=api.ParticipantPermission(
                        can_subscribe=can_subscribe,
                        can_publish=bool(sources),
                        can_publish_data=can_publish_data,
                        can_publish_sources=source_values,
                    ),
                )
                if metadata is not None:
                    request.metadata = json.dumps(
                        metadata,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    request.attributes["kaede.generation"] = str(metadata["generation"])
                    request.attributes["kaede.user_domain"] = str(metadata["user_domain"])
                await client.room.update_participant(request)
        except Exception as exc:
            raise LiveKitError("LiveKit participant update failed") from exc

    async def list_participants(self, room: str) -> list[Any]:
        try:
            async with self._client(room) as client:
                response = await client.room.list_participants(
                    api.ListParticipantsRequest(room=room)
                )
                return list(response.participants)
        except Exception as exc:
            raise LiveKitError("LiveKit participant reconciliation failed") from exc


def receive_webhook(settings: Settings, body: str, authorization: str) -> Any:
    if settings.voice_api_key is None or settings.voice_api_secret is None:
        raise LiveKitError("LiveKit credentials are not configured")
    try:
        verifier = api.TokenVerifier(
            settings.voice_api_key.get_secret_value(),
            settings.voice_api_secret.get_secret_value(),
        )
        return api.WebhookReceiver(verifier).receive(body, authorization)
    except Exception as exc:
        raise LiveKitError("invalid LiveKit webhook signature") from exc


def participant_metadata(participant: Any) -> dict[str, object]:
    try:
        parsed = json.loads(str(participant.metadata))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LiveKitError("participant metadata is invalid") from exc
    if not isinstance(parsed, dict):
        raise LiveKitError("participant metadata is invalid")
    return cast(dict[str, object], parsed)


async def screen_share_is_active(
    settings: Settings,
    room: str,
    identity: str,
) -> bool:
    """Confirm that an exact LiveKit participant currently publishes a screen share."""

    participants = await LiveKitControl(settings).list_participants(room)
    screen_share_source = api.TrackSource.Value("SCREEN_SHARE")
    return any(
        str(participant.identity) == identity
        and any(track.source == screen_share_source for track in participant.tracks)
        for participant in participants
    )
