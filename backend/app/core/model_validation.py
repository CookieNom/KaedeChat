from __future__ import annotations

from collections.abc import Mapping
from types import UnionType
from typing import Literal, Union, get_args, get_origin

from pydantic import BaseModel, BeforeValidator, model_validator


def _strict_integer_shape(annotation: object) -> tuple[str, object | None] | None:
    """Locate integer fields while leaving refs and parsed timestamps alone."""

    if annotation is int:
        return ("scalar", None)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Literal and arguments and all(type(item) is int for item in arguments):
        return ("scalar", None)
    if origin in {Union, UnionType}:
        present = tuple(item for item in arguments if item is not type(None))
        return _strict_integer_shape(present[0]) if len(present) == 1 else None
    if origin is list and len(arguments) == 1:
        child = _strict_integer_shape(arguments[0])
        return ("list", child) if child is not None else None
    return None


def _strict_boolean_shape(annotation: object) -> tuple[str, object | None] | None:
    if annotation is bool:
        return ("scalar", None)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Literal and arguments and all(type(item) is bool for item in arguments):
        return ("scalar", None)
    if origin in {Union, UnionType}:
        present = tuple(item for item in arguments if item is not type(None))
        return _strict_boolean_shape(present[0]) if len(present) == 1 else None
    if origin is list and len(arguments) == 1:
        child = _strict_boolean_shape(arguments[0])
        return ("list", child) if child is not None else None
    return None


def _strict_number_shape(annotation: object) -> tuple[str, object | None] | None:
    """Locate JSON number fields that may accept either integer or decimal syntax."""

    if annotation is float:
        return ("scalar", None)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Literal and arguments and all(type(item) in {int, float} for item in arguments):
        return ("scalar", None)
    if origin in {Union, UnionType}:
        present = tuple(item for item in arguments if item is not type(None))
        if len(present) == 1:
            return _strict_number_shape(present[0])
        if len(present) == 2 and set(present) == {int, float}:
            return ("scalar", None)
        return None
    if origin is list and len(arguments) == 1:
        child = _strict_number_shape(arguments[0])
        return ("list", child) if child is not None else None
    return None


def _require_strict_integer(value: object, shape: tuple[str, object | None], field: str) -> None:
    if value is None:
        return
    kind, child = shape
    if kind == "scalar":
        if type(value) is not int:
            raise ValueError(f"{field} must be an integer")
        return
    if not isinstance(value, (list, tuple)):
        return
    if not isinstance(child, tuple):
        raise RuntimeError("integer list validator is malformed")
    for item in value:
        _require_strict_integer(item, child, field)


def _require_strict_boolean(value: object, shape: tuple[str, object | None], field: str) -> None:
    if value is None:
        return
    kind, child = shape
    if kind == "scalar":
        if type(value) is not bool:
            raise ValueError(f"{field} must be a boolean")
        return
    if not isinstance(value, (list, tuple)):
        return
    if not isinstance(child, tuple):
        raise RuntimeError("boolean list validator is malformed")
    for item in value:
        _require_strict_boolean(item, child, field)


def _require_strict_number(value: object, shape: tuple[str, object | None], field: str) -> None:
    if value is None:
        return
    kind, child = shape
    if kind == "scalar":
        if type(value) not in {int, float}:
            raise ValueError(f"{field} must be a JSON number")
        return
    if not isinstance(value, (list, tuple)):
        return
    if not isinstance(child, tuple):
        raise RuntimeError("number list validator is malformed")
    for item in value:
        _require_strict_number(item, child, field)


def validate_unambiguous_model_input(
    value: object,
    model_fields: Mapping[str, object],
) -> object:
    """Reject NUL text and ambiguous boolean or numeric JSON coercion."""

    def reject_nul(item: object) -> None:
        if isinstance(item, str) and "\x00" in item:
            raise ValueError("must not contain NUL characters")
        if isinstance(item, Mapping):
            for key, child in item.items():
                reject_nul(key)
                reject_nul(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                reject_nul(child)

    reject_nul(value)
    if isinstance(value, Mapping):
        for name, model_field in model_fields.items():
            if name not in value:
                continue
            metadata = getattr(model_field, "metadata", ())
            if any(isinstance(item, BeforeValidator) for item in metadata):
                # Custom wire types (for example decimal-string snowflakes)
                # own a stricter, non-integer input contract.
                continue
            integer_shape = _strict_integer_shape(getattr(model_field, "annotation", None))
            if integer_shape is not None:
                _require_strict_integer(value[name], integer_shape, name)
            boolean_shape = _strict_boolean_shape(getattr(model_field, "annotation", None))
            if boolean_shape is not None:
                _require_strict_boolean(value[name], boolean_shape, name)
            number_shape = _strict_number_shape(getattr(model_field, "annotation", None))
            if number_shape is not None:
                _require_strict_number(value[name], number_shape, name)
    return value


class UnambiguousInputModel(BaseModel):
    """Base for externally supplied JSON that must not rely on coercion."""

    @model_validator(mode="before")
    @classmethod
    def reject_ambiguous_wire_values(cls, value: object) -> object:
        return validate_unambiguous_model_input(value, cls.model_fields)
