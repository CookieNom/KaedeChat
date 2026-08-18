from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from typing import Any, TypeVar, cast
from urllib.parse import urljoin, urlsplit

import httpx
from websockets.asyncio.client import connect

from .errors import ApiError, Forbidden, NotFound, RateLimited
from .intents import Intents
from .models import (
    Attachment,
    Ban,
    Channel,
    ChannelDeleteEvent,
    Emoji,
    EmojiDeleteEvent,
    Guild,
    GuildDeleteEvent,
    Interaction,
    Invite,
    MISSING,
    Member,
    MemberRemoveEvent,
    Message,
    MessageDeleteEvent,
    MissingType,
    PinEvent,
    PresenceEvent,
    RawEvent,
    ReactionEvent,
    ReadyEvent,
    Role,
    RoleDeleteEvent,
    TypingEvent,
    VoiceOccupancy,
    VoiceStateEvent,
    Webhook,
)
from .refs import EntityRef, User
from .state import WorkerState, canonical_application_home, canonical_target_origin

Handler = Callable[..., Awaitable[None]]
Check = Callable[[object], bool]
T = TypeVar("T")


_EVENT_ALIASES = {
    "MESSAGE": "MESSAGE_CREATE",
    "MESSAGE_EDIT": "MESSAGE_UPDATE",
    "REACTION_ADD": "MESSAGE_REACTION_ADD",
    "REACTION_REMOVE": "MESSAGE_REACTION_REMOVE",
    "MEMBER_JOIN": "GUILD_MEMBER_ADD",
    "MEMBER_UPDATE": "GUILD_MEMBER_UPDATE",
    "MEMBER_REMOVE": "GUILD_MEMBER_REMOVE",
    "GUILD_JOIN": "GUILD_CREATE",
    "GUILD_REMOVE": "GUILD_DELETE",
    "ROLE_CREATE": "GUILD_ROLE_CREATE",
    "ROLE_UPDATE": "GUILD_ROLE_UPDATE",
    "ROLE_DELETE": "GUILD_ROLE_DELETE",
    "INTERACTION": "INTERACTION_CREATE",
    "TYPING": "TYPING_START",
    "PRESENCE": "PRESENCE_UPDATE",
    "VOICE_STATE": "VOICE_STATE_UPDATE",
}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def event_name(value: str) -> str:
    normalized = value.removeprefix("on_").upper()
    return _EVENT_ALIASES.get(normalized, normalized)


def _guild_ref_from_topic(topic: str | None) -> EntityRef | None:
    if topic is None or not topic.startswith("guild:"):
        return None
    parts = topic.split(":", 2)
    if len(parts) != 3 or not parts[1]:
        return None
    try:
        guild_id = int(parts[2])
    except ValueError:
        return None
    return EntityRef(guild_id, parts[1]) if guild_id >= 0 else None


def _provided_fields(**values: object) -> dict[str, object]:
    return {
        name: value
        for name, value in values.items()
        if not isinstance(value, MissingType)
    }


def _version_headers(version: str | None) -> dict[str, str]:
    if not version:
        raise ValueError("the current resource version is required for this update")
    return {"If-Match": version}


class Client:
    def __init__(self, *, worker_state: WorkerState, intents: Intents | None = None):
        self.worker_state = worker_state
        self.intents = intents or Intents.default()
        self._targets: dict[str, httpx.AsyncClient] = {}
        self._tokens: dict[str, tuple[str, float]] = {}
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._commands: list[dict[str, Any]] = []
        self._cursors: dict[str, dict[str, int]] = defaultdict(dict)
        self._waiters: dict[str, list[tuple[asyncio.Future[object], Check | None]]] = (
            defaultdict(list)
        )
        self._stopping = False
        self._sockets: set[Any] = set()
        self._cursor_lock = asyncio.Lock()
        for target, cursors in self.worker_state.load_cursors().items():
            self._cursors[target].update(cursors)

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def event(self, function: Handler) -> Handler:
        self._handlers[event_name(function.__name__)].append(function)
        return function

    def listen(self, name: str | None = None) -> Callable[[Handler], Handler]:
        def decorator(function: Handler) -> Handler:
            self._handlers[event_name(name or function.__name__)].append(function)
            return function

        return decorator

    def remove_listener(self, function: Handler, name: str | None = None) -> None:
        listeners = self._handlers.get(event_name(name or function.__name__), [])
        if function in listeners:
            listeners.remove(function)

    async def wait_for(
        self,
        name: str,
        *,
        check: Check | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - public discord.py-compatible API
    ) -> object:
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        key = event_name(name)
        waiter = (future, check)
        self._waiters[key].append(waiter)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            if waiter in self._waiters.get(key, []):
                self._waiters[key].remove(waiter)

    def command(
        self,
        *,
        name: str | None = None,
        description: str = "",
        type: str = "chat_input",
        default_member_permissions: list[str] | None = None,
        contexts: list[str] | None = None,
        options: list[dict[str, Any]] | None = None,
    ) -> Callable[[Handler], Handler]:
        def decorator(function: Handler) -> Handler:
            command_name = name or function.__name__.lower()
            self._commands.append(
                {
                    "name": command_name,
                    "type": type,
                    "description": description,
                    "default_member_permissions": default_member_permissions or [],
                    "contexts": contexts or ["guild"],
                    "options": options or [],
                }
            )
            self._handlers[f"COMMAND:{command_name}"].append(function)
            return function

        return decorator

    async def sync_commands(self, *, application_home: str, control_token: str) -> None:
        origin = canonical_application_home(
            application_home, self.worker_state.application_ref
        )
        async with httpx.AsyncClient(
            base_url=origin,
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            response = await http.put(
                f"/api/v1/bot-control/applications/{self.worker_state.application_ref}/commands",
                headers={"Authorization": f"BotControl {control_token}"},
                json={"commands": self._commands},
            )
            response.raise_for_status()

    async def add_target(self, base_url: str) -> str:
        origin = canonical_target_origin(base_url)
        if origin not in self._targets:
            self._targets[origin] = httpx.AsyncClient(base_url=origin, timeout=30)
            await self._token(origin, force=True)
        return origin

    def _target(self, target: str | None) -> str:
        if target is None:
            if len(self._targets) != 1:
                raise ValueError(
                    "target is required when the client has zero or multiple instances"
                )
            return next(iter(self._targets))
        return canonical_target_origin(target)

    def _sign(self, payload: bytes) -> str:
        return _b64(self.worker_state.private_key.sign(payload))

    async def _token(self, origin: str, *, force: bool = False) -> str:
        cached = self._tokens.get(origin)
        if cached and cached[1] - 30 > time.time() and not force:
            return cached[0]
        now = int(time.time())
        expiry = now + 60
        nonce = secrets.token_urlsafe(24)
        audience = f"{origin}/api/v1/bots/token"
        assertion = (
            f"kaede-worker-assertion-v1\n{self.worker_state.application_ref}\n"
            f"{self.worker_state.worker_id}\n{audience}\n{now}\n{expiry}\n{nonce}"
        ).encode()
        response = await self._targets[origin].post(
            "/api/v1/bots/token",
            json={
                "application_ref": str(self.worker_state.application_ref),
                "worker_id": self.worker_state.worker_id,
                "audience": audience,
                "issued_at": now,
                "expires_at": expiry,
                "nonce": nonce,
                "signature": self._sign(assertion),
            },
        )
        await self._raise(response)
        data = response.json()
        self._tokens[origin] = (
            data["access_token"],
            time.time() + int(data["expires_in"]),
        )
        return cast(str, data["access_token"])

    def _proof_headers(self, method: str, target: str, token: str) -> dict[str, str]:
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        payload = (
            f"kaede-dpop-v1\n{method.upper()}\n{target}\n{timestamp}\n{nonce}\n{digest}"
        ).encode()
        return {
            "Authorization": f"Bot {token}",
            "X-Kaede-Bot-Timestamp": str(timestamp),
            "X-Kaede-Bot-Nonce": nonce,
            "X-Kaede-Bot-Proof": self._sign(payload),
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        target: str | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        origin = self._target(target)
        if origin not in self._targets:
            await self.add_target(origin)
        signed_target = path
        if params:
            signed_target = f"{path}?{httpx.QueryParams(params)}"
        force_token = False
        for attempt in range(3):
            token = await self._token(origin, force=force_token)
            request_headers = dict(headers or {})
            request_headers.update(self._proof_headers(method, signed_target, token))
            response = await self._targets[origin].request(
                method,
                path,
                json=json,
                params=params,
                headers=request_headers,
            )
            if response.status_code == 401 and attempt == 0:
                force_token = True
                continue
            force_token = False
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(
                    min(
                        30.0,
                        max(0.05, float(response.headers.get("Retry-After", "1"))),
                    )
                )
                continue
            await self._raise(response)
            return None if response.status_code == 204 else response.json()
        raise ApiError(
            503, "BOT_REQUEST_RETRY_EXHAUSTED", "Bot request retries were exhausted"
        )

    async def _redirect_location(
        self,
        path: str,
        *,
        target: str | None = None,
    ) -> str:
        """Resolve an authenticated API redirect without forwarding bot proofs.

        Media is served through short-lived object-storage URLs. The bot token
        and proof headers are used only against the Kaede origin and are never
        copied to the redirected host.
        """

        origin = self._target(target)
        if origin not in self._targets:
            await self.add_target(origin)
        force_token = False
        for attempt in range(3):
            token = await self._token(origin, force=force_token)
            response = await self._targets[origin].get(
                path,
                headers=self._proof_headers("GET", path, token),
                follow_redirects=False,
            )
            if response.status_code == 401 and attempt == 0:
                force_token = True
                continue
            force_token = False
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(
                    min(
                        30.0,
                        max(0.05, float(response.headers.get("Retry-After", "1"))),
                    )
                )
                continue
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise ApiError(
                        502, "MEDIA_REDIRECT_INVALID", "Media redirect is missing"
                    )
                resolved = urljoin(origin, location)
                parsed = urlsplit(resolved)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.fragment
                ):
                    raise ApiError(
                        502,
                        "MEDIA_REDIRECT_INVALID",
                        "Media redirect is not a safe HTTPS URL",
                    )
                return resolved
            await self._raise(response)
            raise ApiError(
                502, "MEDIA_REDIRECT_INVALID", "Media endpoint did not redirect"
            )
        raise ApiError(
            503, "BOT_REQUEST_RETRY_EXHAUSTED", "Bot request retries were exhausted"
        )

    async def _raise(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            body = response.json()
            detail = body.get("detail", {}) if isinstance(body, dict) else {}
        except ValueError:
            detail = {}
        code = (
            str(detail.get("code", "KAEDE_API_ERROR"))
            if isinstance(detail, dict)
            else "KAEDE_API_ERROR"
        )
        message = (
            str(detail.get("message") or code.replace("_", " ").title())
            if isinstance(detail, dict)
            else code.replace("_", " ").title()
        )
        if response.status_code == 429:
            raise RateLimited(
                429,
                code,
                message,
                float(response.headers.get("Retry-After", "1")),
                detail,
            )
        error_type = (
            Forbidden
            if response.status_code == 403
            else NotFound
            if response.status_code == 404
            else ApiError
        )
        raise error_type(response.status_code, code, message, detail)

    async def fetch_user(self, ref: EntityRef, *, target: str | None = None) -> User:
        return User.from_payload(
            await self.request("GET", f"/api/v1/bots/users/{ref}", target=target)
        )

    async def fetch_guilds(self, *, target: str | None = None) -> list[Guild]:
        origin = self._target(target)
        raw = await self.request("GET", "/api/v1/bots/guilds", target=origin)
        return [Guild.from_payload(self, origin, item) for item in raw]

    async def fetch_guild(
        self, guild: EntityRef, *, target: str | None = None
    ) -> Guild:
        origin = self._target(target)
        raw = await self.request("GET", f"/api/v1/bots/guilds/{guild}", target=origin)
        return Guild.from_payload(self, origin, raw)

    async def fetch_channels(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Channel]:
        origin = self._target(target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/channels", target=origin
        )
        return [Channel.from_payload(self, origin, item) for item in raw]

    async def fetch_channel(
        self, channel: EntityRef, *, target: str | None = None
    ) -> Channel:
        origin = self._target(target)
        raw = await self.request(
            "GET", f"/api/v1/bots/channels/{channel}", target=origin
        )
        return Channel.from_payload(self, origin, raw)

    async def fetch_members(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        limit: int = 100,
        after: EntityRef | None = None,
        query: str | None = None,
    ) -> list[Member]:
        origin = self._target(target)
        params: dict[str, Any] = {"limit": min(1000, max(1, limit))}
        if after is not None:
            params["after"] = str(after)
        if query is not None:
            params["query"] = query
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/members",
            target=origin,
            params=params,
        )
        return [Member.from_payload(self, origin, item) for item in raw]

    async def fetch_roles(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Role]:
        origin = self._target(target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/roles", target=origin
        )
        return [Role.from_payload(self, origin, item) for item in raw]

    async def edit_guild(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
        name: str | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        federated_history_policy: str | MissingType = MISSING,
    ) -> Guild:
        origin = self._target(target)
        body = _provided_fields(
            name=name,
            description=description,
            federated_history_policy=federated_history_policy,
        )
        if not body:
            raise ValueError("at least one guild field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}",
            target=origin,
            json=body,
            headers=_version_headers(version),
        )
        return Guild.from_payload(self, origin, raw)

    async def create_channel(
        self,
        guild: EntityRef,
        name: str,
        *,
        target: str | None = None,
        type: int = 0,
        topic: str | None = None,
        parent_id: int | None = None,
        rate_limit_per_user: int = 0,
    ) -> Channel:
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/channels",
            target=origin,
            json={
                "name": name,
                "type": type,
                "topic": topic,
                "parent_id": str(parent_id) if parent_id is not None else None,
                "rate_limit_per_user": rate_limit_per_user,
            },
        )
        return Channel.from_payload(self, origin, raw)

    async def edit_channel(
        self,
        guild: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
        name: str | MissingType = MISSING,
        topic: str | None | MissingType = MISSING,
        parent_id: int | None | MissingType = MISSING,
        rate_limit_per_user: int | MissingType = MISSING,
        federated_history_policy: str | MissingType = MISSING,
        sync_permissions: bool | MissingType = MISSING,
    ) -> Channel:
        origin = self._target(target)
        body = _provided_fields(
            name=name,
            topic=topic,
            parent_id=(str(parent_id) if isinstance(parent_id, int) else parent_id),
            rate_limit_per_user=rate_limit_per_user,
            federated_history_policy=federated_history_policy,
            sync_permissions=sync_permissions,
        )
        if not body:
            raise ValueError("at least one channel field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}",
            target=origin,
            json=body,
            headers=_version_headers(version),
        )
        return Channel.from_payload(self, origin, raw)

    async def reorder_channels(
        self,
        guild: EntityRef,
        positions: list[tuple[EntityRef, int, int | None, bool]],
        *,
        target: str | None = None,
    ) -> list[Channel]:
        origin = self._target(target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/channels",
            target=origin,
            json={
                "channels": [
                    {
                        "id": str(channel.id),
                        "position": position,
                        "parent_id": str(parent_id) if parent_id is not None else None,
                        "sync_permissions": sync_permissions,
                    }
                    for channel, position, parent_id, sync_permissions in positions
                ]
            },
        )
        return [Channel.from_payload(self, origin, item) for item in raw]

    async def delete_channel(
        self,
        guild: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}",
            target=target,
        )

    async def create_role(
        self,
        guild: EntityRef,
        name: str,
        *,
        target: str | None = None,
        permissions: int = 0,
        color: int = 0,
        hoist: bool = False,
        mentionable: bool = False,
    ) -> Role:
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/roles",
            target=origin,
            json={
                "name": name,
                "permissions": str(permissions),
                "color": color,
                "hoist": hoist,
                "mentionable": mentionable,
            },
        )
        return Role.from_payload(self, origin, raw)

    async def edit_role(
        self,
        guild: EntityRef,
        role: EntityRef,
        *,
        target: str | None = None,
        version: str | None,
        name: str | MissingType = MISSING,
        permissions: int | MissingType = MISSING,
        color: int | MissingType = MISSING,
        hoist: bool | MissingType = MISSING,
        mentionable: bool | MissingType = MISSING,
    ) -> Role:
        origin = self._target(target)
        body = _provided_fields(
            name=name,
            permissions=(
                str(permissions) if isinstance(permissions, int) else permissions
            ),
            color=color,
            hoist=hoist,
            mentionable=mentionable,
        )
        if not body:
            raise ValueError("at least one role field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/roles/{role}",
            target=origin,
            json=body,
            headers=_version_headers(version),
        )
        return Role.from_payload(self, origin, raw)

    async def reorder_roles(
        self,
        guild: EntityRef,
        positions: list[tuple[Role, int]],
        *,
        target: str | None = None,
    ) -> list[Role]:
        origin = self._target(target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/roles",
            target=origin,
            json={
                "roles": [
                    {
                        "id": str(role.ref.id),
                        "position": position,
                        "version": _version_headers(role.version)["If-Match"],
                    }
                    for role, position in positions
                ]
            },
        )
        return [Role.from_payload(self, origin, item) for item in raw]

    async def delete_role(
        self,
        guild: EntityRef,
        role: EntityRef,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/roles/{role}",
            target=target,
        )

    async def add_member_role(
        self,
        guild: EntityRef,
        user: EntityRef,
        role: EntityRef,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/members/{user}/roles/{role}",
            target=target,
        )

    async def remove_member_role(
        self,
        guild: EntityRef,
        user: EntityRef,
        role: EntityRef,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/members/{user}/roles/{role}",
            target=target,
        )

    async def set_member_roles(
        self,
        guild: EntityRef,
        user: EntityRef,
        roles: list[EntityRef],
        *,
        target: str | None = None,
    ) -> Member:
        origin = self._target(target)
        raw = await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/members/{user}/roles",
            target=origin,
            json={"role_ids": [str(role) for role in roles]},
        )
        return Member.from_payload(self, origin, raw)

    async def upload_attachment(
        self,
        channel: EntityRef,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
        encryption_mode: str = "plaintext",
        encryption_protocol: str | None = None,
    ) -> Attachment:
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/attachments",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": encryption_mode,
                "encryption_protocol": encryption_protocol,
            },
        )
        attachment = Attachment.from_payload(self, origin, raw)
        if not attachment.upload_url:
            raise ApiError(502, "UPLOAD_TICKET_INVALID", "Upload ticket has no URL")
        parsed = urlsplit(attachment.upload_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ApiError(502, "UPLOAD_TICKET_INVALID", "Upload URL is not safe HTTPS")
        async with httpx.AsyncClient(
            timeout=60,
            follow_redirects=False,
            trust_env=False,
        ) as upload_client:
            response = await upload_client.put(
                attachment.upload_url,
                content=data,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(data)),
                },
            )
        if response.is_redirect:
            raise ApiError(
                502, "UPLOAD_REDIRECT_REJECTED", "Upload URL redirected unexpectedly"
            )
        response.raise_for_status()
        return attachment

    async def fetch_attachment(
        self, attachment: EntityRef, *, target: str | None = None
    ) -> Attachment:
        origin = self._target(target)
        raw = await self.request(
            "GET", f"/api/v1/bots/attachments/{attachment}", target=origin
        )
        return Attachment.from_payload(self, origin, raw)

    async def download_attachment(
        self,
        attachment: EntityRef,
        *,
        variant: str = "original",
        target: str | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        if variant not in {
            "original",
            "thumbnail_128",
            "thumbnail_512",
            "thumbnail_1024",
            "poster",
        }:
            raise ValueError("unsupported attachment variant")
        location = await self._redirect_location(
            f"/api/v1/bots/attachments/{attachment}/{variant}", target=target
        )
        async with httpx.AsyncClient(
            timeout=60,
            follow_redirects=False,
            trust_env=False,
        ) as media_client:
            async with media_client.stream("GET", location) as response:
                if response.is_redirect:
                    raise ApiError(
                        502,
                        "MEDIA_REDIRECT_INVALID",
                        "Object storage redirected unexpectedly",
                    )
                response.raise_for_status()
                declared = response.headers.get("Content-Length")
                if max_bytes is not None and declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        raise ApiError(
                            502,
                            "MEDIA_RESPONSE_INVALID",
                            "Object storage returned an invalid content length",
                        ) from None
                    if declared_size > max_bytes:
                        raise ValueError("attachment exceeds max_bytes")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if max_bytes is None:
                        body.extend(chunk)
                        continue
                    # Retain at most the single byte needed to prove the
                    # configured limit was exceeded, then close the stream.
                    body.extend(chunk[: max_bytes + 1 - len(body)])
                    if len(body) > max_bytes:
                        raise ValueError("attachment exceeds max_bytes")
                return bytes(body)

    async def open_dm(
        self,
        handle: str,
        *,
        installation_id: int,
        target: str | None = None,
    ) -> Channel:
        origin = self._target(target)
        raw = await self.request(
            "POST",
            "/api/v1/bots/dms",
            target=origin,
            json={"handle": handle},
            headers={"X-Kaede-Bot-Installation": str(installation_id)},
        )
        return Channel.from_payload(self, origin, raw)

    async def create_invite(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        channel_id: int | None = None,
        max_uses: int | None = None,
        max_age_seconds: int | None = 86_400,
    ) -> Invite:
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/invites",
            target=origin,
            json={
                "channel_id": str(channel_id) if channel_id is not None else None,
                "max_uses": max_uses,
                "max_age_seconds": max_age_seconds,
            },
        )
        return Invite.from_payload(self, origin, raw)

    async def invites(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Invite]:
        origin = self._target(target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/invites", target=origin
        )
        return [Invite.from_payload(self, origin, item) for item in raw]

    async def revoke_invite(
        self,
        guild: EntityRef,
        code: str,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/invites/{code}",
            target=target,
        )

    async def create_webhook(
        self,
        guild: EntityRef,
        channel: EntityRef,
        name: str,
        *,
        target: str | None = None,
    ) -> Webhook:
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/channels/{channel}/webhooks",
            target=origin,
            json={"name": name},
        )
        return Webhook.from_payload(self, origin, raw)

    async def webhooks(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Webhook]:
        origin = self._target(target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/webhooks", target=origin
        )
        return [Webhook.from_payload(self, origin, item) for item in raw]

    async def edit_webhook(
        self,
        guild: EntityRef,
        webhook_id: int,
        *,
        target: str | None = None,
        name: str | MissingType = MISSING,
        avatar_hash: str | None | MissingType = MISSING,
    ) -> Webhook:
        origin = self._target(target)
        body = _provided_fields(name=name, avatar_hash=avatar_hash)
        if not body:
            raise ValueError("at least one webhook field is required")
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}",
            target=origin,
            json=body,
        )
        return Webhook.from_payload(self, origin, raw)

    async def rotate_webhook(
        self,
        guild: EntityRef,
        webhook_id: int,
        *,
        target: str | None = None,
    ) -> Webhook:
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}/rotate",
            target=origin,
        )
        return Webhook.from_payload(self, origin, raw)

    async def delete_webhook(
        self,
        guild: EntityRef,
        webhook_id: int,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/webhooks/{webhook_id}",
            target=target,
        )

    async def emojis(
        self, guild: EntityRef, *, target: str | None = None
    ) -> list[Emoji]:
        origin = self._target(target)
        raw = await self.request(
            "GET", f"/api/v1/bots/guilds/{guild}/emojis", target=origin
        )
        return [Emoji.from_payload(self, origin, item) for item in raw]

    async def upload_emoji(
        self,
        guild: EntityRef,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        target: str | None = None,
    ) -> Attachment:
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/emojis/tickets",
            target=origin,
            json={
                "filename": filename,
                "content_type": content_type,
                "size": len(data),
                "encryption_mode": "plaintext",
                "encryption_protocol": None,
            },
        )
        attachment = Attachment.from_payload(self, origin, raw)
        if not attachment.upload_url:
            raise ApiError(502, "UPLOAD_TICKET_INVALID", "Upload ticket has no URL")
        parsed = urlsplit(attachment.upload_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ApiError(502, "UPLOAD_TICKET_INVALID", "Upload URL is not safe HTTPS")
        async with httpx.AsyncClient(
            timeout=60, follow_redirects=False, trust_env=False
        ) as upload_client:
            response = await upload_client.put(
                attachment.upload_url,
                content=data,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(data)),
                },
            )
        if response.is_redirect:
            raise ApiError(
                502, "UPLOAD_REDIRECT_REJECTED", "Upload URL redirected unexpectedly"
            )
        response.raise_for_status()
        return attachment

    async def commit_emoji(
        self,
        guild: EntityRef,
        attachment: EntityRef,
        name: str,
        *,
        target: str | None = None,
    ) -> Emoji | Attachment:
        origin = self._target(target)
        raw = await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/emojis",
            target=origin,
            json={"attachment_id": str(attachment.id), "name": name},
        )
        if isinstance(raw, dict) and raw.get("guild_id") is not None:
            return Emoji.from_payload(self, origin, raw)
        return Attachment.from_payload(self, origin, raw)

    async def delete_emoji(
        self,
        guild: EntityRef,
        emoji_id: int,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/emojis/{emoji_id}",
            target=target,
        )

    async def set_voice_moderation(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        server_mute: bool | None = None,
        server_deaf: bool | None = None,
        reason: str | None = None,
    ) -> None:
        if server_mute is None and server_deaf is None:
            raise ValueError("server_mute or server_deaf is required")
        await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/members/{user}/voice",
            target=target,
            json={"server_mute": server_mute, "server_deaf": server_deaf},
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def disconnect_voice(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/members/{user}/voice",
            target=target,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def move_voice(
        self,
        guild: EntityRef,
        user: EntityRef,
        channel: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        await self.request(
            "POST",
            f"/api/v1/bots/guilds/{guild}/members/{user}/voice/move",
            target=target,
            json={"channel_id": str(channel)},
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def send_message(
        self,
        channel: EntityRef,
        content: str | None = None,
        *,
        target: str | None = None,
        reply_to: EntityRef | None = None,
        attachment_ids: list[int] | None = None,
        e2ee: dict[str, Any] | None = None,
        installation_id: int | None = None,
    ) -> Message:
        origin = self._target(target)
        body: dict[str, Any] = {
            "content": content,
            "attachment_ids": attachment_ids or [],
            "allowed_mentions": {"parse": []},
        }
        if reply_to is not None:
            body["message_reference"] = {
                "message_id": str(reply_to.id),
                "message_domain": reply_to.domain,
            }
        if e2ee is not None:
            body["content"] = None
            body["e2ee"] = e2ee
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/messages",
            target=origin,
            json=body,
            headers=(
                {"X-Kaede-Bot-Installation": str(installation_id)}
                if installation_id is not None
                else None
            ),
        )
        message = Message.from_payload(self, origin, raw)
        message.bot_installation_id = installation_id
        return message

    async def history(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        before: EntityRef | None = None,
        after: EntityRef | None = None,
        around: EntityRef | None = None,
        limit: int = 50,
    ) -> list[Message]:
        origin = self._target(target)
        params: dict[str, Any] = {"limit": min(100, max(1, limit))}
        for name, value in (("before", before), ("after", after), ("around", around)):
            if value is not None:
                params[name] = str(value)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/messages",
            target=origin,
            params=params,
        )
        return [Message.from_payload(self, origin, item) for item in raw]

    async def edit_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        content: str,
        *,
        target: str | None = None,
    ) -> Message:
        origin = self._target(target)
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/channels/{channel}/messages/{message}",
            target=origin,
            json={"content": content},
        )
        return Message.from_payload(self, origin, raw)

    async def delete_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/messages/{message}",
            target=target,
        )

    async def bulk_delete_messages(
        self,
        channel: EntityRef,
        messages: list[EntityRef],
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/messages/bulk-delete",
            target=target,
            json={"message_ids": [str(item) for item in messages]},
        )

    async def add_reaction(
        self,
        channel: EntityRef,
        message: EntityRef,
        emoji: str,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions",
            target=target,
            json={"emoji": emoji},
        )

    async def remove_reaction(
        self,
        channel: EntityRef,
        message: EntityRef,
        emoji: str,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/{emoji}",
            target=target,
        )

    async def remove_user_reaction(
        self,
        channel: EntityRef,
        message: EntityRef,
        user: EntityRef,
        emoji: str,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/{emoji}/{user}",
            target=target,
        )

    async def reaction_users(
        self,
        channel: EntityRef,
        message: EntityRef,
        emoji: str,
        *,
        target: str | None = None,
        after: EntityRef | None = None,
        limit: int = 50,
    ) -> tuple[list[User], int, EntityRef | None]:
        params: dict[str, Any] = {"limit": min(100, max(1, limit))}
        if after is not None:
            params["after"] = str(after)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/{emoji}",
            target=target,
            params=params,
        )
        next_after = raw.get("next_after")
        return (
            [User.from_payload(item) for item in raw.get("items", [])],
            int(raw.get("total", 0)),
            EntityRef.parse(next_after) if isinstance(next_after, str) else None,
        )

    async def pins(
        self, channel: EntityRef, *, target: str | None = None
    ) -> list[Message]:
        origin = self._target(target)
        raw = await self.request(
            "GET", f"/api/v1/bots/channels/{channel}/pins", target=origin
        )
        return [Message.from_payload(self, origin, item) for item in raw]

    async def pin_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "PUT",
            f"/api/v1/bots/channels/{channel}/pins/{message}",
            target=target,
        )

    async def unpin_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        *,
        target: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/channels/{channel}/pins/{message}",
            target=target,
        )

    async def trigger_typing(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        installation_id: int | None = None,
    ) -> None:
        await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/typing",
            target=target,
            headers=(
                {"X-Kaede-Bot-Installation": str(installation_id)}
                if installation_id is not None
                else None
            ),
        )

    async def edit_member(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        nickname: str | None | MissingType = MISSING,
        timeout_until: datetime | None | MissingType = MISSING,
        timeout_indefinite: bool | MissingType = MISSING,
        reason: str | None = None,
    ) -> Member:
        origin = self._target(target)
        body: dict[str, Any] = {}
        if not isinstance(nickname, MissingType):
            body["nickname"] = nickname
        if not isinstance(timeout_until, MissingType):
            body["timeout_until"] = (
                timeout_until.isoformat() if timeout_until is not None else None
            )
        if not isinstance(timeout_indefinite, MissingType):
            body["timeout_indefinite"] = timeout_indefinite
        if not body:
            raise ValueError("at least one member field is required")
        headers = {"X-Audit-Log-Reason": reason} if reason else None
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/guilds/{guild}/members/{user}",
            target=origin,
            json=body,
            headers=headers,
        )
        return Member.from_payload(self, origin, raw)

    async def kick_member(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/members/{user}",
            target=target,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def ban_member(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
        delete_message_seconds: int = 0,
        expires_at: datetime | None = None,
    ) -> None:
        await self.request(
            "PUT",
            f"/api/v1/bots/guilds/{guild}/bans/{user}",
            target=target,
            json={
                "reason": reason,
                "delete_message_seconds": delete_message_seconds,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def unban_member(
        self,
        guild: EntityRef,
        user: EntityRef,
        *,
        target: str | None = None,
        reason: str | None = None,
    ) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/bots/guilds/{guild}/bans/{user}",
            target=target,
            headers={"X-Audit-Log-Reason": reason} if reason else None,
        )

    async def bans(
        self,
        guild: EntityRef,
        *,
        target: str | None = None,
        after: EntityRef | None = None,
        limit: int = 50,
    ) -> list[Ban]:
        origin = self._target(target)
        params: dict[str, Any] = {"limit": min(1000, max(1, limit))}
        if after is not None:
            params["after"] = str(after)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/guilds/{guild}/bans",
            target=origin,
            params=params,
        )
        return [Ban.from_payload(self, origin, item) for item in raw]

    async def voice_occupancy(
        self, channel: EntityRef, *, target: str | None = None
    ) -> VoiceOccupancy:
        raw = await self.request(
            "GET", f"/api/v1/bots/channels/{channel}/voice/occupancy", target=target
        )
        participants = raw.get("participants", raw.get("occupants", []))
        return VoiceOccupancy(
            channel_ref=channel,
            participants=tuple(item for item in participants if isinstance(item, dict)),
            generated_at=(
                int(raw["generated_at"])
                if raw.get("generated_at") is not None
                else None
            ),
        )

    def _event_model(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        target: str,
        topic: str | None,
        sequence: int,
    ) -> object:
        if event_type == "INTERACTION_CREATE":
            return Interaction.from_payload(self, target, data)
        if event_type in {"MESSAGE_CREATE", "MESSAGE_UPDATE"} and "created_at" in data:
            return Message.from_payload(self, target, data)
        if event_type == "MESSAGE_DELETE":
            return MessageDeleteEvent(
                target,
                EntityRef(int(data["id"]), str(data["origin_domain"])),
                EntityRef(int(data["channel_id"]), str(data["channel_domain"])),
            )
        if event_type in {"MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"}:
            return ReactionEvent(
                target,
                EntityRef(int(data["id"]), str(data["origin_domain"])),
                EntityRef(int(data["channel_id"]), str(data["channel_domain"])),
                EntityRef(int(data["user_id"]), str(data["user_domain"])),
                str(data["reaction"]),
            )
        if event_type == "MESSAGE_PIN_UPDATE":
            return PinEvent(
                target,
                EntityRef(int(data["id"]), str(data["origin_domain"])),
                EntityRef(int(data["channel_id"]), str(data["channel_domain"])),
                bool(data["pinned"]),
            )
        if event_type == "READY":
            return ReadyEvent(
                target,
                EntityRef.parse(str(data["application_ref"])),
                int(data["worker_id"]),
                tuple(data.get("installations") or ()),
                tuple(str(item) for item in data.get("intents") or ()),
            )
        if event_type in {"GUILD_CREATE", "GUILD_UPDATE"} and "name" in data:
            return Guild.from_payload(self, target, data)
        if event_type == "GUILD_DELETE":
            return GuildDeleteEvent(
                target,
                EntityRef(int(data["id"]), str(data["origin_domain"])),
            )
        if event_type in {"CHANNEL_CREATE", "CHANNEL_UPDATE"} and "type" in data:
            return Channel.from_payload(self, target, data)
        if event_type == "CHANNEL_DELETE":
            return ChannelDeleteEvent(
                target,
                EntityRef(int(data["id"]), str(data["origin_domain"])),
                (
                    EntityRef(int(data["guild_id"]), str(data["guild_domain"]))
                    if data.get("guild_id") is not None
                    and data.get("guild_domain") is not None
                    else None
                ),
            )
        if event_type in {"GUILD_ROLE_CREATE", "GUILD_ROLE_UPDATE"} and "name" in data:
            return Role.from_payload(self, target, data)
        if event_type == "GUILD_ROLE_DELETE":
            return RoleDeleteEvent(
                target,
                EntityRef(int(data["id"]), str(data["origin_domain"])),
                EntityRef(int(data["guild_id"]), str(data["guild_domain"])),
            )
        if event_type == "GUILD_EMOJI_CREATE" and data.get("name") is not None:
            return Emoji.from_payload(self, target, data)
        if event_type == "GUILD_EMOJI_DELETE":
            return EmojiDeleteEvent(
                target,
                EntityRef(int(data["id"]), str(data["origin_domain"])),
                EntityRef(int(data["guild_id"]), str(data["guild_domain"])),
            )
        if event_type in {"GUILD_MEMBER_ADD", "GUILD_MEMBER_UPDATE"} and isinstance(
            data.get("user"), dict
        ):
            return Member.from_payload(self, target, data)
        if event_type == "GUILD_MEMBER_REMOVE":
            return MemberRemoveEvent(
                target,
                EntityRef(int(data["guild_id"]), str(data["guild_domain"])),
                EntityRef(int(data["user_id"]), str(data["user_domain"])),
            )
        if event_type == "TYPING_START":
            return TypingEvent(
                target,
                EntityRef(int(data["channel_id"]), str(data["channel_domain"])),
                EntityRef(int(data["user_id"]), str(data["user_domain"])),
                int(data["timestamp"]),
            )
        if event_type == "PRESENCE_UPDATE" and all(
            key in data for key in ("user_id", "user_domain", "status")
        ):
            topic_guild_ref = _guild_ref_from_topic(topic)
            return PresenceEvent(
                target=target,
                user_ref=EntityRef(int(data["user_id"]), str(data["user_domain"])),
                status=str(data["status"]),
                custom_status=(
                    str(data["custom_status"])
                    if data.get("custom_status") is not None
                    else None
                ),
                raw=data,
                guild_ref=(
                    EntityRef(int(data["guild_id"]), str(data["guild_domain"]))
                    if data.get("guild_id") is not None
                    and data.get("guild_domain") is not None
                    else topic_guild_ref
                ),
            )
        if event_type == "VOICE_STATE_UPDATE":
            topic_guild_ref = _guild_ref_from_topic(topic)
            guild_domain = (
                data.get("guild_domain")
                or data.get("channel_domain")
                or (topic_guild_ref.domain if topic_guild_ref is not None else None)
            )
            user_domain = data.get("user_domain")
            channel_domain = data.get("channel_domain") or guild_domain
            participants = data.get("participants")
            return VoiceStateEvent(
                target=target,
                guild_ref=(
                    EntityRef(int(data["guild_id"]), str(guild_domain))
                    if data.get("guild_id") is not None and guild_domain is not None
                    else topic_guild_ref
                ),
                channel_ref=(
                    EntityRef(int(data["channel_id"]), str(channel_domain))
                    if data.get("channel_id") is not None and channel_domain is not None
                    else None
                ),
                user_ref=(
                    EntityRef(int(data["user_id"]), str(user_domain))
                    if data.get("user_id") is not None and user_domain is not None
                    else None
                ),
                connected=(
                    bool(data["connected"])
                    if data.get("connected") is not None
                    else None
                ),
                self_mute=(
                    bool(data["self_mute"])
                    if data.get("self_mute") is not None
                    else None
                ),
                self_deaf=(
                    bool(data["self_deaf"])
                    if data.get("self_deaf") is not None
                    else None
                ),
                server_mute=(
                    bool(data["server_mute"])
                    if data.get("server_mute") is not None
                    else None
                ),
                server_deaf=(
                    bool(data["server_deaf"])
                    if data.get("server_deaf") is not None
                    else None
                ),
                participants=tuple(
                    item for item in participants if isinstance(item, dict)
                )
                if isinstance(participants, list)
                else (),
                heartbeat=bool(data.get("heartbeat", False)),
                raw=data,
            )
        return RawEvent(target, event_type, data, topic, sequence)

    async def _report_handler_error(
        self, event_type: str, target: str, error: Exception
    ) -> None:
        if event_type == "ERROR":
            return
        payload = RawEvent(
            target,
            "ERROR",
            {"event_type": event_type, "error": error},
        )
        for handler in tuple(self._handlers.get("ERROR", [])):
            with suppress(Exception):
                await handler(payload)

    async def dispatch(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        target: str | None = None,
        topic: str | None = None,
        sequence: int = 0,
    ) -> None:
        event_type = event_name(event_type)
        origin = self._target(target)
        model = self._event_model(
            event_type, data, target=origin, topic=topic, sequence=sequence
        )
        for future, check in tuple(self._waiters.get(event_type, [])):
            if future.done():
                continue
            try:
                accepted = check is None or check(model)
            except Exception as exc:
                future.set_exception(exc)
                continue
            if accepted:
                future.set_result(model)
        handlers: list[Handler] = []
        if isinstance(model, Interaction):
            handlers.extend(self._handlers.get(f"COMMAND:{model.command['name']}", []))
        handlers.extend(self._handlers.get(event_type, []))
        for handler in tuple(handlers):
            try:
                await handler(model)
            except Exception as exc:
                await self._report_handler_error(event_type, origin, exc)

    async def _save_cursors(self) -> None:
        async with self._cursor_lock:
            self.worker_state.save_cursors(
                {target: dict(cursors) for target, cursors in self._cursors.items()}
            )

    async def _gateway_once(self, target: str) -> None:
        if target not in self._targets:
            await self.add_target(target)
        token = await self._token(target)
        parsed = urlsplit(target)
        uri = f"wss://{parsed.netloc}/api/v1/bots/gateway"
        async with connect(uri, max_size=1_048_576, open_timeout=15) as socket:
            self._sockets.add(socket)
            hello = json.loads(await socket.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000
            timestamp = int(time.time())
            nonce = secrets.token_urlsafe(24)
            digest = hashlib.sha256(token.encode()).hexdigest()
            proof = self._sign(
                f"kaede-dpop-v1\nGET\n/api/v1/bots/gateway\n{timestamp}\n{nonce}\n{digest}".encode()
            )
            await socket.send(
                json.dumps(
                    {
                        "op": 2,
                        "token": token,
                        "timestamp": timestamp,
                        "nonce": nonce,
                        "proof": proof,
                        "cursors": self._cursors[target],
                        "intents": self.intents.names(),
                    }
                )
            )

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(interval)
                    await socket.send(json.dumps({"op": 1}))

            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                async for encoded in socket:
                    event = json.loads(encoded)
                    if event.get("op") != 0:
                        continue
                    topic = event.get("topic")
                    sequence = event.get("s", 0)
                    await self.dispatch(
                        str(event.get("t", "")),
                        event.get("d") or {},
                        target=target,
                        topic=topic if isinstance(topic, str) else None,
                        sequence=sequence if isinstance(sequence, int) else 0,
                    )
                    # Persist only after dispatch completes. A crash in a user
                    # handler then replays the event instead of acknowledging
                    # work that the application never finished.
                    if topic and isinstance(sequence, int) and sequence > 0:
                        self._cursors[target][topic] = sequence
                        await self._save_cursors()
            finally:
                heartbeat_task.cancel()
                self._sockets.discard(socket)

    async def gateway(self, target: str) -> None:
        target = canonical_target_origin(target)
        backoff = 1.0
        while not self._stopping:
            try:
                await self._gateway_once(target)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.dispatch(
                    "GATEWAY_ERROR",
                    {"error": str(exc), "retry_in": backoff},
                    target=target,
                )
            if not self._stopping:
                await asyncio.sleep(backoff + secrets.randbelow(500) / 1000)
                backoff = min(30.0, backoff * 2)

    async def start(self, *targets: str) -> None:
        if not targets:
            raise ValueError("at least one target instance is required")
        self._stopping = False
        origins = list(
            dict.fromkeys([await self.add_target(target) for target in targets])
        )
        await asyncio.gather(*(self.gateway(origin) for origin in origins))

    async def close(self) -> None:
        self._stopping = True
        await asyncio.gather(
            *(socket.close() for socket in tuple(self._sockets)),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(client.aclose() for client in self._targets.values()),
            return_exceptions=True,
        )
