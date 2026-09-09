from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import applications
from app.db.bot_models import BotApplication, DeveloperTeam, DeveloperTeamMember
from app.db.models import User


@pytest.mark.asyncio
async def test_application_settings_survive_patch_and_fresh_get(postgres_schema):
    for table in (
        "users",
        "developer_teams",
        "developer_team_members",
        "bot_applications",
        "application_commands",
        "bot_application_targets",
        "bot_dm_capabilities",
    ):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
        )  # noqa: S608
    domain = "apps.example"
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        owner = User(
            id=1,
            origin_domain=domain,
            username="owner",
            is_local=True,
            account_type="human",
            password_hash="test",
            password_kdf_version=2,
            password_auth_salt=b"a" * 16,
            e2ee_vault_salt=b"b" * 16,
            profile_resolved=True,
        )
        bot = User(
            id=2,
            origin_domain=domain,
            username="bot",
            is_local=True,
            account_type="bot",
            profile_resolved=True,
        )
        session.add_all(
            [
                owner,
                bot,
                DeveloperTeam(id=3, origin_domain=domain, name="Team"),
                DeveloperTeamMember(
                    team_id=3, team_domain=domain, user_id=1, user_domain=domain, role="owner"
                ),
                BotApplication(
                    id=4,
                    origin_domain=domain,
                    team_id=3,
                    team_domain=domain,
                    bot_user_id=2,
                    bot_user_domain=domain,
                    name="App",
                ),
            ]
        )
        await session.commit()
        api = FastAPI()
        api.patch("/applications/{application_ref}")(applications.patch_application)
        api.get("/applications/{application_ref}")(applications.get_application)
        api.dependency_overrides[applications.require_user] = lambda: SimpleNamespace(user=owner)
        api.dependency_overrides[applications.get_session] = lambda: session
        api.dependency_overrides[applications.get_settings] = lambda: SimpleNamespace(domain=domain)
        changes = {
            "name": "Updated app",
            "description": "A description",
            "support_url": "https://apps.example/support",
            "privacy_url": "https://apps.example/privacy",
            "terms_url": "https://apps.example/terms",
            "directory_enabled": False,
            "directory_summary": "A summary",
            "directory_category": "utilities",
            "directory_tags": ["tools"],
            "directory_media": [{"type": "youtube", "video_id": "dQw4w9WgXcQ"}],
            "directory_external_links": [{"name": "Website", "url": "https://apps.example/app"}],
            "directory_supported_locales": ["fr"],
            "directory_description_localizations": {"fr": "Une application"},
            "target_policy": "local_only",
            "default_scopes": ["messages.send"],
            "default_intents": ["guild_messages"],
            "default_permissions": "1024",
            "supported_install_types": ["guild_install"],
            "user_install_scopes": ["applications.commands", "interactions.respond"],
            "user_install_contexts": ["guild"],
            "e2ee_modes": ["participant"],
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="https://apps.example"
        ) as client:
            response = await client.patch("/applications/4@apps.example", json=changes)
            assert response.status_code == 200, response.text
            assert {key: response.json()[key] for key in changes} == changes
            session.expunge_all()
            response = await client.get("/applications/4@apps.example")
            assert response.status_code == 200, response.text
            assert {key: response.json()[key] for key in changes} == changes
