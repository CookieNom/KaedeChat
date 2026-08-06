from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from pydantic import BeforeValidator, GetCoreSchemaHandler, PlainSerializer
from pydantic_core import CoreSchema, core_schema

from app.core.settings import DOMAIN_RE

# PostgreSQL BIGINT is signed. Keeping the API boundary aligned with its storage
# representation prevents otherwise-valid identifiers from failing as database
# overflows later in request handling.
MAX_SNOWFLAKE = (1 << 63) - 1


def validate_snowflake(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("snowflake must be an unsigned decimal integer")
    if isinstance(value, str):
        if (
            not value
            or not value.isascii()
            or not value.isdecimal()
            or (len(value) > 1 and value.startswith("0"))
        ):
            raise ValueError("snowflake must be a canonical unsigned decimal string")
    elif not isinstance(value, int):
        raise ValueError("snowflake must be an unsigned decimal integer or string")
    parsed = int(value)
    if not 0 <= parsed <= MAX_SNOWFLAKE:
        raise ValueError("snowflake is outside the PostgreSQL BIGINT range")
    return parsed


def validate_wire_snowflake(value: object) -> int:
    """Validate a snowflake supplied in a JSON request body.

    JSON numbers cannot safely carry every signed-BIGINT value through a
    JavaScript client, so wire identifiers and masks must be decimal strings.
    Internal model construction continues to use :data:`Snowflake` below.
    """

    if not isinstance(value, str):
        raise ValueError("snowflake must be a canonical unsigned decimal string")
    return validate_snowflake(value)


Snowflake = Annotated[
    int,
    BeforeValidator(validate_snowflake),
    PlainSerializer(lambda value: str(value), return_type=str, when_used="json"),
]

WireSnowflake = Annotated[
    int,
    BeforeValidator(validate_wire_snowflake, json_schema_input_type=str),
    PlainSerializer(lambda value: str(value), return_type=str, when_used="json"),
]


@dataclass(frozen=True, slots=True)
class EntityReference:
    """A composite federated identifier.

    Bare decimal values remain a local-instance shorthand for backwards
    compatibility. Federated references must include their normalized origin
    domain as ``<decimal>@<domain>``.
    """

    id: int
    domain: str | None = None

    def __post_init__(self) -> None:
        validate_snowflake(self.id)
        if self.domain is not None:
            if self.domain != self.domain.rstrip(".").lower():
                raise ValueError("entity reference domain must be normalized")
            if not DOMAIN_RE.fullmatch(self.domain):
                raise ValueError("entity reference domain is invalid")

    def resolve(self, local_domain: str) -> tuple[int, str]:
        return self.id, self.domain or local_domain

    def __str__(self) -> str:
        return f"{self.id}@{self.domain}" if self.domain is not None else str(self.id)


def validate_entity_reference(value: str | int | EntityReference) -> EntityReference:
    if isinstance(value, EntityReference):
        return value
    if isinstance(value, bool):
        raise ValueError("entity reference must be a decimal ID with an optional origin domain")
    raw = str(value)
    identifier, separator, domain = raw.partition("@")
    parsed = validate_snowflake(identifier)
    if not separator:
        return EntityReference(parsed)
    if not domain or "@" in domain or domain != domain.rstrip(".").lower():
        raise ValueError("entity reference domain must be normalized")
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError("entity reference domain is invalid")
    return EntityReference(parsed, domain)


class EntityRef(str):
    """FastAPI-safe, canonical wire representation of an entity reference."""

    def __new__(cls, value: str) -> EntityRef:
        reference = validate_entity_reference(value)
        return str.__new__(cls, str(reference))

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        del source_type, handler
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())

    @property
    def reference(self) -> EntityReference:
        return validate_entity_reference(str(self))

    @property
    def id(self) -> int:
        return self.reference.id

    @property
    def domain(self) -> str | None:
        return self.reference.domain

    def resolve(self, local_domain: str) -> tuple[int, str]:
        return self.reference.resolve(local_domain)


class EntityReferenceLike(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def domain(self) -> str | None: ...

    def resolve(self, local_domain: str) -> tuple[int, str]: ...
