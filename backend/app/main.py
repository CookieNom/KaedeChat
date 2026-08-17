from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from redis.asyncio import Redis
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.admin import router as admin_router
from app.api.admin_portal import router as admin_portal_router
from app.api.applications import federation_router as bot_install_federation_router
from app.api.applications import router as applications_router
from app.api.auth import router as auth_router
from app.api.bot_federation import router as bot_federation_router
from app.api.bot_gateway import router as bot_gateway_router
from app.api.bots import router as bots_router
from app.api.calls import router as calls_router
from app.api.channels import router as channels_router
from app.api.dms import router as dms_router
from app.api.e2ee import router as e2ee_router
from app.api.federation import router as federation_router
from app.api.gifs import router as gifs_router
from app.api.guild_lifecycle import router as guild_lifecycle_router
from app.api.guilds import router as guilds_router
from app.api.interactions import federation_router as interaction_federation_router
from app.api.interactions import router as interactions_router
from app.api.invites import router as invites_router
from app.api.link_previews import router as link_previews_router
from app.api.management import router as management_router
from app.api.media import router as media_router
from app.api.moderation import router as moderation_router
from app.api.push import relay_router as push_relay_router
from app.api.push import router as push_router
from app.api.relationships import router as relationships_router
from app.api.search import router as search_router
from app.api.users import router as users_router
from app.api.voice import router as voice_router
from app.api.webhooks import router as webhooks_router
from app.core.errors import (
    API_ERROR_RESPONSES,
    apply_response_cache_policy,
    error_response,
    http_exception_response,
    validation_exception_response,
)
from app.core.json_limits import strict_json_loads
from app.core.logging import configure_logging
from app.core.metrics import render_metrics
from app.core.settings import get_settings
from app.core.snowflake import SnowflakeGenerator, WorkerLease
from app.db.session import create_engine_and_sessionmaker
from app.federation.delivery import FederationOutboxCapacityExceeded
from app.federation.identity_storage import FederationIdentityQuotaExceeded
from app.federation.network import FederationInstanceQuotaExceeded
from app.federation.security import bounded_request_body
from app.voice.background import voice_coordinator

settings = get_settings()
configure_logging(settings.log_level)
log = structlog.get_logger()
TRACE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def safe_trace_id(candidate: str | None) -> str:
    if candidate is not None and TRACE_ID_RE.fullmatch(candidate):
        return candidate
    return secrets.token_hex(16)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.dragonfly_url.get_secret_value(), decode_responses=True)
    lease: WorkerLease | None = None
    voice_task: asyncio.Task[None] | None = None
    try:
        lease = await WorkerLease.acquire(redis)
        lease.start_heartbeat()
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.redis = redis
        app.state.snowflake = SnowflakeGenerator(lease)
        if settings.voice_enabled:
            voice_task = asyncio.create_task(
                voice_coordinator(redis, sessionmaker, settings),
                name="voice-coordinator",
            )
        log.info("service_started", service="api", domain=settings.domain)
        yield
    finally:
        if voice_task is not None:
            voice_task.cancel()
            await asyncio.gather(voice_task, return_exceptions=True)
        if lease is not None:
            await lease.close()
        await redis.aclose()
        await engine.dispose()
        log.info("service_stopped", service="api")


app = FastAPI(
    title="Kaede Chat API",
    version="0.1.0",
    default_response_class=JSONResponse,
    responses=API_ERROR_RESPONSES,
    lifespan=lifespan,
)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(admin_portal_router)
app.include_router(applications_router)
app.include_router(bot_install_federation_router)
app.include_router(bots_router)
app.include_router(bot_federation_router)
app.include_router(bot_gateway_router)
app.include_router(federation_router)
app.include_router(users_router)
app.include_router(voice_router)
app.include_router(guilds_router)
app.include_router(gifs_router)
app.include_router(guild_lifecycle_router)
app.include_router(channels_router)
app.include_router(calls_router)
app.include_router(dms_router)
app.include_router(e2ee_router)
app.include_router(invites_router)
app.include_router(interactions_router)
app.include_router(interaction_federation_router)
app.include_router(link_previews_router)
app.include_router(management_router)
app.include_router(media_router)
app.include_router(moderation_router)
app.include_router(push_router)
app.include_router(push_relay_router)
app.include_router(relationships_router)
app.include_router(search_router)
app.include_router(webhooks_router)


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return http_exception_response(request, exc)


@app.exception_handler(RequestValidationError)
async def handle_validation_exception(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return validation_exception_response(request, exc)


@app.exception_handler(FederationIdentityQuotaExceeded)
@app.exception_handler(FederationInstanceQuotaExceeded)
@app.exception_handler(FederationOutboxCapacityExceeded)
async def handle_federation_capacity_exception(
    request: Request,
    exc: (
        FederationIdentityQuotaExceeded
        | FederationInstanceQuotaExceeded
        | FederationOutboxCapacityExceeded
    ),
) -> JSONResponse:
    return http_exception_response(
        request,
        HTTPException(
            status_code=507,
            detail=exc.detail(federation=request.url.path.startswith("/_kaede/")),
        ),
    )


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_request_error", error_type=type(exc).__name__)
    return error_response(
        request,
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        message=(
            "Kaede could not complete this request because of a server error. "
            "Try again; if it continues, provide the error reference to support."
        ),
    )


@app.middleware("http")
async def trace_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started_at = time.monotonic()
    trace_id = safe_trace_id(request.headers.get("X-Kaede-Trace-Id"))
    request.state.trace_id = trace_id
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    try:
        mutation = request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }
        is_federation = request.url.path.startswith("/_kaede/")
        is_api = request.url.path.startswith("/api/v1/")
        response: Response
        if mutation and (is_federation or is_api):
            try:
                body = await bounded_request_body(
                    request,
                    max_bytes=1024 * 1024 if is_federation else 2 * 1024 * 1024,
                    too_large_code=(
                        "KAED_FED_BATCH_TOO_LARGE" if is_federation else "REQUEST_BODY_TOO_LARGE"
                    ),
                )
                if is_federation and body:
                    request.state.federation_json = strict_json_loads(body)
            except ValueError:
                response = error_response(
                    request,
                    status_code=400,
                    code="KAED_FED_INVALID_JSON",
                    message="The federated request is not valid, unambiguous JSON.",
                )
            except StarletteHTTPException as exc:
                response = http_exception_response(request, exc)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["X-Kaede-Trace-Id"] = trace_id
        apply_response_cache_policy(request.url.path, response)
        route = request.scope.get("route")
        route_template = getattr(route, "path", "unmatched")
        log.info(
            "request_completed",
            method=request.method,
            route=route_template,
            status_code=response.status_code,
            duration_ms=round((time.monotonic() - started_at) * 1000, 2),
        )
        return response
    finally:
        structlog.contextvars.clear_contextvars()


@app.get("/health/live", include_in_schema=False)
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def ready(request: Request) -> JSONResponse:
    try:
        async with request.app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await request.app.state.redis.ping()
        if not request.app.state.snowflake.available:
            raise RuntimeError("snowflake worker lease is unavailable")
    except Exception:
        log.exception("readiness_failed")
        return error_response(
            request,
            status_code=503,
            code="SERVICE_NOT_READY",
            message="Kaede is still starting or one of its required services is unavailable.",
        )
    return JSONResponse({"status": "ready"})


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> PlainTextResponse:
    if not settings.metrics_enabled:
        return PlainTextResponse("Metrics are disabled on this server.\n", status_code=404)
    return PlainTextResponse(
        await render_metrics(request.app.state.redis, request.app.state.sessionmaker),
        media_type="text/plain; version=0.0.4",
    )
