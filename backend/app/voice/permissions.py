from collections.abc import Mapping

from app.core.permissions import Permission
from app.core.types import EntityRef
from app.voice.schemas import CurrentUserVoiceStateUpdate

# Keep direct-human, direct-bot, and federated Stage permission gates on one
# contract. Discord intentionally gives the Stage-instance resource and the
# voice-state resource different permission rules.
STAGE_INSTANCE_VIEW_PERMISSIONS = Permission.VIEW_CHANNEL
STAGE_INSTANCE_MODERATOR_PERMISSIONS = (
    Permission.MANAGE_CHANNELS | Permission.MUTE_MEMBERS | Permission.MOVE_MEMBERS
)
STAGE_VOICE_STATE_READ_PERMISSIONS = Permission.CONNECT
STAGE_VOICE_STATE_MODERATOR_PERMISSIONS = Permission.MUTE_MEMBERS
VOICE_CHANNEL_ACCESS_PERMISSIONS = Permission.VIEW_CHANNEL | Permission.CONNECT


def stage_voice_state_read_permissions(
    *,
    actor_id: int,
    actor_domain: str,
    target_ref: EntityRef,
    default_domain: str,
) -> Permission:
    """Discord permits reading your own joined Stage state without CONNECT."""

    return (
        Permission(0)
        if target_ref.resolve(default_domain) == (actor_id, actor_domain)
        else STAGE_VOICE_STATE_READ_PERMISSIONS
    )


def current_stage_voice_state_permissions(
    payload: CurrentUserVoiceStateUpdate,
) -> Permission:
    """Return the installation/live permission mask implied by a self patch."""

    required = Permission(0)
    if "suppress" in payload.model_fields_set and payload.suppress is False:
        required |= STAGE_VOICE_STATE_MODERATOR_PERMISSIONS
    if payload.request_to_speak_timestamp is not None:
        required |= Permission.REQUEST_TO_SPEAK
    return required


def federated_stage_voice_state_permissions(
    operation: str,
    payload: Mapping[str, object],
    *,
    actor_id: int,
    actor_domain: str,
    default_domain: str,
) -> Permission:
    """Strictly derive a federated bot's payload-dependent Stage grant mask."""

    if operation == "stage_voice_state.get":
        if set(payload) != {"user_ref"} or not isinstance(payload["user_ref"], str):
            raise ValueError("Stage voice-state get payload is invalid")
        return stage_voice_state_read_permissions(
            actor_id=actor_id,
            actor_domain=actor_domain,
            target_ref=EntityRef(payload["user_ref"]),
            default_domain=default_domain,
        )
    if operation == "stage_voice_state.self":
        if set(payload) != {"data"} or not isinstance(payload["data"], dict):
            raise ValueError("Stage self voice-state payload is invalid")
        update = CurrentUserVoiceStateUpdate.model_validate(payload["data"])
        return current_stage_voice_state_permissions(update)
    raise ValueError("Stage voice-state operation has no dynamic permission contract")
