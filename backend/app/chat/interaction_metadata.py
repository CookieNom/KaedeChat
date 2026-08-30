from __future__ import annotations

from typing import Any

from app.bots.interaction_owners import normalize_authorizing_integration_owners
from app.core.types import EntityRef, validate_wire_snowflake

INTERACTION_TYPES = {"command", "component", "modal_submit"}
COMMAND_TYPES = {"chat_input", "user", "message"}
INTEGRATION_TYPES = {"guild_install", "user_install", "dm_capability"}


def _qualified_ref(value: object, label: str) -> tuple[int, str]:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = EntityRef(value)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.domain is None:
        raise ValueError(f"{label} must be qualified")
    return parsed.id, parsed.domain


def _profile(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "origin_domain",
        "username",
        "display_name",
        "avatar_hash",
        "bot",
    }:
        raise ValueError(f"{label} is invalid")
    user_id = validate_wire_snowflake(value.get("id"))
    user_ref = _qualified_ref(
        f"{value.get('id')}@{value.get('origin_domain')}",
        f"{label} reference",
    )
    username = value.get("username")
    display_name = value.get("display_name")
    avatar_hash = value.get("avatar_hash")
    bot = value.get("bot")
    if (
        user_ref[0] != user_id
        or not isinstance(username, str)
        or not 1 <= len(username) <= 32
        or display_name is not None
        and (not isinstance(display_name, str) or not 1 <= len(display_name) <= 32)
        or avatar_hash is not None
        and (not isinstance(avatar_hash, str) or not 1 <= len(avatar_hash) <= 128)
        or not isinstance(bot, bool)
    ):
        raise ValueError(f"{label} is invalid")
    return {
        "id": str(user_id),
        "origin_domain": user_ref[1],
        "username": username,
        "display_name": display_name,
        "avatar_hash": avatar_hash,
        "bot": bot,
    }


def _message_ref(
    value: dict[str, Any],
    prefix: str,
) -> tuple[int, str] | None:
    raw_id = value.get(f"{prefix}_id")
    raw_domain = value.get(f"{prefix}_domain")
    raw_ref = value.get(f"{prefix}_ref")
    if raw_id is None and raw_domain is None and raw_ref is None:
        return None
    if raw_id is None or raw_domain is None or raw_ref is None:
        raise ValueError(f"interaction {prefix.replace('_', ' ')} is incomplete")
    parsed_id = validate_wire_snowflake(raw_id)
    parsed_ref = _qualified_ref(raw_ref, f"interaction {prefix.replace('_', ' ')}")
    if (
        parsed_ref
        != _qualified_ref(
            f"{raw_id}@{raw_domain}",
            f"interaction {prefix.replace('_', ' ')}",
        )
        or parsed_ref[0] != parsed_id
    ):
        raise ValueError(f"interaction {prefix.replace('_', ' ')} is inconsistent")
    return parsed_ref


def _validated_core(value: object, *, depth: int = 0) -> dict[str, object]:
    if not isinstance(value, dict) or depth > 2:
        raise ValueError("interaction metadata is invalid")
    allowed = {
        "id",
        "origin_domain",
        "interaction_ref",
        "type",
        "user",
        "user_ref",
        "application_ref",
        "integration_type",
        "authorizing_integration_owners",
        "command_name",
        "command_type",
        "target_user",
        "target_user_ref",
        "target_message_id",
        "target_message_domain",
        "target_message_ref",
        "original_response_message_id",
        "original_response_message_domain",
        "original_response_message_ref",
        "interacted_message_id",
        "interacted_message_domain",
        "interacted_message_ref",
        "triggering_interaction_metadata",
    }
    if set(value) - allowed:
        raise ValueError("interaction metadata contains unknown fields")
    interaction_id = validate_wire_snowflake(value.get("id"))
    interaction_ref = _qualified_ref(value.get("interaction_ref"), "interaction reference")
    if (
        interaction_ref
        != _qualified_ref(
            f"{value.get('id')}@{value.get('origin_domain')}",
            "interaction reference",
        )
        or interaction_ref[0] != interaction_id
    ):
        raise ValueError("interaction reference is inconsistent")
    interaction_type = value.get("type")
    integration_type = value.get("integration_type")
    if interaction_type not in INTERACTION_TYPES or integration_type not in INTEGRATION_TYPES:
        raise ValueError("interaction type is invalid")
    user = _profile(value.get("user"), "interaction user")
    user_ref = _qualified_ref(value.get("user_ref"), "interaction user reference")
    if user_ref != (int(str(user["id"])), str(user["origin_domain"])):
        raise ValueError("interaction user reference is inconsistent")
    application_ref = _qualified_ref(value.get("application_ref"), "interaction application")
    try:
        owners = normalize_authorizing_integration_owners(
            value.get("authorizing_integration_owners")
        )
    except ValueError as exc:
        raise ValueError("interaction authorizing owners are invalid") from exc
    if (
        integration_type == "guild_install"
        and "guild_install" not in owners
        or integration_type == "user_install"
        and "user_install" not in owners
    ):
        raise ValueError("interaction authorizing owners are invalid")

    command_name = value.get("command_name")
    command_type = value.get("command_type")
    target_user = value.get("target_user")
    target_user_ref = value.get("target_user_ref")
    target_message_ref = _message_ref(value, "target_message")
    _message_ref(value, "original_response_message")
    interacted_message_ref = _message_ref(value, "interacted_message")
    triggering = value.get("triggering_interaction_metadata")
    if interaction_type == "command":
        if (
            not isinstance(command_name, str)
            or not 1 <= len(command_name) <= 32
            or command_type not in COMMAND_TYPES
            or interacted_message_ref is not None
            or triggering is not None
        ):
            raise ValueError("application-command interaction metadata is invalid")
        if command_type == "user":
            parsed_target = _profile(target_user, "interaction target user")
            parsed_target_ref = _qualified_ref(
                target_user_ref,
                "interaction target user reference",
            )
            if (
                parsed_target_ref
                != (
                    int(str(parsed_target["id"])),
                    str(parsed_target["origin_domain"]),
                )
                or target_message_ref is not None
            ):
                raise ValueError("user-command interaction target is invalid")
        elif target_user is not None or target_user_ref is not None:
            raise ValueError("interaction target user is invalid")
        if (command_type == "message") != (target_message_ref is not None):
            raise ValueError("message-command interaction target is invalid")
    elif interaction_type == "component":
        if any(
            item is not None for item in (command_name, command_type, target_user, target_user_ref)
        ):
            raise ValueError("component interaction metadata is invalid")
        if (
            interacted_message_ref is None
            or target_message_ref is not None
            or triggering is not None
        ):
            raise ValueError("component interaction source is invalid")
    else:
        if (
            any(
                item is not None
                for item in (
                    command_name,
                    command_type,
                    target_user,
                    target_user_ref,
                    target_message_ref,
                    interacted_message_ref,
                )
            )
            or triggering is None
        ):
            raise ValueError("modal interaction metadata is invalid")
        triggering = _validated_core(triggering, depth=depth + 1)

    normalized = dict(value)
    normalized["id"] = str(interaction_id)
    normalized["origin_domain"] = interaction_ref[1]
    normalized["interaction_ref"] = f"{interaction_ref[0]}@{interaction_ref[1]}"
    normalized["user"] = user
    normalized["user_ref"] = f"{user_ref[0]}@{user_ref[1]}"
    normalized["application_ref"] = f"{application_ref[0]}@{application_ref[1]}"
    normalized["authorizing_integration_owners"] = owners
    if triggering is not None:
        normalized["triggering_interaction_metadata"] = triggering
    return {str(key): item for key, item in normalized.items()}


def validate_interaction_metadata(
    value: object,
    *,
    message_type: int,
    application_ref: tuple[int, str] | None,
    referenced_message_ref: tuple[int, str] | None,
    message_ref: tuple[int, str] | None = None,
) -> dict[str, object] | None:
    if value is None:
        if message_type in {20, 23}:
            raise ValueError("application-command message is missing interaction metadata")
        return None
    metadata = _validated_core(value)
    metadata_application = _qualified_ref(
        metadata["application_ref"],
        "interaction application",
    )
    if application_ref != metadata_application:
        raise ValueError("interaction application does not match its message")
    command_type = metadata.get("command_type")
    expected_message_type = (
        20
        if metadata.get("type") == "command" and command_type == "chat_input"
        else 23
        if metadata.get("type") == "command"
        else 0
    )
    if message_type != expected_message_type:
        raise ValueError("interaction metadata does not match its message type")
    target_ref = _message_ref(metadata, "target_message")
    original_response_ref = _message_ref(metadata, "original_response_message")
    if command_type == "message" and original_response_ref is None:
        if referenced_message_ref != target_ref:
            raise ValueError("message-command response lost its target reference")
    elif referenced_message_ref is not None and message_type != 19:
        raise ValueError("interaction response has an unexpected message reference")
    if message_ref is not None:
        interaction_ref = _qualified_ref(metadata["interaction_ref"], "interaction reference")
        if interaction_ref[1] != message_ref[1] or interaction_ref >= message_ref:
            raise ValueError("interaction metadata is not ordered before its response")
    return metadata
