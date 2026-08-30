from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

import app.api.bot_federation as bot_federation
import app.bots.developer_projection as developer_projection
from app.api.bot_federation import (
    BOT_RUNTIME_MANIFEST_EVENT,
    BotManifest,
    BotRuntimeManifest,
    apply_manifest_application,
    apply_manifest_worker,
    bootstrap_runtime_application_projection,
    federation_worker_authorization,
    fetch_runtime_bot_manifest,
    local_runtime_manifest,
    materialize_manifest_application,
    materialize_manifest_commands,
    materialize_manifest_emojis,
    materialize_manifest_template,
    materialize_manifest_workers,
    materialize_runtime_bot_manifest,
    refresh_remote_worker_authorization,
)
from app.bots.auth import issue_bot_token
from app.bots.developer_projection import (
    DeveloperApplicationProjection,
    apply_developer_team_snapshot,
)
from app.bots.dm_capability import (
    BOT_DM_CAPABILITY_EVENT,
    BotDMCapabilityPayload,
    bot_dm_grant_id,
    capability_fingerprint,
)
from app.db.bot_models import (
    ApplicationCommand,
    ApplicationEmoji,
    BotApplication,
    BotApplicationTarget,
    BotDMCapability,
    BotInstallTemplate,
    BotWorker,
    DeveloperTeam,
    DeveloperTeamMember,
    DeveloperTeamMemberHighwater,
)
from app.db.models import User
from app.federation.network import FederationNetworkError
from app.federation.schemas import EventEnvelope

APP_DOMAIN = "apps.example"
INSTALL_DOMAIN = "guilds.example"
TARGET_DOMAIN = "users.example"
APP_ID = 20
BOT_ID = 10
WORKER_ID = 40
MANIFEST_GENERATION = 7
REVOCATION_GENERATION = 4


def application() -> BotApplication:
    return BotApplication(
        id=APP_ID,
        origin_domain=APP_DOMAIN,
        team_id=30,
        team_domain=APP_DOMAIN,
        bot_user_id=BOT_ID,
        bot_user_domain=APP_DOMAIN,
        name="Weather",
        status="active",
        target_policy="open",
        default_scopes=["dm.send", "messages.metadata"],
        default_intents=["direct_messages"],
        default_permissions=0,
        supported_install_types=["guild_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["guild", "bot_dm", "private_channel"],
        e2ee_modes=[],
        manifest_generation=MANIFEST_GENERATION,
        command_generation=9,
        revocation_generation=REVOCATION_GENERATION,
    )


def bot_user() -> User:
    return User(
        id=BOT_ID,
        origin_domain=APP_DOMAIN,
        is_local=True,
        account_type="bot",
        username="weather_bot",
        password_hash=None,
        profile_version=1,
        e2ee_device_generation=0,
        profile_resolved=True,
    )


def worker() -> BotWorker:
    return BotWorker(
        id=WORKER_ID,
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        name="production",
        public_key=b"w" * 32,
        scopes=["dm.send", "messages.metadata"],
        intents=["direct_messages"],
        target_domains=[],
        generation=3,
    )


def runtime_manifest(*, target_domain: str = TARGET_DOMAIN) -> BotRuntimeManifest:
    return BotRuntimeManifest.model_validate(
        {
            "target_domain": target_domain,
            "application": {
                "id": str(APP_ID),
                "origin_domain": APP_DOMAIN,
                "team_id": "30",
                "team_domain": APP_DOMAIN,
                "name": "Weather",
                "status": "active",
                "target_policy": "open",
                "default_scopes": ["dm.send", "messages.metadata"],
                "default_intents": ["direct_messages"],
                "default_permissions": "0",
                "supported_install_types": ["guild_install"],
                "user_install_scopes": ["applications.commands", "interactions.respond"],
                "user_install_contexts": ["guild", "bot_dm", "private_channel"],
                "e2ee_modes": [],
                "manifest_generation": str(MANIFEST_GENERATION),
                "command_generation": "9",
                "bot_user": {
                    "id": str(BOT_ID),
                    "origin_domain": APP_DOMAIN,
                    "account_type": "bot",
                    "username": "weather_bot",
                },
            },
            "revocation_generation": str(REVOCATION_GENERATION),
            "workers": [
                {
                    "id": str(WORKER_ID),
                    "name": "production",
                    "public_key": base64.urlsafe_b64encode(b"w" * 32).decode().rstrip("="),
                    "scopes": ["dm.send", "messages.metadata"],
                    "intents": ["direct_messages"],
                    "target_domains": [],
                    "generation": "3",
                }
            ],
        }
    )


def bot_manifest() -> BotManifest:
    runtime = runtime_manifest()
    return BotManifest.model_validate(
        {
            "application": runtime.application.model_dump(mode="json"),
            "template": {
                "id": "70",
                "slug": "default",
                "name": "Default",
                "description": "Install Weather",
                "scopes": ["messages.metadata", "dm.send"],
                "intents": ["direct_messages"],
                "permissions": "0",
                "contexts": ["guild"],
                "e2ee_mode": "disabled",
                "generation": "5",
            },
            "workers": [worker.model_dump(mode="json") for worker in runtime.workers],
            "commands": [
                {
                    "id": "50",
                    "name": "forecast",
                    "description": "Show the forecast",
                    "type": "chat_input",
                    "contexts": ["guild"],
                    "integration_types": ["guild_install"],
                }
            ],
            "emojis": [
                {
                    "id": "60",
                    "name": "sunny",
                    "media_hash": "a" * 64,
                    "animated": False,
                    "available": True,
                    "version": "2",
                }
            ],
        }
    )


def developer_application_projection() -> DeveloperApplicationProjection:
    return DeveloperApplicationProjection(
        id=str(APP_ID),
        origin_domain=APP_DOMAIN,
        team_id="30",
        team_domain=APP_DOMAIN,
        name="Weather",
        description=None,
        icon_hash=None,
        banner_hash="b" * 64,
        support_url=None,
        privacy_url=None,
        terms_url="https://apps.example/terms",
        directory_enabled=False,
        directory_approved=False,
        directory_summary="Weather across federated guilds.",
        directory_category=None,
        directory_tags=[],
        directory_collections=[],
        directory_media=[],
        directory_external_links=[],
        directory_supported_locales=[],
        directory_description_localizations={},
        status="active",
        custody_mode="managed",
        target_policy="open",
        default_scopes=["dm.send", "messages.metadata"],
        default_intents=["direct_messages"],
        default_permissions="0",
        supported_install_types=["guild_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["guild", "bot_dm", "private_channel"],
        e2ee_modes=[],
        manifest_generation=str(MANIFEST_GENERATION),
        command_generation="9",
        revocation_generation=str(REVOCATION_GENERATION),
        bot_user={
            "id": str(BOT_ID),
            "origin_domain": APP_DOMAIN,
            "account_type": "bot",
            "username": "weather_bot",
            "profile_version": 1,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("developer_projection_first", [False, True])
async def test_manifest_materialization_uses_authoritative_team_in_either_arrival_order(
    monkeypatch: pytest.MonkeyPatch,
    developer_projection_first: bool,
) -> None:
    manifest = runtime_manifest()
    remote_bot = bot_user()
    remote_bot.is_local = False
    actor = User(
        id=80,
        origin_domain=TARGET_DOMAIN,
        is_local=True,
        account_type="human",
        username="developer",
        password_hash="hash",
        profile_resolved=True,
    )
    projection = developer_application_projection()
    snapshot = {
        "team_id": "30",
        "team_domain": APP_DOMAIN,
        "team_name": "Weather developers",
        "personal": False,
        "revision": "1",
        "member_id": str(actor.id),
        "member_domain": actor.origin_domain,
        "member_role": "developer",
        "applications": [projection.model_dump(mode="json")],
    }
    state = SimpleNamespace(
        team=None,
        application=None,
        member=None,
        highwater=None,
    )
    added: list[object] = []

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is BotApplication:
            return state.application
        if model is User:
            return remote_bot
        if model is DeveloperTeam:
            assert key == (30, APP_DOMAIN)
            return state.team
        if model is DeveloperTeamMember:
            return state.member
        if model is DeveloperTeamMemberHighwater:
            return state.highwater
        if model is BotApplicationTarget:
            return None
        return None

    def add(item: object) -> None:
        added.append(item)
        if isinstance(item, DeveloperTeam):
            state.team = item
        elif isinstance(item, BotApplication):
            state.application = item
        elif isinstance(item, DeveloperTeamMember):
            state.member = item
        elif isinstance(item, DeveloperTeamMemberHighwater):
            state.highwater = item

    async def scalars(_statement: object) -> list[BotApplication]:
        return [state.application] if state.application is not None else []

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(side_effect=get),
        scalars=AsyncMock(side_effect=scalars),
        add=Mock(side_effect=add),
        flush=AsyncMock(),
    )
    bot_upsert = AsyncMock(return_value=remote_bot)
    developer_upsert = AsyncMock(return_value=remote_bot)
    monkeypatch.setattr(bot_federation, "upsert_remote_user", bot_upsert)
    monkeypatch.setattr(developer_projection, "upsert_remote_user", developer_upsert)
    settings = cast(Any, SimpleNamespace(domain=TARGET_DOMAIN))

    if developer_projection_first:
        assert await apply_developer_team_snapshot(
            cast(Any, session),
            settings,
            APP_DOMAIN,
            actor,
            snapshot,
        )

    materialized, _, _, _ = await materialize_manifest_application(
        cast(Any, session),
        manifest,
        settings,
        app_id=APP_ID,
        domain=APP_DOMAIN,
    )

    if not developer_projection_first:
        assert await apply_developer_team_snapshot(
            cast(Any, session),
            settings,
            APP_DOMAIN,
            actor,
            snapshot,
        )

    assert materialized is state.application
    assert (materialized.team_id, materialized.team_domain) == (30, APP_DOMAIN)
    assert state.team.name == "Weather developers"
    assert state.team.federation_revision == 1
    assert state.team.federation_metadata_fingerprint is not None
    assert state.team.federation_applications_fingerprint is not None
    assert state.member.role == "developer"
    assert state.highwater.revision == 1
    assert sum(isinstance(item, DeveloperTeam) for item in added) == 1
    assert sum(isinstance(item, BotApplication) for item in added) == 1
    assert materialized.banner_hash == "b" * 64
    assert materialized.terms_url == "https://apps.example/terms"
    assert materialized.directory_summary == "Weather across federated guilds."
    assert materialized.revocation_generation == REVOCATION_GENERATION


def test_manifest_application_rejects_cross_authority_team_identity() -> None:
    raw = runtime_manifest().application.model_dump(mode="json")
    raw["team_domain"] = "other.example"

    with pytest.raises(ValueError, match="team must belong"):
        bot_federation.ManifestApplication.model_validate(raw)


def test_manifest_application_requires_an_authoritative_bot_profile() -> None:
    raw = runtime_manifest().application.model_dump(mode="json")
    raw["bot_user"] = {**raw["bot_user"], "account_type": "human"}

    with pytest.raises(ValueError, match="application bot must belong"):
        bot_federation.ManifestApplication.model_validate(raw)


@pytest.mark.asyncio
async def test_manifest_materialization_rejects_bot_identity_bound_to_another_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound_application = application()
    bound_application.id = APP_ID + 1

    async def scalar(statement: object) -> object | None:
        if "FROM bot_applications" in str(statement):
            return bound_application
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=scalar),
        get=AsyncMock(),
    )
    upsert = AsyncMock()
    monkeypatch.setattr(bot_federation, "upsert_remote_user", upsert)

    with pytest.raises(FederationNetworkError, match="reuses another application's bot identity"):
        await materialize_manifest_application(
            cast(Any, session),
            runtime_manifest(),
            cast(Any, SimpleNamespace(domain=TARGET_DOMAIN)),
            app_id=APP_ID,
            domain=APP_DOMAIN,
        )

    session.get.assert_not_awaited()
    upsert.assert_not_awaited()
    lock_scopes = [
        next(value for value in call.args[0].compile().params.values() if isinstance(value, str))
        for call in session.scalar.await_args_list[:3]
    ]
    assert lock_scopes == [
        f"bot-manifest:{APP_ID}@{APP_DOMAIN}",
        f"bot-application-user:{BOT_ID}@{APP_DOMAIN}",
        f"developer-team-projection:30@{APP_DOMAIN}",
    ]


def test_manifest_application_generation_replay_equivocation_and_advance() -> None:
    app = application()
    replay_raw = runtime_manifest().model_dump(mode="json")
    replay_raw["application"]["default_scopes"].reverse()
    replay_raw["application"]["user_install_contexts"].reverse()
    replay = BotRuntimeManifest.model_validate(replay_raw)

    assert apply_manifest_application(app, replay, created=False) is False

    equivocation_raw = replay.model_dump(mode="json")
    equivocation_raw["application"]["name"] = "Different Weather"
    equivocation = BotRuntimeManifest.model_validate(equivocation_raw)
    with pytest.raises(FederationNetworkError, match="equivocates at application"):
        apply_manifest_application(app, equivocation, created=False)
    assert app.name == "Weather"

    advance_raw = equivocation.model_dump(mode="json")
    advance_raw["application"]["manifest_generation"] = str(MANIFEST_GENERATION + 1)
    advance = BotRuntimeManifest.model_validate(advance_raw)
    assert apply_manifest_application(app, advance, created=False) is True
    assert (app.name, app.manifest_generation) == (
        "Different Weather",
        MANIFEST_GENERATION + 1,
    )


@pytest.mark.asyncio
async def test_manifest_template_generation_replay_equivocation_and_advance() -> None:
    manifest = bot_manifest()
    remote = manifest.template
    template = BotInstallTemplate(
        id=700,
        source_id=int(remote.id),
        source_domain=APP_DOMAIN,
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        slug=remote.slug,
        name=remote.name,
        description=remote.description,
        scopes=list(reversed(remote.scopes)),
        intents=list(remote.intents),
        permissions=int(remote.permissions),
        contexts=list(remote.contexts),
        e2ee_mode=remote.e2ee_mode,
        generation=int(remote.generation),
        active=True,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=template), get=AsyncMock())
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=701))

    assert (
        await materialize_manifest_template(
            cast(Any, session),
            manifest,
            cast(Any, snowflake),
            app_id=APP_ID,
            domain=APP_DOMAIN,
        )
        is template
    )

    equivocation_raw = manifest.model_dump(mode="json")
    equivocation_raw["template"]["name"] = "Other template"
    equivocation = BotManifest.model_validate(equivocation_raw)
    with pytest.raises(FederationNetworkError, match="equivocates at template"):
        await materialize_manifest_template(
            cast(Any, session),
            equivocation,
            cast(Any, snowflake),
            app_id=APP_ID,
            domain=APP_DOMAIN,
        )

    advance_raw = equivocation.model_dump(mode="json")
    advance_raw["template"]["generation"] = str(int(remote.generation) + 1)
    advance = BotManifest.model_validate(advance_raw)
    await materialize_manifest_template(
        cast(Any, session),
        advance,
        cast(Any, snowflake),
        app_id=APP_ID,
        domain=APP_DOMAIN,
    )
    assert (template.name, template.generation) == ("Other template", int(remote.generation) + 1)


def test_manifest_worker_generation_replay_equivocation_and_advance() -> None:
    remote = bot_manifest().workers[0]
    row = worker()
    public_key = base64.urlsafe_b64decode(remote.public_key + "=")

    apply_manifest_worker(row, remote, public_key, created=False)
    equivocation = remote.model_copy(update={"name": "other-production"})
    with pytest.raises(FederationNetworkError, match="equivocates at worker"):
        apply_manifest_worker(row, equivocation, public_key, created=False)
    assert row.name == "production"

    advance = equivocation.model_copy(update={"generation": "4"})
    apply_manifest_worker(row, advance, public_key, created=False)
    assert (row.name, row.generation) == ("other-production", 4)


@pytest.mark.asyncio
async def test_manifest_command_generation_replay_equivocation_and_advance() -> None:
    manifest = bot_manifest()
    remote = manifest.commands[0]
    command = ApplicationCommand(
        id=500,
        source_id=int(remote.id),
        source_domain=APP_DOMAIN,
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        name=remote.name,
        type=remote.type,
        definition=remote.model_dump(mode="json", exclude={"id"}),
        contexts=list(remote.contexts),
        integration_types=list(remote.integration_types),
        generation=int(manifest.application.command_generation),
        state="active",
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[command]),
        scalar=AsyncMock(return_value=command),
        get=AsyncMock(),
        add=Mock(),
    )
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=501))

    await materialize_manifest_commands(
        cast(Any, session),
        manifest,
        cast(Any, snowflake),
        app_id=APP_ID,
        domain=APP_DOMAIN,
        generation_advanced=False,
    )

    equivocation_raw = manifest.model_dump(mode="json")
    equivocation_raw["commands"][0]["description"] = "Different forecast"
    equivocation = BotManifest.model_validate(equivocation_raw)
    with pytest.raises(FederationNetworkError, match="equivocates at command"):
        await materialize_manifest_commands(
            cast(Any, session),
            equivocation,
            cast(Any, snowflake),
            app_id=APP_ID,
            domain=APP_DOMAIN,
            generation_advanced=False,
        )

    advance_raw = equivocation.model_dump(mode="json")
    advance_raw["application"]["command_generation"] = "10"
    advance = BotManifest.model_validate(advance_raw)
    await materialize_manifest_commands(
        cast(Any, session),
        advance,
        cast(Any, snowflake),
        app_id=APP_ID,
        domain=APP_DOMAIN,
        generation_advanced=True,
    )
    assert command.definition["description"] == "Different forecast"
    assert command.generation == 10


@pytest.mark.asyncio
async def test_manifest_emoji_generation_replay_equivocation_and_advance() -> None:
    manifest = bot_manifest()
    remote = manifest.emojis[0]
    bot = bot_user()
    emoji = ApplicationEmoji(
        id=int(remote.id),
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        name=remote.name,
        name_casefold=remote.name.casefold(),
        media_hash=remote.media_hash,
        object_key=None,
        animated=remote.animated,
        available=remote.available,
        creator_id=BOT_ID,
        creator_domain=APP_DOMAIN,
        version=int(remote.version),
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=emoji),
        scalars=AsyncMock(return_value=[emoji]),
        add=Mock(),
        delete=AsyncMock(),
    )

    await materialize_manifest_emojis(
        cast(Any, session),
        manifest,
        bot,
        app_id=APP_ID,
        domain=APP_DOMAIN,
        manifest_generation_advanced=False,
    )

    equivocation_raw = manifest.model_dump(mode="json")
    equivocation_raw["emojis"][0]["name"] = "cloudy"
    equivocation = BotManifest.model_validate(equivocation_raw)
    with pytest.raises(FederationNetworkError, match="equivocates at application emoji"):
        await materialize_manifest_emojis(
            cast(Any, session),
            equivocation,
            bot,
            app_id=APP_ID,
            domain=APP_DOMAIN,
            manifest_generation_advanced=False,
        )

    advance_raw = equivocation.model_dump(mode="json")
    advance_raw["emojis"][0]["version"] = "3"
    advance = BotManifest.model_validate(advance_raw)
    await materialize_manifest_emojis(
        cast(Any, session),
        advance,
        bot,
        app_id=APP_ID,
        domain=APP_DOMAIN,
        manifest_generation_advanced=False,
    )
    assert (emoji.name, emoji.version) == ("cloudy", 3)


@pytest.mark.asyncio
async def test_manifest_omissions_only_delete_on_new_parent_generation() -> None:
    manifest_raw = bot_manifest().model_dump(mode="json")
    manifest_raw["workers"] = []
    manifest_raw["commands"] = []
    manifest_raw["emojis"] = []
    omitted = BotManifest.model_validate(manifest_raw)
    remote_worker = worker()
    remote_worker.source_id = WORKER_ID
    remote_worker.source_domain = APP_DOMAIN
    remote_command = ApplicationCommand(
        id=500,
        source_id=50,
        source_domain=APP_DOMAIN,
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        name="forecast",
        type="chat_input",
        definition={"name": "forecast", "description": "Show the forecast", "type": "chat_input"},
        contexts=["guild"],
        integration_types=["guild_install"],
        generation=9,
        state="active",
    )
    remote_emoji = ApplicationEmoji(
        id=60,
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        name="sunny",
        name_casefold="sunny",
        media_hash="a" * 64,
        object_key=None,
        animated=False,
        available=True,
        creator_id=BOT_ID,
        creator_domain=APP_DOMAIN,
        version=2,
    )
    worker_session = SimpleNamespace(scalars=AsyncMock(return_value=[remote_worker]))
    emoji_session = SimpleNamespace(
        scalars=AsyncMock(return_value=[remote_emoji]),
        delete=AsyncMock(),
    )
    command_session = SimpleNamespace(scalars=AsyncMock(return_value=[remote_command]))
    snowflake = cast(Any, SimpleNamespace(mint=AsyncMock(return_value=999)))

    await materialize_manifest_workers(
        cast(Any, worker_session),
        omitted,
        snowflake,
        app_id=APP_ID,
        domain=APP_DOMAIN,
        manifest_generation_advanced=False,
    )
    await materialize_manifest_emojis(
        cast(Any, emoji_session),
        omitted,
        bot_user(),
        app_id=APP_ID,
        domain=APP_DOMAIN,
        manifest_generation_advanced=False,
    )
    with pytest.raises(FederationNetworkError, match="equivocates at command"):
        await materialize_manifest_commands(
            cast(Any, command_session),
            omitted,
            snowflake,
            app_id=APP_ID,
            domain=APP_DOMAIN,
            generation_advanced=False,
        )
    assert remote_worker.revoked_at is None
    assert remote_command.state == "active"
    emoji_session.delete.assert_not_awaited()

    await materialize_manifest_workers(
        cast(Any, worker_session),
        omitted,
        snowflake,
        app_id=APP_ID,
        domain=APP_DOMAIN,
        manifest_generation_advanced=True,
    )
    await materialize_manifest_emojis(
        cast(Any, emoji_session),
        omitted,
        bot_user(),
        app_id=APP_ID,
        domain=APP_DOMAIN,
        manifest_generation_advanced=True,
    )
    await materialize_manifest_commands(
        cast(Any, command_session),
        omitted,
        snowflake,
        app_id=APP_ID,
        domain=APP_DOMAIN,
        generation_advanced=True,
    )
    assert remote_worker.revoked_at is not None
    assert remote_command.state == "superseded"
    emoji_session.delete.assert_awaited_once_with(remote_emoji)


def capability_payload() -> BotDMCapabilityPayload:
    pair_key = "c" * 64
    installation_ref = f"60@{INSTALL_DOMAIN}"
    application_ref = f"{APP_ID}@{APP_DOMAIN}"
    bot_ref = f"{BOT_ID}@{APP_DOMAIN}"
    return BotDMCapabilityPayload.model_validate(
        {
            "grant_id": bot_dm_grant_id(
                "guild",
                installation_ref,
                application_ref,
                bot_ref,
                pair_key,
                TARGET_DOMAIN,
            ),
            "source_kind": "guild",
            "installation_ref": installation_ref,
            "application_ref": application_ref,
            "bot_user_ref": bot_ref,
            "guild_ref": f"70@{INSTALL_DOMAIN}",
            "target_user_ref": f"80@{TARGET_DOMAIN}",
            "pair_key": pair_key,
            "authority_domain": TARGET_DOMAIN,
            "scopes": ["dm.send", "messages.metadata"],
            "intents": ["direct_messages"],
            "channel_restrictions": [],
            "e2ee_mode": "disabled",
            "installation_revision": "2",
            "runtime_manifest_generation": str(MANIFEST_GENERATION),
            "runtime_revocation_generation": str(REVOCATION_GENERATION),
            "target_access_revocation_generation": "3",
            "runtime_snapshot_fingerprint": "a" * 64,
            "revision": "5",
            "status": "active",
            "expires_at_ms": str(
                int((datetime.now(UTC) + timedelta(minutes=5)).timestamp() * 1000)
            ),
        }
    )


def capability_row(payload: BotDMCapabilityPayload) -> BotDMCapability:
    proof = EventEnvelope.model_validate(
        {
            "event_id": "kcfe_runtimebootstrap01",
            "origin": INSTALL_DOMAIN,
            "type": BOT_DM_CAPABILITY_EVENT,
            "ts": int(datetime.now(UTC).timestamp() * 1000),
            "actor": {"id": str(BOT_ID), "domain": APP_DOMAIN},
            "context": {},
            "content": payload.model_dump(mode="json"),
            "signatures": {INSTALL_DOMAIN: {"ed25519:test": "signature"}},
        }
    )
    return BotDMCapability(
        id=61,
        grant_id=payload.grant_id,
        source_kind="guild",
        source_installation_id=60,
        source_installation_domain=INSTALL_DOMAIN,
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        bot_user_id=BOT_ID,
        bot_user_domain=APP_DOMAIN,
        guild_id=70,
        guild_domain=INSTALL_DOMAIN,
        installing_user_id=None,
        installing_user_domain=None,
        target_user_id=80,
        target_user_domain=TARGET_DOMAIN,
        pair_key=payload.pair_key,
        authority_domain=TARGET_DOMAIN,
        conversation_id=90,
        conversation_domain=TARGET_DOMAIN,
        granted_scopes=list(payload.scopes),
        granted_intents=list(payload.intents),
        channel_restrictions=[],
        e2ee_mode="disabled",
        revision=int(payload.revision),
        target_access_revocation_generation=int(payload.target_access_revocation_generation),
        status="active",
        proof_fingerprint=capability_fingerprint(payload),
        proof=proof.model_dump(mode="json"),
        expires_at=payload.expires_at,
        revoked_at=None,
    )


@pytest.mark.asyncio
async def test_runtime_manifest_supports_guild_only_application_without_install_template() -> None:
    app = application()
    bot = bot_user()
    active_worker = worker()
    result = Mock()
    result.one_or_none.return_value = (app, bot)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        scalars=AsyncMock(side_effect=[[], [active_worker]]),
    )

    manifest, signer = await local_runtime_manifest(
        cast(Any, session),
        APP_ID,
        TARGET_DOMAIN,
        cast(Any, SimpleNamespace(domain=APP_DOMAIN)),
    )

    assert signer is bot
    assert manifest.target_domain == TARGET_DOMAIN
    assert manifest.application.supported_install_types == ["guild_install"]
    assert manifest.revocation_generation == str(REVOCATION_GENERATION)
    assert [item.id for item in manifest.workers] == [str(WORKER_ID)]
    assert session.scalars.await_count == 2


@pytest.mark.asyncio
async def test_runtime_manifest_fetch_is_bound_to_c_and_exact_a_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = runtime_manifest()
    envelope = SimpleNamespace(
        type=BOT_RUNTIME_MANIFEST_EVENT,
        actor=SimpleNamespace(id=str(BOT_ID), domain=APP_DOMAIN),
        content=manifest.model_dump(mode="json"),
    )
    request = AsyncMock(return_value=SimpleNamespace(status_code=200))
    monkeypatch.setattr(bot_federation, "signed_request", request)
    monkeypatch.setattr(bot_federation, "decode_federation_response_json", Mock(return_value={}))
    envelope_validator = AsyncMock(return_value=envelope)
    monkeypatch.setattr(bot_federation, "validated_event_envelope", envelope_validator)
    settings = cast(Any, SimpleNamespace(domain=TARGET_DOMAIN))
    session = cast(Any, SimpleNamespace(get=AsyncMock(return_value=application())))

    accepted = await fetch_runtime_bot_manifest(
        session,
        settings,
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        bot_user_id=BOT_ID,
        bot_user_domain=APP_DOMAIN,
        manifest_generation=MANIFEST_GENERATION,
        revocation_generation=REVOCATION_GENERATION,
    )

    assert accepted == manifest
    runtime_request = request.await_args
    assert runtime_request is not None
    assert runtime_request.args[3] == APP_DOMAIN
    assert runtime_request.args[4].endswith(f"/{APP_ID}/runtime-manifest")
    with pytest.raises(FederationNetworkError, match="invalid"):
        await fetch_runtime_bot_manifest(
            session,
            settings,
            application_id=APP_ID,
            application_domain=APP_DOMAIN,
            bot_user_id=BOT_ID,
            bot_user_domain=APP_DOMAIN,
            manifest_generation=MANIFEST_GENERATION,
            revocation_generation=REVOCATION_GENERATION + 1,
        )

    substituted_team = runtime_manifest().model_dump(mode="json")
    substituted_team["application"]["team_id"] = "31"
    envelope_validator.return_value = SimpleNamespace(
        type=BOT_RUNTIME_MANIFEST_EVENT,
        actor=SimpleNamespace(id=str(BOT_ID), domain=APP_DOMAIN),
        content=substituted_team,
    )
    with pytest.raises(FederationNetworkError, match="invalid"):
        await fetch_runtime_bot_manifest(
            session,
            settings,
            application_id=APP_ID,
            application_domain=APP_DOMAIN,
            bot_user_id=BOT_ID,
            bot_user_domain=APP_DOMAIN,
            manifest_generation=MANIFEST_GENERATION,
            revocation_generation=REVOCATION_GENERATION,
        )


@pytest.mark.asyncio
async def test_runtime_manifest_materialization_promotes_exact_pending_a_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = runtime_manifest()
    app = application()
    app.revocation_generation = 1
    bot = bot_user()
    target = BotApplicationTarget(
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        target_domain=TARGET_DOMAIN,
        generation=1,
        guild_installations=0,
        user_installations=0,
        runtime_manifest_generation=MANIFEST_GENERATION,
        runtime_revocation_generation=REVOCATION_GENERATION,
        runtime_access_revocation_generation=3,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=b"r" * 32,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=None), flush=AsyncMock())
    materialize_application = AsyncMock(return_value=(app, bot, True, True))
    materialize_workers = AsyncMock()
    promote = AsyncMock(return_value=target)
    monkeypatch.setattr(
        bot_federation,
        "materialize_manifest_application",
        materialize_application,
    )
    monkeypatch.setattr(bot_federation, "materialize_manifest_workers", materialize_workers)
    monkeypatch.setattr(bot_federation, "promote_application_runtime_highwater", promote)

    projected_app, projected_bot, projected_target = await materialize_runtime_bot_manifest(
        cast(Any, session),
        manifest,
        cast(Any, SimpleNamespace(domain=TARGET_DOMAIN)),
        cast(Any, SimpleNamespace()),
    )

    assert (projected_app, projected_bot, projected_target) == (app, bot, target)
    assert app.revocation_generation == REVOCATION_GENERATION
    materialize_workers.assert_awaited_once()
    promote.assert_awaited_once_with(
        session,
        app,
        target_domain=TARGET_DOMAIN,
    )


@pytest.mark.asyncio
async def test_established_exact_c_projection_bootstraps_without_a_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application()
    bot = bot_user()
    bot.is_local = False
    target = BotApplicationTarget(
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        target_domain=TARGET_DOMAIN,
        generation=1,
        guild_installations=0,
        user_installations=0,
        runtime_manifest_generation=MANIFEST_GENERATION,
        runtime_revocation_generation=REVOCATION_GENERATION,
        runtime_access_revocation_generation=3,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=b"r" * 32,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[app, target]),
        get=AsyncMock(return_value=bot),
    )
    fetch = AsyncMock()
    monkeypatch.setattr(bot_federation, "fetch_runtime_bot_manifest", fetch)

    projected = await bootstrap_runtime_application_projection(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain=TARGET_DOMAIN)),
        cast(Any, SimpleNamespace()),
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        bot_user_id=BOT_ID,
        bot_user_domain=APP_DOMAIN,
        manifest_generation=MANIFEST_GENERATION,
        revocation_generation=REVOCATION_GENERATION,
        access_revocation_generation=3,
        runtime_snapshot_fingerprint=b"r" * 32,
    )

    assert projected == (app, bot, target)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_c_projection_promotes_newer_access_epoch_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application()
    bot = bot_user()
    bot.is_local = False
    old_target = BotApplicationTarget(
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        target_domain=TARGET_DOMAIN,
        generation=1,
        guild_installations=0,
        user_installations=0,
        runtime_manifest_generation=MANIFEST_GENERATION,
        runtime_revocation_generation=REVOCATION_GENERATION,
        runtime_access_revocation_generation=2,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=b"o" * 32,
    )
    promoted_target = BotApplicationTarget(
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        target_domain=TARGET_DOMAIN,
        generation=1,
        guild_installations=0,
        user_installations=0,
        runtime_manifest_generation=MANIFEST_GENERATION,
        runtime_revocation_generation=REVOCATION_GENERATION,
        runtime_access_revocation_generation=3,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=b"n" * 32,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[app, old_target]),
        get=AsyncMock(return_value=bot),
    )
    promote = AsyncMock(return_value=promoted_target)
    fetch = AsyncMock()
    monkeypatch.setattr(bot_federation, "promote_application_runtime_highwater", promote)
    monkeypatch.setattr(bot_federation, "fetch_runtime_bot_manifest", fetch)

    projected = await bootstrap_runtime_application_projection(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain=TARGET_DOMAIN)),
        cast(Any, SimpleNamespace()),
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        bot_user_id=BOT_ID,
        bot_user_domain=APP_DOMAIN,
        manifest_generation=MANIFEST_GENERATION,
        revocation_generation=REVOCATION_GENERATION,
        access_revocation_generation=3,
        runtime_snapshot_fingerprint=b"n" * 32,
    )

    assert projected == (app, bot, promoted_target)
    promote.assert_awaited_once_with(session, app, target_domain=TARGET_DOMAIN)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_equals_c_bootstrap_uses_local_authority_without_self_federation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application()
    bot = bot_user()
    target = BotApplicationTarget(
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        target_domain=APP_DOMAIN,
        generation=1,
        guild_installations=0,
        user_installations=0,
        runtime_manifest_generation=MANIFEST_GENERATION,
        runtime_revocation_generation=REVOCATION_GENERATION,
        runtime_access_revocation_generation=2,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=b"l" * 32,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[app, target]),
        get=AsyncMock(return_value=bot),
    )
    fetch = AsyncMock()
    monkeypatch.setattr(bot_federation, "fetch_runtime_bot_manifest", fetch)

    projected = await bootstrap_runtime_application_projection(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain=APP_DOMAIN)),
        cast(Any, SimpleNamespace()),
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        bot_user_id=BOT_ID,
        bot_user_domain=APP_DOMAIN,
        manifest_generation=MANIFEST_GENERATION,
        revocation_generation=REVOCATION_GENERATION,
        access_revocation_generation=2,
        runtime_snapshot_fingerprint=b"l" * 32,
    )

    assert projected == (app, bot, target)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_b_c_dm_token_refresh_bootstraps_from_a_not_install_authority_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = capability_payload()
    row = capability_row(payload)
    session = SimpleNamespace(scalar=AsyncMock(return_value=row))
    settings = cast(Any, SimpleNamespace(domain=TARGET_DOMAIN))
    snowflake = cast(Any, SimpleNamespace())
    bootstrap = AsyncMock()
    require_runtime = AsyncMock()
    request = AsyncMock(return_value=SimpleNamespace(status_code=200))
    validated = SimpleNamespace()
    monkeypatch.setattr(bot_federation, "bootstrap_runtime_application_projection", bootstrap)
    monkeypatch.setattr(bot_federation, "require_stored_capability_runtime", require_runtime)
    monkeypatch.setattr(bot_federation, "signed_request", request)
    monkeypatch.setattr(bot_federation, "decode_federation_response_json", Mock(return_value={}))
    monkeypatch.setattr(
        bot_federation,
        "validated_worker_authorization",
        AsyncMock(return_value=validated),
    )
    monkeypatch.setattr(
        bot_federation,
        "refresh_manifest_for_worker_authorization",
        AsyncMock(),
    )
    monkeypatch.setattr(bot_federation, "apply_worker_authorization", AsyncMock())

    await refresh_remote_worker_authorization(
        cast(Any, session),
        settings,
        snowflake,
        APP_ID,
        APP_DOMAIN,
        WORKER_ID,
        dm_capability_grant_id=payload.grant_id,
        dm_capability_revision=int(payload.revision),
    )

    bootstrap.assert_awaited_once_with(
        session,
        settings,
        snowflake,
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        bot_user_id=BOT_ID,
        bot_user_domain=APP_DOMAIN,
        manifest_generation=MANIFEST_GENERATION,
        revocation_generation=REVOCATION_GENERATION,
        access_revocation_generation=3,
        runtime_snapshot_fingerprint=bytes.fromhex(payload.runtime_snapshot_fingerprint),
    )
    require_runtime.assert_awaited_once_with(session, settings, payload)
    # C contacts application authority A for both runtime identity and worker
    # authorization.  B remains only the capability signer.
    worker_request = request.await_args
    assert worker_request is not None
    assert worker_request.args[3] == APP_DOMAIN
    assert worker_request.args[4].endswith(f"/{APP_ID}/workers/{WORKER_ID}/authorization")
    assert worker_request.kwargs["query"] == {
        "dm_capability_grant_id": payload.grant_id,
        "dm_capability_revision": str(payload.revision),
    }


@pytest.mark.asyncio
async def test_a_worker_delegation_requires_c_runtime_and_accepts_wildcard_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application()
    local_worker = worker()
    bot = bot_user()
    payload = capability_payload()
    capability = capability_row(payload)
    result = Mock()
    result.one_or_none.return_value = (app, local_worker, bot)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        scalars=AsyncMock(return_value=[]),
        scalar=AsyncMock(return_value=capability),
    )
    settings = cast(Any, SimpleNamespace(domain=APP_DOMAIN))
    require_runtime = AsyncMock()
    monkeypatch.setattr(
        bot_federation,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(bot_federation, "require_stored_capability_runtime", require_runtime)
    monkeypatch.setattr(
        bot_federation,
        "build_envelope",
        AsyncMock(return_value={"accepted": True}),
    )

    response = await federation_worker_authorization(
        APP_ID,
        WORKER_ID,
        cast(Any, SimpleNamespace(origin=TARGET_DOMAIN, silenced=False)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        settings,
        payload.grant_id,
        int(payload.revision),
    )

    assert response == {"accepted": True}
    require_runtime.assert_awaited_once_with(
        session,
        settings,
        payload,
    )


@pytest.mark.asyncio
async def test_dm_capability_does_not_bypass_nonempty_worker_target_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application()
    restricted_worker = worker()
    restricted_worker.target_domains = ["other.example"]
    result = Mock()
    result.one_or_none.return_value = (app, restricted_worker, bot_user())
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        scalars=AsyncMock(return_value=[]),
        scalar=AsyncMock(),
    )
    monkeypatch.setattr(
        bot_federation,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as denied:
        await federation_worker_authorization(
            APP_ID,
            WORKER_ID,
            cast(Any, SimpleNamespace(origin=TARGET_DOMAIN, silenced=False)),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain=APP_DOMAIN)),
            capability_payload().grant_id,
            5,
        )

    assert getattr(denied.value, "status_code", None) == 404
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_promoted_a_projection_allows_c_to_mint_a_dm_capability_token() -> None:
    app = application()
    remote_worker = worker()
    remote_worker.id = 400
    remote_worker.source_id = WORKER_ID
    remote_worker.source_domain = APP_DOMAIN
    payload = capability_payload()
    capability = capability_row(payload)
    target = BotApplicationTarget(
        application_id=APP_ID,
        application_domain=APP_DOMAIN,
        target_domain=TARGET_DOMAIN,
        generation=1,
        guild_installations=0,
        user_installations=0,
        runtime_manifest_generation=MANIFEST_GENERATION,
        runtime_revocation_generation=REVOCATION_GENERATION,
        runtime_access_revocation_generation=3,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=bytes.fromhex(payload.runtime_snapshot_fingerprint),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=target),
        add=Mock(),
        flush=AsyncMock(),
    )

    token, raw = await issue_bot_token(
        cast(Any, session),
        token_id=500,
        worker=remote_worker,
        application=app,
        dpop_thumbprint="worker-thumbprint",
        target_domain=TARGET_DOMAIN,
        dm_capability=capability,
    )

    assert raw.startswith("kb1_at_")
    assert token.dm_capability_id == capability.id
    assert token.dm_capability_revision == capability.revision
    assert token.scopes == ["dm.send", "messages.metadata"]
    session.add.assert_called_once_with(token)
