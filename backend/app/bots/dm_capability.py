from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.bots.application_contract import SUPPORTED_APPLICATION_SCOPES
from app.bots.runtime_control import (
    ApplicationRuntimeSnapshot,
    application_runtime_snapshot_fingerprint,
)
from app.bots.target_contract import NonnegativeDecimal
from app.core.base64url import encode_base64url
from app.core.bot_intents import SUPPORTED_BOT_INTENTS
from app.core.federation import canonical_json
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import DOMAIN_RE, Settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import MAX_SNOWFLAKE, EntityRef
from app.db.bot_models import (
    BotApplication,
    BotApplicationRuntimeHighwater,
    BotApplicationTarget,
    BotDMCapability,
    BotDMCapabilityHighwater,
    BotToken,
)
from app.db.models import Channel, DMConversation, User
from app.federation.client import signed_request
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
)
from app.federation.replication import profile_from_user
from app.federation.schemas import EventEnvelope, RemoteUserProfile
from app.federation.security import validated_event_envelope

BOT_DM_CAPABILITY_EVENT = "bot.dm.installation-capability"
BOT_DM_CAPABILITY_LEASE = timedelta(minutes=10)
# Settings cap federation skew at 15 minutes. Keep terminal fences for an
# additional safety margin so every previously valid active lease has expired
# before its anti-resurrection ledger can be collected.
BOT_DM_CAPABILITY_HIGHWATER_RETENTION = timedelta(minutes=30)
MAX_BOT_DM_CAPABILITY_HIGHWATERS_PER_INSTALLATION_AUTHORITY = 100_000
RUNTIME_FENCE_SESSION_KEY = "bot_dm_runtime_fences"


class BotDMCapabilityAuthorityUnavailable(RuntimeError):
    """The exact installation authority could not refresh a short lease."""


class BotDMCapabilityProofInvalid(RuntimeError):
    """The installation authority returned a malformed or swapped proof."""


class BotDMCapabilitySourceRejected(PermissionError):
    """The authenticated installation authority definitively rejected a grant."""


@dataclass(frozen=True, slots=True)
class BotDMCapabilityFenceExpectation:
    """Exact local projection lineage that one rejection is allowed to fence."""

    grant_id: str
    source_kind: str
    source_installation_ref: tuple[int, str]
    application_ref: tuple[int, str]
    bot_user_ref: tuple[int, str]
    guild_ref: tuple[int | None, str | None]
    installing_user_ref: tuple[int | None, str | None]
    target_user_ref: tuple[int, str]
    pair_key: str
    authority_domain: str
    conversation_ref: tuple[int | None, str | None]
    revision: int
    proof_fingerprint: bytes

    def matches(self, row: BotDMCapability) -> bool:
        return (
            row.grant_id == self.grant_id
            and row.source_kind == self.source_kind
            and (row.source_installation_id, row.source_installation_domain)
            == self.source_installation_ref
            and (row.application_id, row.application_domain) == self.application_ref
            and (row.bot_user_id, row.bot_user_domain) == self.bot_user_ref
            and (row.guild_id, row.guild_domain) == self.guild_ref
            and (row.installing_user_id, row.installing_user_domain) == self.installing_user_ref
            and (row.target_user_id, row.target_user_domain) == self.target_user_ref
            and row.pair_key == self.pair_key
            and row.authority_domain == self.authority_domain
            and (row.conversation_id, row.conversation_domain) == self.conversation_ref
            and row.revision == self.revision
            and row.proof_fingerprint == self.proof_fingerprint
        )


def _positive_decimal(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
        or int(value) > MAX_SNOWFLAKE
    ):
        raise ValueError("capability identity/revision must be a positive decimal string")
    return value


def _canonical_domain(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("capability domain must be a string")
    normalized = value.rstrip(".").lower()
    if normalized != value or DOMAIN_RE.fullmatch(value) is None:
        raise ValueError("capability domain must be canonical")
    return value


PositiveDecimal = Annotated[str, BeforeValidator(_positive_decimal)]
FederationDomain = Annotated[str, BeforeValidator(_canonical_domain)]


def _canonical_ref(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("capability reference must be a string")
    parsed = EntityRef(value)
    if parsed.domain is None or str(parsed) != value:
        raise ValueError("capability references must be qualified and canonical")
    return value


QualifiedRef = Annotated[str, BeforeValidator(_canonical_ref)]


class BotDMCapabilityPayload(UnambiguousInputModel):
    """The exact, short-lived installation fact signed by authority B."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    grant_id: str = Field(pattern=r"^kbdg_[A-Za-z0-9_-]{43}$")
    source_kind: Literal["guild", "user"]
    installation_ref: QualifiedRef
    application_ref: QualifiedRef
    bot_user_ref: QualifiedRef
    guild_ref: QualifiedRef | None = None
    installing_user_ref: QualifiedRef | None = None
    target_user_ref: QualifiedRef
    pair_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_domain: FederationDomain
    scopes: list[str] = Field(max_length=64)
    intents: list[str] = Field(max_length=32)
    channel_restrictions: list[QualifiedRef] = Field(max_length=500)
    e2ee_mode: Literal["disabled", "participant"]
    installation_revision: PositiveDecimal
    runtime_manifest_generation: PositiveDecimal
    runtime_revocation_generation: PositiveDecimal
    target_access_revocation_generation: NonnegativeDecimal
    runtime_snapshot_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: PositiveDecimal
    status: Literal["active", "suspended", "revoked"]
    expires_at_ms: PositiveDecimal

    @field_validator("scopes")
    @classmethod
    def known_scopes(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or not set(value) <= SUPPORTED_APPLICATION_SCOPES:
            raise ValueError("capability scopes must be sorted, unique, and supported")
        if "dm.send" not in value:
            raise ValueError("a DM capability requires dm.send")
        return value

    @field_validator("intents")
    @classmethod
    def known_intents(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or not set(value) <= SUPPORTED_BOT_INTENTS:
            raise ValueError("capability intents must be sorted, unique, and supported")
        if "direct_messages" not in value:
            raise ValueError("a DM capability requires the direct_messages intent")
        return value

    @field_validator("channel_restrictions")
    @classmethod
    def unique_restrictions(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("capability channel restrictions must be sorted and unique")
        return value

    @model_validator(mode="after")
    def coherent_authorities(self) -> BotDMCapabilityPayload:
        installation = EntityRef(self.installation_ref)
        application = EntityRef(self.application_ref)
        bot = EntityRef(self.bot_user_ref)
        if self.grant_id != bot_dm_grant_id(
            self.source_kind,
            self.installation_ref,
            self.application_ref,
            self.bot_user_ref,
            self.pair_key,
            self.authority_domain,
        ):
            raise ValueError("capability grant id does not match its immutable identity")
        if self.source_kind == "guild":
            if self.guild_ref is None or self.installing_user_ref is not None:
                raise ValueError("guild capability source context is invalid")
            if installation.domain != EntityRef(self.guild_ref).domain:
                raise ValueError("installation and guild authorities must match")
        elif self.installing_user_ref is None or self.guild_ref is not None:
            raise ValueError("user capability source context is invalid")
        elif installation.domain != EntityRef(self.installing_user_ref).domain:
            raise ValueError("installation and user authorities must match")
        elif self.installing_user_ref != self.target_user_ref:
            raise ValueError("a user installation may authorize only its installing user")
        if application.domain != bot.domain:
            raise ValueError("application and bot authorities must match")
        if int(self.expires_at_ms) <= 0:
            raise ValueError("capability expiry must be positive")
        return self

    @property
    def installation(self) -> EntityRef:
        return EntityRef(self.installation_ref)

    @property
    def application(self) -> EntityRef:
        return EntityRef(self.application_ref)

    @property
    def bot_user(self) -> EntityRef:
        return EntityRef(self.bot_user_ref)

    @property
    def guild(self) -> EntityRef | None:
        return EntityRef(self.guild_ref) if self.guild_ref is not None else None

    @property
    def installing_user(self) -> EntityRef | None:
        return EntityRef(self.installing_user_ref) if self.installing_user_ref is not None else None

    @property
    def target_user(self) -> EntityRef:
        return EntityRef(self.target_user_ref)

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(int(self.expires_at_ms) / 1000, tz=UTC)


class BotDMCapabilityAttestRequest(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["guild", "user"]
    installation_ref: QualifiedRef
    application_ref: QualifiedRef
    bot_user_ref: QualifiedRef
    target: RemoteUserProfile
    pair_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_domain: FederationDomain
    source_runtime_proof: dict[str, object]
    authority_runtime_proof: dict[str, object]
    refresh_grant_id: str | None = Field(
        default=None,
        pattern=r"^kbdg_[A-Za-z0-9_-]{43}$",
    )


class BotDMCapabilityApplyRequest(UnambiguousInputModel):
    """A's relay of B's original proof to the bound conversation authority C."""

    model_config = ConfigDict(extra="forbid")

    proof: dict[str, object]
    runtime_proof: dict[str, object]
    grant_id: str = Field(pattern=r"^kbdg_[A-Za-z0-9_-]{43}$")
    revision: PositiveDecimal
    conversation_ref: QualifiedRef


class BotDMCapabilityValidateRequest(UnambiguousInputModel):
    """C's freshness check against the B-authoritative grant ledger."""

    model_config = ConfigDict(extra="forbid")

    proof: dict[str, object]
    grant_id: str = Field(pattern=r"^kbdg_[A-Za-z0-9_-]{43}$")
    revision: PositiveDecimal


def bot_dm_grant_id(
    source_kind: Literal["guild", "user"],
    installation_ref: str,
    application_ref: str,
    bot_user_ref: str,
    pair_key: str,
    authority_domain: str,
) -> str:
    digest = hashlib.sha256(
        (
            "kaede-bot-dm-capability-v1\n"
            f"{source_kind}\n{installation_ref}\n{application_ref}\n{bot_user_ref}\n"
            f"{pair_key}\n{authority_domain}"
        ).encode()
    ).digest()
    return "kbdg_" + encode_base64url(digest)


def capability_fingerprint(payload: BotDMCapabilityPayload) -> bytes:
    return hashlib.sha256(canonical_json(payload.model_dump(mode="json"))).digest()


def capability_authorization_fingerprint(payload: BotDMCapabilityPayload) -> bytes:
    """Hash durable authorization state, excluding the renewable lease."""

    rendered = payload.model_dump(mode="json")
    rendered.pop("revision")
    rendered.pop("expires_at_ms")
    return hashlib.sha256(canonical_json(rendered)).digest()


def capability_identity_fingerprint(payload: BotDMCapabilityPayload) -> bytes:
    """Hash only the immutable grant identity, independent of authorization."""

    return hashlib.sha256(
        canonical_json(
            {
                "grant_id": payload.grant_id,
                "source_kind": payload.source_kind,
                "installation_ref": payload.installation_ref,
                "application_ref": payload.application_ref,
                "bot_user_ref": payload.bot_user_ref,
                "guild_ref": payload.guild_ref,
                "installing_user_ref": payload.installing_user_ref,
                "target_user_ref": payload.target_user_ref,
                "pair_key": payload.pair_key,
                "authority_domain": payload.authority_domain,
            }
        )
    ).digest()


def bot_dm_capability_fence_expectation(
    row: BotDMCapability,
) -> BotDMCapabilityFenceExpectation:
    return BotDMCapabilityFenceExpectation(
        grant_id=row.grant_id,
        source_kind=row.source_kind,
        source_installation_ref=(
            row.source_installation_id,
            row.source_installation_domain,
        ),
        application_ref=(row.application_id, row.application_domain),
        bot_user_ref=(row.bot_user_id, row.bot_user_domain),
        guild_ref=(row.guild_id, row.guild_domain),
        installing_user_ref=(row.installing_user_id, row.installing_user_domain),
        target_user_ref=(row.target_user_id, row.target_user_domain),
        pair_key=row.pair_key,
        authority_domain=row.authority_domain,
        conversation_ref=(row.conversation_id, row.conversation_domain),
        revision=row.revision,
        proof_fingerprint=row.proof_fingerprint,
    )


def consume_bot_dm_runtime_fence(
    session: AsyncSession,
    expectation: BotDMCapabilityFenceExpectation,
) -> datetime | None:
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return None
    fences = info.get(RUNTIME_FENCE_SESSION_KEY)
    if not isinstance(fences, dict):
        return None
    value = fences.pop(expectation, None)
    return value if isinstance(value, datetime) else None


async def lock_bot_dm_capability_projection(
    session: AsyncSession,
    expectation: BotDMCapabilityFenceExpectation,
    *,
    require_active: bool = False,
    now: datetime | None = None,
) -> BotDMCapability | None:
    """Serialize one exact grant lineage before acquiring its mutable row."""

    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"bot-dm-capability:{expectation.grant_id}", 0)
            )
        )
    )
    row = await session.scalar(
        select(BotDMCapability)
        .where(BotDMCapability.grant_id == expectation.grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or not expectation.matches(row):
        return None
    if require_active and not capability_is_active(row, now=now):
        return None
    return row


async def _revoke_bot_dm_capability_tokens(
    session: AsyncSession,
    capability_ids: Iterable[int],
    *,
    revoked_at: datetime,
) -> None:
    ids = tuple(sorted(set(capability_ids)))
    if not ids:
        return
    await session.execute(
        update(BotToken)
        .where(
            BotToken.dm_capability_id.in_(ids),
            BotToken.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )


async def fence_bot_dm_capability(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    expectation: BotDMCapabilityFenceExpectation,
    *,
    now: datetime | None = None,
) -> tuple[bool, list[Channel]]:
    """Atomically suspend one exact active lineage and tear down all live access."""

    fenced_at = now or datetime.now(UTC)
    row = await lock_bot_dm_capability_projection(
        session,
        expectation,
        require_active=True,
        now=fenced_at,
    )
    if row is None:
        return False, []
    row.status = "suspended"
    row.revoked_at = fenced_at
    await _revoke_bot_dm_capability_tokens(
        session,
        (row.id,),
        revoked_at=fenced_at,
    )

    from app.bots.e2ee import revoke_bot_e2ee_access

    channels = await revoke_bot_e2ee_access(
        session,
        redis,
        settings,
        dm_capability_ids=(row.id,),
        now=fenced_at,
    )
    return True, channels


async def fence_bot_dm_capabilities_for_pair(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    first: User,
    second: User,
    *,
    now: datetime | None = None,
) -> list[Channel]:
    """Fence local-authority bot/human grants while their privacy pair is locked."""

    account_types = {first.account_type, second.account_type}
    if account_types != {"bot", "human"}:
        return []
    bot = first if first.account_type == "bot" else second
    human = second if bot is first else first
    current_time = now or datetime.now(UTC)
    observed_rows = list(
        await session.scalars(
            select(BotDMCapability)
            .where(
                BotDMCapability.authority_domain == settings.domain,
                BotDMCapability.bot_user_id == bot.id,
                BotDMCapability.bot_user_domain == bot.origin_domain,
                BotDMCapability.target_user_id == human.id,
                BotDMCapability.target_user_domain == human.origin_domain,
                BotDMCapability.status.in_(("active", "suspended")),
            )
            .order_by(BotDMCapability.grant_id)
        )
    )
    channels: list[Channel] = []
    known_channels: set[tuple[int, str]] = set()
    for expectation in map(bot_dm_capability_fence_expectation, observed_rows):
        locked = await lock_bot_dm_capability_projection(session, expectation)
        if locked is None:
            continue
        if locked.status == "suspended":
            # A newer privacy edge must remain distinguishable from an
            # in-request runtime suspension after that runtime transaction
            # commits and releases its locks.
            locked.revoked_at = current_time
            continue
        _fenced, affected = await fence_bot_dm_capability(
            session,
            redis,
            settings,
            expectation,
            now=current_time,
        )
        for channel in affected:
            key = (channel.id, channel.origin_domain)
            if key not in known_channels:
                known_channels.add(key)
                channels.append(channel)
    return channels


def require_capability_runtime_binding(
    capability: BotDMCapabilityPayload,
    runtime_envelope: EventEnvelope,
    runtime_snapshot: ApplicationRuntimeSnapshot,
) -> None:
    """Require B's grant to bind the exact A-signed authority-target proof."""

    if (
        runtime_snapshot.application_id != str(capability.application.id)
        or runtime_snapshot.application_domain != capability.application.domain
        or runtime_snapshot.bot_user_id != str(capability.bot_user.id)
        or runtime_snapshot.bot_user_domain != capability.bot_user.domain
        or runtime_snapshot.target_domain != capability.authority_domain
        or int(runtime_snapshot.manifest_generation) != int(capability.runtime_manifest_generation)
        or int(runtime_snapshot.revocation_generation)
        != int(capability.runtime_revocation_generation)
        or int(runtime_snapshot.access_revocation_generation)
        != int(capability.target_access_revocation_generation)
        or application_runtime_snapshot_fingerprint(runtime_snapshot).hex()
        != capability.runtime_snapshot_fingerprint
    ):
        raise ValueError("bot DM capability runtime proof binding is invalid")


def authority_attested_bot_dm_capability(
    event_type: str,
    content: object,
    *,
    expected_authority: str,
    actor: tuple[str, str],
) -> bool:
    if event_type != BOT_DM_CAPABILITY_EVENT or not isinstance(content, dict):
        return False
    try:
        capability = BotDMCapabilityPayload.model_validate(content)
    except ValueError:
        return False
    bot = capability.bot_user
    return (
        capability.installation.domain == expected_authority and (str(bot.id), bot.domain) == actor
    )


async def validated_bot_dm_capability_proof(
    session: AsyncSession,
    settings: Settings,
    raw_proof: object,
    *,
    expected_installation_authority: str | None = None,
) -> tuple[EventEnvelope, BotDMCapabilityPayload]:
    """Verify B's original envelope without re-attesting it at A or C."""

    try:
        preliminary = EventEnvelope.model_validate(raw_proof)
        payload = BotDMCapabilityPayload.model_validate(preliminary.content)
    except ValueError as exc:
        raise ValueError("bot DM capability proof is malformed") from exc
    installation_authority = payload.installation.domain
    if (
        preliminary.type != BOT_DM_CAPABILITY_EVENT
        or preliminary.origin != installation_authority
        or (
            expected_installation_authority is not None
            and installation_authority != expected_installation_authority
        )
        or not authority_attested_bot_dm_capability(
            preliminary.type,
            preliminary.content,
            expected_authority=installation_authority,
            actor=(preliminary.actor.id, preliminary.actor.domain),
        )
    ):
        raise ValueError("bot DM capability proof authority is invalid")
    envelope = await validated_event_envelope(
        session,
        settings,
        installation_authority,
        raw_proof,
        allow_authority_attested_actor=True,
    )
    if payload.status == "active" and payload.expires_at <= datetime.now(UTC):
        raise ValueError("bot DM capability proof expired")
    signed_at = datetime.fromtimestamp(envelope.ts / 1000, tz=UTC)
    if payload.expires_at > signed_at + BOT_DM_CAPABILITY_LEASE:
        raise ValueError("bot DM capability proof lease is too long")
    return envelope, payload


async def validated_bot_dm_capability_context(
    session: AsyncSession,
    settings: Settings,
    raw_proof: object,
    *,
    relay_domain: str,
    bot: User,
    target: User,
    pair_key: str,
    authority_domain: str,
    refresh_grant_id: str | None = None,
) -> tuple[EventEnvelope, BotDMCapabilityPayload]:
    """Verify the original install-authority proof and its exact DM binding."""

    envelope, payload = await validated_bot_dm_capability_proof(
        session,
        settings,
        raw_proof,
    )
    expected_bot = EntityRef(f"{bot.id}@{bot.origin_domain}")
    expected_target = EntityRef(f"{target.id}@{target.origin_domain}")
    if (
        relay_domain not in {payload.application.domain, payload.authority_domain}
        or payload.bot_user != expected_bot
        or payload.target_user != expected_target
        or payload.pair_key != pair_key
        or payload.authority_domain != authority_domain
    ):
        raise ValueError("bot DM capability does not match the requested conversation")
    return envelope, payload


async def apply_bot_dm_capability(
    session: AsyncSession,
    snowflake: SnowflakeGenerator | None,
    proof: EventEnvelope,
    payload: BotDMCapabilityPayload,
    *,
    conversation: DMConversation | None = None,
    runtime_admitted: bool = False,
    admit_fenced_projection: bool = False,
    preserve_local_fence: bool = False,
    now: datetime | None = None,
) -> tuple[BotDMCapability | None, bool]:
    """Apply one monotonic proof; equal-generation equivocation is rejected."""

    if payload.status == "active" and not runtime_admitted:
        raise ValueError("active bot DM capability lacks current application runtime proof")
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"bot-dm-capability:{payload.grant_id}", 0)
            )
        )
    )
    fingerprint = capability_fingerprint(payload)
    identity_fingerprint = capability_identity_fingerprint(payload)
    authorization_fingerprint = capability_authorization_fingerprint(payload)
    current_time = now or datetime.now(UTC)
    highwater_expires_at = current_time + BOT_DM_CAPABILITY_HIGHWATER_RETENTION
    highwater = await session.scalar(
        select(BotDMCapabilityHighwater)
        .where(
            BotDMCapabilityHighwater.grant_id == payload.grant_id,
            BotDMCapabilityHighwater.expires_at > current_time,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    incoming_revision = int(payload.revision)
    if highwater is None:
        # Different grants use different locks, so serialize quota accounting
        # at the installation authority before pruning, counting, and adding.
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        f"bot-dm-capability-highwater-authority:{payload.installation.domain}",
                        0,
                    )
                )
            )
        )
        await session.execute(
            delete(BotDMCapabilityHighwater).where(
                BotDMCapabilityHighwater.installation_authority_domain
                == payload.installation.domain,
                BotDMCapabilityHighwater.expires_at <= current_time,
            )
        )
        live_rows = int(
            await session.scalar(
                select(func.count())
                .select_from(BotDMCapabilityHighwater)
                .where(
                    BotDMCapabilityHighwater.installation_authority_domain
                    == payload.installation.domain,
                    BotDMCapabilityHighwater.expires_at > current_time,
                )
            )
            or 0
        )
        if live_rows >= MAX_BOT_DM_CAPABILITY_HIGHWATERS_PER_INSTALLATION_AUTHORITY:
            raise ValueError("bot DM capability high-water quota exceeded")
        highwater = BotDMCapabilityHighwater(
            grant_id=payload.grant_id,
            installation_authority_domain=payload.installation.domain,
            identity_fingerprint=identity_fingerprint,
            revision=incoming_revision,
            authorization_fingerprint=authorization_fingerprint,
            status=payload.status,
            expires_at=highwater_expires_at,
        )
        session.add(highwater)
    else:
        if (
            highwater.installation_authority_domain != payload.installation.domain
            or highwater.identity_fingerprint != identity_fingerprint
        ):
            raise ValueError("bot DM capability changed immutable grant identity")
        if incoming_revision < highwater.revision:
            row = await session.scalar(
                select(BotDMCapability)
                .where(BotDMCapability.grant_id == payload.grant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            return row, False
        if incoming_revision == highwater.revision:
            if highwater.authorization_fingerprint != authorization_fingerprint:
                raise ValueError("bot DM capability revision equivocated")
        else:
            highwater.revision = incoming_revision
            highwater.authorization_fingerprint = authorization_fingerprint
            highwater.status = payload.status
        highwater.expires_at = highwater_expires_at

    row = await session.scalar(
        select(BotDMCapability)
        .where(BotDMCapability.grant_id == payload.grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    preserve_fence = bool(
        preserve_local_fence and row is not None and not capability_is_active(row, now=current_time)
    )
    if row is not None:
        if incoming_revision < row.revision:
            return row, False
        if incoming_revision == row.revision:
            try:
                current_proof = EventEnvelope.model_validate(row.proof)
                current_payload = BotDMCapabilityPayload.model_validate(current_proof.content)
            except ValueError as exc:
                raise ValueError("stored bot DM capability proof is malformed") from exc
            if capability_authorization_fingerprint(
                current_payload
            ) != capability_authorization_fingerprint(payload):
                raise ValueError("bot DM capability revision equivocated")
            conversation_changed = False
            if conversation is not None:
                expected = (conversation.id, conversation.origin_domain)
                stored = (row.conversation_id, row.conversation_domain)
                if stored not in {(None, None), expected}:
                    raise ValueError("bot DM capability was replayed to another conversation")
                if stored == (None, None):
                    row.conversation_id, row.conversation_domain = expected
                    conversation_changed = True
            admission_revision = int(row.admission_revision or row.revision)
            locally_reactivated = (
                admit_fenced_projection
                and incoming_revision > admission_revision
                and payload.status == "active"
                and (row.status != "active" or row.revoked_at is not None)
            )
            if locally_reactivated:
                row.status = "active"
                row.revoked_at = None
                row.admission_revision = incoming_revision
                row.target_access_revocation_generation = int(
                    payload.target_access_revocation_generation
                )
            # A lease renewal is not an authorization revision. Keeping the
            # revision stable preserves exact call/media bindings while the
            # newer B-signed proof extends the fail-closed expiry. Delayed or
            # replayed shorter leases are authenticated no-ops.
            if payload.expires_at <= row.expires_at:
                return row, conversation_changed or locally_reactivated
            row.proof_fingerprint = fingerprint
            row.proof = proof.model_dump(mode="json")
            row.expires_at = payload.expires_at
            return row, True

    if row is None and payload.status != "active":
        # A terminal B proof is itself the durable fence. It must survive even
        # when the active grant, users, or conversation never materialized on
        # this instance.
        return None, True
    if row is None:
        bot = await session.get(User, (payload.bot_user.id, payload.bot_user.domain))
        target = await session.get(User, (payload.target_user.id, payload.target_user.domain))
        if (
            bot is None
            or bot.account_type != "bot"
            or target is None
            or target.account_type != "human"
        ):
            raise ValueError("bot DM capability identities are not materialized correctly")
    if conversation is not None:
        if (
            conversation.pair_key != payload.pair_key
            or conversation.authority_domain != payload.authority_domain
            or conversation.origin_domain != payload.authority_domain
        ):
            raise ValueError("bot DM capability does not match the conversation")
        conversation_ref: tuple[int | None, str | None] = (
            conversation.id,
            conversation.origin_domain,
        )
    elif row is not None:
        # Lease refreshes are attested before the DM authority is contacted.
        # Preserve an existing authority binding instead of temporarily
        # unbinding the grant while applying the newer source proof at A/C.
        conversation_ref = (row.conversation_id, row.conversation_domain)
    else:
        conversation_ref = (None, None)
    guild = payload.guild
    installing_user = payload.installing_user
    values = {
        "grant_id": payload.grant_id,
        "source_kind": payload.source_kind,
        "source_installation_id": payload.installation.id,
        "source_installation_domain": payload.installation.domain,
        "application_id": payload.application.id,
        "application_domain": payload.application.domain,
        "bot_user_id": payload.bot_user.id,
        "bot_user_domain": payload.bot_user.domain,
        "guild_id": guild.id if guild is not None else None,
        "guild_domain": guild.domain if guild is not None else None,
        "installing_user_id": (installing_user.id if installing_user is not None else None),
        "installing_user_domain": (installing_user.domain if installing_user is not None else None),
        "target_user_id": payload.target_user.id,
        "target_user_domain": payload.target_user.domain,
        "pair_key": payload.pair_key,
        "authority_domain": payload.authority_domain,
        "conversation_id": conversation_ref[0],
        "conversation_domain": conversation_ref[1],
        "granted_scopes": list(payload.scopes),
        "granted_intents": list(payload.intents),
        "channel_restrictions": list(payload.channel_restrictions),
        "e2ee_mode": payload.e2ee_mode,
        "revision": incoming_revision,
        "admission_revision": (
            int(row.admission_revision or row.revision)
            if preserve_fence and row is not None
            else incoming_revision
        ),
        "target_access_revocation_generation": int(payload.target_access_revocation_generation),
        "status": "suspended" if preserve_fence else payload.status,
        "proof_fingerprint": fingerprint,
        "proof": proof.model_dump(mode="json"),
        "expires_at": payload.expires_at,
        "revoked_at": (
            row.revoked_at or current_time
            if preserve_fence and row is not None
            else current_time
            if payload.status != "active"
            else None
        ),
    }
    if row is None:
        if snowflake is None:
            raise RuntimeError("new bot DM capability requires a snowflake generator")
        row = BotDMCapability(
            id=await snowflake.mint(),
            **values,
        )
        session.add(row)
    else:
        immutable = (
            row.source_kind,
            row.source_installation_id,
            row.source_installation_domain,
            row.application_id,
            row.application_domain,
            row.bot_user_id,
            row.bot_user_domain,
            row.guild_id,
            row.guild_domain,
            row.installing_user_id,
            row.installing_user_domain,
            row.target_user_id,
            row.target_user_domain,
            row.pair_key,
            row.authority_domain,
        )
        incoming_immutable = (
            payload.source_kind,
            payload.installation.id,
            payload.installation.domain,
            payload.application.id,
            payload.application.domain,
            payload.bot_user.id,
            payload.bot_user.domain,
            guild.id if guild is not None else None,
            guild.domain if guild is not None else None,
            installing_user.id if installing_user is not None else None,
            installing_user.domain if installing_user is not None else None,
            payload.target_user.id,
            payload.target_user.domain,
            payload.pair_key,
            payload.authority_domain,
        )
        if immutable != incoming_immutable:
            raise ValueError("bot DM capability changed immutable grant identity")
        for field, value in values.items():
            setattr(row, field, value)
        if row.status != "active":
            await _revoke_bot_dm_capability_tokens(
                session,
                (row.id,),
                revoked_at=row.revoked_at or current_time,
            )
    return row, True


def capability_is_active(row: BotDMCapability, *, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    return row.status == "active" and row.revoked_at is None and row.expires_at > current


def usable_dm_capability(
    *,
    at: datetime | ColumnElement[datetime] | None = None,
) -> ColumnElement[bool]:
    """Require an active, unrevoked, unexpired DM capability in SQL.

    The default uses the database transaction timestamp. Callers that already
    capture a Python timestamp can pass it explicitly so every related check
    retains the same time boundary.
    """

    return and_(
        BotDMCapability.status == "active",
        BotDMCapability.revoked_at.is_(None),
        BotDMCapability.expires_at > (func.now() if at is None else at),
    )


def _stored_bot_dm_capability_proof(
    row: BotDMCapability,
) -> tuple[EventEnvelope, BotDMCapabilityPayload]:
    try:
        envelope = EventEnvelope.model_validate(row.proof)
        payload = BotDMCapabilityPayload.model_validate(envelope.content)
    except ValueError as exc:
        raise ValueError("stored bot DM capability proof is malformed") from exc
    guild = payload.guild
    installing_user = payload.installing_user
    expected_row = (
        payload.grant_id,
        payload.source_kind,
        payload.installation.id,
        payload.installation.domain,
        payload.application.id,
        payload.application.domain,
        payload.bot_user.id,
        payload.bot_user.domain,
        guild.id if guild is not None else None,
        guild.domain if guild is not None else None,
        installing_user.id if installing_user is not None else None,
        installing_user.domain if installing_user is not None else None,
        payload.target_user.id,
        payload.target_user.domain,
        payload.pair_key,
        payload.authority_domain,
        list(payload.scopes),
        list(payload.intents),
        list(payload.channel_restrictions),
        payload.e2ee_mode,
        int(payload.revision),
        int(payload.target_access_revocation_generation),
        payload.expires_at,
    )
    stored_row = (
        row.grant_id,
        row.source_kind,
        row.source_installation_id,
        row.source_installation_domain,
        row.application_id,
        row.application_domain,
        row.bot_user_id,
        row.bot_user_domain,
        row.guild_id,
        row.guild_domain,
        row.installing_user_id,
        row.installing_user_domain,
        row.target_user_id,
        row.target_user_domain,
        row.pair_key,
        row.authority_domain,
        list(row.granted_scopes),
        list(row.granted_intents),
        list(row.channel_restrictions),
        row.e2ee_mode,
        row.revision,
        int(row.target_access_revocation_generation or 0),
        row.expires_at,
    )
    if (
        stored_row != expected_row
        or envelope.type != BOT_DM_CAPABILITY_EVENT
        or envelope.origin != payload.installation.domain
        or (envelope.actor.id, envelope.actor.domain)
        != (str(payload.bot_user.id), payload.bot_user.domain)
        or row.proof_fingerprint != capability_fingerprint(payload)
    ):
        raise ValueError("stored bot DM capability proof does not match its projection")
    return envelope, payload


def stored_source_bot_dm_capability_payload(
    row: BotDMCapability,
    *,
    now: datetime | None = None,
) -> BotDMCapabilityPayload:
    """Recover B's signed source ledger independently of C's admission fence."""

    _envelope, payload = _stored_bot_dm_capability_proof(row)
    if payload.status != "active" or payload.expires_at <= (now or datetime.now(UTC)):
        raise ValueError("stored bot DM capability source proof is inactive")
    return payload


def stored_bot_dm_capability_payload(
    row: BotDMCapability,
    *,
    now: datetime | None = None,
) -> BotDMCapabilityPayload:
    """Recover the exact B-signed payload represented by an active C projection."""

    _envelope, payload = _stored_bot_dm_capability_proof(row)
    if (
        row.status != payload.status
        or not capability_is_active(row, now=now)
        or payload.status != "active"
        or payload.expires_at <= (now or datetime.now(UTC))
        or row.conversation_id is None
        or row.conversation_domain != row.authority_domain
    ):
        raise ValueError("stored bot DM capability proof does not match its projection")
    return payload


def dm_capability_runtime_ready(
    application: BotApplication,
    target: BotApplicationTarget | None,
    row: BotDMCapability,
    *,
    target_domain: str,
    now: datetime | None = None,
) -> bool:
    """Join C's live B grant to the exact current A runtime projection."""

    try:
        payload = stored_bot_dm_capability_payload(row, now=now)
    except ValueError:
        return False
    expected = (
        int(payload.runtime_manifest_generation),
        int(payload.runtime_revocation_generation),
        int(payload.target_access_revocation_generation),
    )
    return bool(
        target is not None
        and (application.id, application.origin_domain)
        == (payload.application.id, payload.application.domain)
        and (application.bot_user_id, application.bot_user_domain)
        == (payload.bot_user.id, payload.bot_user.domain)
        and application.status == "active"
        and (application.manifest_generation, application.revocation_generation) == expected[:2]
        and target.target_domain == target_domain == payload.authority_domain
        and (target.application_id, target.application_domain)
        == (application.id, application.origin_domain)
        and (
            int(target.runtime_manifest_generation or 0),
            int(target.runtime_revocation_generation or 0),
            int(target.runtime_access_revocation_generation or 0),
        )
        == expected
        and target.runtime_status == "active"
        and target.runtime_target_allowed is True
        and target.runtime_fingerprint is not None
        and target.runtime_fingerprint.hex() == payload.runtime_snapshot_fingerprint
        and row.authority_domain == target_domain
        and row.conversation_domain == target_domain
    )


async def require_stored_capability_runtime(
    session: AsyncSession,
    settings: Settings,
    capability: BotDMCapabilityPayload,
) -> None:
    """Join an active B proof to the exact durable A target high-water."""

    if settings.domain == capability.application.domain:
        target_domain = capability.authority_domain
    elif settings.domain == capability.authority_domain:
        target_domain = settings.domain
    else:
        raise PermissionError("bot DM capability was delivered outside its runtime authorities")
    expected = (
        int(capability.runtime_manifest_generation),
        int(capability.runtime_revocation_generation),
        int(capability.target_access_revocation_generation),
    )
    target = await session.scalar(
        select(BotApplicationTarget)
        .where(
            BotApplicationTarget.application_id == capability.application.id,
            BotApplicationTarget.application_domain == capability.application.domain,
            BotApplicationTarget.target_domain == target_domain,
        )
        .with_for_update()
    )
    if target is not None:
        stored = (
            int(target.runtime_manifest_generation or 0),
            int(target.runtime_revocation_generation or 0),
            int(target.runtime_access_revocation_generation or 0),
        )
        ready = (
            stored == expected
            and target.runtime_status == "active"
            and bool(target.runtime_target_allowed)
            and target.runtime_fingerprint is not None
            and target.runtime_fingerprint.hex() == capability.runtime_snapshot_fingerprint
        )
        application = await session.get(
            BotApplication,
            (capability.application.id, capability.application.domain),
        )
        if application is not None and (
            application.manifest_generation > expected[0]
            or application.revocation_generation > expected[1]
            or application.status != "active"
        ):
            ready = False
        if ready:
            return
        raise PermissionError("bot DM capability runtime proof is stale")
    pending = await session.scalar(
        select(BotApplicationRuntimeHighwater)
        .where(
            BotApplicationRuntimeHighwater.application_id == capability.application.id,
            BotApplicationRuntimeHighwater.application_domain == capability.application.domain,
            BotApplicationRuntimeHighwater.target_domain == target_domain,
        )
        .with_for_update()
    )
    if (
        pending is None
        or pending.expires_at <= datetime.now(UTC)
        or (
            (
                pending.manifest_generation,
                pending.revocation_generation,
                pending.access_revocation_generation,
            )
            != expected
            or pending.status != "active"
            or not pending.target_allowed
            or pending.runtime_fingerprint.hex() != capability.runtime_snapshot_fingerprint
            or (pending.bot_user_id, pending.bot_user_domain)
            != (capability.bot_user.id, capability.bot_user.domain)
        )
    ):
        raise PermissionError("bot DM capability has no current application runtime proof")


async def validate_bot_dm_capability_at_source(
    session: AsyncSession,
    settings: Settings,
    proof: EventEnvelope,
    capability: BotDMCapabilityPayload,
) -> None:
    """Fail closed unless B still recognizes this exact active proof."""

    source_domain = capability.installation.domain
    if source_domain is None:  # Qualified by the capability schema; keeps type narrowing local.
        raise ValueError("bot DM capability installation authority is missing")
    if source_domain == settings.domain:
        row = await session.scalar(
            select(BotDMCapability)
            .where(BotDMCapability.grant_id == capability.grant_id)
            .execution_options(populate_existing=True)
        )
        try:
            source_payload = (
                stored_source_bot_dm_capability_payload(row) if row is not None else None
            )
        except ValueError as exc:
            raise BotDMCapabilitySourceRejected(
                "installation authority no longer recognizes the DM grant"
            ) from exc
        if source_payload != capability:
            raise BotDMCapabilitySourceRejected(
                "installation authority no longer recognizes the DM grant"
            )
        return
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            source_domain,
            "/_kaede/v1/bot-dm/capabilities/validate",
            payload=BotDMCapabilityValidateRequest(
                proof=proof.model_dump(mode="json"),
                grant_id=capability.grant_id,
                revision=str(capability.revision),
            ).model_dump(mode="json"),
            request_timeout=8,
            max_response_bytes=16 * 1024,
        )
    except FederationNetworkError as exc:
        raise BotDMCapabilityAuthorityUnavailable("installation authority is unavailable") from exc
    if response.status_code in {429} or response.status_code >= 500:
        raise BotDMCapabilityAuthorityUnavailable("installation authority is unavailable")
    if response.status_code == 403:
        raise BotDMCapabilitySourceRejected(
            "installation authority no longer recognizes the DM grant"
        )
    if response.status_code in {401, 404, 409}:
        raise BotDMCapabilityProofInvalid(
            f"installation authority returned an ambiguous status ({response.status_code})"
        )
    if response.status_code != 200:
        raise BotDMCapabilityProofInvalid(
            f"installation authority returned an invalid status ({response.status_code})"
        )
    try:
        rendered = decode_federation_response_json(response, max_response_bytes=16 * 1024)
    except FederationNetworkError as exc:
        raise BotDMCapabilityProofInvalid(
            "installation authority returned an invalid DM grant validation"
        ) from exc
    if rendered != {
        "grant_id": capability.grant_id,
        "revision": str(capability.revision),
        "expires_at_ms": str(int(capability.expires_at.timestamp() * 1000)),
    }:
        raise BotDMCapabilityProofInvalid(
            "installation authority changed the DM grant validation identity"
        )


async def fence_bot_dm_capability_projections(
    session: AsyncSession,
    *,
    application_ref: tuple[int, str],
    now: datetime | None = None,
) -> list[BotDMCapability]:
    """Fail closed on locally stored proofs after an application access edge.

    Only the installation authority may sign a new capability revision, so a
    relay or conversation authority must not forge a revoked proof. Marking
    the local projection suspended blocks every capability query immediately.
    Active re-admission is separately gated by the exact A-signed target
    runtime proof. This projection change is therefore idempotent: replaying a
    disabled snapshot must not invent a second causal generation.
    """

    observed_rows = list(
        await session.scalars(
            select(BotDMCapability)
            .where(
                BotDMCapability.application_id == application_ref[0],
                BotDMCapability.application_domain == application_ref[1],
                BotDMCapability.status == "active",
                BotDMCapability.revoked_at.is_(None),
            )
            .order_by(BotDMCapability.grant_id)
            .execution_options(populate_existing=True)
        )
    )
    fenced_at = now or datetime.now(UTC)
    runtime_fences: dict[BotDMCapabilityFenceExpectation, datetime] | None = None
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        runtime_fences = info.setdefault(RUNTIME_FENCE_SESSION_KEY, {})
    fenced_rows: list[BotDMCapability] = []
    for expectation in map(bot_dm_capability_fence_expectation, observed_rows):
        row = await lock_bot_dm_capability_projection(
            session,
            expectation,
            require_active=True,
            now=fenced_at,
        )
        if row is None:
            continue
        row.status = "suspended"
        row.revoked_at = fenced_at
        fenced_rows.append(row)
        if runtime_fences is not None:
            runtime_fences[expectation] = fenced_at
    await _revoke_bot_dm_capability_tokens(
        session,
        (row.id for row in fenced_rows),
        revoked_at=fenced_at,
    )
    return fenced_rows


def next_capability_expiry() -> datetime:
    return datetime.now(UTC) + BOT_DM_CAPABILITY_LEASE


async def fetch_bot_dm_capability_proof(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    redis: Redis,
    *,
    source_kind: Literal["guild", "user"],
    installation_ref: EntityRef,
    application_ref: EntityRef,
    bot: User,
    target: User,
    pair_key: str,
    authority_domain: str,
    source_runtime_proof: EventEnvelope,
    authority_runtime_proof: EventEnvelope,
    refresh_grant_id: str | None = None,
) -> tuple[EventEnvelope, BotDMCapabilityPayload, BotDMCapability]:
    """Ask B for its original proof and retain it unchanged at app home A."""

    installation_domain = installation_ref.domain
    if installation_domain is None:
        raise ValueError("remote bot DM grants require a qualified installation reference")
    if application_ref.domain != settings.domain or bot.origin_domain != settings.domain:
        raise ValueError("only the application home may request a bot DM grant")
    request = BotDMCapabilityAttestRequest(
        source_kind=source_kind,
        installation_ref=str(installation_ref),
        application_ref=str(application_ref),
        bot_user_ref=f"{bot.id}@{bot.origin_domain}",
        target=RemoteUserProfile.model_validate(profile_from_user(target)),
        pair_key=pair_key,
        authority_domain=authority_domain,
        source_runtime_proof=source_runtime_proof.model_dump(mode="json"),
        authority_runtime_proof=authority_runtime_proof.model_dump(mode="json"),
        refresh_grant_id=refresh_grant_id,
    )
    raw: object
    if installation_domain == settings.domain:
        # Never self-HTTP while this transaction owns the application lock.
        # Reuse B's exact endpoint implementation in-process so A=B and
        # A=B=C have the same validation and persistence contract without a
        # second database session waiting on our own lock.
        from fastapi import HTTPException

        from app.api.bot_dm_federation import _attest_bot_dm_capability
        from app.federation.security import FederationPrincipal

        try:
            raw = await _attest_bot_dm_capability(
                request,
                FederationPrincipal(origin=settings.domain, key_id="local:self"),
                session,
                redis,
                snowflake,
                settings,
                commit=False,
            )
        except HTTPException as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise BotDMCapabilityAuthorityUnavailable(
                    "installation authority is unavailable"
                ) from exc
            if exc.status_code == 403:
                raise BotDMCapabilitySourceRejected(
                    f"installation authority rejected DM grant ({exc.status_code})"
                ) from exc
            if exc.status_code in {401, 404, 409}:
                raise BotDMCapabilityProofInvalid(
                    f"installation authority returned an ambiguous status ({exc.status_code})"
                ) from exc
            raise BotDMCapabilityProofInvalid(
                f"installation authority returned an invalid status ({exc.status_code})"
            ) from exc
    else:
        try:
            response = await signed_request(
                session,
                settings,
                "POST",
                installation_domain,
                "/_kaede/v1/bot-dm/capabilities/attest",
                payload=request.model_dump(mode="json"),
                request_timeout=8,
                max_response_bytes=64 * 1024,
            )
        except FederationNetworkError as exc:
            raise BotDMCapabilityAuthorityUnavailable(
                "installation authority is unavailable"
            ) from exc
        if response.status_code in {429} or response.status_code >= 500:
            raise BotDMCapabilityAuthorityUnavailable("installation authority is unavailable")
        if response.status_code == 403:
            raise BotDMCapabilitySourceRejected(
                f"installation authority rejected DM grant ({response.status_code})"
            )
        if response.status_code in {401, 404, 409}:
            raise BotDMCapabilityProofInvalid(
                f"installation authority returned an ambiguous status ({response.status_code})"
            )
        if response.status_code != 200:
            raise BotDMCapabilityProofInvalid(
                f"installation authority returned an invalid status ({response.status_code})"
            )
        try:
            raw = decode_federation_response_json(response, max_response_bytes=64 * 1024)
        except FederationNetworkError as exc:
            raise BotDMCapabilityProofInvalid(
                "installation authority returned an invalid DM grant"
            ) from exc
    try:
        envelope, capability = await validated_bot_dm_capability_proof(
            session,
            settings,
            raw,
            expected_installation_authority=installation_domain,
        )
    except (TypeError, ValueError) as exc:
        raise BotDMCapabilityProofInvalid(
            "installation authority returned an invalid DM grant"
        ) from exc
    if (
        capability.source_kind != source_kind
        or capability.installation != installation_ref
        or capability.application != application_ref
        or capability.bot_user != EntityRef(f"{bot.id}@{bot.origin_domain}")
        or capability.target_user != EntityRef(f"{target.id}@{target.origin_domain}")
        or capability.pair_key != pair_key
        or capability.authority_domain != authority_domain
        or (refresh_grant_id is not None and capability.grant_id != refresh_grant_id)
    ):
        raise BotDMCapabilityProofInvalid(
            "installation authority changed the requested DM grant identity"
        )
    try:
        authority_runtime_snapshot = ApplicationRuntimeSnapshot.model_validate(
            authority_runtime_proof.content
        )
        require_capability_runtime_binding(
            capability,
            authority_runtime_proof,
            authority_runtime_snapshot,
        )
    except ValueError as exc:
        raise BotDMCapabilityProofInvalid(
            "installation authority changed the application runtime binding"
        ) from exc
    await require_stored_capability_runtime(session, settings, capability)
    row, _ = await apply_bot_dm_capability(
        session,
        snowflake,
        envelope,
        capability,
        runtime_admitted=True,
    )
    pending_local_admission = bool(
        refresh_grant_id is None
        and authority_domain == settings.domain
        and row is not None
        and row.revision == int(capability.revision)
        and int(row.admission_revision or row.revision) < row.revision
        and stored_source_bot_dm_capability_payload(row) == capability
    )
    if (
        row is None
        or (not capability_is_active(row) and not pending_local_admission)
        or row.revision != int(capability.revision)
        or int(row.target_access_revocation_generation or 0)
        != int(capability.target_access_revocation_generation)
    ):
        raise PermissionError("bot DM capability is fenced by a newer access revocation")
    return envelope, capability, row


async def refresh_bot_dm_capability_proof(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    redis: Redis,
    row: BotDMCapability,
    *,
    source_runtime_proof: EventEnvelope,
    authority_runtime_proof: EventEnvelope,
) -> tuple[EventEnvelope, BotDMCapabilityPayload, BotDMCapability]:
    """Refresh an immutable grant without recomputing its mutable user handle."""

    bot = await session.get(User, (row.bot_user_id, row.bot_user_domain))
    target = await session.get(User, (row.target_user_id, row.target_user_domain))
    if (
        bot is None
        or bot.account_type != "bot"
        or bot.disabled_at is not None
        or target is None
        or target.account_type != "human"
        or target.disabled_at is not None
    ):
        raise PermissionError("bot DM capability identity is unavailable")
    if row.source_kind not in {"guild", "user"}:
        raise BotDMCapabilityProofInvalid("stored bot DM capability source is invalid")
    source_kind: Literal["guild", "user"] = "guild" if row.source_kind == "guild" else "user"
    return await fetch_bot_dm_capability_proof(
        session,
        settings,
        snowflake,
        redis,
        source_kind=source_kind,
        installation_ref=EntityRef(
            f"{row.source_installation_id}@{row.source_installation_domain}"
        ),
        application_ref=EntityRef(f"{row.application_id}@{row.application_domain}"),
        bot=bot,
        target=target,
        pair_key=row.pair_key,
        authority_domain=row.authority_domain,
        source_runtime_proof=source_runtime_proof,
        authority_runtime_proof=authority_runtime_proof,
        refresh_grant_id=row.grant_id,
    )


async def revoke_bot_dm_capabilities(
    session: AsyncSession,
    settings: Settings,
    *,
    guild_installation_ids: Iterable[int] = (),
    user_installation_ids: Iterable[int] = (),
    application_ref: tuple[int, str] | None = None,
    now: datetime | None = None,
) -> tuple[list[BotDMCapability], set[str]]:
    """Revoke B-authoritative DM leases and queue exact A/C tombstones.

    This helper performs no commit. Installation/app mutation, local lease
    state, and both durable destination projections therefore remain atomic.
    """

    guild_ids = set(guild_installation_ids)
    user_ids = set(user_installation_ids)
    source_conditions = []
    if guild_ids:
        source_conditions.append(
            and_(
                BotDMCapability.source_kind == "guild",
                BotDMCapability.source_installation_id.in_(guild_ids),
                BotDMCapability.source_installation_domain == settings.domain,
            )
        )
    if user_ids:
        source_conditions.append(
            and_(
                BotDMCapability.source_kind == "user",
                BotDMCapability.source_installation_id.in_(user_ids),
                BotDMCapability.source_installation_domain == settings.domain,
            )
        )
    if application_ref is not None:
        source_conditions.append(
            and_(
                BotDMCapability.application_id == application_ref[0],
                BotDMCapability.application_domain == application_ref[1],
                BotDMCapability.source_installation_domain == settings.domain,
            )
        )
    if not source_conditions:
        return [], set()
    observed_rows = list(
        await session.scalars(
            select(BotDMCapability)
            .where(
                or_(*source_conditions),
                BotDMCapability.status != "revoked",
            )
            .order_by(BotDMCapability.grant_id)
            .execution_options(populate_existing=True)
        )
    )
    if not observed_rows:
        return [], set()

    from app.federation.events import (
        build_envelope,
        discard_superseded_latest_state_event,
        queue_event,
    )

    revoked_at = now or datetime.now(UTC)
    destinations: set[str] = set()
    rows: list[BotDMCapability] = []
    for grant_id in (observed.grant_id for observed in observed_rows):
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"bot-dm-capability:{grant_id}", 0)
                )
            )
        )
        row = await session.scalar(
            select(BotDMCapability)
            .where(
                BotDMCapability.grant_id == grant_id,
                or_(*source_conditions),
                BotDMCapability.status != "revoked",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            continue
        rows.append(row)
        prior = EventEnvelope.model_validate(row.proof)
        prior_payload = BotDMCapabilityPayload.model_validate(prior.content)
        revoked = prior_payload.model_copy(
            update={
                "revision": str(row.revision + 1),
                "status": "revoked",
                "expires_at_ms": str(
                    int((revoked_at + BOT_DM_CAPABILITY_LEASE).timestamp() * 1000)
                ),
            }
        )
        bot = await session.get(User, (row.bot_user_id, row.bot_user_domain))
        if bot is None or bot.account_type != "bot":
            raise RuntimeError("bot DM capability lost its bot identity")
        local_envelope = await build_envelope(
            session,
            settings,
            BOT_DM_CAPABILITY_EVENT,
            bot,
            revoked.model_dump(mode="json"),
            authority_attested_actor=True,
        )
        await apply_bot_dm_capability(
            session,
            None,
            EventEnvelope.model_validate(local_envelope),
            revoked,
        )
        for destination in sorted(
            {row.application_domain, row.authority_domain} - {settings.domain}
        ):
            await discard_superseded_latest_state_event(
                session,
                destination=destination,
                event_type=BOT_DM_CAPABILITY_EVENT,
                grant_id=row.grant_id,
            )
            envelope = await build_envelope(
                session,
                settings,
                BOT_DM_CAPABILITY_EVENT,
                bot,
                revoked.model_dump(mode="json"),
                authority_attested_actor=True,
            )
            await queue_event(session, settings, destination, envelope)
            destinations.add(destination)
    return rows, destinations
