from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import e2ee as e2ee_api
from app.api.e2ee import (
    RoomActivationRequest,
    RoomProposalRequest,
    account_vault_chain_root,
    account_vault_digest,
    apply_e2ee_control_metadata,
    encode_base64url,
    get_account_vault_digests,
    protocol_request_digest,
    require_prepared_account_vault,
    validate_remote_room_commit_response,
)
from app.db.models import (
    Channel,
    E2EEAccountVault,
    E2EEAccountVaultDigest,
    E2EEControlRecord,
    Message,
)
from app.federation.schemas import E2EERoomOperationStatusRequest, E2EERoomProxyRequest

OPERATION_ID = "keo_" + "o" * 43
DEVICE_ID = "ked_" + "d" * 43
VAULT_DIGEST = encode_base64url(b"v" * 32)
GROUP_ID = encode_base64url(b"g" * 32)


async def test_guild_room_participant_queries_compile_with_membership_join() -> None:
    from sqlalchemy.dialects import postgresql

    statements: list[str] = []

    async def scalars(statement):
        statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return []

    session = SimpleNamespace(scalars=scalars)
    access = SimpleNamespace(
        guild=SimpleNamespace(id=1, origin_domain="home.example"),
        channel=SimpleNamespace(id=2, origin_domain="home.example"),
    )
    assert await e2ee_api.room_participants(session, AsyncMock(), access) == []
    assert len(statements) == 2
    assert "EXISTS (SELECT guild_members.user_id \nFROM guild_members \nWHERE" in statements[1]


def _actor() -> dict[str, object]:
    return {
        "id": "7",
        "origin_domain": "beta.localhost",
        "username": "remote_user",
        "profile_version": 1,
        "e2ee_device_generation": 1,
    }


def test_room_operation_requests_require_canonical_operation_and_vault_context() -> None:
    proposal = RoomProposalRequest(
        operation_id=OPERATION_ID,
        sender_device_id=DEVICE_ID,
    )
    activation = RoomActivationRequest(
        **proposal.model_dump(),
        policy_generation="2",
        epoch="1",
        group_id=GROUP_ID,
        commit=encode_base64url(b"commit"),
        welcome=encode_base64url(b"welcome"),
        prepared_vault_revision="3",
        prepared_vault_digest=VAULT_DIGEST,
        vault_lease_token=encode_base64url(b"l" * 32),
    )
    assert activation.operation_id == OPERATION_ID
    assert activation.prepared_vault_revision == "3"
    with pytest.raises(ValidationError):
        RoomProposalRequest(
            operation_id="keo_short",
            sender_device_id=DEVICE_ID,
        )
    with pytest.raises(ValidationError):
        RoomActivationRequest.model_validate(
            {**activation.model_dump(), "prepared_vault_digest": VAULT_DIGEST + "="}
        )


async def test_control_capture_migration_defaults_unreconciled_controls_to_audit(
    postgres_schema,
) -> None:
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from app.db.base import Base

    connection = postgres_schema
    migration = importlib.import_module(
        "migrations.versions.e5c7b9a1d204_fail_closed_e2ee_control_capture"
    )
    await connection.execute(
        text(
            "CREATE TABLE e2ee_control_records (id bigint, origin_domain text, channel_id "
            "bigint, channel_domain text, author_id bigint, author_domain text, "
            "policy_generation bigint, epoch bigint, operation text, apply_mode text, "
            "envelope jsonb, created_at timestamptz, room_operation_id text, PRIMARY KEY "
            "(id, origin_domain), CONSTRAINT ck_e2ee_control_records_apply_mode_value CHECK "
            "((operation = 'welcome' AND apply_mode = 'join') OR (operation = 'commit' AND "
            "apply_mode IN ('process', 'audit'))))"
        )
    )
    await connection.execute(
        text(
            "CREATE TABLE messages (id bigint, origin_domain text, channel_id bigint, "
            "channel_domain text, author_id bigint, author_domain text, "
            "encryption_policy_generation bigint, encryption_epoch bigint, e2ee jsonb, "
            "created_at timestamptz DEFAULT now())"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO e2ee_control_records (id, origin_domain, operation, apply_mode, "
            "room_operation_id) VALUES (1, 'home.example', 'welcome', 'join', NULL), (2, "
            "'home.example', 'commit', 'process', NULL), (3, 'home.example', 'welcome', "
            "'join', 'signed-operation'), (4, 'home.example', 'commit', 'process', "
            "'signed-operation')"
        )
    )

    def upgrade(sync):
        with Operations.context(
            MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
        ):
            migration.upgrade()

    await connection.run_sync(upgrade)
    assert (
        await connection.execute(
            text("SELECT id, apply_mode FROM e2ee_control_records ORDER BY id")
        )
    ).all() == [(1, "audit"), (2, "audit"), (3, "join"), (4, "process")]
    await connection.execute(
        text(
            "CREATE TRIGGER capture AFTER INSERT ON messages FOR EACH ROW EXECUTE FUNCTION "
            "kaede_capture_e2ee_control_record()"
        )
    )
    for identifier, operation in ((5, "welcome"), (6, "commit")):
        await connection.execute(
            text(
                "INSERT INTO messages (id, origin_domain, channel_id, channel_domain, "
                "author_id, author_domain, encryption_policy_generation, encryption_epoch, "
                "e2ee) VALUES (:id, 'home.example', 10, 'home.example', 7, 'home.example', "
                "2, 1, jsonb_build_object('operation', CAST(:operation AS text), "
                "'ciphertext', 'sealed'))"
            ),
            {"id": identifier, "operation": operation},
        )
    assert (
        await connection.execute(
            text(
                "SELECT id, apply_mode, envelope->>'ciphertext' FROM e2ee_control_records "
                "WHERE id >= 5 ORDER BY id"
            )
        )
    ).all() == [(5, "audit", "sealed"), (6, "audit", "sealed")]

    def downgrade(sync):
        with Operations.context(
            MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
        ):
            migration.downgrade()

    await connection.run_sync(downgrade)
    assert (
        await connection.execute(
            text("SELECT id, apply_mode FROM e2ee_control_records ORDER BY id")
        )
    ).all() == [
        (1, "join"),
        (2, "process"),
        (3, "join"),
        (4, "process"),
        (5, "join"),
        (6, "process"),
    ]


def test_federation_activation_requires_complete_positive_attestation() -> None:
    common = {
        "channel_id": "10",
        "channel_domain": "alpha.localhost",
        "actor": _actor(),
        "operation_id": OPERATION_ID,
        "sender_device_id": DEVICE_ID,
    }
    proposal = E2EERoomProxyRequest.model_validate(common)
    assert proposal.vault_attested is None
    activation = E2EERoomProxyRequest.model_validate(
        {
            **common,
            "policy_generation": "2",
            "epoch": "1",
            "group_id": GROUP_ID,
            "commit": encode_base64url(b"commit"),
            "welcome": encode_base64url(b"welcome"),
            "prepared_vault_revision": "3",
            "prepared_vault_digest": VAULT_DIGEST,
            "vault_attested": True,
        }
    )
    assert activation.vault_attested is True
    with pytest.raises(ValidationError, match="incomplete"):
        E2EERoomProxyRequest.model_validate({**common, "policy_generation": "2"})
    with pytest.raises(ValidationError, match="attestation"):
        E2EERoomProxyRequest.model_validate(
            {
                **activation.model_dump(),
                "vault_attested": False,
            }
        )


def _remote_commit_result(kind: str = "activate") -> tuple[dict[str, object], dict[str, object]]:
    rendered: dict[str, object] = {
        "id": "10",
        "origin_domain": "alpha.localhost",
        "operation_id": OPERATION_ID,
        "operation_status": "committed",
        "encryption_mode": "e2ee",
        "encryption_state": "active",
        "encryption_policy_generation": "2",
        "encryption_protocol": "mls10",
        "encryption_suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
        "encryption_group_id": GROUP_ID,
        "encryption_epoch": "1",
        "controls": [
            {
                "id": "101",
                "origin_domain": "alpha.localhost",
                "operation": "welcome",
                "apply": True,
            },
            {
                "id": "102",
                "origin_domain": "alpha.localhost",
                "operation": "commit",
                "apply": False,
            },
        ],
    }
    prepared: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "status": "prepared",
        "policy": {
            "mode": "plaintext" if kind == "activate" else "e2ee",
            "state": "proposed" if kind == "activate" else "rekeying",
            "generation": "2",
            "protocol": "mls10",
            "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
            "group_id": GROUP_ID,
            "epoch": None,
        },
        "key_packages": [],
    }
    status: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "kind": kind,
        "status": "committed",
        "prepared": prepared,
        "committed": rendered,
    }
    return rendered, status


@pytest.mark.parametrize("kind", ["activate", "rekey"])
@pytest.mark.parametrize(
    ("target", "key", "value"),
    [
        ("rendered", "operation_id", "keo_" + "x" * 43),
        ("rendered", "encryption_group_id", encode_base64url(b"x" * 32)),
        ("status", "status", "prepared"),
        ("commit", "apply", True),
        ("welcome", "origin_domain", "beta.localhost"),
    ],
)
def test_remote_commit_response_rejects_unbound_authority_results(
    kind: str,
    target: str,
    key: str,
    value: object,
) -> None:
    rendered, status = deepcopy(_remote_commit_result(kind))
    channel = cast(
        Channel,
        SimpleNamespace(id=10, origin_domain="alpha.localhost"),
    )
    validate_remote_room_commit_response(
        rendered,
        status,
        kind=kind,
        operation_id=OPERATION_ID,
        channel=channel,
        policy_generation="2",
        group_id=GROUP_ID,
        authority="alpha.localhost",
    )
    if target == "rendered":
        rendered[key] = value
    elif target == "status":
        status[key] = value
    else:
        controls = cast(list[dict[str, object]], rendered["controls"])
        controls[0 if target == "welcome" else 1][key] = value
    with pytest.raises(HTTPException) as caught:
        validate_remote_room_commit_response(
            rendered,
            status,
            kind=kind,
            operation_id=OPERATION_ID,
            channel=channel,
            policy_generation="2",
            group_id=GROUP_ID,
            authority="alpha.localhost",
        )
    assert caught.value.status_code == 502
    assert caught.value.detail == {"code": "E2EE_ROOM_AUTHORITY_INVALID_RESPONSE"}


def test_operation_status_schema_acceptance() -> None:
    request = E2EERoomOperationStatusRequest.model_validate(
        {
            "channel_id": "10",
            "channel_domain": "alpha.localhost",
            "actor": _actor(),
            "operation_id": OPERATION_ID,
        }
    )
    assert request.operation_id == OPERATION_ID
    assert request.actor.origin_domain == "beta.localhost"


def test_protocol_request_digest_is_canonical_and_context_bound() -> None:
    first = protocol_request_digest("label", {"b": 2, "a": "one"})
    reordered = protocol_request_digest("label", {"a": "one", "b": 2})
    changed_label = protocol_request_digest("other", {"a": "one", "b": 2})
    changed_value = protocol_request_digest("label", {"a": "one", "b": 3})
    assert first == reordered
    assert first != changed_label
    assert first != changed_value


def test_account_vault_digest_binds_format_nonce_and_ciphertext() -> None:
    vault = cast(
        E2EEAccountVault,
        SimpleNamespace(
            format_version=2,
            revision=7,
            nonce=b"n" * 12,
            ciphertext=b"ciphertext",
        ),
    )
    digest = account_vault_digest(vault)
    assert len(digest) == 32
    assert digest.hex() == "6448c7e03807d27468a4276d5bca771bcf8d8ed1620922cec03dd3546fbd782f"
    for field, value in (
        ("format_version", 3),
        ("revision", 8),
        ("nonce", b"m" * 12),
        ("ciphertext", b"other ciphertext"),
    ):
        changed = cast(E2EEAccountVault, SimpleNamespace(**{**vars(vault), field: value}))
        assert digest != account_vault_digest(changed), field


def test_account_vault_chain_root_matches_mobile_and_web_vector() -> None:
    digest = bytes.fromhex("02a2c5fecb100b0c89e61b1b6bac265503e8ab3933958d1ebfd561e02af67b96")
    assert encode_base64url(account_vault_chain_root(bytes(32), 1, digest)) == (
        "CAEkikOBbzZQ0cRXCHB9tNKIKtLoERyk6okiTTReHcU"
    )
    with pytest.raises(ValueError):
        account_vault_chain_root(bytes(31), 1, digest)


@pytest.mark.asyncio
async def test_account_vault_digest_page_is_strict_consecutive_and_canonical() -> None:
    rows = [
        E2EEAccountVaultDigest(
            user_id=7,
            user_domain="alpha.localhost",
            user_is_local=True,
            revision=revision,
            digest=bytes([revision]) * 32,
        )
        for revision in (3, 4, 5)
    ]
    session = SimpleNamespace(scalars=AsyncMock(return_value=rows))
    auth = SimpleNamespace(user=SimpleNamespace(id=7, origin_domain="alpha.localhost"))

    page = await get_account_vault_digests(
        after=2,
        limit=2,
        auth=auth,
        session=session,
    )

    assert page == {
        "digests": [
            {"revision": "3", "digest": encode_base64url(bytes([3]) * 32)},
            {"revision": "4", "digest": encode_base64url(bytes([4]) * 32)},
        ],
        "next_after": "4",
    }

    session.scalars = AsyncMock(return_value=[rows[0], rows[2]])
    with pytest.raises(RuntimeError, match="not consecutive"):
        await get_account_vault_digests(
            after=2,
            limit=2,
            auth=auth,
            session=session,
        )


@pytest.mark.asyncio
async def test_prepared_vault_rechecks_redis_lease_after_durable_user_lock() -> None:
    events: list[str] = []
    user = SimpleNamespace(id=7, origin_domain="alpha.localhost", is_local=True)
    vault = cast(
        E2EEAccountVault,
        SimpleNamespace(
            format_version=2,
            revision=1,
            nonce=b"n" * 12,
            ciphertext=b"opaque-vault",
        ),
    )
    token = encode_base64url(b"l" * 32)

    async def scalar(_: object) -> object:
        events.append("user-lock" if not events else "vault-lock")
        return user if len(events) == 1 else vault

    async def get(_: str) -> str:
        events.append("redis-read")
        return token

    session = SimpleNamespace(scalar=scalar)
    redis = SimpleNamespace(get=get)

    await require_prepared_account_vault(
        session,
        redis,
        user,
        lease_token=token,
        revision="1",
        digest=encode_base64url(account_vault_digest(vault)),
    )

    assert events == ["user-lock", "redis-read", "vault-lock"]


@pytest.mark.asyncio
async def test_stale_vault_holder_is_fenced_if_lease_changes_while_waiting_for_lock() -> None:
    events: list[str] = []
    user = SimpleNamespace(id=7, origin_domain="alpha.localhost", is_local=True)
    stale_token = encode_base64url(b"s" * 32)
    current_token = encode_base64url(b"n" * 32)

    async def scalar(_: object) -> object:
        events.append("user-lock")
        return user

    async def get(_: str) -> str:
        events.append("redis-read")
        return current_token

    with pytest.raises(HTTPException) as caught:
        await require_prepared_account_vault(
            SimpleNamespace(scalar=scalar),
            SimpleNamespace(get=get),
            user,
            lease_token=stale_token,
            revision="1",
            digest=VAULT_DIGEST,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "E2EE_ACCOUNT_VAULT_LEASE_EXPIRED"}
    assert events == ["user-lock", "redis-read"]


@pytest.mark.asyncio
async def test_claiming_operation_retry_releases_room_lock_before_user_package_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = cast(
        object,
        SimpleNamespace(id=7, origin_domain="alpha.localhost", is_local=True),
    )
    channel = cast(
        Channel,
        SimpleNamespace(
            id=10,
            origin_domain="alpha.localhost",
            type=0,
            encryption_mode="plaintext",
            encryption_state="plaintext",
            encryption_policy_generation=0,
        ),
    )
    access = SimpleNamespace(channel=channel, guild=SimpleNamespace())
    payload = RoomProposalRequest(
        operation_id=OPERATION_ID,
        sender_device_id=DEVICE_ID,
    )
    request_digest = e2ee_api._operation_request_digest(
        "activate", channel, cast(object, user), payload
    )
    operation = SimpleNamespace(
        id=OPERATION_ID,
        authority_domain="alpha.localhost",
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        actor_id=7,
        actor_domain="alpha.localhost",
        sender_device_id=DEVICE_ID,
        kind="activate",
        status="claiming",
        request_digest=request_digest,
        base_policy_generation=0,
        policy_generation=1,
        group_id=GROUP_ID,
        participant_refs=[{"id": "7", "domain": "alpha.localhost"}],
        key_packages=[],
        prepared_response=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, None, operation, operation]),
        get=AsyncMock(return_value=user),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        e2ee_api,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(e2ee_api, "require_room_policy_authority", AsyncMock())
    monkeypatch.setattr(e2ee_api, "require_active_sender_device", AsyncMock())
    monkeypatch.setattr(e2ee_api, "room_participants", AsyncMock(return_value=[user]))

    async def claim_after_commit(*_: object, **__: object) -> list[dict[str, str]]:
        assert session.commit.await_count == 1
        return [{"device_id": DEVICE_ID, "key_package": "opaque"}]

    monkeypatch.setattr(e2ee_api, "claim_room_key_packages", claim_after_commit)

    response = await e2ee_api._propose_room_operation(
        "activate",
        access,
        payload,
        SimpleNamespace(user=user),
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="alpha.localhost"),
    )

    assert response["status"] == "prepared"
    assert session.commit.await_count == 2


@pytest.mark.parametrize("kind", ["activate", "rekey"])
async def test_room_commit_saves_controls_before_last_message_reference(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    domain = "alpha.localhost"
    user = SimpleNamespace(id=7, origin_domain=domain)
    channel = SimpleNamespace(
        id=10,
        origin_domain=domain,
        last_message_id=None,
        last_message_domain=None,
        encryption_mode="plaintext" if kind == "activate" else "e2ee",
        encryption_state="plaintext" if kind == "activate" else "rekeying",
        encryption_policy_generation=0,
        encryption_epoch=0,
    )
    access = SimpleNamespace(channel=channel, guild=SimpleNamespace(origin_domain=domain))
    operation = SimpleNamespace(
        id=OPERATION_ID,
        authority_domain=domain,
        channel_id=10,
        channel_domain=domain,
        actor_id=7,
        actor_domain=domain,
        sender_device_id=DEVICE_ID,
        kind=kind,
        status="prepared",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        policy_generation=1,
        base_policy_generation=0,
        group_id=GROUP_ID,
        participant_refs=[{"id": "7", "domain": domain}],
        key_packages=[],
    )
    payload = RoomActivationRequest(
        operation_id=OPERATION_ID,
        sender_device_id=DEVICE_ID,
        policy_generation="1",
        epoch="1",
        group_id=GROUP_ID,
        commit=encode_base64url(b"commit"),
        welcome=encode_base64url(b"welcome"),
        prepared_vault_revision="1",
        prepared_vault_digest=VAULT_DIGEST,
        vault_lease_token=encode_base64url(b"l" * 32),
    )
    pending: list[object] = []
    saved_messages: dict[int, Message] = {}

    async def flush() -> None:
        # Channels are updated before pending message inserts. Enforce the
        # immediate last-message foreign key at that boundary.
        if channel.last_message_id is not None:
            assert channel.last_message_id in saved_messages
        for row in pending:
            if isinstance(row, Message):
                saved_messages[row.id] = row
        pending.clear()

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=operation),
        scalars=AsyncMock(return_value=[]),
        add=pending.append,
        flush=flush,
        commit=AsyncMock(side_effect=flush),
    )
    for name, result in (
        ("load_channel_access", access),
        ("lock_local_channel_mutation", access),
        ("require_room_policy_authority", None),
        ("require_active_sender_device", None),
        ("require_prepared_account_vault", None),
        ("room_participants", [user]),
        ("evict_channel_media_sessions", None),
        ("apply_e2ee_control_metadata", None),
        ("publish_policy_update", None),
        ("queue_guild_mutation", None),
        ("materialize_updated_at", None),
        ("wake_queued_guild_federation", None),
        ("publish_channel_dispatch", None),
    ):
        monkeypatch.setattr(e2ee_api, name, AsyncMock(return_value=result))
    monkeypatch.setattr(e2ee_api, "message_payload", lambda *_: {})
    monkeypatch.setattr(e2ee_api, "channel_payload", lambda *_: {})
    monkeypatch.setattr(e2ee_api, "profile_from_user", lambda *_: {})

    response = await e2ee_api._commit_room_operation(
        kind,
        e2ee_api.EntityRef(f"10@{domain}"),
        payload,
        SimpleNamespace(user=user),
        session,
        AsyncMock(),
        SimpleNamespace(mint=AsyncMock(side_effect=[20, 21])),
        SimpleNamespace(domain=domain),
        actor_home_attested=False,
    )

    assert response["operation_status"] == "committed"
    assert [item["operation"] for item in response["controls"]] == ["welcome", "commit"]
    assert channel.last_message_id == 21
    assert channel.last_message_domain == domain
    assert set(saved_messages) == {20, 21}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_announcement_channel_rejects_e2ee_before_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = cast(
        object,
        SimpleNamespace(id=7, origin_domain="alpha.localhost", is_local=True),
    )
    channel = cast(
        Channel,
        SimpleNamespace(
            id=10,
            origin_domain="alpha.localhost",
            type=5,
            encryption_mode="plaintext",
            encryption_state="plaintext",
            encryption_policy_generation=0,
        ),
    )
    access = SimpleNamespace(channel=channel, guild=SimpleNamespace())
    monkeypatch.setattr(
        e2ee_api,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(e2ee_api, "require_room_policy_authority", AsyncMock())
    monkeypatch.setattr(e2ee_api, "require_active_sender_device", AsyncMock())
    session = SimpleNamespace(scalar=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await e2ee_api._propose_room_operation(
            "activate",
            access,
            RoomProposalRequest(
                operation_id=OPERATION_ID,
                sender_device_id=DEVICE_ID,
            ),
            SimpleNamespace(user=user),
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="alpha.localhost"),
        )

    assert caught.value.detail == {"code": "E2EE_CROSSPOST_UNSUPPORTED"}
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_control_metadata_marks_paired_commit_audit_only() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    message = cast(
        Message,
        SimpleNamespace(
            id=100,
            origin_domain="alpha.localhost",
            channel_id=10,
            channel_domain="alpha.localhost",
            author_id=7,
            author_domain="beta.localhost",
            encryption_policy_generation=2,
            encryption_epoch=1,
            e2ee={"operation": "commit", "ciphertext": "opaque"},
            created_at=datetime(2026, 8, 18, tzinfo=UTC),
        ),
    )
    await apply_e2ee_control_metadata(
        session,
        message,
        {
            "operation_id": OPERATION_ID,
            "operation_domain": "alpha.localhost",
            "apply": False,
        },
        expected_authority="alpha.localhost",
    )
    record = cast(E2EEControlRecord, session.add.call_args.args[0])
    assert record.apply_mode == "audit"
    assert record.room_operation_id == OPERATION_ID


@pytest.mark.asyncio
async def test_operation_control_rejects_missing_or_wrong_authority_metadata() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    message = cast(
        Message,
        SimpleNamespace(e2ee={"operation": "welcome"}),
    )
    with pytest.raises(ValueError, match="metadata"):
        await apply_e2ee_control_metadata(
            session,
            message,
            None,
            expected_authority="alpha.localhost",
        )
    with pytest.raises(ValueError, match="metadata"):
        await apply_e2ee_control_metadata(
            session,
            message,
            {
                "operation_id": OPERATION_ID,
                "operation_domain": "beta.localhost",
                "apply": True,
            },
            expected_authority="alpha.localhost",
        )
