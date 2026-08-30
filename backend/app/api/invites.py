from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
import string
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from typing import Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    optional_user,
    require_user,
)
from app.api.guilds import local_guild
from app.api.scheduled_events import (
    active_scheduled_event_by_ref,
    active_scheduled_event_for_invite,
    require_scheduled_event_view,
    scheduled_event_invite_payload,
)
from app.automod.service import AutoModPostCommit, evaluate_member_profile
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import (
    guild_authority_owner,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.hierarchy import guild_role, require_can_manage_role
from app.chat.invites import (
    grant_invite_roles,
    invite_allows_user,
    invite_target_payload,
)
from app.chat.payloads import guild_payload, member_payload, user_payload
from app.chat.permissions import get_permissions, require_permissions
from app.chat.schemas import InviteCreate
from app.core.channel_types import GUILD_VOICE_CHANNEL_TYPES
from app.core.errors import parse_upstream_error
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.proxy import resolve_client_ip
from app.core.rate_limits import (
    CLIENT_RATE_LIMITS,
    enforce_client_rate_limit,
    enforce_keyed_rate_limit,
)
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReference, validate_snowflake
from app.db.models import (
    Ban,
    Channel,
    Guild,
    GuildInstanceBan,
    GuildMember,
    Invite,
    MemberRole,
    User,
)
from app.federation.client import signed_request
from app.federation.guild_management import (
    guild_management_dict_body,
    guild_management_list_body,
    proxy_remote_guild_management,
    qualified_management_ref,
)
from app.federation.guilds import (
    apply_guild_snapshot,
    begin_remote_guild_join,
    fetch_guild_snapshot,
)
from app.federation.identity_storage import FederationIdentityQuotaExceeded
from app.federation.network import (
    FederationInstanceQuotaExceeded,
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.replica_storage import (
    REPLICA_QUOTA_ERROR_CODE,
    FederationReplicaQuotaExceeded,
    mark_replica_capacity_paused,
    mark_replica_quota_paused,
)
from app.federation.replication import profile_from_user
from app.voice.livekit import LiveKitError, screen_share_is_active
from app.voice.rooms import guild_room_name, participant_identity
from app.voice.state import occupant_in_room

router = APIRouter(prefix="/api/v1", tags=["invites"])
ALPHABET = string.ascii_letters + string.digits
INVITE_CODE_LENGTH = 8
REMOTE_INVITE_PREVIEW_CONCURRENCY = 16
remote_invite_preview_slots = asyncio.Semaphore(REMOTE_INVITE_PREVIEW_CONCURRENCY)
log = structlog.get_logger()

MAX_TARGET_USERS_UPLOAD_BYTES = 512 * 1024
MAX_TARGET_USERS = 1_000

FEDERATED_GUILD_PAYLOAD_FIELDS = frozenset(
    {
        "id",
        "origin_domain",
        "name",
        "description",
        "icon_hash",
        "banner_hash",
        "owner_id",
        "owner_domain",
        "permission_generation",
        "federated_history_policy",
        "history_policy_generation",
        "unavailable",
        "sync_status",
        "sync_error_code",
        "version",
    }
)
FEDERATED_INVITE_RESOLVE_FIELDS = frozenset(
    {
        "code",
        "guild",
        "channel_id",
        "target_type",
        "target_user_id",
        "scheduled_event_id",
        "role_ids",
        "target_user_count",
    }
)
FEDERATED_INVITE_MANAGED_FIELDS = frozenset(
    {
        *FEDERATED_INVITE_RESOLVE_FIELDS,
        "expires_at",
        "uses",
        "max_uses",
        "temporary",
        "reusable",
        "created_at",
        "revoked_at",
    }
)


def validated_federated_guild_payload(
    payload: object,
    *,
    expected_guild: tuple[int, str],
) -> dict[str, object]:
    """Validate the exact public guild projection nested in invite responses."""

    if not isinstance(payload, dict) or set(payload) != FEDERATED_GUILD_PAYLOAD_FIELDS:
        raise ValueError("federated guild projection has an invalid shape")
    guild_id = validate_snowflake(payload.get("id"))
    guild_domain = normalize_domain(str(payload.get("origin_domain", "")))
    if (
        (guild_id, guild_domain) != expected_guild
        or payload.get("id") != str(guild_id)
        or payload.get("origin_domain") != guild_domain
    ):
        raise ValueError("federated guild projection escaped its requested identity")
    name = payload.get("name")
    description = payload.get("description")
    if (
        not isinstance(name, str)
        or not 2 <= len(name) <= 100
        or (
            description is not None and (not isinstance(description, str) or len(description) > 500)
        )
    ):
        raise ValueError("federated guild projection has invalid text")
    for asset_field in ("icon_hash", "banner_hash"):
        value = payload.get(asset_field)
        if value is not None and (not isinstance(value, str) or not value or len(value) > 128):
            raise ValueError("federated guild projection has invalid media metadata")
    for snowflake_field in (
        "owner_id",
        "permission_generation",
        "history_policy_generation",
    ):
        value = payload.get(snowflake_field)
        if not isinstance(value, str) or str(validate_snowflake(value)) != value:
            raise ValueError("federated guild projection has an invalid version")
    raw_version = payload.get("version")
    if not isinstance(raw_version, str) or len(raw_version) > 64:
        raise ValueError("federated guild projection has an invalid version")
    try:
        parsed_version = datetime.fromisoformat(raw_version)
    except ValueError:
        raise ValueError("federated guild projection has an invalid version") from None
    if parsed_version.tzinfo is None or parsed_version.isoformat() != raw_version:
        raise ValueError("federated guild projection has an invalid version")
    owner_domain = normalize_domain(str(payload.get("owner_domain", "")))
    sync_status = payload.get("sync_status")
    sync_error_code = payload.get("sync_error_code")
    if (
        payload.get("owner_domain") != owner_domain
        or payload.get("federated_history_policy") not in {"disabled", "full_retained"}
        or type(payload.get("unavailable")) is not bool
        or sync_status not in {"ready", "syncing", "stale", "failed", "quota_paused"}
        or (
            sync_error_code is not None
            and (
                not isinstance(sync_error_code, str)
                or not sync_error_code
                or len(sync_error_code) > 64
                or sync_status not in {"failed", "quota_paused"}
            )
        )
    ):
        raise ValueError("federated guild projection has invalid authority state")
    return cast(dict[str, object], payload)


def parse_invite_management_code(value: str) -> tuple[str, str | None]:
    """Parse a local code or the canonical ``code@authority`` management form."""

    code, separator, domain = value.rpartition("@")
    if not separator:
        code = value
        domain = ""
    if len(code) != INVITE_CODE_LENGTH or any(character not in ALPHABET for character in code):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    if not separator:
        return code, None
    try:
        canonical_domain = normalize_domain(domain)
    except FederationNetworkError:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"}) from None
    if domain != canonical_domain:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    return code, canonical_domain


def normalize_target_user_refs(values: object, default_domain: str) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_TARGET_USERS:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVITE_TARGET_USERS_INVALID"},
        )
    normalized: list[str] = []
    seen: set[tuple[int, str]] = set()
    errors: list[str] = []
    for index, raw in enumerate(values, start=2):
        if not isinstance(raw, str):
            errors.append(f"Line {index}: invalid user ID format")
            continue
        try:
            user_ref = EntityRef(raw).resolve(default_domain)
        except ValueError:
            errors.append(f"Line {index}: invalid user ID format")
            continue
        if user_ref in seen:
            continue
        seen.add(user_ref)
        normalized.append(f"{user_ref[0]}@{user_ref[1]}")
    if errors:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVITE_TARGET_USERS_INVALID",
                "target_users_file": errors[:100],
            },
        )
    return normalized


def target_users_csv_bytes(raw: bytes, content_type: str) -> bytes:
    if not content_type.lower().startswith("multipart/form-data"):
        return raw
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
    )
    if not message.is_multipart():
        raise HTTPException(
            status_code=400,
            detail={"code": "INVITE_TARGET_USERS_INVALID"},
        )
    uploads = [
        part.get_payload(decode=True)
        for part in message.iter_parts()
        if part.get_param("name", header="content-disposition") == "target_users_file"
    ]
    if len(uploads) != 1 or uploads[0] is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVITE_TARGET_USERS_FILE_REQUIRED"},
        )
    return cast(bytes, uploads[0])


def parse_target_users_upload(raw: bytes, content_type: str, default_domain: str) -> list[str]:
    if len(raw) > MAX_TARGET_USERS_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "INVITE_TARGET_USERS_FILE_TOO_LARGE"},
        )
    if content_type.lower().startswith("application/json"):
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVITE_TARGET_USERS_INVALID"},
            ) from None
        if not isinstance(body, dict) or set(body) != {"target_user_ids"}:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVITE_TARGET_USERS_INVALID"},
            )
        return normalize_target_user_refs(body["target_user_ids"], default_domain)
    csv_bytes = target_users_csv_bytes(raw, content_type)
    if len(csv_bytes) > MAX_TARGET_USERS_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "INVITE_TARGET_USERS_FILE_TOO_LARGE"},
        )
    try:
        decoded = csv_bytes.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(decoded), strict=True))
    except (UnicodeDecodeError, csv.Error):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVITE_TARGET_USERS_INVALID"},
        ) from None
    if not rows or rows[0] != ["user_id"] or any(len(row) != 1 for row in rows[1:]):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVITE_TARGET_USERS_INVALID"},
        )
    return normalize_target_user_refs([row[0] for row in rows[1:]], default_domain)


def render_target_users_csv(target_user_ids: list[str], code: str) -> Response:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["user_id"])
    writer.writerows([user_id] for user_id in target_user_ids)
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={code}-target-users.csv"},
    )


def validated_federated_target_user_refs(payload: object, default_domain: str) -> list[str]:
    if not isinstance(payload, list) or any(
        not isinstance(user_ref, str) or "@" not in user_ref for user_ref in payload
    ):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
        )
    try:
        return normalize_target_user_refs(payload, default_domain)
    except HTTPException:
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
        ) from None


def validated_target_users_job_status(payload: object) -> dict[str, object]:
    expected = {
        "status",
        "total_users",
        "processed_users",
        "created_at",
        "completed_at",
        "error_message",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
        )
    status_code = payload["status"]
    total_users = payload["total_users"]
    processed_users = payload["processed_users"]
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or status_code not in {0, 1, 2, 3}
        or isinstance(total_users, bool)
        or not isinstance(total_users, int)
        or not 0 <= total_users <= MAX_TARGET_USERS
        or isinstance(processed_users, bool)
        or not isinstance(processed_users, int)
        or not 0 <= processed_users <= total_users
    ):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
        )
    for field in ("created_at", "completed_at"):
        raw_timestamp = payload[field]
        if raw_timestamp is None and field == "completed_at":
            continue
        if not isinstance(raw_timestamp, str):
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
            )
        try:
            parsed = datetime.fromisoformat(raw_timestamp)
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
            ) from None
        if parsed.tzinfo is None:
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
            )
    error_message = payload["error_message"]
    if error_message is not None and (
        not isinstance(error_message, str) or len(error_message) > 1_000
    ):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
        )
    return cast(dict[str, object], payload)


async def publish_existing_replica_status(
    session: AsyncSession,
    redis: Redis,
    guild_id: int,
    guild_domain: str,
) -> None:
    """Project a committed replica pause without hiding the API's 507 response."""

    guild = await session.get(Guild, (guild_id, guild_domain), populate_existing=True)
    if guild is None:
        # A first-time join rolls the snapshot back completely. There is no
        # navigation entry to update; the initiating request carries the
        # actionable capacity response instead.
        return
    try:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_UPDATE",
            guild_payload(guild),
        )
    except Exception:
        log.exception(
            "remote_guild_capacity_status_publish_failed",
            guild_id=str(guild.id),
            guild_domain=guild.origin_domain,
        )


async def new_invite_code(session: AsyncSession) -> str:
    for _ in range(10):
        code = "".join(secrets.choice(ALPHABET) for _ in range(INVITE_CODE_LENGTH))
        if await session.get(Invite, code) is None:
            return code
    raise RuntimeError("could not allocate an invite code")


async def validate_invite_targets(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    channel: Channel | None,
    payload: InviteCreate,
) -> tuple[
    tuple[int | None, str | None],
    tuple[int | None, str | None],
]:
    """Resolve and authorize Discord-compatible invite target references."""

    target_user_ref: tuple[int | None, str | None] = (None, None)
    if payload.target_type is not None and (
        channel is None or channel.type not in GUILD_VOICE_CHANNEL_TYPES
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVITE_TARGET_REQUIRES_VOICE_CHANNEL"},
        )
    if payload.target_type == "stream":
        if payload.target_user_id is None or channel is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVITE_TARGET_STREAM_UNAVAILABLE"},
            )
        resolved_user_ref = payload.target_user_id.resolve(settings.domain)
        target_user_ref = resolved_user_ref
        member = await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, resolved_user_ref[0], resolved_user_ref[1]),
        )
        if member is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVITE_TARGET_USER_NOT_IN_GUILD"},
            )
        room = guild_room_name(guild.id, channel.id)
        identity = participant_identity(resolved_user_ref[0], resolved_user_ref[1])
        occupant = await occupant_in_room(
            redis,
            guild.origin_domain,
            room,
            identity,
        )
        if occupant is None or not occupant.can_stream:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVITE_TARGET_STREAM_UNAVAILABLE"},
            )
        try:
            is_streaming = await screen_share_is_active(
                settings,
                room,
                identity,
            )
        except LiveKitError:
            raise HTTPException(
                status_code=503,
                detail={"code": "VOICE_SERVICE_UNAVAILABLE"},
            ) from None
        if not is_streaming:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVITE_TARGET_STREAM_UNAVAILABLE"},
            )
    scheduled_event_ref: tuple[int | None, str | None] = (None, None)
    if payload.scheduled_event_id is not None:
        scheduled_event_ref = payload.scheduled_event_id.resolve(guild.origin_domain)
        if scheduled_event_ref[1] != guild.origin_domain:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVITE_TARGET_EVENT_INVALID"},
            )
    return target_user_ref, scheduled_event_ref


async def validate_community_invite(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    actor: User,
    payload: InviteCreate,
) -> tuple[list[str], list[str]]:
    """Validate role-granting and targeted invites once at the guild authority."""

    role_refs: list[tuple[int, str]] = []
    if payload.role_ids:
        await require_permissions(session, redis, guild, actor, Permission.MANAGE_ROLES)
        for raw_ref in payload.role_ids:
            role_id, role_domain = raw_ref.resolve(guild.origin_domain)
            if role_domain != guild.origin_domain or role_id == guild.id:
                raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
            role = await guild_role(session, guild, role_id)
            await require_can_manage_role(session, guild, actor, role)
            role_refs.append((role.id, role.origin_domain))

    target_user_refs: list[tuple[int, str]] = []
    if payload.target_user_ids:
        await require_permissions(session, redis, guild, actor, Permission.MANAGE_GUILD)
        target_user_refs = [raw_ref.resolve(settings.domain) for raw_ref in payload.target_user_ids]
    return (
        [f"{role_id}@{domain}" for role_id, domain in sorted(set(role_refs))],
        [f"{user_id}@{domain}" for user_id, domain in sorted(set(target_user_refs))],
    )


def invite_payload(
    invite: Invite,
    guild: Guild,
    *,
    guild_scheduled_event: dict[str, object] | None = None,
    include_metadata: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": invite.code,
        "guild": guild_payload(guild),
        "channel_id": str(invite.channel_id) if invite.channel_id is not None else None,
        **invite_target_payload(invite),
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
    }
    if include_metadata:
        payload.update(
            {
                "uses": invite.uses,
                "max_uses": invite.max_uses,
                "temporary": invite.temporary,
                "reusable": invite.reusable,
                "created_at": invite.created_at.isoformat(),
                "revoked_at": invite.revoked_at.isoformat() if invite.revoked_at else None,
            }
        )
    if guild_scheduled_event is not None:
        payload["guild_scheduled_event"] = guild_scheduled_event
    return payload


def validated_federated_invite_payload(
    payload: object,
    *,
    expected_guild: tuple[int, str],
    expected_code: str | None = None,
    expected_channel_id: int | None = None,
    validate_channel: bool = False,
    shape: Literal["resolve", "managed"] = "managed",
) -> dict[str, object]:
    """Fail closed if a signed invite result escapes its requested resource."""

    try:
        expected_fields = (
            FEDERATED_INVITE_RESOLVE_FIELDS
            if shape == "resolve"
            else FEDERATED_INVITE_MANAGED_FIELDS
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("guild"), dict):
            raise ValueError
        if set(payload) not in {expected_fields, expected_fields | {"guild_scheduled_event"}}:
            raise ValueError
        guild_payload = validated_federated_guild_payload(
            payload["guild"],
            expected_guild=expected_guild,
        )
        guild_ref = (
            validate_snowflake(guild_payload["id"]),
            normalize_domain(str(guild_payload["origin_domain"])),
        )
        code = payload.get("code")
        channel_id = payload.get("channel_id")
        if channel_id is not None and (
            not isinstance(channel_id, str) or str(validate_snowflake(channel_id)) != channel_id
        ):
            raise ValueError
        if (
            guild_ref != expected_guild
            or not isinstance(code, str)
            or len(code) != INVITE_CODE_LENGTH
            or not code.isascii()
            or not code.isalnum()
            or (expected_code is not None and code != expected_code)
            or (
                validate_channel
                and (
                    (expected_channel_id is None and channel_id is not None)
                    or (
                        expected_channel_id is not None
                        and validate_snowflake(channel_id) != expected_channel_id
                    )
                )
            )
        ):
            raise ValueError
        raw_roles = payload.get("role_ids")
        if not isinstance(raw_roles, list) or len(raw_roles) > 100:
            raise ValueError
        role_refs: set[tuple[int, str]] = set()
        previous_role: tuple[int, str] | None = None
        for raw_role in raw_roles:
            if not isinstance(raw_role, str) or "@" not in raw_role:
                raise ValueError
            role_ref = EntityRef(raw_role).resolve(expected_guild[1])
            if (
                role_ref[1] != expected_guild[1]
                or raw_role != f"{role_ref[0]}@{role_ref[1]}"
                or role_ref in role_refs
                or (previous_role is not None and role_ref <= previous_role)
            ):
                raise ValueError
            role_refs.add(role_ref)
            previous_role = role_ref
        raw_target_count = payload.get("target_user_count")
        if type(raw_target_count) is not int or not 0 <= raw_target_count <= MAX_TARGET_USERS:
            raise ValueError
        target_type = payload.get("target_type")
        target_user = payload.get("target_user_id")
        if target_type not in {None, "stream"} or (target_type == "stream") != (
            target_user is not None
        ):
            raise ValueError
        if target_user is not None and (not isinstance(target_user, str) or "@" not in target_user):
            raise ValueError
        if target_user is not None:
            target_ref = EntityRef(target_user).resolve(expected_guild[1])
            if target_user != f"{target_ref[0]}@{target_ref[1]}":
                raise ValueError
        raw_event = payload.get("scheduled_event_id")
        event_ref: tuple[int, str] | None = None
        if raw_event is not None:
            if not isinstance(raw_event, str):
                raise ValueError
            event_ref = EntityRef(raw_event).resolve(expected_guild[1])
            if event_ref[1] != expected_guild[1] or raw_event != f"{event_ref[0]}@{event_ref[1]}":
                raise ValueError
        raw_nested_event = payload.get("guild_scheduled_event")
        if raw_nested_event is not None:
            if not isinstance(raw_nested_event, dict) or event_ref is None:
                raise ValueError
            nested_event_ref = (
                validate_snowflake(raw_nested_event["id"]),
                normalize_domain(str(raw_nested_event["origin_domain"])),
            )
            nested_guild_ref = (
                validate_snowflake(raw_nested_event["guild_id"]),
                normalize_domain(str(raw_nested_event["guild_domain"])),
            )
            if nested_event_ref != event_ref or nested_guild_ref != expected_guild:
                raise ValueError
        if raw_nested_event is not None and event_ref is None:
            raise ValueError
        if shape == "resolve" and (event_ref is None) != (raw_nested_event is None):
            raise ValueError
        if shape == "managed":
            uses = payload.get("uses")
            max_uses = payload.get("max_uses")
            if (
                type(uses) is not int
                or uses < 0
                or (
                    max_uses is not None
                    and (type(max_uses) is not int or not 1 <= max_uses <= 100 or uses > max_uses)
                )
                or type(payload.get("temporary")) is not bool
                or type(payload.get("reusable")) is not bool
            ):
                raise ValueError
            for field in ("expires_at", "created_at", "revoked_at"):
                raw_timestamp = payload.get(field)
                if raw_timestamp is None and field != "created_at":
                    continue
                if not isinstance(raw_timestamp, str):
                    raise ValueError
                parsed_timestamp = datetime.fromisoformat(raw_timestamp)
                if parsed_timestamp.tzinfo is None:
                    raise ValueError
    except (FederationNetworkError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
        ) from None
    return cast(dict[str, object], payload)


def validated_federated_invite_resolution(
    payload: object,
    *,
    expected_code: str,
    expected_authority: str,
) -> dict[str, object]:
    """Validate a public invite preview against its exact code and authority."""

    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("guild"), dict):
            raise ValueError
        raw_guild = cast(dict[str, object], payload["guild"])
        guild_ref = (
            validate_snowflake(raw_guild.get("id")),
            normalize_domain(str(raw_guild.get("origin_domain", ""))),
        )
        if guild_ref[1] != expected_authority:
            raise ValueError
        return validated_federated_invite_payload(
            payload,
            expected_guild=guild_ref,
            expected_code=expected_code,
            shape="resolve",
        )
    except HTTPException:
        raise
    except (FederationNetworkError, TypeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
        ) from None


def validated_federated_join_payload(
    payload: object,
    *,
    expected_guild: tuple[int, str],
) -> dict[str, object]:
    """Bind a join acknowledgement to the requested guild and one exact shape."""

    try:
        if not isinstance(payload, dict) or set(payload) != {"guild", "snapshot_seq"}:
            raise ValueError
        validated_federated_guild_payload(
            payload.get("guild"),
            expected_guild=expected_guild,
        )
        raw_snapshot_seq = payload.get("snapshot_seq")
        if (
            not isinstance(raw_snapshot_seq, str)
            or str(validate_snowflake(raw_snapshot_seq)) != raw_snapshot_seq
        ):
            raise ValueError
    except (FederationNetworkError, TypeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATION_GUILD_JOIN_FAILED"},
        ) from None
    return cast(dict[str, object], payload)


async def require_invite_revoke_access(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    invite: Invite,
) -> None:
    """Allow guild managers or managers of the invite's exact channel."""

    await require_bot_invite_channel_access(
        session,
        redis,
        guild,
        actor,
        invite,
    )
    guild_permissions = await get_permissions(session, redis, guild, actor)
    if guild_permissions & Permission.MANAGE_GUILD:
        return
    channel = (
        await session.get(Channel, (invite.channel_id, invite.channel_domain))
        if invite.channel_id is not None and invite.channel_domain is not None
        else None
    )
    if channel is None or (channel.guild_id, channel.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    channel_permissions = await get_permissions(
        session,
        redis,
        guild,
        actor,
        channel=channel,
    )
    if not channel_permissions & Permission.MANAGE_CHANNELS:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})


async def require_bot_invite_channel_access(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    invite: Invite,
    *,
    raise_on_denied: bool = True,
) -> bool:
    """Keep every channel reference in an invite inside a bot's live grant."""

    if getattr(actor, "account_type", "human") != "bot":
        return True

    channel_refs: set[tuple[int, str]] = set()
    valid = (invite.channel_id is None) == (invite.channel_domain is None)
    if invite.channel_id is not None and invite.channel_domain is not None:
        channel_refs.add((invite.channel_id, invite.channel_domain))

    event = await active_scheduled_event_for_invite(session, invite)
    if event is not None:
        valid = valid and (event.channel_id is None) == (event.channel_domain is None)
        if event.channel_id is not None and event.channel_domain is not None:
            channel_refs.add((event.channel_id, event.channel_domain))

    for channel_ref in sorted(channel_refs, key=lambda item: (item[1], item[0])):
        channel = await session.get(Channel, channel_ref)
        if (
            channel is None
            or channel.unavailable
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        ):
            valid = False
            break
        permissions = await get_permissions(session, redis, guild, actor, channel=channel)
        if permissions & Permission.VIEW_CHANNEL != Permission.VIEW_CHANNEL:
            valid = False
            break

    if valid:
        return True
    if raise_on_denied:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_CHANNEL_RESTRICTED"},
        )
    return False


async def _invite_channel_and_guild(
    session: AsyncSession,
    settings: Settings,
    channel_ref: EntityRef,
) -> tuple[Channel, Guild]:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel = await session.get(Channel, (channel_id, channel_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.guild_id is None
        or channel.guild_domain is None
        or channel.origin_domain != channel.guild_domain
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    return channel, guild


async def _active_invite_payloads(
    session: AsyncSession,
    guild: Guild,
    *,
    channel: Channel | None = None,
    include_metadata: bool = True,
    viewer: tuple[Redis, User] | None = None,
) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    statement = select(Invite).where(
        Invite.guild_id == guild.id,
        Invite.guild_domain == guild.origin_domain,
        Invite.revoked_at.is_(None),
        or_(Invite.expires_at.is_(None), Invite.expires_at > now),
        or_(Invite.max_uses.is_(None), Invite.uses < Invite.max_uses),
    )
    if channel is not None:
        statement = statement.where(
            Invite.channel_id == channel.id,
            Invite.channel_domain == channel.origin_domain,
        )
    invites = list(await session.scalars(statement.order_by(Invite.created_at.desc(), Invite.code)))
    rendered: list[dict[str, object]] = []
    for invite in invites:
        if viewer is not None and not await require_bot_invite_channel_access(
            session,
            viewer[0],
            guild,
            viewer[1],
            invite,
            raise_on_denied=False,
        ):
            continue
        event_payload = await scheduled_event_invite_payload(session, invite)
        if invite.scheduled_event_id is not None and event_payload is None:
            continue
        rendered.append(
            invite_payload(
                invite,
                guild,
                guild_scheduled_event=event_payload,
                include_metadata=include_metadata,
            )
        )
    return rendered


async def get_managed_invite(
    guild_ref: EntityRef,
    code: str,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> dict[str, object]:
    """Fetch one active invite within an installed guild's authority boundary."""

    expected_guild = guild_ref.resolve(settings.domain)
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        auth.user,
        "invite.get",
        {"code": code},
    )
    if proxied is not None:
        return validated_federated_invite_payload(
            guild_management_dict_body(proxied, 200),
            expected_guild=expected_guild,
            expected_code=code,
        )

    guild = await local_guild(session, settings, guild_ref)
    now = datetime.now(UTC)
    invite = await session.scalar(
        select(Invite).where(
            Invite.code == code,
            Invite.guild_id == guild.id,
            Invite.guild_domain == guild.origin_domain,
            Invite.revoked_at.is_(None),
            or_(Invite.expires_at.is_(None), Invite.expires_at > now),
            or_(Invite.max_uses.is_(None), Invite.uses < Invite.max_uses),
        )
    )
    if invite is None or not await require_bot_invite_channel_access(
        session,
        redis,
        guild,
        auth.user,
        invite,
        raise_on_denied=False,
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    event_payload = await scheduled_event_invite_payload(session, invite)
    if invite.scheduled_event_id is not None and event_payload is None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    return invite_payload(
        invite,
        guild,
        guild_scheduled_event=event_payload,
        include_metadata=True,
    )


@router.post("/guilds/{guild_id}/invites")
async def create_invite(
    guild_id: EntityRef,
    payload: InviteCreate,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["invite_create"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    normalized_reason = normalize_audit_reason(reason)
    _, guild_domain = guild_id.resolve(settings.domain)
    remote_data = payload.model_dump(mode="json")
    if payload.channel_id is not None:
        remote_data["channel_id"] = qualified_management_ref(payload.channel_id, guild_domain)
    if payload.target_user_id is not None:
        remote_data["target_user_id"] = qualified_management_ref(
            payload.target_user_id,
            settings.domain,
        )
    if payload.scheduled_event_id is not None:
        remote_data["scheduled_event_id"] = qualified_management_ref(
            payload.scheduled_event_id,
            guild_domain,
        )
    remote_data["role_ids"] = [
        qualified_management_ref(role_ref, guild_domain) for role_ref in payload.role_ids
    ]
    remote_data["target_user_ids"] = [
        qualified_management_ref(user_ref, settings.domain) for user_ref in payload.target_user_ids
    ]
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "invite.create",
        {"data": remote_data, "reason": normalized_reason},
    )
    if proxied is not None:
        expected_guild = guild_id.resolve(settings.domain)
        expected_channel_id = (
            payload.channel_id.resolve(expected_guild[1])[0]
            if payload.channel_id is not None
            else None
        )
        return validated_federated_invite_payload(
            guild_management_dict_body(proxied, 201),
            expected_guild=expected_guild,
            expected_channel_id=expected_channel_id,
            validate_channel=True,
        )

    guild = await local_guild(session, settings, guild_id, for_update=True)
    if payload.channel_id is None:
        channel_id = None
        channel_domain = None
    else:
        channel_id, channel_domain = payload.channel_id.resolve(guild.origin_domain)
        if channel_domain != guild.origin_domain:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    channel: Channel | None = None
    if channel_id is not None:
        channel = await session.scalar(
            select(Channel).where(
                Channel.id == channel_id,
                Channel.origin_domain == channel_domain,
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
            )
        )
        if channel is None:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            required_permissions("invite.create"),
            channel=channel,
        )
    else:
        await require_permissions(
            session, redis, guild, auth.user, required_permissions("invite.create")
        )
    target_user_ref, scheduled_event_ref = await validate_invite_targets(
        session,
        redis,
        settings,
        guild,
        channel,
        payload,
    )
    role_ids, target_user_ids = await validate_community_invite(
        session,
        redis,
        settings,
        guild,
        auth.user,
        payload,
    )
    scheduled_event = (
        await active_scheduled_event_by_ref(session, guild, payload.scheduled_event_id)
        if payload.scheduled_event_id is not None
        else None
    )
    if payload.scheduled_event_id is not None and scheduled_event is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVITE_TARGET_EVENT_INVALID"},
        )
    if scheduled_event is not None:
        await require_scheduled_event_view(
            session,
            redis,
            guild,
            auth.user,
            scheduled_event,
        )
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=payload.max_age_seconds)
        if payload.max_age_seconds is not None
        else None
    )
    if not payload.unique:
        reusable_invite = await session.scalar(
            select(Invite)
            .where(
                Invite.guild_id == guild.id,
                Invite.guild_domain == guild.origin_domain,
                Invite.channel_id == channel_id,
                Invite.channel_domain == channel_domain,
                Invite.inviter_id == auth.user.id,
                Invite.inviter_domain == auth.user.origin_domain,
                Invite.max_uses == payload.max_uses,
                Invite.temporary == payload.temporary,
                Invite.reusable.is_(True),
                Invite.target_type == payload.target_type,
                Invite.target_user_id == target_user_ref[0],
                Invite.target_user_domain == target_user_ref[1],
                Invite.scheduled_event_id == scheduled_event_ref[0],
                Invite.scheduled_event_domain == scheduled_event_ref[1],
                Invite.role_ids == role_ids,
                Invite.target_user_ids == target_user_ids,
                Invite.revoked_at.is_(None),
                or_(Invite.max_uses.is_(None), Invite.uses < Invite.max_uses),
                (
                    Invite.expires_at.is_(None)
                    if expires_at is None
                    else Invite.expires_at > datetime.now(UTC)
                ),
            )
            .order_by(Invite.created_at.desc())
            .limit(1)
        )
        if reusable_invite is not None:
            return invite_payload(
                reusable_invite,
                guild,
                guild_scheduled_event=await scheduled_event_invite_payload(
                    session, reusable_invite
                ),
            )

    invite = Invite(
        code=await new_invite_code(session),
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        channel_id=channel_id,
        channel_domain=channel_domain,
        inviter_id=auth.user.id,
        inviter_domain=auth.user.origin_domain,
        max_uses=payload.max_uses,
        temporary=payload.temporary,
        reusable=not payload.unique,
        target_type=payload.target_type,
        target_user_id=target_user_ref[0],
        target_user_domain=target_user_ref[1],
        scheduled_event_id=scheduled_event_ref[0],
        scheduled_event_domain=scheduled_event_ref[1],
        role_ids=role_ids,
        target_user_ids=target_user_ids,
        expires_at=expires_at,
    )
    session.add(invite)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        40,
        target_type="invite",
        target_ref={"code": invite.code},
        reason=normalized_reason,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    rendered = invite_payload(
        invite,
        guild,
        guild_scheduled_event=(
            await scheduled_event_invite_payload(session, invite)
            if scheduled_event is not None
            else None
        ),
    )
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "INVITE_CREATE",
        rendered,
    )
    return rendered


@router.get("/guilds/{guild_id}/invites")
async def list_invites(
    guild_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "invite.list",
        {},
    )
    if proxied is not None:
        expected_guild = guild_id.resolve(settings.domain)
        return [
            validated_federated_invite_payload(item, expected_guild=expected_guild)
            for item in guild_management_list_body(proxied, 200)
        ]

    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("guild.invite.list"),
    )
    return await _active_invite_payloads(
        session,
        guild,
        include_metadata=True,
        viewer=(redis, auth.user),
    )


@router.get("/channels/{channel_ref}/invites")
async def list_channel_invites(
    channel_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    channel, guild = await _invite_channel_and_guild(session, settings, channel_ref)
    guild_ref = EntityRef(f"{guild.id}@{guild.origin_domain}")
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        auth.user,
        "invite.list_channel",
        {"channel_ref": f"{channel.id}@{channel.origin_domain}"},
    )
    if proxied is not None:
        expected_guild = (guild.id, guild.origin_domain)
        return [
            validated_federated_invite_payload(
                item,
                expected_guild=expected_guild,
                expected_channel_id=channel.id,
                validate_channel=True,
            )
            for item in guild_management_list_body(proxied, 200)
        ]

    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("channel.invite.list"),
        channel=channel,
    )
    return await _active_invite_payloads(
        session,
        guild,
        channel=channel,
        viewer=(redis, auth.user),
    )


@router.delete("/invites/{code}")
async def revoke_invite(
    code: str,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    bare_code, code_authority = parse_invite_management_code(code)
    normalized_reason = normalize_audit_reason(reason)
    expected_guild: tuple[int, str] | None = None
    if guild_ref is not None:
        expected_guild = guild_ref.resolve(settings.domain)
    if code_authority is not None:
        if guild_ref is None or expected_guild is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVITE_GUILD_REFERENCE_REQUIRED"},
            )
        if expected_guild[1] != code_authority:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVITE_AUTHORITY_MISMATCH"},
            )
    if guild_ref is not None:
        proxied = await proxy_remote_guild_management(
            session,
            settings,
            guild_ref,
            auth.user,
            "invite.revoke",
            {"code": bare_code, "reason": normalized_reason},
        )
        if proxied is not None:
            if expected_guild is None:
                raise RuntimeError("proxied invite revoke is missing its guild binding")
            return validated_federated_invite_payload(
                guild_management_dict_body(proxied, 200),
                expected_guild=expected_guild,
                expected_code=bare_code,
            )

    invite = await session.scalar(select(Invite).where(Invite.code == bare_code).with_for_update())
    if invite is None or invite.revoked_at is not None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    if expected_guild is not None and expected_guild != (
        invite.guild_id,
        invite.guild_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    # Invite acceptance also takes locks in invite -> guild order. Reuse that
    # order here so revoke and accept cannot deadlock each other.
    guild = await local_guild(
        session,
        settings,
        EntityRef(f"{invite.guild_id}@{invite.guild_domain}"),
        for_update=True,
    )
    await require_invite_revoke_access(session, redis, guild, auth.user, invite)
    invite.revoked_at = datetime.now(UTC)
    rendered = invite_payload(
        invite,
        guild,
        guild_scheduled_event=await scheduled_event_invite_payload(session, invite),
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        42,
        target_type="invite",
        target_ref={"code": invite.code},
        reason=normalized_reason,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "INVITE_DELETE",
        rendered,
    )
    return rendered


async def local_invite_for_target_users(
    code: str,
    guild_ref: EntityRef | EntityReference | None,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    allow_audit_log: bool,
    for_update: bool = False,
) -> tuple[Invite, Guild]:
    statement = select(Invite).where(Invite.code == code, Invite.revoked_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    invite = await session.scalar(statement)
    if invite is None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    expected_guild = guild_ref.resolve(settings.domain) if guild_ref is not None else None
    if expected_guild is not None and expected_guild != (
        invite.guild_id,
        invite.guild_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = await local_guild(
        session,
        settings,
        EntityRef(f"{invite.guild_id}@{invite.guild_domain}"),
        for_update=for_update,
    )
    await require_bot_invite_channel_access(
        session,
        redis,
        guild,
        auth.user,
        invite,
    )
    if (invite.inviter_id, invite.inviter_domain) == (
        auth.user.id,
        auth.user.origin_domain,
    ):
        return invite, guild
    permissions = await get_permissions(session, redis, guild, auth.user)
    allowed = Permission.MANAGE_GUILD
    if allow_audit_log:
        allowed |= Permission.VIEW_AUDIT_LOG
    if not permissions & allowed:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    return invite, guild


async def local_get_invite_target_users(
    code: str,
    guild_ref: EntityRef | EntityReference | None,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> dict[str, object]:
    invite, _guild = await local_invite_for_target_users(
        code,
        guild_ref,
        auth,
        session,
        redis,
        settings,
        allow_audit_log=True,
    )
    return {"target_user_ids": list(invite.target_user_ids)}


def invite_target_users_job_status(invite: Invite) -> dict[str, object]:
    completed_at = invite.updated_at or invite.created_at
    return {
        "status": 2,
        "total_users": len(invite.target_user_ids),
        "processed_users": len(invite.target_user_ids),
        "created_at": completed_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "error_message": None,
    }


async def local_get_invite_target_users_job_status(
    code: str,
    guild_ref: EntityRef | EntityReference | None,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> dict[str, object]:
    invite, _guild = await local_invite_for_target_users(
        code,
        guild_ref,
        auth,
        session,
        redis,
        settings,
        allow_audit_log=True,
    )
    return invite_target_users_job_status(invite)


async def local_update_invite_target_users(
    code: str,
    target_user_ids: list[str],
    guild_ref: EntityRef | EntityReference | None,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    reason: str | None = None,
) -> dict[str, object]:
    normalized = normalize_target_user_refs(target_user_ids, settings.domain)
    invite, guild = await local_invite_for_target_users(
        code,
        guild_ref,
        auth,
        session,
        redis,
        settings,
        allow_audit_log=False,
        for_update=True,
    )
    if invite.target_user_ids != normalized:
        previous_count = len(invite.target_user_ids)
        invite.target_user_ids = normalized
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            41,
            target_type="invite",
            target_ref={"code": invite.code},
            reason=reason,
            changes=[
                {
                    "key": "target_user_count",
                    "old_value": previous_count,
                    "new_value": len(normalized),
                }
            ],
        )
        await session.commit()
        await session.refresh(invite)
        await wake_queued_guild_federation(guild)
    else:
        await session.commit()
    return invite_target_users_job_status(invite)


def invite_management_scope(
    code: str,
    guild_ref: EntityRef | None,
    settings: Settings,
) -> tuple[str, str | None]:
    bare_code, code_authority = parse_invite_management_code(code)
    if guild_ref is None:
        if code_authority is None:
            return bare_code, None
        raise HTTPException(
            status_code=400,
            detail={"code": "INVITE_GUILD_REFERENCE_REQUIRED"},
        )
    guild_authority = guild_ref.resolve(settings.domain)[1]
    if code_authority is not None and guild_authority != code_authority:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVITE_AUTHORITY_MISMATCH"},
        )
    # An explicit guild reference is sufficient to route a bare Discord-style
    # code. Requiring a second authority suffix here made remote management
    # fall through to a same-snowflake local replica lookup.
    return bare_code, guild_authority


@router.get("/invites/{code}/target-users")
async def get_invite_target_users(
    code: str,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    bare_code, code_authority = invite_management_scope(code, guild_ref, settings)
    if code_authority is not None:
        remote_guild_ref = cast(EntityRef, guild_ref)
        proxied = await proxy_remote_guild_management(
            session,
            settings,
            remote_guild_ref,
            auth.user,
            "invite.target_users.get",
            {"code": bare_code},
        )
        if proxied is not None:
            body = guild_management_dict_body(proxied, 200)
            target_user_ids = validated_federated_target_user_refs(
                body.get("target_user_ids"), settings.domain
            )
            return render_target_users_csv(target_user_ids, bare_code)
    body = await local_get_invite_target_users(bare_code, guild_ref, auth, session, redis, settings)
    return render_target_users_csv(cast(list[str], body["target_user_ids"]), bare_code)


@router.put("/invites/{code}/target-users")
async def update_invite_target_users(
    code: str,
    request: Request,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    raw = await request.body()
    target_user_ids = parse_target_users_upload(
        raw,
        request.headers.get("Content-Type", "text/csv"),
        settings.domain,
    )
    bare_code, code_authority = invite_management_scope(code, guild_ref, settings)
    if code_authority is not None:
        remote_guild_ref = cast(EntityRef, guild_ref)
        proxied = await proxy_remote_guild_management(
            session,
            settings,
            remote_guild_ref,
            auth.user,
            "invite.target_users.update",
            {
                "code": bare_code,
                "target_user_ids": target_user_ids,
                "reason": reason,
            },
        )
        if proxied is not None:
            return validated_target_users_job_status(guild_management_dict_body(proxied, 200))
    return await local_update_invite_target_users(
        bare_code,
        target_user_ids,
        guild_ref,
        auth,
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/invites/{code}/target-users/job-status")
async def get_invite_target_users_job_status(
    code: str,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    bare_code, code_authority = invite_management_scope(code, guild_ref, settings)
    if code_authority is not None:
        remote_guild_ref = cast(EntityRef, guild_ref)
        proxied = await proxy_remote_guild_management(
            session,
            settings,
            remote_guild_ref,
            auth.user,
            "invite.target_users.status",
            {"code": bare_code},
        )
        if proxied is not None:
            return validated_target_users_job_status(guild_management_dict_body(proxied, 200))
    return await local_get_invite_target_users_job_status(
        bare_code, guild_ref, auth, session, redis, settings
    )


@router.get("/invites/{code}")
async def get_invite(
    code: str,
    request: Request,
    response: Response,
    auth: AuthenticatedUser | None = Depends(optional_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if len(code) > 320:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    source_ip = resolve_client_ip(
        supplied_secret=request.headers.get("X-Kaede-Proxy-Secret"),
        configured_secret=(
            settings.proxy_secret.get_secret_value() if settings.proxy_secret is not None else None
        ),
        forwarded_for=request.headers.get("X-Forwarded-For"),
        direct_host=request.client.host if request.client is not None else None,
    )
    await enforce_keyed_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["invite_preview"],
        identity=source_ip,
    )
    viewer = auth if isinstance(auth, AuthenticatedUser) else None
    remote_code, separator, raw_domain = code.rpartition("@")
    if separator:
        try:
            domain = normalize_domain(raw_domain)
        except FederationNetworkError:
            raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"}) from None
        if domain == settings.domain:
            code = remote_code
        else:
            await enforce_keyed_rate_limit(
                redis,
                response,
                CLIENT_RATE_LIMITS["invite_preview_destination"],
                identity=domain,
            )
            await enforce_keyed_rate_limit(
                redis,
                response,
                CLIENT_RATE_LIMITS["invite_preview_global"],
                identity="outbound",
            )
            try:
                async with asyncio.timeout(0.1):
                    await remote_invite_preview_slots.acquire()
            except TimeoutError:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "FEDERATION_INVITE_PREVIEW_BUSY"},
                    headers={"Retry-After": "1"},
                ) from None
            try:
                try:
                    resolve_payload: dict[str, str] = {"code": remote_code}
                    if viewer is not None and viewer.user.origin_domain == settings.domain:
                        resolve_payload["viewer_id"] = str(viewer.user.id)
                    resolved = await signed_request(
                        session,
                        settings,
                        "POST",
                        domain,
                        "/_kaede/v1/invites/resolve",
                        payload=resolve_payload,
                    )
                except FederationInstanceQuotaExceeded as exc:
                    raise HTTPException(
                        status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                        detail=exc.detail(),
                    ) from exc
            finally:
                remote_invite_preview_slots.release()
            if resolved.status_code == 404:
                raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
            if resolved.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATION_INVITE_RESOLVE_FAILED"},
                )
            try:
                payload = validated_federated_invite_resolution(
                    decode_federation_response_json(resolved),
                    expected_code=remote_code,
                    expected_authority=domain,
                )
            except (FederationNetworkError, HTTPException, TypeError, ValueError):
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATION_INVITE_RESOLVE_FAILED"},
                ) from None
            return {**payload, "code": code, "origin_domain": domain}
    invite = await session.get(Invite, code)
    now = datetime.now(UTC)
    if (
        invite is None
        or invite.revoked_at is not None
        or (invite.expires_at is not None and invite.expires_at <= now)
        or (invite.max_uses is not None and invite.uses >= invite.max_uses)
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    if invite.target_user_ids and (
        viewer is None or not invite_allows_user(invite, viewer.user.id, viewer.user.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = await session.scalar(
        select(Guild).where(Guild.id == invite.guild_id, Guild.origin_domain == invite.guild_domain)
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    event_payload = await scheduled_event_invite_payload(session, invite)
    if invite.scheduled_event_id is not None and event_payload is None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    return invite_payload(invite, guild, guild_scheduled_event=event_payload)


@router.post("/invites/{code}")
async def accept_invite(
    code: str,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    automod_post_commit = AutoModPostCommit()
    if len(code) > 320:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["invite_accept"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    remote_code, separator, raw_domain = code.rpartition("@")
    if separator:
        try:
            domain = normalize_domain(raw_domain)
        except FederationNetworkError:
            raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"}) from None
        if domain == settings.domain:
            code = remote_code
        else:
            try:
                resolved = await signed_request(
                    session,
                    settings,
                    "POST",
                    domain,
                    "/_kaede/v1/invites/resolve",
                    payload={"code": remote_code, "viewer_id": str(auth.user.id)},
                )
            except FederationInstanceQuotaExceeded as exc:
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail=exc.detail(),
                ) from exc
            if resolved.status_code == 404:
                raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
            if resolved.status_code != 200:
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_INVITE_RESOLVE_FAILED"}
                )
            try:
                resolved_payload = validated_federated_invite_resolution(
                    decode_federation_response_json(resolved),
                    expected_code=remote_code,
                    expected_authority=domain,
                )
                resolved_guild = cast(dict[str, object], resolved_payload["guild"])
                resolved_guild_id = validate_snowflake(resolved_guild["id"])
            except (FederationNetworkError, HTTPException, KeyError, TypeError, ValueError):
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_INVITE_RESOLVE_FAILED"}
                ) from None
            join_intent_started = await begin_remote_guild_join(
                session,
                settings,
                guild_id=resolved_guild_id,
                guild_domain=domain,
                user_id=auth.user.id,
                user_domain=auth.user.origin_domain,
            )
            if join_intent_started:
                # Make the explicit local intent visible before the remote
                # authority can emit an add event. It remains as a fail-closed
                # pending marker if the network request or snapshot fails.
                await session.commit()
            try:
                joined = await signed_request(
                    session,
                    settings,
                    "POST",
                    domain,
                    f"/_kaede/v1/guilds/{resolved_guild_id}/join",
                    payload={"code": remote_code, "user": profile_from_user(auth.user)},
                )
            except FederationInstanceQuotaExceeded as exc:
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail=exc.detail(),
                ) from exc
            if joined.status_code in {403, 404}:
                try:
                    error_body = decode_federation_response_json(joined)
                except FederationNetworkError:
                    error_body = None
                detail = parse_upstream_error(error_body, "INVITE_NOT_FOUND")
                raise HTTPException(status_code=joined.status_code, detail=detail)
            if joined.status_code == status.HTTP_507_INSUFFICIENT_STORAGE:
                try:
                    error_body = decode_federation_response_json(joined)
                except FederationNetworkError:
                    error_body = None
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail=parse_upstream_error(error_body, "FEDERATION_GUILD_JOIN_FAILED"),
                )
            if joined.status_code != 200:
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_GUILD_JOIN_FAILED"}
                )
            try:
                joined_payload = validated_federated_join_payload(
                    decode_federation_response_json(joined),
                    expected_guild=(resolved_guild_id, domain),
                )
                joined_guild = cast(dict[str, object], joined_payload["guild"])
                guild_id = validate_snowflake(joined_guild["id"])
                joined_snapshot_seq = validate_snowflake(joined_payload["snapshot_seq"])
            except (FederationNetworkError, HTTPException, KeyError, TypeError, ValueError):
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_GUILD_JOIN_FAILED"}
                ) from None
            if guild_id != resolved_guild_id:
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_GUILD_JOIN_FAILED"}
                )
            try:
                async with asyncio.timeout(45):
                    snapshot = await fetch_guild_snapshot(session, settings, domain, guild_id)
                    if validate_snowflake(snapshot.get("snapshot_seq")) < joined_snapshot_seq:
                        raise ValueError("joined guild snapshot predates the join acknowledgement")
                    guild = await apply_guild_snapshot(
                        session,
                        settings,
                        snapshot,
                        expected_origin=domain,
                        expected_guild_id=guild_id,
                        required_member=(auth.user.id, auth.user.origin_domain),
                    )
            except TimeoutError:
                raise HTTPException(
                    status_code=504, detail={"code": "FEDERATION_GUILD_JOIN_TIMEOUT"}
                ) from None
            except FederationReplicaQuotaExceeded as exc:
                # Snapshot application is atomic. Roll back its over-limit
                # rows, then preserve a clear pause marker when this was a
                # refresh of an existing replica. A first-time join has no
                # replica left after rollback, but still receives the precise
                # operator-actionable error instead of a generic 502.
                await session.rollback()
                paused_existing_replica = await mark_replica_quota_paused(
                    session,
                    settings,
                    guild_id,
                    domain,
                    exc,
                )
                await session.commit()
                if paused_existing_replica:
                    await publish_existing_replica_status(
                        session,
                        redis,
                        guild_id,
                        domain,
                    )
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail={"code": REPLICA_QUOTA_ERROR_CODE},
                ) from None
            except (FederationIdentityQuotaExceeded, FederationInstanceQuotaExceeded) as exc:
                await session.rollback()
                paused_existing_replica = await mark_replica_capacity_paused(
                    session,
                    settings,
                    guild_id,
                    domain,
                    error_code=exc.code,
                    internal_error=str(exc),
                )
                await session.commit()
                if paused_existing_replica:
                    await publish_existing_replica_status(
                        session,
                        redis,
                        guild_id,
                        domain,
                    )
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail=exc.detail(),
                ) from None
            except (ValueError, RuntimeError):
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_SNAPSHOT_FAILED"}
                ) from None
            await session.commit()
            # Snapshot application performs bulk upserts, and PostgreSQL-managed
            # timestamps can be expired at commit.  Refresh before handing this
            # object to synchronous payload/topic helpers.
            await session.refresh(guild)
            await wake_queued_guild_federation(guild)
            from app.tasks import federation_history_sync

            await enqueue_best_effort(
                federation_history_sync,
                guild.id,
                guild.origin_domain,
                auth.user.id,
            )
            result = guild_payload(guild)
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "GUILD_CREATE",
                result,
            )
            await publish_dispatch(
                redis,
                user_topic(auth.user.origin_domain, auth.user.id),
                "GUILD_CREATE",
                result,
            )
            return result
    invite = await session.scalar(select(Invite).where(Invite.code == code).with_for_update())
    now = datetime.now(UTC)
    if (
        invite is None
        or invite.revoked_at is not None
        or (invite.expires_at is not None and invite.expires_at <= now)
        or (invite.max_uses is not None and invite.uses >= invite.max_uses)
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    if not invite_allows_user(invite, auth.user.id, auth.user.origin_domain):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    if (
        invite.scheduled_event_id is not None
        and await active_scheduled_event_for_invite(session, invite) is None
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = await local_guild(
        session,
        settings,
        EntityRef(f"{invite.guild_id}@{invite.guild_domain}"),
    )
    locked_guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked_guild is None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = locked_guild
    banned = await session.scalar(
        select(Ban).where(
            Ban.guild_id == guild.id,
            Ban.guild_domain == guild.origin_domain,
            Ban.user_id == auth.user.id,
            Ban.user_domain == auth.user.origin_domain,
            or_(Ban.expires_at.is_(None), Ban.expires_at > now),
        )
    )
    if banned is not None:
        raise HTTPException(status_code=403, detail={"code": "BANNED_FROM_GUILD"})
    instance_banned = await session.scalar(
        select(GuildInstanceBan.instance_domain).where(
            GuildInstanceBan.guild_id == guild.id,
            GuildInstanceBan.guild_domain == guild.origin_domain,
            GuildInstanceBan.instance_domain == auth.user.origin_domain,
            or_(
                GuildInstanceBan.expires_at.is_(None),
                GuildInstanceBan.expires_at > now,
            ),
        )
    )
    if instance_banned is not None:
        raise HTTPException(status_code=403, detail={"code": "INSTANCE_BANNED_FROM_GUILD"})
    member = await session.scalar(
        select(GuildMember).where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_id == auth.user.id,
            GuildMember.user_domain == auth.user.origin_domain,
        )
    )
    if member is None:
        member = GuildMember(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            joined_at=now,
            temporary=invite.temporary,
        )
        session.add(member)
        granted_roles, _newly_granted = await grant_invite_roles(session, guild, member, invite)
        e2ee_policy_channels: list[Channel] = []
        invite.uses += 1
        owner = await guild_authority_owner(session, settings, guild)
        await queue_guild_mutation(
            session,
            settings,
            guild,
            owner,
            "guild.member.add",
            {
                "user": profile_from_user(auth.user),
                "joined_at": now.isoformat(),
                "temporary": member.temporary,
                "role_ids": [
                    {"id": str(role.id), "origin_domain": role.origin_domain}
                    for role in granted_roles
                ],
            },
            e2ee_policy_channels=e2ee_policy_channels,
        )
        automod_post_commit = await evaluate_member_profile(
            session,
            settings,
            snowflake,
            guild,
            auth.user,
        )
        await session.commit()
        # Guild sequence assignment updates the server-managed resource
        # version; reload it before synchronous dispatch serialization.
        await session.refresh(guild)
        await wake_queued_guild_federation(guild)
        await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_ADD",
            {
                "guild_id": str(guild.id),
                "user": user_payload(auth.user),
                "role_ids": [str(role.id) for role in granted_roles],
            },
        )
        await publish_dispatch(
            redis,
            user_topic(auth.user.origin_domain, auth.user.id),
            "GUILD_CREATE",
            guild_payload(guild),
        )
        await automod_post_commit.publish(redis)
    else:
        _granted_roles, newly_granted = await grant_invite_roles(session, guild, member, invite)
        if newly_granted:
            e2ee_policy_channels = []
            invite.uses += 1
            owner = await guild_authority_owner(session, settings, guild)
            for role in newly_granted:
                member.member_version += 1
                await queue_guild_mutation(
                    session,
                    settings,
                    guild,
                    owner,
                    "guild.member.role.add",
                    {
                        "user": {
                            "id": str(member.user_id),
                            "origin_domain": member.user_domain,
                        },
                        "role": {
                            "id": str(role.id),
                            "origin_domain": role.origin_domain,
                        },
                        "member_version": str(member.member_version),
                    },
                    snapshot_required=True,
                    e2ee_policy_channels=e2ee_policy_channels,
                )
            role_ids = list(
                await session.scalars(
                    select(MemberRole.role_id).where(
                        MemberRole.guild_id == guild.id,
                        MemberRole.guild_domain == guild.origin_domain,
                        MemberRole.user_id == member.user_id,
                        MemberRole.user_domain == member.user_domain,
                    )
                )
            )
            rendered_member = member_payload(member, auth.user, role_ids)
            await session.commit()
            await session.refresh(guild)
            await wake_queued_guild_federation(guild)
            await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "GUILD_MEMBER_UPDATE",
                rendered_member,
            )
        else:
            # expire_on_commit=False keeps the locked Guild usable for the
            # idempotent response. AsyncSession.rollback() would expire it and
            # make the synchronous serializer attempt forbidden implicit I/O.
            await session.commit()
    return guild_payload(guild)
