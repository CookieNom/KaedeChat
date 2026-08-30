from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, cast

from pydantic import ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.application_contract import (
    DEVELOPER_TEAM_APPLICATION_LIMIT,
    canonical_application_manifest_projection,
    validate_application_https_url,
    validate_application_icon_hash,
    validate_application_install_contract,
    validate_known_permission_mask,
)
from app.bots.directory_contract import (
    DirectoryDescriptionLocalizations,
    DirectoryExternalLinks,
    DirectoryMediaList,
    DirectorySupportedLocales,
    validate_directory_localizations,
)
from app.bots.projection_locking import (
    bot_application_identity_owner,
    lock_bot_projection_identities,
)
from app.bots.runtime_control import (
    queue_application_runtime_snapshots,
    target_runtime_projection_ready,
)
from app.core.federation import DEVELOPER_TEAM_SNAPSHOT_EVENT
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings
from app.core.task_wake import wake_federation_destinations as wake_application_runtime_deliveries
from app.core.task_wake import wake_federation_destinations as wake_developer_team_snapshots
from app.db.bot_models import (
    BotApplication,
    BotApplicationTarget,
    DeveloperTeam,
    DeveloperTeamMember,
    DeveloperTeamMemberHighwater,
)
from app.db.materialization import materialize_updated_at
from app.db.models import User
from app.federation.events import (
    build_envelope,
    discard_superseded_latest_state_event,
    queue_event,
)
from app.federation.replication import profile_from_user, upsert_remote_user
from app.federation.schemas import FederationDomain, RemoteUserProfile, SnowflakeString

DeveloperTeamRole = Literal[
    "owner",
    "administrator",
    "developer",
    "security",
    "analyst",
    "support",
]


def manifest_team_placeholder_name(team_id: int) -> str:
    """Return the bounded sentinel used before a team's first signed snapshot."""

    return f"Remote manifest team {team_id}"


def _is_manifest_team_placeholder(team: DeveloperTeam, local_domain: str) -> bool:
    return (
        team.origin_domain != local_domain
        and team.federation_revision == 1
        and team.federation_metadata_fingerprint is None
        and team.federation_applications_fingerprint is None
        and team.personal is False
        and team.name == manifest_team_placeholder_name(team.id)
    )


class DeveloperApplicationProjection(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    id: SnowflakeString
    origin_domain: FederationDomain
    team_id: SnowflakeString
    team_domain: FederationDomain
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    icon_hash: str | None = Field(default=None, max_length=128)
    banner_hash: str | None = Field(default=None, max_length=128)
    support_url: str | None = Field(default=None, max_length=2048)
    privacy_url: str | None = Field(default=None, max_length=2048)
    terms_url: str | None = Field(default=None, max_length=2048)
    directory_enabled: bool
    directory_approved: bool
    directory_summary: str | None = Field(default=None, max_length=200)
    directory_category: (
        Literal["entertainment", "games", "moderation", "productivity", "social", "utilities"]
        | None
    ) = None
    directory_tags: list[str] = Field(max_length=5)
    directory_collections: list[Literal["featured", "staff-picks", "new-and-noteworthy"]] = Field(
        max_length=3
    )
    directory_media: DirectoryMediaList
    directory_external_links: DirectoryExternalLinks
    directory_supported_locales: DirectorySupportedLocales
    directory_description_localizations: DirectoryDescriptionLocalizations
    status: Literal[
        "draft",
        "active",
        "review_required",
        "suspended",
        "deleting",
        "deleted",
    ]
    custody_mode: Literal["managed", "external"]
    target_policy: Literal["open", "allowlist", "blocklist", "local_only"]
    default_scopes: list[str] = Field(max_length=64)
    default_intents: list[str] = Field(max_length=32)
    default_permissions: SnowflakeString
    supported_install_types: list[Literal["guild_install", "user_install"]] = Field(
        min_length=1, max_length=2
    )
    user_install_scopes: list[str] = Field(min_length=2, max_length=4)
    user_install_contexts: list[Literal["guild", "bot_dm", "private_channel"]] = Field(
        min_length=1, max_length=3
    )
    e2ee_modes: list[Literal["participant"]] = Field(max_length=1)
    manifest_generation: SnowflakeString
    command_generation: SnowflakeString
    revocation_generation: SnowflakeString
    bot_user: RemoteUserProfile

    @field_validator(
        "manifest_generation",
        "command_generation",
        "revocation_generation",
    )
    @classmethod
    def canonical_integer(cls, value: str) -> str:
        parsed = int(value)
        if str(parsed) != value or parsed < 0:
            raise ValueError("application projection integer is not canonical")
        return value

    @field_validator("id", "team_id")
    @classmethod
    def positive_identity(cls, value: str) -> str:
        if int(value) < 1:
            raise ValueError("application projection identity is invalid")
        return value

    @field_validator("default_permissions")
    @classmethod
    def valid_permissions(cls, value: str) -> str:
        validate_known_permission_mask(int(value), label="default permissions")
        return value

    @field_validator("icon_hash", "banner_hash")
    @classmethod
    def valid_icon_hash(cls, value: str | None) -> str | None:
        return validate_application_icon_hash(value)

    @field_validator("support_url", "privacy_url", "terms_url")
    @classmethod
    def valid_application_url(cls, value: str | None) -> str | None:
        return validate_application_https_url(value)

    @field_validator("directory_tags")
    @classmethod
    def canonical_directory_tags(cls, value: list[str]) -> list[str]:
        if value != list(dict.fromkeys(value)) or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", item) for item in value
        ):
            raise ValueError("directory tags are not canonical")
        return value

    @field_validator("directory_collections")
    @classmethod
    def unique_directory_collections(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("directory collections must be unique")
        return value

    @model_validator(mode="after")
    def authority_shape(self) -> DeveloperApplicationProjection:
        if (
            self.origin_domain != self.team_domain
            or self.bot_user.origin_domain != self.origin_domain
            or self.bot_user.account_type != "bot"
            or int(self.bot_user.id) < 1
        ):
            raise ValueError("application projection authority is invalid")
        if any(
            int(value) < 1
            for value in (
                self.manifest_generation,
                self.command_generation,
                self.revocation_generation,
            )
        ):
            raise ValueError("application projection generation is invalid")
        validate_application_install_contract(
            default_scopes=self.default_scopes,
            default_intents=self.default_intents,
            supported_install_types=self.supported_install_types,
            user_install_scopes=self.user_install_scopes,
            user_install_contexts=self.user_install_contexts,
            e2ee_modes=self.e2ee_modes,
        )
        if self.directory_approved and (
            not self.directory_enabled
            or not self.name.strip()
            or not (self.description or "").strip()
            or not (self.directory_summary or "").strip()
            or self.directory_category is None
            or not self.directory_tags
            or self.support_url is None
            or self.privacy_url is None
            or self.terms_url is None
        ):
            raise ValueError("approved directory metadata is incomplete")
        validate_directory_localizations(
            list(self.directory_supported_locales),
            dict(self.directory_description_localizations),
        )
        return self


class DeveloperTeamSnapshot(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    team_id: SnowflakeString
    team_domain: FederationDomain
    team_name: str = Field(min_length=1, max_length=100)
    personal: bool
    revision: SnowflakeString
    member_id: SnowflakeString
    member_domain: FederationDomain
    member_role: DeveloperTeamRole | None
    applications: list[DeveloperApplicationProjection] = Field(
        max_length=DEVELOPER_TEAM_APPLICATION_LIMIT
    )

    @field_validator("team_id", "member_id", "revision")
    @classmethod
    def positive_revision(cls, value: str) -> str:
        parsed = int(value)
        if str(parsed) != value or parsed < 1:
            raise ValueError("developer team revision is invalid")
        return value

    @model_validator(mode="after")
    def consistent_applications(self) -> DeveloperTeamSnapshot:
        if self.member_role is None and self.applications:
            raise ValueError("revoked team snapshots cannot disclose applications")
        refs: set[tuple[str, str]] = set()
        bot_refs: set[tuple[str, str]] = set()
        for application in self.applications:
            if (application.team_id, application.team_domain) != (
                self.team_id,
                self.team_domain,
            ):
                raise ValueError("application belongs to another developer team")
            ref = (application.id, application.origin_domain)
            if ref in refs:
                raise ValueError("developer snapshot repeats an application")
            refs.add(ref)
            bot_ref = (application.bot_user.id, application.bot_user.origin_domain)
            if bot_ref in bot_refs:
                raise ValueError("developer snapshot repeats a bot identity")
            bot_refs.add(bot_ref)
        return self


def authority_attested_developer_team_snapshot(
    event_type: str,
    raw: object,
    *,
    expected_authority: str,
    actor: tuple[str, str],
) -> bool:
    if event_type != DEVELOPER_TEAM_SNAPSHOT_EVENT:
        return False
    try:
        snapshot = DeveloperTeamSnapshot.model_validate(raw)
    except ValueError:
        return False
    return (
        snapshot.team_domain == expected_authority
        and (snapshot.member_id, snapshot.member_domain) == actor
    )


def _application_projection(
    application: BotApplication,
    bot: User,
) -> DeveloperApplicationProjection:
    return DeveloperApplicationProjection(
        id=str(application.id),
        origin_domain=application.origin_domain,
        team_id=str(application.team_id),
        team_domain=application.team_domain,
        name=application.name,
        description=application.description,
        icon_hash=application.icon_hash,
        banner_hash=application.banner_hash,
        support_url=application.support_url,
        privacy_url=application.privacy_url,
        terms_url=application.terms_url,
        directory_enabled=application.directory_enabled,
        directory_approved=application.directory_approved,
        directory_summary=application.directory_summary,
        directory_category=cast(
            Literal[
                "entertainment",
                "games",
                "moderation",
                "productivity",
                "social",
                "utilities",
            ]
            | None,
            application.directory_category,
        ),
        directory_tags=list(application.directory_tags),
        directory_collections=cast(
            list[Literal["featured", "staff-picks", "new-and-noteworthy"]],
            list(application.directory_collections),
        ),
        directory_media=list(application.directory_media or []),
        directory_external_links=list(application.directory_external_links or []),
        directory_supported_locales=list(application.directory_supported_locales or []),
        directory_description_localizations=dict(
            application.directory_description_localizations or {}
        ),
        status=cast(
            Literal[
                "draft",
                "active",
                "review_required",
                "suspended",
                "deleting",
                "deleted",
            ],
            application.status,
        ),
        custody_mode=cast(Literal["managed", "external"], application.custody_mode),
        target_policy=cast(
            Literal["open", "allowlist", "blocklist", "local_only"],
            application.target_policy,
        ),
        default_scopes=list(application.default_scopes),
        default_intents=list(application.default_intents),
        default_permissions=str(application.default_permissions),
        supported_install_types=list(application.supported_install_types),
        user_install_scopes=list(application.user_install_scopes),
        user_install_contexts=list(application.user_install_contexts),
        e2ee_modes=list(application.e2ee_modes),
        manifest_generation=str(application.manifest_generation),
        command_generation=str(application.command_generation),
        revocation_generation=str(application.revocation_generation),
        bot_user=RemoteUserProfile.model_validate(profile_from_user(bot)),
    )


async def _team_application_projections(
    session: AsyncSession,
    team: DeveloperTeam,
) -> list[DeveloperApplicationProjection]:
    rows = (
        await session.execute(
            select(BotApplication, User)
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.team_id == team.id,
                BotApplication.team_domain == team.origin_domain,
                BotApplication.status != "deleted",
            )
            .order_by(BotApplication.id)
            .limit(DEVELOPER_TEAM_APPLICATION_LIMIT + 1)
        )
    ).all()
    if len(rows) > DEVELOPER_TEAM_APPLICATION_LIMIT:
        raise RuntimeError("developer team exceeds the federated application limit")
    return [_application_projection(application, bot) for application, bot in rows]


async def queue_developer_team_snapshots(
    session: AsyncSession,
    settings: Settings,
    team: DeveloperTeam,
    *,
    revoked_members: tuple[User, ...] = (),
) -> set[str]:
    """Queue a full per-member projection in the mutation transaction."""

    locked_team = await session.scalar(
        select(DeveloperTeam)
        .where(
            DeveloperTeam.id == team.id,
            DeveloperTeam.origin_domain == team.origin_domain,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_team is None or locked_team.origin_domain != settings.domain:
        raise RuntimeError("only a developer-team authority may publish its snapshot")
    team = locked_team
    team.federation_revision += 1
    rows = (
        await session.execute(
            select(DeveloperTeamMember, User)
            .join(
                User,
                (User.id == DeveloperTeamMember.user_id)
                & (User.origin_domain == DeveloperTeamMember.user_domain),
            )
            .where(
                DeveloperTeamMember.team_id == team.id,
                DeveloperTeamMember.team_domain == team.origin_domain,
            )
        )
    ).all()
    applications = await _team_application_projections(session, team)
    recipients: dict[tuple[int, str], tuple[User, DeveloperTeamRole | None]] = {
        (user.id, user.origin_domain): (user, cast(DeveloperTeamRole, member.role))
        for member, user in rows
    }
    for user in revoked_members:
        recipients.setdefault((user.id, user.origin_domain), (user, None))
    destinations: set[str] = set()
    for user, role in recipients.values():
        if user.origin_domain == settings.domain:
            continue
        snapshot = DeveloperTeamSnapshot(
            team_id=str(team.id),
            team_domain=team.origin_domain,
            team_name=team.name,
            personal=team.personal,
            revision=str(team.federation_revision),
            member_id=str(user.id),
            member_domain=user.origin_domain,
            member_role=role,
            applications=applications if role is not None else [],
        )
        await discard_superseded_latest_state_event(
            session,
            destination=user.origin_domain,
            event_type=DEVELOPER_TEAM_SNAPSHOT_EVENT,
            actor_ref=(user.id, user.origin_domain),
            team_ref=(team.id, team.origin_domain),
        )
        envelope = await build_envelope(
            session,
            settings,
            DEVELOPER_TEAM_SNAPSHOT_EVENT,
            user,
            snapshot.model_dump(mode="json"),
            authority_attested_actor=True,
        )
        await queue_event(session, settings, user.origin_domain, envelope)
        destinations.add(user.origin_domain)
    return destinations


async def commit_developer_team_mutation(
    session: AsyncSession,
    settings: Settings,
    team: DeveloperTeam,
    *,
    revoked_members: tuple[User, ...] = (),
) -> None:
    """Commit an authority mutation with its durable remote projections."""

    destinations = await queue_developer_team_snapshots(
        session,
        settings,
        team,
        revoked_members=revoked_members,
    )
    await session.commit()
    await wake_developer_team_snapshots(destinations)


async def commit_developer_application_mutation(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    *,
    runtime_target_domains: set[str] | None = None,
) -> None:
    """Commit an application mutation and refresh every remote developer."""

    team = await session.get(
        DeveloperTeam,
        (application.team_id, application.team_domain),
    )
    if team is None or team.origin_domain != settings.domain:
        raise RuntimeError("local application is missing its authoritative developer team")
    developer_destinations = await queue_developer_team_snapshots(
        session,
        settings,
        team,
    )
    runtime_destinations = await queue_application_runtime_snapshots(
        session,
        settings,
        application,
        additional_target_domains=runtime_target_domains,
    )
    await materialize_updated_at(session, application)
    await session.commit()
    await wake_developer_team_snapshots(developer_destinations)
    await wake_application_runtime_deliveries(runtime_destinations)


_MANIFEST_APPLICATION_FIELDS = (
    "name",
    "description",
    "icon_hash",
    "support_url",
    "privacy_url",
    "target_policy",
    "default_scopes",
    "default_intents",
    "supported_install_types",
    "user_install_scopes",
    "user_install_contexts",
    "e2ee_modes",
)
_DEVELOPER_APPLICATION_FIELDS = (
    "banner_hash",
    "terms_url",
    "directory_enabled",
    "directory_approved",
    "directory_summary",
    "directory_category",
    "directory_tags",
    "directory_collections",
    "directory_media",
    "directory_external_links",
    "directory_supported_locales",
    "directory_description_localizations",
    "custody_mode",
)


def _application_manifest_projection(
    application: BotApplication | DeveloperApplicationProjection,
) -> tuple[object, ...]:
    return canonical_application_manifest_projection(
        name=application.name,
        description=application.description,
        icon_hash=application.icon_hash,
        support_url=application.support_url,
        privacy_url=application.privacy_url,
        target_policy=application.target_policy,
        default_scopes=application.default_scopes,
        default_intents=application.default_intents,
        default_permissions=int(application.default_permissions),
        supported_install_types=application.supported_install_types,
        user_install_scopes=application.user_install_scopes,
        user_install_contexts=application.user_install_contexts,
        e2ee_modes=application.e2ee_modes,
    )


def _apply_application_projection(
    application: BotApplication,
    incoming: DeveloperApplicationProjection,
    *,
    created: bool,
    runtime_target: BotApplicationTarget | None,
) -> None:
    if not created and (
        application.team_id,
        application.team_domain,
        application.bot_user_id,
        application.bot_user_domain,
    ) != (
        int(incoming.team_id),
        incoming.team_domain,
        int(incoming.bot_user.id),
        incoming.bot_user.origin_domain,
    ):
        raise ValueError("developer application projection changes immutable identity")
    application.team_id = int(incoming.team_id)
    application.team_domain = incoming.team_domain
    application.bot_user_id = int(incoming.bot_user.id)
    application.bot_user_domain = incoming.bot_user.origin_domain
    wire = incoming.model_dump(mode="json")
    incoming_manifest_generation = int(incoming.manifest_generation)
    current_manifest_generation = application.manifest_generation or 1
    if (
        not created
        and incoming_manifest_generation == current_manifest_generation
        and _application_manifest_projection(application)
        != _application_manifest_projection(incoming)
    ):
        raise ValueError("developer application projection conflicts at manifest generation")
    if created or incoming_manifest_generation > current_manifest_generation:
        for field in _MANIFEST_APPLICATION_FIELDS:
            setattr(application, field, getattr(incoming, field))
        application.default_permissions = int(incoming.default_permissions)
        application.manifest_generation = incoming_manifest_generation
    for field in _DEVELOPER_APPLICATION_FIELDS:
        setattr(
            application,
            field,
            wire[field]
            if field in {"directory_media", "directory_external_links"}
            else getattr(incoming, field),
        )
    application.command_generation = max(
        application.command_generation or 1,
        int(incoming.command_generation),
    )
    application.revocation_generation = max(
        application.revocation_generation or 1,
        int(incoming.revocation_generation),
    )
    previous_status = application.status
    runtime_ready = target_runtime_projection_ready(
        runtime_target,
        manifest_generation=application.manifest_generation,
        revocation_generation=application.revocation_generation,
    )
    if incoming.status != "active" or created or runtime_target is None or runtime_ready:
        application.status = incoming.status
    else:
        # Developer projections expose control-plane metadata to remote team
        # members.  They cannot reactivate a runtime target ahead of the
        # separately signed target projection carrying the same generations.
        application.status = previous_status if previous_status != "active" else "suspended"


def _projection_fingerprint(value: object) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).digest()


def _snapshot_metadata_fingerprint(snapshot: DeveloperTeamSnapshot) -> bytes:
    return _projection_fingerprint(
        {
            "team_id": snapshot.team_id,
            "team_domain": snapshot.team_domain,
            "team_name": snapshot.team_name,
            "personal": snapshot.personal,
            "revision": snapshot.revision,
        }
    )


def _snapshot_applications_fingerprint(snapshot: DeveloperTeamSnapshot) -> bytes:
    return _projection_fingerprint(
        {
            "team_id": snapshot.team_id,
            "team_domain": snapshot.team_domain,
            "revision": snapshot.revision,
            "applications": [item.model_dump(mode="json") for item in snapshot.applications],
        }
    )


def _snapshot_fingerprint(snapshot: DeveloperTeamSnapshot) -> bytes:
    return _projection_fingerprint(snapshot.model_dump(mode="json"))


def _validated_incoming_snapshot(
    settings: Settings,
    origin: str,
    actor: User,
    raw: object,
) -> DeveloperTeamSnapshot:
    snapshot = DeveloperTeamSnapshot.model_validate(raw)
    if (
        snapshot.team_domain != origin
        or origin == settings.domain
        or snapshot.member_domain != settings.domain
        or (snapshot.member_id, snapshot.member_domain) != (str(actor.id), actor.origin_domain)
        or not actor.is_local
        or actor.account_type != "human"
    ):
        raise ValueError("developer team snapshot authority is invalid")
    return snapshot


async def _prepare_snapshot_team(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    snapshot: DeveloperTeamSnapshot,
) -> tuple[DeveloperTeam, bool, bool]:
    await lock_bot_projection_identities(
        session,
        team_refs=((int(snapshot.team_id), snapshot.team_domain),),
    )
    team = await session.get(
        DeveloperTeam,
        (int(snapshot.team_id), snapshot.team_domain),
        with_for_update=True,
    )
    revision = int(snapshot.revision)
    if team is not None and revision < team.federation_revision:
        return team, False, False
    metadata_fingerprint = _snapshot_metadata_fingerprint(snapshot)
    applications_fingerprint = (
        _snapshot_applications_fingerprint(snapshot) if snapshot.member_role is not None else None
    )
    apply_applications = False
    if team is None:
        team = DeveloperTeam(
            id=int(snapshot.team_id),
            origin_domain=snapshot.team_domain,
            name=snapshot.team_name,
            personal=snapshot.personal,
            federation_revision=revision,
            federation_metadata_fingerprint=metadata_fingerprint,
            federation_applications_fingerprint=applications_fingerprint,
        )
        session.add(team)
        apply_applications = applications_fingerprint is not None
    elif revision > team.federation_revision:
        if team.origin_domain == settings.domain:
            raise ValueError("remote snapshot collides with a local developer team")
        team.name = snapshot.team_name
        team.personal = snapshot.personal
        team.federation_revision = revision
        team.federation_metadata_fingerprint = metadata_fingerprint
        team.federation_applications_fingerprint = applications_fingerprint
        apply_applications = applications_fingerprint is not None
    else:
        stored_metadata_fingerprint = team.federation_metadata_fingerprint
        if stored_metadata_fingerprint is None:
            if _is_manifest_team_placeholder(team, settings.domain):
                team.name = snapshot.team_name
                team.personal = snapshot.personal
            elif team.name != snapshot.team_name or team.personal != snapshot.personal:
                raise ValueError("developer team snapshot conflicts at the same revision")
            team.federation_metadata_fingerprint = metadata_fingerprint
        elif stored_metadata_fingerprint != metadata_fingerprint:
            raise ValueError("developer team snapshot conflicts at the same revision")
        if applications_fingerprint is not None:
            stored_applications_fingerprint = team.federation_applications_fingerprint
            if stored_applications_fingerprint is None:
                team.federation_applications_fingerprint = applications_fingerprint
                apply_applications = True
            elif stored_applications_fingerprint != applications_fingerprint:
                raise ValueError("developer team applications conflict at the same revision")

    highwater = await session.get(
        DeveloperTeamMemberHighwater,
        (team.id, team.origin_domain, actor.id, actor.origin_domain),
        with_for_update=True,
    )
    snapshot_fingerprint = _snapshot_fingerprint(snapshot)
    if highwater is not None and revision < highwater.revision:
        return team, False, False
    if highwater is not None and revision == highwater.revision:
        if highwater.snapshot_fingerprint != snapshot_fingerprint:
            raise ValueError("developer team member snapshot conflicts at the same revision")
        return team, False, False
    if highwater is None:
        session.add(
            DeveloperTeamMemberHighwater(
                team_id=team.id,
                team_domain=team.origin_domain,
                user_id=actor.id,
                user_domain=actor.origin_domain,
                user_is_local=True,
                revision=revision,
                snapshot_fingerprint=snapshot_fingerprint,
            )
        )
    else:
        highwater.revision = revision
        highwater.snapshot_fingerprint = snapshot_fingerprint
    return team, True, apply_applications


async def _apply_snapshot_member(
    session: AsyncSession,
    team: DeveloperTeam,
    actor: User,
    role: DeveloperTeamRole | None,
) -> bool:
    member = await session.get(
        DeveloperTeamMember,
        (team.id, team.origin_domain, actor.id, actor.origin_domain),
        with_for_update=True,
    )
    if role is None:
        if member is not None:
            await session.delete(member)
        return False
    if member is None:
        session.add(
            DeveloperTeamMember(
                team_id=team.id,
                team_domain=team.origin_domain,
                user_id=actor.id,
                user_domain=actor.origin_domain,
                user_is_local=True,
                role=role,
            )
        )
    else:
        member.role = role
    return True


async def _upsert_snapshot_application(
    session: AsyncSession,
    settings: Settings,
    origin: str,
    team: DeveloperTeam,
    incoming: DeveloperApplicationProjection,
    application: BotApplication | None,
    runtime_target: BotApplicationTarget | None,
) -> None:
    bot = await upsert_remote_user(session, settings, incoming.bot_user)
    bot.account_type = "bot"
    if bot.origin_domain != origin:
        raise ValueError("developer application projection bot identity is invalid")
    key = (int(incoming.id), incoming.origin_domain)
    created = application is None
    if application is None:
        application = BotApplication(
            id=key[0],
            origin_domain=key[1],
            team_id=team.id,
            team_domain=team.origin_domain,
            bot_user_id=bot.id,
            bot_user_domain=bot.origin_domain,
            name=incoming.name,
        )
        session.add(application)
    _apply_application_projection(
        application,
        incoming,
        created=created,
        runtime_target=runtime_target,
    )


type _SnapshotApplicationState = tuple[BotApplication | None, BotApplicationTarget | None]


async def _preflight_snapshot_applications(
    session: AsyncSession,
    settings: Settings,
    applications: list[DeveloperApplicationProjection],
) -> dict[tuple[int, str], _SnapshotApplicationState]:
    """Fence every identity and reject the whole snapshot before profile writes."""

    application_refs = {(int(item.id), item.origin_domain) for item in applications}
    bot_refs = {(int(item.bot_user.id), item.bot_user.origin_domain) for item in applications}
    await lock_bot_projection_identities(
        session,
        application_refs=application_refs,
        bot_user_refs=bot_refs,
    )
    states: dict[tuple[int, str], _SnapshotApplicationState] = {}
    for incoming in applications:
        key = (int(incoming.id), incoming.origin_domain)
        bot_key = (int(incoming.bot_user.id), incoming.bot_user.origin_domain)
        owner = await bot_application_identity_owner(session, bot_key)
        if owner is not None and (owner.id, owner.origin_domain) != key:
            raise ValueError(
                "developer application projection reuses another application's bot identity"
            )
        application = await session.get(BotApplication, key, with_for_update=True)
        if application is not None and (
            application.team_id,
            application.team_domain,
            application.bot_user_id,
            application.bot_user_domain,
        ) != (
            int(incoming.team_id),
            incoming.team_domain,
            bot_key[0],
            bot_key[1],
        ):
            raise ValueError("developer application projection changes immutable identity")
        existing_bot = await session.get(User, bot_key)
        if existing_bot is not None and existing_bot.account_type != "bot":
            raise ValueError("developer application projection reuses a human identity")
        runtime_target = (
            None
            if application is None
            else await session.get(
                BotApplicationTarget,
                (application.id, application.origin_domain, settings.domain),
                with_for_update=True,
            )
        )
        states[key] = (application, runtime_target)
    return states


async def _apply_snapshot_applications(
    session: AsyncSession,
    settings: Settings,
    origin: str,
    team: DeveloperTeam,
    applications: list[DeveloperApplicationProjection],
    states: dict[tuple[int, str], _SnapshotApplicationState],
) -> None:
    for incoming in applications:
        application, runtime_target = states[(int(incoming.id), incoming.origin_domain)]
        await _upsert_snapshot_application(
            session,
            settings,
            origin,
            team,
            incoming,
            application,
            runtime_target,
        )
    incoming_refs = {(int(item.id), item.origin_domain) for item in applications}
    stored = await session.scalars(
        select(BotApplication).where(
            BotApplication.team_id == team.id,
            BotApplication.team_domain == team.origin_domain,
        )
    )
    for application in stored:
        if (application.id, application.origin_domain) not in incoming_refs:
            application.status = "deleted"


async def apply_developer_team_snapshot(
    session: AsyncSession,
    settings: Settings,
    origin: str,
    actor: User,
    raw: object,
) -> bool:
    """Apply one monotonic authority snapshot for the local target member."""

    snapshot = _validated_incoming_snapshot(settings, origin, actor, raw)
    application_states = (
        await _preflight_snapshot_applications(session, settings, snapshot.applications)
        if snapshot.member_role is not None
        else {}
    )
    team, member_changed, applications_changed = await _prepare_snapshot_team(
        session, settings, actor, snapshot
    )
    if not member_changed:
        return False
    active = await _apply_snapshot_member(session, team, actor, snapshot.member_role)
    if active and applications_changed:
        await _apply_snapshot_applications(
            session,
            settings,
            origin,
            team,
            snapshot.applications,
            application_states,
        )
    return True
