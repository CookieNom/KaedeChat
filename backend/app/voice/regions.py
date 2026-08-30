from __future__ import annotations

from fastapi import HTTPException

from app.core.settings import Settings
from app.voice.schemas import VoiceRegion


def configured_voice_regions(settings: Settings) -> list[VoiceRegion]:
    """Return a detached public representation of the configured catalog."""

    return [VoiceRegion.model_validate(region.model_dump()) for region in settings.voice_regions]


def require_configured_rtc_region(settings: Settings, value: str | None) -> str | None:
    """Resolve a channel RTC region against this voice authority's catalog.

    ``None`` deliberately remains Discord's automatic-region selection. Region
    IDs are otherwise authority-owned configuration, never arbitrary client
    provider input.
    """

    if value is None:
        return None
    configured = {region.id: region.id for region in settings.voice_regions}
    try:
        return configured[value]
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VOICE_REGION_INVALID", "rtc_region": value},
        ) from exc
