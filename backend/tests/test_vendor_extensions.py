from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import AdminPrincipal
from app.api import vendor
from app.db.bot_models import InstanceAuditEvent
from app.vendor import load_extensions


async def test_extension_management_requires_dedicated_capability():
    principal = AdminPrincipal(None, frozenset({"auditor"}), frozenset({"admin.read"}))
    with pytest.raises(HTTPException) as error:
        await vendor.require_vendor_admin(principal)
    assert error.value.status_code == 403


async def test_installed_pane_and_reader_consent(postgres_schema, tmp_path, monkeypatch):
    package = tmp_path / "example"
    package.mkdir()
    (package / "extension.py").write_text(
        "from app.vendor import Extension, LinkRule\n"
        'extension = Extension("Example", "Example pane", [], lambda rows: rows,\n'
        '    lambda rows, platform: [LinkRule(**r) for r in rows] if platform == "web" else [])'
    )
    monkeypatch.setattr(vendor, "extensions", load_extensions(tmp_path))
    await postgres_schema.execute(
        text(
            "CREATE TABLE user_settings (user_id bigint, user_domain text, "
            "shopping_links boolean, updated_at timestamptz)"
        )
    )
    await postgres_schema.execute(
        text(
            "CREATE TABLE vendor_configurations (domain text, package text, records jsonb, "
            "version integer, PRIMARY KEY(domain,package))"
        )
    )
    await postgres_schema.execute(
        text("INSERT INTO user_settings VALUES (1,'local.test',NULL,NULL)")
    )
    auth = SimpleNamespace(
        user=SimpleNamespace(
            id=1,
            origin_domain="local.test",
            account_type="human",
            is_local=True,
            created_at=datetime.now(UTC) - timedelta(days=14, minutes=1),
        )
    )
    await postgres_schema.run_sync(InstanceAuditEvent.__table__.create)
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=1))
    row = {"host": "store.test", "parameter": "ref", "value": "shared", "replace": True}
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        await vendor.save_pane(
            "example", vendor.Records(version=0, records=[row]), auth, session, snowflake
        )
        assert (await vendor.panes(auth, session))[0]["records"] == [row]
        with pytest.raises(HTTPException) as conflict:
            await vendor.save_pane(
                "example", vendor.Records(version=0, records=[]), auth, session, snowflake
            )
        assert conflict.value.status_code == 409
        assert (
            await session.scalar(text("SELECT action FROM instance_audit_events"))
            == "vendor.configuration.update"
        )
        assert not (await vendor.open_shopping("mobile", auth, session))["prompt"]
        state = await vendor.open_shopping("web", auth, session)
        assert state["prompt"] and not state["rules"]
        assert not (await vendor.open_shopping("web", auth, session))["prompt"]
        state = await vendor.choose_shopping("web", vendor.Choice(enabled=True), auth, session)
        assert state["rules"][0].model_dump() == row
        state = await vendor.choose_shopping("web", vendor.Choice(enabled=False), auth, session)
        assert not state["rules"]
