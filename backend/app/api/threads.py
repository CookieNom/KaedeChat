from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import (
    installation_for_channel,
    installation_for_guild,
    redact_bot_thread_payload,
    require_owned_attachments_for_installation,
    user_auth,
)
from app.api.channels import (
    MessageAdmissionOptions,
    create_message,
    publish_current_thread_member_updates,
    raise_proxy_rejection,
    slowmode_retry_after_ms,
)
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.auth.tokens import AccessGrant
from app.bots.auth import BotPrincipal, require_bot
from app.chat.audit import add_audit_entry
from app.chat.channel_access import (
    ChannelAccess,
    load_channel_access,
    lock_local_channel_mutation,
)
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import (
    federation_channel_state,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import (
    attachment_payload,
    channel_payload,
    render_message_payload,
    rich_thread_member_payload,
    thread_member_payload,
    thread_source_starter_payload,
    user_payload,
)
from app.chat.permissions import get_permissions, require_permissions
from app.chat.schemas import MessageCreate, RequestModel, cleaned_nonempty
from app.chat.thread_limits import MAX_ACTIVE_THREADS, require_active_thread_capacity
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, WireSnowflake
from app.db.models import (
    Attachment,
    Channel,
    Guild,
    GuildMember,
    Message,
    MessageProjection,
    ThreadMember,
    User,
)
from app.federation.client import signed_request
from app.federation.events import record_attachment_recipients, record_room_federation_recipient
from app.federation.network import FederationNetworkError, decode_federation_response_json
from app.federation.replication import profile_from_user, upsert_remote_user
from app.federation.schemas import RemoteUserProfile
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
)
from app.media.service import finalize_attachment
from app.tasks import media_process

router = APIRouter(prefix="/api/v1", tags=["threads and forums"])
bot_router = APIRouter(prefix="/api/v1/bots", tags=["bot threads and forums"])
federation_router = APIRouter(tags=["thread and forum federation"])

THREAD_TYPES = frozenset({10, 11, 12})
THREAD_PARENTS = frozenset({0, 5, 15})
PUBLIC_THREAD_TYPES = frozenset({10, 11})
AUTO_ARCHIVE_DURATIONS = frozenset({60, 1440, 4320, 10080})
THREAD_FLAG_PINNED = 1 << 1
FORUM_FLAG_REQUIRE_TAG = 1 << 4
MAX_THREAD_MEMBERS = 1000
MESSAGE_FLAG_HAS_THREAD = 1 << 5


class ThreadCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100)
    type: Literal[10, 11, 12] | None = None
    auto_archive_duration: int | None = None
    rate_limit_per_user: int | None = Field(default=None, ge=0, le=21_600)
    invitable: bool | None = None
    applied_tag_ids: list[WireSnowflake] = Field(
        default_factory=list,
        max_length=5,
        validation_alias=AliasChoices("applied_tag_ids", "applied_tags"),
    )
    message: MessageCreate | None = None

    # Flat compatibility keeps the web/mobile composer thin while the nested
    # form remains the canonical bot/slash-command contract.
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    e2ee: dict[str, object] | None = None
    client_nonce: str | None = Field(default=None, min_length=1, max_length=64)
    referenced_message_id: EntityRef | None = None
    mention_user_ids: list[EntityRef] = Field(default_factory=list, max_length=100)
    attachment_ids: list[WireSnowflake] = Field(default_factory=list, max_length=10)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)

    @field_validator("auto_archive_duration")
    @classmethod
    def valid_auto_archive(cls, value: int | None) -> int | None:
        if value is not None and value not in AUTO_ARCHIVE_DURATIONS:
            raise ValueError("unsupported auto-archive duration")
        return value

    @model_validator(mode="after")
    def one_starter_form(self) -> ThreadCreate:
        flat_fields = {
            "content",
            "e2ee",
            "client_nonce",
            "referenced_message_id",
            "mention_user_ids",
            "attachment_ids",
        }
        flat_supplied = bool(self.model_fields_set & flat_fields)
        if self.message is not None and flat_supplied:
            raise ValueError("use either message or flat starter fields, not both")
        if len(set(self.applied_tag_ids)) != len(self.applied_tag_ids):
            raise ValueError("applied tag IDs must be unique")
        return self

    def starter(self) -> MessageCreate | None:
        if self.message is not None:
            return self.message
        if not self.model_fields_set & {
            "content",
            "e2ee",
            "client_nonce",
            "referenced_message_id",
            "mention_user_ids",
            "attachment_ids",
        }:
            return None
        return MessageCreate(
            content=self.content,
            e2ee=self.e2ee,
            client_nonce=self.client_nonce,
            referenced_message_id=self.referenced_message_id,
            mention_user_ids=self.mention_user_ids,
            # WireSnowflake validates the JSON wire form and stores an int.
            # Rebuilding the nested compatibility object therefore has to
            # restore the canonical decimal-string input expected by the
            # MessageCreate validator.
            attachment_ids=[str(item) for item in self.attachment_ids],
        )


class ThreadFromMessageCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100)
    auto_archive_duration: int | None = None
    rate_limit_per_user: int | None = Field(default=None, ge=0, le=21_600)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)

    @field_validator("auto_archive_duration")
    @classmethod
    def valid_auto_archive(cls, value: int | None) -> int | None:
        if value is not None and value not in AUTO_ARCHIVE_DURATIONS:
            raise ValueError("unsupported auto-archive duration")
        return value


class ThreadUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    archived: bool | None = None
    locked: bool | None = None
    invitable: bool | None = None
    auto_archive_duration: int | None = None
    rate_limit_per_user: int | None = Field(default=None, ge=0, le=21_600)
    applied_tag_ids: list[WireSnowflake] | None = Field(
        default=None,
        max_length=5,
        validation_alias=AliasChoices("applied_tag_ids", "applied_tags"),
    )
    pinned: bool | None = None
    flags: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @field_validator("auto_archive_duration")
    @classmethod
    def valid_auto_archive(cls, value: int | None) -> int | None:
        if value is not None and value not in AUTO_ARCHIVE_DURATIONS:
            raise ValueError("unsupported auto-archive duration")
        return value

    @model_validator(mode="after")
    def valid_update(self) -> ThreadUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one thread field is required")
        if self.applied_tag_ids is not None and len(set(self.applied_tag_ids)) != len(
            self.applied_tag_ids
        ):
            raise ValueError("applied tag IDs must be unique")
        if self.flags is not None and self.flags & ~THREAD_FLAG_PINNED:
            raise ValueError("thread flags contain unsupported bits")
        if (
            self.flags is not None
            and self.pinned is not None
            and bool(self.flags & THREAD_FLAG_PINNED) != self.pinned
        ):
            raise ValueError("pinned and flags disagree")
        return self


class ThreadMemberUpdate(RequestModel):
    flags: int = Field(default=0, ge=0)
    notification_level: Literal["inherit", "all", "mentions", "none"] = "inherit"


class GuildThreadProxyRequest(RequestModel):
    """A signed remote member mutation evaluated by the guild authority."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "thread.create",
        "thread.create_from_message",
        "thread.update",
        "thread.delete",
        "thread.member.put",
        "thread.member.delete",
    ]
    actor: RemoteUserProfile
    channel_id: WireSnowflake
    payload: dict[str, Any] = Field(default_factory=dict, max_length=24)
    message_id: EntityRef | None = None
    target_user_id: EntityRef | None = None
    attachments: list[dict[str, object]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def operation_shape(self) -> GuildThreadProxyRequest:
        source_operation = self.operation == "thread.create_from_message"
        member_operation = self.operation in {"thread.member.put", "thread.member.delete"}
        if source_operation != (self.message_id is not None):
            raise ValueError("source thread mutation has an invalid message reference")
        if member_operation != (self.target_user_id is not None):
            raise ValueError("thread member mutation has an invalid target reference")
        if self.attachments and self.operation != "thread.create":
            raise ValueError("attachments are valid only for thread creation")
        if self.operation in {"thread.delete", "thread.member.delete"} and self.payload:
            raise ValueError("delete thread mutations do not accept a payload")
        if (
            self.operation
            in {
                "thread.create",
                "thread.create_from_message",
                "thread.update",
            }
            and not self.payload
        ):
            raise ValueError("thread mutation payload is required")
        return self


async def proxy_remote_thread_mutation(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    operation: str,
    *,
    payload: dict[str, Any] | None = None,
    message_ref: EntityRef | None = None,
    target_ref: EntityRef | None = None,
    attachments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Submit one mutation to the remote guild home and bound its response."""

    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("thread proxy requires a remote guild")
    body: dict[str, object] = {
        "operation": operation,
        "actor": profile_from_user(actor),
        "channel_id": str(access.channel.id),
        "payload": payload or {},
        "attachments": attachments or [],
    }
    if message_ref is not None:
        message_id, message_domain = message_ref.resolve(settings.domain)
        body["message_id"] = f"{message_id}@{message_domain}"
    if target_ref is not None:
        target_id, target_domain = target_ref.resolve(settings.domain)
        body["target_user_id"] = f"{target_id}@{target_domain}"
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/proxy-thread",
            payload=body,
        )
    except (httpx.HTTPError, FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"}
        ) from None
    raise_proxy_rejection(response, {400, 403, 404, 409, 410, 422, 429, 507})
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_WRITE_UNAVAILABLE"})
    try:
        decoded = decode_federation_response_json(response)
    except FederationNetworkError:
        decoded = None
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"})
    result = {str(key): value for key, value in decoded.items()}
    if operation in {"thread.create", "thread.create_from_message", "thread.update"}:
        rendered = result.get("thread")
        if not isinstance(rendered, dict):
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"}
            )
        expected_id = None if operation.startswith("thread.create") else str(access.channel.id)
        if (
            (expected_id is not None and rendered.get("id") != expected_id)
            or rendered.get("origin_domain") != guild.origin_domain
            or rendered.get("guild_id") != str(guild.id)
            or rendered.get("guild_domain") != guild.origin_domain
            or rendered.get("type") not in THREAD_TYPES
        ):
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"}
            )
        if operation.startswith("thread.create") and (
            rendered.get("parent_id") != str(access.channel.id)
            or rendered.get("parent_domain") != access.channel.origin_domain
            or rendered.get("owner_id") != str(actor.id)
            or rendered.get("owner_domain") != actor.origin_domain
        ):
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"}
            )
        if operation.startswith("thread.create") and payload is not None:
            requested_type = payload.get("type")
            expected_type = (
                (10 if access.channel.type == 5 else 11)
                if operation == "thread.create_from_message"
                else _thread_type(
                    access.channel,
                    int(requested_type) if requested_type is not None else None,
                )
            )
            if rendered.get("name") != payload.get("name") or rendered.get("type") != expected_type:
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
                )
        if operation == "thread.create_from_message" and message_ref is not None:
            source_id, source_domain = message_ref.resolve(settings.domain)
            if (rendered.get("id"), rendered.get("origin_domain")) != (
                str(source_id),
                source_domain,
            ):
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
                )
        if operation == "thread.create" and payload is not None:
            nested_starter = payload.get("message")
            starter_request = nested_starter if isinstance(nested_starter, dict) else payload
            attachment_ids = starter_request.get("attachment_ids", [])
            has_starter = bool(
                starter_request.get("content") is not None
                or starter_request.get("e2ee") is not None
                or isinstance(attachment_ids, list)
                and attachment_ids
            )
            if has_starter:
                starter = rendered.get("starter_message")
                if not isinstance(starter, dict):
                    raise HTTPException(
                        status_code=502,
                        detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
                    )
                raw_rendered_attachments = starter.get("attachments")
                rendered_attachment_refs = (
                    {
                        (item.get("id"), item.get("origin_domain"))
                        for item in raw_rendered_attachments
                        if isinstance(item, dict)
                    }
                    if isinstance(raw_rendered_attachments, list)
                    else set()
                )
                requested_attachment_refs = {
                    (str(item.get("id")), item.get("origin_domain")) for item in attachments or []
                }
                if (
                    starter.get("author_id") != str(actor.id)
                    or starter.get("author_domain") != actor.origin_domain
                    or starter.get("content") != starter_request.get("content")
                    or starter.get("e2ee") != starter_request.get("e2ee")
                    or starter.get("client_nonce") != starter_request.get("client_nonce")
                    or rendered_attachment_refs != requested_attachment_refs
                ):
                    raise HTTPException(
                        status_code=502,
                        detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
                    )
        if operation == "thread.update" and payload is not None:
            for field in (
                "name",
                "archived",
                "locked",
                "invitable",
                "auto_archive_duration",
                "rate_limit_per_user",
            ):
                if field in payload and rendered.get(field) != payload[field]:
                    raise HTTPException(
                        status_code=502,
                        detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
                    )
            requested_tags = payload.get("applied_tag_ids")
            if requested_tags is not None and rendered.get("applied_tag_ids") != [
                str(item) for item in cast(list[object], requested_tags)
            ]:
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
                )
            requested_pinned = payload.get("pinned")
            if requested_pinned is None and "flags" in payload:
                requested_pinned = bool(int(payload["flags"]) & THREAD_FLAG_PINNED)
            if requested_pinned is not None and bool(
                int(cast(int | str, rendered.get("flags", 0))) & THREAD_FLAG_PINNED
            ) != bool(requested_pinned):
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
                )
        return {str(key): value for key, value in rendered.items()}
    expected_flag = "deleted" if operation == "thread.delete" else "updated"
    if result.get(expected_flag) is not True:
        raise HTTPException(status_code=502, detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"})
    return result


async def prepare_remote_starter_attachments(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    starter: MessageCreate | None,
) -> tuple[list[Attachment], list[dict[str, object]]]:
    if starter is None or not starter.attachment_ids:
        return [], []
    guild = access.guild
    if guild is None or guild.origin_domain == settings.domain:
        raise RuntimeError("remote starter attachment preparation requires a remote guild")
    attachments: list[Attachment] = []
    for attachment_id in starter.attachment_ids:
        attachment = await finalize_attachment(
            session,
            settings,
            actor,
            int(attachment_id),
            required_purpose="attachment",
        )
        if attachment.message_id is not None or attachment.message_domain is not None:
            raise HTTPException(status_code=409, detail={"code": "ATTACHMENT_ALREADY_USED"})
        attachments.append(attachment)
    await record_attachment_recipients(
        session,
        {(item.id, item.origin_domain) for item in attachments},
        guild.origin_domain,
        room_ref=("guild", guild.id, guild.origin_domain),
    )
    # The authority may commit and disclose these references before the HTTP
    # response reaches us. Persist their terminal-deletion recipients first.
    await session.commit()
    return attachments, [attachment_payload(item) for item in attachments]


def _encode_thread_cursor(
    thread: Channel,
    timestamp: datetime,
    *,
    archived: bool,
    include_archived: bool = False,
    sort_order: int,
) -> str:
    raw = json.dumps(
        {
            "v": 1,
            "p": 1 if thread.flags & THREAD_FLAG_PINNED else 0,
            "t": timestamp.isoformat(),
            "i": str(thread.id),
            "d": thread.origin_domain,
            "a": "all" if include_archived else archived,
            "s": sort_order,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_thread_cursor(
    value: str,
    *,
    archived: bool,
    include_archived: bool = False,
    sort_order: int,
) -> tuple[bool, datetime, int, str]:
    try:
        padding = "=" * (-len(value) % 4)
        raw = json.loads(base64.urlsafe_b64decode(value + padding))
        if (
            not isinstance(raw, dict)
            or raw.get("v") != 1
            or raw.get("p") not in {0, 1}
            or raw.get("a") != ("all" if include_archived else archived)
            or raw.get("s") != sort_order
            or not isinstance(raw.get("i"), str)
            or not raw["i"].isdigit()
            or not isinstance(raw.get("d"), str)
            or not 1 <= len(raw["d"]) <= 253
        ):
            raise ValueError
        timestamp = datetime.fromisoformat(str(raw.get("t")))
        if timestamp.tzinfo is None:
            raise ValueError
        thread_id = int(raw["i"])
        if not 0 < thread_id < 1 << 63:
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail={"code": "INVALID_THREAD_CURSOR"}) from None
    return bool(raw["p"]), timestamp, thread_id, raw["d"]


def _available_tag_ids(parent: Channel) -> set[int]:
    values: set[int] = set()
    for raw in cast(list[object], getattr(parent, "available_tags", []) or []):
        if isinstance(raw, dict) and str(raw.get("id", "")).isdigit():
            values.add(int(str(raw["id"])))
    return values


def _moderated_tag_ids(parent: Channel) -> set[int]:
    return {
        int(str(raw["id"]))
        for raw in cast(list[object], getattr(parent, "available_tags", []) or [])
        if isinstance(raw, dict) and str(raw.get("id", "")).isdigit() and bool(raw.get("moderated"))
    }


def validate_applied_tags(
    parent: Channel,
    applied: list[int],
    *,
    require_tag: bool = True,
) -> None:
    if parent.type != 15:
        if applied:
            raise HTTPException(status_code=400, detail={"code": "THREAD_TAGS_FORUM_ONLY"})
        return
    if not set(applied) <= _available_tag_ids(parent):
        raise HTTPException(status_code=400, detail={"code": "FORUM_TAG_INVALID"})
    if require_tag and parent.flags & FORUM_FLAG_REQUIRE_TAG and not applied:
        raise HTTPException(status_code=400, detail={"code": "FORUM_TAG_REQUIRED"})


async def thread_access(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    thread_ref: EntityRef,
    *,
    needed: Permission = Permission.VIEW_CHANNEL,
    lock: bool = False,
) -> tuple[ChannelAccess, int]:
    access = await load_channel_access(session, settings, actor, thread_ref)
    if access.guild is None or access.channel.type not in THREAD_TYPES:
        raise HTTPException(status_code=404, detail={"code": "THREAD_NOT_FOUND"})
    if lock:
        if access.guild.origin_domain != settings.domain:
            raise HTTPException(status_code=409, detail={"code": "THREAD_AUTHORITY_REMOTE"})
        access = await lock_local_channel_mutation(session, settings, access)
    guild = access.guild
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "THREAD_NOT_FOUND"})
    permissions = await require_permissions(
        session,
        redis,
        guild,
        actor,
        needed,
        channel=access.channel,
    )
    return access, permissions


async def rendered_thread(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    thread: Channel,
) -> dict[str, object]:
    rendered = channel_payload(thread)
    permissions = await get_permissions(session, redis, guild, actor, channel=thread)
    rendered["permissions"] = str(int(permissions))
    current_member = await session.get(
        ThreadMember,
        (thread.id, thread.origin_domain, actor.id, actor.origin_domain),
    )
    rendered["member"] = (
        thread_member_payload(current_member) if current_member is not None else None
    )
    starter: Message | None = None
    if (
        permissions & Permission.READ_MESSAGE_HISTORY
        and thread.starter_message_id is not None
        and thread.starter_message_domain is not None
    ):
        starter = await session.get(
            Message,
            (thread.starter_message_id, thread.starter_message_domain),
        )
    if starter is not None:
        starter_payload = await render_message_payload(session, starter)
        if (starter.channel_id, starter.channel_domain) == (
            thread.parent_id,
            thread.parent_domain,
        ):
            starter_payload = thread_source_starter_payload(thread, starter_payload)
        rendered["starter_message"] = starter_payload
    elif thread.starter_message_id is not None:
        parent = await session.get(Channel, (thread.parent_id, thread.parent_domain))
        started_from_parent_message = (
            thread.starter_message_id == thread.id
            and thread.starter_message_domain == thread.origin_domain
            and parent is not None
            and parent.type in {0, 5}
        )
        owner = (
            await session.get(User, (thread.owner_id, thread.owner_domain))
            if thread.owner_id is not None and thread.owner_domain is not None
            else None
        )
        unavailable_source: dict[str, object] = {
            "id": str(thread.starter_message_id),
            "origin_domain": thread.starter_message_domain,
            "channel_id": str(thread.parent_id if started_from_parent_message else thread.id),
            "channel_domain": (
                thread.parent_domain if started_from_parent_message else thread.origin_domain
            ),
            "author_id": str(thread.owner_id),
            "author_domain": thread.owner_domain,
            "author": user_payload(owner) if owner is not None else None,
            "content": None,
            "e2ee": None,
            "encryption_policy_generation": str(thread.encryption_policy_generation),
            "encryption_epoch": None,
            "message_type": 0,
            "flags": MESSAGE_FLAG_HAS_THREAD if started_from_parent_message else 0,
            "client_nonce": None,
            "referenced_message_id": None,
            "referenced_message_domain": None,
            "message_reference": None,
            "mention_user_refs": [],
            "attachments": [],
            "webhook_id": None,
            "webhook": None,
            "edited_at": None,
            "deleted_at": None,
            "created_at": thread.created_at.isoformat(),
            "content_unavailable": True,
            "attachments_unavailable": True,
        }
        rendered["starter_message"] = (
            thread_source_starter_payload(thread, unavailable_source)
            if started_from_parent_message
            else unavailable_source
        )
    else:
        rendered["starter_message"] = None
    return rendered


def _thread_type(parent: Channel, requested: int | None) -> int:
    if parent.type == 15:
        if requested not in {None, 11}:
            raise HTTPException(status_code=400, detail={"code": "FORUM_PUBLIC_THREADS_ONLY"})
        return 11
    if parent.type == 5:
        if requested not in {None, 10}:
            raise HTTPException(
                status_code=400, detail={"code": "ANNOUNCEMENT_THREAD_TYPE_REQUIRED"}
            )
        return 10
    if parent.type == 0:
        if requested not in {None, 11, 12}:
            raise HTTPException(status_code=400, detail={"code": "TEXT_THREAD_TYPE_INVALID"})
        # Discord's generic Start Thread without Message endpoint defaults an
        # omitted type to PRIVATE_THREAD. First-party public composers send 11.
        return requested or 12
    raise HTTPException(status_code=400, detail={"code": "THREAD_PARENT_INVALID"})


def _thread_create_permission(thread_type: int) -> Permission:
    return (
        Permission.CREATE_PRIVATE_THREADS if thread_type == 12 else Permission.CREATE_PUBLIC_THREADS
    )


async def enforce_thread_create_slowmode(
    redis: Redis, parent: Channel, actor: User, permissions: int
) -> None:
    if (
        not parent.rate_limit_per_user
        or actor.account_type == "bot"
        or permissions & Permission.BYPASS_SLOWMODE
    ):
        return
    slowmode_key = (
        f"thread-create:{parent.origin_domain}:{parent.id}:{actor.origin_domain}:{actor.id}"
    )
    allowed = await redis.set(slowmode_key, "1", ex=parent.rate_limit_per_user, nx=True)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "SLOWMODE_RATE_LIMITED",
                "retry_after_ms": await slowmode_retry_after_ms(redis, slowmode_key),
            },
        )


async def create_parent_thread_notice(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    parent: Channel,
    thread: Channel,
    actor: User,
    *,
    created_at: datetime,
) -> dict[str, object]:
    """Persist Discord's type-18 parent notice using the thread snowflake."""

    notice = Message(
        id=thread.id,
        origin_domain=thread.origin_domain,
        channel_id=parent.id,
        channel_domain=parent.origin_domain,
        author_id=actor.id,
        author_domain=actor.origin_domain,
        content=thread.name,
        encryption_policy_generation=parent.encryption_policy_generation,
        encryption_epoch=parent.encryption_epoch,
        message_type=18,
        flags=MESSAGE_FLAG_HAS_THREAD,
        mention_user_refs=[],
        created_at=created_at,
    )
    session.add(notice)
    session.add(
        MessageProjection(
            message_id=notice.id,
            message_domain=notice.origin_domain,
            channel_id=parent.id,
            channel_domain=parent.origin_domain,
            mention_user_refs=[],
        )
    )
    parent.last_message_id = notice.id
    parent.last_message_domain = notice.origin_domain
    await session.flush()
    rendered = await render_message_payload(session, notice, actor)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.message.create",
        {"message": rendered, "author": profile_from_user(actor)},
        channel=parent,
    )
    return rendered


async def create_thread_service(
    parent_ref: EntityRef,
    payload: ThreadCreate,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    *,
    replicated_attachments: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    access = await load_channel_access(session, settings, auth.user, parent_ref)
    if access.guild is None or access.channel.type not in THREAD_PARENTS:
        raise HTTPException(status_code=400, detail={"code": "THREAD_PARENT_INVALID"})
    if access.guild.origin_domain != settings.domain:
        starter_payload = payload.starter()
        if starter_payload is not None and starter_payload.client_nonce is None:
            raise HTTPException(
                status_code=400, detail={"code": "CLIENT_NONCE_REQUIRED_FOR_FEDERATION"}
            )
        if auth.user.account_type == "bot" and (
            access.channel.e2ee_required or access.channel.encryption_mode == "e2ee"
        ):
            raise HTTPException(status_code=409, detail={"code": "BOT_THREAD_E2EE_UNSUPPORTED"})
        local_attachments, remote_attachments = await prepare_remote_starter_attachments(
            session, settings, access, auth.user, starter_payload
        )
        try:
            return await proxy_remote_thread_mutation(
                session,
                settings,
                access,
                auth.user,
                "thread.create",
                payload=payload.model_dump(mode="json", exclude_none=True),
                attachments=remote_attachments,
            )
        finally:
            for attachment in local_attachments:
                await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
    access = await lock_local_channel_mutation(session, settings, access)
    parent = access.channel
    guild = cast(Guild, access.guild)
    if auth.user.account_type == "bot" and (
        parent.e2ee_required or parent.encryption_mode == "e2ee"
    ):
        raise HTTPException(status_code=409, detail={"code": "BOT_THREAD_E2EE_UNSUPPORTED"})
    thread_type = _thread_type(parent, payload.type)
    starter_payload = payload.starter()
    if parent.type == 15 and starter_payload is None:
        raise HTTPException(status_code=400, detail={"code": "FORUM_STARTER_REQUIRED"})
    if (
        parent.type == 15
        and starter_payload is not None
        and starter_payload.content is not None
        and len(starter_payload.content) > 2000
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "FORUM_STARTER_CONTENT_TOO_LONG", "max_length": 2000},
        )
    if parent.type == 15 and parent.e2ee_required and not settings.e2ee_activation_enabled:
        raise HTTPException(status_code=403, detail={"code": "E2EE_ACTIVATION_DISABLED"})
    if parent.encryption_mode == "e2ee" and starter_payload is not None:
        raise HTTPException(
            status_code=409, detail={"code": "THREAD_E2EE_CHILD_ACTIVATION_REQUIRED"}
        )
    if parent.type == 15:
        needed = Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES
    else:
        needed = Permission.VIEW_CHANNEL | _thread_create_permission(thread_type)
        if starter_payload is not None:
            needed |= Permission.SEND_MESSAGES_IN_THREADS
    actor_permissions = await require_permissions(
        session, redis, guild, auth.user, needed, channel=parent
    )
    applied_tags = [int(item) for item in payload.applied_tag_ids]
    validate_applied_tags(parent, applied_tags)
    if set(applied_tags) & _moderated_tag_ids(parent) and not (
        actor_permissions & Permission.MANAGE_THREADS
    ):
        raise HTTPException(status_code=403, detail={"code": "MODERATED_TAG_FORBIDDEN"})
    if thread_type != 12 and payload.invitable is not None:
        raise HTTPException(status_code=400, detail={"code": "THREAD_INVITABLE_PRIVATE_ONLY"})

    # A thread-create retry must resolve before it consumes another slowmode
    # admission or snowflake. The guild mutation fence already serializes local
    # channel writes; this narrower advisory fence also documents and preserves
    # the nonce scope if that coarse lock is ever relaxed.
    starter_nonce = starter_payload.client_nonce if starter_payload is not None else None
    if starter_nonce is not None:
        nonce_lock = int.from_bytes(
            hashlib.blake2b(
                (
                    f"thread-create:{parent.id}@{parent.origin_domain}:"
                    f"{auth.user.id}@{auth.user.origin_domain}:{starter_nonce}"
                ).encode(),
                digest_size=8,
            ).digest(),
            byteorder="big",
            signed=True,
        )
        await session.execute(select(func.pg_advisory_xact_lock(nonce_lock)))
        existing_thread = await session.scalar(
            select(Channel)
            .join(
                Message,
                (Message.id == Channel.starter_message_id)
                & (Message.origin_domain == Channel.starter_message_domain)
                & (Message.channel_id == Channel.id)
                & (Message.channel_domain == Channel.origin_domain),
            )
            .where(
                Channel.parent_id == parent.id,
                Channel.parent_domain == parent.origin_domain,
                Channel.owner_id == auth.user.id,
                Channel.owner_domain == auth.user.origin_domain,
                Channel.type.in_(THREAD_TYPES),
                Channel.unavailable.is_(False),
                Message.author_id == auth.user.id,
                Message.author_domain == auth.user.origin_domain,
                Message.client_nonce == starter_nonce,
            )
        )
        if existing_thread is not None:
            existing_result = await rendered_thread(
                session, redis, guild, auth.user, existing_thread
            )
            if parent.type == 15:
                existing_result["message"] = existing_result.get("starter_message")
            return existing_result

    await require_active_thread_capacity(session, guild)
    await enforce_thread_create_slowmode(redis, parent, auth.user, actor_permissions)

    now = datetime.now(UTC)
    thread_id = await snowflake.mint()
    auto_archive = payload.auto_archive_duration or parent.default_auto_archive_duration or 1440
    slowmode = (
        payload.rate_limit_per_user
        if payload.rate_limit_per_user is not None
        else int(parent.default_thread_rate_limit_per_user or 0)
    )
    thread = Channel(
        id=thread_id,
        origin_domain=settings.domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=thread_type,
        name=payload.name,
        topic=None,
        position=0,
        parent_id=parent.id,
        parent_domain=parent.origin_domain,
        permissions_synced=False,
        rate_limit_per_user=slowmode,
        owner_id=auth.user.id,
        owner_domain=auth.user.origin_domain,
        archived=False,
        locked=False,
        invitable=(payload.invitable if payload.invitable is not None else True)
        if thread_type == 12
        else None,
        auto_archive_duration=auto_archive,
        archive_timestamp=now,
        last_activity_at=now,
        message_count=0,
        total_message_sent=0,
        member_count=1,
        flags=0,
        applied_tag_ids=[str(item) for item in applied_tags],
        e2ee_required=bool(
            (parent.type == 15 and parent.e2ee_required) or parent.encryption_mode == "e2ee"
        ),
        created_floor_id=thread_id,
        created_at=now,
    )
    session.add(thread)
    creator_membership = ThreadMember(
        thread_id=thread.id,
        thread_domain=thread.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        flags=0,
        notification_level="inherit",
    )
    session.add(creator_membership)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        110,
        target_type="thread",
        target_ref={"id": str(thread.id)},
    )
    await session.flush()
    initial_thread_state = federation_channel_state(thread)
    if starter_payload is not None:
        initial_thread_state.update(
            {
                "starter_message_id": None,
                "starter_message_domain": None,
                "message_count": 0,
                "total_message_sent": 0,
            }
        )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.channel.create",
        {"channel": initial_thread_state},
        channel=thread,
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.thread.member.upsert",
        {
            "member": thread_member_payload(creator_membership),
            "member_count": thread.member_count,
        },
        channel=thread,
        snapshot_required=thread.type == 12,
    )
    if parent.type == 15:
        parent.last_thread_id = thread.id
        parent.last_thread_domain = thread.origin_domain
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.forum.cursor.update",
            {
                "forum": {"id": str(parent.id), "origin_domain": parent.origin_domain},
                "last_thread_id": str(parent.last_thread_id),
                "last_thread_domain": parent.last_thread_domain,
            },
            channel=parent,
        )
    parent_notice: dict[str, object] | None = None
    if (
        parent.type in {0, 5}
        and thread.type in PUBLIC_THREAD_TYPES
        and parent.encryption_mode == "plaintext"
    ):
        parent_notice = await create_parent_thread_notice(
            session,
            settings,
            guild,
            parent,
            thread,
            auth.user,
            created_at=now,
        )
    starter: dict[str, object] | None = None
    if starter_payload is not None:
        starter = await create_message(
            EntityRef(f"{thread.id}@{thread.origin_domain}"),
            starter_payload,
            Response(),
            auth,
            session,
            redis,
            snowflake,
            settings,
            MessageAdmissionOptions(
                allow_required_e2ee_starter=parent.type == 15,
                mark_thread_starter=True,
                queue_thread_create=False,
                defer_dispatch=True,
                forum_starter_permissions_checked=parent.type == 15,
                forced_message_id=thread.id if parent.type == 15 else None,
                replicated_attachments=replicated_attachments,
            ),
        )
        await session.refresh(thread)
    else:
        await session.commit()
    await wake_queued_guild_federation(guild)
    result = await rendered_thread(session, redis, guild, auth.user, thread)
    result["starter_message"] = starter
    if parent.type == 15:
        result["message"] = starter
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "THREAD_CREATE",
        channel_payload(thread) | {"newly_created": True},
    )
    creator_member = await session.get(
        ThreadMember,
        (thread.id, thread.origin_domain, auth.user.id, auth.user.origin_domain),
    )
    if creator_member is not None:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "THREAD_MEMBER_UPDATE",
            thread_member_payload(creator_member),
            audience_user_refs=[f"{auth.user.id}@{auth.user.origin_domain}"],
        )
    if parent_notice is not None:
        parent_notice["thread"] = channel_payload(thread)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "MESSAGE_CREATE",
            parent_notice,
        )
    if starter is not None:
        await publish_dispatch(
            redis, guild_topic(guild.origin_domain, guild.id), "MESSAGE_CREATE", starter
        )
    return result


@router.post("/channels/{parent_ref}/threads", status_code=status.HTTP_201_CREATED)
async def create_thread(
    parent_ref: EntityRef,
    payload: ThreadCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await create_thread_service(
        parent_ref, payload, auth, session, redis, snowflake, settings
    )


@router.post(
    "/channels/{parent_ref}/messages/{message_ref}/threads",
    status_code=status.HTTP_201_CREATED,
)
async def create_thread_from_message(
    parent_ref: EntityRef,
    message_ref: EntityRef,
    payload: ThreadFromMessageCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    access = await load_channel_access(session, settings, auth.user, parent_ref)
    if access.guild is None or access.channel.type not in {0, 5}:
        raise HTTPException(status_code=400, detail={"code": "THREAD_PARENT_INVALID"})
    if access.guild.origin_domain != settings.domain:
        if auth.user.account_type == "bot" and (
            access.channel.e2ee_required or access.channel.encryption_mode == "e2ee"
        ):
            raise HTTPException(status_code=409, detail={"code": "BOT_THREAD_E2EE_UNSUPPORTED"})
        return await proxy_remote_thread_mutation(
            session,
            settings,
            access,
            auth.user,
            "thread.create_from_message",
            payload=payload.model_dump(mode="json", exclude_none=True),
            message_ref=message_ref,
        )
    access = await lock_local_channel_mutation(session, settings, access)
    parent = access.channel
    guild = cast(Guild, access.guild)
    if parent.encryption_mode == "e2ee":
        raise HTTPException(
            status_code=409, detail={"code": "THREAD_E2EE_CHILD_ACTIVATION_REQUIRED"}
        )
    source_id, source_domain = message_ref.resolve(settings.domain)
    source = await session.scalar(
        select(Message)
        .where(
            Message.id == source_id,
            Message.origin_domain == source_domain,
            Message.channel_id == parent.id,
            Message.channel_domain == parent.origin_domain,
            Message.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if source is None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    existing = await session.scalar(
        select(Channel.id).where(
            Channel.starter_message_id == source.id,
            Channel.starter_message_domain == source.origin_domain,
            Channel.type.in_(THREAD_TYPES),
            Channel.unavailable.is_(False),
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail={"code": "MESSAGE_ALREADY_HAS_THREAD"})
    thread_type = 10 if parent.type == 5 else 11
    actor_permissions = await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        Permission.VIEW_CHANNEL | _thread_create_permission(thread_type),
        channel=parent,
    )
    await require_active_thread_capacity(session, guild)
    await enforce_thread_create_slowmode(redis, parent, auth.user, actor_permissions)
    now = datetime.now(UTC)
    # Discord binds a thread started from an existing message to that
    # message's snowflake. Guild messages are authoritative at the guild home,
    # so the channel identity has the same authoritative domain too.
    thread_id = source.id
    if source.origin_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "THREAD_AUTHORITY_REMOTE"})
    conflicting_channel = await session.get(Channel, (thread_id, settings.domain))
    if conflicting_channel is not None:
        raise HTTPException(status_code=409, detail={"code": "THREAD_ID_CONFLICT"})
    thread = Channel(
        id=thread_id,
        origin_domain=settings.domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=thread_type,
        name=payload.name,
        position=0,
        parent_id=parent.id,
        parent_domain=parent.origin_domain,
        permissions_synced=False,
        rate_limit_per_user=(
            payload.rate_limit_per_user
            if payload.rate_limit_per_user is not None
            else int(parent.default_thread_rate_limit_per_user or 0)
        ),
        owner_id=auth.user.id,
        owner_domain=auth.user.origin_domain,
        archived=False,
        locked=False,
        invitable=None,
        auto_archive_duration=payload.auto_archive_duration
        or parent.default_auto_archive_duration
        or 1440,
        archive_timestamp=now,
        last_activity_at=now,
        starter_message_id=source.id,
        starter_message_domain=source.origin_domain,
        # Parent sources are projected as type 21; the FK-backed child cursor
        # stays empty until the first message physically stored in the child.
        last_message_id=None,
        last_message_domain=None,
        message_count=0,
        total_message_sent=0,
        member_count=1,
        flags=0,
        applied_tag_ids=[],
        e2ee_required=False,
        created_floor_id=thread_id,
        created_at=now,
    )
    session.add(thread)
    creator_membership = ThreadMember(
        thread_id=thread.id,
        thread_domain=thread.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
        flags=0,
        notification_level="inherit",
    )
    session.add(creator_membership)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        110,
        target_type="thread",
        target_ref={"id": str(thread.id)},
    )
    source.flags |= MESSAGE_FLAG_HAS_THREAD
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.channel.create",
        {"channel": federation_channel_state(thread)},
        channel=thread,
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.thread.member.upsert",
        {
            "member": thread_member_payload(creator_membership),
            "member_count": thread.member_count,
        },
        channel=thread,
        snapshot_required=False,
    )
    rendered_source = await render_message_payload(session, source, auth.user)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.message.update",
        {"message": rendered_source, "thread_attached": True},
        channel=parent,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    result = await rendered_thread(session, redis, guild, auth.user, thread)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "THREAD_CREATE",
        channel_payload(thread) | {"newly_created": True},
    )
    creator_member = await session.get(
        ThreadMember,
        (thread.id, thread.origin_domain, auth.user.id, auth.user.origin_domain),
    )
    if creator_member is not None:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "THREAD_MEMBER_UPDATE",
            thread_member_payload(creator_member),
            audience_user_refs=[f"{auth.user.id}@{auth.user.origin_domain}"],
        )
    # Attaching a thread is a flags/reference transition, not fresh history.
    # Keep the retained federation mutation complete for replica convergence,
    # but do not replay an old message's content to live viewers who may lack
    # READ_MESSAGE_HISTORY.
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "MESSAGE_UPDATE",
        {
            "id": str(source.id),
            "origin_domain": source.origin_domain,
            "channel_id": str(source.channel_id),
            "channel_domain": source.channel_domain,
            "flags": source.flags,
            "thread": channel_payload(thread),
        },
    )
    return result


@router.get("/channels/{thread_ref}")
async def get_thread(
    thread_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    access, _ = await thread_access(session, redis, settings, auth.user, thread_ref)
    return await rendered_thread(
        session, redis, cast(Guild, access.guild), auth.user, access.channel
    )


async def list_parent_threads_service(
    parent_ref: EntityRef,
    actor: User,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    archived: bool,
    include_archived: bool,
    before: datetime | None,
    cursor: str | None,
    limit: int,
    tag_ids: list[int],
    sort_order: int | None,
    query: str | None,
) -> dict[str, object]:
    access = await load_channel_access(session, settings, actor, parent_ref)
    if access.guild is None or access.channel.type not in THREAD_PARENTS:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    guild = access.guild
    parent = access.channel
    if parent.type != 15 and (
        include_archived or tag_ids or query is not None or sort_order is not None
    ):
        raise HTTPException(status_code=400, detail={"code": "FORUM_FILTERS_FORUM_ONLY"})
    listing_permission = Permission.VIEW_CHANNEL
    if archived or include_archived:
        listing_permission |= Permission.READ_MESSAGE_HISTORY
    parent_permissions = await require_permissions(
        session, redis, guild, actor, listing_permission, channel=parent
    )
    effective_sort = sort_order if sort_order is not None else parent.default_sort_order
    effective_sort = int(effective_sort or 0)
    statement = select(Channel).where(
        Channel.parent_id == parent.id,
        Channel.parent_domain == parent.origin_domain,
        Channel.type.in_(THREAD_TYPES),
        Channel.unavailable.is_(False),
    )
    if not include_archived:
        statement = statement.where(Channel.archived.is_(archived))
    elif parent.type != 15:
        raise HTTPException(status_code=400, detail={"code": "FORUM_FEED_FORUM_ONLY"})
    if not parent_permissions & Permission.MANAGE_THREADS:
        statement = statement.where(
            or_(
                Channel.type != 12,
                exists().where(
                    ThreadMember.thread_id == Channel.id,
                    ThreadMember.thread_domain == Channel.origin_domain,
                    ThreadMember.user_id == actor.id,
                    ThreadMember.user_domain == actor.origin_domain,
                ),
            )
        )
    if tag_ids:
        statement = statement.where(
            or_(*(Channel.applied_tag_ids.contains([str(tag_id)]) for tag_id in tag_ids))
        )
    if query is not None:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(Channel.name.ilike(f"%{escaped}%", escape="\\"))
    cursor_column = (
        Channel.created_at
        if effective_sort == 1
        else Channel.last_activity_at
        if parent.type == 15 or not archived
        else Channel.archive_timestamp
    )
    pinned = Channel.flags.op("&")(THREAD_FLAG_PINNED) != 0
    if before is not None and cursor is not None:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PAGINATION"})
    if cursor is not None:
        cursor_pinned, cursor_at, cursor_id, cursor_domain = _decode_thread_cursor(
            cursor,
            archived=archived,
            include_archived=include_archived,
            sort_order=effective_sort,
        )
        lower_key = or_(
            cursor_column < cursor_at,
            and_(
                cursor_column == cursor_at,
                or_(
                    Channel.origin_domain < cursor_domain,
                    and_(
                        Channel.origin_domain == cursor_domain,
                        Channel.id < cursor_id,
                    ),
                ),
            ),
        )
        statement = statement.where(
            or_(and_(pinned, lower_key), ~pinned) if cursor_pinned else and_(~pinned, lower_key)
        )
    elif before is not None:
        # Legacy timestamp-only cursors remain accepted for older clients. New
        # clients use the exact composite cursor returned below.
        statement = statement.where(cursor_column < before)
    statement = statement.order_by(
        pinned.desc(),
        cursor_column.desc(),
        Channel.origin_domain.desc(),
        Channel.id.desc(),
    ).limit(limit + 1)
    rows = list(await session.scalars(statement))
    has_more = len(rows) > limit
    visible = rows[:limit]
    memberships = list(
        await session.scalars(
            select(ThreadMember).where(
                ThreadMember.thread_id.in_([item.id for item in visible] or [-1]),
                ThreadMember.thread_domain == parent.origin_domain,
                ThreadMember.user_id == actor.id,
                ThreadMember.user_domain == actor.origin_domain,
            )
        )
    )
    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        next_cursor_at: datetime | None = (
            last.created_at
            if effective_sort == 1
            else last.last_activity_at
            if parent.type == 15 or not archived
            else last.archive_timestamp
        )
        if next_cursor_at is not None:
            next_cursor = _encode_thread_cursor(
                last,
                next_cursor_at,
                archived=archived,
                include_archived=include_archived,
                sort_order=effective_sort,
            )
    return {
        "threads": [
            await rendered_thread(session, redis, guild, actor, thread) for thread in visible
        ],
        "members": [thread_member_payload(member) for member in memberships],
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


@router.get("/channels/{parent_ref}/threads")
async def list_parent_threads(
    parent_ref: EntityRef,
    archived: bool = False,
    include_archived: bool = False,
    before: datetime | None = None,
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    tag_id: list[WireSnowflake] | None = Query(default=None, max_length=20),
    sort_order: Literal[0, 1] | None = None,
    query: str | None = Query(default=None, min_length=1, max_length=100),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if archived and include_archived:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PAGINATION"})
    return await list_parent_threads_service(
        parent_ref,
        auth.user,
        session,
        redis,
        settings,
        archived=archived,
        include_archived=include_archived,
        before=before,
        cursor=cursor,
        limit=limit,
        tag_ids=list(dict.fromkeys(int(item) for item in tag_id or [])),
        sort_order=sort_order,
        query=query,
    )


@router.get("/guilds/{guild_ref}/threads/active")
async def list_active_guild_threads(
    guild_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    member = await session.get(
        GuildMember,
        (guild_id, guild_domain, auth.user.id, auth.user.origin_domain),
    )
    if guild is None or member is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return await active_thread_sync_payload(session, redis, guild, auth.user)


async def active_thread_sync_payload(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    *,
    parent_ids: set[int] | None = None,
) -> dict[str, object]:
    """Build the personalized Discord THREAD_LIST_SYNC/active-list shape."""

    statement = select(Channel).where(
        Channel.guild_id == guild.id,
        Channel.guild_domain == guild.origin_domain,
        Channel.type.in_(THREAD_TYPES),
        Channel.unavailable.is_(False),
        Channel.archived.is_(False),
    )
    if parent_ids is not None:
        statement = statement.where(Channel.parent_id.in_(parent_ids or {-1}))
    candidates = list(
        await session.scalars(
            statement.order_by(Channel.last_activity_at.desc(), Channel.id.desc()).limit(
                MAX_ACTIVE_THREADS + 1
            )
        )
    )
    if len(candidates) > MAX_ACTIVE_THREADS:
        raise HTTPException(status_code=409, detail={"code": "ACTIVE_THREAD_LIMIT"})
    visible = [
        item
        for item in candidates
        if await get_permissions(session, redis, guild, actor, channel=item)
        & Permission.VIEW_CHANNEL
    ]
    memberships = list(
        await session.scalars(
            select(ThreadMember).where(
                ThreadMember.thread_id.in_([item.id for item in visible] or [-1]),
                ThreadMember.thread_domain == guild.origin_domain,
                ThreadMember.user_id == actor.id,
                ThreadMember.user_domain == actor.origin_domain,
            )
        )
    )
    payload: dict[str, object] = {
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
        "threads": [
            await rendered_thread(session, redis, guild, actor, thread) for thread in visible
        ],
        "members": [thread_member_payload(member) for member in memberships],
        "has_more": False,
    }
    if parent_ids is not None:
        payload["channel_ids"] = [str(item) for item in sorted(parent_ids)]
        payload["channel_refs"] = [
            {"id": str(item), "origin_domain": guild.origin_domain} for item in sorted(parent_ids)
        ]
    return payload


@router.patch("/channels/{thread_ref}")
async def update_thread(
    thread_ref: EntityRef,
    payload: ThreadUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    proxy_access, _ = await thread_access(
        session, redis, settings, auth.user, thread_ref, lock=False
    )
    if cast(Guild, proxy_access.guild).origin_domain != settings.domain:
        return await proxy_remote_thread_mutation(
            session,
            settings,
            proxy_access,
            auth.user,
            "thread.update",
            payload=payload.model_dump(mode="json", exclude_none=True),
        )
    access, permissions = await thread_access(
        session, redis, settings, auth.user, thread_ref, lock=True
    )
    guild = cast(Guild, access.guild)
    thread = access.channel
    was_archived = bool(thread.archived)
    owner = (thread.owner_id, thread.owner_domain) == (
        auth.user.id,
        auth.user.origin_domain,
    )
    manager = bool(permissions & Permission.MANAGE_THREADS)
    values = payload.model_dump(exclude_unset=True)
    flags = values.pop("flags", None)
    if flags is not None:
        values["pinned"] = bool(flags & THREAD_FLAG_PINNED)
    if thread.locked and not manager:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "THREAD_LOCKED",
                "permissions": str(int(Permission.MANAGE_THREADS)),
            },
        )
    reopen_only = set(values) == {"archived"} and values.get("archived") is False
    if not owner and not manager:
        if not reopen_only:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
        membership = await session.get(
            ThreadMember,
            (thread.id, thread.origin_domain, auth.user.id, auth.user.origin_domain),
        )
        if membership is None or not permissions & Permission.SEND_MESSAGES_IN_THREADS:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    if thread.archived and values.get("archived") is not False:
        raise HTTPException(status_code=409, detail={"code": "THREAD_ARCHIVED"})
    if "rate_limit_per_user" in values and not manager:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MISSING_PERMISSIONS",
                "permissions": str(int(Permission.MANAGE_THREADS)),
            },
        )
    if ("locked" in values or "pinned" in values) and not manager:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MISSING_PERMISSIONS",
                "permissions": str(int(Permission.MANAGE_THREADS)),
            },
        )
    if "invitable" in values and thread.type != 12:
        raise HTTPException(status_code=400, detail={"code": "THREAD_INVITABLE_PRIVATE_ONLY"})
    parent = await session.get(Channel, (thread.parent_id, thread.parent_domain))
    if parent is None:
        raise HTTPException(status_code=409, detail={"code": "THREAD_PARENT_INVALID"})
    if values.get("applied_tag_ids") is not None:
        next_tags = {int(item) for item in values["applied_tag_ids"]}
        validate_applied_tags(parent, list(next_tags), require_tag=False)
        previous_tags = {int(item) for item in thread.applied_tag_ids}
        if (previous_tags ^ next_tags) & _moderated_tag_ids(parent) and not manager:
            raise HTTPException(status_code=403, detail={"code": "MODERATED_TAG_FORBIDDEN"})
    if thread.archived and values.get("archived") is False:
        await require_active_thread_capacity(
            session,
            guild,
            excluding=(thread.id, thread.origin_domain),
        )

    changed: list[dict[str, object]] = []
    sibling_updates: list[Channel] = []
    pinned = values.pop("pinned", None)
    if pinned is not None:
        if parent.type != 15:
            raise HTTPException(status_code=400, detail={"code": "THREAD_PIN_FORUM_ONLY"})
        if not manager:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
        if pinned:
            siblings = list(
                await session.scalars(
                    select(Channel)
                    .where(
                        Channel.parent_id == parent.id,
                        Channel.parent_domain == parent.origin_domain,
                        Channel.id != thread.id,
                        Channel.type.in_(THREAD_TYPES),
                        Channel.flags.op("&")(THREAD_FLAG_PINNED) != 0,
                    )
                    .with_for_update()
                )
            )
            for sibling in siblings:
                sibling.flags &= ~THREAD_FLAG_PINNED
            sibling_updates.extend(siblings)
            thread.flags |= THREAD_FLAG_PINNED
        else:
            thread.flags &= ~THREAD_FLAG_PINNED
        changed.append({"key": "pinned", "new_value": pinned})
    if values.get("archived") is True and thread.flags & THREAD_FLAG_PINNED:
        thread.flags &= ~THREAD_FLAG_PINNED
        changed.append({"key": "pinned", "old_value": True, "new_value": False})
    for field, value in values.items():
        if field == "applied_tag_ids" and value is not None:
            value = [str(item) for item in value]
        old = getattr(thread, field)
        if old != value:
            setattr(thread, field, value)
            changed.append({"key": field, "old_value": old, "new_value": value})
    archived_changed = any(item["key"] == "archived" for item in changed)
    auto_archive_changed = any(item["key"] == "auto_archive_duration" for item in changed)
    if archived_changed or auto_archive_changed:
        activity_at = datetime.now(UTC)
        thread.archive_timestamp = activity_at
        # Discord treats changing the auto-archive duration and unarchiving as
        # activity. Keep the sweep deadline and forum Recently Active order on
        # the same authoritative timestamp.
        if auto_archive_changed or (was_archived and not thread.archived):
            thread.last_activity_at = activity_at
    if changed:
        for item in [*sibling_updates, thread]:
            await queue_guild_mutation(
                session,
                settings,
                guild,
                auth.user,
                "guild.channel.update",
                {"channel": federation_channel_state(item)},
                channel=item,
            )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            111,
            target_type="thread",
            target_ref={"id": str(thread.id)},
            changes=changed,
        )
        await session.commit()
        await wake_queued_guild_federation(guild)
        for item in sibling_updates:
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "THREAD_UPDATE",
                channel_payload(item),
            )
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "THREAD_UPDATE",
            channel_payload(thread),
        )
        if was_archived and not thread.archived:
            await publish_current_thread_member_updates(
                session,
                redis,
                guild,
                thread,
            )
    return await rendered_thread(session, redis, guild, auth.user, thread)


@router.delete("/channels/{thread_ref}", status_code=204)
async def delete_thread(
    thread_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> Response:
    proxy_access, _ = await thread_access(
        session,
        redis,
        settings,
        auth.user,
        thread_ref,
        needed=Permission.VIEW_CHANNEL | Permission.MANAGE_THREADS,
        lock=False,
    )
    if cast(Guild, proxy_access.guild).origin_domain != settings.domain:
        await proxy_remote_thread_mutation(
            session,
            settings,
            proxy_access,
            auth.user,
            "thread.delete",
        )
        return Response(status_code=204)
    access, _ = await thread_access(
        session,
        redis,
        settings,
        auth.user,
        thread_ref,
        needed=Permission.VIEW_CHANNEL | Permission.MANAGE_THREADS,
        lock=True,
    )
    guild = cast(Guild, access.guild)
    thread = access.channel
    deleted_payload = {
        "id": str(thread.id),
        "origin_domain": thread.origin_domain,
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
        "parent_id": str(thread.parent_id),
        "parent_domain": thread.parent_domain,
        "type": thread.type,
    }
    parent = await session.get(Channel, (thread.parent_id, thread.parent_domain))
    if parent is None:
        raise HTTPException(status_code=409, detail={"code": "THREAD_PARENT_INVALID"})

    linked_message_event: tuple[str, dict[str, object]] | None = None
    linked_message = await session.get(Message, (thread.id, thread.origin_domain))
    if linked_message is not None and (
        linked_message.channel_id,
        linked_message.channel_domain,
    ) == (parent.id, parent.origin_domain):
        if linked_message.message_type == 18:
            linked_message.content = None
            linked_message.e2ee = None
            linked_message.deleted_at = datetime.now(UTC)
            await queue_guild_mutation(
                session,
                settings,
                guild,
                auth.user,
                "guild.message.delete",
                {
                    "message": {
                        "id": str(linked_message.id),
                        "origin_domain": linked_message.origin_domain,
                    },
                    "deleted_at": linked_message.deleted_at.isoformat(),
                },
                channel=parent,
            )
            linked_message_event = (
                "MESSAGE_DELETE",
                {
                    "id": str(linked_message.id),
                    "origin_domain": linked_message.origin_domain,
                    "channel_id": str(parent.id),
                    "channel_domain": parent.origin_domain,
                },
            )
        elif linked_message.flags & MESSAGE_FLAG_HAS_THREAD:
            linked_message.flags &= ~MESSAGE_FLAG_HAS_THREAD
            rendered_source = await render_message_payload(session, linked_message, auth.user)
            await queue_guild_mutation(
                session,
                settings,
                guild,
                auth.user,
                "guild.message.update",
                {"message": rendered_source, "thread_detached": True},
                channel=parent,
            )
            linked_message_event = (
                "MESSAGE_UPDATE",
                {
                    "id": str(linked_message.id),
                    "origin_domain": linked_message.origin_domain,
                    "channel_id": str(parent.id),
                    "channel_domain": parent.origin_domain,
                    "flags": linked_message.flags,
                    "thread": None,
                },
            )

    if parent.type == 15 and (parent.last_thread_id, parent.last_thread_domain) == (
        thread.id,
        thread.origin_domain,
    ):
        previous_post = await session.scalar(
            select(Channel)
            .where(
                Channel.parent_id == parent.id,
                Channel.parent_domain == parent.origin_domain,
                Channel.type == 11,
                Channel.unavailable.is_(False),
                Channel.id != thread.id,
            )
            .order_by(Channel.created_at.desc(), Channel.origin_domain.desc(), Channel.id.desc())
            .limit(1)
        )
        parent.last_thread_id = previous_post.id if previous_post is not None else None
        parent.last_thread_domain = (
            previous_post.origin_domain if previous_post is not None else None
        )
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.forum.cursor.update",
            {
                "forum": {"id": str(parent.id), "origin_domain": parent.origin_domain},
                "last_thread_id": (
                    str(parent.last_thread_id) if parent.last_thread_id is not None else None
                ),
                "last_thread_domain": parent.last_thread_domain,
            },
            channel=parent,
        )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.channel.delete",
        {"channel": deleted_payload},
        channel=thread,
    )
    # Visibility for remote destinations must be computed against the live
    # private-thread membership above. Only tombstone it after durable routing.
    thread.unavailable = True
    # Unavailable tombstones no longer participate in inherited permissions.
    # Detaching them lets an emptied parent be physically deleted without the
    # parent FK's SET NULL action violating the live-thread invariant.
    thread.parent_id = None
    thread.parent_domain = None
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        112,
        target_type="thread",
        target_ref={"id": str(thread.id)},
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    if linked_message_event is not None:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            linked_message_event[0],
            linked_message_event[1],
        )
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "THREAD_DELETE", deleted_payload
    )
    return Response(status_code=204)


async def list_thread_members_service(
    thread_ref: EntityRef,
    actor: User,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    limit: int = 1000,
    after: EntityRef | None = None,
    with_member: bool = False,
) -> list[dict[str, object]]:
    access, _ = await thread_access(session, redis, settings, actor, thread_ref)
    statement = select(ThreadMember).where(
        ThreadMember.thread_id == access.channel.id,
        ThreadMember.thread_domain == access.channel.origin_domain,
    )
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        statement = statement.where(
            or_(
                ThreadMember.user_domain > after_domain,
                and_(
                    ThreadMember.user_domain == after_domain,
                    ThreadMember.user_id > after_id,
                ),
            )
        )
    members = list(
        await session.scalars(
            statement.order_by(ThreadMember.user_domain, ThreadMember.user_id).limit(limit)
        )
    )
    if with_member:
        return [await rich_thread_member_payload(session, item) for item in members]
    return [thread_member_payload(item) for item in members]


async def get_thread_member_service(
    thread_ref: EntityRef,
    target_ref: EntityRef,
    actor: User,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    with_member: bool = False,
) -> dict[str, object]:
    access, _ = await thread_access(session, redis, settings, actor, thread_ref)
    target_id, target_domain = target_ref.resolve(settings.domain)
    member = await session.get(
        ThreadMember,
        (access.channel.id, access.channel.origin_domain, target_id, target_domain),
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "THREAD_MEMBER_NOT_FOUND"})
    return (
        await rich_thread_member_payload(session, member)
        if with_member
        else thread_member_payload(member)
    )


@router.get("/channels/{thread_ref}/thread-members")
async def list_thread_members(
    thread_ref: EntityRef,
    limit: int = Query(default=100, ge=1, le=100),
    after: EntityRef | None = None,
    with_member: bool = False,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    return await list_thread_members_service(
        thread_ref,
        auth.user,
        session,
        redis,
        settings,
        limit=limit,
        after=after,
        with_member=with_member,
    )


@router.get("/channels/{thread_ref}/thread-members/{user_ref}")
async def get_thread_member(
    thread_ref: EntityRef,
    user_ref: EntityRef,
    with_member: bool = False,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await get_thread_member_service(
        thread_ref,
        user_ref,
        auth.user,
        session,
        redis,
        settings,
        with_member=with_member,
    )


async def put_thread_member_service(
    thread_ref: EntityRef,
    target_ref: EntityRef,
    payload: ThreadMemberUpdate,
    actor: User,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> None:
    target_id, target_domain = target_ref.resolve(settings.domain)
    self_update = (target_id, target_domain) == (actor.id, actor.origin_domain)
    proxy_access, _ = await thread_access(
        session,
        redis,
        settings,
        actor,
        thread_ref,
        needed=(
            Permission.VIEW_CHANNEL
            if self_update
            else Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES_IN_THREADS
        ),
        lock=False,
    )
    if cast(Guild, proxy_access.guild).origin_domain != settings.domain:
        await proxy_remote_thread_mutation(
            session,
            settings,
            proxy_access,
            actor,
            "thread.member.put",
            payload=payload.model_dump(mode="json", exclude_unset=True),
            target_ref=target_ref,
        )
        return
    access, permissions = await thread_access(
        session,
        redis,
        settings,
        actor,
        thread_ref,
        needed=(
            Permission.VIEW_CHANNEL
            if self_update
            else Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES_IN_THREADS
        ),
        lock=True,
    )
    guild = cast(Guild, access.guild)
    thread = access.channel
    if thread.archived:
        raise HTTPException(status_code=409, detail={"code": "THREAD_ARCHIVED"})
    actor_member = await session.get(
        ThreadMember,
        (thread.id, thread.origin_domain, actor.id, actor.origin_domain),
    )
    if not self_update and (payload.flags != 0 or payload.notification_level != "inherit"):
        raise HTTPException(
            status_code=400,
            detail={"code": "THREAD_MEMBER_PREFERENCES_SELF_ONLY"},
        )
    if (
        not self_update
        and thread.type == 12
        and not (
            permissions & Permission.MANAGE_THREADS
            or (thread.invitable and actor_member is not None)
        )
    ):
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    target_guild_member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, target_id, target_domain),
    )
    if target_guild_member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_MEMBER_NOT_FOUND"})
    target = await session.get(User, (target_id, target_domain))
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    if target.account_type == "bot" and (thread.e2ee_required or thread.encryption_mode == "e2ee"):
        raise HTTPException(status_code=409, detail={"code": "BOT_THREAD_E2EE_UNSUPPORTED"})
    parent = await session.get(Channel, (thread.parent_id, thread.parent_domain))
    if parent is None:
        raise HTTPException(status_code=409, detail={"code": "THREAD_PARENT_INVALID"})
    target_parent_permissions = await get_permissions(session, redis, guild, target, channel=parent)
    if not target_parent_permissions & Permission.VIEW_CHANNEL:
        raise HTTPException(status_code=403, detail={"code": "THREAD_MEMBER_PARENT_HIDDEN"})
    member = await session.get(
        ThreadMember,
        (thread.id, thread.origin_domain, target_id, target_domain),
    )
    member_added = member is None
    if member is None:
        if int(thread.member_count or 0) >= MAX_THREAD_MEMBERS:
            raise HTTPException(status_code=409, detail={"code": "THREAD_MEMBER_LIMIT"})
        member = ThreadMember(
            thread_id=thread.id,
            thread_domain=thread.origin_domain,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=target_id,
            user_domain=target_domain,
            flags=payload.flags,
            notification_level=payload.notification_level,
        )
        session.add(member)
        thread.member_count = int(thread.member_count or 0) + 1
    else:
        if not self_update:
            return
        member.flags = payload.flags
        member.notification_level = payload.notification_level
    private_access_changed = thread.type == 12 and member_added
    thread_rekeyed = False
    if private_access_changed:
        guild.permission_generation += 1
        if thread.encryption_mode == "e2ee" and thread.encryption_state == "active":
            thread.encryption_state = "rekeying"
            thread_rekeyed = True
    await session.flush()
    await session.refresh(member)
    if thread_rekeyed:
        await queue_guild_mutation(
            session,
            settings,
            guild,
            actor,
            "guild.channel.update",
            {"channel": federation_channel_state(thread)},
            channel=thread,
        )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.thread.member.upsert",
        {
            "member": thread_member_payload(member),
            "member_count": thread.member_count,
        },
        channel=thread,
        snapshot_required=private_access_changed,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    if thread_rekeyed:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "THREAD_UPDATE",
            channel_payload(thread),
        )
    rendered = thread_member_payload(member)
    rich_rendered = await rich_thread_member_payload(session, member)
    topic = guild_topic(guild.origin_domain, guild.id)
    await publish_dispatch(
        redis,
        topic,
        "THREAD_MEMBER_UPDATE",
        rendered,
        audience_user_refs=[f"{target_id}@{target_domain}"],
    )
    if not member_added:
        return
    await publish_dispatch(
        redis,
        topic,
        "THREAD_CREATE",
        channel_payload(thread) | {"member": rendered},
        audience_user_refs=[f"{target_id}@{target_domain}"],
    )
    await publish_dispatch(
        redis,
        topic,
        "THREAD_MEMBERS_UPDATE",
        {
            "id": str(thread.id),
            "thread_domain": thread.origin_domain,
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "member_count": min(50, int(thread.member_count or 0)),
            "added_members": [rich_rendered],
            "removed_member_ids": [],
        },
    )


@router.put("/channels/{thread_ref}/thread-members/@me", status_code=204)
async def join_thread(
    thread_ref: EntityRef,
    payload: ThreadMemberUpdate | None = None,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await put_thread_member_service(
        thread_ref,
        EntityRef(f"{auth.user.id}@{auth.user.origin_domain}"),
        payload or ThreadMemberUpdate(),
        auth.user,
        session,
        redis,
        settings,
    )
    return Response(status_code=204)


@router.put("/channels/{thread_ref}/thread-members/{user_ref}", status_code=204)
async def add_thread_member(
    thread_ref: EntityRef,
    user_ref: EntityRef,
    payload: ThreadMemberUpdate | None = None,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await put_thread_member_service(
        thread_ref,
        user_ref,
        payload or ThreadMemberUpdate(),
        auth.user,
        session,
        redis,
        settings,
    )
    return Response(status_code=204)


async def delete_thread_member_service(
    thread_ref: EntityRef,
    target_ref: EntityRef,
    actor: User,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> None:
    target_id, target_domain = target_ref.resolve(settings.domain)
    proxy_access, _ = await thread_access(session, redis, settings, actor, thread_ref, lock=False)
    if cast(Guild, proxy_access.guild).origin_domain != settings.domain:
        await proxy_remote_thread_mutation(
            session,
            settings,
            proxy_access,
            actor,
            "thread.member.delete",
            target_ref=target_ref,
        )
        return
    access, permissions = await thread_access(
        session, redis, settings, actor, thread_ref, lock=True
    )
    guild = cast(Guild, access.guild)
    thread = access.channel
    if thread.archived:
        raise HTTPException(status_code=409, detail={"code": "THREAD_ARCHIVED"})
    self_remove = (target_id, target_domain) == (actor.id, actor.origin_domain)
    private_creator = thread.type == 12 and (
        thread.owner_id,
        thread.owner_domain,
    ) == (actor.id, actor.origin_domain)
    if not self_remove and not (permissions & Permission.MANAGE_THREADS or private_creator):
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    member = await session.get(
        ThreadMember,
        (thread.id, thread.origin_domain, target_id, target_domain),
    )
    if member is None:
        return
    await session.delete(member)
    thread.member_count = max(0, int(thread.member_count or 0) - 1)
    private_access_changed = thread.type == 12
    thread_rekeyed = False
    if private_access_changed:
        guild.permission_generation += 1
        if thread.encryption_mode == "e2ee" and thread.encryption_state == "active":
            thread.encryption_state = "rekeying"
            thread_rekeyed = True
    if thread_rekeyed:
        await queue_guild_mutation(
            session,
            settings,
            guild,
            actor,
            "guild.channel.update",
            {"channel": federation_channel_state(thread)},
            channel=thread,
        )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.thread.member.delete",
        {
            "thread_id": str(thread.id),
            "thread_domain": thread.origin_domain,
            "user_id": str(target_id),
            "user_domain": target_domain,
            "member_count": thread.member_count,
        },
        channel=thread,
        snapshot_required=private_access_changed,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    if thread_rekeyed:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "THREAD_UPDATE",
            channel_payload(thread),
        )
    topic = guild_topic(guild.origin_domain, guild.id)
    await publish_dispatch(
        redis,
        topic,
        "THREAD_MEMBERS_UPDATE",
        {
            "id": str(thread.id),
            "thread_domain": thread.origin_domain,
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "member_count": min(50, thread.member_count),
            "added_members": [],
            "removed_member_ids": [str(target_id)],
            "removed_member_refs": [{"id": str(target_id), "origin_domain": target_domain}],
        },
    )


@router.delete("/channels/{thread_ref}/thread-members/@me", status_code=204)
async def leave_thread(
    thread_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await delete_thread_member_service(
        thread_ref,
        EntityRef(f"{auth.user.id}@{auth.user.origin_domain}"),
        auth.user,
        session,
        redis,
        settings,
    )
    return Response(status_code=204)


@router.delete("/channels/{thread_ref}/thread-members/{user_ref}", status_code=204)
async def remove_thread_member(
    thread_ref: EntityRef,
    user_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await delete_thread_member_service(thread_ref, user_ref, auth.user, session, redis, settings)
    return Response(status_code=204)


@federation_router.post("/_kaede/v1/guilds/{guild_id}/proxy-thread")
async def federation_guild_thread_proxy(
    guild_id: int,
    payload: GuildThreadProxyRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Evaluate a remote guild member's thread mutation at the guild home."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "thread-mutation",
        capacity=600,
        refill_per_minute=600,
    )
    if payload.actor.origin_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    guild = await session.get(Guild, (guild_id, settings.domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    try:
        actor = await upsert_remote_user(session, settings, payload.actor)
        await record_room_federation_recipient(
            session,
            ("guild", guild.id, guild.origin_domain),
            principal.origin,
        )
        auth = AuthenticatedUser(
            actor,
            AccessGrant(actor.id, actor.origin_domain, "federation-proxy"),
            "",
            False,
        )
        channel_ref = EntityRef(f"{int(payload.channel_id)}@{settings.domain}")
        if payload.operation == "thread.create":
            create_payload = ThreadCreate.model_validate(payload.payload)
            rendered = await create_thread_service(
                channel_ref,
                create_payload,
                auth,
                session,
                redis,
                snowflake,
                settings,
                replicated_attachments=tuple(payload.attachments),
            )
            return {"thread": rendered}
        if payload.operation == "thread.create_from_message":
            if payload.message_id is None:
                raise ValueError("source message reference is missing")
            source_payload = ThreadFromMessageCreate.model_validate(payload.payload)
            rendered = await create_thread_from_message(
                channel_ref,
                payload.message_id,
                source_payload,
                auth,
                session,
                redis,
                snowflake,
                settings,
            )
            return {"thread": rendered}
        if payload.operation == "thread.update":
            update_payload = ThreadUpdate.model_validate(payload.payload)
            rendered = await update_thread(
                channel_ref,
                update_payload,
                auth,
                session,
                redis,
                snowflake,
                settings,
            )
            return {"thread": rendered}
        if payload.operation == "thread.delete":
            await delete_thread(
                channel_ref,
                auth,
                session,
                redis,
                snowflake,
                settings,
            )
            return {"deleted": True}
        if payload.target_user_id is None:
            raise ValueError("thread member target is missing")
        if payload.operation == "thread.member.put":
            member_payload = ThreadMemberUpdate.model_validate(payload.payload)
            await put_thread_member_service(
                channel_ref,
                payload.target_user_id,
                member_payload,
                actor,
                session,
                redis,
                settings,
            )
        else:
            await delete_thread_member_service(
                channel_ref,
                payload.target_user_id,
                actor,
                session,
                redis,
                settings,
            )
        return {"updated": True}
    except ValueError:
        await session.rollback()
        raise HTTPException(
            status_code=422, detail={"code": "KAED_THREAD_MUTATION_INVALID"}
        ) from None


# Bot routes are deliberately thin adapters over the exact human services.
def redact_bot_thread_result(
    rendered: dict[str, object],
    principal: BotPrincipal,
    installation: object | None,
) -> dict[str, object]:
    granted_scopes = getattr(installation, "granted_scopes", ())
    return redact_bot_thread_payload(
        rendered,
        can_read_history=(
            "messages.history" in principal.scopes and "messages.history" in granted_scopes
        ),
        can_read_content=(
            "messages.content" in principal.scopes and "messages.content" in granted_scopes
        ),
        can_read_attachments=(
            "attachments.read" in principal.scopes and "attachments.read" in granted_scopes
        ),
    )


@bot_router.post("/channels/{parent_ref}/threads", status_code=201)
async def bot_create_thread(
    parent_ref: EntityRef,
    payload: ThreadCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    parent, installation = await installation_for_channel(
        session, settings, principal, parent_ref, "messages.send"
    )
    if parent.e2ee_required or parent.encryption_mode == "e2ee":
        raise HTTPException(status_code=409, detail={"code": "BOT_THREAD_E2EE_UNSUPPORTED"})
    starter = payload.starter()
    if starter is not None and starter.attachment_ids:
        if installation is None:
            raise HTTPException(status_code=404, detail={"code": "BOT_INSTALLATION_NOT_FOUND"})
        await require_owned_attachments_for_installation(
            session,
            settings,
            principal,
            installation,
            [int(item) for item in starter.attachment_ids],
        )
    return redact_bot_thread_result(
        await create_thread_service(
            parent_ref,
            payload,
            user_auth(principal),
            session,
            redis,
            snowflake,
            settings,
        ),
        principal,
        installation,
    )


@bot_router.post("/channels/{parent_ref}/messages/{message_ref}/threads", status_code=201)
async def bot_create_thread_from_message(
    parent_ref: EntityRef,
    message_ref: EntityRef,
    payload: ThreadFromMessageCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    parent, installation = await installation_for_channel(
        session, settings, principal, parent_ref, "messages.send"
    )
    if parent.e2ee_required or parent.encryption_mode == "e2ee":
        raise HTTPException(status_code=409, detail={"code": "BOT_THREAD_E2EE_UNSUPPORTED"})
    return redact_bot_thread_result(
        await create_thread_from_message(
            parent_ref,
            message_ref,
            payload,
            user_auth(principal),
            session,
            redis,
            snowflake,
            settings,
        ),
        principal,
        installation,
    )


@bot_router.get("/channels/{parent_ref}/threads")
async def bot_list_parent_threads(
    parent_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    archived: bool = False,
    include_archived: bool = False,
    before: datetime | None = None,
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    tag_id: list[WireSnowflake] | None = Query(default=None, max_length=20),
    sort_order: Literal[0, 1] | None = None,
    query: str | None = Query(default=None, min_length=1, max_length=100),
) -> dict[str, object]:
    if archived and include_archived:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PAGINATION"})
    _, installation = await installation_for_channel(
        session, settings, principal, parent_ref, "channels.read"
    )
    result = await list_parent_threads_service(
        parent_ref,
        principal.user,
        session,
        redis,
        settings,
        archived=archived,
        include_archived=include_archived,
        before=before,
        cursor=cursor,
        limit=limit,
        tag_ids=list(dict.fromkeys(int(item) for item in tag_id or [])),
        sort_order=sort_order,
        query=query,
    )
    for rendered in cast(list[dict[str, object]], result["threads"]):
        redact_bot_thread_result(rendered, principal, installation)
    return result


@bot_router.get("/guilds/{guild_ref}/threads/active")
async def bot_list_active_guild_threads(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "channels.read"
    )
    result = await list_active_guild_threads(
        guild_ref, user_auth(principal), session, redis, settings
    )
    for rendered in cast(list[dict[str, object]], result["threads"]):
        redact_bot_thread_result(rendered, principal, installation)
    return result


@bot_router.patch("/channels/{thread_ref}")
async def bot_update_thread(
    thread_ref: EntityRef,
    payload: ThreadUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _, installation = await installation_for_channel(
        session, settings, principal, thread_ref, "channels.manage"
    )
    return redact_bot_thread_result(
        await update_thread(
            thread_ref,
            payload,
            user_auth(principal),
            session,
            redis,
            snowflake,
            settings,
        ),
        principal,
        installation,
    )


@bot_router.delete("/channels/{thread_ref}", status_code=204)
async def bot_delete_thread(
    thread_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, thread_ref, "channels.manage")
    return await delete_thread(
        thread_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@bot_router.get("/channels/{thread_ref}/thread-members")
async def bot_list_thread_members(
    thread_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=100, ge=1, le=100),
    after: EntityRef | None = None,
    with_member: bool = False,
) -> list[dict[str, object]]:
    await installation_for_channel(session, settings, principal, thread_ref, "members.read")
    return await list_thread_members_service(
        thread_ref,
        principal.user,
        session,
        redis,
        settings,
        limit=limit,
        after=after,
        with_member=with_member,
    )


@bot_router.get("/channels/{thread_ref}/thread-members/{user_ref}")
async def bot_get_thread_member(
    thread_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    with_member: bool = False,
) -> dict[str, object]:
    await installation_for_channel(session, settings, principal, thread_ref, "members.read")
    return await get_thread_member_service(
        thread_ref,
        user_ref,
        principal.user,
        session,
        redis,
        settings,
        with_member=with_member,
    )


@bot_router.put("/channels/{thread_ref}/thread-members/@me", status_code=204)
async def bot_join_thread(
    thread_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    payload: ThreadMemberUpdate | None = None,
) -> Response:
    await installation_for_channel(session, settings, principal, thread_ref, "channels.read")
    await put_thread_member_service(
        thread_ref,
        EntityRef(f"{principal.user.id}@{principal.user.origin_domain}"),
        payload or ThreadMemberUpdate(),
        principal.user,
        session,
        redis,
        settings,
    )
    return Response(status_code=204)


@bot_router.delete("/channels/{thread_ref}/thread-members/@me", status_code=204)
async def bot_leave_thread(
    thread_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, thread_ref, "channels.read")
    await delete_thread_member_service(
        thread_ref,
        EntityRef(f"{principal.user.id}@{principal.user.origin_domain}"),
        principal.user,
        session,
        redis,
        settings,
    )
    return Response(status_code=204)


@bot_router.put("/channels/{thread_ref}/thread-members/{user_ref}", status_code=204)
async def bot_add_thread_member(
    thread_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    payload: ThreadMemberUpdate | None = None,
) -> Response:
    await installation_for_channel(session, settings, principal, thread_ref, "messages.send")
    await put_thread_member_service(
        thread_ref,
        user_ref,
        payload or ThreadMemberUpdate(),
        principal.user,
        session,
        redis,
        settings,
    )
    return Response(status_code=204)


@bot_router.delete("/channels/{thread_ref}/thread-members/{user_ref}", status_code=204)
async def bot_remove_thread_member(
    thread_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await installation_for_channel(session, settings, principal, thread_ref, "channels.manage")
    await delete_thread_member_service(
        thread_ref, user_ref, principal.user, session, redis, settings
    )
    return Response(status_code=204)
