from __future__ import annotations

from typing import cast

from fastapi import HTTPException
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import cast as sql_cast
from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from app.api.dependencies import AuthenticatedUser
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef, EntityReferenceLike
from app.db.models import Attachment, TrackerTask
from app.federation.guild_management import GuildManagementOperation
from app.media.schemas import UploadTicketRequest
from app.media.service import (
    attachment_payload,
    create_upload_ticket,
    finalize_attachment,
    ticket_payload,
)
from app.tracker.service import proxy_remote_tracker_mutation, tracker_context


def task_references_attachment(attachment: Attachment | type[Attachment]) -> Exists:
    return exists().where(
        TrackerTask.channel_id == attachment.upload_channel_id,
        TrackerTask.channel_domain == attachment.upload_channel_domain,
        func.jsonb_path_exists(
            TrackerTask.custom_values,
            sql_cast("$.*[*] ? (@.id == $ref)", JSONPATH),
            func.jsonb_build_object(
                "ref", func.concat(attachment.id, "@", attachment.origin_domain)
            ),
        ),
    )


def file_value(attachment: Attachment) -> dict[str, object]:
    mime = attachment.detected_content_type or attachment.content_type
    return {
        "id": f"{attachment.id}@{attachment.origin_domain}",
        "name": attachment.filename,
        "type": "image"
        if mime.startswith("image/")
        else "video"
        if mime.startswith("video/")
        else "file",
    }


async def tracker_media(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    auth: AuthenticatedUser,
    channel_ref: EntityReferenceLike,
    action: str,
    data: dict[str, object],
) -> dict[str, object]:
    operation = cast(GuildManagementOperation, f"tracker.attachment.{action}")
    proxied, body = await proxy_remote_tracker_mutation(
        session,
        settings,
        auth,
        channel_ref,
        operation,
        {"data": data},
    )
    if proxied:
        return body
    context = await tracker_context(
        session,
        redis,
        settings,
        auth,
        channel_ref,
        mutation=action != "read",
        needed=required_permissions("tracker.read"),
    )
    if action != "read" and not context.permissions & int(
        Permission.ADMINISTRATOR
        | Permission.CREATE_TRACKER_TASKS
        | Permission.EDIT_OWN_TRACKER_TASKS
        | Permission.MANAGE_TRACKER_TASKS
    ):
        raise HTTPException(403, detail={"code": "MISSING_PERMISSIONS"})
    if action != "read":
        from app.automod.service import require_member_interactions_allowed

        if context.access.guild is None:
            raise HTTPException(404, detail={"code": "TRACKER_NOT_FOUND"})
        await require_member_interactions_allowed(
            session,
            context.access.guild,
            auth.user,
            Permission.CREATE_TRACKER_TASKS,
        )
    remote_actor = auth.user.origin_domain != settings.domain and not auth.user.is_local
    if action == "ticket":
        try:
            payload = UploadTicketRequest.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(422, detail={"code": "TRACKER_FIELD_INVALID"}) from exc
        if payload.encryption_mode != "plaintext":
            raise HTTPException(400, detail={"code": "E2EE_NOT_ENABLED"})
        ticket_attachment, url = await create_upload_ticket(
            session,
            settings,
            snowflake,
            auth.user,
            filename=payload.filename,
            content_type=payload.content_type,
            size=payload.size,
            purpose="tracker_attachment",
            federated_guild_upload=remote_actor,
        )
        ticket_attachment.upload_channel_id = context.board.channel_id
        ticket_attachment.upload_channel_domain = context.board.channel_domain
        await session.commit()
        return ticket_payload(ticket_attachment, url)
    if set(data) != {"attachment_id"} or not isinstance(data["attachment_id"], str):
        raise HTTPException(400, detail={"code": "TRACKER_FIELD_INVALID"})
    try:
        ref = EntityRef(data["attachment_id"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, detail={"code": "TRACKER_FIELD_INVALID"}) from exc
    attachment = await session.get(
        Attachment,
        ref.resolve(settings.domain),
        populate_existing=True,
        with_for_update={"read": True} if action == "read" else None,
    )
    if (
        attachment is None
        or attachment.deleted_at is not None
        or attachment.purpose != "tracker_attachment"
        or attachment.origin_domain != settings.domain
        or (attachment.upload_channel_id, attachment.upload_channel_domain)
        != (context.board.channel_id, context.board.channel_domain)
    ):
        raise HTTPException(404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    if action == "commit":
        attachment = await finalize_attachment(
            session,
            settings,
            auth.user,
            attachment.id,
            required_purpose="tracker_attachment",
            federated_guild_upload=remote_actor,
        )
        await session.commit()
        if attachment.scan_status == "pending":
            from app.core.task_wake import enqueue_best_effort
            from app.tasks import media_process

            await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        if attachment.scan_status != "clean":
            return attachment_payload(attachment)
        return file_value(attachment)
    if not await session.scalar(select(task_references_attachment(attachment))) and (
        attachment.uploader_id,
        attachment.uploader_domain,
    ) != (auth.user.id, auth.user.origin_domain):
        raise HTTPException(404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    from app.api.media import redirect_to_object

    # Return a short-lived private storage capability through the authenticated,
    # authority-qualified API; browsers and desktop can stream without buffering.
    redirect = redirect_to_object(settings, attachment, "original", public=False)
    return {"url": redirect.headers["location"]}
