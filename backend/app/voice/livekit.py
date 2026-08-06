from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from livekit import api

from app.core.settings import Settings

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
                    can_publish_data=False,
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
        if settings.voice_api_key is None or settings.voice_api_secret is None:
            raise LiveKitError("LiveKit credentials are not configured")
        self.settings = settings

    def _client(self) -> Any:
        return api.LiveKitAPI(
            url=self.settings.voice_livekit_url,
            api_key=cast(Any, self.settings.voice_api_key).get_secret_value(),
            api_secret=cast(Any, self.settings.voice_api_secret).get_secret_value(),
        )

    async def ensure_room(self, room: str) -> None:
        try:
            async with self._client() as client:
                rooms = await client.room.list_rooms(api.ListRoomsRequest(names=[room]))
                if rooms.rooms:
                    return
                try:
                    await client.room.create_room(
                        api.CreateRoomRequest(name=room, empty_timeout=300)
                    )
                except Exception:
                    # A concurrent first mint may have won the create race. Verify
                    # the room before treating the conflict as an outage.
                    rooms = await client.room.list_rooms(api.ListRoomsRequest(names=[room]))
                    if not rooms.rooms:
                        raise
        except Exception as exc:
            raise LiveKitError("LiveKit room creation failed") from exc

    async def remove_participant(self, room: str, identity: str) -> None:
        try:
            async with self._client() as client:
                await client.room.remove_participant(
                    api.RoomParticipantIdentity(room=room, identity=identity)
                )
        except Exception as exc:
            raise LiveKitError("LiveKit participant removal failed") from exc

    async def delete_room(self, room: str) -> None:
        """Delete a room and disconnect every participant in it."""

        try:
            async with self._client() as client:
                await client.room.delete_room(api.DeleteRoomRequest(room=room))
        except Exception as exc:
            raise LiveKitError("LiveKit room deletion failed") from exc

    async def list_rooms(self) -> list[Any]:
        try:
            async with self._client() as client:
                response = await client.room.list_rooms(api.ListRoomsRequest())
                return list(response.rooms)
        except Exception as exc:
            raise LiveKitError("LiveKit room listing failed") from exc

    async def update_participant(
        self,
        room: str,
        identity: str,
        *,
        can_speak: bool,
        can_stream: bool,
        can_subscribe: bool,
    ) -> None:
        sources = publication_sources(can_speak=can_speak, can_stream=can_stream)
        source_values = [source.upper() for source in sources]
        try:
            async with self._client() as client:
                await client.room.update_participant(
                    api.UpdateParticipantRequest(
                        room=room,
                        identity=identity,
                        permission=api.ParticipantPermission(
                            can_subscribe=can_subscribe,
                            can_publish=bool(sources),
                            can_publish_data=False,
                            can_publish_sources=source_values,
                        ),
                    )
                )
        except Exception as exc:
            raise LiveKitError("LiveKit participant update failed") from exc

    async def list_participants(self, room: str) -> list[Any]:
        try:
            async with self._client() as client:
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
