import importlib
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def federation_verifier(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv(
        "ALPHA_DATABASE_URL",
        "postgresql+asyncpg://kaede:kaede@alpha-postgres:5432/kaede",
    )
    monkeypatch.setenv(
        "BETA_DATABASE_URL",
        "postgresql+asyncpg://kaede:kaede@beta-postgres:5432/kaede",
    )
    monkeypatch.setenv("BETA_DRAGONFLY_URL", "redis://beta-dragonfly:6379/0")
    return importlib.import_module("scripts.verify_federation")


def test_bounded_directory_page_accepts_strict_bounded_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    payload = {
        "items": [{"ref": "20@alpha.localhost"}],
        "next_cursor": "20",
        "collections": [],
        "selected_collection": None,
    }

    page, items = verifier.bounded_directory_page(payload, maximum=1)

    assert page == payload
    assert items == payload["items"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"items": [], "next_cursor": None, "collections": []},
        {
            "items": [{}, {}],
            "next_cursor": None,
            "collections": [],
            "selected_collection": None,
        },
        {
            "items": ["not-an-object"],
            "next_cursor": None,
            "collections": [],
            "selected_collection": None,
        },
        {
            "items": [],
            "next_cursor": "020",
            "collections": [],
            "selected_collection": None,
        },
    ],
)
def test_bounded_directory_page_rejects_malformed_or_oversized_payloads(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    verifier = federation_verifier(monkeypatch)

    with pytest.raises(verifier.VerificationFailure):
        verifier.bounded_directory_page(payload, maximum=1)


def test_bounded_directory_page_rejects_nonpositive_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)

    with pytest.raises(ValueError, match="positive"):
        verifier.bounded_directory_page({}, maximum=0)


@pytest.mark.parametrize(
    ("body", "header", "expected"),
    [
        ({"detail": {"retry_after_ms": 750}}, "1.25", 1.25),
        ({"detail": {"retry_after_ms": 750}}, None, 0.75),
        ({"detail": {"retry_after_ms": True}}, "0.5", 0.5),
        ({"detail": {"retry_after_ms": -1}}, "not-a-number", None),
        (None, "nan", None),
    ],
)
def test_rate_limit_retry_seconds_uses_only_positive_finite_hints(
    monkeypatch: pytest.MonkeyPatch,
    body: object,
    header: str | None,
    expected: float | None,
) -> None:
    verifier = federation_verifier(monkeypatch)
    headers = {"Retry-After": header} if header is not None else None
    response = httpx.Response(429, json=body, headers=headers)

    assert verifier.rate_limit_retry_seconds(response) == expected


def test_single_inbox_result_requires_exact_retry_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    expected = {
        "results": [
            {
                "event_id": "event-1",
                "status": "retry",
                "code": "KAED_FED_RESYNC_RETRY",
            }
        ]
    }

    verifier.require_single_inbox_result(
        httpx.Response(200, json=expected),
        event_id="event-1",
        status="retry",
        code="KAED_FED_RESYNC_RETRY",
    )
    with pytest.raises(verifier.VerificationFailure, match="invalid result"):
        verifier.require_single_inbox_result(
            httpx.Response(200, json={"results": []}),
            event_id="event-1",
            status="retry",
            code="KAED_FED_RESYNC_RETRY",
        )


@pytest.mark.parametrize("raw_sequence", [None, 1, True, "0", "01", "-1", "1.0"])
def test_guild_event_sequence_rejects_noncanonical_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_sequence: object,
) -> None:
    verifier = federation_verifier(monkeypatch)

    with pytest.raises(verifier.VerificationFailure, match="invalid guild sequence"):
        verifier.guild_event_sequence(
            {"context": {"seq": raw_sequence}},
            label="fixture",
        )

    assert verifier.guild_event_sequence({"context": {"seq": "42"}}, label="fixture") == 42


@pytest.mark.asyncio
async def test_park_locked_outbox_events_requires_and_parks_exact_active_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    first_outbox = SimpleNamespace(status="retry", next_retry_at=None, last_error="blocked")
    second_outbox = SimpleNamespace(status="pending", next_retry_at=None, last_error=None)
    first_event = SimpleNamespace(
        event_id="event-1",
        envelope={"content": {"message": {"client_nonce": "gap-1"}}},
    )
    second_event = SimpleNamespace(
        event_id="event-2",
        envelope={"content": {"message": {"client_nonce": "gap-2"}}},
    )
    result = Mock()
    result.tuples.return_value = [
        (first_outbox, first_event),
        (second_outbox, second_event),
    ]
    session = AsyncMock()
    session.execute.return_value = result

    parked = await verifier.park_locked_outbox_events(
        session,
        "beta.localhost",
        {"gap-1", "gap-2"},
    )

    assert parked == {
        "gap-1": ("event-1", first_event.envelope),
        "gap-2": ("event-2", second_event.envelope),
    }
    assert first_outbox.status == second_outbox.status == "pending"
    assert first_outbox.next_retry_at == second_outbox.next_retry_at
    assert first_outbox.last_error is None

    with pytest.raises(verifier.VerificationFailure, match="missing"):
        await verifier.park_locked_outbox_events(
            session,
            "beta.localhost",
            {"missing"},
        )


@pytest.mark.asyncio
async def test_guild_policy_delivery_reports_receiver_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    monkeypatch.setattr(
        verifier,
        "guild_policy_outbox_state",
        AsyncMock(return_value=("failed", "sender rejected delivery", "event-1")),
    )
    monkeypatch.setattr(
        verifier,
        "database_scalar",
        AsyncMock(return_value="receiver rejected event"),
    )

    with pytest.raises(
        verifier.VerificationFailure,
        match="sender rejected delivery; receiver error: receiver rejected event",
    ):
        await verifier.require_guild_policy_delivery("full_retained", label="opt-in")


@pytest.mark.asyncio
async def test_wait_for_honors_rate_limit_and_rebuilds_each_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    private_key = Ed25519PrivateKey.generate()
    responses = iter(
        (
            httpx.Response(
                429,
                json={"detail": {"retry_after_ms": 1_500}},
                headers={"Retry-After": "1"},
            ),
            httpx.Response(200),
        )
    )
    assertions: list[dict[str, object]] = []

    async def operation() -> httpx.Response:
        assertions.append(
            verifier.worker_assertion(
                private_key,
                "20@alpha.localhost",
                40,
                "https://beta.localhost",
                "/api/v1/bots/token",
            )
        )
        return next(responses)

    now = 100.0

    async def advance(delay: float) -> None:
        nonlocal now
        now += delay

    sleep = AsyncMock(side_effect=advance)
    monkeypatch.setattr(verifier.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        verifier, "time", SimpleNamespace(monotonic=lambda: now, time=lambda: 1_700_000_000 + now)
    )

    result = await verifier.wait_for(
        operation,
        lambda response: response.status_code == 200,
        "token did not converge",
        poll_seconds=0.2,
    )

    assert result.status_code == 200
    sleep.assert_awaited_once_with(1.5)
    assert len(assertions) == 2
    assert assertions[0]["nonce"] != assertions[1]["nonce"]
    assert assertions[0]["signature"] != assertions[1]["signature"]


@pytest.mark.asyncio
async def test_wait_for_caps_rate_limit_sleep_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    response = httpx.Response(429, json={"detail": {"retry_after_ms": 30_000}})
    now = 100.0

    async def respond() -> httpx.Response:
        nonlocal now
        now += 0.5
        return response

    async def advance(delay: float) -> None:
        nonlocal now
        now += delay

    operation = AsyncMock(side_effect=respond)
    sleep = AsyncMock(side_effect=advance)
    monkeypatch.setattr(verifier.asyncio, "sleep", sleep)
    monkeypatch.setattr(verifier, "time", SimpleNamespace(monotonic=lambda: now))

    with pytest.raises(verifier.VerificationFailure, match="HTTP 429"):
        await verifier.wait_for(
            operation,
            lambda _response: False,
            "deadline reached",
            wait_seconds=2.0,
        )

    operation.assert_awaited_once()
    sleep.assert_awaited_once_with(1.5)
