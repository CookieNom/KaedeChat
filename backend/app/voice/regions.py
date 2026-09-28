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


async def instance_voice_regions(settings: Settings) -> list[VoiceRegion]:
    from app.voice.rtc import configuration, rtc_session

    async with rtc_session(settings) as session:
        config = await configuration(session, settings)
    if config.provider == "builtin":
        return configured_voice_regions(settings)
    if not config.allow_region_selection:
        return []
    return [
        VoiceRegion(id=region.id, name=region.name) for region in config.regions if region.enabled
    ]


async def require_instance_rtc_region(settings: Settings, value: str | None) -> str | None:
    if value is None:
        return None
    if value in {region.id for region in await instance_voice_regions(settings)}:
        return value
    raise HTTPException(
        status_code=400, detail={"code": "VOICE_REGION_INVALID", "rtc_region": value}
    )
