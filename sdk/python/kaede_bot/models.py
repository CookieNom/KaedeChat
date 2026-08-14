from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TYPE_CHECKING

from .refs import EntityRef, User

if TYPE_CHECKING:
    from .client import Client


@dataclass(slots=True)
class Message:
    client: "Client"
    ref: EntityRef
    channel_ref: EntityRef
    author: User | None
    content: str | None
    created_at: datetime
    attachments: list[dict[str, Any]]
    content_unavailable: bool = False

    @classmethod
    def from_payload(cls, client: "Client", payload: dict[str, Any]) -> "Message":
        channel = payload.get("channel") or {}
        author = payload.get("author")
        channel_id = channel.get("id", payload.get("channel_id"))
        channel_domain = channel.get("origin_domain", payload.get("channel_domain"))
        if channel_id is None or not isinstance(channel_domain, str):
            raise ValueError(
                "message payload is missing its composite channel reference"
            )
        return cls(
            client,
            EntityRef(int(payload["id"]), str(payload["origin_domain"])),
            EntityRef(
                int(channel_id),
                channel_domain,
            ),
            User.from_payload(author) if isinstance(author, dict) else None,
            payload.get("content"),
            datetime.fromisoformat(payload["created_at"]),
            list(payload.get("attachments") or []),
            bool(payload.get("content_unavailable", False)),
        )

    async def reply(self, content: str) -> "Message":
        return await self.client.send_message(
            self.channel_ref, content, reply_to=self.ref
        )

    async def add_reaction(self, emoji: str) -> None:
        await self.client.add_reaction(self.channel_ref, self.ref, emoji)


@dataclass(slots=True)
class Interaction:
    client: "Client"
    id: int
    application_ref: EntityRef
    channel_ref: EntityRef
    user: User
    command: dict[str, Any]
    options: dict[str, Any] | None
    encrypted_payload: dict[str, Any] | None

    @classmethod
    def from_payload(cls, client: "Client", payload: dict[str, Any]) -> "Interaction":
        return cls(
            client,
            int(payload["id"]),
            EntityRef.parse(payload["application_ref"]),
            EntityRef.parse(payload["channel_ref"]),
            User.from_payload(payload["user"]),
            payload["command"],
            payload.get("options"),
            payload.get("encrypted_payload"),
        )

    async def defer(self) -> None:
        await self.client.request("POST", f"/api/v1/bots/interactions/{self.id}/defer")

    async def respond(
        self, content: str, *, e2ee: dict[str, Any] | None = None
    ) -> Message:
        payload: dict[str, Any] = {"content": content}
        if e2ee is not None:
            payload = {"content": None, "e2ee": e2ee}
        raw = await self.client.request(
            "POST",
            f"/api/v1/bots/interactions/{self.id}/response",
            json={"message": payload},
        )
        return Message.from_payload(self.client, raw)
