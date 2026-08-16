from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.privacy import blocked_between, lock_relationship_pair, relationship
from app.core.dm import MAX_GROUP_DM_PARTICIPANTS
from app.core.settings import Settings
from app.db.models import Channel, DMConversation, DMParticipant, User
from app.federation.replication import profile_from_user


def group_conversation_content(
    conversation: DMConversation,
    channel: Channel,
    participants: Iterable[User],
    *,
    deleted: bool = False,
) -> dict[str, object]:
    users = list(participants)
    return {
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
