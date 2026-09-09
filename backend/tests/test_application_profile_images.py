from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import application_assets


@pytest.mark.asyncio
@pytest.mark.parametrize("icon,banner", [("a" * 64, "b" * 64), (None, "b" * 64), (None, None)])
async def test_asset_mutations_update_bot_before_publishing_snapshots(monkeypatch, icon, banner):
    bot = SimpleNamespace(avatar_hash="c" * 64, banner_hash="d" * 64, profile_version=7)
    application = SimpleNamespace(
        bot_user_id=30, bot_user_domain="apps.example", icon_hash=icon, banner_hash=banner
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=bot))
    queue = AsyncMock(return_value={"remote.example"})
    wake = AsyncMock()

    async def commit(*args):
        assert (bot.avatar_hash, bot.banner_hash, bot.profile_version) == (icon, banner, 8)
        queue.assert_awaited_once()
        wake.assert_not_awaited()

    monkeypatch.setattr(application_assets, "queue_profile_updates", queue)
    monkeypatch.setattr(application_assets, "commit_developer_application_mutation", commit)
    monkeypatch.setattr(application_assets, "wake_federation_destinations", wake)
    await application_assets._commit_asset_mutation(
        session, SimpleNamespace(domain="apps.example"), application, profile_changed=True
    )
    wake.assert_awaited_once_with({"remote.example"})


@pytest.mark.asyncio
async def test_other_assets_leave_bot_profile_unchanged(monkeypatch):
    session = SimpleNamespace(scalar=AsyncMock())
    commit = AsyncMock()
    monkeypatch.setattr(application_assets, "commit_developer_application_mutation", commit)
    monkeypatch.setattr(application_assets, "wake_federation_destinations", AsyncMock())
    await application_assets._commit_asset_mutation(
        session, SimpleNamespace(), SimpleNamespace(), profile_changed=False
    )
    session.scalar.assert_not_awaited()
    commit.assert_awaited_once()
