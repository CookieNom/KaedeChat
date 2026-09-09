from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

import app.bots.developer_projection as developer_projection
from app.bots.developer_projection import (
    DEVELOPER_TEAM_SNAPSHOT_EVENT,
    DeveloperApplicationProjection,
    DeveloperTeamSnapshot,
    apply_developer_team_snapshot,
    authority_attested_developer_team_snapshot,
    queue_developer_team_snapshots,
)
from app.db.bot_models import (
    BotApplication,
    BotApplicationTarget,
    DeveloperTeam,
    DeveloperTeamMember,
    DeveloperTeamMemberHighwater,
)
from app.db.models import User


def human(user_id: int, domain: str, *, local: bool) -> User:
    return User(
        id=user_id,
        origin_domain=domain,
        is_local=local,
        account_type="human",
        username=f"user_{user_id}",
        password_hash="hash" if local else None,
        profile_resolved=True,
    )


def bot() -> User:
    return User(
        id=30,
        origin_domain="apps.example",
        is_local=True,
        account_type="bot",
        username="weather",
        display_name="Weather",
        password_hash=None,
        profile_resolved=True,
        profile_version=1,
        e2ee_device_generation=0,
    )


def application_projection() -> DeveloperApplicationProjection:
    return DeveloperApplicationProjection(
        id="20",
        origin_domain="apps.example",
        team_id="10",
        team_domain="apps.example",
        name="Weather",
        description="Forecasts for every server.",
        icon_hash="a" * 64,
        banner_hash="b" * 64,
        support_url="https://apps.example/support",
        privacy_url="https://apps.example/privacy",
        terms_url="https://apps.example/terms",
        directory_enabled=True,
        directory_approved=True,
        directory_summary="Local forecasts and alerts.",
        directory_category="utilities",
        directory_tags=["weather"],
        directory_collections=["featured"],
        directory_media=[{"type": "youtube", "video_id": "dQw4w9WgXcQ"}],
        directory_external_links=[{"name": "Website", "url": "https://apps.example/weather"}],
        directory_supported_locales=["en-US", "fr"],
        directory_description_localizations={"fr": "Prévisions pour chaque serveur."},
        status="active",
        custody_mode="managed",
        target_policy="open",
        default_scopes=[],
        default_intents=[],
        default_permissions="0",
        supported_install_types=["guild_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["guild"],
        e2ee_modes=["participant"],
        manifest_generation="1",
        command_generation="1",
        revocation_generation="1",
        bot_user={
            "id": "30",
            "origin_domain": "apps.example",
            "account_type": "bot",
            "username": "weather",
            "display_name": "Weather",
            "profile_version": 1,
        },
    )


def snapshot(
    *,
    role: str | None = "developer",
    name: str = "Platform",
    member_id: int = 40,
    revision: int = 4,
) -> dict[str, object]:
    return {
        "team_id": "10",
        "team_domain": "apps.example",
        "team_name": name,
        "personal": False,
        "revision": str(revision),
        "member_id": str(member_id),
        "member_domain": "users.example",
        "member_role": role,
        "applications": [application_projection().model_dump(mode="json")] if role else [],
    }


class ProjectionSession:
    def __init__(self, team: DeveloperTeam | None = None) -> None:
        self.teams: dict[tuple[int, str], DeveloperTeam] = {}
        self.members: dict[tuple[int, str, int, str], DeveloperTeamMember] = {}
        self.highwaters: dict[tuple[int, str, int, str], DeveloperTeamMemberHighwater] = {}
        if team is not None:
            self.teams[(team.id, team.origin_domain)] = team
        self.scalar = AsyncMock(return_value=None)

    async def get(self, model: object, key: tuple[object, ...], **_: object) -> object | None:
        if model is DeveloperTeam:
            return self.teams.get((int(key[0]), str(key[1])))
        if model in {BotApplication, BotApplicationTarget, User}:
            return None
        normalized = (int(key[0]), str(key[1]), int(key[2]), str(key[3]))
        if model is DeveloperTeamMember:
            return self.members.get(normalized)
        if model is DeveloperTeamMemberHighwater:
            return self.highwaters.get(normalized)
        raise AssertionError(f"unexpected projection model lookup: {model!r}")

    def add(self, value: object) -> None:
        if isinstance(value, DeveloperTeam):
            self.teams[(value.id, value.origin_domain)] = value
        elif isinstance(value, DeveloperTeamMember):
            key = (value.team_id, value.team_domain, value.user_id, value.user_domain)
            self.members[key] = value
        elif isinstance(value, DeveloperTeamMemberHighwater):
            key = (value.team_id, value.team_domain, value.user_id, value.user_domain)
            self.highwaters[key] = value
        else:
            raise AssertionError(f"unexpected projection insert: {value!r}")

    async def delete(self, value: object) -> None:
        if not isinstance(value, DeveloperTeamMember):
            raise AssertionError(f"unexpected projection deletion: {value!r}")
        key = (value.team_id, value.team_domain, value.user_id, value.user_domain)
        self.members.pop(key, None)


def test_team_snapshot_binds_authority_target_and_revocation_disclosure() -> None:
    payload = snapshot()
    assert authority_attested_developer_team_snapshot(
        DEVELOPER_TEAM_SNAPSHOT_EVENT,
        payload,
        expected_authority="apps.example",
        actor=("40", "users.example"),
    )
    assert not authority_attested_developer_team_snapshot(
        DEVELOPER_TEAM_SNAPSHOT_EVENT,
        payload,
        expected_authority="attacker.example",
        actor=("40", "users.example"),
    )
    with pytest.raises(ValidationError):
        DeveloperTeamSnapshot.model_validate(
            {**payload, "member_role": None, "applications": payload["applications"]}
        )
    assert not authority_attested_developer_team_snapshot(
        DEVELOPER_TEAM_SNAPSHOT_EVENT,
        payload,
        expected_authority="apps.example",
        actor=("41", "users.example"),
    )
    application = application_projection().model_dump(mode="json")
    unique = [
        {
            **application,
            "id": str(100 + index),
            "bot_user": {**application["bot_user"], "id": str(200 + index)},
        }
        for index in range(76)
    ]
    assert (
        len(
            DeveloperTeamSnapshot.model_validate(
                {**payload, "applications": unique[:75]}
            ).applications
        )
        == 75
    )
    with pytest.raises(ValidationError):
        DeveloperTeamSnapshot.model_validate({**payload, "applications": unique})
    repeated_bot = {**application, "id": "21"}
    with pytest.raises(ValidationError, match="repeats a bot identity"):
        DeveloperTeamSnapshot.model_validate(
            {**payload, "applications": [application, repeated_bot]}
        )
    with pytest.raises(ValidationError):
        DeveloperTeamSnapshot.model_validate({**payload, "personal": 0})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "0"),
        ("team_id", "0"),
        ("default_permissions", str(1 << 19)),
        ("default_scopes", ["messages.send", "not.a.scope"]),
        ("default_intents", ["guilds", "guilds"]),
        ("supported_install_types", ["guild_install", "guild_install"]),
        ("user_install_scopes", ["applications.commands", "unknown.scope"]),
        ("user_install_contexts", ["guild", "guild"]),
        ("e2ee_modes", ["participant", "participant"]),
        ("support_url", "http://apps.example/support"),
        ("privacy_url", "https://user:secret@apps.example/privacy"),
        ("terms_url", "http://apps.example/terms"),
        ("icon_hash", "not-a-sha256"),
        ("banner_hash", "not-a-sha256"),
        ("directory_tags", ["weather", "weather"]),
        ("directory_collections", ["featured", "featured"]),
        (
            "directory_media",
            [
                {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
                {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
            ],
        ),
        ("directory_external_links", [{"name": "Website", "url": "http://apps.example"}]),
        ("directory_supported_locales", ["en-US", "en-US"]),
        ("directory_description_localizations", {"xx": "Unknown locale"}),
    ],
)
def test_application_projection_rejects_malformed_authority_contract(
    field: str,
    value: object,
) -> None:
    payload = application_projection().model_dump(mode="json")
    payload[field] = value

    with pytest.raises(ValidationError):
        DeveloperApplicationProjection.model_validate(payload)


@pytest.mark.parametrize("mutation", [{"account_type": "human"}, {"id": "0"}])
def test_application_projection_requires_an_authoritative_bot_profile(
    mutation: dict[str, str],
) -> None:
    payload = application_projection().model_dump(mode="json")
    payload["bot_user"] = {**payload["bot_user"], **mutation}

    with pytest.raises(ValidationError):
        DeveloperApplicationProjection.model_validate(payload)


@pytest.mark.asyncio
async def test_snapshot_preflights_every_bot_identity_before_profile_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = application_projection()
    second_raw = first.model_dump(mode="json")
    second_raw["id"] = "21"
    second_raw["bot_user"] = {
        **second_raw["bot_user"],
        "id": "31",
        "username": "weather_alerts",
    }
    second = DeveloperApplicationProjection.model_validate(second_raw)
    bound_application = BotApplication(
        id=99,
        origin_domain="apps.example",
        team_id=11,
        team_domain="apps.example",
        bot_user_id=31,
        bot_user_domain="apps.example",
        name="Already bound",
    )
    owner_lookups = 0

    async def scalar(statement: object) -> object | None:
        nonlocal owner_lookups
        if "FROM bot_applications" not in str(statement):
            return None
        owner_lookups += 1
        return None if owner_lookups == 1 else bound_application

    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=scalar),
        get=AsyncMock(return_value=None),
    )
    upsert = AsyncMock()
    monkeypatch.setattr(developer_projection, "upsert_remote_user", upsert)
    raw = snapshot()
    raw["applications"] = [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ]

    with pytest.raises(ValueError, match="reuses another application's bot identity"):
        await apply_developer_team_snapshot(
            session,
            SimpleNamespace(domain="users.example"),
            "apps.example",
            human(40, "users.example", local=True),
            raw,
        )

    assert owner_lookups == 2
    upsert.assert_not_awaited()


def test_developer_projection_cannot_reactivate_ahead_of_runtime_projection() -> None:
    application = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=10,
        team_domain="apps.example",
        bot_user_id=30,
        bot_user_domain="apps.example",
        name="Weather",
        status="suspended",
        manifest_generation=1,
        command_generation=1,
        revocation_generation=1,
    )
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="users.example",
        generation=1,
        guild_installations=0,
        user_installations=1,
        runtime_manifest_generation=1,
        runtime_revocation_generation=1,
        runtime_status="suspended",
        runtime_target_allowed=True,
        runtime_fingerprint=b"r" * 32,
    )
    incoming = application_projection().model_copy(
        update={
            "manifest_generation": "2",
            "command_generation": "2",
            "revocation_generation": "2",
            "status": "active",
        }
    )

    developer_projection._apply_application_projection(
        application,
        incoming,
        created=False,
        runtime_target=target,
    )

    assert application.status == "suspended"
    assert (application.manifest_generation, application.revocation_generation) == (2, 2)
    assert application.terms_url == "https://apps.example/terms"
    assert application.directory_approved is True
    assert application.directory_tags == ["weather"]
    assert application.directory_media == [{"type": "youtube", "video_id": "dQw4w9WgXcQ"}]
    assert application.directory_supported_locales == ["en-US", "fr"]

    target.runtime_manifest_generation = 2
    target.runtime_revocation_generation = 2
    target.runtime_status = "active"
    developer_projection._apply_application_projection(
        application,
        incoming,
        created=False,
        runtime_target=target,
    )

    assert application.status == "active"


def test_stale_developer_projection_preserves_manifest_and_generation_highwaters() -> None:
    application = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=10,
        team_domain="apps.example",
        bot_user_id=30,
        bot_user_domain="apps.example",
        name="Weather v5",
        description="New signed manifest",
        icon_hash=None,
        support_url="https://apps.example/new-support",
        privacy_url="https://apps.example/new-privacy",
        status="active",
        target_policy="blocklist",
        default_scopes=["guilds.read"],
        default_intents=["guilds"],
        default_permissions=0,
        supported_install_types=["guild_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["guild"],
        e2ee_modes=[],
        manifest_generation=5,
        command_generation=8,
        revocation_generation=9,
    )
    stale = application_projection().model_copy(
        update={
            "manifest_generation": "2",
            "command_generation": "3",
            "revocation_generation": "4",
        }
    )

    developer_projection._apply_application_projection(
        application,
        stale,
        created=False,
        runtime_target=None,
    )

    assert (
        application.name,
        application.description,
        application.support_url,
        application.privacy_url,
        application.target_policy,
        application.default_scopes,
        application.default_intents,
    ) == (
        "Weather v5",
        "New signed manifest",
        "https://apps.example/new-support",
        "https://apps.example/new-privacy",
        "blocklist",
        ["guilds.read"],
        ["guilds"],
    )
    assert (
        application.manifest_generation,
        application.command_generation,
        application.revocation_generation,
    ) == (5, 8, 9)
    assert application.terms_url == "https://apps.example/terms"
    assert application.directory_summary == "Local forecasts and alerts."

    equal_but_different = stale.model_copy(update={"manifest_generation": "5"})
    with pytest.raises(ValueError, match="conflicts at manifest generation"):
        developer_projection._apply_application_projection(
            application,
            equal_but_different,
            created=False,
            runtime_target=None,
        )


@pytest.mark.asyncio
async def test_snapshot_coalescing_deletes_the_previous_member_projection(postgres_schema) -> None:
    import json

    from sqlalchemy import text

    connection = postgres_schema
    await connection.execute(
        text(
            "CREATE TABLE federation_events (origin_domain text, event_id text, event_type "
            "text, envelope jsonb)"
        )
    )
    await connection.execute(
        text(
            "CREATE TABLE federation_outbox (id bigint GENERATED ALWAYS AS IDENTITY, "
            "destination text, event_origin_domain text, event_id text)"
        )
    )
    event_type = DEVELOPER_TEAM_SNAPSHOT_EVENT
    baseline = {
        "actor": {"id": "40", "domain": "users.example"},
        "content": {"team_id": "10", "team_domain": "apps.example"},
    }
    candidates = [
        ("old", "users.example", event_type, baseline),
        ("shared", "users.example", event_type, baseline),
        ("destination", "other.example", event_type, baseline),
        (
            "actor_id",
            "users.example",
            event_type,
            baseline | {"actor": {"id": "99", "domain": "users.example"}},
        ),
        (
            "actor_domain",
            "users.example",
            event_type,
            baseline | {"actor": {"id": "40", "domain": "other.example"}},
        ),
        ("type", "users.example", "guild.message.create", baseline),
    ]
    candidates.extend(
        [
            (
                "team_id",
                "users.example",
                event_type,
                baseline | {"content": {"team_id": "11", "team_domain": "apps.example"}},
            ),
            (
                "team_domain",
                "users.example",
                event_type,
                baseline | {"content": {"team_id": "10", "team_domain": "other.example"}},
            ),
        ]
    )
    for identifier, target, kind, envelope in candidates:
        await connection.execute(
            text(
                "INSERT INTO federation_events VALUES ('apps.example', :id, :type, "
                "CAST(:envelope AS jsonb))"
            ),
            {"id": identifier, "type": kind, "envelope": json.dumps(envelope)},
        )
        await connection.execute(
            text(
                "INSERT INTO federation_outbox (destination, event_origin_domain, event_id) "
                "VALUES (:destination, 'apps.example', :id)"
            ),
            {"destination": target, "id": identifier},
        )
    await connection.execute(
        text(
            "INSERT INTO federation_outbox (destination, event_origin_domain, event_id) "
            "VALUES ('other.example', 'apps.example', 'shared')"
        )
    )
    await developer_projection.discard_superseded_latest_state_event(
        connection,
        destination="users.example",
        event_type=event_type,
        actor_ref=(40, "users.example"),
        team_ref=(10, "apps.example"),
    )
    assert set(
        (
            await connection.execute(text("SELECT event_id, destination FROM federation_outbox"))
        ).all()
    ) == {
        (identifier, destination)
        for identifier, destination, _kind, _envelope in candidates
        if identifier not in {"old", "shared"}
    } | {("shared", "other.example")}
    assert set(await connection.scalars(text("SELECT event_id FROM federation_events"))) == {
        identifier for identifier, *_rest in candidates if identifier != "old"
    }


@pytest.mark.asyncio
async def test_authority_queues_one_monotonic_snapshot_per_remote_member(
    monkeypatch: pytest.MonkeyPatch,
    postgres_schema,
) -> None:
    team = DeveloperTeam(
        id=10,
        origin_domain="apps.example",
        name="Platform",
        personal=False,
        federation_revision=3,
    )
    remote_user = human(40, "users.example", local=False)
    member = DeveloperTeamMember(
        team_id=10,
        team_domain="apps.example",
        user_id=40,
        user_domain="users.example",
        user_is_local=False,
        role="developer",
    )
    app = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=10,
        team_domain="apps.example",
        bot_user_id=30,
        bot_user_domain="apps.example",
        name="Weather",
        description=None,
        icon_hash=None,
        banner_hash=None,
        support_url=None,
        privacy_url=None,
        terms_url=None,
        directory_enabled=False,
        directory_approved=False,
        directory_summary=None,
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
        default_scopes=[],
        default_intents=[],
        default_permissions=0,
        supported_install_types=["guild_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["guild"],
        e2ee_modes=["participant"],
        manifest_generation=1,
        command_generation=1,
        revocation_generation=1,
    )
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    for table in ("developer_teams", "developer_team_members", "bot_applications", "users"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    second = human(41, "second.example", local=False)
    local = human(42, "apps.example", local=True)
    members = [
        DeveloperTeamMember(
            team_id=10,
            team_domain="apps.example",
            user_id=user.id,
            user_domain=user.origin_domain,
            user_is_local=user.is_local,
            role="developer",
        )
        for user in (second, local)
    ]
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        session.add_all([team, remote_user, member, second, local, *members, app, bot()])
        await session.flush()
        attributes = {
            column.name: getattr(app, column.name) for column in BotApplication.__table__.columns
        }
        second_bot = bot()
        second_bot.id, second_bot.username = 31, "secondbot"
        session.add(second_bot)
        session.add_all(
            [
                BotApplication(**(attributes | values))
                for values in (
                    {"id": 21, "status": "suspended", "bot_user_id": 31},
                    {"id": 22, "status": "deleted"},
                    {"id": 23, "team_id": 11},
                    {"id": 24, "team_domain": "other.example"},
                )
            ]
        )
        await session.flush()
        build = AsyncMock(
            side_effect=lambda *args, **kwargs: {
                "event_id": "kcfe_" + args[4]["member_id"],
                "content": args[4],
            }
        )
        queue = AsyncMock()
        discard = AsyncMock()
        monkeypatch.setattr(developer_projection, "build_envelope", build)
        monkeypatch.setattr(developer_projection, "queue_event", queue)
        monkeypatch.setattr(developer_projection, "discard_superseded_latest_state_event", discard)
        config = SimpleNamespace(domain="apps.example")
        assert await queue_developer_team_snapshots(session, config, team) == {
            "users.example",
            "second.example",
        }
        assert team.federation_revision == 4
        assert {call.args[4]["member_id"] for call in build.await_args_list} == {"40", "41"}
        for call in build.await_args_list:
            assert call.args[4]["revision"] == "4"
            assert [app["id"] for app in call.args[4]["applications"]] == ["20", "21"]
            assert call.kwargs["authority_attested_actor"] is True
        assert {
            (call.args[2], call.args[3]["content"]["member_id"]) for call in queue.await_args_list
        } == {("users.example", "40"), ("second.example", "41")}
        assert {
            (call.kwargs["destination"], call.kwargs["actor_ref"], call.kwargs["team_ref"])
            for call in discard.await_args_list
        } == {
            ("users.example", (40, "users.example"), (10, "apps.example")),
            ("second.example", (41, "second.example"), (10, "apps.example")),
        }


@pytest.mark.asyncio
async def test_member_home_rejects_same_revision_equivocation() -> None:
    team = DeveloperTeam(
        id=10,
        origin_domain="apps.example",
        name="Stored name",
        personal=False,
        federation_revision=4,
    )
    actor = human(40, "users.example", local=True)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(return_value=team),
    )

    with pytest.raises(ValueError, match="conflicts at the same revision"):
        await apply_developer_team_snapshot(
            session,
            SimpleNamespace(domain="users.example"),
            "apps.example",
            actor,
            snapshot(role=None, name="Equivocated name"),
        )


@pytest.mark.asyncio
async def test_member_home_applies_authoritative_revocation() -> None:
    team = DeveloperTeam(
        id=10,
        origin_domain="apps.example",
        name="Platform",
        personal=False,
        federation_revision=3,
    )
    actor = human(40, "users.example", local=True)
    member = DeveloperTeamMember(
        team_id=10,
        team_domain="apps.example",
        user_id=40,
        user_domain="users.example",
        user_is_local=True,
        role="developer",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(side_effect=[team, None, member]),
        add=Mock(),
        delete=AsyncMock(),
    )

    applied = await apply_developer_team_snapshot(
        session,
        SimpleNamespace(domain="users.example"),
        "apps.example",
        actor,
        snapshot(role=None),
    )

    assert applied is True
    assert team.federation_revision == 4
    session.delete.assert_awaited_once_with(member)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_member_first", [False, True])
async def test_same_home_members_apply_every_team_revision_in_either_delivery_order(
    monkeypatch: pytest.MonkeyPatch,
    changed_member_first: bool,
) -> None:
    team = DeveloperTeam(
        id=10,
        origin_domain="apps.example",
        name="Platform",
        personal=False,
        federation_revision=3,
    )
    session = ProjectionSession(team)
    actors = {
        40: human(40, "users.example", local=True),
        41: human(41, "users.example", local=True),
    }
    apply_applications = AsyncMock()
    monkeypatch.setattr(
        developer_projection,
        "_apply_snapshot_applications",
        apply_applications,
    )
    settings = SimpleNamespace(domain="users.example")

    async def deliver(revision: int, roles: dict[int, str | None], order: list[int]) -> None:
        for member_id in order:
            assert await apply_developer_team_snapshot(
                session,
                settings,
                "apps.example",
                actors[member_id],
                snapshot(
                    role=roles[member_id],
                    member_id=member_id,
                    revision=revision,
                ),
            )

    await deliver(4, {40: "developer"}, [40])
    assert session.members[(10, "apps.example", 40, "users.example")].role == "developer"

    add_order = [41, 40] if changed_member_first else [40, 41]
    await deliver(5, {40: "developer", 41: "support"}, add_order)
    assert {key[2]: member.role for key, member in session.members.items()} == {
        40: "developer",
        41: "support",
    }

    role_order = [40, 41] if changed_member_first else [41, 40]
    await deliver(6, {40: "administrator", 41: "support"}, role_order)
    assert session.members[(10, "apps.example", 40, "users.example")].role == "administrator"

    revoke_order = [40, 41] if changed_member_first else [41, 40]
    await deliver(7, {40: None, 41: "support"}, revoke_order)
    revoked_key = (10, "apps.example", 40, "users.example")
    assert revoked_key not in session.members
    assert session.highwaters[revoked_key].revision == 7
    assert session.highwaters[(10, "apps.example", 41, "users.example")].revision == 7
    assert team.federation_revision == 7

    assert not await apply_developer_team_snapshot(
        session,
        settings,
        "apps.example",
        actors[40],
        snapshot(role="administrator", member_id=40, revision=6),
    )
    assert revoked_key not in session.members
    assert apply_applications.await_count == 4


@pytest.mark.asyncio
async def test_member_highwater_preserves_replay_and_equivocation_fencing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team = DeveloperTeam(
        id=10,
        origin_domain="apps.example",
        name="Platform",
        personal=False,
        federation_revision=3,
    )
    session = ProjectionSession(team)
    actor = human(40, "users.example", local=True)
    monkeypatch.setattr(
        developer_projection,
        "_apply_snapshot_applications",
        AsyncMock(),
    )
    settings = SimpleNamespace(domain="users.example")
    exact = snapshot(role="developer", member_id=40, revision=4)

    assert await apply_developer_team_snapshot(session, settings, "apps.example", actor, exact)
    assert not await apply_developer_team_snapshot(session, settings, "apps.example", actor, exact)
    with pytest.raises(ValueError, match="member snapshot conflicts at the same revision"):
        await apply_developer_team_snapshot(
            session,
            settings,
            "apps.example",
            actor,
            snapshot(role="support", member_id=40, revision=4),
        )
