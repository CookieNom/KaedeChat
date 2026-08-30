from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.custom_emojis import custom_emoji_refs
from app.chat.custom_stickers import sticker_item_payload, validate_sticker_items
from app.chat.expression_authorization import (
    EXPRESSION_USE_AUTHORIZATION_EVENT,
    ExpressionOperation,
    ExpressionUseAuthorization,
    canonical_expression_emoji_tokens,
    expression_actor_intent_resources,
)
from app.core.federation import canonical_json
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.bot_models import ApplicationEmoji, BotApplication
from app.db.models import Emoji, Guild, Sticker, User
from app.federation.actor_intents import (
    FederatedActorIntent,
    build_human_actor_intent,
    consume_actor_intent_nonce,
    validate_worker_actor_intent,
)
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError, decode_federation_response_json
from app.federation.replication import profile_from_user
from app.federation.security import validated_event_envelope

ExpressionAuthorizationMap = dict[str, dict[str, object]]


def _expression_projection_by_authority(
    emoji_tokens: list[str],
    sticker_refs: list[EntityRef],
    *,
    default_domain: str,
) -> dict[str, tuple[list[str], list[str]]]:
    projections: dict[str, tuple[list[str], list[str]]] = {}
    for token in canonical_expression_emoji_tokens(emoji_tokens):
        references = custom_emoji_refs(token)
        if len(references) != 1:
            raise ValueError("expression emoji token has ambiguous authority")
        authority = references[0].origin_domain
        tokens, stickers = projections.setdefault(authority, ([], []))
        tokens.append(token)
    for reference in sticker_refs:
        sticker_id, authority = reference.resolve(default_domain)
        tokens, stickers = projections.setdefault(authority, ([], []))
        stickers.append(f"{sticker_id}@{authority}")
    for tokens, stickers in projections.values():
        tokens.sort()
        stickers.sort()
        if len(stickers) != len(set(stickers)):
            raise ValueError("expression sticker references must be unique")
    return dict(sorted(projections.items()))


def _authorization_nonce(
    actor: User,
    raw_intent: dict[str, object] | None,
) -> str:
    if actor.account_type == "human":
        if raw_intent is not None:
            raise ValueError("human expression request cannot carry a worker intent")
        return secrets.token_urlsafe(24)
    intent = FederatedActorIntent.model_validate(raw_intent)
    nonce = intent.resources.get("authorization_nonce")
    if nonce is None:
        raise ValueError("expression worker intent omits its authorization nonce")
    return nonce


async def acquire_expression_use_authorizations(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    *,
    application_ref: tuple[int, str] | None,
    actor_intents: dict[str, dict[str, object]],
    target_guild_ref: str,
    target_channel_ref: str,
    target_message_ref: str | None,
    operation: ExpressionOperation,
    operation_id: str,
    emoji_tokens: list[str],
    sticker_refs: list[EntityRef],
) -> tuple[ExpressionAuthorizationMap, list[dict[str, object]]]:
    """Acquire and verify every S receipt before routing its metadata to T."""

    target_domain = EntityRef(target_channel_ref).domain
    if target_domain is None or EntityRef(target_guild_ref).domain != target_domain:
        raise ValueError("expression target references are inconsistent")
    projections = _expression_projection_by_authority(
        emoji_tokens,
        sticker_refs,
        default_domain=target_domain,
    )
    if actor.account_type == "bot" and application_ref is None:
        raise ValueError("bot expression request requires an application")
    if actor.account_type == "human" and application_ref is not None:
        raise ValueError("human expression request cannot claim an application")
    if set(actor_intents) - set(projections):
        raise ValueError("expression request contains an unrelated actor intent")

    proofs: ExpressionAuthorizationMap = {}
    sticker_items: list[dict[str, object]] = []
    for source_authority, (source_tokens, source_sticker_refs) in projections.items():
        raw_intent = actor_intents.get(source_authority)
        nonce = _authorization_nonce(actor, raw_intent)
        intent_resources = expression_actor_intent_resources(
            source_authority=source_authority,
            target_guild_ref=target_guild_ref,
            target_channel_ref=target_channel_ref,
            target_message_ref=target_message_ref,
            operation=operation,
            operation_id=operation_id,
            emoji_tokens=source_tokens,
            sticker_refs=source_sticker_refs,
            authorization_nonce=nonce,
        )
        if actor.account_type == "human" and source_authority != settings.domain:
            raw_intent = await build_human_actor_intent(
                session,
                settings,
                actor,
                action="expression.use.authorize",
                audience=source_authority,
                resources=intent_resources,
            )
        request_body: dict[str, object] = {
            "actor": profile_from_user(actor),
            "application_ref": (
                f"{application_ref[0]}@{application_ref[1]}"
                if application_ref is not None
                else None
            ),
            "actor_intent": raw_intent,
            "source_authority": source_authority,
            "target_guild_ref": target_guild_ref,
            "target_channel_ref": target_channel_ref,
            "target_message_ref": target_message_ref,
            "operation": operation,
            "operation_id": operation_id,
            "emoji_tokens": source_tokens,
            "sticker_refs": source_sticker_refs,
            "nonce": nonce,
        }
        if source_authority == settings.domain:
            from app.api.expressions import (
                ExpressionUseAuthorizeRequest,
                issue_expression_use_authorization,
            )

            request = ExpressionUseAuthorizeRequest.model_validate(request_body)
            application = (
                await session.get(BotApplication, application_ref)
                if application_ref is not None
                else None
            )
            if actor.account_type == "human":
                if actor.origin_domain != settings.domain:
                    raise HTTPException(
                        status_code=403,
                        detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"},
                    )
            else:
                if application is None or application_ref is None or raw_intent is None:
                    raise HTTPException(
                        status_code=403,
                        detail={"code": "BOT_ACTOR_INTENT_INVALID"},
                    )
                try:
                    await validate_worker_actor_intent(
                        session,
                        settings.domain,
                        raw_intent,
                        expected_action="expression.use.authorize",
                        expected_audience=settings.domain,
                        expected_application_ref=application_ref,
                        expected_actor_ref=(actor.id, actor.origin_domain),
                        expected_resources=intent_resources,
                        runtime_target_domain=target_domain,
                        redis=redis,
                    )
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=403,
                        detail={"code": "BOT_ACTOR_INTENT_INVALID"},
                    ) from exc
            proof = await issue_expression_use_authorization(
                request,
                actor,
                application,
                session,
                redis,
                settings,
            )
        else:
            try:
                response = await signed_request(
                    session,
                    settings,
                    "POST",
                    source_authority,
                    "/_kaede/v1/expressions/authorize",
                    payload=request_body,
                    max_response_bytes=256 * 1024,
                    guild_context=True,
                )
            except (FederationNetworkError, RuntimeError):
                raise HTTPException(
                    status_code=503,
                    detail={"code": "EXPRESSION_AUTHORIZATION_UNAVAILABLE"},
                ) from None
            if response.status_code != 200:
                status_code = (
                    response.status_code
                    if response.status_code in {400, 403, 404, 409, 429}
                    else 503
                )
                raise HTTPException(
                    status_code=status_code,
                    detail={"code": "EXPRESSION_AUTHORIZATION_REJECTED"},
                )
            try:
                raw_proof = decode_federation_response_json(
                    response,
                    max_response_bytes=256 * 1024,
                )
            except FederationNetworkError:
                raise HTTPException(
                    status_code=502,
                    detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
                ) from None
            if not isinstance(raw_proof, dict):
                raise HTTPException(
                    status_code=502,
                    detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
                )
            proof = {str(key): value for key, value in raw_proof.items()}

        raw_content = proof.get("content")
        try:
            receipt = ExpressionUseAuthorization.model_validate(raw_content)
            expected_sticker_refs = [
                f"{item['id']}@{item['origin_domain']}" for item in receipt.sticker_items
            ]
            if expected_sticker_refs != source_sticker_refs:
                raise ValueError("expression receipt changed its sticker projection")
            await validated_expression_use_authorization(
                session,
                redis,
                settings,
                proof,
                source_authority=source_authority,
                requester_ref=f"{actor.id}@{actor.origin_domain}",
                requester_type=actor.account_type,
                application_ref=(
                    f"{application_ref[0]}@{application_ref[1]}"
                    if application_ref is not None
                    else None
                ),
                target_guild_ref=target_guild_ref,
                target_channel_ref=target_channel_ref,
                target_message_ref=target_message_ref,
                operation=operation,
                operation_id=operation_id,
                expected_emoji_tokens=source_tokens,
                expected_sticker_items=receipt.sticker_items,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "EXPRESSION_AUTHORIZATION_INVALID"},
            ) from exc
        proofs[source_authority] = proof
        sticker_items.extend(receipt.sticker_items)
    sticker_items.sort(key=lambda item: f"{item['id']}@{item['origin_domain']}")
    return proofs, validate_sticker_items(sticker_items, maximum=9)


async def validate_expression_authorization_map(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    raw_proofs: ExpressionAuthorizationMap,
    *,
    requester_ref: str,
    requester_type: Literal["human", "bot"],
    application_ref: str | None,
    target_guild_ref: str,
    target_channel_ref: str,
    target_message_ref: str | None,
    operation: ExpressionOperation,
    operation_id: str,
    emoji_tokens: list[str],
    sticker_items: list[dict[str, object]],
) -> tuple[list[str], list[dict[str, object]]]:
    """Verify an exact proof partition at T and return attested projections."""

    sticker_items = validate_sticker_items(sticker_items, maximum=9)
    sticker_refs = [EntityRef(f"{item['id']}@{item['origin_domain']}") for item in sticker_items]
    projections = _expression_projection_by_authority(
        emoji_tokens,
        sticker_refs,
        default_domain=cast(str, EntityRef(target_channel_ref).domain),
    )
    if set(raw_proofs) != set(projections):
        raise ValueError("expression authorization authorities are incomplete")
    attested_tokens: list[str] = []
    attested_items: list[dict[str, object]] = []
    for source_authority, (source_tokens, source_sticker_refs) in projections.items():
        source_items = [item for item in sticker_items if item["origin_domain"] == source_authority]
        if [
            f"{item['id']}@{item['origin_domain']}" for item in source_items
        ] != source_sticker_refs:
            raise ValueError("expression sticker proof partition is invalid")
        await validated_expression_use_authorization(
            session,
            redis,
            settings,
            raw_proofs[source_authority],
            source_authority=source_authority,
            requester_ref=requester_ref,
            requester_type=requester_type,
            application_ref=application_ref,
            target_guild_ref=target_guild_ref,
            target_channel_ref=target_channel_ref,
            target_message_ref=target_message_ref,
            operation=operation,
            operation_id=operation_id,
            expected_emoji_tokens=source_tokens,
            expected_sticker_items=source_items,
        )
        attested_tokens.extend(source_tokens)
        attested_items.extend(source_items)
    return sorted(attested_tokens), attested_items


async def validate_attested_expression_target(
    session: AsyncSession,
    actor: User,
    guild: Guild,
    actor_permissions: int,
    emoji_tokens: list[str],
    sticker_items: list[dict[str, object]],
) -> None:
    """Recheck T-owned metadata and T's external-expression permissions."""

    permissions = Permission(actor_permissions)
    for token in emoji_tokens:
        references = custom_emoji_refs(token)
        if len(references) != 1:
            raise ValueError("attested expression emoji is invalid")
        reference = references[0]
        if reference.origin_domain != guild.origin_domain:
            if not permissions & Permission.USE_EXTERNAL_EMOJIS:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "USE_EXTERNAL_EMOJIS_REQUIRED"},
                )
            continue
        application_emoji = None
        if actor.account_type == "bot":
            application_emoji = await session.scalar(
                select(ApplicationEmoji)
                .join(
                    BotApplication,
                    (BotApplication.id == ApplicationEmoji.application_id)
                    & (BotApplication.origin_domain == ApplicationEmoji.application_domain),
                )
                .where(
                    ApplicationEmoji.id == reference.id,
                    ApplicationEmoji.application_domain == reference.origin_domain,
                    BotApplication.bot_user_id == actor.id,
                    BotApplication.bot_user_domain == actor.origin_domain,
                )
            )
        if application_emoji is not None:
            if (
                not application_emoji.available
                or application_emoji.name != reference.name
                or bool(application_emoji.animated) != reference.animated
            ):
                raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
            continue
        emoji = await session.get(Emoji, (reference.id, reference.origin_domain))
        if (
            emoji is None
            or not emoji.available
            or emoji.name != reference.name
            or bool(emoji.animated) != reference.animated
        ):
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
        if (emoji.guild_id, emoji.guild_domain) != (
            guild.id,
            guild.origin_domain,
        ) and not permissions & Permission.USE_EXTERNAL_EMOJIS:
            raise HTTPException(
                status_code=403,
                detail={"code": "USE_EXTERNAL_EMOJIS_REQUIRED"},
            )

    for item in validate_sticker_items(sticker_items, maximum=9):
        if item["origin_domain"] != guild.origin_domain:
            if not permissions & Permission.USE_EXTERNAL_STICKERS:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "USE_EXTERNAL_STICKERS_REQUIRED"},
                )
            continue
        sticker = await session.get(Sticker, (int(str(item["id"])), guild.origin_domain))
        if sticker is None or sticker_item_payload(sticker) != item:
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_INVALID"})
        if (sticker.guild_id, sticker.guild_domain) != (
            guild.id,
            guild.origin_domain,
        ) and not permissions & Permission.USE_EXTERNAL_STICKERS:
            raise HTTPException(
                status_code=403,
                detail={"code": "USE_EXTERNAL_STICKERS_REQUIRED"},
            )


async def validated_expression_use_authorization(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    raw_proof: object,
    *,
    source_authority: str,
    requester_ref: str,
    requester_type: str,
    application_ref: str | None,
    target_guild_ref: str,
    target_channel_ref: str,
    target_message_ref: str | None,
    operation: ExpressionOperation,
    operation_id: str,
    expected_emoji_tokens: list[str],
    expected_sticker_items: list[dict[str, object]],
    now: datetime | None = None,
) -> ExpressionUseAuthorization:
    """Verify one exact S-signed expression receipt and consume it at T."""

    if not isinstance(raw_proof, dict):
        raise ValueError("expression authorization is not an event envelope")
    envelope = await validated_event_envelope(
        session,
        settings,
        source_authority,
        raw_proof,
        allow_authority_attested_actor=True,
    )
    authorization = ExpressionUseAuthorization.model_validate(envelope.content)
    requester = EntityRef(requester_ref)
    current = now or datetime.now(UTC)
    if (
        envelope.type != EXPRESSION_USE_AUTHORIZATION_EVENT
        or envelope.context
        != {
            "source_authority": source_authority,
            "target_channel_ref": target_channel_ref,
        }
        or requester.domain is None
        or (envelope.actor.id, envelope.actor.domain) != (str(requester.id), requester.domain)
        or authorization.source_authority != source_authority
        or authorization.requester_ref != requester_ref
        or authorization.requester_type != requester_type
        or authorization.application_ref != application_ref
        or authorization.target_guild_ref != target_guild_ref
        or authorization.target_channel_ref != target_channel_ref
        or authorization.target_message_ref != target_message_ref
        or authorization.operation != operation
        or authorization.operation_id != operation_id
        or authorization.emoji_tokens != expected_emoji_tokens
        or authorization.sticker_items != expected_sticker_items
        or authorization.issued_at
        > current + timedelta(seconds=settings.federation_clock_skew_seconds)
        or authorization.expires_at <= current
        or abs((envelope.ts / 1000) - authorization.issued_at.timestamp()) > 1
    ):
        raise ValueError("expression authorization binding is invalid")
    await consume_actor_intent_nonce(
        redis,
        authority_domain=source_authority,
        intent_kind="expression-source",
        action=operation,
        actor_ref=(requester.id, requester.domain),
        audience=EntityRef(target_channel_ref).domain or "",
        nonce=authorization.nonce,
        expires_at=int(authorization.expires_at.timestamp()),
        fingerprint=canonical_json(authorization.model_dump(mode="json")),
        now=current,
    )
    return authorization
