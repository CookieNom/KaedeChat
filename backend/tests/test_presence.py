from __future__ import annotations

import pytest

from app.chat import presence as account_presence
from app.db.models import User


@pytest.mark.asyncio
async def test_account_presence_broadcast_includes_private_preference_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, dict[str, object], int]] = []

    async def set_state(
        _redis: object,
        _user: User,
        status: str,
        **projection: object,
    ) -> int:
        assert status == "invisible"
        assert projection == {"activities": [], "since": None, "afk": False}
        return 17

    async def publish(
        _redis: object,
        topic: str,
        data: dict[str, object],
        *,
        user_domain: str,
        user_id: int,
        generation: int,
    ) -> bool:
        assert (user_domain, user_id) == ("alpha.test", 7)
        published.append((topic, data, generation))
        return True

    monkeypatch.setattr(account_presence, "set_presence_state", set_state)
    monkeypatch.setattr(account_presence, "publish_presence", publish)
    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )

    visible, generation = await account_presence.broadcast_presence_preference(
        object(),  # type: ignore[arg-type]
        user,
        "invisible",
        [
            "user:alpha.test:7",
            "guild:alpha.test:42",
            "user:other.test:8",
            "guild:alpha.test:42",
        ],
    )

    assert (visible, generation) == ("offline", 17)
    assert published == [
        (
            "user:alpha.test:7",
            {
                "user_id": "7",
                "user_domain": "alpha.test",
                "status": "offline",
                "activities": [],
                "since": None,
                "afk": False,
                "client_status": {},
                "preference": "invisible",
            },
            17,
        ),
        (
            "guild:alpha.test:42",
            {
                "user_id": "7",
                "user_domain": "alpha.test",
                "status": "offline",
                "activities": [],
                "since": None,
                "afk": False,
                "client_status": {},
            },
            17,
        ),
    ]
