"""Conservative, non-executing classification of append-only Alembic upgrades."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

PREFIX = "backend/migrations/"
TYPES = {
    "sa.BigInteger",
    "sa.Integer",
    "sa.SmallInteger",
    "sa.String",
    "sa.Text",
    "sa.Boolean",
    "sa.Date",
    "sa.DateTime",
    "sa.Time",
    "sa.LargeBinary",
    "sa.Float",
    "sa.Numeric",
    "sa.JSON",
    "sa.Uuid",
    "postgresql.JSONB",
    "postgresql.UUID",
    "postgresql.ARRAY",
}
CONSTRUCTORS = TYPES | {
    "sa.Column",
    "sa.PrimaryKeyConstraint",
    "sa.ForeignKeyConstraint",
    "sa.UniqueConstraint",
    "sa.CheckConstraint",
    "sa.true",
    "sa.false",
    "sa.func.now",
}
IMPORTS = {
    "import sqlalchemy as sa",
    "from alembic import op",
    "from sqlalchemy.dialects import postgresql",
    "from __future__ import annotations",
    "from collections.abc import Sequence",
}


def sources(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text()
        for path in sorted((root / PREFIX).rglob("*.py"))
    }


def manifest(files: dict[str, str]) -> str:
    return json.dumps(
        {p: hashlib.sha256(s.encode()).hexdigest() for p, s in files.items()},
        sort_keys=True,
    )


def name(node: ast.AST) -> str:
    return ast.unparse(node)


def literal(node: ast.AST) -> object:
    return ast.literal_eval(node)


def expression(node: ast.AST) -> bool:
    if isinstance(node, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.UnaryOp)):
        try:
            literal(node)
            return True
        except (ValueError, TypeError):
            return False
    if isinstance(node, ast.Attribute):
        return name(node) in TYPES
    if isinstance(node, ast.Call):
        if name(node.func) == "sa.CheckConstraint":
            # CheckConstraint accepts raw SQL, unlike ordinary string values.
            # Only recognize simple predicates; functions/compound SQL need review.
            if (
                not node.args
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str)
                or re.fullmatch(
                    r"[a-z_][a-z_0-9]*(?:\s*(?:>=|<=|<>|=|>|<)\s*(?:-?\d+|true|false))?",
                    node.args[0].value,
                )
                is None
            ):
                return False
        if name(node.func) == "sa.text":
            return (
                len(node.args) == 1
                and not node.keywords
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and re.fullmatch(
                    r"[a-z_][a-z_0-9]* IS (?:NOT )?NULL", node.args[0].value
                )
                is not None
            )
        return (
            name(node.func) in CONSTRUCTORS
            and all(expression(a) for a in node.args)
            and all(k.arg is not None and expression(k.value) for k in node.keywords)
        )
    return False


def metadata(tree: ast.Module) -> dict[str, object]:
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                }:
                    result[target.id] = literal(node.value)
    return result


def safe_upgrade(tree: ast.Module) -> bool:
    """Recognize literal additive operations; never import candidate Python."""
    upgrades = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and name(node) in IMPORTS:
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not all(
                isinstance(t, ast.Name)
                and t.id in {"revision", "down_revision", "branch_labels", "depends_on"}
                for t in targets
            ):
                return False
            literal(node.value)
            if any(isinstance(n, ast.Call) for n in ast.walk(node)):
                return False
            if (
                any(t.id in {"branch_labels", "depends_on"} for t in targets)
                and literal(node.value) is not None
            ):
                return False
            continue
        if isinstance(node, ast.FunctionDef) and node.name in {"upgrade", "downgrade"}:
            if (
                node.decorator_list
                or ast.unparse(node.args)
                or (node.returns is not None and name(node.returns) != "None")
            ):
                return False
            if node.name == "upgrade":
                upgrades.append(node)
            continue
        return False
    if len(upgrades) != 1:
        return False
    created = set()
    for node in upgrades[0].body:
        if isinstance(node, ast.Pass):
            continue
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            return False
        call = node.value
        if not all(expression(a) for a in call.args) or not all(
            k.arg is not None and expression(k.value) for k in call.keywords
        ):
            return False
        kw = {k.arg: k.value for k in call.keywords}
        operation = name(call.func)
        if operation == "op.create_table":
            if len(call.args) < 2 or kw:
                return False
            table = literal(call.args[0])
            if not isinstance(table, str) or table in created:
                return False
            if not all(
                isinstance(a, ast.Call) and name(a.func) in CONSTRUCTORS - TYPES
                for a in call.args[1:]
            ):
                return False
            created.add(table)
        elif operation == "op.add_column":
            if len(call.args) != 2 or kw or not isinstance(literal(call.args[0]), str):
                return False
            column = call.args[1]
            if (
                not isinstance(column, ast.Call)
                or name(column.func) != "sa.Column"
                or len(column.args) != 2
            ):
                return False
            if not isinstance(literal(column.args[0]), str):
                return False
            typ = column.args[1]
            if name(typ.func if isinstance(typ, ast.Call) else typ) not in TYPES:
                return False
            options = {k.arg: k.value for k in column.keywords}
            if set(options) - {"nullable", "server_default", "comment"}:
                return False
            default = options.get("server_default")
            if default is not None and not (
                isinstance(default, ast.Constant)
                and isinstance(default.value, str)
                or isinstance(default, ast.Call)
                and name(default) in {"sa.true()", "sa.false()"}
            ):
                return False
            nullable = literal(options["nullable"]) if "nullable" in options else True
            if not isinstance(nullable, bool) or (
                nullable is False and default is None
            ):
                return False
        elif operation == "op.create_index":
            # Existing-table indexes need explicit concurrent execution/recovery.
            if (
                len(call.args) != 3
                or literal(call.args[1]) not in created
                or set(kw) - {"unique", "postgresql_where"}
            ):
                return False
        else:
            # Includes raw SQL, backfills, relaxed constraints (new values may
            # break old readers), renames, drops, and dynamic helper functions.
            return False
    return True


def online_revisions(previous: str | None, files: dict[str, str]) -> list[str]:
    """Return the approved chain or raise with an actionable maintenance reason."""
    try:
        baseline = json.loads(previous or "null")
        hashes = json.loads(manifest(files))
        if not isinstance(baseline, dict) or not baseline:
            raise ValueError("migration baseline is unavailable")
        if any(hashes.get(path) != value for path, value in baseline.items()):
            raise ValueError("previous migration files were modified or removed")
        added = set(files) - set(baseline)
        if any(not p.startswith(PREFIX + "versions/") for p in added):
            raise ValueError("migration runner or helper changed")
        revisions = {}
        for path, source in files.items():
            if not path.startswith(PREFIX + "versions/"):
                continue
            tree = ast.parse(source)
            info = metadata(tree)
            revision, parent = info.get("revision"), info.get("down_revision")
            if (
                not isinstance(revision, str)
                or revision in revisions
                or not (parent is None or isinstance(parent, str))
            ):
                raise ValueError("migration history is not a unique linear chain")
            if path in added and not safe_upgrade(tree):
                raise ValueError(
                    f"{Path(path).name} contains operations not proven online-safe"
                )
            revisions[revision] = (parent, path)
        heads = set(revisions) - {parent for parent, _ in revisions.values()}
        if len(heads) != 1:
            raise ValueError("migration history has multiple heads")
        ordered = []
        seen = set()
        current = heads.pop()
        while current is not None:
            if current in seen or current not in revisions:
                raise ValueError("migration history is incomplete or cyclic")
            seen.add(current)
            parent, path = revisions[current]
            ordered.append((current, path))
            current = parent
        if seen != set(revisions):
            raise ValueError("migration history is disconnected")
        ordered.reverse()
        pending = [r for r, p in ordered if p in added]
        if added and {p for _, p in ordered[-len(added) :]} != added:
            raise ValueError("new migrations do not extend the deployed history")
        return pending
    except (SyntaxError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError("migration history could not be classified safely") from exc
