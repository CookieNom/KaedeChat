import ast
import importlib
import inspect
import textwrap
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.chat.custom_emojis import canonical_reaction_emoji


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

    assert page is payload
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


def test_default_wait_covers_periodic_federation_sweep_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)

    assert inspect.signature(verifier.wait_for).parameters["wait_seconds"].default == 120


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


def test_remote_bot_discovery_waits_cover_durable_delivery_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    tree = ast.parse(textwrap.dedent(inspect.getsource(verifier.verify_remote_bot_runtime)))
    configured_waits: list[tuple[str, dict[str, object]]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wait_for"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            configured_waits.append(
                (
                    node.args[0].id,
                    {
                        keyword.arg: ast.literal_eval(keyword.value)
                        for keyword in node.keywords
                        if keyword.arg is not None
                    },
                )
            )

    target_waits = [
        keywords
        for operation, keywords in configured_waits
        if operation in {"discovered_targets", "discovered_user_target"}
    ]
    assert len(target_waits) == 3
    assert all(keywords == {"wait_seconds": 120, "poll_seconds": 1.0} for keywords in target_waits)
    token_waits = [
        keywords for operation, keywords in configured_waits if operation == "acquired_remote_token"
    ]
    assert token_waits == [{"wait_seconds": 120, "poll_seconds": 1.0}]


def test_history_import_wait_covers_durable_delivery_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    tree = ast.parse(textwrap.dedent(inspect.getsource(verifier.verify)))
    waits = [
        {
            keyword.arg: ast.literal_eval(keyword.value)
            for keyword in node.keywords
            if keyword.arg is not None
        }
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wait_for"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Constant)
            and node.args[2].value == "permission-bound historical export did not arrive"
        )
    ]

    assert waits == [{"wait_seconds": 120}]


def test_durable_mutation_waits_cover_periodic_delivery_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    functions = (verifier.require_guild_policy_delivery, verifier.verify)
    expected_messages = {
        "history {label} outbox did not settle",
        "federated CONNECT denial did not reach the remote member",
        "CONNECT did not recover after the federated overwrite returned to inherit",
        "granular channel create did not replicate",
    }
    waits: dict[str, object] = {}
    for function in functions:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "wait_for"
                and len(node.args) >= 3
            ):
                continue
            message = node.args[2]
            if isinstance(message, ast.Constant) and isinstance(message.value, str):
                label = message.value
            elif (
                isinstance(message, ast.JoinedStr)
                and message.values
                and isinstance(message.values[0], ast.Constant)
            ):
                label = f"{message.values[0].value}{{label}} outbox did not settle"
            else:
                continue
            if label not in expected_messages:
                continue
            waits[label] = next(
                ast.literal_eval(keyword.value)
                for keyword in node.keywords
                if keyword.arg == "wait_seconds"
            )

    assert waits == {message: 120 for message in expected_messages}


def test_federation_reaction_fixture_uses_one_canonical_emoji(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    tree = ast.parse(textwrap.dedent(inspect.getsource(verifier.verify)))
    assignments = [
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "reaction_emoji"
                for target in node.targets
            )
        )
    ]
    uses = sum(
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == "reaction_emoji"
        for node in ast.walk(tree)
    )

    assert assignments == ["🏮"]
    assert canonical_reaction_emoji(assignments[0]) == assignments[0]
    assert uses == 3


def test_federation_reaction_fixture_uses_standard_own_reaction_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    source = textwrap.dedent(inspect.getsource(verifier.verify))
    tree = ast.parse(source)
    reaction_calls = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"post", "put", "delete"}
            and node.args
            and "reactions" in ast.unparse(node.args[0])
        )
    ]

    assert [node.func.attr for node in sorted(reaction_calls, key=lambda node: node.lineno)] == [
        "put",
        "delete",
    ]
    assert all("/@me" in ast.unparse(node.args[0]) for node in reaction_calls)


def test_final_async_peer_drain_waits_for_outbox_to_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    source = textwrap.dedent(inspect.getsource(verifier.verify))
    drain_at = source.index("drained = await alpha.post(")
    expected_counts_at = source.index("expected_nonces =")
    drain_section = source[drain_at:expected_counts_at]

    assert "drained.status_code == 202" in drain_section
    assert "await wait_for(" in drain_section
    assert "FederationOutbox.status.in_" in drain_section
    assert "lambda value: value == 0" in drain_section


def test_gap_recovery_waits_cover_durable_delivery_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = federation_verifier(monkeypatch)
    source = textwrap.dedent(inspect.getsource(verifier.verify))
    tree = ast.parse(source)
    assignments = [
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "durable_gap_wait_seconds"
                for target in node.targets
            )
        )
    ]
    wait_uses = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == "durable_gap_wait_seconds"
        )
    ]
    held_delivery = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.AsyncWith)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "hold_outbox_delivery"
                for item in node.items
            )
        )
    ]
    parked_gap_calls = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "park_locked_outbox_events"
        )
    ]
    signed_injections = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "signed_request"
        )
    ]
    rearmed_gap_calls = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_outbox_event"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Constant)
            and node.args[2].value in {"m3-gap-1", "m3-gap-2"}
            and any(
                keyword.arg == "due"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
        )
    ]

    assert assignments == [120]
    assert len(wait_uses) == 5
    assert len(held_delivery) == 1
    assert len(parked_gap_calls) == 1
    assert len(signed_injections) == 1
    rearmed_nonces = [
        node.args[2].value for node in sorted(rearmed_gap_calls, key=lambda node: node.lineno)
    ]
    assert rearmed_nonces == [
        "m3-gap-1",
        "m3-gap-2",
    ]
    held_at = held_delivery[0].lineno
    parked_at = parked_gap_calls[0].lineno
    injected_at = signed_injections[0].lineno
    assert held_at < parked_at < injected_at < min(node.lineno for node in rearmed_gap_calls)
    gap_section = source[
        source.index("durable_gap_wait_seconds = 120") : source.index("created_channel =")
    ]
    assert "/api/v1/admin/federation/blocks" not in gap_section
    assert "/api/v1/admin/federation/peers/beta.localhost/drain" not in gap_section


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

    sleep = AsyncMock()
    monkeypatch.setattr(verifier.asyncio, "sleep", sleep)

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
    operation = AsyncMock(return_value=response)
    sleep = AsyncMock()
    monotonic = Mock(side_effect=(100.0, 100.0, 100.5, 102.0))
    monkeypatch.setattr(verifier.asyncio, "sleep", sleep)
    monkeypatch.setattr(verifier, "time", SimpleNamespace(monotonic=monotonic))

    with pytest.raises(verifier.VerificationFailure, match="HTTP 429"):
        await verifier.wait_for(
            operation,
            lambda _response: False,
            "deadline reached",
            wait_seconds=2.0,
        )

    operation.assert_awaited_once()
    sleep.assert_awaited_once_with(1.5)
