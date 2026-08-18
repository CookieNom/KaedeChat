from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, select, tuple_
from websockets.asyncio.client import connect

from app.auth.security import hash_password
from app.core.settings import get_settings
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
from app.federation.delivery import drain_destination
from scripts.verification import VerificationFailure, failure_message, require

PASSWORD = "correct horse battery staple"  # noqa: S105 - disposable validation credential
ALPHA_URL = os.getenv("ALPHA_URL", "http://alpha-api:8000")
BETA_URL = os.getenv("BETA_URL", "http://beta-api:8000")
ALPHA_DATABASE_URL = os.environ["ALPHA_DATABASE_URL"]
BETA_DATABASE_URL = os.environ["BETA_DATABASE_URL"]
BETA_DRAGONFLY_URL = os.environ["BETA_DRAGONFLY_URL"]
TLS_CA_FILE = os.getenv("TLS_CA_FILE")


def entity_ref(payload: dict[str, Any]) -> str:
    """Render the canonical API reference for a federated entity payload."""

    identifier = payload.get("id")
    domain = payload.get("origin_domain")
    require(isinstance(identifier, str) and identifier.isdecimal(), "entity ID is invalid")
    require(isinstance(domain, str) and bool(domain), "entity origin is invalid")
    return f"{identifier}@{domain}"


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
            if user is None:
                user = User(
                    id=user_id,
                    origin_domain=domain,
                    is_local=True,
                    username=username,
                    email=f"{username}@example.test",
                    password_hash=hash_password(PASSWORD),
                    email_verified_at=datetime.now(UTC),
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


async def wait_for(
    operation: Any, predicate: Any, message: str, *, wait_seconds: float = 60
) -> Any:
    deadline = time.monotonic() + wait_seconds
    last: Any = None
    while time.monotonic() < deadline:
        last = await operation()
        if predicate(last):
            return last
        await asyncio.sleep(0.2)
    if isinstance(last, httpx.Response):
        detail = f"HTTP {last.status_code}: {last.text[:2000]}"
    else:
        detail = repr(last)
    raise VerificationFailure(f"{message}; last observed result: {detail}")


async def login(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"X-Kaede-Client": "mobile"},
        json={"identifier": username, "password": PASSWORD},
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


async def verify() -> None:
    await seed_user(ALPHA_DATABASE_URL, "alpha.localhost", 9_000_000_000_001, "alice")
    await seed_user(BETA_DATABASE_URL, "beta.localhost", 9_000_000_000_002, "bob")
    async with (
        httpx.AsyncClient(base_url=ALPHA_URL, timeout=15, verify=TLS_CA_FILE or True) as alpha,
        httpx.AsyncClient(base_url=BETA_URL, timeout=15, verify=TLS_CA_FILE or True) as beta,
    ):
        alice = await login(alpha, "alice")
        bob = await login(beta, "bob")
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
        require(opened.status_code == 201, f"federated DM open failed: {opened.text}")
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
            require(sent_dm.status_code == 201, f"federated DM send failed: {sent_dm.text}")
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
        require(outage_dm.status_code == 201, "outage DM was not durably accepted")
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
        require(reply.status_code == 201, f"reverse federated DM failed: {reply.text}")

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
            retained_before_join.status_code == 201,
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
        require(invite.status_code == 201, f"invite creation failed: {invite.text}")
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
            emoji_message.status_code == 201,
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
                "federation_peer_overrides": {"beta.localhost": "http://beta-api:8000"},
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
            wait_seconds=60,
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
            wait_seconds=60,
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
        await wait_for(
            lambda: beta.get(f"/api/v1/channels/{channel_ref}/messages", headers=bob),
            lambda item: (
                item.status_code == 200
                and any(
                    message["client_nonce"] == "m3-history-before-join" for message in item.json()
                )
            ),
            "permission-bound historical export did not arrive",
            wait_seconds=45,
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
        opt_out_delivery = await wait_for(
            lambda: guild_policy_outbox_state(
                ALPHA_DATABASE_URL,
                "beta.localhost",
                "disabled",
            ),
            lambda value: value is not None and value[0] in {"delivered", "failed"},
            "history opt-out outbox did not settle",
            wait_seconds=90,
        )
        receiver_error = await database_scalar(
            BETA_DATABASE_URL,
            select(FederationInbox.error).where(
                FederationInbox.origin_domain == "alpha.localhost",
                FederationInbox.event_id == opt_out_delivery[2],
            ),
        )
        require(
            opt_out_delivery[0] == "delivered",
            (
                "history opt-out delivery failed: "
                f"{opt_out_delivery[1]}; receiver error: "
                f"{receiver_error}"
            ),
        )
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
        require(proxied.status_code == 201, f"remote guild write failed: {proxied.text}")
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
            proxied_reply.status_code == 201,
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

        blocked = await beta.put(
            "/api/v1/admin/federation/blocks",
            headers=admin_beta,
            json={"domain": "alpha.localhost", "level": "suspend"},
        )
        require(blocked.status_code == 204, "gap test suspension failed")
        for nonce in ("m3-gap-1", "m3-gap-2"):
            sent = await alpha.post(
                f"/api/v1/channels/{channel_ref}/messages",
                headers=alice,
                json={"content": f"Sequence {nonce}.", "client_nonce": nonce},
            )
            require(sent.status_code == 201, f"gap fixture {nonce} failed")
            await wait_for(
                lambda nonce=nonce: outbox_status(ALPHA_DATABASE_URL, nonce),
                lambda value: value == "retry",
                f"gap fixture {nonce} was not retained",
            )
        await beta.delete("/api/v1/admin/federation/blocks/alpha.localhost", headers=admin_beta)
        await set_outbox_event(ALPHA_DATABASE_URL, "beta.localhost", "m3-gap-1", due=False)
        await set_outbox_event(ALPHA_DATABASE_URL, "beta.localhost", "m3-gap-2", due=True)
        await alpha.post("/api/v1/admin/federation/peers/beta.localhost/drain", headers=admin_alpha)

        async def beta_guild_history() -> httpx.Response:
            return await beta.get(f"/api/v1/channels/{channel_ref}/messages", headers=bob)

        gap_history = await wait_for(
            beta_guild_history,
            lambda item: (
                item.status_code == 200
                and {"m3-gap-1", "m3-gap-2"} <= {message["client_nonce"] for message in item.json()}
            ),
            "forced guild sequence gap did not backfill",
        )
        require(
            sum(message["client_nonce"] == "m3-gap-1" for message in gap_history.json()) == 1,
            "gap fill duplicated the first guild message",
        )
        await set_outbox_event(ALPHA_DATABASE_URL, "beta.localhost", "m3-gap-1", due=True)
        await alpha.post("/api/v1/admin/federation/peers/beta.localhost/drain", headers=admin_alpha)
        await wait_for(
            lambda: outbox_status(ALPHA_DATABASE_URL, "m3-gap-1"),
            lambda value: value == "delivered",
            "late gap event did not drain as a duplicate",
        )
        await set_outbox_event(ALPHA_DATABASE_URL, "beta.localhost", "m3-gap-2", due=True)
        await alpha.post("/api/v1/admin/federation/peers/beta.localhost/drain", headers=admin_alpha)
        await wait_for(
            lambda: outbox_status(ALPHA_DATABASE_URL, "m3-gap-2"),
            lambda value: value == "delivered",
            "gap-triggering event did not settle after background synchronization",
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
            wait_seconds=90,
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
        require(created_role.status_code == 201, f"role creation failed: {created_role.text}")
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
            mutation_message_response.status_code == 201,
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
        reacted = await alpha.post(
            f"/api/v1/channels/{channel_ref}/messages/{mutation_message_ref}/reactions",
            headers=alice,
            json={"emoji": "lantern"},
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
                Reaction.emoji_key == "lantern",
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
            f"/api/v1/channels/{channel_ref}/messages/{mutation_message_ref}/reactions/lantern",
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
            removed_channel.status_code == 204,
            f"channel removal failed: {removed_channel.text}",
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
            leave_invite.status_code == 201,
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
            rejoin_invite.status_code == 201,
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

    expected_nonces = (
        "m3-dm-1",
        "m3-dm-outage",
        "m3-dm-reply",
        "m3-history-before-join",
        "m3-guild-1",
        "m3-guild-reply",
        "m3-gap-1",
        "m3-gap-2",
        "m3-mutations",
    )
    alpha_messages = await row_count(
        ALPHA_DATABASE_URL, Message, Message.client_nonce.in_(expected_nonces)
    )
    beta_messages = await row_count(
        BETA_DATABASE_URL, Message, Message.client_nonce.in_(expected_nonces)
    )
    beta_dm_messages = await row_count(
        BETA_DATABASE_URL,
        Message,
        Message.client_nonce.in_(("m3-dm-1", "m3-dm-outage", "m3-dm-reply")),
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
        alpha_messages == 9,
        f"authoritative message count expected 9; received {alpha_messages}",
    )
    require(
        beta_dm_messages == 3,
        "direct-message replica count expected 3 after guild access removal; "
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
    pending = await row_count(
        ALPHA_DATABASE_URL,
        FederationOutbox,
        FederationOutbox.status.in_(("pending", "retry", "circuit")),
    )
    require(pending == 0, f"Alpha outbox expected 0 pending rows; received {pending}")
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
