from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from redis.asyncio import Redis
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.asyncio.client import connect

from app.auth.security import hash_password
from app.chat.presence import decode_presence_state
from app.core.settings import Settings, get_settings
from app.db.bot_models import ApplicationAsset, BotApplication, InstanceAdminGrant
from app.db.models import (
    Channel,
    Emoji,
    FederatedHistoryMessage,
    FederationEvent,
    FederationInbox,
    FederationOutbox,
    Guild,
    GuildMember,
    MemberRole,
    Message,
    Pin,
    Reaction,
    Relationship,
    Role,
    User,
    UserSettings,
)
from app.db.session import create_engine_and_sessionmaker
from app.federation.client import signed_request
from app.federation.delivery import drain_destination
from scripts.verification import (
    PASSWORD_KDF_VERSION,
    VerificationFailure,
    authentication_secret,
    failure_message,
    require,
)

PASSWORD = "correct horse battery staple"  # noqa: S105 - disposable validation credential
PASSWORD_AUTH_SALT = bytes(range(16))
PASSWORD_VAULT_SALT = bytes(reversed(range(16)))
ALPHA_URL = os.getenv("ALPHA_URL", "http://alpha-api:8000")
BETA_URL = os.getenv("BETA_URL", "http://beta-api:8000")
ALPHA_DATABASE_URL = os.environ["ALPHA_DATABASE_URL"]
BETA_DATABASE_URL = os.environ["BETA_DATABASE_URL"]
BETA_DRAGONFLY_URL = os.environ["BETA_DRAGONFLY_URL"]
TLS_CA_FILE = os.getenv("TLS_CA_FILE")
BOT_GATEWAY_URL = os.getenv(
    "BETA_BOT_GATEWAY_URL",
    "ws://beta-api:8000/api/v1/bots/gateway",
)
BOT_GATEWAY_PATH = "/api/v1/bots/gateway"

BOT_RUNTIME_SCOPES = [
    "applications.commands",
    "interactions.respond",
    "guilds.read",
    "channels.read",
    "messages.metadata",
    "messages.content",
    "messages.history",
    "messages.send",
]
BOT_RUNTIME_INTENTS = ["guilds", "guild_messages", "message_content", "interactions"]
DIRECTORY_PROFILE: dict[str, object] = {
    "directory_summary": "Federated runtime and interaction checks.",
    "directory_category": "utilities",
    "directory_tags": ["federation", "runtime"],
    "support_url": "https://support.alpha.localhost/federated-acceptance",
    "privacy_url": "https://alpha.localhost/privacy/federated-acceptance",
    "terms_url": "https://alpha.localhost/terms/federated-acceptance",
}
DIRECTORY_COLLECTION_CATALOG = [
    {
        "slug": "featured",
        "name": "Featured",
        "description": "Apps selected by this instance's directory team.",
    },
    {
        "slug": "staff-picks",
        "name": "Staff Picks",
        "description": "Apps recommended by this instance's staff.",
    },
    {
        "slug": "new-and-noteworthy",
        "name": "New & Noteworthy",
        "description": "Recently highlighted apps worth discovering.",
    },
]
DIRECTORY_IMAGE = {
    "name": "Federated runtime preview",
    "media_hash": "b" * 64,
    "content_type": "image/png",
    "width": 1280,
    "height": 720,
}
DIRECTORY_YOUTUBE_ID = "dQw4w9WgXcQ"
DIRECTORY_EXTERNAL_LINKS = [
    {"name": "Website", "url": "https://alpha.localhost/federated-acceptance"}
]
DIRECTORY_SUPPORTED_LOCALES = ["en-US", "fr"]
DIRECTORY_DESCRIPTION_LOCALIZATIONS = {"fr": "Validation fédérée du runtime et des interactions."}


def base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def worker_assertion(
    private_key: Ed25519PrivateKey,
    application_ref: str,
    worker_id: int,
    logical_origin: str,
    path: str,
) -> dict[str, object]:
    """Build the SDK's audience-bound, one-use worker assertion."""

    issued_at = int(time.time())
    expires_at = issued_at + 60
    nonce = secrets.token_urlsafe(24)
    audience = f"{logical_origin}{path}"
    message = (
        f"kaede-worker-assertion-v1\n{application_ref}\n{worker_id}\n"
        f"{audience}\n{issued_at}\n{expires_at}\n{nonce}"
    ).encode()
    return {
        "application_ref": application_ref,
        "worker_id": worker_id,
        "audience": audience,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce,
        "signature": base64url(private_key.sign(message)),
    }


def bot_proof_headers(
    private_key: Ed25519PrivateKey,
    method: str,
    signed_target: str,
    token: str,
) -> dict[str, str]:
    """Bind one bot API request to its token, method, and exact target."""

    timestamp = int(time.time())
    nonce = secrets.token_urlsafe(24)
    token_digest = hashlib.sha256(token.encode()).hexdigest()
    message = (
        f"kaede-dpop-v1\n{method.upper()}\n{signed_target}\n{timestamp}\n{nonce}\n{token_digest}"
    ).encode()
    return {
        "Authorization": f"Bot {token}",
        "X-Kaede-Bot-Timestamp": str(timestamp),
        "X-Kaede-Bot-Nonce": nonce,
        "X-Kaede-Bot-Proof": base64url(private_key.sign(message)),
    }


async def acquire_bot_token(
    client: httpx.AsyncClient,
    private_key: Ed25519PrivateKey,
    application_ref: str,
    worker_id: int,
    logical_origin: str,
) -> httpx.Response:
    path = "/api/v1/bots/token"
    return await client.post(
        path,
        json=worker_assertion(
            private_key,
            application_ref,
            worker_id,
            logical_origin,
            path,
        ),
    )


async def bot_request(
    client: httpx.AsyncClient,
    private_key: Ed25519PrivateKey,
    token: str,
    method: str,
    path: str,
    *,
    json_body: object | None = None,
) -> httpx.Response:
    return await client.request(
        method,
        path,
        headers=bot_proof_headers(private_key, method, path, token),
        json=json_body,
    )


async def receive_gateway_frame(
    socket: Any,
    *,
    timeout_seconds: float,
    label: str,
) -> dict[str, Any]:
    """Receive one bounded, strictly shaped Gateway JSON frame."""

    try:
        encoded = await asyncio.wait_for(socket.recv(), timeout=timeout_seconds)
    except TimeoutError:
        raise VerificationFailure(f"{label} timed out") from None
    if isinstance(encoded, bytes):
        try:
            encoded = encoded.decode("utf-8")
        except UnicodeDecodeError:
            raise VerificationFailure(f"{label} was not UTF-8 JSON") from None
    require(isinstance(encoded, str), f"{label} was not a text frame")
    try:
        frame: Any = json.loads(encoded)
    except json.JSONDecodeError:
        raise VerificationFailure(f"{label} was not valid JSON") from None
    require(isinstance(frame, dict), f"{label} was not a JSON object")
    require(all(isinstance(key, str) for key in frame), f"{label} had a non-string key")
    return cast(dict[str, Any], frame)


def bot_gateway_identify(
    private_key: Ed25519PrivateKey,
    token: str,
) -> dict[str, object]:
    """Build the bot Gateway IDENTIFY frame from the normal DPoP proof."""

    proof = bot_proof_headers(private_key, "GET", BOT_GATEWAY_PATH, token)
    return {
        "op": 2,
        "token": token,
        "timestamp": int(proof["X-Kaede-Bot-Timestamp"]),
        "nonce": proof["X-Kaede-Bot-Nonce"],
        "proof": proof["X-Kaede-Bot-Proof"],
        "cursors": {},
        "intents": BOT_RUNTIME_INTENTS,
    }


def ready_installation_revision(
    ready: dict[str, Any],
    *,
    application_ref: str,
    worker_id: int,
    installation_id: str | None,
    user_installation_id: str | None,
) -> str:
    """Validate READY and return the selected installation's live revision."""

    require(ready.get("op") == 0 and ready.get("t") == "READY", "bot Gateway READY missing")
    data = ready.get("d")
    require(isinstance(data, dict), "bot Gateway READY data is invalid")
    ready_data = cast(dict[str, Any], data)
    require(
        ready_data.get("application_ref") == application_ref
        and ready_data.get("worker_id") == str(worker_id),
        "bot Gateway READY was bound to the wrong application worker",
    )
    guild_install = installation_id is not None
    require(
        guild_install != (user_installation_id is not None),
        "interaction acceptance requires exactly one installation lineage",
    )
    collection_name = "installations" if guild_install else "user_installations"
    selected_id = installation_id if guild_install else user_installation_id
    collection = ready_data.get(collection_name)
    require(
        isinstance(collection, list) and all(isinstance(item, dict) for item in collection),
        f"bot Gateway READY {collection_name} are invalid",
    )
    installation_rows = cast(list[dict[str, Any]], collection)
    matches = [item for item in installation_rows if item.get("id") == selected_id]
    require(len(matches) == 1, "bot Gateway READY omitted the selected installation")
    revision = matches[0].get("capability_revision")
    require(
        isinstance(revision, str)
        and revision.isascii()
        and revision.isdecimal()
        and not revision.startswith("0"),
        "bot Gateway READY installation revision is invalid",
    )
    return cast(str, revision)


@asynccontextmanager
async def invoke_bot_interaction(
    client: httpx.AsyncClient,
    private_key: Ed25519PrivateKey,
    token: str,
    application_ref: str,
    worker_id: int,
    user_headers: dict[str, str],
    channel_ref: str,
    guild_ref: str,
    *,
    installation_id: str | None = None,
    user_installation_id: str | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """Keep Gateway open while a caller uses the private lifecycle token."""

    async with connect(BOT_GATEWAY_URL, max_size=1_048_576, open_timeout=15) as socket:
        hello = await receive_gateway_frame(
            socket,
            timeout_seconds=15,
            label="bot Gateway HELLO",
        )
        require(hello.get("op") == 10, "bot Gateway HELLO missing")
        await socket.send(json.dumps(bot_gateway_identify(private_key, token)))
        ready = await receive_gateway_frame(
            socket,
            timeout_seconds=15,
            label="bot Gateway READY",
        )
        installation_revision = ready_installation_revision(
            ready,
            application_ref=application_ref,
            worker_id=worker_id,
            installation_id=installation_id,
            user_installation_id=user_installation_id,
        )

        invoked = await client.post(
            f"/api/v1/channels/{channel_ref}/interactions",
            headers=user_headers,
            json={
                "application_ref": application_ref,
                "command_name": "federation-check",
                "command_type": "chat_input",
                "options": {},
            },
        )
        require(invoked.status_code == 202, f"remote bot interaction failed: {invoked.text}")
        acknowledgement = invoked.json()
        interaction_id = acknowledgement.get("id")
        require(
            isinstance(interaction_id, str)
            and interaction_id.isascii()
            and interaction_id.isdecimal()
            and not interaction_id.startswith("0"),
            "remote interaction omitted its ID",
        )
        interaction_id = cast(str, interaction_id)
        require(
            acknowledgement.get("interaction_ref") == f"{interaction_id}@beta.localhost",
            "remote interaction acknowledgement had the wrong authority",
        )

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            frame = await receive_gateway_frame(
                socket,
                timeout_seconds=max(0.1, deadline - time.monotonic()),
                label="INTERACTION_CREATE",
            )
            if frame.get("op") != 0 or frame.get("t") != "INTERACTION_CREATE":
                continue
            event = frame.get("d")
            if not isinstance(event, dict) or event.get("id") != interaction_id:
                continue
            expected_integration = (
                "guild_install" if installation_id is not None else "user_install"
            )
            require(
                event.get("interaction_ref") == f"{interaction_id}@beta.localhost"
                and event.get("application_ref") == application_ref
                and event.get("channel_ref") == channel_ref
                and event.get("guild_ref") == guild_ref
                and event.get("integration_type") == expected_integration
                and event.get("installation_id") == installation_id
                and event.get("user_installation_id") == user_installation_id
                and event.get("installation_revision") == installation_revision,
                "INTERACTION_CREATE was not bound to the invoked installation",
            )
            command = event.get("command")
            require(
                isinstance(command, dict) and command.get("name") == "federation-check",
                "INTERACTION_CREATE was bound to the wrong command",
            )
            interaction_token = event.get("token")
            require(
                isinstance(interaction_token, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{43}", interaction_token) is not None,
                "INTERACTION_CREATE omitted its lifecycle token",
            )
            yield interaction_id, cast(str, interaction_token)
            return
    raise VerificationFailure("matching INTERACTION_CREATE did not reach the bot Gateway")


def entity_ref(payload: dict[str, Any]) -> str:
    """Render the canonical API reference for a federated entity payload."""

    identifier = payload.get("id")
    domain = payload.get("origin_domain")
    require(isinstance(identifier, str) and identifier.isdecimal(), "entity ID is invalid")
    require(isinstance(domain, str) and bool(domain), "entity origin is invalid")
    return f"{identifier}@{domain}"


def bounded_directory_page(
    payload: object,
    *,
    maximum: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the bounded public Directory page envelope used by acceptance."""

    if maximum < 1:
        raise ValueError("directory page maximum must be positive")
    require(isinstance(payload, dict), "application Directory page was not an object")
    page = cast(dict[str, Any], payload)
    require(
        set(page) == {"items", "next_cursor", "collections", "selected_collection"},
        "application Directory page fields were invalid",
    )
    raw_items = page["items"]
    require(
        isinstance(raw_items, list) and len(raw_items) <= maximum,
        "application Directory page exceeded its requested bound",
    )
    require(
        all(isinstance(item, dict) for item in raw_items),
        "application Directory page contained a non-object item",
    )
    next_cursor = page["next_cursor"]
    require(
        next_cursor is None
        or (
            isinstance(next_cursor, str)
            and next_cursor.isascii()
            and next_cursor.isdecimal()
            and not next_cursor.startswith("0")
        ),
        "application Directory cursor was not a canonical wire ID",
    )
    return page, cast(list[dict[str, Any]], raw_items)


def versioned_headers(auth_headers: dict[str, str], resource: dict[str, Any]) -> dict[str, str]:
    """Attach the optimistic-concurrency version advertised by an API resource."""

    version = resource.get("version")
    if not isinstance(version, str) or not version:
        raise VerificationFailure(
            f"API resource omitted the version required for an If-Match request: {resource!r}"
        )
    return {**auth_headers, "If-Match": version}


async def seed_user(database_url: str, domain: str, user_id: int, username: str) -> None:
    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            user = await session.get(User, (user_id, domain))
            password_hash = hash_password(
                authentication_secret(PASSWORD, domain, PASSWORD_AUTH_SALT)
            )
            if user is None:
                user = User(
                    id=user_id,
                    origin_domain=domain,
                    is_local=True,
                    username=username,
                    email=f"{username}@example.test",
                    email_verified_at=datetime.now(UTC),
                    password_hash=password_hash,
                    password_kdf_version=2,
                    password_auth_salt=PASSWORD_AUTH_SALT,
                    e2ee_vault_salt=PASSWORD_VAULT_SALT,
                )
                session.add(user)
                await session.flush()
                session.add(
                    UserSettings(
                        user_id=user.id,
                        user_domain=user.origin_domain,
                        user_is_local=True,
                        dm_privacy="everyone",
                    )
                )
            else:
                user.password_hash = password_hash
                user.password_kdf_version = 2
                user.password_auth_salt = PASSWORD_AUTH_SALT
                user.e2ee_vault_salt = PASSWORD_VAULT_SALT
            await session.commit()
    finally:
        await engine.dispose()


async def seed_guild_emoji(
    database_url: str,
    *,
    domain: str,
    guild_id: int,
    creator_id: int,
    emoji_id: int,
) -> None:
    """Seed immutable media metadata for federation tests without an object store."""

    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            session.add(
                Emoji(
                    id=emoji_id,
                    origin_domain=domain,
                    guild_id=guild_id,
                    guild_domain=domain,
                    name="federated_lantern",
                    object_key=f"validation/{emoji_id}",
                    media_hash="a" * 64,
                    animated=False,
                    creator_id=creator_id,
                    creator_domain=domain,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


async def seed_application_directory_fixture(
    database_url: str,
    *,
    application_id: int,
    application_domain: str,
    reviewer_id: int,
    reviewer_domain: str,
) -> int:
    """Bootstrap only Directory state unavailable through the public harness.

    The disposable acceptance stack has no initial operator capable of granting
    the reviewer role and no object-store upload fixture. Approval itself and
    every user-visible Directory action still use their normal authenticated APIs.
    """

    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            application = await session.get(
                BotApplication,
                (application_id, application_domain),
            )
            require(application is not None, "Directory fixture application was not committed")

            grant = await session.scalar(
                select(InstanceAdminGrant).where(
                    InstanceAdminGrant.user_id == reviewer_id,
                    InstanceAdminGrant.user_domain == reviewer_domain,
                    InstanceAdminGrant.role == "bot_reviewer",
                )
            )
            if grant is None:
                require(
                    await session.get(InstanceAdminGrant, reviewer_id) is None,
                    "Directory fixture reviewer grant ID collided",
                )
                session.add(
                    InstanceAdminGrant(
                        id=reviewer_id,
                        user_id=reviewer_id,
                        user_domain=reviewer_domain,
                        user_is_local=True,
                        role="bot_reviewer",
                        granted_by_id=reviewer_id,
                        granted_by_domain=reviewer_domain,
                    )
                )
            else:
                if grant.revoked_at is not None or grant.expires_at is not None:
                    grant.generation += 1
                grant.revoked_at = None
                grant.expires_at = None

            screenshot = await session.scalar(
                select(ApplicationAsset).where(
                    ApplicationAsset.application_id == application_id,
                    ApplicationAsset.application_domain == application_domain,
                    ApplicationAsset.kind == "store",
                    ApplicationAsset.name == DIRECTORY_IMAGE["name"],
                )
            )
            if screenshot is None:
                require(
                    await session.get(ApplicationAsset, application_id) is None,
                    "Directory fixture screenshot ID collided",
                )
                session.add(
                    ApplicationAsset(
                        id=application_id,
                        application_id=application_id,
                        application_domain=application_domain,
                        kind="store",
                        name=cast(str, DIRECTORY_IMAGE["name"]),
                        media_hash=cast(str, DIRECTORY_IMAGE["media_hash"]),
                        object_key=(f"validation/application-directory/{application_id}.png"),
                        content_type=cast(str, DIRECTORY_IMAGE["content_type"]),
                        width=cast(int, DIRECTORY_IMAGE["width"]),
                        height=cast(int, DIRECTORY_IMAGE["height"]),
                    )
                )
                screenshot_id = application_id
            else:
                screenshot_id = screenshot.id
            await session.commit()
            return screenshot_id
    finally:
        await engine.dispose()


def rate_limit_retry_seconds(response: httpx.Response) -> float | None:
    """Return the longest valid numeric retry hint from a 429 response."""

    if response.status_code != 429:
        return None
    hints: list[float] = []
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            retry_after_ms = detail.get("retry_after_ms")
            if (
                isinstance(retry_after_ms, int)
                and not isinstance(retry_after_ms, bool)
                and retry_after_ms > 0
            ):
                hints.append(retry_after_ms / 1_000)
    retry_after_header = response.headers.get("Retry-After")
    if retry_after_header is not None:
        try:
            retry_after = float(retry_after_header)
        except ValueError:
            retry_after = 0
        if math.isfinite(retry_after) and retry_after > 0:
            hints.append(retry_after)
    return max(hints) if hints else None


async def wait_for(
    operation: Any,
    predicate: Any,
    message: str,
    *,
    # Durable federation wakeups can intentionally coalesce behind an active
    # per-peer drain. The minute sweep is the recovery path, so leave a full
    # second sweep window instead of racing the first boundary.
    wait_seconds: float = 120,
    poll_seconds: float = 0.2,
) -> Any:
    if poll_seconds <= 0:
        raise ValueError("poll interval must be positive")
    deadline = time.monotonic() + wait_seconds
    last: Any = None
    while time.monotonic() < deadline:
        last = await operation()
        if predicate(last):
            return last
        retry_after = rate_limit_retry_seconds(last) if isinstance(last, httpx.Response) else None
        sleep_seconds = max(poll_seconds, retry_after or 0)
        await asyncio.sleep(min(sleep_seconds, max(0, deadline - time.monotonic())))
    if isinstance(last, httpx.Response):
        detail = f"HTTP {last.status_code}: {last.text[:2000]}"
    else:
        detail = repr(last)
    raise VerificationFailure(f"{message}; last observed result: {detail}")


async def login(client: httpx.AsyncClient, username: str, domain: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"X-Kaede-Client": "mobile"},
        json={
            "identifier": username,
            "password": authentication_secret(PASSWORD, domain, PASSWORD_AUTH_SALT),
            "password_kdf_version": PASSWORD_KDF_VERSION,
        },
    )
    require(response.status_code == 200, f"{username} login failed: {response.text}")
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def row_count(database_url: str, model: type[Any], *conditions: Any) -> int:
    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            value = await session.scalar(select(func.count()).select_from(model).where(*conditions))
            return int(value or 0)
    finally:
        await engine.dispose()


async def database_scalar(database_url: str, statement: Any) -> Any:
    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            return await session.scalar(statement)
    finally:
        await engine.dispose()


async def set_outbox_event(
    database_url: str,
    destination: str,
    nonce: str,
    *,
    due: bool,
) -> None:
    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    select(FederationOutbox, FederationEvent)
                    .join(
                        FederationEvent,
                        (FederationEvent.origin_domain == FederationOutbox.event_origin_domain)
                        & (FederationEvent.event_id == FederationOutbox.event_id),
                    )
                    .where(FederationOutbox.destination == destination)
                )
            ).tuples()
            matched = False
            for outbox, event in rows:
                message = event.envelope.get("content", {}).get("message", {})
                if message.get("client_nonce") != nonce:
                    continue
                outbox.status = "pending"
                outbox.next_retry_at = datetime.now(UTC) + (
                    timedelta(0) if due else timedelta(hours=1)
                )
                outbox.last_error = None
                matched = True
            require(matched, f"outbox event {nonce} was not found")
            await session.commit()
    finally:
        await engine.dispose()


async def outbox_status(database_url: str, nonce: str) -> str | None:
    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    select(FederationOutbox, FederationEvent).join(
                        FederationEvent,
                        (FederationEvent.origin_domain == FederationOutbox.event_origin_domain)
                        & (FederationEvent.event_id == FederationOutbox.event_id),
                    )
                )
            ).tuples()
            for outbox, event in rows:
                message = event.envelope.get("content", {}).get("message", {})
                if message.get("client_nonce") == nonce:
                    return outbox.status
            return None
    finally:
        await engine.dispose()


@asynccontextmanager
async def hold_outbox_delivery(
    database_url: str,
    destination: str,
) -> AsyncIterator[AsyncSession]:
    """Hold the same peer lock used by the ordered outbox drainer."""

    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            await session.scalar(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(f"kaede-outbox-drain:{destination}", 0)
                    )
                )
            )
            yield session
            await session.commit()
    finally:
        await engine.dispose()


async def park_locked_outbox_events(
    session: AsyncSession,
    destination: str,
    nonces: set[str],
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Park exact retained events while their destination drain lock is held."""

    require(bool(nonces), "at least one outbox event must be parked")
    rows = (
        await session.execute(
            select(FederationOutbox, FederationEvent)
            .join(
                FederationEvent,
                (FederationEvent.origin_domain == FederationOutbox.event_origin_domain)
                & (FederationEvent.event_id == FederationOutbox.event_id),
            )
            .where(FederationOutbox.destination == destination)
            .with_for_update()
        )
    ).tuples()
    matches: dict[str, tuple[FederationOutbox, FederationEvent]] = {}
    for outbox, event in rows:
        message = event.envelope.get("content", {}).get("message", {})
        nonce = message.get("client_nonce")
        if nonce not in nonces:
            continue
        require(nonce not in matches, f"outbox event {nonce} was not unique")
        require(
            outbox.status in {"pending", "retry", "circuit"},
            f"outbox event {nonce} was not retained: {outbox.status}",
        )
        matches[cast(str, nonce)] = (outbox, event)
    require(
        set(matches) == nonces,
        f"outbox events were not retained: {sorted(nonces - set(matches))}",
    )
    parked_until = datetime.now(UTC) + timedelta(hours=1)
    parked: dict[str, tuple[str, dict[str, Any]]] = {}
    for nonce, (outbox, event) in matches.items():
        outbox.status = "pending"
        outbox.next_retry_at = parked_until
        outbox.last_error = None
        parked[nonce] = (event.event_id, event.envelope)
    return parked


def guild_event_sequence(envelope: dict[str, Any], *, label: str) -> int:
    context = envelope.get("context")
    raw_sequence = context.get("seq") if isinstance(context, dict) else None
    if not (
        isinstance(raw_sequence, str)
        and raw_sequence.isdigit()
        and raw_sequence == str(int(raw_sequence))
        and int(raw_sequence) > 0
    ):
        raise VerificationFailure(f"{label} has an invalid guild sequence")
    return int(raw_sequence)


async def guild_replica_state(
    database_url: str,
    guild_id: int,
    guild_domain: str,
) -> tuple[int, str, bool] | None:
    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            row = (
                await session.execute(
                    select(Guild.last_event_seq, Guild.sync_status, Guild.unavailable).where(
                        Guild.id == guild_id,
                        Guild.origin_domain == guild_domain,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            return int(row.last_event_seq), str(row.sync_status), bool(row.unavailable)
    finally:
        await engine.dispose()


async def drain_peer_outbox(
    database_url: str,
    settings: Settings,
    destination: str,
) -> int:
    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        return await drain_destination(sessionmaker, settings, destination)
    finally:
        await engine.dispose()


def require_single_inbox_result(
    response: httpx.Response,
    *,
    event_id: str,
    status: str,
    code: str,
) -> None:
    expected = {
        "results": [
            {
                "event_id": event_id,
                "status": status,
                "code": code,
            }
        ]
    }
    try:
        payload = response.json()
    except ValueError:
        payload = None
    require(
        response.status_code == 200 and payload == expected,
        (
            "signed inbox injection returned an invalid result: "
            f"{response.status_code} {response.text}"
        ),
    )


async def guild_policy_outbox_state(
    database_url: str,
    destination: str,
    policy: str,
) -> tuple[str, str | None, str] | None:
    engine, sessionmaker = create_engine_and_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    select(FederationOutbox, FederationEvent)
                    .join(
                        FederationEvent,
                        (FederationEvent.origin_domain == FederationOutbox.event_origin_domain)
                        & (FederationEvent.event_id == FederationOutbox.event_id),
                    )
                    .where(FederationOutbox.destination == destination)
                    .order_by(FederationOutbox.id.desc())
                )
            ).tuples()
            for outbox, event in rows:
                guild_state = event.envelope.get("content", {}).get("guild", {})
                if (
                    event.event_type == "guild.update"
                    and guild_state.get("federated_history_policy") == policy
                ):
                    return outbox.status, outbox.last_error, event.event_id
            return None
    finally:
        await engine.dispose()


async def require_guild_policy_delivery(policy: str, *, label: str) -> None:
    """Wait through durable delivery and surface both sender and receiver errors."""

    delivery = await wait_for(
        lambda: guild_policy_outbox_state(
            ALPHA_DATABASE_URL,
            "beta.localhost",
            policy,
        ),
        lambda value: value is not None and value[0] in {"delivered", "failed"},
        f"history {label} outbox did not settle",
        wait_seconds=120,
    )
    receiver_error = await database_scalar(
        BETA_DATABASE_URL,
        select(FederationInbox.error).where(
            FederationInbox.origin_domain == "alpha.localhost",
            FederationInbox.event_id == delivery[2],
        ),
    )
    require(
        delivery[0] == "delivered",
        (f"history {label} delivery failed: {delivery[1]}; receiver error: {receiver_error}"),
    )


async def channel_mutation_state(
    channel_id: int,
    event_type: str,
) -> dict[str, Any]:
    """Return sender, receiver, and replica state for an acceptance mutation."""

    event_id: str | None = None
    event_seq: str | None = None
    outbox_status_value: str | None = None
    outbox_error: str | None = None
    alpha_engine, alpha_sessionmaker = create_engine_and_sessionmaker(ALPHA_DATABASE_URL)
    try:
        async with alpha_sessionmaker() as session:
            rows = (
                await session.execute(
                    select(FederationOutbox, FederationEvent)
                    .join(
                        FederationEvent,
                        (FederationEvent.origin_domain == FederationOutbox.event_origin_domain)
                        & (FederationEvent.event_id == FederationOutbox.event_id),
                    )
                    .where(
                        FederationOutbox.destination == "beta.localhost",
                        FederationEvent.event_type == event_type,
                    )
                    .order_by(FederationOutbox.id.desc())
                )
            ).tuples()
            for outbox, event in rows:
                channel = event.envelope.get("content", {}).get("channel", {})
                if channel.get("id") == str(channel_id):
                    event_id = event.event_id
                    event_seq = str(event.envelope.get("context", {}).get("seq"))
                    outbox_status_value = outbox.status
                    outbox_error = outbox.last_error
                    break
    finally:
        await alpha_engine.dispose()

    inbox_status: str | None = None
    inbox_error: str | None = None
    replica_name: str | None = None
    replica_last_seq: int | None = None
    beta_engine, beta_sessionmaker = create_engine_and_sessionmaker(BETA_DATABASE_URL)
    try:
        async with beta_sessionmaker() as session:
            if event_id is not None:
                inbox = await session.get(FederationInbox, ("alpha.localhost", event_id))
                if inbox is not None:
                    inbox_status = inbox.status
                    inbox_error = inbox.error
            replica_name = await session.scalar(
                select(Channel.name).where(
                    Channel.id == channel_id,
                    Channel.origin_domain == "alpha.localhost",
                )
            )
            replica_last_seq = await session.scalar(
                select(Guild.last_event_seq).where(Guild.origin_domain == "alpha.localhost")
            )
    finally:
        await beta_engine.dispose()
    return {
        "event_id": event_id,
        "event_seq": event_seq,
        "outbox_status": outbox_status_value,
        "outbox_error": outbox_error,
        "inbox_status": inbox_status,
        "inbox_error": inbox_error,
        "replica_name": replica_name,
        "replica_last_seq": replica_last_seq,
    }


async def verify_remote_application_directory(
    alpha: httpx.AsyncClient,
    beta: httpx.AsyncClient,
    alice: dict[str, str],
    bob: dict[str, str],
    *,
    application_id: int,
    application_ref: str,
    bot_ref: str,
) -> str:
    """Exercise Alpha discovery, curation, policy, and Add App resolution from Beta."""

    screenshot_id = await seed_application_directory_fixture(
        ALPHA_DATABASE_URL,
        application_id=application_id,
        application_domain="alpha.localhost",
        reviewer_id=9_000_000_000_001,
        reviewer_domain="alpha.localhost",
    )
    enabled = await alpha.patch(
        f"/api/v1/applications/{application_ref}",
        headers=alice,
        json={
            "directory_enabled": True,
            "directory_media": [
                {"type": "image", "asset_id": str(screenshot_id)},
                {"type": "youtube", "video_id": DIRECTORY_YOUTUBE_ID},
            ],
            "directory_external_links": DIRECTORY_EXTERNAL_LINKS,
            "directory_supported_locales": DIRECTORY_SUPPORTED_LOCALES,
            "directory_description_localizations": DIRECTORY_DESCRIPTION_LOCALIZATIONS,
        },
    )
    require(enabled.status_code == 200, f"Directory opt-in failed: {enabled.text}")
    require(
        enabled.json().get("directory_enabled") is True
        and enabled.json().get("directory_approved") is False,
        "Directory opt-in bypassed operator approval",
    )

    search_params: dict[str, str | int] = {
        "domain": "alpha.localhost",
        "q": "Federated Acceptance Bot",
        "limit": 10,
    }
    hidden_search = await beta.get(
        "/api/v1/application-directory",
        headers=bob,
        params=search_params,
    )
    require(
        hidden_search.status_code == 200,
        f"unapproved remote Directory search failed: {hidden_search.text}",
    )
    _, hidden_items = bounded_directory_page(hidden_search.json(), maximum=10)
    require(
        all(item.get("ref") != application_ref for item in hidden_items),
        "unapproved application leaked into the federated Directory",
    )
    hidden_detail = await beta.get(
        f"/api/v1/application-directory/{application_ref}",
        headers=bob,
    )
    require(
        hidden_detail.status_code == 404,
        f"unapproved remote Directory detail was visible: {hidden_detail.text}",
    )
    unlisted_profile = await beta.get(
        f"/api/v1/application-directory/bot-profiles/{bot_ref}",
        headers=bob,
    )
    require(
        unlisted_profile.status_code == 200
        and unlisted_profile.json().get("bot_ref") == bot_ref
        and unlisted_profile.json().get("application_ref") == application_ref
        and unlisted_profile.json().get("directory_listed") is False,
        f"remote bot profile Add App resolution failed before listing: {unlisted_profile.text}",
    )

    approved = await alpha.patch(
        f"/api/v1/administration/applications/{application_ref}/directory",
        headers=alice,
        json={
            "approved": True,
            "collections": ["featured", "staff-picks"],
            "reason": "Federation acceptance fixture",
        },
    )
    require(approved.status_code == 200, f"Directory approval failed: {approved.text}")
    require(
        approved.json().get("directory_approved") is True
        and approved.json().get("directory_collections") == ["featured", "staff-picks"],
        "Directory approval response lost its curation state",
    )

    visible_search = await beta.get(
        "/api/v1/application-directory",
        headers=bob,
        params={
            **search_params,
            "category": "utilities",
            "tag": "federation",
            "collection": "featured",
        },
    )
    require(
        visible_search.status_code == 200,
        f"approved remote Directory search failed: {visible_search.text}",
    )
    page, visible_items = bounded_directory_page(visible_search.json(), maximum=10)
    require(
        page["collections"] == DIRECTORY_COLLECTION_CATALOG
        and page["selected_collection"] == "featured"
        and page["next_cursor"] is None,
        "remote Directory page lost collection or pagination metadata",
    )
    matches = [item for item in visible_items if item.get("ref") == application_ref]
    require(len(matches) == 1, "approved application was not uniquely discoverable from Beta")
    expected_summary = {
        "id": str(application_id),
        "ref": application_ref,
        "origin_domain": "alpha.localhost",
        "name": "Federated Acceptance Bot",
        "summary": DIRECTORY_PROFILE["directory_summary"],
        "category": DIRECTORY_PROFILE["directory_category"],
        "tags": DIRECTORY_PROFILE["directory_tags"],
        "collections": ["featured", "staff-picks"],
        "icon_hash": None,
        "banner_hash": None,
        "verified": True,
        "install_template": {
            "slug": "federation-check",
            "name": "Federation check",
            "description": None,
            "install_types": ["guild_install", "user_install"],
            "default_install_type": "guild_install",
        },
        "user_install_supported": True,
    }
    require(
        matches[0] == expected_summary,
        "remote Directory summary projection was incomplete or invalid",
    )

    visible_detail = await beta.get(
        f"/api/v1/application-directory/{application_ref}",
        headers=bob,
    )
    require(
        visible_detail.status_code == 200,
        f"approved remote Directory detail failed: {visible_detail.text}",
    )
    detail = visible_detail.json()
    require(isinstance(detail, dict), "remote Directory detail was not an object")
    popular_commands = detail.get("popular_commands")
    require(
        isinstance(popular_commands, list)
        and len(popular_commands) == 1
        and isinstance(popular_commands[0], dict)
        and isinstance(popular_commands[0].get("id"), str)
        and popular_commands[0]["id"].isdecimal()
        and popular_commands[0].get("name") == "federation-check"
        and popular_commands[0].get("description") == "Verify a remote bot interaction.",
        "remote Directory detail lost its popular slash command",
    )
    expected_detail = {
        **expected_summary,
        "description": "Cross-instance runtime acceptance fixture.",
        "support_url": DIRECTORY_PROFILE["support_url"],
        "privacy_policy_url": DIRECTORY_PROFILE["privacy_url"],
        "terms_url": DIRECTORY_PROFILE["terms_url"],
        "media": [
            {
                "type": "image",
                "asset_id": str(screenshot_id),
                **DIRECTORY_IMAGE,
            },
            {"type": "youtube", "video_id": DIRECTORY_YOUTUBE_ID},
        ],
        "external_links": DIRECTORY_EXTERNAL_LINKS,
        "supported_locales": DIRECTORY_SUPPORTED_LOCALES,
        "description_localizations": DIRECTORY_DESCRIPTION_LOCALIZATIONS,
        "popular_commands": popular_commands,
        "similar_apps": [],
    }
    require(
        detail == expected_detail,
        "remote Directory detail product-page projection was incomplete or invalid",
    )
    listed_profile = await beta.get(
        f"/api/v1/application-directory/bot-profiles/{bot_ref}",
        headers=bob,
    )
    require(
        listed_profile.status_code == 200
        and listed_profile.json()
        == {
            "bot_ref": bot_ref,
            "application_ref": application_ref,
            "origin_domain": "alpha.localhost",
            "name": "Federated Acceptance Bot",
            "install_template": expected_summary["install_template"],
            "directory_listed": True,
        },
        f"remote listed bot profile lost its Add App identity: {listed_profile.text}",
    )

    restricted = await alpha.patch(
        f"/api/v1/applications/{application_ref}",
        headers=alice,
        json={"target_policy": "local_only"},
    )
    require(
        restricted.status_code == 200 and restricted.json().get("directory_approved") is True,
        f"Directory target-policy restriction failed: {restricted.text}",
    )
    denied_search = await beta.get(
        "/api/v1/application-directory",
        headers=bob,
        params=search_params,
    )
    require(
        denied_search.status_code == 200,
        f"policy-filtered remote Directory search failed: {denied_search.text}",
    )
    _, denied_items = bounded_directory_page(denied_search.json(), maximum=10)
    require(
        all(item.get("ref") != application_ref for item in denied_items),
        "local-only application leaked into a remote Directory search",
    )
    denied_detail = await beta.get(
        f"/api/v1/application-directory/{application_ref}",
        headers=bob,
    )
    require(
        denied_detail.status_code == 404,
        f"local-only remote Directory detail was visible: {denied_detail.text}",
    )
    denied_profile = await beta.get(
        f"/api/v1/application-directory/bot-profiles/{bot_ref}",
        headers=bob,
    )
    require(
        denied_profile.status_code == 404,
        f"local-only remote bot profile exposed Add App: {denied_profile.text}",
    )

    restored = await alpha.patch(
        f"/api/v1/applications/{application_ref}",
        headers=alice,
        json={"target_policy": "open"},
    )
    require(
        restored.status_code == 200 and restored.json().get("directory_approved") is True,
        f"Directory target-policy restore failed: {restored.text}",
    )
    restored_detail = await beta.get(
        f"/api/v1/application-directory/{application_ref}",
        headers=bob,
    )
    require(
        restored_detail.status_code == 200 and restored_detail.json() == expected_detail,
        f"remote Directory entry did not return after policy restore: {restored_detail.text}",
    )

    install_template = expected_summary["install_template"]
    require(isinstance(install_template, dict), "Directory entry omitted its install template")
    template_slug = cast(dict[str, object], install_template).get("slug")
    require(isinstance(template_slug, str), "Directory install template slug was invalid")
    resolved_invite = await beta.get(f"/api/v1/bot-invites/{application_ref}/{template_slug}")
    require(
        resolved_invite.status_code == 200,
        f"remote Directory Add App resolution failed: {resolved_invite.text}",
    )
    invite = resolved_invite.json()
    invite_application = invite.get("application") if isinstance(invite, dict) else None
    invite_template = invite.get("template") if isinstance(invite, dict) else None
    require(
        isinstance(invite_application, dict)
        and invite_application.get("id") == str(application_id)
        and invite_application.get("origin_domain") == "alpha.localhost"
        and invite_application.get("name") == "Federated Acceptance Bot"
        and invite_application.get("support_url") == DIRECTORY_PROFILE["support_url"]
        and invite_application.get("privacy_url") == DIRECTORY_PROFILE["privacy_url"]
        and invite_application.get("target_policy") == "open"
        and invite_application.get("supported_install_types") == ["guild_install", "user_install"],
        "remote Directory Add App resolution lost application metadata",
    )
    require(
        isinstance(invite_template, dict)
        and invite_template.get("slug") == template_slug
        and invite_template.get("name") == "Federation check"
        and invite_template.get("scopes") == BOT_RUNTIME_SCOPES
        and invite_template.get("intents") == BOT_RUNTIME_INTENTS
        and invite_template.get("permissions") == "8"
        and invite_template.get("contexts") == ["guild"]
        and invite_template.get("e2ee_mode") == "disabled",
        "remote Directory Add App resolution lost install-template grants",
    )
    return cast(str, template_slug)


async def verify_remote_bot_runtime(
    alpha: httpx.AsyncClient,
    beta: httpx.AsyncClient,
    alice: dict[str, str],
    bob: dict[str, str],
) -> None:
    """Exercise an Alpha-owned application directly against a Beta guild.

    This deliberately uses the same worker assertions and request proofs as the
    public SDK while keeping the validation container's internal HTTP transport
    separate from the externally signed HTTPS audience.
    """

    created_application = await alpha.post(
        "/api/v1/applications",
        headers=alice,
        json={
            "name": "Federated Acceptance Bot",
            "description": "Cross-instance runtime acceptance fixture.",
        },
    )
    require(
        created_application.status_code == 201,
        f"bot application creation failed: {created_application.text}",
    )
    application_ref = entity_ref(created_application.json())
    bot_user = created_application.json().get("bot_user")
    bot_ref = bot_user.get("ref") if isinstance(bot_user, dict) else None
    require(
        isinstance(bot_ref, str) and bot_ref.endswith("@alpha.localhost"),
        "bot application creation omitted its authority-qualified bot identity",
    )
    qualified_bot_ref = cast(str, bot_ref)
    configured_application = await alpha.patch(
        f"/api/v1/applications/{application_ref}",
        headers=alice,
        json={
            "default_scopes": BOT_RUNTIME_SCOPES,
            "default_intents": BOT_RUNTIME_INTENTS,
            "default_permissions": "8",
            "supported_install_types": ["guild_install", "user_install"],
            "user_install_scopes": ["applications.commands", "interactions.respond"],
            "user_install_contexts": ["guild", "bot_dm", "private_channel"],
            "e2ee_modes": ["participant"],
            "target_policy": "open",
            **DIRECTORY_PROFILE,
        },
    )
    require(
        configured_application.status_code == 200,
        f"bot application configuration failed: {configured_application.text}",
    )
    registered_commands = await alpha.put(
        f"/api/v1/applications/{application_ref}/commands",
        headers=alice,
        json={
            "commands": [
                {
                    "name": "federation-check",
                    "description": "Verify a remote bot interaction.",
                    "contexts": ["guild", "bot_dm", "private_channel"],
                    "integration_types": ["guild_install", "user_install"],
                }
            ]
        },
    )
    require(
        registered_commands.status_code == 200,
        f"bot command registration failed: {registered_commands.text}",
    )

    private_key = Ed25519PrivateKey.generate()
    public_key = base64url(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    created_worker = await alpha.post(
        f"/api/v1/applications/{application_ref}/workers",
        headers=alice,
        json={
            "name": "federation-validation",
            "public_key": public_key,
            "scopes": BOT_RUNTIME_SCOPES,
            "intents": BOT_RUNTIME_INTENTS,
            "target_domains": ["beta.localhost"],
            "session_limit": 1,
        },
    )
    require(
        created_worker.status_code == 201,
        f"bot worker enrollment failed: {created_worker.text}",
    )
    worker_id = int(created_worker.json()["id"])
    created_template = await alpha.post(
        f"/api/v1/applications/{application_ref}/install-templates",
        headers=alice,
        json={
            "slug": "federation-check",
            "name": "Federation check",
            "scopes": BOT_RUNTIME_SCOPES,
            "intents": BOT_RUNTIME_INTENTS,
            "permissions": "8",
            "contexts": ["guild"],
            "e2ee_mode": "disabled",
        },
    )
    require(
        created_template.status_code == 201,
        f"bot install template creation failed: {created_template.text}",
    )

    application_id = created_application.json().get("id")
    require(
        isinstance(application_id, str)
        and application_id.isascii()
        and application_id.isdecimal()
        and not application_id.startswith("0"),
        "bot application creation returned a noncanonical wire ID",
    )
    directory_template_slug = await verify_remote_application_directory(
        alpha,
        beta,
        alice,
        bob,
        application_id=int(application_id),
        application_ref=application_ref,
        bot_ref=qualified_bot_ref,
    )

    created_guild = await beta.post(
        "/api/v1/guilds",
        headers=bob,
        json={"name": "Remote Bot Runtime"},
    )
    require(created_guild.status_code == 201, f"bot target guild failed: {created_guild.text}")
    guild = created_guild.json()
    guild_ref = entity_ref(guild)
    channel_ref = entity_ref(guild["channels"][0])
    installed = await beta.post(
        f"/api/v1/guilds/{guild_ref}/integrations/bots",
        headers=bob,
        params={
            "application_ref": application_ref,
            "template_slug": directory_template_slug,
        },
    )
    require(installed.status_code == 201, f"remote bot installation failed: {installed.text}")
    installation_id = installed.json().get("id")
    require(
        isinstance(installation_id, str) and installation_id.isdecimal(),
        "remote bot installation omitted its stable ID",
    )

    target_path = "/api/v1/bot-workers/targets"

    async def discovered_targets() -> httpx.Response:
        return await alpha.post(
            target_path,
            json=worker_assertion(
                private_key,
                application_ref,
                worker_id,
                "https://alpha.localhost",
                target_path,
            ),
        )

    targets = await wait_for(
        discovered_targets,
        lambda item: (
            item.status_code == 200
            and any(target.get("domain") == "beta.localhost" for target in item.json()["targets"])
        ),
        "application home did not discover its remote guild installation",
        wait_seconds=120,
        poll_seconds=1.0,
    )
    require(
        any(
            target.get("domain") == "beta.localhost"
            and "guild_install" in target.get("install_types", [])
            for target in targets.json()["targets"]
        ),
        "target discovery omitted the remote guild-install capability",
    )

    async def acquired_remote_token() -> httpx.Response:
        # Every attempt carries a fresh signed nonce while Beta waits for the
        # application home's asynchronously delivered runtime projection.
        return await acquire_bot_token(
            beta,
            private_key,
            application_ref,
            worker_id,
            "https://beta.localhost",
        )

    token_response = await wait_for(
        acquired_remote_token,
        lambda item: item.status_code == 200,
        "remote bot runtime projection did not converge before token acquisition",
        wait_seconds=120,
        poll_seconds=1.0,
    )
    token = token_response.json().get("access_token")
    require(isinstance(token, str) and token, "remote bot token response was malformed")
    bot_headers = {"X-Kaede-Bot-Installation": installation_id}

    guilds = await bot_request(
        beta,
        private_key,
        token,
        "GET",
        "/api/v1/bots/guilds",
    )
    require(guilds.status_code == 200, f"remote bot guild list failed: {guilds.text}")
    require(
        any(entity_ref(item) == guild_ref for item in guilds.json()),
        "remote bot token did not expose its Beta guild",
    )
    message_path = f"/api/v1/bots/channels/{channel_ref}/messages"
    bot_message = await beta.post(
        message_path,
        headers={
            **bot_proof_headers(private_key, "POST", message_path, token),
            **bot_headers,
        },
        json={
            "content": "Alpha application speaking directly at Beta.",
            "client_nonce": "federation-bot-runtime-message",
        },
    )
    require(bot_message.status_code == 200, f"remote bot message failed: {bot_message.text}")
    human_history = await beta.get(f"/api/v1/channels/{channel_ref}/messages", headers=bob)
    require(human_history.status_code == 200, f"bot message history failed: {human_history.text}")
    require(
        any(
            item.get("client_nonce") == "federation-bot-runtime-message"
            and item.get("content") == "Alpha application speaking directly at Beta."
            for item in human_history.json()
        ),
        "remote bot message was not visible to the target guild's human member",
    )

    commands = await beta.get(
        f"/api/v1/channels/{channel_ref}/application-commands",
        headers=bob,
    )
    require(commands.status_code == 200, f"remote bot command list failed: {commands.text}")
    require(
        any(
            item.get("application_ref") == application_ref
            and item.get("name") == "federation-check"
            for item in commands.json()
        ),
        "remote installation did not materialize the Alpha command at Beta",
    )
    async with invoke_bot_interaction(
        beta,
        private_key,
        token,
        application_ref,
        worker_id,
        bob,
        channel_ref,
        guild_ref,
        installation_id=installation_id,
    ) as (interaction_id, interaction_token):
        response_path = f"/api/v1/bots/interactions/{interaction_id}/response"
        interaction_response = await beta.post(
            response_path,
            headers={
                **bot_proof_headers(private_key, "POST", response_path, token),
                **bot_headers,
                "X-Kaede-Interaction-Token": interaction_token,
            },
            json={"message": {"content": "Remote command acknowledged."}},
        )
        require(
            interaction_response.status_code == 201,
            f"remote interaction response failed: {interaction_response.text}",
        )
        require(
            interaction_response.json().get("content") == "Remote command acknowledged.",
            "remote interaction response lost its public message content",
        )

    user_installed = await beta.post(
        "/api/v1/users/@me/application-installations",
        headers=bob,
        json={
            "application_ref": application_ref,
            "scopes": ["applications.commands", "interactions.respond"],
            "intents": ["interactions"],
            "contexts": ["guild", "bot_dm", "private_channel"],
        },
    )
    require(
        user_installed.status_code == 201,
        f"remote user installation failed: {user_installed.text}",
    )
    require(
        user_installed.json().get("application_ref") == application_ref,
        "remote user installation was bound to the wrong application",
    )
    user_installation_id = user_installed.json().get("id")
    require(
        isinstance(user_installation_id, str) and user_installation_id.isdecimal(),
        "remote user installation omitted its stable ID",
    )

    user_target_guild = await beta.post(
        "/api/v1/guilds",
        headers=bob,
        json={"name": "User App Runtime"},
    )
    require(
        user_target_guild.status_code == 201,
        f"user-install target guild failed: {user_target_guild.text}",
    )
    user_guild_ref = entity_ref(user_target_guild.json())
    user_channel_ref = entity_ref(user_target_guild.json()["channels"][0])
    user_commands = await beta.get(
        f"/api/v1/channels/{user_channel_ref}/application-commands",
        headers=bob,
    )
    require(
        user_commands.status_code == 200,
        f"user-installed command discovery failed: {user_commands.text}",
    )
    require(
        any(
            item.get("application_ref") == application_ref
            and item.get("name") == "federation-check"
            and "user_install" in item.get("integration_types", [])
            for item in user_commands.json()
        ),
        "remote user installation did not expose its command without a guild installation",
    )
    async with invoke_bot_interaction(
        beta,
        private_key,
        token,
        application_ref,
        worker_id,
        bob,
        user_channel_ref,
        user_guild_ref,
        user_installation_id=user_installation_id,
    ) as (user_interaction_id, user_interaction_token):
        user_response_path = f"/api/v1/bots/interactions/{user_interaction_id}/response"
        user_interaction_response = await beta.post(
            user_response_path,
            headers={
                **bot_proof_headers(
                    private_key,
                    "POST",
                    user_response_path,
                    token,
                ),
                "X-Kaede-Interaction-Token": user_interaction_token,
            },
            json={"message": {"content": "Remote user command acknowledged."}},
        )
        require(
            user_interaction_response.status_code == 201,
            f"remote user-installed interaction response failed: {user_interaction_response.text}",
        )
        require(
            user_interaction_response.json().get("content") == "Remote user command acknowledged.",
            "remote user-installed interaction response lost its public message content",
        )

    async def discovered_user_target() -> httpx.Response:
        return await alpha.post(
            target_path,
            json=worker_assertion(
                private_key,
                application_ref,
                worker_id,
                "https://alpha.localhost",
                target_path,
            ),
        )

    user_targets = await wait_for(
        discovered_user_target,
        lambda item: (
            item.status_code == 200
            and any(
                target.get("domain") == "beta.localhost"
                and "user_install" in target.get("install_types", [])
                for target in item.json()["targets"]
            )
        ),
        "application home did not discover its remote user installation",
        wait_seconds=120,
        poll_seconds=1.0,
    )
    require(
        any(
            target.get("domain") == "beta.localhost"
            and set(target.get("install_types", [])) >= {"guild_install", "user_install"}
            for target in user_targets.json()["targets"]
        ),
        "application target projection did not merge guild and user installations",
    )

    async with invoke_bot_interaction(
        beta,
        private_key,
        token,
        application_ref,
        worker_id,
        bob,
        user_channel_ref,
        user_guild_ref,
        user_installation_id=user_installation_id,
    ) as (pending_user_interaction_id, pending_user_interaction_token):
        removed_user_installation = await beta.delete(
            f"/api/v1/users/@me/application-installations/{user_installation_id}",
            headers=bob,
        )
        require(
            removed_user_installation.status_code == 204,
            f"remote user-install revocation failed: {removed_user_installation.text}",
        )
        revoked_user_response_path = (
            f"/api/v1/bots/interactions/{pending_user_interaction_id}/response"
        )
        revoked_user_response = await beta.post(
            revoked_user_response_path,
            headers={
                **bot_proof_headers(
                    private_key,
                    "POST",
                    revoked_user_response_path,
                    token,
                ),
                "X-Kaede-Interaction-Token": pending_user_interaction_token,
            },
            json={"message": {"content": "This response must be rejected."}},
        )
        require(
            revoked_user_response.status_code == 404,
            "a bot retained access to an interaction after its user installation was revoked",
        )
    commands_after_user_revoke = await beta.get(
        f"/api/v1/channels/{user_channel_ref}/application-commands",
        headers=bob,
    )
    require(
        commands_after_user_revoke.status_code == 200
        and not any(
            item.get("application_ref") == application_ref
            for item in commands_after_user_revoke.json()
        ),
        "a revoked user-installed command remained discoverable without a guild installation",
    )

    targets_after_user_revoke = await wait_for(
        discovered_user_target,
        lambda item: (
            item.status_code == 200
            and any(
                target.get("domain") == "beta.localhost"
                and set(target.get("install_types", [])) == {"guild_install"}
                for target in item.json()["targets"]
            )
        ),
        "application target projection retained a revoked remote user installation",
        wait_seconds=120,
        poll_seconds=1.0,
    )
    require(
        any(
            target.get("domain") == "beta.localhost"
            and set(target.get("install_types", [])) == {"guild_install"}
            for target in targets_after_user_revoke.json()["targets"]
        ),
        "revoking a user installation disturbed the independent guild installation",
    )

    revoked = await alpha.delete(
        f"/api/v1/applications/{application_ref}/workers/{worker_id}",
        headers=alice,
    )
    require(revoked.status_code == 204, f"worker revocation failed: {revoked.text}")

    async def existing_token_after_revoke() -> httpx.Response:
        return await bot_request(
            beta,
            private_key,
            token,
            "GET",
            "/api/v1/bots/guilds",
        )

    await wait_for(
        existing_token_after_revoke,
        lambda item: item.status_code == 401,
        "remote target continued accepting an already-issued revoked-worker token",
    )
    fresh_after_revoke = await acquire_bot_token(
        beta,
        private_key,
        application_ref,
        worker_id,
        "https://beta.localhost",
    )
    require(
        fresh_after_revoke.status_code == 401,
        "remote target minted a new token for a revoked application worker",
    )


async def verify() -> None:
    await seed_user(ALPHA_DATABASE_URL, "alpha.localhost", 9_000_000_000_001, "alice")
    await seed_user(BETA_DATABASE_URL, "beta.localhost", 9_000_000_000_002, "bob")
    async with (
        httpx.AsyncClient(base_url=ALPHA_URL, timeout=15, verify=TLS_CA_FILE or True) as alpha,
        httpx.AsyncClient(base_url=BETA_URL, timeout=15, verify=TLS_CA_FILE or True) as beta,
    ):
        alice = await login(alpha, "alice", "alpha.localhost")
        bob = await login(beta, "bob", "beta.localhost")
        profile_update = await alpha.patch(
            "/api/v1/users/@me",
            headers=alice,
            json={
                "display_name": "Alice Maple",
                "bio": "Testing the federated wire.",
                "custom_status": "Across the maple wire",
            },
        )
        require(profile_update.status_code == 200, f"profile update failed: {profile_update.text}")
        remote_profile = await beta.get(
            "/api/v1/users/lookup?handle=alice%40alpha.localhost",
            headers=bob,
        )
        require(remote_profile.status_code == 200, "versioned remote profile lookup failed")
        require(
            remote_profile.json()["handle"] == "alice@alpha.localhost",
            "lookup identity mismatch",
        )
        require(
            remote_profile.json().get("bio") == "Testing the federated wire."
            and remote_profile.json().get("custom_status") == "Across the maple wire",
            "mutable remote profile fields were not replicated",
        )

        await verify_remote_bot_runtime(alpha, beta, alice, bob)

        friend_request = await alpha.post(
            "/api/v1/users/@me/relationships",
            headers=alice,
            json={"handle": "bob@beta.localhost"},
        )
        require(
            friend_request.status_code == 201,
            f"remote friend request failed: {friend_request.text}",
        )

        async def beta_relationships() -> httpx.Response:
            return await beta.get("/api/v1/users/@me/relationships", headers=bob)

        await wait_for(
            beta_relationships,
            lambda item: (
                item.status_code == 200
                and any(
                    relationship["type"] == "pending_in"
                    and relationship["user"]["handle"] == "alice@alpha.localhost"
                    for relationship in item.json()
                )
            ),
            "remote friend request did not arrive",
        )
        accepted_friend = await beta.put(
            "/api/v1/users/@me/relationships/9000000000001@alpha.localhost",
            headers=bob,
        )
        require(
            accepted_friend.status_code == 200,
            f"remote friend acceptance failed: {accepted_friend.text}",
        )

        async def alpha_relationships() -> httpx.Response:
            return await alpha.get("/api/v1/users/@me/relationships", headers=alice)

        await wait_for(
            alpha_relationships,
            lambda item: (
                item.status_code == 200
                and any(relationship["type"] == "friend" for relationship in item.json())
            ),
            "remote friend acceptance did not converge",
        )
        privacy = await alpha.patch(
            "/api/v1/users/@me/settings",
            headers=alice,
            json={"dm_privacy": "friends"},
        )
        require(privacy.status_code == 200, "friend-only privacy setup failed")
        removed_friend = await alpha.delete(
            "/api/v1/users/@me/relationships/9000000000002@beta.localhost",
            headers=alice,
        )
        require(removed_friend.status_code == 204, "remote friendship removal failed")
        denied_after_removal = await beta.post(
            "/api/v1/users/@me/channels",
            headers=bob,
            json={"handle": "alice@alpha.localhost"},
        )
        require(
            denied_after_removal.status_code == 403,
            "remote peer retained DM privileges after local relationship removal",
        )
        await wait_for(
            beta_relationships,
            lambda item: item.status_code == 200 and not item.json(),
            "remote friendship removal did not converge",
        )
        require(
            await row_count(
                ALPHA_DATABASE_URL,
                Relationship,
                Relationship.user_id == 9_000_000_000_001,
                Relationship.target_id == 9_000_000_000_002,
            )
            == 0,
            "local authoritative relationship survived removal",
        )

        # Reconnect the pair for the DM replication checks below.
        friend_request = await alpha.post(
            "/api/v1/users/@me/relationships",
            headers=alice,
            json={"handle": "bob@beta.localhost"},
        )
        require(friend_request.status_code == 201, "second remote friend request failed")
        await wait_for(
            beta_relationships,
            lambda item: (
                item.status_code == 200
                and any(relationship["type"] == "pending_in" for relationship in item.json())
            ),
            "second remote friend request did not arrive",
        )
        accepted_friend = await beta.put(
            "/api/v1/users/@me/relationships/9000000000001@alpha.localhost",
            headers=bob,
        )
        require(accepted_friend.status_code == 200, "second friend acceptance failed")
        await wait_for(
            alpha_relationships,
            lambda item: (
                item.status_code == 200
                and any(relationship["type"] == "friend" for relationship in item.json())
            ),
            "second friend acceptance did not converge",
        )

        opened = await alpha.post(
            "/api/v1/users/@me/channels",
            headers=alice,
            json={"handle": "bob@beta.localhost"},
        )
        require(opened.status_code == 200, f"federated DM open failed: {opened.text}")
        dm = opened.json()
        dm_id = dm["id"]
        dm_domain = dm["origin_domain"]
        dm_ref = entity_ref(dm)

        async def beta_dms() -> httpx.Response:
            return await beta.get("/api/v1/users/@me/channels", headers=bob)

        await wait_for(
            beta_dms,
            lambda item: (
                item.status_code == 200
                and any(
                    (channel["id"], channel["origin_domain"]) == (dm_id, dm_domain)
                    for channel in item.json()
                )
            ),
            "DM conversation did not replicate",
        )
        async with connect("ws://beta-gateway:8001/gateway?v=1&encoding=json") as socket:
            hello = json.loads(await socket.recv())
            require(hello["op"] == 10, "Beta gateway HELLO missing")
            token = bob["Authorization"].removeprefix("Bearer ")
            await socket.send(json.dumps({"op": 2, "d": {"token": token}}))
            ready = json.loads(await socket.recv())
            require(ready.get("t") == "READY", "Beta gateway READY missing")
            sent_dm = await alpha.post(
                f"/api/v1/channels/{dm_ref}/messages",
                headers=alice,
                json={"content": "Across the maple wire.", "client_nonce": "m3-dm-1"},
            )
            require(sent_dm.status_code == 200, f"federated DM send failed: {sent_dm.text}")
            deadline = time.monotonic() + 15
            delivered_live = False
            while time.monotonic() < deadline:
                dispatch = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
                if (
                    dispatch.get("t") == "MESSAGE_CREATE"
                    and dispatch.get("d", {}).get("client_nonce") == "m3-dm-1"
                ):
                    delivered_live = True
                    break
            require(delivered_live, "federated DM did not reach Bob's live gateway")

        async def beta_dm_history() -> httpx.Response:
            return await beta.get(f"/api/v1/channels/{dm_ref}/messages", headers=bob)

        history = await wait_for(
            beta_dm_history,
            lambda item: (
                item.status_code == 200
                and any(message["client_nonce"] == "m3-dm-1" for message in item.json())
            ),
            "DM message did not replicate",
        )
        require(len(history.json()) == 1, "DM was duplicated on first delivery")

        admin_alpha = {"Authorization": "Bearer alpha-development-admin-token"}
        admin_beta = {"Authorization": "Bearer beta-development-admin-token"}
        await set_outbox_event(ALPHA_DATABASE_URL, "beta.localhost", "m3-dm-1", due=True)
        duplicate_drain = await alpha.post(
            "/api/v1/admin/federation/peers/beta.localhost/drain", headers=admin_alpha
        )
        require(duplicate_drain.status_code == 202, "duplicate delivery did not queue")
        await wait_for(
            lambda: outbox_status(ALPHA_DATABASE_URL, "m3-dm-1"),
            lambda value: value == "delivered",
            "duplicate delivery did not settle",
        )
        require(
            await row_count(BETA_DATABASE_URL, Message, Message.client_nonce == "m3-dm-1") == 1,
            "inbox idempotency allowed a duplicate DM",
        )

        blocked = await beta.put(
            "/api/v1/admin/federation/blocks",
            headers=admin_beta,
            json={"domain": "alpha.localhost", "level": "suspend"},
        )
        require(blocked.status_code == 204, "test peer suspension failed")
        outage_dm = await alpha.post(
            f"/api/v1/channels/{dm_ref}/messages",
            headers=alice,
            json={"content": "Held while Beta sleeps.", "client_nonce": "m3-dm-outage"},
        )
        require(outage_dm.status_code == 200, "outage DM was not durably accepted")
        await wait_for(
            lambda: outbox_status(ALPHA_DATABASE_URL, "m3-dm-outage"),
            lambda value: value == "retry",
            "peer outage did not enter retry state",
        )
        unblocked = await beta.delete(
            "/api/v1/admin/federation/blocks/alpha.localhost", headers=admin_beta
        )
        require(unblocked.status_code == 204, "test peer suspension could not be healed")
        await set_outbox_event(ALPHA_DATABASE_URL, "beta.localhost", "m3-dm-outage", due=True)
        await alpha.post("/api/v1/admin/federation/peers/beta.localhost/drain", headers=admin_alpha)
        await wait_for(
            beta_dm_history,
            lambda item: (
                item.status_code == 200
                and any(message["client_nonce"] == "m3-dm-outage" for message in item.json())
            ),
            "DM did not drain after the peer healed",
        )
        reply = await beta.post(
            f"/api/v1/channels/{dm_ref}/messages",
            headers=bob,
            json={"content": "Beta answers.", "client_nonce": "m3-dm-reply"},
        )
        require(reply.status_code == 200, f"reverse federated DM failed: {reply.text}")

        async def alpha_dm_history() -> httpx.Response:
            return await alpha.get(f"/api/v1/channels/{dm_ref}/messages", headers=alice)

        await wait_for(
            alpha_dm_history,
            lambda item: (
                item.status_code == 200
                and any(message["client_nonce"] == "m3-dm-reply" for message in item.json())
            ),
            "reverse DM did not replicate",
        )

        created = await alpha.post(
            "/api/v1/guilds", headers=alice, json={"name": "Federated Lanterns"}
        )
        require(created.status_code == 201, f"guild creation failed: {created.text}")
        guild = created.json()
        guild_id = guild["id"]
        guild_ref = entity_ref(guild)
        channel = guild["channels"][0]
        channel_ref = entity_ref(channel)
        retained_before_join = await alpha.post(
            f"/api/v1/channels/{channel_ref}/messages",
            headers=alice,
            json={
                "content": "This predates the remote member.",
                "client_nonce": "m3-history-before-join",
            },
        )
        require(
            retained_before_join.status_code == 200,
            f"historical export fixture failed: {retained_before_join.text}",
        )
        emoji_id = 9_000_000_000_100
        emoji_token = f"<:federated_lantern:{emoji_id}@alpha.localhost>"
        await seed_guild_emoji(
            ALPHA_DATABASE_URL,
            domain="alpha.localhost",
            guild_id=int(guild_id),
            creator_id=9_000_000_000_001,
            emoji_id=emoji_id,
        )
        invite = await alpha.post(
            f"/api/v1/guilds/{guild_ref}/invites",
            headers=alice,
            json={"channel_id": channel["id"], "max_uses": 1},
        )
        require(invite.status_code == 200, f"invite creation failed: {invite.text}")
        joined = await beta.post(
            f"/api/v1/invites/{invite.json()['code']}@alpha.localhost", headers=bob
        )
        require(joined.status_code == 200, f"remote guild join failed: {joined.text}")
        replica = await beta.get(f"/api/v1/guilds/{guild_ref}", headers=bob)
        require(replica.status_code == 200, f"replicated guild unavailable: {replica.text}")
        require(entity_ref(replica.json()) == guild_ref, "wrong guild replica identity")
        require(
            any(
                emoji.get("id") == str(emoji_id) and emoji.get("media_hash") == "a" * 64
                for emoji in replica.json().get("emojis", [])
            ),
            "custom emoji metadata was omitted from the guild snapshot",
        )
        emoji_message = await beta.post(
            f"/api/v1/channels/{channel_ref}/messages",
            headers=bob,
            json={"content": emoji_token, "client_nonce": "m3-custom-emoji"},
        )
        require(
            emoji_message.status_code == 200,
            f"remote custom emoji message failed: {emoji_message.text}",
        )
        authoritative_emoji_message = await alpha.get(
            f"/api/v1/channels/{channel_ref}/messages", headers=alice
        )
        require(
            any(
                message.get("client_nonce") == "m3-custom-emoji"
                and message.get("content") == emoji_token
                for message in authoritative_emoji_message.json()
            ),
            "custom emoji identity did not survive the federated proxy write",
        )

        # Reproduce the channel permission editor's deny -> inherit path on a
        # remote member. This guards both the empty-overwrite transition and
        # the replica permission-generation fence used by cached voice ACLs.
        voice_created = await alpha.post(
            f"/api/v1/guilds/{guild_ref}/channels",
            headers=alice,
            json={"name": "federated-voice-acl", "type": 2},
        )
        require(
            voice_created.status_code == 201,
            f"voice permission fixture failed: {voice_created.text}",
        )
        voice_channel = voice_created.json()
        voice_channel_id = voice_channel["id"]
        voice_channel_ref = entity_ref(voice_channel)

        def replicated_voice_permissions(response: httpx.Response) -> int | None:
            if response.status_code != 200:
                return None
            for item in response.json().get("channels", []):
                if item.get("id") == voice_channel_id:
                    return int(item.get("permissions", "0"))
            return None

        # Delivery is durably asynchronous. Drain this fixture synchronously so
        # the permission assertion tests guild replication rather than whether
        # a duplicate Taskiq wake lost an advisory-lock race on a busy runner.
        delivery_settings = get_settings().model_copy(
            update={
                "domain": "alpha.localhost",
                "federation_peer_overrides": {"beta.localhost": BETA_URL},
                "federation_ca_file": TLS_CA_FILE,
            }
        )
        delivery_engine, delivery_sessionmaker = create_engine_and_sessionmaker(ALPHA_DATABASE_URL)
        delivery_redis = Redis.from_url(os.environ["KAEDE_DRAGONFLY_URL"])
        try:

            async def drain_voice_fixture() -> httpx.Response:
                await drain_destination(
                    delivery_sessionmaker,
                    delivery_settings,
                    "beta.localhost",
                    delivery_redis,
                )
                return await beta.get(f"/api/v1/guilds/{guild_ref}", headers=bob)

            try:
                await wait_for(
                    drain_voice_fixture,
                    lambda response: bool(
                        (replicated_voice_permissions(response) or 0) & (1 << 20)
                    ),
                    "replicated voice channel did not grant the default CONNECT permission",
                    wait_seconds=60,
                )
            except VerificationFailure as exc:
                async with delivery_sessionmaker() as diagnostic_session:
                    outbox = (
                        await diagnostic_session.execute(
                            select(
                                FederationOutbox.status,
                                FederationOutbox.attempts,
                                FederationOutbox.last_error,
                                FederationEvent.envelope,
                            )
                            .join(
                                FederationEvent,
                                (
                                    FederationEvent.origin_domain
                                    == FederationOutbox.event_origin_domain
                                )
                                & (FederationEvent.event_id == FederationOutbox.event_id),
                            )
                            .where(FederationEvent.event_type == "guild.channel.create")
                        )
                    ).one()
                beta_engine, beta_sessionmaker = create_engine_and_sessionmaker(BETA_DATABASE_URL)
                try:
                    async with beta_sessionmaker() as beta_session:
                        beta_guild = await beta_session.get(
                            Guild, (int(guild_id), "alpha.localhost")
                        )
                        replica_state = (
                            None
                            if beta_guild is None
                            else (
                                beta_guild.last_event_seq,
                                beta_guild.snapshot_generation,
                                beta_guild.sync_status,
                                beta_guild.sync_error_code,
                            )
                        )
                finally:
                    await beta_engine.dispose()
                context = outbox.envelope.get("context", {})
                raise VerificationFailure(
                    "voice channel replication diagnostic: "
                    f"outbox={(outbox.status, outbox.attempts, outbox.last_error)!r}; "
                    f"event_context={context!r}; replica={replica_state!r}"
                ) from exc
        finally:
            await delivery_redis.aclose()
            await delivery_engine.dispose()
        voice_denied = await alpha.put(
            f"/api/v1/guilds/{guild_ref}/channels/{voice_channel_ref}/overwrites",
            headers=alice,
            json={
                "target_id": guild_ref,
                "target_type": "role",
                "allow": "0",
                "deny": str(1 << 20),
            },
        )
        require(voice_denied.status_code == 200, f"voice deny failed: {voice_denied.text}")
        await wait_for(
            lambda: beta.get(f"/api/v1/guilds/{guild_ref}", headers=bob),
            lambda response: (
                replicated_voice_permissions(response) is not None
                and not (int(replicated_voice_permissions(response) or 0) & (1 << 20))
            ),
            "federated CONNECT denial did not reach the remote member",
            wait_seconds=120,
        )
        voice_inherited = await alpha.put(
            f"/api/v1/guilds/{guild_ref}/channels/{voice_channel_ref}/overwrites",
            headers=alice,
            json={
                "target_id": guild_ref,
                "target_type": "role",
                "allow": "0",
                "deny": "0",
            },
        )
        require(
            voice_inherited.status_code == 200,
            f"voice inherit reset failed: {voice_inherited.text}",
        )
        await wait_for(
            lambda: beta.get(f"/api/v1/guilds/{guild_ref}", headers=bob),
            lambda response: bool((replicated_voice_permissions(response) or 0) & (1 << 20)),
            "CONNECT did not recover after the federated overwrite returned to inherit",
            wait_seconds=120,
        )

        # Presence originates in the gateway, but the worker owns federation
        # signing material. Exercise that boundary over the real broker and
        # confirm the remote member home projects the resulting live event.
        async with (
            connect("ws://beta-gateway:8001/gateway?v=1&encoding=json") as beta_socket,
            connect("ws://alpha-gateway:8001/gateway?v=1&encoding=json") as alpha_socket,
        ):
            beta_hello = json.loads(await beta_socket.recv())
            require(beta_hello["op"] == 10, "Beta presence gateway HELLO missing")
            await beta_socket.send(
                json.dumps(
                    {
                        "op": 2,
                        "d": {"token": bob["Authorization"].removeprefix("Bearer ")},
                    }
                )
            )
            beta_ready = json.loads(await beta_socket.recv())
            require(beta_ready.get("t") == "READY", "Beta presence gateway READY missing")

            alpha_hello = json.loads(await alpha_socket.recv())
            require(alpha_hello["op"] == 10, "Alpha presence gateway HELLO missing")
            await alpha_socket.send(
                json.dumps(
                    {
                        "op": 2,
                        "d": {"token": alice["Authorization"].removeprefix("Bearer ")},
                    }
                )
            )
            alpha_ready = json.loads(await alpha_socket.recv())
            require(alpha_ready.get("t") == "READY", "Alpha presence gateway READY missing")
            await alpha_socket.send(json.dumps({"op": 3, "d": {"status": "idle"}}))

            deadline = time.monotonic() + 20
            federated_presence_arrived = False
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                dispatch = json.loads(await asyncio.wait_for(beta_socket.recv(), timeout=remaining))
                data = dispatch.get("d", {})
                if (
                    dispatch.get("t") == "PRESENCE_UPDATE"
                    and data.get("user_id") == "9000000000001"
                    and data.get("user_domain") == "alpha.localhost"
                    and data.get("status") == "idle"
                ):
                    federated_presence_arrived = True
                    break
            require(
                federated_presence_arrived,
                "signed presence did not cross the gateway/worker federation boundary",
            )
            beta_presence_cache = Redis.from_url(BETA_DRAGONFLY_URL)
            try:
                cached_presence = decode_presence_state(
                    await beta_presence_cache.get("presence:alpha.localhost:9000000000001")
                )
            finally:
                await beta_presence_cache.aclose()
            require(
                cached_presence is not None
                and cached_presence[0] == "idle"
                and cached_presence[2] == [],
                "Dragonfly changed the remote empty activities array during presence receive",
            )

        beta_before_opt_in = await beta.get(
            f"/api/v1/channels/{channel_ref}/messages",
            headers=bob,
        )
        require(beta_before_opt_in.status_code == 200, "replicated channel history is unreadable")
        require(
            not any(
                item["client_nonce"] == "m3-history-before-join"
                for item in beta_before_opt_in.json()
            ),
            "history crossed instances while export policy was disabled",
        )
        authoritative_guild = await alpha.get(f"/api/v1/guilds/{guild_ref}", headers=alice)
        require(
            authoritative_guild.status_code == 200,
            f"authoritative guild refresh failed: {authoritative_guild.text}",
        )
        history_opt_in = await alpha.patch(
            f"/api/v1/guilds/{guild_ref}",
            headers=versioned_headers(alice, authoritative_guild.json()),
            json={"federated_history_policy": "full_retained"},
        )
        require(history_opt_in.status_code == 200, f"history opt-in failed: {history_opt_in.text}")
        await require_guild_policy_delivery("full_retained", label="opt-in")
        await wait_for(
            lambda: beta.get(f"/api/v1/channels/{channel_ref}/messages", headers=bob),
            lambda item: (
                item.status_code == 200
                and any(
                    message["client_nonce"] == "m3-history-before-join" for message in item.json()
                )
            ),
            "permission-bound historical export did not arrive",
            wait_seconds=120,
        )
        require(
            await row_count(
                BETA_DATABASE_URL,
                FederatedHistoryMessage,
                FederatedHistoryMessage.message_id == int(retained_before_join.json()["id"]),
                FederatedHistoryMessage.message_domain == "alpha.localhost",
            )
            == 1,
            "historical message arrived without import provenance",
        )
        history_opt_out = await alpha.patch(
            f"/api/v1/guilds/{guild_ref}",
            headers=versioned_headers(alice, history_opt_in.json()),
            json={"federated_history_policy": "disabled"},
        )
        require(
            history_opt_out.status_code == 200,
            f"history opt-out failed: {history_opt_out.text}",
        )
        await require_guild_policy_delivery("disabled", label="opt-out")
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(Guild.federated_history_policy).where(
                    Guild.id == int(guild_id),
                    Guild.origin_domain == "alpha.localhost",
                ),
            ),
            lambda value: value == "disabled",
            "history opt-out policy did not replicate",
            wait_seconds=10,
        )
        await wait_for(
            lambda: row_count(
                BETA_DATABASE_URL,
                FederatedHistoryMessage,
                FederatedHistoryMessage.message_id == int(retained_before_join.json()["id"]),
                FederatedHistoryMessage.message_domain == "alpha.localhost",
            ),
            lambda value: value == 0,
            "best-effort history purge did not follow policy revocation",
            wait_seconds=45,
        )
        beta_after_opt_out = await beta.get(f"/api/v1/channels/{channel_ref}/messages", headers=bob)
        require(beta_after_opt_out.status_code == 200, "replicated channel history is unreadable")
        require(
            not any(
                message["client_nonce"] == "m3-history-before-join"
                for message in beta_after_opt_out.json()
            ),
            "purged historical message remains readable",
        )

        proxied = await beta.post(
            f"/api/v1/channels/{channel_ref}/messages",
            headers=bob,
            json={"content": "Written through the home.", "client_nonce": "m3-guild-1"},
        )
        require(proxied.status_code == 200, f"remote guild write failed: {proxied.text}")
        require(proxied.json()["origin_domain"] == "alpha.localhost", "home did not mint ID")
        proxied_reply = await beta.post(
            f"/api/v1/channels/{channel_ref}/messages",
            headers=bob,
            json={
                "content": "A federated reply.",
                "client_nonce": "m3-guild-reply",
                "referenced_message_id": (
                    f"{proxied.json()['id']}@{proxied.json()['origin_domain']}"
                ),
            },
        )
        require(
            proxied_reply.status_code == 200,
            f"remote guild reply failed: {proxied_reply.text}",
        )
        require(
            (
                proxied_reply.json()["referenced_message_id"],
                proxied_reply.json()["referenced_message_domain"],
            )
            == (proxied.json()["id"], "alpha.localhost"),
            "remote guild reply lost its composite reference",
        )
        alpha_history = await alpha.get(f"/api/v1/channels/{channel_ref}/messages", headers=alice)
        require(
            any(item["client_nonce"] == "m3-guild-1" for item in alpha_history.json()),
            "authoritative guild message is absent at home",
        )
        require(
            any(item["client_nonce"] == "m3-guild-reply" for item in alpha_history.json()),
            "authoritative guild reply is absent at home",
        )

        durable_gap_wait_seconds = 120

        async def settled_pre_gap_history() -> httpx.Response:
            await drain_peer_outbox(
                ALPHA_DATABASE_URL,
                delivery_settings,
                "beta.localhost",
            )
            return await beta.get(f"/api/v1/channels/{channel_ref}/messages", headers=bob)

        await wait_for(
            settled_pre_gap_history,
            lambda item: (
                item.status_code == 200
                and {"m3-guild-1", "m3-guild-reply"}
                <= {message["client_nonce"] for message in item.json()}
            ),
            "pre-gap guild messages did not settle at the replica",
            wait_seconds=durable_gap_wait_seconds,
            poll_seconds=1.0,
        )
        async with hold_outbox_delivery(ALPHA_DATABASE_URL, "beta.localhost") as gap_session:
            for nonce in ("m3-gap-1", "m3-gap-2"):
                sent = await alpha.post(
                    f"/api/v1/channels/{channel_ref}/messages",
                    headers=alice,
                    json={"content": f"Sequence {nonce}.", "client_nonce": nonce},
                )
                require(sent.status_code == 200, f"gap fixture {nonce} failed")
            parked_gap_events = await park_locked_outbox_events(
                gap_session,
                "beta.localhost",
                {"m3-gap-1", "m3-gap-2"},
            )
        gap_1_event_id, gap_1_event = parked_gap_events["m3-gap-1"]
        gap_2_event_id, gap_2_event = parked_gap_events["m3-gap-2"]
        require(gap_1_event_id != gap_2_event_id, "gap fixtures reused an event identity")
        gap_1_sequence = guild_event_sequence(gap_1_event, label="first gap event")
        gap_2_sequence = guild_event_sequence(gap_2_event, label="second gap event")
        require(
            gap_2_sequence == gap_1_sequence + 1,
            "gap fixtures were not consecutive guild events",
        )
        replica_before_gap = await guild_replica_state(
            BETA_DATABASE_URL,
            int(guild_id),
            "alpha.localhost",
        )
        require(
            replica_before_gap == (gap_1_sequence - 1, "ready", False),
            f"gap replica precondition was not stable: {replica_before_gap!r}",
        )
        gap_engine, gap_sessionmaker = create_engine_and_sessionmaker(ALPHA_DATABASE_URL)
        try:
            async with gap_sessionmaker() as gap_session:
                gap_injection = await signed_request(
                    gap_session,
                    delivery_settings,
                    "POST",
                    "beta.localhost",
                    "/_kaede/v1/inbox",
                    payload={"events": [gap_2_event]},
                    request_timeout=15,
                )
        finally:
            await gap_engine.dispose()
        require_single_inbox_result(
            gap_injection,
            event_id=gap_2_event_id,
            status="retry",
            code="KAED_FED_RESYNC_RETRY",
        )

        async def beta_guild_history() -> httpx.Response:
            return await beta.get(f"/api/v1/channels/{channel_ref}/messages", headers=bob)

        gap_history = await wait_for(
            beta_guild_history,
            lambda item: (
                item.status_code == 200
                and {"m3-gap-1", "m3-gap-2"} <= {message["client_nonce"] for message in item.json()}
            ),
            "forced guild sequence gap did not backfill",
            wait_seconds=durable_gap_wait_seconds,
            poll_seconds=1.0,
        )
        require(
            sum(message["client_nonce"] == "m3-gap-1" for message in gap_history.json()) == 1,
            "gap fill duplicated the first guild message",
        )
        replica_after_gap = await wait_for(
            lambda: guild_replica_state(
                BETA_DATABASE_URL,
                int(guild_id),
                "alpha.localhost",
            ),
            lambda value: (
                value is not None and value[0] >= gap_2_sequence and value[1:] == ("ready", False)
            ),
            "gap recovery did not restore a ready replica",
            wait_seconds=durable_gap_wait_seconds,
            poll_seconds=1.0,
        )
        require(replica_after_gap is not None, "gap recovery replica disappeared")

        async def drain_gap_outbox(nonce: str) -> str | None:
            await drain_peer_outbox(
                ALPHA_DATABASE_URL,
                delivery_settings,
                "beta.localhost",
            )
            return await outbox_status(ALPHA_DATABASE_URL, nonce)

        await set_outbox_event(ALPHA_DATABASE_URL, "beta.localhost", "m3-gap-1", due=True)
        await wait_for(
            lambda: drain_gap_outbox("m3-gap-1"),
            lambda value: value == "delivered",
            "late gap event did not drain as a duplicate",
            wait_seconds=durable_gap_wait_seconds,
            poll_seconds=1.0,
        )
        await set_outbox_event(ALPHA_DATABASE_URL, "beta.localhost", "m3-gap-2", due=True)
        await wait_for(
            lambda: drain_gap_outbox("m3-gap-2"),
            lambda value: value == "delivered",
            "gap-triggering event did not settle after background synchronization",
            wait_seconds=durable_gap_wait_seconds,
            poll_seconds=1.0,
        )

        created_channel = await alpha.post(
            f"/api/v1/guilds/{guild_ref}/channels",
            headers=alice,
            json={"name": "remote-lanterns", "type": 0},
        )
        require(
            created_channel.status_code == 201,
            f"structural federation fixture failed: {created_channel.text}",
        )
        mutation_channel = created_channel.json()
        mutation_channel_ref = entity_ref(mutation_channel)
        await wait_for(
            lambda: channel_mutation_state(
                int(mutation_channel["id"]),
                "guild.channel.create",
            ),
            lambda value: value["replica_name"] == "remote-lanterns",
            "granular channel create did not replicate",
            wait_seconds=120,
        )
        updated_channel = await alpha.patch(
            f"/api/v1/guilds/{guild_ref}/channels/{mutation_channel_ref}",
            headers=versioned_headers(alice, mutation_channel),
            json={"name": "lantern-archive", "topic": "Sequenced across homes."},
        )
        require(
            updated_channel.status_code == 200, f"channel update failed: {updated_channel.text}"
        )
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(Channel.name).where(
                    Channel.id == int(mutation_channel["id"]),
                    Channel.origin_domain == "alpha.localhost",
                ),
            ),
            lambda value: value == "lantern-archive",
            "granular channel update did not replicate",
        )

        created_role = await alpha.post(
            f"/api/v1/guilds/{guild_ref}/roles",
            headers=alice,
            json={"name": "Traveler", "permissions": "0"},
        )
        require(created_role.status_code == 200, f"role creation failed: {created_role.text}")
        role = created_role.json()
        role_ref = entity_ref(role)
        updated_role = await alpha.patch(
            f"/api/v1/guilds/{guild_ref}/roles/{role_ref}",
            headers=versioned_headers(alice, role),
            json={"name": "Federated Traveler", "mentionable": True},
        )
        require(updated_role.status_code == 200, f"role update failed: {updated_role.text}")
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(Role.name).where(
                    Role.id == int(role["id"]), Role.origin_domain == "alpha.localhost"
                ),
            ),
            lambda value: value == "Federated Traveler",
            "granular role create/update did not replicate",
        )
        assigned = await alpha.put(
            f"/api/v1/guilds/{guild_ref}/members/9000000000002@beta.localhost/roles/{role_ref}",
            headers=alice,
        )
        require(
            assigned.status_code == 204, f"remote member role assignment failed: {assigned.text}"
        )
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(MemberRole.role_id).where(
                    MemberRole.guild_id == int(guild_id),
                    MemberRole.guild_domain == "alpha.localhost",
                    MemberRole.user_id == 9_000_000_000_002,
                    MemberRole.user_domain == "beta.localhost",
                    MemberRole.role_id == int(role["id"]),
                ),
            ),
            lambda value: value == int(role["id"]),
            "granular member role assignment did not replicate",
        )
        member_update = await alpha.patch(
            f"/api/v1/guilds/{guild_ref}/members/9000000000002@beta.localhost",
            headers=alice,
            json={"nickname": "Maple Courier"},
        )
        require(member_update.status_code == 200, f"member update failed: {member_update.text}")
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(GuildMember.nickname).where(
                    GuildMember.guild_id == int(guild_id),
                    GuildMember.guild_domain == "alpha.localhost",
                    GuildMember.user_id == 9_000_000_000_002,
                    GuildMember.user_domain == "beta.localhost",
                ),
            ),
            lambda value: value == "Maple Courier",
            "granular member update did not replicate",
        )

        mutation_message_response = await alpha.post(
            f"/api/v1/channels/{channel_ref}/messages",
            headers=alice,
            json={"content": "Mutable lantern.", "client_nonce": "m3-mutations"},
        )
        require(
            mutation_message_response.status_code == 200,
            f"mutation message creation failed: {mutation_message_response.text}",
        )
        mutation_message = mutation_message_response.json()
        mutation_message_ref = entity_ref(mutation_message)
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(Message.content).where(
                    Message.id == int(mutation_message["id"]),
                    Message.origin_domain == "alpha.localhost",
                ),
            ),
            lambda value: value == "Mutable lantern.",
            "mutation message did not replicate",
        )
        edited = await alpha.patch(
            f"/api/v1/channels/{channel_ref}/messages/{mutation_message_ref}",
            headers=alice,
            json={"content": "Edited across the federation."},
        )
        require(edited.status_code == 200, f"message edit failed: {edited.text}")
        reaction_emoji = "🏮"
        reacted = await alpha.put(
            f"/api/v1/channels/{channel_ref}/messages/{mutation_message_ref}/reactions/"
            f"{quote(reaction_emoji, safe='')}/@me",
            headers=alice,
        )
        require(reacted.status_code == 204, f"reaction add failed: {reacted.text}")
        pinned = await alpha.put(
            f"/api/v1/channels/{channel_ref}/pins/{mutation_message_ref}", headers=alice
        )
        require(pinned.status_code == 204, f"pin add failed: {pinned.text}")
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(Message.content).where(
                    Message.id == int(mutation_message["id"]),
                    Message.origin_domain == "alpha.localhost",
                ),
            ),
            lambda value: value == "Edited across the federation.",
            "granular message edit did not replicate",
        )
        await wait_for(
            lambda: row_count(
                BETA_DATABASE_URL,
                Reaction,
                Reaction.message_id == int(mutation_message["id"]),
                Reaction.message_domain == "alpha.localhost",
                Reaction.emoji_key == reaction_emoji,
            ),
            lambda value: value == 1,
            "granular reaction add did not replicate",
        )
        await wait_for(
            lambda: row_count(
                BETA_DATABASE_URL,
                Pin,
                Pin.message_id == int(mutation_message["id"]),
                Pin.message_domain == "alpha.localhost",
            ),
            lambda value: value == 1,
            "granular pin add did not replicate",
        )
        unpinned = await alpha.delete(
            f"/api/v1/channels/{channel_ref}/pins/{mutation_message_ref}", headers=alice
        )
        require(unpinned.status_code == 204, f"pin removal failed: {unpinned.text}")
        unreacted = await alpha.delete(
            f"/api/v1/channels/{channel_ref}/messages/{mutation_message_ref}/reactions/"
            f"{quote(reaction_emoji, safe='')}/@me",
            headers=alice,
        )
        require(unreacted.status_code == 204, f"reaction removal failed: {unreacted.text}")
        deleted_message = await alpha.delete(
            f"/api/v1/channels/{channel_ref}/messages/{mutation_message_ref}", headers=alice
        )
        require(
            deleted_message.status_code == 204, f"message deletion failed: {deleted_message.text}"
        )
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(Message.deleted_at).where(
                    Message.id == int(mutation_message["id"]),
                    Message.origin_domain == "alpha.localhost",
                ),
            ),
            lambda value: value is not None,
            "granular message deletion did not replicate",
        )
        await wait_for(
            lambda: row_count(
                BETA_DATABASE_URL,
                Reaction,
                Reaction.message_id == int(mutation_message["id"]),
                Reaction.message_domain == "alpha.localhost",
            ),
            lambda value: value == 0,
            "granular reaction removal did not replicate",
        )
        await wait_for(
            lambda: row_count(
                BETA_DATABASE_URL,
                Pin,
                Pin.message_id == int(mutation_message["id"]),
                Pin.message_domain == "alpha.localhost",
            ),
            lambda value: value == 0,
            "granular pin removal did not replicate",
        )

        authoritative_guild = await alpha.get(f"/api/v1/guilds/{guild_ref}", headers=alice)
        require(
            authoritative_guild.status_code == 200,
            f"authoritative guild refresh failed: {authoritative_guild.text}",
        )
        guild_update = await alpha.patch(
            f"/api/v1/guilds/{guild_ref}",
            headers=versioned_headers(alice, authoritative_guild.json()),
            json={"name": "Federated Paper Lanterns"},
        )
        require(guild_update.status_code == 200, f"guild update failed: {guild_update.text}")
        await wait_for(
            lambda: beta.get(f"/api/v1/guilds/{guild_ref}", headers=bob),
            lambda item: (
                item.status_code == 200 and item.json()["name"] == "Federated Paper Lanterns"
            ),
            "granular guild update did not replicate",
        )
        removed_role = await alpha.delete(
            f"/api/v1/guilds/{guild_ref}/roles/{role_ref}", headers=alice
        )
        require(removed_role.status_code == 204, f"role removal failed: {removed_role.text}")
        removed_channel = await alpha.delete(
            f"/api/v1/guilds/{guild_ref}/channels/{mutation_channel_ref}", headers=alice
        )
        require(
            removed_channel.status_code == 200,
            f"channel removal failed: {removed_channel.text}",
        )
        removed_channel_body = removed_channel.json()
        require(
            entity_ref(removed_channel_body) == mutation_channel_ref
            and removed_channel_body.get("guild_id") == str(guild_id)
            and removed_channel_body.get("guild_domain") == "alpha.localhost",
            "channel removal did not return the deleted authority resource",
        )
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(Role.id).where(
                    Role.id == int(role["id"]), Role.origin_domain == "alpha.localhost"
                ),
            ),
            lambda value: value is None,
            "granular role deletion did not replicate",
        )
        await wait_for(
            lambda: database_scalar(
                BETA_DATABASE_URL,
                select(Channel.unavailable).where(
                    Channel.id == int(mutation_channel["id"]),
                    Channel.origin_domain == "alpha.localhost",
                ),
            ),
            lambda value: value is True,
            "granular channel deletion did not produce an inaccessible tombstone",
        )

        leave_guild_created = await alpha.post(
            "/api/v1/guilds", headers=alice, json={"name": "Remote Leave Fixture"}
        )
        require(
            leave_guild_created.status_code == 201,
            f"remote leave guild creation failed: {leave_guild_created.text}",
        )
        leave_guild = leave_guild_created.json()
        leave_guild_ref = entity_ref(leave_guild)
        leave_invite = await alpha.post(
            f"/api/v1/guilds/{leave_guild_ref}/invites",
            headers=alice,
            json={"channel_id": leave_guild["channels"][0]["id"]},
        )
        require(
            leave_invite.status_code == 200,
            f"remote leave invite creation failed: {leave_invite.text}",
        )
        leave_joined = await beta.post(
            f"/api/v1/invites/{leave_invite.json()['code']}@alpha.localhost",
            headers=bob,
        )
        require(
            leave_joined.status_code == 200,
            f"remote leave fixture join failed: {leave_joined.text}",
        )
        remote_left = await beta.delete(
            f"/api/v1/guilds/{leave_guild_ref}/members/@me", headers=bob
        )
        require(remote_left.status_code == 204, f"remote guild leave failed: {remote_left.text}")
        await wait_for(
            lambda: row_count(
                ALPHA_DATABASE_URL,
                GuildMember,
                GuildMember.guild_id == int(leave_guild["id"]),
                GuildMember.guild_domain == "alpha.localhost",
                GuildMember.user_id == 9000000000002,
                GuildMember.user_domain == "beta.localhost",
            ),
            lambda value: value == 0,
            "remote guild leave did not remove authoritative membership",
        )
        await wait_for(
            lambda: beta.get(f"/api/v1/guilds/{leave_guild_ref}", headers=bob),
            lambda item: item.status_code == 404,
            "remote guild leave did not hide and purge the replica",
        )

        rejoin_invite = await alpha.post(
            f"/api/v1/guilds/{guild_ref}/invites",
            headers=alice,
            json={"channel_id": channel["id"]},
        )
        require(
            rejoin_invite.status_code == 200,
            f"instance-ban rejoin fixture failed: {rejoin_invite.text}",
        )
        instance_banned = await alpha.put(
            f"/api/v1/guilds/{guild_ref}/instance-bans/beta.localhost",
            headers=alice,
            json={"reason": "federation acceptance sanction", "expires_at": None},
        )
        require(
            instance_banned.status_code == 204,
            f"remote instance ban failed: {instance_banned.text}",
        )
        active_instance_bans = await alpha.get(
            f"/api/v1/guilds/{guild_ref}/instance-bans", headers=alice
        )
        require(
            active_instance_bans.status_code == 200
            and any(
                item["instance_domain"] == "beta.localhost" for item in active_instance_bans.json()
            ),
            f"remote instance ban was not listed: {active_instance_bans.text}",
        )
        await wait_for(
            lambda: beta.get(f"/api/v1/guilds/{guild_ref}", headers=bob),
            lambda item: item.status_code == 404,
            "origin-wide access revocation did not remove the remote member",
        )
        blocked_rejoin = await beta.post(
            f"/api/v1/invites/{rejoin_invite.json()['code']}@alpha.localhost",
            headers=bob,
        )
        require(
            blocked_rejoin.status_code == 403,
            f"instance-banned origin could still rejoin: {blocked_rejoin.text}",
        )

        drained = await alpha.post(
            "/api/v1/admin/federation/peers/beta.localhost/drain",
            headers=admin_alpha,
        )
        require(drained.status_code == 202, f"manual peer drain failed: {drained.text}")

        await wait_for(
            lambda: row_count(
                ALPHA_DATABASE_URL,
                FederationOutbox,
                FederationOutbox.status.in_(("pending", "retry", "circuit")),
            ),
            lambda value: value == 0,
            "Alpha outbox did not settle after the accepted manual drain",
        )

    expected_dm_nonces = (
        "m3-dm-1",
        "m3-dm-outage",
        "m3-dm-reply",
    )
    expected_guild_nonces = (
        "m3-history-before-join",
        "m3-guild-1",
        "m3-guild-reply",
        "m3-gap-1",
        "m3-gap-2",
        "m3-mutations",
    )
    expected_nonces = (*expected_dm_nonces, *expected_guild_nonces)
    alpha_messages = await row_count(
        ALPHA_DATABASE_URL, Message, Message.client_nonce.in_(expected_nonces)
    )
    beta_messages = await row_count(
        BETA_DATABASE_URL, Message, Message.client_nonce.in_(expected_nonces)
    )
    beta_dm_messages = await row_count(
        BETA_DATABASE_URL,
        Message,
        Message.client_nonce.in_(expected_dm_nonces),
    )
    beta_guild_messages = await row_count(
        BETA_DATABASE_URL,
        Message,
        tuple_(Message.channel_id, Message.channel_domain).in_(
            select(Channel.id, Channel.origin_domain).where(
                Channel.guild_id == int(guild_id),
                Channel.guild_domain == "alpha.localhost",
            )
        ),
    )
    require(
        alpha_messages == len(expected_nonces),
        f"authoritative message count expected {len(expected_nonces)}; received {alpha_messages}",
    )
    require(
        beta_dm_messages == len(expected_dm_nonces),
        f"direct-message replica count expected {len(expected_dm_nonces)} "
        "after guild access removal; "
        f"received {beta_dm_messages}",
    )
    require(
        beta_guild_messages == 0 and beta_messages == beta_dm_messages,
        "revoked guild message cache was retained",
    )
    beta_inbox = await row_count(BETA_DATABASE_URL, FederationInbox)
    require(
        beta_inbox >= 2,
        f"durable federation inbox expected at least 2 rows; received {beta_inbox}",
    )
    guild_replicas = await row_count(
        BETA_DATABASE_URL,
        Guild,
        Guild.id == int(guild_id),
        Guild.origin_domain == "alpha.localhost",
    )
    require(
        guild_replicas == 1,
        f"guild snapshot expected exactly 1 replica; received {guild_replicas}",
    )
    dragonfly = Redis.from_url(BETA_DRAGONFLY_URL)
    try:
        active_links = await dragonfly.zcard("federation:link:connections:alpha.localhost")
    finally:
        await dragonfly.aclose()
    require(active_links >= 1, "durable delivery never established the signed hot link")
    print("M3 two-instance federation verification passed")


if __name__ == "__main__":
    try:
        asyncio.run(verify())
    except VerificationFailure as error:
        raise SystemExit(failure_message("federation", error, "make federation-check")) from None
