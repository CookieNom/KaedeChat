from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core import metrics


class _Result:
    def __init__(self, row: tuple[int, ...]) -> None:
        self.row = row

    def one(self) -> tuple[int, ...]:
        return self.row


class _Session:
    def __init__(self) -> None:
        self.rows = iter(((7, 2), (80, 800), (120, 1200, 3)))

    async def execute(self, _statement: object) -> _Result:
        return _Result(next(self.rows))


class _SessionContext:
    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Redis:
    async def scan_iter(self, **_kwargs: object) -> Any:
        if False:
            yield "unused"

    async def get(self, _key: str) -> int:
        return 0

    async def hgetall(self, _key: str) -> dict[str, str]:
        return {}


@pytest.mark.asyncio
async def test_metrics_expose_federation_capacity_and_replica_pauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        metrics,
        "get_settings",
        lambda: SimpleNamespace(
            federation_inbox_max_events_total=5_000,
            federation_inbox_max_bytes_total=50_000,
            media_remote_cache_bytes=500_000,
        ),
    )

    rendered = await metrics.render_metrics(_Redis(), lambda: _SessionContext())  # type: ignore[arg-type]

    assert "kaede_federation_inbox_capacity_events 5000" in rendered
    assert "kaede_federation_inbox_capacity_bytes 50000" in rendered
    assert "kaede_federation_replica_retained_rows 120" in rendered
    assert "kaede_federation_replica_retained_bytes 1200" in rendered
    assert "kaede_federation_replica_quota_paused_guilds 3" in rendered
    assert "kaede_federation_remote_media_cache_capacity_bytes 500000" in rendered
