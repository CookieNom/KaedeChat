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
    ) -> Channel:
        return await self.client.create_channel(
            self.ref,
            name,
            target=self.target,
            type=type,
            topic=topic,
            parent_id=parent_id,
            rate_limit_per_user=rate_limit_per_user,
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
    permissions: int = 0
    rate_limit_per_user: int = 0
    encryption_mode: str = "plaintext"
    search_available: bool = True
    version: str | None = None
    bot_installation_id: int | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Channel:
        return cls(
            client=client,
            target=target,
            ref=EntityRef(int(payload["id"]), str(payload["origin_domain"])),
            guild_ref=_optional_ref(payload, "guild_id", "guild_domain"),
            type=int(payload.get("type", 0)),
            name=str(payload["name"]) if payload.get("name") is not None else None,
            topic=str(payload["topic"]) if payload.get("topic") is not None else None,
            position=int(payload.get("position", 0)),
            parent_ref=_optional_ref(payload, "parent_id", "parent_domain"),
            permissions=int(payload.get("permissions", 0)),
            rate_limit_per_user=int(payload.get("rate_limit_per_user", 0)),
            encryption_mode=str(payload.get("encryption_mode", "plaintext")),
            search_available=bool(payload.get("search_available", True)),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
            bot_installation_id=(
                int(payload["bot_installation_id"])
                if payload.get("bot_installation_id") is not None
                else None
            ),
        )

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
            federated_history_policy=federated_history_policy,
            sync_permissions=sync_permissions,
        )

    async def delete(self) -> None:
        if self.guild_ref is None:
            raise ValueError(
                "direct-message channels cannot be deleted through guild management"
            )
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
    created_at: datetime
    attachments: list[Attachment]
    content_unavailable: bool = False
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    referenced_message_ref: EntityRef | None = None
    flags: int = 0
    pinned_at: datetime | None = None
    bot_installation_id: int | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Message:
        channel = payload.get("channel") or {}
        author = payload.get("author")
        channel_id = channel.get("id", payload.get("channel_id"))
        channel_domain = channel.get("origin_domain", payload.get("channel_domain"))
        if channel_id is None or not isinstance(channel_domain, str):
            raise ValueError(
                "message payload is missing its composite channel reference"
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
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            attachments=[
                Attachment.from_payload(client, target, item)
                for item in payload.get("attachments") or []
                if isinstance(item, dict)
            ],
            content_unavailable=bool(payload.get("content_unavailable", False)),
            edited_at=_datetime(payload.get("edited_at")),
            deleted_at=_datetime(payload.get("deleted_at")),
            referenced_message_ref=_optional_ref(
                payload, "referenced_message_id", "referenced_message_domain"
            ),
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
