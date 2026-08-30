from __future__ import annotations

from dataclasses import dataclass, field
from typing import NoReturn

from fastapi import HTTPException
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.channel_access import ChannelAccess
from app.chat.payloads import channel_payload, member_payload, role_payload, user_payload
from app.chat.permissions import get_permissions
from app.chat.rich_content import (
    MESSAGE_LAYOUT_COMPONENT_ADAPTER,
    Button,
    ChannelSelect,
    CheckboxGroup,
    CheckboxV2,
    FileUpload,
    Label,
    MentionableSelect,
    Modal,
    RadioGroup,
    RoleSelect,
    StringSelect,
    TextDisplay,
    TextInput,
    UserSelect,
    walk_component_tree,
)
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import Channel, GuildMember, MemberRole, Role, User

InteractiveComponent = (
    Button | StringSelect | UserSelect | RoleSelect | MentionableSelect | ChannelSelect
)
ModalInput = (
    TextInput
    | StringSelect
    | UserSelect
    | RoleSelect
    | MentionableSelect
    | ChannelSelect
    | FileUpload
    | RadioGroup
    | CheckboxGroup
    | CheckboxV2
)
EntitySelect = UserSelect | RoleSelect | MentionableSelect | ChannelSelect


@dataclass(frozen=True, slots=True)
class ValidatedComponentSubmission:
    component: InteractiveComponent
    component_type: int | str
    values: list[str]


@dataclass(frozen=True, slots=True)
class ValidatedModalSubmission:
    modal: Modal
    components: list[dict[str, object]]
    entity_fields: list[tuple[EntitySelect, list[str]]]
    file_fields: list[tuple[FileUpload, list[str]]]


def _invalid(code: str, message: str, *, status_code: int = 422) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _component_type(component: InteractiveComponent | ModalInput) -> int | str:
    return component.type


def _normalize_select_values(
    component: StringSelect | EntitySelect,
    raw_values: object,
) -> list[str]:
    if not isinstance(raw_values, list) or any(not isinstance(item, str) for item in raw_values):
        _invalid("COMPONENT_VALUES_INVALID", "This control submitted an invalid selection.")
    values = list(raw_values)
    if len(values) != len(set(values)):
        _invalid("COMPONENT_VALUES_INVALID", "A control cannot select the same value twice.")
    if not component.min_values <= len(values) <= component.max_values:
        _invalid(
            "COMPONENT_VALUES_INVALID",
            (
                f"Choose between {component.min_values} and {component.max_values} "
                "items for this control."
            ),
        )
    if isinstance(component, StringSelect):
        allowed = {option.value for option in component.options}
        if not set(values) <= allowed:
            _invalid(
                "COMPONENT_VALUES_INVALID",
                "Choose only options that are currently available in this control.",
            )
    return values


def validate_component_submission(
    source_rows: object,
    custom_id: str,
    raw_values: object,
) -> ValidatedComponentSubmission:
    """Bind a browser submission to one exact application-authored component."""

    component = resolve_interactive_component(source_rows, custom_id)
    if isinstance(component, Button):
        if raw_values != []:
            _invalid("COMPONENT_VALUES_INVALID", "Buttons cannot submit selected values.")
        values: list[str] = []
    else:
        values = _normalize_select_values(component, raw_values)
    return ValidatedComponentSubmission(component, _component_type(component), values)


def resolve_interactive_component(
    source_rows: object,
    custom_id: str,
) -> InteractiveComponent:
    """Resolve one enabled control without inspecting its private submission."""

    if not isinstance(source_rows, list):
        _invalid(
            "INTERACTION_VIEW_INVALID",
            "This interaction view is invalid. Ask the bot to send it again.",
            status_code=409,
        )
    try:
        layouts = [MESSAGE_LAYOUT_COMPONENT_ADAPTER.validate_python(row) for row in source_rows]
    except ValidationError:
        _invalid(
            "INTERACTION_VIEW_INVALID",
            "This interaction view is invalid. Ask the bot to send it again.",
            status_code=409,
        )
    matches = [
        component
        for layout in layouts
        for component in walk_component_tree(layout)
        if getattr(component, "custom_id", None) == custom_id
    ]
    if len(matches) != 1 or not isinstance(
        matches[0],
        (Button, StringSelect, UserSelect, RoleSelect, MentionableSelect, ChannelSelect),
    ):
        _invalid(
            "COMPONENT_NOT_FOUND",
            "That control is no longer part of this message.",
            status_code=404,
        )
    component = matches[0]
    if getattr(component, "disabled", False):
        _invalid(
            "COMPONENT_DISABLED",
            "That control is disabled and cannot be submitted.",
            status_code=409,
        )
    return component


def _submitted_modal_input(
    source: ModalInput,
    raw: object,
) -> tuple[
    dict[str, object],
    tuple[EntitySelect, list[str]] | None,
    tuple[FileUpload, list[str]] | None,
]:
    submitted = _validated_modal_input_identity(source, raw)
    if isinstance(source, TextInput):
        return _submitted_text_input(source, submitted), None, None
    if isinstance(source, CheckboxV2):
        return _submitted_checkbox(source, submitted), None, None
    if isinstance(source, RadioGroup):
        return _submitted_radio_group(source, submitted), None, None
    if isinstance(source, (FileUpload, CheckboxGroup)):
        normalized, values = _submitted_multi_value_input(source, submitted)
        return normalized, None, ((source, values) if isinstance(source, FileUpload) else None)
    normalized, values = _submitted_select_input(source, submitted)
    return normalized, ((source, values) if isinstance(source, EntitySelect) else None), None


def _validated_modal_input_identity(source: ModalInput, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        _invalid("MODAL_SUBMISSION_INVALID", "This form contains an invalid field.")
    if raw.get("custom_id") != source.custom_id or raw.get("type") != _component_type(source):
        _invalid(
            "MODAL_SUBMISSION_INVALID",
            "This form no longer matches the form sent by the bot. Run it again.",
            status_code=409,
        )
    return raw


def _submitted_text_input(source: TextInput, raw: dict[str, object]) -> dict[str, object]:
    value = raw.get("value")
    if set(raw) != {"type", "custom_id", "value"} or not isinstance(value, str):
        _invalid("MODAL_SUBMISSION_INVALID", "This form contains an invalid text field.")
    if source.required and not value:
        _invalid("MODAL_SUBMISSION_INVALID", f"{source.label or 'This field'} is required.")
    minimum = source.min_length or 0
    maximum = source.max_length or 4_000
    if not minimum <= len(value) <= maximum:
        _invalid(
            "MODAL_SUBMISSION_INVALID",
            (
                f"{source.label or 'This field'} must contain between "
                f"{minimum} and {maximum} characters."
            ),
        )
    return {"type": source.type, "custom_id": source.custom_id, "value": value}


def _submitted_checkbox(source: CheckboxV2, raw: dict[str, object]) -> dict[str, object]:
    if set(raw) != {"type", "custom_id", "value"} or not isinstance(raw.get("value"), bool):
        _invalid("MODAL_SUBMISSION_INVALID", "This form contains an invalid checkbox.")
    return {"type": source.type, "custom_id": source.custom_id, "value": raw["value"]}


def _submitted_radio_group(source: RadioGroup, raw: dict[str, object]) -> dict[str, object]:
    if set(raw) != {"type", "custom_id", "value"}:
        _invalid("MODAL_SUBMISSION_INVALID", "This form contains an invalid radio group.")
    value = raw["value"]
    if value is None and source.required:
        _invalid("MODAL_SUBMISSION_INVALID", "Choose a radio option.")
    if value is not None and (
        not isinstance(value, str) or value not in {option.value for option in source.options}
    ):
        _invalid("MODAL_SUBMISSION_INVALID", "Choose an available radio option.")
    return {"type": source.type, "custom_id": source.custom_id, "value": value}


def _submitted_multi_value_input(
    source: FileUpload | CheckboxGroup,
    raw: dict[str, object],
) -> tuple[dict[str, object], list[str]]:
    raw_values = raw.get("values")
    if (
        set(raw) != {"type", "custom_id", "values"}
        or not isinstance(raw_values, list)
        or any(not isinstance(value, str) for value in raw_values)
    ):
        _invalid("MODAL_SUBMISSION_INVALID", "This form contains an invalid multi-value field.")
    values = list(raw_values)
    if len(values) != len(set(values)):
        _invalid("MODAL_SUBMISSION_INVALID", "A field cannot submit duplicate values.")
    max_values = (
        source.max_values
        if source.max_values is not None
        else len(source.options)
        if isinstance(source, CheckboxGroup)
        else 0
    )
    if not source.min_values <= len(values) <= max_values:
        _invalid(
            "MODAL_SUBMISSION_INVALID",
            f"Choose between {source.min_values} and {max_values} values.",
        )
    if isinstance(source, CheckboxGroup) and not set(values) <= {
        option.value for option in source.options
    }:
        _invalid("MODAL_SUBMISSION_INVALID", "Choose only available checkbox options.")
    return {"type": source.type, "custom_id": source.custom_id, "values": values}, values


def _submitted_select_input(
    source: StringSelect | EntitySelect,
    raw: dict[str, object],
) -> tuple[dict[str, object], list[str]]:
    if set(raw) != {"type", "custom_id", "values"}:
        _invalid("MODAL_SUBMISSION_INVALID", "This form contains an invalid select field.")
    values = _normalize_select_values(source, raw.get("values"))
    return {"type": source.type, "custom_id": source.custom_id, "values": values}, values


def _submitted_label_row(
    source: Label,
    submitted: object,
) -> tuple[
    dict[str, object],
    tuple[EntitySelect, list[str]] | None,
    tuple[FileUpload, list[str]] | None,
]:
    if (
        not isinstance(submitted, dict)
        or not set(submitted) <= {"type", "id", "component"}
        or submitted.get("type") != 18
        or "component" not in submitted
    ):
        _invalid(
            "MODAL_SUBMISSION_INVALID",
            "This form no longer matches the form sent by the bot. Run it again.",
        )
    normalized, entity_field, file_field = _submitted_modal_input(
        source.component,
        submitted["component"],
    )
    row: dict[str, object] = {"type": 18, "component": normalized}
    if source.id is not None:
        row["id"] = source.id
    return row, entity_field, file_field


def _submitted_action_row(
    source: object,
    submitted: object,
) -> tuple[
    dict[str, object],
    tuple[EntitySelect, list[str]] | None,
    tuple[FileUpload, list[str]] | None,
]:
    if (
        not hasattr(source, "components")
        or not isinstance(submitted, dict)
        or set(submitted) != {"type", "components"}
        or submitted.get("type") != 1
        or not isinstance(submitted.get("components"), list)
        or len(submitted["components"]) != 1
    ):
        _invalid(
            "MODAL_SUBMISSION_INVALID",
            "This form no longer matches the form sent by the bot. Run it again.",
        )
    source_input = source.components[0]
    if not isinstance(
        source_input,
        (TextInput, StringSelect, UserSelect, RoleSelect, MentionableSelect, ChannelSelect),
    ):
        _invalid(
            "INTERACTION_MODAL_INVALID",
            "This form contains an unsupported field. Run the interaction again.",
            status_code=409,
        )
    normalized, entity_field, file_field = _submitted_modal_input(
        source_input,
        submitted["components"][0],
    )
    return {"type": 1, "components": [normalized]}, entity_field, file_field


def _submitted_modal_row(
    source: object,
    submitted: object,
) -> tuple[
    dict[str, object],
    tuple[EntitySelect, list[str]] | None,
    tuple[FileUpload, list[str]] | None,
]:
    if isinstance(source, Label):
        return _submitted_label_row(source, submitted)
    return _submitted_action_row(source, submitted)


def validate_modal_submission(
    source_payload: object,
    custom_id: str,
    submitted_rows: object,
) -> ValidatedModalSubmission:
    """Validate a modal submit against the exact stored type-9 callback schema."""

    modal = resolve_modal_definition(source_payload, custom_id)

    source_fields = [item for item in modal.components if not isinstance(item, TextDisplay)]
    if not isinstance(submitted_rows, list) or len(submitted_rows) != len(source_fields):
        _invalid(
            "MODAL_SUBMISSION_INVALID",
            "Every field from the original form must be submitted exactly once.",
        )
    normalized_rows: list[dict[str, object]] = []
    entity_fields: list[tuple[EntitySelect, list[str]]] = []
    file_fields: list[tuple[FileUpload, list[str]]] = []
    for source_row, submitted_row in zip(source_fields, submitted_rows, strict=True):
        normalized, entity_field, file_field = _submitted_modal_row(
            source_row,
            submitted_row,
        )
        normalized_rows.append(normalized)
        if entity_field is not None:
            entity_fields.append(entity_field)
        if file_field is not None:
            file_fields.append(file_field)
    return ValidatedModalSubmission(modal, normalized_rows, entity_fields, file_fields)


def resolve_modal_definition(source_payload: object, custom_id: str) -> Modal:
    """Authenticate the application-authored modal while its answers stay opaque."""

    try:
        modal = Modal.model_validate(source_payload)
    except ValidationError:
        _invalid(
            "INTERACTION_MODAL_INVALID",
            "This form is invalid or has expired. Run the interaction again.",
            status_code=409,
        )
    if modal.custom_id != custom_id:
        _invalid(
            "INTERACTION_MODAL_INVALID",
            "This form is not the form sent by the bot. Run the interaction again.",
            status_code=409,
        )
    return modal


async def _guild_user_projection(
    session: AsyncSession,
    access: ChannelAccess,
    user_ref: tuple[int, str],
) -> tuple[dict[str, object], dict[str, object]] | None:
    if access.guild is None:
        return None
    user = await session.get(User, user_ref)
    member = await session.get(
        GuildMember,
        (access.guild.id, access.guild.origin_domain, user_ref[0], user_ref[1]),
    )
    if user is None or member is None or user.disabled_at is not None:
        return None
    role_ids = list(
        await session.scalars(
            select(MemberRole.role_id).where(
                MemberRole.guild_id == access.guild.id,
                MemberRole.guild_domain == access.guild.origin_domain,
                MemberRole.user_id == user.id,
                MemberRole.user_domain == user.origin_domain,
            )
        )
    )
    return user_payload(user), member_payload(member, user, role_ids)


@dataclass(slots=True)
class EntityResolutionState:
    session: AsyncSession
    redis: Redis
    settings: Settings
    access: ChannelAccess
    actor: User
    users: dict[str, object] = field(default_factory=dict)
    members: dict[str, object] = field(default_factory=dict)
    roles: dict[str, object] = field(default_factory=dict)
    channels: dict[str, object] = field(default_factory=dict)
    source_permissions: int | None = None


def selected_entity_ref(raw_value: str, settings: Settings) -> tuple[int, str]:
    try:
        return EntityRef(raw_value).resolve(settings.domain)
    except (TypeError, ValueError):
        _invalid(
            "COMPONENT_VALUE_INVALID",
            "One selected item is no longer available here.",
        )


async def resolve_selected_user(
    state: EntityResolutionState,
    entity_ref: tuple[int, str],
    canonical: str,
) -> bool:
    if state.access.guild is None:
        participant = next(
            (
                user
                for user in state.access.participants
                if (user.id, user.origin_domain) == entity_ref and user.disabled_at is None
            ),
            None,
        )
        if participant is None:
            return False
        state.users[canonical] = user_payload(participant)
        return True
    projection = await _guild_user_projection(state.session, state.access, entity_ref)
    if projection is None:
        return False
    state.users[canonical], state.members[canonical] = projection
    return True


async def resolve_selected_role(
    state: EntityResolutionState,
    component: RoleSelect | MentionableSelect,
    entity_ref: tuple[int, str],
    canonical: str,
) -> None:
    guild = state.access.guild
    if guild is None:
        _invalid("COMPONENT_VALUE_INVALID", "Roles cannot be selected outside a guild.")
    role = await state.session.get(Role, entity_ref)
    if role is None or (role.guild_id, role.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        _invalid(
            "COMPONENT_VALUE_INVALID",
            "One selected role is no longer available in this guild.",
        )
    if isinstance(component, MentionableSelect) and not role.mentionable:
        if state.source_permissions is None:
            state.source_permissions = await get_permissions(
                state.session,
                state.redis,
                guild,
                state.actor,
                channel=state.access.channel,
            )
        if not state.source_permissions & Permission.MENTION_EVERYONE:
            _invalid(
                "COMPONENT_VALUE_INVALID",
                "You cannot select that role as a mentionable item.",
            )
    state.roles[canonical] = role_payload(role)


async def resolve_selected_channel(
    state: EntityResolutionState,
    component: ChannelSelect,
    entity_ref: tuple[int, str],
    canonical: str,
) -> None:
    guild = state.access.guild
    if guild is None:
        _invalid("COMPONENT_VALUE_INVALID", "Channels cannot be selected outside a guild.")
    channel = await state.session.get(Channel, entity_ref)
    if (
        channel is None
        or channel.unavailable
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        or (component.channel_types and channel.type not in component.channel_types)
    ):
        _invalid(
            "COMPONENT_VALUE_INVALID",
            "One selected channel is no longer available in this guild.",
        )
    permissions = await get_permissions(
        state.session,
        state.redis,
        guild,
        state.actor,
        channel=channel,
    )
    if not permissions & Permission.VIEW_CHANNEL:
        _invalid(
            "COMPONENT_VALUE_INVALID",
            "You cannot view one of the selected channels.",
        )
    state.channels[canonical] = channel_payload(channel) | {"permissions": str(permissions)}


async def resolve_selected_entity(
    state: EntityResolutionState,
    component: EntitySelect,
    raw_value: str,
) -> None:
    entity_ref = selected_entity_ref(raw_value, state.settings)
    canonical = f"{entity_ref[0]}@{entity_ref[1]}"
    if isinstance(component, (UserSelect, MentionableSelect)):
        if await resolve_selected_user(state, entity_ref, canonical):
            return
        if isinstance(component, UserSelect):
            _invalid(
                "COMPONENT_VALUE_INVALID",
                "One selected user is no longer available in this channel.",
            )
    if isinstance(component, (RoleSelect, MentionableSelect)):
        await resolve_selected_role(state, component, entity_ref, canonical)
        return
    if isinstance(component, ChannelSelect):
        await resolve_selected_channel(state, component, entity_ref, canonical)
        return
    _invalid("COMPONENT_VALUE_INVALID", "This control submitted an invalid item.")


async def resolve_component_entities(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    fields: list[tuple[EntitySelect, list[str]]],
) -> dict[str, object]:
    """Resolve selected IDs through channel/guild authority, never client projections."""

    state = EntityResolutionState(session, redis, settings, access, actor)
    for component, values in fields:
        for raw_value in values:
            await resolve_selected_entity(state, component, raw_value)
    return {
        **({"users": state.users} if state.users else {}),
        **({"members": state.members} if state.members else {}),
        **({"roles": state.roles} if state.roles else {}),
        **({"channels": state.channels} if state.channels else {}),
    }
