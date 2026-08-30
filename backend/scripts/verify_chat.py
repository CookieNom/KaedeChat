from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote, urlparse

from fastapi.testclient import TestClient

import app.api.auth as auth_api
import app.gateway as gateway_api
from app.chat.presence import PRESENCE_TTL_SECONDS, decode_presence_state
from app.core.gateway_ops import GatewayOp
from app.core.settings import get_settings
from app.db.models import User
from app.email.backends import OutboundEmail
from app.email.outbox import drain_email_outbox
from app.gateway import app as gateway_app
from app.main import app as api_app
from scripts.email_tokens import token_from_email
from scripts.verification import (
    PASSWORD_KDF_VERSION,
    VerificationFailure,
    authentication_secret,
    failure_message,
    password_kdf_metadata,
    receive_dispatch,
    require,
)

PASSWORD = "correct horse battery staple"  # noqa: S105 - disposable validation credential
AUTH_SALT = bytes(range(16))
VAULT_SALT = bytes(reversed(range(16)))
REACTION_EMOJI = "🍁"


def register(
    client: TestClient,
    emails: list[str],
    username: str,
    client_ip: str,
    deliver_mail: Callable[[], None],
) -> None:
    settings = get_settings()
    proxy_secret = settings.proxy_secret
    if proxy_secret is None:
        raise VerificationFailure(
            "KAEDE_PROXY_SECRET is not configured for the validation environment"
        )
    response = client.post(
        "/api/v1/auth/register",
        headers={
            "X-Forwarded-For": client_ip,
            "X-Kaede-Proxy-Secret": proxy_secret.get_secret_value(),
        },
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": authentication_secret(PASSWORD, settings.domain, AUTH_SALT),
            "password_kdf": password_kdf_metadata(
                AUTH_SALT,
                vault_salt=VAULT_SALT,
            ),
        },
    )
    require(response.status_code == 201, f"{username} registration failed: {response.text}")
    deliver_mail()
    verified = client.post(
        "/api/v1/auth/verify-email", json={"token": token_from_email(emails.pop())}
    )
    require(verified.status_code == 200, f"{username} verification failed: {verified.text}")


def login(client: TestClient, username: str) -> dict[str, str]:
    settings = get_settings()
    response = client.post(
        "/api/v1/auth/login",
        headers={"X-Kaede-Client": "mobile"},
        json={
            "identifier": username,
            "password": authentication_secret(PASSWORD, settings.domain, AUTH_SALT),
            "password_kdf_version": PASSWORD_KDF_VERSION,
        },
    )
    require(response.status_code == 200, f"{username} login failed: {response.text}")
    return cast(dict[str, str], response.json())


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Kaede-Client": "mobile"}


def versioned_headers(auth_headers: dict[str, str], resource: dict[str, Any]) -> dict[str, str]:
    version = resource.get("version")
    if not isinstance(version, str) or not version:
        raise VerificationFailure(
            f"API resource omitted the version required for an If-Match request: {resource!r}"
        )
    return {**auth_headers, "If-Match": version}


def string_field(resource: dict[str, Any], field: str, context: str) -> str:
    value = resource.get(field)
    if not isinstance(value, str) or not value:
        raise VerificationFailure(
            f"{context} expected {field} as a non-empty JSON string; received {value!r}"
        )
    return value


def decimal_string_field(resource: dict[str, Any], field: str, context: str) -> str:
    value = string_field(resource, field, context)
    if not value.isdecimal():
        raise VerificationFailure(
            f"{context} expected {field} as a decimal JSON string; received {value!r}"
        )
    return value


def websocket_cookie_headers(token: str) -> dict[str, str]:
    app_url = urlparse(get_settings().app_url)
    return {
        "cookie": f"kc_access={token}",
        "Origin": f"{app_url.scheme}://{app_url.netloc}",
    }


def verify() -> None:
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    emails: list[str] = []

    class CaptureEmailBackend:
        async def send(self, message: OutboundEmail) -> None:
            emails.append(message.text)

    async def suppress_immediate_wake() -> None:
        """Prevent the separately running worker from winning the test claim."""

    def suppress_presence_fanout(user: User, status: str, generation: int) -> None:
        """Avoid sharing Taskiq's loop-bound broker across the dual TestClients."""

        del user, status, generation

    auth_api.wake_email_outbox = suppress_immediate_wake
    # Federation presence fanout is exercised separately by federation acceptance.
    gateway_api.schedule_presence_fanout = suppress_presence_fanout

    with TestClient(api_app) as api, TestClient(gateway_app) as gateway:

        async def cached_presence(
            user_id: str,
        ) -> tuple[str, int, list[dict[str, object]], int | None, bool] | None:
            raw = await gateway_app.state.redis.get(f"presence:schema.localhost:{user_id}")
            return decode_presence_state(raw)

        def read_cached_presence(
            user_id: str,
        ) -> tuple[str, int, list[dict[str, object]], int | None, bool] | None:
            portal = gateway.portal
            if portal is None:
                raise VerificationFailure(
                    "FastAPI's test portal is unavailable; verify the Gateway lifespan started"
                )
            return portal.call(cached_presence, user_id)

        async def claim_cached_presence(user_id: str) -> int:
            return await gateway_api.claim_expired_presence(
                gateway_app.state.redis,
                f"schema.localhost:{user_id}",
                int(time.time()) + PRESENCE_TTL_SECONDS + 1,
            )

        async def drain_mail() -> None:
            await drain_email_outbox(
                api_app.state.sessionmaker,
                get_settings(),
                backend=CaptureEmailBackend(),
            )

        def deliver_mail() -> None:
            portal = api.portal
            if portal is None:
                raise VerificationFailure(
                    "FastAPI's test portal is unavailable; verify the application lifespan started"
                )
            portal.call(drain_mail)

        register(api, emails, "alice", "192.0.2.10", deliver_mail)
        register(api, emails, "bob", "192.0.2.11", deliver_mail)
        register(api, emails, "charlie", "192.0.2.12", deliver_mail)
        alice = login(api, "alice")
        bob = login(api, "bob")
        charlie = login(api, "charlie")
        alice_headers = bearer(alice["access_token"])
        bob_headers = bearer(bob["access_token"])
        charlie_headers = bearer(charlie["access_token"])
        alice_me = api.get("/api/v1/users/@me", headers=alice_headers).json()
        bob_me = api.get("/api/v1/users/@me", headers=bob_headers).json()
        charlie_me = api.get("/api/v1/users/@me", headers=charlie_headers).json()
        alice_id = decimal_string_field(alice_me, "id", "Alice profile")
        bob_id = decimal_string_field(bob_me, "id", "Bob profile")
        charlie_id = decimal_string_field(charlie_me, "id", "Charlie profile")

        created = api.post("/api/v1/guilds", headers=alice_headers, json={"name": "Maple House"})
        require(created.status_code == 201, f"guild create failed: {created.text}")
        guild = created.json()
        guild_id = decimal_string_field(guild, "id", "guild create")
        channel_id = decimal_string_field(guild["channels"][0], "id", "default channel")

        category_created = api.post(
            f"/api/v1/guilds/{guild_id}/channels",
            headers=alice_headers,
            json={"name": "Social", "type": 4},
        )
        require(
            category_created.status_code == 201,
            f"category create failed: {category_created.text}",
        )
        category_id = decimal_string_field(category_created.json(), "id", "category create")
        child_created = api.post(
            f"/api/v1/guilds/{guild_id}/channels",
            headers=alice_headers,
            json={"name": "off-topic", "type": 0, "parent_id": category_id},
        )
        require(
            child_created.status_code == 201,
            f"category child create failed: {child_created.text}",
        )
        child_id = decimal_string_field(child_created.json(), "id", "category child create")
        reordered = api.patch(
            f"/api/v1/guilds/{guild_id}/channels",
            headers=alice_headers,
            json={
                "channels": [
                    {"id": category_id, "position": 0, "parent_id": None},
                    {"id": child_id, "position": 1, "parent_id": category_id},
                    {"id": channel_id, "position": 2, "parent_id": None},
                ]
            },
        )
        require(reordered.status_code == 204, f"channel reorder failed: {reordered.text}")
        require(not reordered.content, "channel reorder returned a body with HTTP 204")
        guild_after_reorder = api.get(f"/api/v1/guilds/{guild_id}", headers=alice_headers)
        require(
            guild_after_reorder.status_code == 200,
            "guild reload after reorder expected HTTP 200; received "
            f"HTTP {guild_after_reorder.status_code}: {guild_after_reorder.text}",
        )
        reordered_channels = guild_after_reorder.json().get("channels")
        reordered_channel_items = (
            [item for item in reordered_channels if isinstance(item, dict)]
            if isinstance(reordered_channels, list)
            else []
        )
        require(
            isinstance(reordered_channels, list)
            and len(reordered_channel_items) == len(reordered_channels)
            and [
                decimal_string_field(item, "id", "reordered guild channel")
                for item in reordered_channel_items
            ]
            == [category_id, child_id, channel_id],
            "channel reorder was not persisted deterministically",
        )
        reordered_child = next(
            (item for item in reordered_channel_items if item.get("id") == child_id),
            None,
        )
        require(
            reordered_child is not None
            and decimal_string_field(
                reordered_child,
                "parent_id",
                "reordered category child",
            )
            == category_id,
            "channel reorder lost the child's category parent",
        )
        require(
            bool(
                int(
                    decimal_string_field(
                        guild_after_reorder.json(),
                        "permissions",
                        "guild permissions",
                    )
                )
                & (1 << 4)
            ),
            "guild response omitted the actor's manage-channels permission",
        )

        invite = api.post(
            f"/api/v1/guilds/{guild_id}/invites",
            headers=alice_headers,
            json={"channel_id": channel_id, "max_uses": 1},
        )
        require(invite.status_code == 200, f"invite create failed: {invite.text}")
        invite_code = string_field(invite.json(), "code", "invite create")
        joined = api.post(f"/api/v1/invites/{invite_code}", headers=bob_headers)
        require(joined.status_code == 200, f"invite accept failed: {joined.text}")
        repeat_invite = api.post(
            f"/api/v1/guilds/{guild_id}/invites",
            headers=alice_headers,
            json={"channel_id": channel_id},
        )
        require(repeat_invite.status_code == 200, "repeat invite creation failed")
        repeat_invite_code = string_field(repeat_invite.json(), "code", "repeat invite")
        repeat_join = api.post(f"/api/v1/invites/{repeat_invite_code}", headers=bob_headers)
        require(
            repeat_join.status_code == 200 and repeat_join.json()["id"] == guild_id,
            f"existing-member invite acceptance was not idempotent: {repeat_join.text}",
        )

        charlie_invite = api.post(
            f"/api/v1/guilds/{guild_id}/invites",
            headers=alice_headers,
            json={"channel_id": channel_id, "max_uses": 1},
        )
        require(charlie_invite.status_code == 200, "Charlie invite creation failed")
        charlie_invite_code = string_field(charlie_invite.json(), "code", "Charlie invite")
        charlie_joined = api.post(f"/api/v1/invites/{charlie_invite_code}", headers=charlie_headers)
        require(charlie_joined.status_code == 200, "Charlie could not join")

        friend_request = api.post(
            "/api/v1/users/@me/relationships",
            headers=alice_headers,
            json={"handle": bob_me["handle"]},
        )
        require(
            friend_request.status_code == 201 and friend_request.json()["type"] == "pending_out",
            f"friend request failed: {friend_request.text}",
        )
        bob_relationships = api.get("/api/v1/users/@me/relationships", headers=bob_headers)
        require(
            bob_relationships.status_code == 200
            and bob_relationships.json()[0]["type"] == "pending_in",
            "incoming friend request was not listed",
        )
        accepted = api.put(f"/api/v1/users/@me/relationships/{alice_id}", headers=bob_headers)
        require(
            accepted.status_code == 200 and accepted.json()["type"] == "friend",
            f"friend request acceptance failed: {accepted.text}",
        )

        blocked = api.put(
            f"/api/v1/users/@me/relationships/{bob_id}/block", headers=charlie_headers
        )
        require(blocked.status_code == 204, "Charlie could not block Bob")
        blocked_dm = api.post(
            "/api/v1/users/@me/channels",
            headers=bob_headers,
            json={"handle": charlie_me["handle"]},
        )
        require(blocked_dm.status_code == 403, "a blocked user opened a direct message")
        unblocked = api.delete(
            f"/api/v1/users/@me/relationships/{bob_id}/block", headers=charlie_headers
        )
        require(unblocked.status_code == 204, "Charlie could not unblock Bob")

        opened_dm = api.post(
            "/api/v1/users/@me/channels",
            headers=bob_headers,
            json={"handle": alice_me["handle"]},
        )
        require(opened_dm.status_code == 200, f"direct message open failed: {opened_dm.text}")
        dm_id = decimal_string_field(opened_dm.json(), "id", "direct message open")
        reopened_dm = api.post(
            "/api/v1/users/@me/channels",
            headers=alice_headers,
            json={"handle": bob_me["handle"]},
        )
        require(
            reopened_dm.status_code == 200 and reopened_dm.json()["id"] == dm_id,
            "direct-message pair was not idempotent",
        )
        outsider_history = api.get(f"/api/v1/channels/{dm_id}/messages", headers=charlie_headers)
        require(outsider_history.status_code == 404, "a non-participant read a direct message")

        moderator = api.post(
            f"/api/v1/guilds/{guild_id}/roles",
            headers=alice_headers,
            json={
                "name": "Moderator",
                "permissions": str(
                    (1 << 1) | (1 << 2) | (1 << 7) | (1 << 27) | (1 << 28) | (1 << 40)
                ),
            },
        )
        require(moderator.status_code == 200, f"moderator role failed: {moderator.text}")
        moderator_id = decimal_string_field(moderator.json(), "id", "moderator role")
        helper = api.post(
            f"/api/v1/guilds/{guild_id}/roles",
            headers=alice_headers,
            json={"name": "Helper", "permissions": str(1 << 6)},
        )
        require(helper.status_code == 200, f"helper role failed: {helper.text}")
        helper_id = decimal_string_field(helper.json(), "id", "helper role")
        reordered = api.patch(
            f"/api/v1/guilds/{guild_id}/roles",
            headers=alice_headers,
            json={
                "roles": [
                    {
                        "id": helper_id,
                        "position": 1,
                        "version": helper.json()["version"],
                    },
                    {
                        "id": moderator_id,
                        "position": 2,
                        "version": moderator.json()["version"],
                    },
                ]
            },
        )
        require(reordered.status_code == 200, f"role positioning failed: {reordered.text}")
        assigned = api.put(
            f"/api/v1/guilds/{guild_id}/members/{bob_id}/roles/{moderator_id}",
            headers=alice_headers,
        )
        require(assigned.status_code == 204, f"moderator assignment failed: {assigned.text}")

        owner_immune = api.patch(
            f"/api/v1/guilds/{guild_id}/members/{alice_id}",
            headers=bob_headers,
            json={"nickname": "Not allowed"},
        )
        require(owner_immune.status_code == 403, "moderator managed the owner")
        equal_role = api.put(
            f"/api/v1/guilds/{guild_id}/members/{charlie_id}/roles/{moderator_id}",
            headers=bob_headers,
        )
        require(equal_role.status_code == 403, "moderator granted their own highest role")
        helper_assigned = api.put(
            f"/api/v1/guilds/{guild_id}/members/{charlie_id}/roles/{helper_id}",
            headers=bob_headers,
        )
        require(helper_assigned.status_code == 204, "moderator could not grant a lower role")
        members = api.get(f"/api/v1/guilds/{guild_id}/members", headers=bob_headers)
        require(
            members.status_code == 200 and len(members.json()) == 3,
            "member list expected HTTP 200 with 3 members; received "
            f"HTTP {members.status_code}: {members.text}",
        )
        charlie_member = next(item for item in members.json() if item["user"]["id"] == charlie_id)
        require(helper_id in charlie_member["role_ids"], "member role was not listed")

        timeout = api.patch(
            f"/api/v1/guilds/{guild_id}/members/{charlie_id}",
            headers={**bob_headers, "X-Audit-Log-Reason": "Cooling-off period"},
            json={"timeout_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        )
        require(timeout.status_code == 200, f"timeout failed: {timeout.text}")
        timed_out_send = api.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=charlie_headers,
            json={"content": "This should be blocked while timed out."},
        )
        require(timed_out_send.status_code == 403, "timed-out member sent a message")
        cleared = api.patch(
            f"/api/v1/guilds/{guild_id}/members/{charlie_id}",
            headers=bob_headers,
            json={"timeout_until": None},
        )
        require(cleared.status_code == 200, "timeout could not be cleared")
        indefinite_timeout = api.patch(
            f"/api/v1/guilds/{guild_id}/members/{charlie_id}",
            headers=bob_headers,
            json={"timeout_indefinite": True},
        )
        require(
            indefinite_timeout.status_code == 200
            and indefinite_timeout.json()["timeout_indefinite"] is True
            and indefinite_timeout.json()["timeout_until"] is None,
            f"indefinite timeout failed: {indefinite_timeout.text}",
        )
        indefinitely_timed_out_send = api.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=charlie_headers,
            json={"content": "This should be blocked indefinitely."},
        )
        require(
            indefinitely_timed_out_send.status_code == 403,
            "indefinitely timed-out member sent a message",
        )
        indefinite_cleared = api.patch(
            f"/api/v1/guilds/{guild_id}/members/{charlie_id}",
            headers=bob_headers,
            json={"timeout_until": None, "timeout_indefinite": False},
        )
        require(indefinite_cleared.status_code == 200, "indefinite timeout could not be cleared")

        before = api.get(f"/api/v1/guilds/{guild_id}", headers=alice_headers).json()
        with gateway.websocket_connect(
            "/gateway?v=1&encoding=json",
            headers=websocket_cookie_headers(bob["access_token"]),
        ) as socket:
            hello = socket.receive_json()
            require(hello["op"] == GatewayOp.HELLO, "gateway HELLO missing")
            socket.send_json({"op": GatewayOp.IDENTIFY, "d": {}})
            ready = socket.receive_json()
            require(ready["t"] == "READY", "gateway READY missing")
            require(ready["d"]["guilds"][0]["id"] == guild_id, "READY guild missing")
            require(
                ready["d"]["dm_channels"][0]["id"] == dm_id,
                "READY direct-message channel missing",
            )
            gateway_session_id = ready["d"]["session_id"]

            socket.send_json(
                {
                    "op": GatewayOp.SUBSCRIBE_MEMBER_LIST,
                    "d": {"guild_id": guild_id, "ranges": [[0, 99]]},
                }
            )
            member_list = receive_dispatch(socket, "GUILD_MEMBER_LIST_UPDATE")
            require(
                member_list["t"] == "GUILD_MEMBER_LIST_UPDATE"
                and len(member_list["d"]["ops"][0]["items"]) == 3,
                "lazy member list missing",
            )

            socket.send_json({"op": GatewayOp.PRESENCE_UPDATE, "d": {"status": "online"}})
            presence = socket.receive_json()
            require(
                presence["t"] == "PRESENCE_UPDATE"
                and presence["d"]["user_id"] == bob_id
                and presence["d"]["user_domain"] == "schema.localhost"
                and presence["d"]["status"] == "online",
                "presence publication failed",
            )
            stored_presence = read_cached_presence(bob_id)
            require(
                stored_presence is not None and stored_presence[2] == [],
                "Dragonfly changed the local empty activities array during presence SET",
            )

            typing = api.post(
                f"/api/v1/channels/{channel_id}@schema.localhost/typing",
                headers=alice_headers,
            )
            require(typing.status_code == 204, f"typing event failed: {typing.text}")
            typing_dispatch = receive_dispatch(socket, "TYPING_START")
            require(
                typing_dispatch["t"] == "TYPING_START"
                and typing_dispatch["d"]["channel_id"] == channel_id
                and typing_dispatch["d"]["channel_domain"] == "schema.localhost"
                and typing_dispatch["d"]["user_domain"] == "schema.localhost",
                "domain-qualified typing dispatch mismatch",
            )

            sent = api.post(
                f"/api/v1/channels/{channel_id}/messages",
                headers=alice_headers,
                json={"content": "The lantern is lit.", "client_nonce": "m2-acceptance-1"},
            )
            require(sent.status_code == 200, f"message create failed: {sent.text}")
            sent_id = decimal_string_field(sent.json(), "id", "message create")
            dispatch = socket.receive_json()
            require(dispatch["t"] == "MESSAGE_CREATE", "message dispatch missing")
            require(
                dispatch["d"]["id"] == sent_id
                and dispatch["d"]["content"] == "The lantern is lit.",
                "dispatch mismatch",
            )

            socket.send_json({"op": GatewayOp.HEARTBEAT, "d": dispatch["s"]})
            ack = socket.receive_json()
            require(ack["op"] == GatewayOp.HEARTBEAT_ACK, "heartbeat ACK missing")
            renewed_presence = read_cached_presence(bob_id)
            require(
                renewed_presence is not None and renewed_presence[2] == [],
                "Dragonfly changed the local empty activities array during presence renewal",
            )
            portal = gateway.portal
            if portal is None:
                raise VerificationFailure(
                    "FastAPI's test portal is unavailable; verify the Gateway lifespan started"
                )
            claimed_generation = portal.call(claim_cached_presence, bob_id)
            claimed_presence = read_cached_presence(bob_id)
            require(
                claimed_generation > 0
                and claimed_presence is not None
                and claimed_presence[1] == claimed_generation
                and claimed_presence[2] == [],
                "Dragonfly changed the local empty activities array during presence claim",
            )
            socket.send_json({"op": GatewayOp.HEARTBEAT, "d": dispatch["s"]})
            require(
                socket.receive_json()["op"] == GatewayOp.HEARTBEAT_ACK,
                "post-claim heartbeat ACK missing",
            )

            dm_sent = api.post(
                f"/api/v1/channels/{dm_id}/messages",
                headers=alice_headers,
                json={"content": "A private paper lantern.", "client_nonce": "m2-dm-1"},
            )
            require(dm_sent.status_code == 200, f"direct message send failed: {dm_sent.text}")
            dm_sent_id = decimal_string_field(dm_sent.json(), "id", "direct message send")
            dm_dispatch = socket.receive_json()
            require(dm_dispatch["t"] == "MESSAGE_CREATE", "direct-message dispatch missing")
            require(
                dm_dispatch["d"]["id"] == dm_sent_id
                and dm_dispatch["d"]["channel_id"] == dm_id
                and dm_dispatch["d"]["content"] == "A private paper lantern.",
                "direct-message dispatch mismatch",
            )
            resume_sequence = dm_dispatch["s"]

        history = api.get(f"/api/v1/channels/{channel_id}/messages", headers=bob_headers)
        require(history.status_code == 200, f"message history failed: {history.text}")
        require(
            history.json()[0]["content"] == "The lantern is lit.",
            "message history newest content did not match 'The lantern is lit.'; "
            f"received {history.json()[0]['content']!r}",
        )
        acked = api.post(
            f"/api/v1/channels/{channel_id}/ack",
            headers=bob_headers,
            json={"message_id": sent_id},
        )
        require(acked.status_code == 204, f"read acknowledgement failed: {acked.text}")
        dm_history = api.get(f"/api/v1/channels/{dm_id}/messages", headers=bob_headers)
        require(
            dm_history.status_code == 200
            and dm_history.json()[0]["content"] == "A private paper lantern.",
            "direct-message history expected HTTP 200 with newest content "
            f"'A private paper lantern.'; received HTTP {dm_history.status_code}: "
            f"{dm_history.text}",
        )

        offline_message = api.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=alice_headers,
            json={
                "content": "@bob, this arrived while your gateway was away.",
                "client_nonce": "m2-resume-mention",
                "mention_user_ids": [bob_id],
            },
        )
        require(
            offline_message.status_code == 200,
            f"offline message failed: {offline_message.text}",
        )
        offline_message_id = decimal_string_field(
            offline_message.json(), "id", "offline message create"
        )
        with gateway.websocket_connect(
            "/gateway?v=1&encoding=json",
            headers=websocket_cookie_headers(bob["access_token"]),
        ) as resumed_socket:
            resume_hello = resumed_socket.receive_json()
            require(resume_hello["op"] == GatewayOp.HELLO, "resume HELLO missing")
            resumed_socket.send_json(
                {
                    "op": GatewayOp.RESUME,
                    "d": {
                        "session_id": gateway_session_id,
                        "seq": resume_sequence,
                    },
                }
            )
            replayed: list[dict[str, object]] = []
            while True:
                resumed_event = resumed_socket.receive_json()
                require(
                    resumed_event.get("op") != GatewayOp.INVALID_SESSION,
                    "gateway rejected a resumable session",
                )
                if resumed_event.get("t") == "RESUMED":
                    break
                replayed.append(resumed_event)
            require(
                any(
                    event.get("t") == "MESSAGE_CREATE"
                    and cast(dict[str, object], event["d"]).get("id") == offline_message_id
                    for event in replayed
                ),
                "gateway resume did not replay the missed message",
            )

        mention_state: dict[str, object] | None = None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            states = api.get("/api/v1/users/@me/read-states", headers=bob_headers)
            require(states.status_code == 200, f"read-state list failed: {states.text}")
            mention_state = next(
                (item for item in states.json() if item["channel_id"] == channel_id), None
            )
            if mention_state is not None and mention_state["mention_count"] == 1:
                break
            time.sleep(0.1)
        require(
            mention_state is not None
            and mention_state["mention_count"] == 1
            and mention_state["unread"] is True,
            "background mention state expected mention_count=1 and unread=true; "
            f"received {mention_state!r}",
        )

        reaction_path = quote(REACTION_EMOJI, safe="")
        reacted = api.put(
            f"/api/v1/channels/{channel_id}/messages/{sent_id}/reactions/{reaction_path}/@me",
            headers=bob_headers,
        )
        require(reacted.status_code == 204, f"reaction create failed: {reacted.text}")
        reaction_removed = api.delete(
            f"/api/v1/channels/{channel_id}/messages/{sent_id}/reactions/{reaction_path}/{bob_id}",
            headers=alice_headers,
        )
        require(
            reaction_removed.status_code == 204,
            f"moderated reaction removal failed: {reaction_removed.text}",
        )
        reacted_again = api.put(
            f"/api/v1/channels/{channel_id}/messages/{sent_id}/reactions/{reaction_path}/@me",
            headers=bob_headers,
        )
        forbidden_group_clear = api.delete(
            f"/api/v1/channels/{channel_id}/messages/{sent_id}/reactions/{reaction_path}",
            headers=bob_headers,
        )
        own_reaction_removed = api.delete(
            f"/api/v1/channels/{channel_id}/messages/{sent_id}/reactions/{reaction_path}/@me",
            headers=bob_headers,
        )
        require(
            reacted_again.status_code == 204
            and forbidden_group_clear.status_code == 403
            and own_reaction_removed.status_code == 204,
            "own reaction removal failed",
        )

        pinned = api.put(
            f"/api/v1/channels/{channel_id}/pins/{sent_id}",
            headers=alice_headers,
        )
        require(pinned.status_code == 204, f"pin create failed: {pinned.text}")
        pins = api.get(f"/api/v1/channels/{channel_id}/pins", headers=bob_headers)
        require(
            pins.status_code == 200 and pins.json()[0]["id"] == sent_id,
            "pin list failed",
        )
        unpinned = api.delete(
            f"/api/v1/channels/{channel_id}/pins/{sent_id}",
            headers=alice_headers,
        )
        empty_pins = api.get(f"/api/v1/channels/{channel_id}/pins", headers=bob_headers)
        require(
            unpinned.status_code == 204
            and empty_pins.status_code == 200
            and empty_pins.json() == [],
            "pin removal failed",
        )

        bulk_messages = [
            api.post(
                f"/api/v1/channels/{channel_id}/messages",
                headers=alice_headers,
                json={"content": f"Bulk moderation candidate {index}."},
            )
            for index in (1, 2)
        ]
        require(
            all(item.status_code == 200 for item in bulk_messages),
            "bulk-delete fixtures expected HTTP 200; received statuses "
            f"{[item.status_code for item in bulk_messages]}",
        )
        bulk_ids = [
            decimal_string_field(item.json(), "id", "bulk-delete fixture") for item in bulk_messages
        ]
        bulk_deleted = api.post(
            f"/api/v1/channels/{channel_id}/messages/bulk-delete",
            headers=alice_headers,
            json={"message_ids": bulk_ids},
        )
        require(bulk_deleted.status_code == 204, f"bulk delete failed: {bulk_deleted.text}")
        bulk_history = api.get(f"/api/v1/channels/{channel_id}/messages", headers=alice_headers)
        deleted_rows = [item for item in bulk_history.json() if item["id"] in bulk_ids]
        require(
            len(deleted_rows) == 2
            and all(item["content"] is None and item["deleted_at"] for item in deleted_rows),
            "bulk delete did not soft-delete every message",
        )

        managed_invite = api.post(
            f"/api/v1/guilds/{guild_id}/invites",
            headers=alice_headers,
            json={"channel_id": channel_id},
        )
        require(managed_invite.status_code == 200, "managed invite could not be created")
        managed_code = string_field(managed_invite.json(), "code", "managed invite")
        denied_invite_list = api.get(f"/api/v1/guilds/{guild_id}/invites", headers=bob_headers)
        require(denied_invite_list.status_code == 403, "non-manager listed guild invites")
        invite_list = api.get(f"/api/v1/guilds/{guild_id}/invites", headers=alice_headers)
        require(
            invite_list.status_code == 200
            and managed_code in {item["code"] for item in invite_list.json()},
            "guild invite list failed",
        )
        revoked_invite = api.delete(f"/api/v1/invites/{managed_code}", headers=alice_headers)
        unavailable_invite = api.get(f"/api/v1/invites/{managed_code}")
        raw_revoked_invite = revoked_invite.json() if revoked_invite.status_code == 200 else None
        revoked_invite_body = raw_revoked_invite if isinstance(raw_revoked_invite, dict) else {}
        raw_revoked_guild = revoked_invite_body.get("guild")
        revoked_guild = raw_revoked_guild if isinstance(raw_revoked_guild, dict) else {}
        require(
            revoked_invite.status_code == 200
            and string_field(revoked_invite_body, "code", "revoked invite") == managed_code
            and decimal_string_field(
                revoked_guild,
                "id",
                "revoked invite guild",
            )
            == guild_id
            and isinstance(revoked_invite_body.get("revoked_at"), str)
            and bool(revoked_invite_body["revoked_at"])
            and unavailable_invite.status_code == 404,
            "invite revocation failed",
        )

        after_message = api.get(f"/api/v1/guilds/{guild_id}", headers=alice_headers).json()
        require(
            decimal_string_field(before, "permission_generation", "guild before messages")
            == decimal_string_field(
                after_message,
                "permission_generation",
                "guild after messages",
            ),
            "message creation changed permission generation",
        )
        denied = api.put(
            f"/api/v1/guilds/{guild_id}/channels/{channel_id}/overwrites",
            headers=alice_headers,
            json={
                "target_id": guild_id,
                "target_type": "role",
                "allow": "0",
                "deny": str((1 << 10) | (1 << 11)),
            },
        )
        require(denied.status_code == 200, f"overwrite failed: {denied.text}")
        with gateway.websocket_connect(
            "/gateway?v=1&encoding=json",
            headers=websocket_cookie_headers(bob["access_token"]),
        ) as filtered_socket:
            require(
                filtered_socket.receive_json()["op"] == GatewayOp.HELLO,
                "filtered READY HELLO missing",
            )
            filtered_socket.send_json({"op": GatewayOp.IDENTIFY, "d": {}})
            filtered_ready = filtered_socket.receive_json()
            require(filtered_ready["t"] == "READY", "filtered READY dispatch missing")
            require(
                channel_id
                not in {state["channel_id"] for state in filtered_ready["d"]["read_states"]},
                "READY leaked read state for a channel hidden by current permissions",
            )
        rejected = api.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=bob_headers,
            json={"content": "This must be rejected."},
        )
        require(rejected.status_code == 403, "channel overwrite did not deny Bob")
        # This acceptance path intentionally exercises more than the normative
        # five-message burst. Wait for one token so the authorization assertion
        # below is not coupled to the independent M6 rate-limit gate.
        time.sleep(1.1)
        owner_send = api.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=alice_headers,
            json={"content": "Owners still bypass overwrites."},
        )
        require(owner_send.status_code == 200, "owner did not bypass channel overwrite")

        banned = api.put(
            f"/api/v1/guilds/{guild_id}/bans/{charlie_id}",
            headers={**bob_headers, "X-Audit-Log-Reason": "Acceptance test"},
            json={
                "delete_message_seconds": 0,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        require(banned.status_code == 204, f"ban failed: {banned.text}")
        bans = api.get(f"/api/v1/guilds/{guild_id}/bans", headers=bob_headers)
        charlie_ban = (
            next(
                (item for item in bans.json() if item.get("user", {}).get("id") == charlie_id),
                None,
            )
            if bans.status_code == 200
            else None
        )
        require(
            bans.status_code == 200
            and charlie_ban is not None
            and charlie_ban["expires_at"] is not None,
            "ban list failed",
        )
        blocked_invite = api.post(
            f"/api/v1/guilds/{guild_id}/invites", headers=alice_headers, json={}
        )
        require(blocked_invite.status_code == 200, "ban-check invite could not be created")
        blocked_invite_code = string_field(blocked_invite.json(), "code", "ban-check invite")
        blocked_join = api.post(f"/api/v1/invites/{blocked_invite_code}", headers=charlie_headers)
        require(blocked_join.status_code == 403, "banned member rejoined")
        unbanned = api.delete(f"/api/v1/guilds/{guild_id}/bans/{charlie_id}", headers=bob_headers)
        require(unbanned.status_code == 204, "unban failed")
        rejoined = api.post(f"/api/v1/invites/{blocked_invite_code}", headers=charlie_headers)
        require(
            rejoined.status_code == 200
            and decimal_string_field(rejoined.json(), "id", "unbanned invite join") == guild_id,
            "unbanned member could not rejoin",
        )
        kicked = api.delete(
            f"/api/v1/guilds/{guild_id}/members/{charlie_id}",
            headers={**bob_headers, "X-Audit-Log-Reason": "Acceptance test complete"},
        )
        require(kicked.status_code == 204, "kick failed")
        audit = api.get(f"/api/v1/guilds/{guild_id}/audit-logs", headers=bob_headers)
        require(audit.status_code == 200 and len(audit.json()) >= 8, "audit log failed")

        lifecycle_created = api.post(
            "/api/v1/guilds", headers=bob_headers, json={"name": "Lifecycle House"}
        )
        require(lifecycle_created.status_code == 201, "lifecycle guild creation failed")
        lifecycle_guild = lifecycle_created.json()
        lifecycle_id = decimal_string_field(lifecycle_guild, "id", "lifecycle guild")
        lifecycle_channel_id = decimal_string_field(
            lifecycle_guild["channels"][0],
            "id",
            "lifecycle default channel",
        )
        lifecycle_invite = api.post(
            f"/api/v1/guilds/{lifecycle_id}/invites",
            headers=bob_headers,
            json={"channel_id": lifecycle_channel_id},
        )
        require(lifecycle_invite.status_code == 200, "lifecycle invite creation failed")
        lifecycle_invite_code = string_field(lifecycle_invite.json(), "code", "lifecycle invite")
        lifecycle_join = api.post(f"/api/v1/invites/{lifecycle_invite_code}", headers=alice_headers)
        require(
            lifecycle_join.status_code == 200
            and decimal_string_field(lifecycle_join.json(), "id", "lifecycle invite join")
            == lifecycle_id,
            "lifecycle guild invite or join failed",
        )
        owner_leave = api.delete(f"/api/v1/guilds/{lifecycle_id}/members/@me", headers=bob_headers)
        require(owner_leave.status_code == 409, "guild owner was allowed to leave directly")
        lifecycle_current = api.get(f"/api/v1/guilds/{lifecycle_id}", headers=bob_headers).json()
        ownership_transfer = api.put(
            f"/api/v1/guilds/{lifecycle_id}/owner",
            headers=versioned_headers(bob_headers, lifecycle_current),
            json={"owner_id": alice_id},
        )
        require(
            ownership_transfer.status_code == 200
            and ownership_transfer.json()["owner_id"] == alice_id
            and ownership_transfer.json()["owner_domain"] == "schema.localhost",
            f"guild ownership transfer failed: {ownership_transfer.text}",
        )
        lifecycle_audit = api.get(
            f"/api/v1/guilds/{lifecycle_id}/audit-logs", headers=alice_headers
        )
        require(
            lifecycle_audit.status_code == 200
            and any(item["action_type"] == 27 for item in lifecycle_audit.json()),
            "guild ownership transfer was not audited",
        )
        lifecycle_left = api.delete(
            f"/api/v1/guilds/{lifecycle_id}/members/@me", headers=bob_headers
        )
        require(lifecycle_left.status_code == 204, "former owner could not leave guild")
        lifecycle_for_delete = api.get(
            f"/api/v1/guilds/{lifecycle_id}", headers=alice_headers
        ).json()
        lifecycle_deleted = api.delete(
            f"/api/v1/guilds/{lifecycle_id}",
            headers=versioned_headers(alice_headers, lifecycle_for_delete),
        )
        lifecycle_missing = api.get(f"/api/v1/guilds/{lifecycle_id}", headers=alice_headers)
        require(
            lifecycle_deleted.status_code == 204 and lifecycle_missing.status_code == 404,
            f"guild deletion failed: {lifecycle_deleted.text}",
        )


def main() -> None:
    verify()
    print("M2 chat verification passed")


if __name__ == "__main__":
    try:
        main()
    except VerificationFailure as error:
        raise SystemExit(failure_message("chat", error, "make chat-check")) from None
