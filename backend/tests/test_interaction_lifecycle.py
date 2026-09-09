import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.responses import Response

import app.api.interactions as interactions
import app.bots.interaction_events as interaction_events
import app.tasks as tasks
from app.chat.rich_content import PollCreate


@pytest.mark.asyncio
async def test_expiry_redacts_private_interactions_and_releases_attachments(
    monkeypatch: pytest.MonkeyPatch,
    postgres_schema,
) -> None:
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.bot_models import BotInteraction, BotInteractionResponse
    from app.db.models import Attachment

    for table in (
        "bot_interactions",
        "bot_interaction_responses",
        "attachments",
        "federated_interaction_response_locators",
        "federation_events",
        "interaction_dispatch_outbox",
        "interaction_create_dispatch_outbox",
        "bot_interaction_polls",
        "federated_interaction_admission_grants",
    ):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    now = datetime.now(UTC)
    interaction = BotInteraction(
        id=10,
        user_id=10,
        user_domain="chat.example",
        status="responded",
        token_hash=b"secret",
        dispatch_fingerprint=b"fingerprint",
        payload={"options": {"document": "30"}},
        encrypted_payload=None,
        expires_at=now - timedelta(seconds=1),
    )
    response = BotInteractionResponse(
        id=20,
        interaction_id=10,
        sequence=0,
        ephemeral=True,
        payload={"content": "private", "attachments": [{"id": "31"}]},
        deleted_at=None,
    )
    invocation_attachment = Attachment(id=30, origin_domain="chat.example", interaction_id=10)
    response_attachment = Attachment(
        id=31, origin_domain="chat.example", interaction_response_id=20
    )
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        unrelated = Attachment(id=32, origin_domain="chat.example", interaction_id=11)
        foreign = Attachment(id=30, origin_domain="foreign.example", interaction_id=10)
        session.add_all(
            [interaction, response, invocation_attachment, response_attachment, unrelated, foreign]
        )
        await session.flush()
        lock = AsyncMock()

        async def discard_row(session, _settings, attachment):
            await session.delete(attachment)

        discard = AsyncMock(side_effect=discard_row)
        monkeypatch.setattr(tasks, "lock_media_tombstone_ref", lock)
        monkeypatch.setattr(tasks, "discard_attachment", discard)
        monkeypatch.setattr(
            tasks,
            "expire_federated_interaction_attachment_grants",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            tasks,
            "queue_interaction_response_event",
            AsyncMock(return_value=None),
        )

        (
            events,
            attachments,
            count,
            relay_destinations,
            expired_topics,
        ) = await tasks.expire_bot_interactions_batch(
            session,
            SimpleNamespace(domain="chat.example"),
            now=now,
        )

        assert count == 1
        assert relay_destinations == set()
        assert expired_topics == {"user:chat.example:10"}
        assert events == [(interaction, response)]
        assert attachments == [invocation_attachment, response_attachment]
        assert interaction.status == "expired"
        assert interaction.token_hash is None
        assert interaction.dispatch_fingerprint is None
        assert interaction.payload == {}
        assert response.deleted_at == now
        assert response.payload == {}
        assert [(call.args[1], call.args[2]) for call in lock.await_args_list] == [
            (30, "chat.example"),
            (31, "chat.example"),
        ]
        assert [call.args[2] for call in discard.await_args_list] == [
            invocation_attachment,
            response_attachment,
        ]
        assert set(
            (await session.execute(select(Attachment.id, Attachment.origin_domain))).all()
        ) == {
            (32, "chat.example"),
            (30, "foreign.example"),
        }


@pytest.mark.asyncio
async def test_local_attachment_lookup_construction() -> None:
    user = SimpleNamespace(id=10, origin_domain="home.example")
    interaction = SimpleNamespace(id=30)
    stored = SimpleNamespace(id=40)
    encrypted_attachment = SimpleNamespace(
        id=50,
        origin_domain="target.example",
        interaction_response_id=40,
        scan_status="encrypted",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[interaction, stored, encrypted_attachment])
    )

    selected = await interactions.local_private_response_attachment(
        session,
        SimpleNamespace(domain="target.example"),
        user,
        (30, "target.example"),
        (40, "target.example"),
        (50, "target.example"),
    )

    assert selected is encrypted_attachment
    attachment_query = session.scalar.await_args_list[2].args[0]
    compiled = str(attachment_query.compile(compile_kwargs={"literal_binds": True}))
    assert "attachments.scan_status IN ('clean', 'encrypted')" in compiled


@pytest.mark.asyncio
async def test_expired_private_response_is_physically_removed_from_redis_stream() -> None:
    now = datetime.now(UTC)
    expired = {
        "t": "INTERACTION_RESPONSE_CREATE",
        "d": {"expires_at": (now - timedelta(seconds=1)).isoformat(), "content": "secret"},
        "topic_seq": 1,
    }
    current = {
        "t": "INTERACTION_RESPONSE_UPDATE",
        "d": {"expires_at": (now + timedelta(minutes=1)).isoformat()},
        "topic_seq": 2,
    }
    redis = SimpleNamespace(
        xrange=AsyncMock(
            return_value=[
                ("1-0", {"event": json.dumps(expired)}),
                ("2-0", {"event": json.dumps(current)}),
            ]
        ),
        xdel=AsyncMock(return_value=1),
    )

    removed = await interaction_events.purge_expired_interaction_response_streams(
        redis,
        {"user:home.example:10"},
        now=now,
    )

    assert removed == 1
    redis.xdel.assert_awaited_once_with(
        "dispatch:stream:user:home.example:10",
        "1-0",
    )


def private_dispatch_objects(now: datetime) -> tuple[SimpleNamespace, ...]:
    interaction = SimpleNamespace(
        id=10,
        application_id=20,
        application_domain="apps.example",
        user_id=30,
        user_domain="home.example",
        channel_id=40,
        channel_domain="home.example",
        response_grant_id=None,
        autocomplete_generation=None,
        expires_at=now + timedelta(minutes=10),
    )
    response = SimpleNamespace(
        id=50,
        interaction_id=10,
        sequence=0,
        response_type=4,
        payload={"content": "private"},
        ephemeral=True,
        message_id=None,
        message_domain=None,
        revision=2,
        deleted_at=None,
    )
    current = SimpleNamespace(
        id=2,
        user_id=30,
        user_domain="home.example",
        interaction_id=10,
        interaction_domain="home.example",
        response_id=50,
        response_domain="home.example",
        revision=2,
        operation="UPDATE",
        event_id=None,
        event_origin_domain=None,
        expires_at=interaction.expires_at,
        attempts=0,
        next_attempt_at=now,
    )
    stale = SimpleNamespace(**(vars(current) | {"id": 1, "revision": 1, "operation": "CREATE"}))
    return interaction, response, current, stale


@pytest.mark.asyncio
async def test_private_dispatch_is_live_only_and_retained_for_sql_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    interaction, response, current, _stale = private_dispatch_objects(now)
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[current]),
        get=AsyncMock(side_effect=[interaction, response]),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    publish = AsyncMock(return_value=True)
    monkeypatch.setattr(interaction_events, "publish_ephemeral", publish)
    redis = SimpleNamespace()

    delivered = await interaction_events.drain_interaction_dispatch_outbox(
        session,
        redis,
    )

    assert delivered == 1
    publish.assert_awaited_once()
    assert publish.await_args.args[:3] == (
        redis,
        "user:home.example:30",
        "INTERACTION_RESPONSE_UPDATE",
    )
    assert current.next_attempt_at == current.expires_at
    session.delete.assert_not_awaited()
    session.commit.assert_awaited_once()
    assert not hasattr(redis, "xadd")
    assert not hasattr(redis, "xrange")


@pytest.mark.asyncio
async def test_private_gateway_replay_selects_latest_exact_sql_revision(postgres_schema) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.bot_models import BotInteraction, BotInteractionResponse, InteractionDispatchOutbox

    for table in ("bot_interactions", "bot_interaction_responses", "interaction_dispatch_outbox"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    now = datetime.now(UTC)
    interaction, response, current, stale = private_dispatch_objects(now)
    async with AsyncSession(bind=postgres_schema) as session:
        session.add(BotInteraction(**vars(interaction)))
        session.add(BotInteractionResponse(**(vars(response) | {"revision": 8})))
        for revision in range(1, 9):
            session.add(
                InteractionDispatchOutbox(
                    **(
                        vars(current)
                        | {
                            "id": revision,
                            "revision": revision,
                            "operation": "UPDATE" if revision > 1 else "CREATE",
                        }
                    )
                )
            )
        session.add(BotInteraction(**(vars(interaction) | {"id": 11})))
        session.add(
            BotInteractionResponse(
                **(
                    vars(response)
                    | {
                        "id": 60,
                        "interaction_id": 11,
                        "revision": 1,
                        "payload": {"content": "later"},
                    }
                )
            )
        )
        session.add(
            InteractionDispatchOutbox(
                **(
                    vars(current)
                    | {
                        "id": 30,
                        "interaction_id": 11,
                        "response_id": 60,
                        "revision": 1,
                        "operation": "CREATE",
                    }
                )
            )
        )
        for identifier, changed in enumerate(
            [
                {"user_id": 31},
                {"user_domain": "foreign.example"},
                {"expires_at": now - timedelta(seconds=1)},
            ],
            start=40,
        ):
            session.add(
                InteractionDispatchOutbox(
                    **(
                        vars(current)
                        | {
                            "id": identifier,
                            "revision": 99,
                        }
                        | changed
                    )
                )
            )
        await session.flush()
        events = await interaction_events.interaction_response_replay_events(
            session,
            user_id=30,
            user_domain="home.example",
            now=now,
            limit=2,
        )
        assert [
            (event["t"], event["d"]["revision"], event["d"]["response_id"]) for event in events
        ] == [
            ("INTERACTION_RESPONSE_UPDATE", "8", "50"),
            ("INTERACTION_RESPONSE_CREATE", "1", "60"),
        ]
        assert all(event["ephemeral"] is True for event in events)
        assert events[1]["d"]["data"]["content"] == "later"


def poll_create() -> PollCreate:
    return PollCreate.model_validate(
        {
            "question": {"text": "Ship it?"},
            "answers": [
                {"poll_media": {"text": "Yes"}},
                {"poll_media": {"text": "Wait"}},
            ],
            "duration": 24,
            "allow_multiselect": False,
            "layout_type": 1,
        }
    )


def test_components_v2_are_valid_only_for_immediate_message_callbacks() -> None:
    interaction = SimpleNamespace(
        status="pending",
        interaction_type="command",
        guild_id=None,
        payload={},
    )
    components = [{"type": 10, "content": "Immediate V2"}]
    callback = interactions.InteractionCallback(
        type=4,
        data={"flags": 1 << 15, "components": components},
    )

    flags, ephemeral = interactions.validate_interaction_callback_type(
        interaction,
        callback,
        SimpleNamespace(),
    )
    message = interactions.interaction_message_from_data(callback.data)

    assert flags == 1 << 15
    assert ephemeral is False
    assert message.flags == 1 << 15
    assert message.components[0].type == 10
    assert message.components[0].content == "Immediate V2"

    deferred = interactions.InteractionCallback(type=5, data={"flags": 1 << 15})
    with pytest.raises(HTTPException) as denied:
        interactions.validate_interaction_callback_type(
            interaction,
            deferred,
            SimpleNamespace(),
        )
    assert denied.value.detail["code"] == "INTERACTION_CALLBACK_FLAGS_INVALID"


@pytest.mark.asyncio
async def test_public_response_exposes_durable_followup_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = SimpleNamespace(id=10, channel_domain="chat.example")
    stored = SimpleNamespace(
        id=30,
        sequence=1,
        revision=2,
        deleted_at=None,
        response_type=4,
        ephemeral=False,
        message_id=40,
        message_domain="chat.example",
        payload={},
    )
    message = SimpleNamespace(deleted_at=None)
    session = SimpleNamespace(get=AsyncMock(return_value=message))
    monkeypatch.setattr(
        interactions,
        "render_message_payload",
        AsyncMock(
            return_value={
                "id": "40",
                "origin_domain": "chat.example",
                "channel_id": "20",
                "channel_domain": "chat.example",
            }
        ),
    )

    rendered = await interactions.interaction_response_payload(session, interaction, stored)

    assert rendered["interaction_id"] == "10"
    assert rendered["response_id"] == "30"
    assert rendered["response_ref"] == "30@chat.example"
    assert rendered["sequence"] == 1
    assert rendered["revision"] == "2"
    assert rendered["ephemeral"] is False


@pytest.mark.asyncio
async def test_public_user_install_poll_finalizes_through_exact_interaction_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = SimpleNamespace(
        id=10,
        channel_id=20,
        channel_domain="chat.example",
    )
    installation = SimpleNamespace(id=50)
    stored = SimpleNamespace(
        id=30,
        sequence=0,
        ephemeral=False,
        message_id=40,
        message_domain="chat.example",
    )
    actor = SimpleNamespace(id=60, origin_domain="apps.example")
    message = SimpleNamespace(
        id=40,
        origin_domain="chat.example",
        author_id=60,
        author_domain="apps.example",
        deleted_at=None,
    )
    poll = SimpleNamespace(finalized_at=None)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[message, poll]),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, installation)),
    )
    monkeypatch.setattr(
        interactions,
        "stored_interaction_response",
        AsyncMock(return_value=stored),
    )
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_encryption",
        AsyncMock(),
    )
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_read_encryption",
        AsyncMock(),
    )
    access = SimpleNamespace(
        channel=SimpleNamespace(id=20, origin_domain="chat.example"),
        guild=None,
    )
    monkeypatch.setattr(
        interactions,
        "interaction_response_channel_access",
        AsyncMock(return_value=access),
    )
    render = AsyncMock(return_value={"id": "40", "origin_domain": "chat.example"})
    publish = AsyncMock()
    projection = AsyncMock(return_value={"id": "40", "response_id": "30"})
    queue_poll_mutation = AsyncMock(return_value=set())
    poll_result_payload = {"id": "41", "origin_domain": "chat.example"}
    ensure_poll_result = AsyncMock(return_value=(poll_result_payload, True))
    monkeypatch.setattr(interactions, "render_message_payload", render)
    monkeypatch.setattr(interactions, "publish_channel_dispatch", publish)
    monkeypatch.setattr(interactions, "interaction_response_payload", projection)
    monkeypatch.setattr(interactions, "queue_dm_poll_mutation", queue_poll_mutation)
    monkeypatch.setattr(interactions, "ensure_poll_result_message", ensure_poll_result)

    redis = SimpleNamespace()
    snowflake = SimpleNamespace()
    settings = SimpleNamespace(domain="chat.example")

    result = await interactions.finalize_interaction_poll(
        10,
        "@original",
        SimpleNamespace(user=actor),
        session,
        redis,
        snowflake,
        settings,
    )

    assert result == {"id": "40", "response_id": "30"}
    assert poll.finalized_at is not None
    queue_poll_mutation.assert_awaited_once_with(
        session,
        settings,
        access,
        actor,
        "dm.poll.finalize",
        message,
        finalized_at=poll.finalized_at,
    )
    ensure_poll_result.assert_awaited_once_with(
        session,
        redis,
        settings,
        snowflake,
        message,
        poll,
    )
    render.assert_awaited_once_with(session, message, viewer=actor)
    publish.assert_any_await(
        redis,
        access,
        "MESSAGE_UPDATE",
        {"id": "40", "origin_domain": "chat.example"},
    )
    publish.assert_any_await(redis, access, "MESSAGE_CREATE", poll_result_payload)
    assert publish.await_count == 2
    projection.assert_awaited_once_with(session, interaction, stored)


@pytest.mark.asyncio
async def test_direct_defer_uses_durable_callback_and_preserves_ephemeral_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = AsyncMock(return_value=Response(status_code=204))
    monkeypatch.setattr(interactions, "callback_interaction", callback)

    result = await interactions.defer_interaction(
        10,
        Response(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        interactions.InteractionDefer(ephemeral=True),
    )

    assert result.status_code == 204
    callback_payload = callback.await_args.args[1]
    assert callback_payload.type == 5
    assert callback_payload.data == {"flags": 64}


@pytest.mark.asyncio
async def test_callback_http_response_modes_match_discord(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimpleNamespace()
    state = interactions.InteractionCallbackState(stored_payload={}, ephemeral=False)
    stored = SimpleNamespace()
    process = AsyncMock(return_value=(context, state, stored))
    wrapper = {
        "interaction": {"id": "10"},
        "resource": {"type": 4, "message": {"id": "30"}},
    }
    render = AsyncMock(return_value=wrapper)
    monkeypatch.setattr(interactions, "process_interaction_callback", process)
    monkeypatch.setattr(interactions, "render_interaction_callback_with_response", render)
    common = (
        10,
        interactions.InteractionCallback(type=4, data={"content": "ready"}),
    )
    dependencies = (
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    omitted_response = Response()
    omitted = await interactions.callback_interaction(
        *common,
        omitted_response,
        *dependencies,
    )
    explicit_response = Response()
    explicit_false = await interactions.callback_interaction(
        *common,
        explicit_response,
        *dependencies,
        False,
    )
    opted_in_response = Response()
    opted_in = await interactions.callback_interaction(
        *common,
        opted_in_response,
        *dependencies,
        True,
    )

    assert isinstance(omitted, Response) and omitted.status_code == 204
    assert isinstance(explicit_false, Response) and explicit_false.status_code == 204
    assert opted_in == wrapper
    assert opted_in_response.status_code == 200
    render.assert_awaited_once_with(context, state, stored)


@pytest.mark.asyncio
async def test_legacy_response_convenience_keeps_message_body_and_creation_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimpleNamespace()
    state = interactions.InteractionCallbackState(stored_payload={}, ephemeral=False)
    stored = SimpleNamespace()
    process = AsyncMock(return_value=(context, state, stored))
    message = {"id": "30", "content": "ready"}
    render = AsyncMock(return_value=message)
    monkeypatch.setattr(interactions, "process_interaction_callback", process)
    monkeypatch.setattr(interactions, "render_interaction_callback", render)
    response = Response()

    result = await interactions.respond_interaction(
        10,
        interactions.InteractionResponse(message={"content": "ready"}),
        response,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    assert result == message
    assert response.status_code == 201
    callback = process.await_args.args[1]
    assert callback.type == 4
    assert callback.data == {"content": "ready"}
    render.assert_awaited_once_with(context, state, stored)


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_type", [4, 5])
async def test_callback_with_response_uses_discord_wrapper(
    callback_type: int,
) -> None:
    context = SimpleNamespace(
        interaction=SimpleNamespace(id=10, interaction_type="command"),
        request=SimpleNamespace(type=callback_type),
    )
    state = interactions.InteractionCallbackState(
        stored_payload={},
        ephemeral=callback_type == 5,
        message_result=({"id": "30", "content": "ready"} if callback_type == 4 else None),
    )
    stored = SimpleNamespace(id=20, ephemeral=callback_type == 5)

    rendered = await interactions.render_interaction_callback_with_response(
        context,
        state,
        stored,
    )

    assert rendered["interaction"] == {
        "id": "10",
        "type": 2,
        "response_message_loading": callback_type == 5,
        "response_message_ephemeral": callback_type == 5,
        **({"response_message_id": "30"} if callback_type == 4 else {}),
    }
    if callback_type == 4:
        assert rendered["resource"] == {
            "type": 4,
            "message": {
                "id": "30",
                "content": "ready",
                "interaction_id": "10",
                "response_id": "20",
            },
        }
    else:
        assert rendered["resource"] == {"type": 5}


def test_deferred_followup_uses_only_original_edit_fields() -> None:
    edit = interactions.deferred_followup_edit(
        interactions.InteractionFollowup(
            message={"content": "ready", "embeds": []},
            ephemeral=True,
        )
    )

    assert edit.content == "ready"
    assert edit.embeds == []

    with pytest.raises(HTTPException) as denied:
        interactions.deferred_followup_edit(
            interactions.InteractionFollowup(
                message={
                    "voice_message": True,
                    "flags": 1 << 13,
                    "attachment_ids": ["41"],
                }
            )
        )
    assert denied.value.detail["code"] == "DEFERRED_FOLLOWUP_EDIT_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owners", "capped"),
    [
        ({"user_install": "20@users.example"}, True),
        (
            {
                "guild_install": "2@guild.example",
                "user_install": "20@users.example",
            },
            False,
        ),
        ({"guild_install": "2@guild.example"}, False),
    ],
)
async def test_followup_limit_applies_only_to_exclusive_user_authority(
    owners: dict[str, str],
    capped: bool,
) -> None:
    interaction = SimpleNamespace(
        id=10,
        payload={
            "_interaction_event_snapshot": {
                "authorizing_integration_owners": owners,
            }
        },
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=5))

    if capped:
        with pytest.raises(HTTPException) as denied:
            await interactions.require_user_install_followup_capacity(
                session,
                interaction,
                SimpleNamespace(),
            )
        assert denied.value.detail["code"] == "USER_INSTALL_FOLLOWUP_LIMIT"
        session.scalar.assert_awaited_once()
    else:
        await interactions.require_user_install_followup_capacity(
            session,
            interaction,
            SimpleNamespace(),
        )
        session.scalar.assert_not_awaited()

    session.scalar.return_value = 4
    await interactions.require_user_install_followup_capacity(
        session, interaction, SimpleNamespace()
    )


@pytest.mark.asyncio
async def test_first_followup_after_message_defer_materializes_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = SimpleNamespace(id=10, status="deferred", callback_type=5)
    installation = SimpleNamespace()
    stored = SimpleNamespace(id=30, response_type=5, ephemeral=True)
    session = SimpleNamespace()
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, installation)),
    )
    load_stored = AsyncMock(return_value=stored)
    monkeypatch.setattr(interactions, "stored_interaction_response", load_stored)
    apply_edit = AsyncMock(return_value={"id": "30", "ephemeral": True})
    monkeypatch.setattr(interactions, "apply_original_interaction_response_edit", apply_edit)
    capacity = AsyncMock()
    monkeypatch.setattr(interactions, "require_user_install_followup_capacity", capacity)

    result = await interactions.create_interaction_followup(
        10,
        interactions.InteractionFollowup(
            message={"content": "ready"},
            # The initial defer's visibility wins over the follow-up request.
            ephemeral=False,
        ),
        Response(),
        SimpleNamespace(),
        session,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
    )

    assert result == {"id": "30", "ephemeral": True}
    load_stored.assert_awaited_once_with(session, 10, sequence=0, for_update=True)
    context = apply_edit.await_args.args[0]
    assert context.request.content == "ready"
    assert context.stored is stored
    capacity.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledgement_deletes_sealed_create_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = SimpleNamespace(
        id=10,
        channel_domain="chat.example",
        callback_type=None,
        acknowledged_at=None,
        responded_at=None,
        status="pending",
        response_message_id=None,
        response_message_domain=None,
    )
    session = SimpleNamespace(
        add=lambda _value: None,
        execute=AsyncMock(),
        commit=AsyncMock(),
    )
    context = SimpleNamespace(
        interaction=interaction,
        request=SimpleNamespace(type=1),
        session=session,
        settings=SimpleNamespace(),
        principal=SimpleNamespace(),
        installation=SimpleNamespace(),
        redis=SimpleNamespace(),
        snowflake=SimpleNamespace(mint=AsyncMock(return_value=30)),
    )
    state = SimpleNamespace(
        stored_payload={},
        ephemeral=False,
        message_ref=None,
        message_body=None,
        updated_ephemeral=None,
        updated_ephemeral_parent=None,
        message_transaction=None,
        private_attachment_ids=[],
        private_attachments_added=[],
        private_attachments_removed=[],
        relay_destinations=set(),
    )
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )

    trace = []
    session.execute.side_effect = lambda statement: trace.append(
        ("delete", statement.compile().params)
    )
    session.commit.side_effect = lambda: trace.append(("commit", None))
    stored = await interactions.persist_interaction_callback(context, state)

    assert stored.interaction_id == interaction.id
    assert interaction.status == "responded"
    statement = str(session.execute.await_args.args[0])
    assert "DELETE FROM interaction_create_dispatch_outbox" in statement
    assert "interaction_create_dispatch_outbox.interaction_id" in statement
    session.commit.assert_awaited_once()

    assert trace == [("delete", {"interaction_id_1": 10}), ("commit", None)]


@pytest.mark.asyncio
async def test_deferred_private_original_materializes_poll_and_becomes_responded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    interaction = SimpleNamespace(
        id=10,
        status="deferred",
        responded_at=None,
        expires_at=now + timedelta(minutes=15),
        channel_id=20,
        channel_domain="chat.example",
        response_message_id=None,
        response_message_domain=None,
    )
    stored = SimpleNamespace(
        id=30,
        response_type=5,
        ephemeral=True,
        message_id=None,
        message_domain=None,
        payload={"flags": 64},
    )
    poll_projection = {
        "question": {"text": "Ship it?"},
        "answers": [
            {"answer_id": 1, "poll_media": {"text": "Yes"}},
            {"answer_id": 2, "poll_media": {"text": "Wait"}},
        ],
        "expiry": (now + timedelta(minutes=15)).isoformat(),
        "allow_multiselect": False,
        "layout_type": 1,
        "finalized_at": None,
        "results": {"is_finalized": False, "answer_counts": []},
    }
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, SimpleNamespace())),
    )
    monkeypatch.setattr(
        interactions,
        "stored_interaction_response",
        AsyncMock(return_value=stored),
    )
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_encryption",
        AsyncMock(),
    )
    create_poll = AsyncMock(return_value=poll_projection)
    monkeypatch.setattr(interactions, "create_interaction_poll", create_poll)
    publish = AsyncMock()
    monkeypatch.setattr(interactions, "publish_interaction_response_event", publish)
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )
    rendered = {"id": "30", "ephemeral": True, "poll": poll_projection}
    monkeypatch.setattr(
        interactions,
        "interaction_response_payload",
        AsyncMock(return_value=rendered),
    )
    invoker = SimpleNamespace(id=40, origin_domain="chat.example")
    monkeypatch.setattr(interactions, "interaction_invoker", AsyncMock(return_value=invoker))
    session = SimpleNamespace(commit=AsyncMock())
    redis = SimpleNamespace()

    result = await interactions.edit_original_interaction_response(
        10,
        interactions.InteractionResponseEdit(poll=poll_create()),
        SimpleNamespace(),
        session,
        redis,
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
    )

    assert result == rendered
    assert interaction.status == "responded"
    assert interaction.responded_at is not None
    assert stored.payload["flags"] == 64
    assert stored.payload["poll"] == poll_projection
    create_poll.assert_awaited_once()
    session.commit.assert_awaited_once()
    publish.assert_awaited_once_with(redis, interaction, stored, "UPDATE")


@pytest.mark.asyncio
async def test_poll_cannot_be_added_after_original_response_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = SimpleNamespace(id=10, status="responded", responded_at=datetime.now(UTC))
    stored = SimpleNamespace(
        response_type=4,
        ephemeral=True,
        message_id=None,
        message_domain=None,
    )
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, SimpleNamespace())),
    )
    monkeypatch.setattr(
        interactions,
        "stored_interaction_response",
        AsyncMock(return_value=stored),
    )
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_encryption",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as denied:
        await interactions.edit_original_interaction_response(
            10,
            interactions.InteractionResponseEdit(poll=poll_create()),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="chat.example"),
        )

    assert denied.value.status_code == 409
    assert denied.value.detail["code"] == "POLL_EDIT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_private_poll_lookup_fails_closed_for_every_user_but_invoker(postgres_schema) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.bot_models import BotInteraction, BotInteractionPoll, BotInteractionResponse

    for table in ("bot_interactions", "bot_interaction_responses", "bot_interaction_polls"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    now = datetime.now(UTC)
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        interaction = BotInteraction(
            id=10, user_id=30, user_domain="chat.example", expires_at=now + timedelta(minutes=5)
        )
        response = BotInteractionResponse(id=20, interaction_id=10, ephemeral=True)
        poll = BotInteractionPoll(
            response_id=20, question={"text": "Choose"}, expires_at=now + timedelta(minutes=5)
        )
        session.add_all([interaction, response, poll])
        await session.flush()
        assert await interactions.invoking_user_interaction_poll(
            session, 10, 20, SimpleNamespace(id=30, origin_domain="chat.example"), for_update=True
        ) == (
            interaction,
            response,
            poll,
        )
        for identifier, domain in [(99, "chat.example"), (30, "foreign.example")]:
            with pytest.raises(HTTPException) as denied:
                await interactions.invoking_user_interaction_poll(
                    session,
                    10,
                    20,
                    SimpleNamespace(id=identifier, origin_domain=domain),
                    for_update=True,
                )
            assert denied.value.status_code == 404
            assert denied.value.detail == {"code": "INTERACTION_POLL_NOT_FOUND"}
