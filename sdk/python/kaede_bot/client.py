from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect

from .errors import ApiError, Forbidden, NotFound, RateLimited
from .intents import Intents
from .models import Interaction, Message
from .refs import EntityRef, User
from .state import WorkerState

Handler = Callable[..., Awaitable[None]]
T = TypeVar("T")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def canonical_target_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("bot targets must be canonical HTTPS origins") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname.endswith(".")
    ):
        raise ValueError("bot targets must be canonical HTTPS origins")
    authority = parsed.hostname.lower()
    port_suffix = f":{port}" if port not in {None, 443} else ""
    return f"https://{authority}{port_suffix}"


class Client:
    def __init__(self, *, worker_state: WorkerState, intents: Intents | None = None):
        self.worker_state = worker_state
        self.intents = intents or Intents.default()
        self._targets: dict[str, httpx.AsyncClient] = {}
        self._tokens: dict[str, tuple[str, float]] = {}
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._commands: list[dict[str, Any]] = []
        self._cursors: dict[str, dict[str, int]] = defaultdict(dict)
        self._stopping = False
        self._sockets: set[Any] = set()

    def event(self, function: Handler) -> Handler:
        self._handlers[function.__name__.removeprefix("on_").upper()].append(function)
        return function

    def command(self, *, name: str | None = None, description: str = ""):
        def decorator(function: Handler) -> Handler:
            command_name = name or function.__name__.lower()
            self._commands.append(
                {
                    "name": command_name,
                    "type": "chat_input",
                    "description": description,
                    "default_member_permissions": [],
                    "contexts": ["guild"],
                    "options": [],
                }
            )
            self._handlers[f"COMMAND:{command_name}"].append(function)
            return function

        return decorator

    async def sync_commands(self, *, application_home: str, control_token: str) -> None:
        async with httpx.AsyncClient(
            base_url=application_home.rstrip("/"), timeout=15
        ) as http:
            response = await http.put(
                f"/api/v1/bot-control/applications/{self.worker_state.application_ref}/commands",
                headers={"Authorization": f"BotControl {control_token}"},
                json={"commands": self._commands},
            )
            response.raise_for_status()

    async def add_target(self, base_url: str) -> str:
        origin = canonical_target_origin(base_url)
        self._targets[origin] = httpx.AsyncClient(base_url=origin, timeout=30)
        await self._token(origin, force=True)
        return origin

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
        return data["access_token"]

    def _proof_headers(self, method: str, target: str, token: str) -> dict[str, str]:
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        payload = f"kaede-dpop-v1\n{method.upper()}\n{target}\n{timestamp}\n{nonce}\n{digest}".encode()
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
    ) -> Any:
        if target is None:
            if len(self._targets) != 1:
                raise ValueError(
                    "target is required when the client connects to multiple instances"
                )
            target = next(iter(self._targets))
        target = canonical_target_origin(target)
        if target not in self._targets:
            await self.add_target(target)
        signed_target = path
        if params:
            signed_target = f"{path}?{httpx.QueryParams(params)}"
        for attempt in range(3):
            token = await self._token(target, force=attempt > 0)
            response = await self._targets[target].request(
                method,
                path,
                json=json,
                params=params,
                headers=self._proof_headers(method, signed_target, token),
            )
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(
                    min(
                        30.0, max(0.05, float(response.headers.get("Retry-After", "1")))
                    )
                )
                continue
            await self._raise(response)
            return None if response.status_code == 204 else response.json()
        raise ApiError(
            503, "BOT_REQUEST_RETRY_EXHAUSTED", "Bot request retries were exhausted"
        )

    async def _raise(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            detail = response.json().get("detail", {})
        except (ValueError, AttributeError):
            detail = {}
        code = (
            detail.get("code", "KAEDE_API_ERROR")
            if isinstance(detail, dict)
            else "KAEDE_API_ERROR"
        )
        message = (
            detail.get("message", code.replace("_", " ").title())
            if isinstance(detail, dict)
            else str(detail)
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

    async def send_message(
        self,
        channel: EntityRef,
        content: str,
        *,
        target: str | None = None,
        reply_to: EntityRef | None = None,
        e2ee: dict[str, Any] | None = None,
    ) -> Message:
        body: dict[str, Any] = {"content": content, "allowed_mentions": {"parse": []}}
        if reply_to is not None:
            body["message_reference"] = {
                "message_id": str(reply_to.id),
                "message_domain": reply_to.domain,
            }
        if e2ee is not None:
            body = {"content": None, "e2ee": e2ee, "allowed_mentions": {"parse": []}}
        raw = await self.request(
            "POST",
            f"/api/v1/bots/channels/{channel}/messages",
            target=target,
            json=body,
        )
        return Message.from_payload(self, raw)

    async def history(
        self,
        channel: EntityRef,
        *,
        target: str | None = None,
        before: EntityRef | None = None,
        limit: int = 50,
    ) -> list[Message]:
        params: dict[str, Any] = {"limit": min(100, max(1, limit))}
        if before:
            params["before"] = str(before)
        raw = await self.request(
            "GET",
            f"/api/v1/bots/channels/{channel}/messages",
            target=target,
            params=params,
        )
        return [Message.from_payload(self, item) for item in raw]

    async def edit_message(
        self,
        channel: EntityRef,
        message: EntityRef,
        content: str,
        *,
        target: str | None = None,
    ) -> Message:
        raw = await self.request(
            "PATCH",
            f"/api/v1/bots/channels/{channel}/messages/{message}",
            target=target,
            json={"content": content},
        )
        return Message.from_payload(self, raw)

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

    async def dispatch(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "INTERACTION_CREATE":
            interaction = Interaction.from_payload(self, data)
            for handler in self._handlers.get(
                f"COMMAND:{interaction.command['name']}", []
            ):
                await handler(interaction)
            for handler in self._handlers.get("INTERACTION", []):
                await handler(interaction)
            return
        model: object = (
            Message.from_payload(self, data)
            if event_type.startswith("MESSAGE") and "id" in data
            else data
        )
        for handler in self._handlers.get(event_type, []):
            await handler(model)

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
                    if topic and isinstance(event.get("s"), int):
                        self._cursors[target][topic] = event["s"]
                    await self.dispatch(event.get("t", ""), event.get("d") or {})
            finally:
                heartbeat_task.cancel()
                self._sockets.discard(socket)

    async def gateway(self, target: str) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                await self._gateway_once(target)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                for handler in self._handlers.get("GATEWAY_ERROR", []):
                    await handler(
                        {"target": target, "error": str(exc), "retry_in": backoff}
                    )
            if not self._stopping:
                await asyncio.sleep(backoff + secrets.randbelow(500) / 1000)
                backoff = min(30.0, backoff * 2)

    async def start(self, *targets: str) -> None:
        if not targets:
            raise ValueError("at least one target instance is required")
        self._stopping = False
        origins = [await self.add_target(target) for target in targets]
        await asyncio.gather(*(self.gateway(origin) for origin in origins))

    async def close(self) -> None:
        self._stopping = True
        await asyncio.gather(
            *(socket.close() for socket in tuple(self._sockets)),
            return_exceptions=True,
        )
        await asyncio.gather(*(client.aclose() for client in self._targets.values()))
