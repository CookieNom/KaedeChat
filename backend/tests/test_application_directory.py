from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

import app.api.admin_portal as admin_portal
import app.api.applications as applications
from app.api.admin_portal import ApplicationDirectoryApprovalPatch
from app.api.application_directory import (
    DIRECTORY_COLLECTIONS,
    DirectoryApplication,
    DirectoryBotProfileApplication,
    DirectoryPage,
    DirectoryPreviewResponse,
    DirectorySearch,
    directory_bot_profile_application,
    directory_detail_projection,
    directory_media_assets_valid,
    directory_patch_requires_reapproval,
    directory_preview_response,
    directory_projection,
    directory_query_domain,
    directory_readiness_errors,
    directory_rows,
    directory_target_allowed,
    federation_search_application_directory,
    validated_remote_bot_profile_application,
    validated_remote_directory_detail,
    validated_remote_directory_page,
)
from app.api.applications import ApplicationPatch, TemplateCreate
from app.api.bot_federation import BotManifest
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.bot_models import BotApplication


def application(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": 10,
        "origin_domain": "alpha.example",
        "name": "Helpful App",
        "directory_summary": "A short useful summary.",
        "description": "A complete product description.",
        "directory_enabled": True,
        "directory_category": "utilities",
        "directory_tags": ["tools", "productivity"],
        "directory_collections": ["featured"],
        "directory_media": [],
        "directory_external_links": [],
        "directory_supported_locales": [],
        "directory_description_localizations": {},
        "icon_hash": None,
        "banner_hash": None,
        "support_url": "https://alpha.example/support",
        "privacy_url": "https://alpha.example/privacy",
        "terms_url": "https://alpha.example/terms",
        "directory_approved": True,
        "supported_install_types": ["guild_install", "user_install"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def application_patch_api(session: object, settings: object) -> FastAPI:
    api = FastAPI()
    api.patch("/api/v1/applications/{application_ref}")(applications.patch_application)

    async def current_user() -> SimpleNamespace:
        return SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="alpha.example"))

    async def current_session() -> object:
        return session

    def current_settings() -> object:
        return settings

    api.dependency_overrides[applications.require_user] = current_user
    api.dependency_overrides[applications.get_session] = current_session
    api.dependency_overrides[applications.get_settings] = current_settings
    return api


def detail_projection(app: SimpleNamespace | None = None) -> dict[str, object]:
    current = app or application()
    return directory_projection(
        current,
        SimpleNamespace(slug="default", name="Install", description=None),
    ) | {
        "description": current.description,
        "support_url": current.support_url,
        "privacy_policy_url": current.privacy_url,
        "terms_url": current.terms_url,
        "media": [],
        "external_links": list(current.directory_external_links),
        "supported_locales": list(current.directory_supported_locales),
        "description_localizations": dict(current.directory_description_localizations),
        "popular_commands": [],
        "similar_apps": [],
    }


def test_directory_projection_exposes_install_paths_without_private_state() -> None:
    template = SimpleNamespace(slug="default", name="Install", description=None)
    payload = directory_projection(application(), template)
    assert payload["verified"] is True
    assert payload["user_install_supported"] is True
    assert "description" not in payload
    assert "support_url" not in payload
    assert "media" not in payload
    assert payload["install_template"] == {
        "slug": "default",
        "name": "Install",
        "description": None,
        "install_types": ["guild_install", "user_install"],
        "default_install_type": "guild_install",
    }
    assert "team_ref" not in payload


@pytest.mark.asyncio
async def test_bot_profile_add_app_resolution_is_authority_owned_and_target_filtered() -> None:
    app = application(directory_enabled=False, directory_approved=False)
    template = SimpleNamespace(slug="default", name="Install", description=None)
    result = SimpleNamespace(one_or_none=lambda: (app, template))
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    payload = await directory_bot_profile_application(
        session,
        SimpleNamespace(domain="alpha.example"),
        (20, "alpha.example"),
        target_domain="beta.example",
    )

    assert payload == {
        "bot_ref": "20@alpha.example",
        "application_ref": "10@alpha.example",
        "origin_domain": "alpha.example",
        "name": "Helpful App",
        "install_template": {
            "slug": "default",
            "name": "Install",
            "description": None,
            "install_types": ["guild_install", "user_install"],
            "default_install_type": "guild_install",
        },
        "directory_listed": False,
    }
    statement = session.execute.await_args.args[0]
    assert "bot_applications.bot_user_id" in str(statement)
    assert "bot_instance_rules.effect" in str(statement)


def test_remote_bot_profile_add_app_response_is_strict_and_request_bound() -> None:
    payload = {
        "bot_ref": "20@alpha.example",
        "application_ref": "10@alpha.example",
        "origin_domain": "alpha.example",
        "name": "Helpful App",
        "install_template": {
            "slug": "default",
            "name": "Install",
            "description": None,
            "install_types": ["guild_install", "user_install"],
            "default_install_type": "guild_install",
        },
        "directory_listed": True,
    }
    assert DirectoryBotProfileApplication.model_validate(payload)
    assert (
        validated_remote_bot_profile_application(
            payload,
            bot_id=20,
            domain="alpha.example",
        )["application_ref"]
        == "10@alpha.example"
    )
    for invalid in (
        payload | {"unexpected": True},
        payload | {"bot_ref": "21@alpha.example"},
        payload | {"application_ref": "10@evil.example"},
    ):
        with pytest.raises((ValidationError, ValueError)):
            validated_remote_bot_profile_application(
                invalid,
                bot_id=20,
                domain="alpha.example",
            )


def test_directory_model_has_persistent_reviewed_metadata() -> None:
    columns = BotApplication.__table__.columns
    assert columns["directory_enabled"].nullable is False
    assert columns["directory_approved"].nullable is False
    assert columns["directory_tags"].nullable is False
    assert columns["directory_collections"].nullable is False
    assert columns["directory_media"].nullable is False
    assert columns["directory_external_links"].nullable is False
    assert columns["directory_supported_locales"].nullable is False
    assert columns["directory_description_localizations"].nullable is False
    assert columns["banner_hash"].type.length == 128
    assert columns["terms_url"].type.length == 2048
    constraint_names = {constraint.name for constraint in BotApplication.__table__.constraints}
    assert "ck_bot_applications_bot_application_directory_category_value" in constraint_names
    assert "ck_bot_applications_bot_application_directory_tags_bounded" in constraint_names
    assert "ck_bot_applications_bot_application_directory_collections_bounded" in constraint_names
    assert "ck_bot_applications_bot_application_directory_media_bounded" in constraint_names
    assert (
        "ck_bot_applications_bot_application_directory_external_links_bounded" in constraint_names
    )
    assert (
        "ck_bot_applications_bot_application_directory_supported_locales_bounded"
        in constraint_names
    )
    assert "ck_bot_applications_bot_application_directory_localizations_bounded" in constraint_names


@pytest.mark.asyncio
async def test_directory_readiness_requires_metadata_and_an_install_path() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    app = application(
        directory_summary=None,
        directory_category=None,
        directory_tags=[],
        support_url=None,
        privacy_url=None,
        supported_install_types=["guild_install"],
    )
    assert await directory_readiness_errors(session, app) == [
        "summary",
        "category",
        "tags",
        "support_url",
        "privacy_url",
        "install_path",
    ]


@pytest.mark.asyncio
async def test_directory_readiness_requires_template_even_for_user_install() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    assert await directory_readiness_errors(session, application()) == [
        "install_path",
        "user_install_command",
    ]


@pytest.mark.asyncio
async def test_directory_readiness_requires_an_active_user_install_command() -> None:
    missing = SimpleNamespace(scalar=AsyncMock(side_effect=[42, None]))
    assert await directory_readiness_errors(
        missing,
        application(supported_install_types=["user_install"]),
    ) == ["user_install_command"]

    ready = SimpleNamespace(scalar=AsyncMock(side_effect=[42, 84]))
    assert (
        await directory_readiness_errors(
            ready,
            application(supported_install_types=["user_install"]),
        )
        == []
    )


@pytest.mark.asyncio
async def test_team_preview_renders_a_complete_unapproved_disabled_draft() -> None:
    app = application(
        directory_enabled=False,
        directory_approved=False,
        supported_install_types=["guild_install"],
    )
    template = SimpleNamespace(id=50, slug="default", name="Install", description=None)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=template),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
        scalars=AsyncMock(return_value=[]),
    )

    payload = await directory_preview_response(
        session,
        SimpleNamespace(domain="alpha.example"),
        app,
    )
    preview = DirectoryPreviewResponse.model_validate(payload)

    assert preview.application is not None
    assert preview.application.verified is False
    assert preview.application.ref == "10@alpha.example"
    assert preview.readiness.status == "incomplete"
    assert preview.readiness.ready is False
    assert preview.readiness.preview_available is True
    assert preview.readiness.missing == ["directory_enabled"]
    assert [item.key for item in preview.readiness.items] == [
        "directory_enabled",
        "summary",
        "category",
        "tags",
        "description",
        "support_url",
        "privacy_url",
        "terms_url",
        "media",
        "external_links",
        "supported_locales",
        "description_localizations",
        "install_path",
        "user_install_command",
    ]


@pytest.mark.asyncio
async def test_team_preview_returns_readiness_without_inventing_incomplete_content() -> None:
    app = application(
        directory_summary=None,
        directory_category=None,
        directory_tags=[],
        description=None,
        support_url=None,
        privacy_url=None,
        terms_url=None,
        supported_install_types=["guild_install"],
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
        scalars=AsyncMock(return_value=[]),
    )

    payload = await directory_preview_response(
        session,
        SimpleNamespace(domain="alpha.example"),
        app,
    )

    draft = payload["application"]
    assert draft["summary"] is None  # type: ignore[index]
    assert draft["category"] is None  # type: ignore[index]
    assert draft["tags"] == []  # type: ignore[index]
    assert draft["description"] is None  # type: ignore[index]
    assert draft["install_template"] is None  # type: ignore[index]
    assert payload["readiness"]["preview_available"] is True  # type: ignore[index]
    assert payload["readiness"]["missing"] == [  # type: ignore[index]
        "summary",
        "category",
        "tags",
        "description",
        "support_url",
        "privacy_url",
        "terms_url",
        "install_path",
    ]


@pytest.mark.asyncio
async def test_directory_preview_route_uses_the_private_team_membership_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    monkeypatch.setattr(
        applications,
        "proxy_remote_application_management",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(applications, "managed_application", AsyncMock(side_effect=denied))

    with pytest.raises(HTTPException) as response:
        await applications.get_application_directory_preview(
            EntityRef("10@alpha.example"),
            SimpleNamespace(user=SimpleNamespace()),
            SimpleNamespace(),
            SimpleNamespace(domain="alpha.example"),
        )

    assert response.value is denied


@pytest.mark.asyncio
async def test_user_only_application_can_create_directory_consent_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application(
        status="draft",
        manifest_generation=1,
        directory_approved=False,
        supported_install_types=["user_install"],
        default_scopes=["messages.send"],
        default_intents=["guilds"],
        e2ee_modes=["participant"],
    )
    member = SimpleNamespace(role="owner")
    auth = SimpleNamespace(user=SimpleNamespace())
    session = SimpleNamespace(add=Mock())
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=77))
    commit = AsyncMock()
    monkeypatch.setattr(
        applications,
        "proxy_remote_application_management",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        applications,
        "managed_application",
        AsyncMock(return_value=(app, member, SimpleNamespace())),
    )
    monkeypatch.setattr(applications, "commit_developer_application_mutation", commit)

    result = await applications.create_template(
        EntityRef("10@alpha.example"),
        TemplateCreate(slug="consent", name="Install"),
        auth,
        session,
        snowflake,
        SimpleNamespace(domain="alpha.example"),
    )

    assert result["invite_url"].endswith("/applications/10@alpha.example/install/consent")
    assert app.status == "active"
    commit.assert_awaited_once()

    with pytest.raises(HTTPException) as unsafe:
        await applications.create_template(
            EntityRef("10@alpha.example"),
            TemplateCreate(
                slug="unsafe",
                name="Unsafe",
                scopes=["messages.send"],
                intents=["guilds"],
                e2ee_mode="participant",
            ),
            auth,
            session,
            snowflake,
            SimpleNamespace(domain="alpha.example"),
        )
    assert unsafe.value.status_code == 409
    assert unsafe.value.detail == {"code": "TEMPLATE_EXCEEDS_APPLICATION"}


def user_only_manifest(*, template: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "application": {
            "id": "10",
            "origin_domain": "alpha.example",
            "team_id": "13",
            "team_domain": "alpha.example",
            "name": "Helpful App",
            "status": "active",
            "target_policy": "open",
            "default_scopes": [
                "applications.commands",
                "interactions.respond",
                "messages.send",
            ],
            "default_intents": ["interactions", "guilds"],
            "default_permissions": "0",
            "supported_install_types": ["user_install"],
            "e2ee_modes": ["participant"],
            "manifest_generation": "1",
            "command_generation": "1",
            "bot_user": {
                "id": "11",
                "origin_domain": "alpha.example",
                "account_type": "bot",
                "username": "helper_bot",
            },
        },
        "template": template
        or {
            "id": "12",
            "slug": "consent",
            "name": "Install",
            "scopes": [],
            "intents": [],
            "permissions": "0",
            "contexts": ["guild"],
            "e2ee_mode": "disabled",
            "generation": "1",
        },
        "workers": [],
        "commands": [],
        "emojis": [],
    }


def test_user_only_consent_manifest_is_federatable_but_cannot_advertise_guild_grants() -> None:
    manifest = BotManifest.model_validate(user_only_manifest())
    assert manifest.template.slug == "consent"

    unsafe_templates = [
        {"scopes": ["messages.send"]},
        {"intents": ["guilds"]},
        {"permissions": "1"},
        {"e2ee_mode": "participant"},
    ]
    for changes in unsafe_templates:
        base_template = user_only_manifest()["template"]
        assert isinstance(base_template, dict)
        template = dict(base_template)
        template.update(changes)
        with pytest.raises(ValidationError, match="unsupported guild install"):
            BotManifest.model_validate(user_only_manifest(template=template))


@pytest.mark.asyncio
async def test_guild_install_rejects_user_only_consent_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=20, origin_domain="alpha.example")
    app = application(status="active", supported_install_types=["user_install"])
    template = SimpleNamespace(slug="consent", active=True)
    bot = SimpleNamespace(account_type="bot", disabled_at=None)
    invite_row = Mock()
    invite_row.one_or_none.return_value = (app, template, bot)
    session = SimpleNamespace(
        get=AsyncMock(return_value=guild),
        scalar=AsyncMock(return_value=guild),
        execute=AsyncMock(return_value=invite_row),
    )
    monkeypatch.setattr(
        applications,
        "get_permissions",
        AsyncMock(return_value=Permission.MANAGE_GUILD),
    )
    monkeypatch.setattr(
        applications,
        "guild_authority_owner",
        AsyncMock(return_value=SimpleNamespace()),
    )
    runtime = AsyncMock()
    monkeypatch.setattr(applications, "require_application_runtime_enabled", runtime)

    with pytest.raises(HTTPException) as denied:
        await applications.install_bot(
            EntityRef("20@alpha.example"),
            EntityRef("10@alpha.example"),
            "consent",
            SimpleNamespace(user=SimpleNamespace()),
            session,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="alpha.example"),
        )

    assert denied.value.status_code == 404
    assert denied.value.detail == {"code": "BOT_INVITE_NOT_FOUND"}
    assert session.get.await_count == 1
    runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_install_command_availability_change_revokes_directory_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application(command_generation=1)
    command = SimpleNamespace(
        id=55,
        type="chat_input",
        name="weather",
        state="active",
        integration_types=["guild_install"],
        definition={},
        contexts=["guild"],
        generation=1,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[command]),
        add=Mock(),
        execute=AsyncMock(),
    )
    commit = AsyncMock()
    monkeypatch.setattr(applications, "commit_developer_application_mutation", commit)
    payload = applications.CommandsPut(
        commands=[
            applications.CommandDefinition(
                name="weather",
                description="Show a forecast",
                integration_types=["user_install"],
            )
        ]
    )

    await applications.replace_application_commands(
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock()),
        app,
        payload,
    )

    assert app.directory_approved is False
    commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_directory_readiness_rejects_operator_preapproval_before_opt_in() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=42))
    assert await directory_readiness_errors(session, application(directory_enabled=False)) == [
        "directory_enabled"
    ]


def test_directory_patch_normalizes_tags_and_rejects_ambiguous_values() -> None:
    patch = ApplicationPatch(directory_tags=[" Tools ", "tools", "music-bot"])
    assert patch.directory_tags == ["tools", "music-bot"]
    with pytest.raises(ValidationError):
        ApplicationPatch(directory_tags=["not a tag"])
    with pytest.raises(ValidationError):
        ApplicationPatch(directory_tags=["one", "two", "three", "four", "five", "six"])


def test_directory_product_metadata_is_strict_bounded_and_canonical() -> None:
    patch = ApplicationPatch(
        directory_media=[
            {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
            {"type": "image", "asset_id": "42"},
        ],
        directory_external_links=[{"name": "  Website  ", "url": "https://alpha.example/app"}],
        directory_supported_locales=["fr", "en-US"],
        directory_description_localizations={"fr": "  Une application utile.  "},
    )
    assert [item.type for item in patch.directory_media or []] == ["youtube", "image"]
    assert (patch.directory_external_links or [])[0].name == "Website"
    assert patch.directory_supported_locales == ["en-US", "fr"]
    assert patch.directory_description_localizations == {"fr": "Une application utile."}

    with pytest.raises(ValidationError):
        ApplicationPatch(
            directory_media=[
                {"type": "youtube", "video_id": f"video{index:06d}"[-11:]} for index in range(6)
            ]
        )
    with pytest.raises(ValidationError):
        ApplicationPatch(
            directory_media=[
                {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
                {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
            ]
        )
    with pytest.raises(ValidationError):
        ApplicationPatch(
            directory_media=[
                {
                    "type": "youtube",
                    "video_id": "dQw4w9WgXcQ",
                    "url": "https://attacker.example/embed",
                }
            ]
        )
    with pytest.raises(ValidationError):
        ApplicationPatch(
            directory_external_links=[{"name": "Website", "url": "http://alpha.example/app"}]
        )
    with pytest.raises(ValidationError):
        ApplicationPatch(
            directory_supported_locales=["en-US"],
            directory_description_localizations={"fr": "Non déclarée"},
        )


@pytest.mark.asyncio
async def test_directory_metadata_patch_serializes_nested_models_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = application(
        directory_enabled=False,
        directory_approved=True,
        default_scopes=[],
        default_intents=[],
        supported_install_types=["guild_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        manifest_generation=7,
    )
    bot = SimpleNamespace()
    session = SimpleNamespace()
    settings = SimpleNamespace(domain="alpha.example")
    managed = AsyncMock(
        return_value=(current, SimpleNamespace(role="owner"), bot),
    )
    commit = AsyncMock()
    monkeypatch.setattr(applications, "managed_application", managed)
    monkeypatch.setattr(
        applications,
        "proxy_remote_application_management",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(applications, "commit_developer_application_mutation", commit)
    monkeypatch.setattr(
        applications,
        "application_payload",
        lambda app, _bot: {
            "directory_summary": app.directory_summary,
            "directory_category": app.directory_category,
            "directory_tags": app.directory_tags,
            "directory_media": app.directory_media,
            "directory_external_links": app.directory_external_links,
            "directory_supported_locales": app.directory_supported_locales,
            "directory_description_localizations": app.directory_description_localizations,
            "manifest_generation": str(app.manifest_generation),
        },
    )
    api = application_patch_api(session, settings)

    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/api/v1/applications/10",
            json={
                "directory_summary": "A polished listing.",
                "directory_category": "utilities",
                "directory_tags": [" Tools ", "tools", "weather"],
                "directory_media": [
                    {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
                ],
                "directory_external_links": [
                    {"name": "  Website  ", "url": "https://alpha.example/app"},
                ],
                "directory_supported_locales": ["fr", "en-US"],
                "directory_description_localizations": {
                    "fr": "  Une application utile.  ",
                },
            },
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "directory_summary": "A polished listing.",
        "directory_category": "utilities",
        "directory_tags": ["tools", "weather"],
        "directory_media": [{"type": "youtube", "video_id": "dQw4w9WgXcQ"}],
        "directory_external_links": [
            {"name": "Website", "url": "https://alpha.example/app"},
        ],
        "directory_supported_locales": ["en-US", "fr"],
        "directory_description_localizations": {"fr": "Une application utile."},
        "manifest_generation": "8",
    }
    assert type(current.directory_media[0]) is dict
    assert type(current.directory_external_links[0]) is dict
    assert type(current.directory_external_links[0]["url"]) is str
    assert current.directory_approved is False
    commit.assert_awaited_once_with(session, settings, current)


@pytest.mark.parametrize(
    "payload",
    [
        {"directory_media": None},
        {
            "directory_media": [
                {
                    "type": "youtube",
                    "video_id": "dQw4w9WgXcQ",
                    "embed_url": "https://attacker.example/embed",
                },
            ],
        },
        {
            "directory_external_links": [
                {"name": "Website", "url": "http://alpha.example/app"},
            ],
        },
    ],
)
@pytest.mark.asyncio
async def test_directory_metadata_patch_fails_closed_over_http(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    session = SimpleNamespace()
    settings = SimpleNamespace(domain="alpha.example")
    managed = AsyncMock()
    proxy = AsyncMock(return_value=None)
    monkeypatch.setattr(applications, "managed_application", managed)
    monkeypatch.setattr(applications, "proxy_remote_application_management", proxy)
    api = application_patch_api(session, settings)

    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch("/api/v1/applications/10", json=payload)

    assert response.status_code == 422
    managed.assert_not_awaited()
    proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_directory_media_references_are_owned_store_assets() -> None:
    asset = SimpleNamespace(
        id=42,
        application_id=10,
        application_domain="alpha.example",
        kind="store",
    )
    session = SimpleNamespace(scalars=AsyncMock(return_value=[asset]))
    app = application(
        directory_media=[
            {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
            {"type": "image", "asset_id": "42"},
        ]
    )
    assert await directory_media_assets_valid(session, app)
    assert not await directory_media_assets_valid(
        SimpleNamespace(scalars=AsyncMock(return_value=[])), app
    )


@pytest.mark.asyncio
async def test_directory_detail_projects_ordered_media_commands_and_similar_apps() -> None:
    app = application(
        directory_media=[
            {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
            {"type": "image", "asset_id": "42"},
        ],
        directory_external_links=[{"name": "Website", "url": "https://alpha.example/app"}],
        directory_supported_locales=["en-US", "fr"],
        directory_description_localizations={"fr": "Une application utile."},
    )
    asset = SimpleNamespace(
        id=42,
        name="Dashboard",
        media_hash="a" * 64,
        content_type="image/png",
        width=1280,
        height=720,
    )
    command = SimpleNamespace(
        authority_id=55,
        name="forecast",
        definition={"description": "Show the current forecast."},
    )
    similar = application(
        id=11,
        name="Another Utility",
        directory_summary="Another useful directory app.",
        directory_tags=["tools"],
        directory_collections=[],
        supported_install_types=["guild_install"],
    )
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[asset], [similar]]),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(command, 7)])),
    )

    payload = await directory_detail_projection(
        session,
        SimpleNamespace(domain="alpha.example"),
        app,
        SimpleNamespace(slug="default", name="Install", description=None),
        target_domain="beta.example",
    )

    assert payload["media"] == [
        {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
        {
            "type": "image",
            "asset_id": "42",
            "name": "Dashboard",
            "media_hash": "a" * 64,
            "content_type": "image/png",
            "width": 1280,
            "height": 720,
        },
    ]
    assert payload["popular_commands"] == [
        {"id": "55", "name": "forecast", "description": "Show the current forecast."}
    ]
    assert [item["ref"] for item in payload["similar_apps"]] == ["11@alpha.example"]
    popular_statement = session.execute.await_args.args[0]
    similar_statement = session.scalars.await_args_list[1].args[0]
    assert popular_statement._limit_clause.value == 5
    assert similar_statement._limit_clause.value == 3
    assert "bot_instance_rules.effect" in str(similar_statement)


def test_remote_directory_detail_rejects_untrusted_product_page_content() -> None:
    payload = detail_projection(
        application(
            directory_external_links=[{"name": "Website", "url": "https://alpha.example/app"}],
            directory_supported_locales=["en-US"],
        )
    )
    invalid_updates = (
        {
            "media": [
                {
                    "type": "youtube",
                    "video_id": "dQw4w9WgXcQ",
                    "embed_url": "https://attacker.example/embed",
                }
            ]
        },
        {"external_links": [{"name": "Website", "url": "http://alpha.example"}]},
        {"description_localizations": {"fr": "Unsupported locale"}},
        {
            "popular_commands": [
                {"id": "55", "name": "forecast", "description": "Forecast"},
                {"id": "55", "name": "weather", "description": "Weather"},
            ]
        },
        {
            "similar_apps": [
                {
                    "id": "11",
                    "ref": "11@evil.example",
                    "origin_domain": "evil.example",
                    "name": "Injected",
                    "summary": "Injected recommendation.",
                    "category": "utilities",
                    "tags": ["tools"],
                    "icon_hash": None,
                }
            ]
        },
    )
    for update in invalid_updates:
        with pytest.raises(ValidationError):
            DirectoryApplication.model_validate(payload | update)


def test_directory_urls_reject_noncanonical_hosts_as_validation_errors() -> None:
    with pytest.raises(ValidationError):
        ApplicationPatch(support_url="https://localhost/support")


def test_directory_approval_only_resets_for_meaningful_review_changes() -> None:
    app = application()
    assert not directory_patch_requires_reapproval(
        app,
        {
            "name": app.name,
            "directory_summary": app.directory_summary,
            "directory_tags": list(app.directory_tags),
        },
    )
    assert directory_patch_requires_reapproval(app, {"directory_summary": "Changed"})
    assert directory_patch_requires_reapproval(app, {"directory_enabled": False})


def test_operator_collection_patch_is_bounded_and_unique() -> None:
    patch = ApplicationDirectoryApprovalPatch(
        collections=["featured", "staff-picks"], reason="  Curated review  "
    )
    assert patch.approved is None
    assert patch.reason == "Curated review"
    with pytest.raises(ValidationError):
        ApplicationDirectoryApprovalPatch(
            collections=["featured", "featured"], reason="Duplicate placement"
        )
    with pytest.raises(ValidationError):
        ApplicationDirectoryApprovalPatch(reason="No change")
    with pytest.raises(ValidationError):
        ApplicationDirectoryApprovalPatch(approved=True, reason="  ")


def test_directory_target_filter_preserves_local_and_enforces_remote_policy() -> None:
    app = application(target_policy="local_only")
    assert directory_target_allowed(
        app, target_domain="alpha.example", local_domain="alpha.example", rule=None
    )
    assert not directory_target_allowed(
        app,
        target_domain="beta.example",
        local_domain="alpha.example",
        rule="allow",
    )
    app.target_policy = "allowlist"
    assert directory_target_allowed(
        app,
        target_domain="beta.example",
        local_domain="alpha.example",
        rule="allow",
    )
    assert not directory_target_allowed(
        app,
        target_domain="gamma.example",
        local_domain="alpha.example",
        rule=None,
    )


def test_directory_query_domain_rejects_invalid_remote_domain() -> None:
    assert directory_query_domain(None, "alpha.example") == "alpha.example"
    assert directory_query_domain("BETA.EXAMPLE.", "alpha.example") == "beta.example"
    with pytest.raises(HTTPException) as invalid:
        directory_query_domain("https://beta.example", "alpha.example")
    assert invalid.value.status_code == 422
    assert invalid.value.detail == {"code": "INVALID_FEDERATION_DOMAIN"}


def test_federated_directory_response_is_strict() -> None:
    page = {
        "items": [],
        "next_cursor": None,
        "collections": list(DIRECTORY_COLLECTIONS),
        "selected_collection": None,
    }
    with pytest.raises(ValidationError):
        DirectoryPage.model_validate(page | {"unexpected": True})

    payload = directory_projection(
        application(), SimpleNamespace(slug="default", name="Install", description=None)
    )
    assert DirectoryPage.model_validate(page | {"items": [payload], "next_cursor": "10"})
    payload["install_template"]["unexpected"] = True  # type: ignore[index]
    with pytest.raises(ValidationError):
        DirectoryPage.model_validate(page | {"items": [payload]})

    payload = directory_projection(
        application(icon_hash="not-a-digest"),
        SimpleNamespace(slug="default", name="Install", description=None),
    )
    with pytest.raises(ValidationError):
        DirectoryPage.model_validate(page | {"items": [payload]})

    payload = detail_projection(application(support_url="javascript:alert(1)"))
    with pytest.raises(ValidationError):
        DirectoryApplication.model_validate(payload)


def test_remote_directory_response_is_bound_to_request_lineage_and_page() -> None:
    payload = directory_projection(
        application(), SimpleNamespace(slug="default", name="Install", description=None)
    )
    page = {
        "items": [payload],
        "next_cursor": None,
        "collections": list(DIRECTORY_COLLECTIONS),
        "selected_collection": None,
    }
    search = DirectorySearch(limit=1)
    assert validated_remote_directory_page(page, domain="alpha.example", search=search)["items"]
    assert (
        validated_remote_directory_detail(
            detail_projection(),
            application_id=10,
            domain="alpha.example",
        )["id"]
        == "10"
    )

    foreign = directory_projection(
        application(origin_domain="evil.example"),
        SimpleNamespace(slug="default", name="Install", description=None),
    )
    with pytest.raises(ValueError):
        validated_remote_directory_page(
            page | {"items": [foreign]}, domain="alpha.example", search=search
        )
    with pytest.raises(ValueError):
        validated_remote_directory_detail(
            detail_projection(),
            application_id=11,
            domain="alpha.example",
        )
    with pytest.raises(ValueError):
        validated_remote_directory_page(
            page | {"selected_collection": "featured"},
            domain="alpha.example",
            search=search,
        )
    collection_search = DirectorySearch(limit=1, collection="featured")
    assert validated_remote_directory_page(
        page | {"selected_collection": "featured"},
        domain="alpha.example",
        search=collection_search,
    )["items"]
    unplaced = directory_projection(
        application(directory_collections=[]),
        SimpleNamespace(slug="default", name="Install", description=None),
    )
    with pytest.raises(ValueError):
        validated_remote_directory_page(
            page | {"items": [unplaced], "selected_collection": "featured"},
            domain="alpha.example",
            search=collection_search,
        )


@pytest.mark.asyncio
async def test_directory_query_applies_policy_before_a_bounded_page() -> None:
    tuples = SimpleNamespace(all=lambda: [])
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(tuples=lambda: tuples))
    )

    rows = await directory_rows(
        session,
        SimpleNamespace(domain="alpha.example"),
        DirectorySearch(limit=2),
        target_domain="beta.example",
    )

    assert rows == []
    statement = session.execute.await_args.args[0]
    assert statement._limit_clause.value == 3
    rendered = str(statement)
    assert "min(bot_install_templates.id)" in rendered
    assert "bot_instance_rules.effect" in rendered


@pytest.mark.asyncio
async def test_silenced_peer_cannot_search_the_application_directory() -> None:
    with pytest.raises(HTTPException) as denied:
        await federation_search_application_directory(
            DirectorySearch(),
            SimpleNamespace(silenced=True),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "KAED_FED_INSTANCE_SILENCED"}


def test_remote_directory_rejects_impossible_install_contracts() -> None:
    payload = directory_projection(
        application(), SimpleNamespace(slug="default", name="Install", description=None)
    )
    page = {
        "items": [payload],
        "next_cursor": None,
        "collections": list(DIRECTORY_COLLECTIONS),
        "selected_collection": None,
    }
    template = payload["install_template"]
    assert isinstance(template, dict)
    for update in (
        {"install_template": None},
        {"verified": False},
        {"user_install_supported": False},
        {"tags": []},
        {"summary": None},
        {"collections": ["featured", "featured"]},
        {
            "install_template": {
                **template,
                "install_types": ["user_install"],
                "default_install_type": "guild_install",
            }
        },
    ):
        with pytest.raises(ValidationError):
            DirectoryPage.model_validate(page | {"items": [payload | update]})

    oversized_items = [
        directory_projection(
            application(id=index),
            SimpleNamespace(slug="default", name="Install", description=None),
        )
        for index in range(1, 52)
    ]
    with pytest.raises(ValidationError):
        DirectoryPage.model_validate(page | {"items": oversized_items})


@pytest.mark.asyncio
async def test_directory_approval_queues_remote_developer_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    app = application(
        team_id=5,
        team_domain="alpha.example",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=app))
    principal = SimpleNamespace(require=Mock())
    commit = AsyncMock()
    monkeypatch.setattr(admin_portal, "audit", AsyncMock())
    monkeypatch.setattr(admin_portal, "commit_developer_application_mutation", commit)
    settings = SimpleNamespace(domain="alpha.example")

    await admin_portal.patch_application_directory_approval(
        EntityRef("10@alpha.example"),
        ApplicationDirectoryApprovalPatch(approved=False, reason="Re-review"),
        principal,
        session,
        SimpleNamespace(),
        settings,
    )

    commit.assert_awaited_once_with(session, settings, app)
