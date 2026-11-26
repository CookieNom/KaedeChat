"""Trusted, operator-installed extensions. Only declarative rules reach chat clients."""

import importlib.util
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

Platform = Literal["web", "desktop", "mobile"]


class LinkRule(BaseModel):
    host: str = Field(max_length=253, pattern=r"^[a-z0-9.-]+$")
    parameter: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    value: str = Field(pattern=r"^[A-Za-z0-9._~-]{1,128}$")
    replace: bool = False


@dataclass(frozen=True)
class Extension:
    title: str
    description: str
    # A pane edits a collection of records. Supported controls: text and checkbox.
    fields: list[dict[str, str]]
    validate: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    link_rules: Callable[[list[dict[str, Any]], Platform], list[LinkRule]] | None = None
    admin_router: APIRouter | None = None
    actions: tuple[tuple[str, str], ...] = ()


def load_extensions(directory: Path) -> dict[str, Extension]:
    extensions = {}
    for path in sorted(directory.glob("*/extension.py")):
        key = path.parent.name
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", key):
            raise ValueError(f"Invalid vendor extension name: {key}")
        spec = importlib.util.spec_from_file_location(
            f"kaede_vendor_{key}", path, submodule_search_locations=[str(path.parent)]
        )
        if not spec or not spec.loader:
            raise RuntimeError(f"Cannot load vendor extension {key}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        if not isinstance(module.extension, Extension):
            raise TypeError(f"Invalid vendor extension {key}")
        extensions[key] = module.extension
    return extensions


extensions = load_extensions(
    Path(
        os.environ.get("KAEDE_VENDOR_DIR")
        or Path(__file__).resolve().parents[2] / "vendor/extensions"
    )
)
