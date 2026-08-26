from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.admin.auth import AdminPrincipal
from app.api import admin_portal
from app.api.admin_portal import ReportActionCreate, ReportPatch, report_subject_ref
from app.api.dependencies import suspension_blocks_request
from app.auth.account_status import (
    account_is_banned,
    account_is_suspended,
    account_is_temporarily_suspended,
)
from app.db.bot_models import AbuseReport
from app.db.models import InstanceUserRestriction, User


def test_report_enforcement_requires_an_action_and_meaningful_reason() -> None:
    with pytest.raises(ValidationError):
        ReportActionCreate(reason="reviewed")
    with pytest.raises(ValidationError):
        ReportActionCreate(account_action="suspend_24h", reason="   ")

    action = ReportActionCreate(
        account_action="suspend_7d",
        message_action="delete_24h",
        reason="  repeated harassment  ",
    )

    assert action.reason == "repeated harassment"


@pytest.mark.parametrize(
    ("target_type", "target_ref", "evidence", "expected"),
    [
        ("user", "7@local.test", {}, "7@local.test"),
        ("message", "99@local.test", {"author_ref": "7@local.test"}, "7@local.test"),
        (
            "attachment",
            "88@local.test",
            {"uploader_ref": "7@local.test"},
            "7@local.test",
        ),
        ("guild", "1@local.test", {}, None),
    ],
)
def test_report_subject_ref_finds_the_account_that_can_be_actioned(
    target_type: str,
    target_ref: str,
    evidence: dict[str, object],
    expected: str | None,
) -> None:
    report = cast(
        AbuseReport,
        SimpleNamespace(target_type=target_type, target_ref=target_ref, evidence=evidence),
    )

    assert report_subject_ref(report) == expected


def test_expired_temporary_suspension_restores_account_access() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    active = SimpleNamespace(disabled_at=None, suspended_until=now - timedelta(seconds=1))
    temporary = SimpleNamespace(disabled_at=None, suspended_until=now + timedelta(hours=1))
    permanent = SimpleNamespace(disabled_at=now, suspended_until=None)

    assert account_is_suspended(active, now=now) is False
    assert account_is_suspended(temporary, now=now) is True
    assert account_is_suspended(permanent, now=now) is True
    assert account_is_temporarily_suspended(temporary, now=now) is True
    assert account_is_banned(temporary) is False
    assert account_is_banned(permanent) is True


@pytest.mark.parametrize(
    ("method", "path", "blocked"),
    [
        ("GET", "/api/v1/channels/1/messages", False),
        ("POST", "/api/v1/channels/1/messages", True),
        ("POST", "/api/v1/channels/1/messages/2/reactions", True),
        ("POST", "/api/v1/auth/logout", False),
        ("POST", "/api/v1/reports", False),
        ("PUT", "/api/v1/reports/1/attachment-evidence", False),
    ],
)
def test_suspended_account_request_policy(method: str, path: str, blocked: bool) -> None:
    request = Request({"type": "http", "method": method, "path": path, "headers": []})

    assert suspension_blocks_request(request) is blocked


class FakeSnowflake:
    async def mint(self) -> int:
        return 123


class FakeTokenStore:
    revoked: list[str] = []

    def __init__(self, _redis: object, _ttl: int) -> None:
        self.revoked = []
        FakeTokenStore.revoked = self.revoked

    async def revoke_session(self, session_id: str) -> None:
        self.revoked.append(session_id)


class FakeSession:
    def __init__(self, report: AbuseReport, target: User) -> None:
        self.report = report
        self.target = target
        self.added: list[object] = []
        self.committed = False
        self.refreshed = False

    async def get(self, model: type[object], key: object, **_kwargs: object) -> object | None:
        if model is AbuseReport:
            return self.report
        if model is User:
            return self.target
        return None

    async def scalars(self, _statement: object) -> list[str]:
        return ["session-one", "session-two"]

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True
        # Model SQLAlchemy expiring the database-generated ON UPDATE value.
        self.report.updated_at = None

    async def refresh(self, value: object) -> None:
        assert value is self.report
        self.report.updated_at = datetime(2026, 8, 25, 12, 0, 1, tzinfo=UTC)
        self.refreshed = True


@pytest.mark.asyncio
async def test_closing_report_refreshes_database_managed_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    report = cast(
        AbuseReport,
        SimpleNamespace(
            id=43,
            target_type="message",
            target_ref="99@local.test",
            evidence={"author_ref": "7@local.test"},
            message_ref="99@local.test",
            source="user",
            reporter_id=9,
            reporter_domain="local.test",
            category="harassment",
            description="abuse",
            encryption_mode="plaintext",
            status="in_review",
            assigned_admin_id=None,
            assigned_admin_domain=None,
            resolution=None,
            created_at=now,
            updated_at=now,
            resolved_at=None,
        ),
    )
    target = cast(User, SimpleNamespace(id=7, origin_domain="local.test"))
    session = FakeSession(report, target)
    actor = cast(User, SimpleNamespace(id=1, origin_domain="local.test"))
    principal = AdminPrincipal(actor, frozenset({"trust_safety"}), frozenset({"*"}))
    monkeypatch.setattr(admin_portal, "audit", AsyncMock())

    result = await admin_portal.patch_report(
        43,
        ReportPatch(status="closed_no_action", resolution="reviewed; no violation"),
        principal,
        cast(Any, session),
        cast(Any, FakeSnowflake()),
    )

    assert result["status"] == "closed_no_action"
    assert result["updated_at"] == "2026-08-25T12:00:01+00:00"
    assert report.resolved_at is not None
    assert session.refreshed is True


@pytest.mark.asyncio
async def test_permanent_report_suspension_closes_case_and_revokes_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    report = cast(
        AbuseReport,
        SimpleNamespace(
            id=44,
            target_type="user",
            target_ref="7@local.test",
            evidence={},
            message_ref=None,
            source="user",
            reporter_id=9,
            reporter_domain="local.test",
            category="harassment",
            description="abuse",
            encryption_mode="plaintext",
            status="submitted",
            assigned_admin_id=None,
            assigned_admin_domain=None,
            resolution=None,
            created_at=now,
            updated_at=now,
            resolved_at=None,
        ),
    )
    target = cast(
        User,
        SimpleNamespace(
            id=7,
            origin_domain="local.test",
            is_local=True,
            account_type="human",
            disabled_at=None,
            suspended_until=None,
        ),
    )
    actor = cast(User, SimpleNamespace(id=1, origin_domain="local.test"))
    principal = AdminPrincipal(actor, frozenset({"trust_safety"}), frozenset({"*"}))
    session = FakeSession(report, target)
    monkeypatch.setattr(admin_portal, "AccessTokenStore", FakeTokenStore)

    result = await admin_portal.enforce_report(
        44,
        ReportActionCreate(account_action="suspend_permanent", reason="confirmed abuse"),
        principal,
        cast(Any, session),
        cast(Any, object()),
        cast(Any, FakeSnowflake()),
        cast(Any, SimpleNamespace(domain="local.test", access_token_ttl_seconds=900)),
    )

    assert target.disabled_at is not None
    assert target.suspended_until is None
    assert report.status == "action_taken"
    assert report.resolution == "confirmed abuse"
    assert session.committed is True
    assert FakeTokenStore.revoked == ["session-one", "session-two"]
    assert cast(dict[str, object], result["enforcement"])["permanently_suspended"] is True


@pytest.mark.asyncio
async def test_remote_report_ban_is_owned_locally_and_removes_local_memberships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    report = cast(
        AbuseReport,
        SimpleNamespace(
            id=45,
            target_type="user",
            target_ref="8@remote.test",
            evidence={},
            message_ref=None,
            source="user",
            reporter_id=9,
            reporter_domain="local.test",
            category="harassment",
            description="abuse",
            encryption_mode="plaintext",
            status="submitted",
            assigned_admin_id=None,
            assigned_admin_domain=None,
            resolution=None,
            created_at=now,
            updated_at=now,
            resolved_at=None,
        ),
    )
    target = cast(
        User,
        SimpleNamespace(
            id=8,
            origin_domain="remote.test",
            is_local=False,
            account_type="human",
            disabled_at=None,
            suspended_until=None,
        ),
    )
    actor = cast(User, SimpleNamespace(id=1, origin_domain="local.test"))
    principal = AdminPrincipal(actor, frozenset({"trust_safety"}), frozenset({"*"}))
    session = FakeSession(report, target)
    remove_memberships = AsyncMock(return_value=[SimpleNamespace()])
    publish_removals = AsyncMock()
    monkeypatch.setattr(admin_portal, "remove_remote_user_from_local_guilds", remove_memberships)
    monkeypatch.setattr(admin_portal, "publish_remote_user_guild_removals", publish_removals)

    result = await admin_portal.enforce_report(
        45,
        ReportActionCreate(account_action="ban_permanent", reason="confirmed abuse"),
        principal,
        cast(Any, session),
        cast(Any, object()),
        cast(Any, FakeSnowflake()),
        cast(Any, SimpleNamespace(domain="local.test", access_token_ttl_seconds=900)),
    )

    restriction = next(item for item in session.added if isinstance(item, InstanceUserRestriction))
    assert restriction.restriction_type == "banned"
    assert restriction.expires_at is None
    remove_memberships.assert_awaited_once()
    publish_removals.assert_awaited_once()
    enforcement = cast(dict[str, object], result["enforcement"])
    assert enforcement["banned"] is True
    assert enforcement["guild_memberships_removed"] == 1
