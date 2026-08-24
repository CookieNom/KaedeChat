from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .refs import EntityRef, User

if TYPE_CHECKING:
    from .client import Client


class MissingType:
    """Sentinel type used to distinguish omitted fields from explicit nulls."""

    __slots__ = ()


MISSING = MissingType()


def _datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _optional_ref(
    payload: dict[str, Any], id_key: str, domain_key: str
) -> EntityRef | None:
    raw_id = payload.get(id_key)
    domain = payload.get(domain_key)
    if raw_id is None or not isinstance(domain, str):
        return None
    return EntityRef(int(raw_id), domain)


@dataclass(slots=True)
class Guild:
    client: Client
    target: str
    ref: EntityRef
    name: str
    description: str | None = None
    icon_hash: str | None = None
    banner_hash: str | None = None
    owner_ref: EntityRef | None = None
    unavailable: bool = False
    sync_status: str | None = None
    permissions: int | None = None
    installation_id: int | None = None
    granted_scopes: tuple[str, ...] = ()
    granted_intents: tuple[str, ...] = ()
    e2ee_mode: str | None = None
    version: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Guild:
        domain = str(payload["origin_domain"])
        owner_domain = payload.get("owner_domain")
        owner_ref = (
            EntityRef(int(payload["owner_id"]), str(owner_domain))
            if payload.get("owner_id") is not None and isinstance(owner_domain, str)
            else None
        )
        return cls(
            client=client,
            target=target,
            ref=EntityRef(int(payload["id"]), domain),
            name=str(payload["name"]),
            description=(
                str(payload["description"])
                if payload.get("description") is not None
                else None
            ),
            icon_hash=(
                str(payload["icon_hash"])
                if payload.get("icon_hash") is not None
                else None
            ),
            banner_hash=(
                str(payload["banner_hash"])
                if payload.get("banner_hash") is not None
                else None
            ),
            owner_ref=owner_ref,
            unavailable=bool(payload.get("unavailable", False)),
            sync_status=(
                str(payload["sync_status"])
                if payload.get("sync_status") is not None
                else None
            ),
            permissions=(
                int(payload["permissions"])
                if payload.get("permissions") is not None
                else None
            ),
            installation_id=(
                int(payload["installation_id"])
                if payload.get("installation_id") is not None
                else None
            ),
            granted_scopes=tuple(
                str(item) for item in payload.get("granted_scopes", [])
            ),
            granted_intents=tuple(
                str(item) for item in payload.get("granted_intents", [])
            ),
            e2ee_mode=(
                str(payload["e2ee_mode"])
                if payload.get("e2ee_mode") is not None
                else None
            ),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
        )

    async def channels(self) -> list[Channel]:
        return await self.client.fetch_channels(self.ref, target=self.target)

    async def active_threads(self) -> ThreadPage:
        return await self.client.active_threads(self.ref, target=self.target)

    async def members(
        self,
        *,
        limit: int = 100,
        after: EntityRef | None = None,
        query: str | None = None,
    ) -> list[Member]:
        return await self.client.fetch_members(
            self.ref, target=self.target, limit=limit, after=after, query=query
        )

    async def roles(self) -> list[Role]:
        return await self.client.fetch_roles(self.ref, target=self.target)

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        federated_history_policy: str | MissingType = MISSING,
    ) -> Guild:
        return await self.client.edit_guild(
            self.ref,
            target=self.target,
            version=self.version,
            name=name,
            description=description,
            federated_history_policy=federated_history_policy,
        )

    async def create_channel(
        self,
        name: str,
        *,
        type: int = 0,
        topic: str | None = None,
        parent_id: int | None = None,
        rate_limit_per_user: int = 0,
        default_thread_rate_limit_per_user: int | MissingType = MISSING,
        default_auto_archive_duration: int | MissingType = MISSING,
        available_tags: list[dict[str, Any]] | MissingType = MISSING,
        default_reaction_emoji: dict[str, Any] | None | MissingType = MISSING,
        default_sort_order: int | None | MissingType = MISSING,
        default_forum_layout: int | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        e2ee_required: bool | MissingType = MISSING,
    ) -> Channel:
        return await self.client.create_channel(
            self.ref,
            name,
            target=self.target,
            type=type,
            topic=topic,
            parent_id=parent_id,
            rate_limit_per_user=rate_limit_per_user,
            default_thread_rate_limit_per_user=default_thread_rate_limit_per_user,
            default_auto_archive_duration=default_auto_archive_duration,
            available_tags=available_tags,
            default_reaction_emoji=default_reaction_emoji,
            default_sort_order=default_sort_order,
            default_forum_layout=default_forum_layout,
            flags=flags,
            e2ee_required=e2ee_required,
        )

    async def create_role(
        self,
        name: str,
        *,
        permissions: int = 0,
        color: int = 0,
        hoist: bool = False,
        mentionable: bool = False,
    ) -> Role:
        return await self.client.create_role(
            self.ref,
            name,
            target=self.target,
            permissions=permissions,
            color=color,
            hoist=hoist,
            mentionable=mentionable,
        )

    async def invites(self) -> list[Invite]:
        return await self.client.invites(self.ref, target=self.target)

    async def webhooks(self) -> list[Webhook]:
        return await self.client.webhooks(self.ref, target=self.target)

    async def emojis(self) -> list[Emoji]:
        return await self.client.emojis(self.ref, target=self.target)

    async def open_dm(self, handle: str) -> Channel:
        if self.installation_id is None:
            raise ValueError("guild payload does not include a bot installation")
        return await self.client.open_dm(
            handle,
            installation_id=self.installation_id,
            target=self.target,
        )


@dataclass(slots=True)
class ThreadMetadata:
    archived: bool
    auto_archive_duration: int
    archive_timestamp: datetime | None = None
    locked: bool = False
    invitable: bool | None = None
    create_timestamp: datetime | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ThreadMetadata:
        return cls(
            archived=bool(payload.get("archived", False)),
            auto_archive_duration=int(payload.get("auto_archive_duration", 1440)),
            archive_timestamp=_datetime(payload.get("archive_timestamp")),
            locked=bool(payload.get("locked", False)),
            invitable=(
                bool(payload["invitable"])
                if payload.get("invitable") is not None
                else None
            ),
            create_timestamp=_datetime(payload.get("create_timestamp")),
        )


@dataclass(slots=True)
class ThreadMember:
    thread_ref: EntityRef
    user_ref: EntityRef
    guild_ref: EntityRef | None = None
    join_timestamp: datetime | None = None
    flags: int = 0
    notification_level: str = "inherit"
    member: Member | None = None
    presence: dict[str, Any] | None = None

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        default_domain: str,
        default_thread: EntityRef | None = None,
        client: Client | None = None,
        target: str | None = None,
    ) -> ThreadMember:
        thread_id = payload.get("id", payload.get("thread_id"))
        thread_domain = payload.get("thread_domain")
        thread_ref = (
            EntityRef(int(thread_id), str(thread_domain))
            if thread_id is not None and isinstance(thread_domain, str)
            else None
        )
        if thread_ref is None:
            if default_thread is None:
                raise ValueError(
                    "thread member payload is missing its thread reference"
                )
            thread_ref = default_thread
        user_id = payload.get("user_id")
        if user_id is None and isinstance(payload.get("user"), dict):
            user_id = payload["user"].get("id")
        user_domain = payload.get("user_domain")
        if user_domain is None and isinstance(payload.get("user"), dict):
            user_domain = payload["user"].get("origin_domain")
        if user_id is None:
            raise ValueError("thread member payload is missing its user reference")
        return cls(
            thread_ref=thread_ref,
            user_ref=EntityRef(int(user_id), str(user_domain or default_domain)),
            guild_ref=_optional_ref(payload, "guild_id", "guild_domain"),
            join_timestamp=_datetime(payload.get("join_timestamp")),
            flags=int(payload.get("flags", 0)),
            notification_level=str(payload.get("notification_level", "inherit")),
            member=(
                Member.from_payload(client, target, payload["member"])
                if client is not None
                and target is not None
                and isinstance(payload.get("member"), dict)
                else None
            ),
            presence=(
                dict(payload["presence"])
                if isinstance(payload.get("presence"), dict)
                else None
            ),
        )


@dataclass(slots=True)
class ForumTag:
    id: int
    name: str
    moderated: bool = False
    emoji_id: int | None = None
    emoji_domain: str | None = None
    emoji_name: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ForumTag:
        return cls(
            id=int(payload["id"]),
            name=str(payload["name"]),
            moderated=bool(payload.get("moderated", False)),
            emoji_id=(
                int(payload["emoji_id"])
                if payload.get("emoji_id") is not None
                else None
            ),
            emoji_domain=(
                str(payload["emoji_domain"])
                if payload.get("emoji_domain") is not None
                else None
            ),
            emoji_name=(
                str(payload["emoji_name"])
                if payload.get("emoji_name") is not None
                else None
            ),
        )


@dataclass(slots=True)
class Channel:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef | None
    type: int
    name: str | None = None
    topic: str | None = None
    position: int = 0
    parent_ref: EntityRef | None = None
    created_at: datetime | None = None
    permissions: int = 0
    rate_limit_per_user: int = 0
    flags: int = 0
    owner_ref: EntityRef | None = None
    last_message_ref: EntityRef | None = None
    starter_message_ref: EntityRef | None = None
    starter_message: Message | None = None
    thread_metadata: ThreadMetadata | None = None
    member: ThreadMember | None = None
    message_count: int = 0
    total_message_sent: int = 0
    member_count: int = 0
    applied_tag_ids: tuple[int, ...] = ()
    available_tags: tuple[ForumTag, ...] = ()
    default_reaction_emoji: dict[str, Any] | None = None
    default_thread_rate_limit_per_user: int | None = None
    default_auto_archive_duration: int | None = None
    default_sort_order: int | None = None
    default_forum_layout: int | None = None
    e2ee_required: bool = False
    encryption_mode: str = "plaintext"
    search_available: bool = True
    newly_created: bool = False
    version: str | None = None
    bot_installation_id: int | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Channel:
        ref = EntityRef(int(payload["id"]), str(payload["origin_domain"]))
        raw_thread_metadata = payload.get("thread_metadata")
        raw_member = payload.get("member")
        raw_tags = payload.get("available_tags")
        raw_applied_tags = payload.get(
            "applied_tags", payload.get("applied_tag_ids", [])
        )
        # Discord calls the atomic forum-create starter `message`; Kaede keeps
        # `starter_message` on ordinary channel projections for clarity.
        raw_starter_message = payload.get("starter_message") or payload.get("message")
        starter_message = (
            Message.from_payload(client, target, raw_starter_message)
            if isinstance(raw_starter_message, dict)
            else None
        )
        return cls(
            client=client,
            target=target,
            ref=ref,
            guild_ref=_optional_ref(payload, "guild_id", "guild_domain"),
            type=int(payload.get("type", 0)),
            name=str(payload["name"]) if payload.get("name") is not None else None,
            topic=str(payload["topic"]) if payload.get("topic") is not None else None,
            position=int(payload.get("position", 0)),
            parent_ref=_optional_ref(payload, "parent_id", "parent_domain"),
            created_at=_datetime(payload.get("created_at")),
            permissions=int(payload.get("permissions", 0)),
            rate_limit_per_user=int(payload.get("rate_limit_per_user", 0)),
            flags=int(payload.get("flags") or 0),
            owner_ref=_optional_ref(payload, "owner_id", "owner_domain"),
            last_message_ref=_optional_ref(
                payload, "last_message_id", "last_message_domain"
            ),
            starter_message_ref=(
                _optional_ref(payload, "starter_message_id", "starter_message_domain")
                or _optional_ref(payload, "source_message_id", "source_message_domain")
                or (starter_message.ref if starter_message is not None else None)
            ),
            starter_message=starter_message,
            thread_metadata=(
                ThreadMetadata.from_payload(
                    raw_thread_metadata
                    if isinstance(raw_thread_metadata, dict)
                    else payload
                )
                if int(payload.get("type", 0)) in {10, 11, 12}
                else None
            ),
            member=(
                ThreadMember.from_payload(
                    raw_member,
                    default_domain=ref.domain,
                    default_thread=ref,
                    client=client,
                    target=target,
                )
                if isinstance(raw_member, dict)
                else None
            ),
            message_count=int(payload.get("message_count") or 0),
            total_message_sent=int(payload.get("total_message_sent") or 0),
            member_count=int(payload.get("member_count") or 0),
            applied_tag_ids=tuple(int(item) for item in raw_applied_tags or ()),
            available_tags=tuple(
                ForumTag.from_payload(item)
                for item in (raw_tags or ())
                if isinstance(item, dict)
            ),
            default_reaction_emoji=(
                dict(payload["default_reaction_emoji"])
                if isinstance(payload.get("default_reaction_emoji"), dict)
                else None
            ),
            default_thread_rate_limit_per_user=(
                int(payload["default_thread_rate_limit_per_user"])
                if payload.get("default_thread_rate_limit_per_user") is not None
                else None
            ),
            default_auto_archive_duration=(
                int(payload["default_auto_archive_duration"])
                if payload.get("default_auto_archive_duration") is not None
                else None
            ),
            default_sort_order=(
                int(payload["default_sort_order"])
                if payload.get("default_sort_order") is not None
                else None
            ),
            default_forum_layout=(
                int(payload["default_forum_layout"])
                if payload.get("default_forum_layout") is not None
                else None
            ),
            e2ee_required=bool(
                payload.get("e2ee_required", False)
                or payload.get("default_thread_encryption_mode") == "e2ee"
            ),
            encryption_mode=str(payload.get("encryption_mode", "plaintext")),
            search_available=bool(payload.get("search_available", True)),
            newly_created=bool(payload.get("newly_created", False)),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
            bot_installation_id=(
                int(payload["bot_installation_id"])
                if payload.get("bot_installation_id") is not None
                else None
            ),
        )

    @property
    def is_thread(self) -> bool:
        return self.type in {10, 11, 12}

    @property
    def is_forum(self) -> bool:
        return self.type == 15

    @property
    def archived(self) -> bool:
        return bool(self.thread_metadata and self.thread_metadata.archived)

    @property
    def locked(self) -> bool:
        return bool(self.thread_metadata and self.thread_metadata.locked)

    async def send(
        self,
        content: str | None = None,
        *,
        reply_to: EntityRef | None = None,
        attachment_ids: list[int] | None = None,
        e2ee: dict[str, Any] | None = None,
    ) -> Message:
        return await self.client.send_message(
            self.ref,
            content,
            target=self.target,
            reply_to=reply_to,
            attachment_ids=attachment_ids,
            e2ee=e2ee,
            installation_id=self.bot_installation_id,
        )

    async def history(
        self, *, before: EntityRef | None = None, limit: int = 50
    ) -> list[Message]:
        return await self.client.history(
            self.ref, target=self.target, before=before, limit=limit
        )

    async def pins(self) -> list[Message]:
        return await self.client.pins(self.ref, target=self.target)

    async def trigger_typing(self) -> None:
        await self.client.trigger_typing(
            self.ref,
            target=self.target,
            installation_id=self.bot_installation_id,
        )

    async def voice_occupancy(self) -> VoiceOccupancy:
        return await self.client.voice_occupancy(self.ref, target=self.target)

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        topic: str | None | MissingType = MISSING,
        parent_id: int | None | MissingType = MISSING,
        rate_limit_per_user: int | MissingType = MISSING,
        default_thread_rate_limit_per_user: int | MissingType = MISSING,
        default_auto_archive_duration: int | MissingType = MISSING,
        available_tags: list[dict[str, Any]] | MissingType = MISSING,
        default_reaction_emoji: dict[str, Any] | None | MissingType = MISSING,
        default_sort_order: int | None | MissingType = MISSING,
        default_forum_layout: int | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        e2ee_required: bool | MissingType = MISSING,
        federated_history_policy: str | MissingType = MISSING,
        sync_permissions: bool | MissingType = MISSING,
    ) -> Channel:
        if self.guild_ref is None:
            raise ValueError(
                "direct-message channels cannot be managed as guild channels"
            )
        return await self.client.edit_channel(
            self.guild_ref,
            self.ref,
            target=self.target,
            version=self.version,
            name=name,
            topic=topic,
            parent_id=parent_id,
            rate_limit_per_user=rate_limit_per_user,
            default_thread_rate_limit_per_user=default_thread_rate_limit_per_user,
            default_auto_archive_duration=default_auto_archive_duration,
            available_tags=available_tags,
            default_reaction_emoji=default_reaction_emoji,
            default_sort_order=default_sort_order,
            default_forum_layout=default_forum_layout,
            flags=flags,
            e2ee_required=e2ee_required,
            federated_history_policy=federated_history_policy,
            sync_permissions=sync_permissions,
        )

    async def delete(self) -> None:
        if self.guild_ref is None:
            raise ValueError(
                "direct-message channels cannot be deleted through guild management"
            )
        if self.is_thread:
            await self.client.delete_thread(self.ref, target=self.target)
            return
        await self.client.delete_channel(self.guild_ref, self.ref, target=self.target)

    async def upload(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        encryption_mode: str = "plaintext",
        encryption_protocol: str | None = None,
    ) -> Attachment:
        return await self.client.upload_attachment(
            self.ref,
            data,
            target=self.target,
            filename=filename,
            content_type=content_type,
            encryption_mode=encryption_mode,
            encryption_protocol=encryption_protocol,
        )

    async def create_webhook(self, name: str) -> Webhook:
        if self.guild_ref is None:
            raise ValueError("webhooks require a guild channel")
        return await self.client.create_webhook(
            self.guild_ref, self.ref, name, target=self.target
        )

    async def start_thread(
        self,
        name: str,
        *,
        type: int | None = None,
        content: str | None = None,
        e2ee: dict[str, Any] | None = None,
        attachment_ids: list[int] | None = None,
        applied_tag_ids: list[int] | None = None,
        auto_archive_duration: int | None = None,
        rate_limit_per_user: int | None = None,
        invitable: bool | None = None,
        client_nonce: str | None = None,
    ) -> Channel:
        """Create a thread or an atomic forum post beneath this channel."""

        return await self.client.start_thread(
            self.ref,
            name,
            target=self.target,
            type=type,
            content=content,
            e2ee=e2ee,
            attachment_ids=attachment_ids,
            applied_tag_ids=applied_tag_ids,
            auto_archive_duration=auto_archive_duration,
            rate_limit_per_user=rate_limit_per_user,
            invitable=invitable,
            client_nonce=client_nonce,
        )

    async def create_post(
        self,
        name: str,
        content: str | None = None,
        *,
        e2ee: dict[str, Any] | None = None,
        attachment_ids: list[int] | None = None,
        applied_tag_ids: list[int] | None = None,
        auto_archive_duration: int | None = None,
        rate_limit_per_user: int | None = None,
        client_nonce: str | None = None,
    ) -> Channel:
        if not self.is_forum:
            raise ValueError("forum posts require a forum channel")
        if content is not None and len(content) > 2000:
            raise ValueError("forum post content cannot exceed 2000 characters")
        if not content and not attachment_ids and e2ee is None:
            raise ValueError("a forum post requires content or an attachment")
        return await self.start_thread(
            name,
            type=11,
            content=content,
            e2ee=e2ee,
            attachment_ids=attachment_ids,
            applied_tag_ids=applied_tag_ids,
            auto_archive_duration=auto_archive_duration,
            rate_limit_per_user=rate_limit_per_user,
            client_nonce=client_nonce,
        )

    async def threads(
        self,
        *,
        archived: bool = False,
        include_archived: bool = False,
        before: datetime | None = None,
        cursor: str | None = None,
        limit: int = 50,
        tag_id: int | None = None,
        tag_ids: list[int] | None = None,
        query: str | None = None,
        sort_order: int | None = None,
    ) -> ThreadPage:
        return await self.client.fetch_threads(
            self.ref,
            target=self.target,
            archived=archived,
            include_archived=include_archived,
            before=before,
            cursor=cursor,
            limit=limit,
            tag_id=tag_id,
            tag_ids=tag_ids,
            query=query,
            sort_order=sort_order,
        )

    async def join(
        self, *, flags: int = 0, notification_level: str = "inherit"
    ) -> None:
        if not self.is_thread:
            raise ValueError("only threads can be joined")
        await self.client.join_thread(
            self.ref,
            target=self.target,
            flags=flags,
            notification_level=notification_level,
        )

    async def leave(self) -> None:
        if not self.is_thread:
            raise ValueError("only threads can be left")
        await self.client.leave_thread(self.ref, target=self.target)

    async def add_member(self, user: EntityRef) -> None:
        if not self.is_thread:
            raise ValueError("members can only be added to threads")
        await self.client.add_thread_member(
            self.ref,
            user,
            target=self.target,
        )

    async def remove_member(self, user: EntityRef) -> None:
        if not self.is_thread:
            raise ValueError("members can only be removed from threads")
        await self.client.remove_thread_member(self.ref, user, target=self.target)

    async def members(
        self,
        *,
        after: EntityRef | None = None,
        limit: int = 100,
        with_member: bool = False,
    ) -> list[ThreadMember]:
        if not self.is_thread:
            raise ValueError("only threads have thread members")
        return await self.client.thread_members(
            self.ref,
            target=self.target,
            after=after,
            limit=limit,
            with_member=with_member,
        )

    async def fetch_member(
        self, user: EntityRef, *, with_member: bool = False
    ) -> ThreadMember:
        if not self.is_thread:
            raise ValueError("only threads have thread members")
        return await self.client.fetch_thread_member(
            self.ref,
            user,
            target=self.target,
            with_member=with_member,
        )

    async def edit_thread(
        self,
        *,
        name: str | MissingType = MISSING,
        archived: bool | MissingType = MISSING,
        locked: bool | MissingType = MISSING,
        invitable: bool | MissingType = MISSING,
        auto_archive_duration: int | MissingType = MISSING,
        rate_limit_per_user: int | MissingType = MISSING,
        applied_tag_ids: list[int] | MissingType = MISSING,
        pinned: bool | MissingType = MISSING,
    ) -> Channel:
        if not self.is_thread:
            raise ValueError("thread settings require a thread channel")
        return await self.client.edit_thread(
            self.ref,
            target=self.target,
            name=name,
            archived=archived,
            locked=locked,
            invitable=invitable,
            auto_archive_duration=auto_archive_duration,
            rate_limit_per_user=rate_limit_per_user,
            applied_tag_ids=applied_tag_ids,
            pinned=pinned,
        )


@dataclass(slots=True)
class ThreadPage:
    threads: list[Channel]
    members: list[ThreadMember]
    has_more: bool = False
    next_cursor: str | None = None


@dataclass(slots=True)
class Role:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    name: str
    color: int
    permissions: int
    position: int
    hoist: bool = False
    mentionable: bool = False
    version: str | None = None

    @classmethod
    def from_payload(cls, client: Client, target: str, payload: dict[str, Any]) -> Role:
        return cls(
            client=client,
            target=target,
            ref=EntityRef(int(payload["id"]), str(payload["origin_domain"])),
            guild_ref=EntityRef(int(payload["guild_id"]), str(payload["guild_domain"])),
            name=str(payload["name"]),
            color=int(payload.get("color", 0)),
            permissions=int(payload.get("permissions", 0)),
            position=int(payload.get("position", 0)),
            hoist=bool(payload.get("hoist", False)),
            mentionable=bool(payload.get("mentionable", False)),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
        )

    @property
    def mention(self) -> str:
        return f"<@&{self.ref}>"

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        permissions: int | MissingType = MISSING,
        color: int | MissingType = MISSING,
        hoist: bool | MissingType = MISSING,
        mentionable: bool | MissingType = MISSING,
    ) -> Role:
        return await self.client.edit_role(
            self.guild_ref,
            self.ref,
            target=self.target,
            version=self.version,
            name=name,
            permissions=permissions,
            color=color,
            hoist=hoist,
            mentionable=mentionable,
        )

    async def delete(self) -> None:
        await self.client.delete_role(self.guild_ref, self.ref, target=self.target)


@dataclass(slots=True)
class Member:
    client: Client
    target: str
    guild_ref: EntityRef
    user: User
    nickname: str | None
    joined_at: datetime
    timeout_until: datetime | None = None
    timeout_indefinite: bool = False
    role_ids: tuple[int, ...] = ()
    presence: str | None = None
    voice_flags: int = 0
    member_version: int = 0

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Member:
        return cls(
            client=client,
            target=target,
            guild_ref=EntityRef(int(payload["guild_id"]), str(payload["guild_domain"])),
            user=User.from_payload(payload["user"]),
            nickname=(
                str(payload["nickname"])
                if payload.get("nickname") is not None
                else None
            ),
            joined_at=datetime.fromisoformat(str(payload["joined_at"])),
            timeout_until=_datetime(payload.get("timeout_until")),
            timeout_indefinite=bool(payload.get("timeout_indefinite", False)),
            role_ids=tuple(int(item) for item in payload.get("role_ids", [])),
            presence=(
                str(payload["presence"])
                if payload.get("presence") is not None
                else None
            ),
            voice_flags=int(payload.get("voice_flags", 0)),
            member_version=int(payload.get("member_version", 0)),
        )

    @property
    def name(self) -> str:
        return self.nickname or self.user.name

    async def edit(
        self,
        *,
        nickname: str | None,
        reason: str | None = None,
    ) -> Member:
        return await self.client.edit_member(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            nickname=nickname,
            reason=reason,
        )

    async def timeout(
        self,
        *,
        until: datetime | None = None,
        indefinite: bool = False,
        reason: str | None = None,
    ) -> Member:
        if until is None and not indefinite:
            raise ValueError("a timeout needs an expiry or indefinite=True")
        return await self.client.edit_member(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            timeout_until=until,
            timeout_indefinite=indefinite,
            reason=reason,
        )

    async def remove_timeout(self, *, reason: str | None = None) -> Member:
        return await self.client.edit_member(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            timeout_until=None,
            timeout_indefinite=False,
            reason=reason,
        )

    async def kick(self, *, reason: str | None = None) -> None:
        await self.client.kick_member(
            self.guild_ref, self.user.ref, target=self.target, reason=reason
        )

    async def ban(
        self,
        *,
        reason: str | None = None,
        delete_message_seconds: int = 0,
        expires_at: datetime | None = None,
    ) -> None:
        await self.client.ban_member(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            reason=reason,
            delete_message_seconds=delete_message_seconds,
            expires_at=expires_at,
        )

    async def add_role(self, role: EntityRef) -> None:
        await self.client.add_member_role(
            self.guild_ref, self.user.ref, role, target=self.target
        )

    async def remove_role(self, role: EntityRef) -> None:
        await self.client.remove_member_role(
            self.guild_ref, self.user.ref, role, target=self.target
        )

    async def set_roles(self, roles: list[EntityRef]) -> Member:
        return await self.client.set_member_roles(
            self.guild_ref, self.user.ref, roles, target=self.target
        )

    async def set_voice_moderation(
        self,
        *,
        server_mute: bool | None = None,
        server_deaf: bool | None = None,
        reason: str | None = None,
    ) -> None:
        await self.client.set_voice_moderation(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            server_mute=server_mute,
            server_deaf=server_deaf,
            reason=reason,
        )

    async def disconnect_voice(self, *, reason: str | None = None) -> None:
        await self.client.disconnect_voice(
            self.guild_ref, self.user.ref, target=self.target, reason=reason
        )

    async def move_voice(
        self, channel: EntityRef, *, reason: str | None = None
    ) -> None:
        await self.client.move_voice(
            self.guild_ref,
            self.user.ref,
            channel,
            target=self.target,
            reason=reason,
        )


@dataclass(slots=True)
class Ban:
    client: Client
    target: str
    guild_ref: EntityRef
    user: User
    reason: str | None
    created_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def from_payload(cls, client: Client, target: str, payload: dict[str, Any]) -> Ban:
        return cls(
            client=client,
            target=target,
            guild_ref=EntityRef(int(payload["guild_id"]), str(payload["guild_domain"])),
            user=User.from_payload(payload["user"]),
            reason=str(payload["reason"])
            if payload.get("reason") is not None
            else None,
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            expires_at=_datetime(payload.get("expires_at")),
        )

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.unban_member(
            self.guild_ref, self.user.ref, target=self.target, reason=reason
        )


@dataclass(slots=True)
class Invite:
    client: Client
    target: str
    code: str
    guild_ref: EntityRef
    channel_id: int | None
    uses: int
    max_uses: int | None
    expires_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Invite:
        guild = payload.get("guild")
        if not isinstance(guild, dict):
            raise ValueError("invite payload is missing its guild")
        return cls(
            client=client,
            target=target,
            code=str(payload["code"]),
            guild_ref=EntityRef(int(guild["id"]), str(guild["origin_domain"])),
            channel_id=(
                int(payload["channel_id"])
                if payload.get("channel_id") is not None
                else None
            ),
            uses=int(payload.get("uses", 0)),
            max_uses=(
                int(payload["max_uses"])
                if payload.get("max_uses") is not None
                else None
            ),
            expires_at=_datetime(payload.get("expires_at")),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            revoked_at=_datetime(payload.get("revoked_at")),
        )

    async def revoke(self) -> None:
        await self.client.revoke_invite(self.guild_ref, self.code, target=self.target)


@dataclass(slots=True)
class Webhook:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    channel_ref: EntityRef
    name: str
    avatar_hash: str | None = None
    revoked: bool = False
    token: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Webhook:
        guild_domain = str(payload["guild_domain"])
        return cls(
            client=client,
            target=target,
            ref=EntityRef(int(payload["id"]), guild_domain),
            guild_ref=EntityRef(int(payload["guild_id"]), guild_domain),
            channel_ref=EntityRef(
                int(payload["channel_id"]), str(payload["channel_domain"])
            ),
            name=str(payload["name"]),
            avatar_hash=(
                str(payload["avatar_hash"])
                if payload.get("avatar_hash") is not None
                else None
            ),
            revoked=bool(payload.get("revoked", False)),
            token=(str(payload["token"]) if payload.get("token") is not None else None),
        )

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        avatar_hash: str | None | MissingType = MISSING,
    ) -> Webhook:
        return await self.client.edit_webhook(
            self.guild_ref,
            self.ref.id,
            target=self.target,
            name=name,
            avatar_hash=avatar_hash,
        )

    async def rotate(self) -> Webhook:
        return await self.client.rotate_webhook(
            self.guild_ref, self.ref.id, target=self.target
        )

    async def delete(self) -> None:
        await self.client.delete_webhook(
            self.guild_ref, self.ref.id, target=self.target
        )


@dataclass(slots=True)
class Emoji:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    name: str
    animated: bool = False
    media_hash: str | None = None
    version: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Emoji:
        return cls(
            client=client,
            target=target,
            ref=EntityRef(int(payload["id"]), str(payload["origin_domain"])),
            guild_ref=EntityRef(int(payload["guild_id"]), str(payload["guild_domain"])),
            name=str(payload["name"]),
            animated=bool(payload.get("animated", False)),
            media_hash=(
                str(payload["media_hash"])
                if payload.get("media_hash") is not None
                else None
            ),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
        )

    @property
    def token(self) -> str:
        prefix = "a" if self.animated else ""
        return f"<{prefix}:{self.name}:{self.ref}>"

    async def delete(self) -> None:
        await self.client.delete_emoji(self.guild_ref, self.ref.id, target=self.target)


@dataclass(slots=True)
class Attachment:
    client: Client
    target: str
    ref: EntityRef
    filename: str
    content_type: str
    size: int
    scan_status: str
    width: int | None = None
    height: int | None = None
    blurhash: str | None = None
    encryption_mode: str = "plaintext"
    encryption_protocol: str | None = None
    purpose: str = "attachment"
    variants: dict[str, Any] = field(default_factory=dict)
    finalized_at: datetime | None = None
    upload_url: str | None = None
    expires_at: datetime | None = None
    installation_id: int | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Attachment:
        return cls(
            client=client,
            target=target,
            ref=EntityRef(int(payload["id"]), str(payload["origin_domain"])),
            filename=str(payload["filename"]),
            content_type=str(payload["content_type"]),
            size=int(payload["size"]),
            scan_status=str(payload.get("scan_status", "pending")),
            width=int(payload["width"]) if payload.get("width") is not None else None,
            height=(
                int(payload["height"]) if payload.get("height") is not None else None
            ),
            blurhash=(
                str(payload["blurhash"])
                if payload.get("blurhash") is not None
                else None
            ),
            encryption_mode=str(payload.get("encryption_mode", "plaintext")),
            encryption_protocol=(
                str(payload["encryption_protocol"])
                if payload.get("encryption_protocol") is not None
                else None
            ),
            purpose=str(payload.get("purpose", "attachment")),
            variants=dict(payload.get("variants") or {}),
            finalized_at=_datetime(payload.get("finalized_at")),
            upload_url=(
                str(payload["upload_url"])
                if payload.get("upload_url") is not None
                else None
            ),
            expires_at=_datetime(payload.get("expires_at")),
            installation_id=(
                int(payload["installation_id"])
                if payload.get("installation_id") is not None
                else None
            ),
        )

    async def refresh(self) -> Attachment:
        return await self.client.fetch_attachment(self.ref, target=self.target)

    async def read(
        self, variant: str = "original", *, max_bytes: int | None = None
    ) -> bytes:
        return await self.client.download_attachment(
            self.ref, variant=variant, target=self.target, max_bytes=max_bytes
        )


@dataclass(slots=True)
class Message:
    client: Client
    target: str
    ref: EntityRef
    channel_ref: EntityRef
    author: User | None
    content: str | None
    created_at: datetime | None
    attachments: list[Attachment]
    message_type: int = 0
    thread: Channel | None = None
    content_unavailable: bool = False
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    referenced_message_ref: EntityRef | None = None
    referenced_message: Message | None = None
    flags: int = 0
    pinned_at: datetime | None = None
    bot_installation_id: int | None = None

    @classmethod
    def from_payload(
        cls,
        client: Client,
        target: str,
        payload: dict[str, Any],
        *,
        _reference_depth: int = 0,
    ) -> Message:
        channel = payload.get("channel") or {}
        author = payload.get("author")
        channel_id = channel.get("id", payload.get("channel_id"))
        channel_domain = channel.get("origin_domain", payload.get("channel_domain"))
        if channel_id is None or not isinstance(channel_domain, str):
            raise ValueError(
                "message payload is missing its composite channel reference"
            )
        message_reference = payload.get("message_reference")
        referenced_message_ref = _optional_ref(
            payload, "referenced_message_id", "referenced_message_domain"
        )
        if referenced_message_ref is None and isinstance(message_reference, dict):
            referenced_message_ref = _optional_ref(
                message_reference, "message_id", "message_domain"
            )
        raw_referenced_message = payload.get("referenced_message")
        referenced_message = (
            cls.from_payload(
                client,
                target,
                raw_referenced_message,
                _reference_depth=_reference_depth + 1,
            )
            if _reference_depth == 0 and isinstance(raw_referenced_message, dict)
            else None
        )
        return cls(
            client=client,
            target=target,
            ref=EntityRef(int(payload["id"]), str(payload["origin_domain"])),
            channel_ref=EntityRef(int(channel_id), channel_domain),
            author=User.from_payload(author) if isinstance(author, dict) else None,
            content=(
                str(payload["content"]) if payload.get("content") is not None else None
            ),
            created_at=_datetime(payload.get("created_at")),
            attachments=[
                Attachment.from_payload(client, target, item)
                for item in payload.get("attachments") or []
                if isinstance(item, dict)
            ],
            message_type=int(payload.get("message_type", 0)),
            thread=(
                Channel.from_payload(client, target, payload["thread"])
                if isinstance(payload.get("thread"), dict)
                else None
            ),
            content_unavailable=bool(payload.get("content_unavailable", False)),
            edited_at=_datetime(payload.get("edited_at")),
            deleted_at=_datetime(payload.get("deleted_at")),
            referenced_message_ref=(
                referenced_message_ref
                or (referenced_message.ref if referenced_message is not None else None)
            ),
            referenced_message=referenced_message,
            flags=int(payload.get("flags", 0)),
            pinned_at=_datetime(payload.get("pinned_at")),
            bot_installation_id=(
                int(payload["bot_installation_id"])
                if payload.get("bot_installation_id") is not None
                else None
            ),
        )

    async def reply(self, content: str) -> Message:
        if self.bot_installation_id is None:
            return await self.client.send_message(
                self.channel_ref,
                content,
                target=self.target,
                reply_to=self.ref,
            )
        return await self.client.send_message(
            self.channel_ref,
            content,
            target=self.target,
            reply_to=self.ref,
            installation_id=self.bot_installation_id,
        )

    async def edit(self, content: str) -> Message:
        return await self.client.edit_message(
            self.channel_ref, self.ref, content, target=self.target
        )

    async def delete(self) -> None:
        await self.client.delete_message(self.channel_ref, self.ref, target=self.target)

    async def add_reaction(self, emoji: str) -> None:
        await self.client.add_reaction(
            self.channel_ref, self.ref, emoji, target=self.target
        )

    async def remove_reaction(self, emoji: str) -> None:
        await self.client.remove_reaction(
            self.channel_ref, self.ref, emoji, target=self.target
        )

    async def pin(self) -> None:
        await self.client.pin_message(self.channel_ref, self.ref, target=self.target)

    async def unpin(self) -> None:
        await self.client.unpin_message(self.channel_ref, self.ref, target=self.target)

    async def start_thread(
        self,
        name: str,
        *,
        auto_archive_duration: int | None = None,
        rate_limit_per_user: int | None = None,
    ) -> Channel:
        return await self.client.start_thread_from_message(
            self.channel_ref,
            self.ref,
            name,
            target=self.target,
            auto_archive_duration=auto_archive_duration,
            rate_limit_per_user=rate_limit_per_user,
        )


@dataclass(slots=True)
class Interaction:
    client: Client
    target: str
    id: int
    application_ref: EntityRef
    guild_ref: EntityRef
    channel_ref: EntityRef
    user: User
    command: dict[str, Any]
    options: dict[str, Any] | None
    encrypted_payload: dict[str, Any] | None
    expires_at: datetime | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Interaction:
        return cls(
            client=client,
            target=target,
            id=int(payload["id"]),
            application_ref=EntityRef.parse(payload["application_ref"]),
            guild_ref=EntityRef.parse(payload["guild_ref"]),
            channel_ref=EntityRef.parse(payload["channel_ref"]),
            user=User.from_payload(payload["user"]),
            command=payload["command"],
            options=payload.get("options"),
            encrypted_payload=payload.get("encrypted_payload"),
            expires_at=_datetime(payload.get("expires_at")),
        )

    async def defer(self) -> None:
        await self.client.request(
            "POST",
            f"/api/v1/bots/interactions/{self.id}/defer",
            target=self.target,
        )

    async def respond(
        self, content: str, *, e2ee: dict[str, Any] | None = None
    ) -> Message:
        payload: dict[str, Any] = {"content": content}
        if e2ee is not None:
            payload = {"content": None, "e2ee": e2ee}
        raw = await self.client.request(
            "POST",
            f"/api/v1/bots/interactions/{self.id}/response",
            target=self.target,
            json={"message": payload},
        )
        return Message.from_payload(self.client, self.target, raw)


@dataclass(frozen=True, slots=True)
class ReadyEvent:
    target: str
    application_ref: EntityRef
    worker_id: int
    installations: tuple[dict[str, Any], ...]
    intents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MessageDeleteEvent:
    target: str
    message_ref: EntityRef
    channel_ref: EntityRef


@dataclass(frozen=True, slots=True)
class ReactionEvent:
    target: str
    message_ref: EntityRef
    channel_ref: EntityRef
    user_ref: EntityRef
    emoji: str


@dataclass(frozen=True, slots=True)
class PinEvent:
    target: str
    message_ref: EntityRef
    channel_ref: EntityRef
    pinned: bool


@dataclass(frozen=True, slots=True)
class MemberRemoveEvent:
    target: str
    guild_ref: EntityRef
    user_ref: EntityRef


@dataclass(frozen=True, slots=True)
class GuildDeleteEvent:
    target: str
    guild_ref: EntityRef


@dataclass(frozen=True, slots=True)
class ChannelDeleteEvent:
    target: str
    channel_ref: EntityRef
    guild_ref: EntityRef | None = None


@dataclass(frozen=True, slots=True)
class ThreadDeleteEvent:
    target: str
    thread_ref: EntityRef
    guild_ref: EntityRef
    parent_ref: EntityRef
    type: int


@dataclass(frozen=True, slots=True)
class ThreadListSyncEvent:
    target: str
    guild_ref: EntityRef
    channel_refs: tuple[EntityRef, ...] | None
    threads: tuple[Channel, ...]
    members: tuple[ThreadMember, ...]


@dataclass(frozen=True, slots=True)
class ThreadMemberUpdateEvent:
    target: str
    member: ThreadMember


@dataclass(frozen=True, slots=True)
class ThreadMembersUpdateEvent:
    target: str
    thread_ref: EntityRef
    guild_ref: EntityRef
    member_count: int
    added_members: tuple[ThreadMember, ...]
    removed_member_refs: tuple[EntityRef, ...]


@dataclass(frozen=True, slots=True)
class RoleDeleteEvent:
    target: str
    role_ref: EntityRef
    guild_ref: EntityRef


@dataclass(frozen=True, slots=True)
class EmojiDeleteEvent:
    target: str
    emoji_ref: EntityRef
    guild_ref: EntityRef


@dataclass(frozen=True, slots=True)
class TypingEvent:
    target: str
    channel_ref: EntityRef
    user_ref: EntityRef
    timestamp: int


@dataclass(frozen=True, slots=True)
class PresenceEvent:
    target: str
    user_ref: EntityRef
    status: str
    custom_status: str | None
    raw: dict[str, Any]
    guild_ref: EntityRef | None = None


@dataclass(frozen=True, slots=True)
class VoiceOccupancy:
    channel_ref: EntityRef
    participants: tuple[dict[str, Any], ...]
    generated_at: int | None = None


@dataclass(frozen=True, slots=True)
class VoiceStateEvent:
    target: str
    guild_ref: EntityRef | None
    channel_ref: EntityRef | None
    user_ref: EntityRef | None
    connected: bool | None
    self_mute: bool | None
    self_deaf: bool | None
    server_mute: bool | None
    server_deaf: bool | None
    participants: tuple[dict[str, Any], ...]
    heartbeat: bool
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RawEvent:
    target: str
    type: str
    data: dict[str, Any]
    topic: str | None = None
    sequence: int = 0
