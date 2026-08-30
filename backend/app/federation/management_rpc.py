from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.json_limits import FEDERATION_JSON_LIMITS, strict_json_loads, validate_json_tree
from app.core.settings import Settings
from app.federation.network import FederationNetworkError

MANAGEMENT_RPC_DEADLINE_SECONDS = 15
MANAGEMENT_RPC_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

SignedRequest = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ManagementRPCErrorContract:
    unavailable: Mapping[str, object]
    failed: Mapping[str, object]
    invalid_response: Mapping[str, object]
    invalid_binding: Mapping[str, object] | None = None


def validate_management_request_shape(
    issued_at: int,
    deadline: int,
    *,
    label: str,
) -> None:
    if deadline <= issued_at or deadline - issued_at > MANAGEMENT_RPC_DEADLINE_SECONDS:
        raise ValueError(f"{label} request deadline is invalid")


def validate_management_json(value: object, *, label: str) -> None:
    validate_json_tree(
        value,
        limits=FEDERATION_JSON_LIMITS,
        label=label,
        allow_floats=True,
    )


def decode_management_json(content: bytes, *, label: str) -> object:
    try:
        return strict_json_loads(
            content,
            limits=FEDERATION_JSON_LIMITS,
            label=label,
            allow_floats=True,
        )
    except ValueError as exc:
        raise FederationNetworkError(f"{label} is invalid") from exc


def _upstream_error(
    status_code: int,
    content: bytes,
    response_headers: Mapping[str, str],
    *,
    label: str,
    fallback: Mapping[str, object],
) -> HTTPException:
    try:
        raw_error = decode_management_json(content, label=f"{label} error response")
    except FederationNetworkError:
        raw_error = None
    detail: object = dict(fallback)
    if isinstance(raw_error, dict) and "detail" in raw_error:
        detail = raw_error["detail"]
    retry_after = response_headers.get("Retry-After")
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


async def request_management_rpc[ResultT: BaseModel](
    session: AsyncSession,
    settings: Settings,
    *,
    authority_domain: str,
    path: str,
    payload: dict[str, Any],
    response_model: type[ResultT],
    response_matches: Callable[[ResultT], bool],
    label: str,
    errors: ManagementRPCErrorContract,
    send: SignedRequest,
) -> ResultT:
    """Send and validate one signed, request-bound management RPC."""

    try:
        upstream = await send(
            session,
            settings,
            "POST",
            authority_domain,
            path,
            payload=payload,
            request_timeout=MANAGEMENT_RPC_DEADLINE_SECONDS,
            max_response_bytes=MANAGEMENT_RPC_MAX_RESPONSE_BYTES,
            allow_json_floats=True,
        )
    except (FederationNetworkError, RuntimeError):
        raise HTTPException(status_code=503, detail=dict(errors.unavailable)) from None
    if not 200 <= upstream.status_code < 300:
        raise _upstream_error(
            upstream.status_code,
            upstream.content,
            upstream.headers,
            label=label,
            fallback=errors.failed,
        )
    try:
        raw = decode_management_json(upstream.content, label=f"{label} response")
        result = response_model.model_validate(raw)
    except (FederationNetworkError, ValueError):
        raise HTTPException(status_code=502, detail=dict(errors.invalid_response)) from None
    if not response_matches(result):
        detail = errors.invalid_binding or errors.invalid_response
        raise HTTPException(status_code=502, detail=dict(detail))
    return result


async def consume_management_request_once(
    redis: Redis,
    settings: Settings,
    *,
    origin: str,
    namespace: str,
    request_id: str,
    issued_at: int,
    deadline: int,
    now: int,
    expired_code: str,
    replayed_code: str,
) -> None:
    """Validate the signed request window and atomically consume its replay id."""

    if issued_at > now + settings.federation_clock_skew_seconds or deadline <= now:
        raise HTTPException(status_code=401, detail={"code": expired_code})
    replay_ttl = max(1, deadline - now + settings.federation_clock_skew_seconds)
    accepted = await redis.set(
        f"federation:{namespace}:{origin}:{request_id}",
        "1",
        ex=replay_ttl,
        nx=True,
    )
    if not accepted:
        raise HTTPException(status_code=409, detail={"code": replayed_code})
