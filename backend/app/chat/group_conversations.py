from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.payloads import message_payload
from app.chat.privacy import blocked_between, lock_relationship_pair, relationship
from app.core.dm import (
    GROUP_DM_MEMBER_ADDED,
    GROUP_DM_MEMBER_LEFT,
    GROUP_DM_MEMBER_REMOVED,
    MAX_GROUP_DM_PARTICIPANTS,
    group_dm_notice_text,
)
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.models import Channel, DMConversation, DMParticipant, Message, MessageProjection, User
from app.federation.dm_storage import (
    admit_federated_dm_message,
    dm_message_storage_delta,
)
from app.federation.replication import profile_from_user


def group_conversation_content(
    conversation: DMConversation,
    channel: Channel,
    participants: Iterable[User],
    *,
    deleted: bool = False,
    notice: dict[str, object] | None = None,
) -> dict[str, object]:
    users = list(participants)
    content: dict[str, object] = {
        "conversation": {
            "id": str(conversation.id),
            "origin_domain": conversation.origin_domain,
            "pair_key": conversation.pair_key,
            "type": "group",
            "authority_domain": conversation.authority_domain,
            "owner": {
                "id": str(conversation.owner_id),
                "origin_domain": conversation.owner_domain,
            },
            "name": channel.name,
            "state_version": str(conversation.state_version),
            "deleted": deleted,
        },
        "participants": [profile_from_user(user) for user in users],
    }
    if notice is not None:
        content["notice"] = notice
    return content


def group_notice_text(
    message_type: int,
    actor: User,
    target: User,
    new_owner: User | None = None,
) -> str:
    actor_name = actor.display_name or actor.username
    target_name = target.display_name or target.username
    owner_name = (new_owner.display_name or new_owner.username) if new_owner else None
    return group_dm_notice_text(message_type, actor_name, target_name, owner_name)


async def create_group_mutation_notice(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    conversation: DMConversation,
    channel: Channel,
    actor: User,
    *,
    action: str,
    target: User | None,
    previous_owner: tuple[int | None, str | None],
    participants: list[User],
) -> tuple[Message, dict[str, object]] | None:
    if action == "rename":
        return None
    if not participants:
        # There is no remaining audience and the conversation is being
        # deleted, so retaining a final leave notice would only create
        # unreachable storage on each former participant's replica.
        return None
    affected = actor if action == "leave" else target
    if affected is None:
        raise RuntimeError("group mutation notice has no affected member")
    if action == "add":
        message_type = GROUP_DM_MEMBER_ADDED
    elif action == "leave":
        message_type = GROUP_DM_MEMBER_LEFT
    elif action == "remove":
        message_type = GROUP_DM_MEMBER_REMOVED
    else:
        return None
    new_owner = None
    if previous_owner == (affected.id, affected.origin_domain) and participants:
        new_owner = next(
            (
                user
                for user in participants
                if (user.id, user.origin_domain)
                == (conversation.owner_id, conversation.owner_domain)
            ),
            None,
        )
    content = group_notice_text(message_type, actor, affected, new_owner)
    message_id = await snowflake.mint()
    # Membership notices are informational system messages, not mentions.  A
    # mention projection would incorrectly notify the person who was added or
    # removed solely because their display name appears in the notice.
    mention_refs: list[dict[str, object]] = []
    await admit_federated_dm_message(
        session,
        settings,
        conversation,
        message_id=message_id,
        message_domain=settings.domain,
        delta=dm_message_storage_delta(
            content=content,
            e2ee=None,
            mention_user_refs=mention_refs,
            attachments=[],
        ),
    )
    message = (
        await session.scalars(
            insert(Message)
            .values(
                id=message_id,
                origin_domain=settings.domain,
                channel_id=conversation.id,
                channel_domain=conversation.origin_domain,
                author_id=actor.id,
                author_domain=actor.origin_domain,
                content=content,
                e2ee=None,
                message_type=message_type,
                flags=4,
                mention_user_refs=mention_refs,
            )
            .returning(Message)
        )
    ).one()
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=message.channel_id,
            channel_domain=message.channel_domain,
            mention_user_refs=mention_refs,
        )
    )
    channel.last_message_id = message.id
    channel.last_message_domain = message.origin_domain
    return message, {
        "message": message_payload(message, actor, []),
        "author": profile_from_user(actor),
        "target": {
            "id": str(affected.id),
            "origin_domain": affected.origin_domain,
        },
    }


async def group_participants(session: AsyncSession, conversation: DMConversation) -> list[User]:
    return list(
        await session.scalars(
            select(User)
            .join(
                DMParticipant,
                (DMParticipant.user_id == User.id)
                & (DMParticipant.user_domain == User.origin_domain),
            )
            .where(
                DMParticipant.conversation_id == conversation.id,
                DMParticipant.conversation_domain == conversation.origin_domain,
            )
            .order_by(DMParticipant.joined_at, User.origin_domain, User.id)
        )
    )


async def reload_group_projection(
    session: AsyncSession,
    conversation_id: int,
    conversation_domain: str,
) -> tuple[DMConversation, Channel, list[User]]:
    """Reload group state after commit before rendering gateway projections.

    Async SQLAlchemy expires ORM attributes on commit.  Rendering an object
    retained across that boundary can otherwise attempt implicit IO and raise
    ``MissingGreenlet`` after the mutation has already succeeded.
    """

    conversation = await session.get(
        DMConversation,
        (conversation_id, conversation_domain),
        populate_existing=True,
    )
    channel = await session.get(
        Channel,
        (conversation_id, conversation_domain),
        populate_existing=True,
    )
    if conversation is None or channel is None or conversation.type != "group":
        raise RuntimeError("committed group DM projection state disappeared")
    return conversation, channel, await group_participants(session, conversation)


async def require_group_invite_friend(session: AsyncSession, inviter: User, invitee: User) -> None:
    """Require a mutual accepted friendship for an automatic group invitation."""

    await lock_relationship_pair(session, inviter, invitee)
    if await blocked_between(session, inviter, invitee):
        raise HTTPException(status_code=403, detail={"code": "GROUP_DM_INVITE_NOT_FRIEND"})
    relations = []
    if inviter.is_local:
        relations.append(await relationship(session, inviter, invitee))
    if invitee.is_local:
        relations.append(await relationship(session, invitee, inviter))
    if not relations or any(item is None or item.type != "friend" for item in relations):
        raise HTTPException(status_code=403, detail={"code": "GROUP_DM_INVITE_NOT_FRIEND"})


async def load_authoritative_group(
    session: AsyncSession,
    settings: Settings,
    conversation_id: int,
    conversation_domain: str,
    *,
    for_update: bool = False,
) -> tuple[DMConversation, Channel]:
    statement = select(DMConversation).where(
        DMConversation.id == conversation_id,
        DMConversation.origin_domain == conversation_domain,
    )
    if for_update:
        statement = statement.with_for_update()
    conversation = await session.scalar(statement)
    channel = await session.get(Channel, (conversation_id, conversation_domain))
    if (
        conversation is None
        or channel is None
        or conversation.type != "group"
        or conversation.authority_domain != settings.domain
        or conversation.origin_domain != settings.domain
    ):
        raise HTTPException(status_code=404, detail={"code": "GROUP_DM_NOT_FOUND"})
    return conversation, channel


async def require_group_member(
    session: AsyncSession, conversation: DMConversation, actor: User
) -> DMParticipant:
    membership = await session.get(
        DMParticipant,
        (
            conversation.id,
            conversation.origin_domain,
            actor.id,
            actor.origin_domain,
        ),
    )
    if membership is None:
        raise HTTPException(status_code=403, detail={"code": "GROUP_DM_NOT_MEMBER"})
    return membership


async def apply_authoritative_group_mutation(
    session: AsyncSession,
    settings: Settings,
    conversation: DMConversation,
    channel: Channel,
    actor: User,
    *,
    action: str,
    target: User | None = None,
    name: str | None = None,
) -> tuple[list[User], list[User], bool]:
    """Apply a serialized mutation at the group home.

    Invite friendship is checked by the caller against the invitee's home
    before `add`; this function owns membership/owner invariants.
    """

    await require_group_member(session, conversation, actor)
    before = await group_participants(session, conversation)
    if action == "rename":
        channel.name = name
    elif action == "add":
        if target is None:
            raise HTTPException(status_code=400, detail={"code": "GROUP_DM_MEMBER_REQUIRED"})
        if len(before) >= MAX_GROUP_DM_PARTICIPANTS:
            raise HTTPException(status_code=409, detail={"code": "GROUP_DM_FULL"})
        existing = await session.get(
            DMParticipant,
            (
                conversation.id,
                conversation.origin_domain,
                target.id,
                target.origin_domain,
            ),
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail={"code": "GROUP_DM_ALREADY_MEMBER"})
        session.add(
            DMParticipant(
                conversation_id=conversation.id,
                conversation_domain=conversation.origin_domain,
                user_id=target.id,
                user_domain=target.origin_domain,
            )
        )
    elif action in {"leave", "remove"}:
        removed = actor if action == "leave" else target
        if removed is None:
            raise HTTPException(status_code=400, detail={"code": "GROUP_DM_MEMBER_REQUIRED"})
        if action == "remove" and (
            actor.id,
            actor.origin_domain,
        ) != (conversation.owner_id, conversation.owner_domain):
            raise HTTPException(status_code=403, detail={"code": "GROUP_DM_OWNER_REQUIRED"})
        if action == "remove" and (removed.id, removed.origin_domain) == (
            conversation.owner_id,
            conversation.owner_domain,
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "GROUP_DM_OWNER_CANNOT_REMOVE_SELF"},
            )
        membership = await session.get(
            DMParticipant,
            (
                conversation.id,
                conversation.origin_domain,
                removed.id,
                removed.origin_domain,
            ),
        )
        if membership is None:
            raise HTTPException(status_code=404, detail={"code": "GROUP_DM_MEMBER_NOT_FOUND"})
        await session.delete(membership)
        await session.flush()
        remaining = await group_participants(session, conversation)
        if not remaining:
            conversation.state_version += 1
            channel.unavailable = True
            return before, [], True
        if (removed.id, removed.origin_domain) == (
            conversation.owner_id,
            conversation.owner_domain,
        ):
            successor = remaining[0]
            conversation.owner_id = successor.id
            conversation.owner_domain = successor.origin_domain
    else:
        raise HTTPException(status_code=400, detail={"code": "GROUP_DM_MUTATION_INVALID"})
    conversation.state_version += 1
    await session.flush()
    return before, await group_participants(session, conversation), False
