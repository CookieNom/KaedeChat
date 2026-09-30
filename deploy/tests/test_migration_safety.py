from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kubernetes"))
from migration_safety import PREFIX, manifest, online_revisions, safe_upgrade, sources
from stack import ROOT


def migration(revision, parent, body):
    return (
        "from alembic import op\nimport sqlalchemy as sa\n"
        "from sqlalchemy.dialects import postgresql\n"
        f"revision = {revision!r}\ndown_revision = {parent!r}\n"
        "branch_labels = None\ndepends_on = None\n"
        "def upgrade():\n"
        + "\n".join("    " + line for line in body.splitlines())
        + "\n"
    )


class MigrationSafetyTests(unittest.TestCase):
    def test_recognized_operations_without_annotations(self):
        for body in (
            'op.add_column("users", sa.Column("note", sa.Text(), nullable=True))',
            'op.add_column("users", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()))',
            'op.add_column("users", sa.Column("options", postgresql.JSONB(), nullable=False, server_default="{}"))',
            'op.create_table("new_table", sa.Column("id", sa.BigInteger(), primary_key=True))\nop.create_index("ix_new", "new_table", ["id"])',
            'op.create_table("new_table", sa.Column("id", sa.BigInteger()))\nop.create_index("ix_new", "new_table", ["id"], postgresql_where=sa.text("id IS NOT NULL"))',
        ):
            with self.subTest(body=body):
                self.assertTrue(safe_upgrade(ast.parse(migration("b", "a", body))))

    def test_unknown_or_incompatible_operations_fail_closed(self):
        for body in (
            'op.execute("UPDATE users SET name = name")',
            'op.drop_column("users", "name")',
            'op.add_column("users", sa.Column("required", sa.Text(), nullable=False))',
            'op.add_column("users", sa.Column("stamp", sa.DateTime(), server_default=sa.func.now()))',
            'op.add_column("users", sa.Column("key", sa.Text(), unique=True))',
            'op.add_column("users", sa.Column("key", sa.Text(), sa.ForeignKey("other.id")))',
            'op.alter_column("users", "name", nullable=True)',
            'op.create_index("ix_users", "users", ["name"])',
            'op.create_check_constraint("check_name", "users", "name IS NOT NULL")',
            'for table in ["users"]:\n    op.add_column(table, sa.Column("note", sa.Text()))',
            'op.create_table("new_table", sa.Column("id", sa.Integer()), sa.CheckConstraint("custom_function(id)"))',
            "helper()",
        ):
            with self.subTest(body=body):
                self.assertFalse(safe_upgrade(ast.parse(migration("b", "a", body))))
        source = migration("b", "a", "pass")
        for extra in (
            "import os\n",
            "op.execute('SELECT 1')\n",
            "revision = helper()\n",
        ):
            with self.subTest(extra=extra):
                try:
                    result = safe_upgrade(ast.parse(source + extra))
                except ValueError:
                    result = False
                self.assertFalse(result)

    def test_every_skipped_revision_and_immutable_history_are_checked(self):
        a = PREFIX + "versions/a.py"
        b = PREFIX + "versions/b.py"
        c = PREFIX + "versions/c.py"
        baseline = {PREFIX + "env.py": "runner", a: migration("a", None, "pass")}
        files = baseline | {
            b: migration(
                "b", "a", 'op.add_column("users", sa.Column("note", sa.Text()))'
            ),
            c: migration("c", "b", "pass"),
        }
        self.assertEqual(online_revisions(manifest(baseline), files), ["b", "c"])
        cases = [
            (None, files),
            (manifest(baseline), files | {a: baseline[a] + "# changed"}),
            (manifest(baseline), files | {PREFIX + "env.py": "changed runner"}),
            (
                manifest(baseline),
                files | {b: migration("b", "a", 'op.execute("DELETE FROM users")')},
            ),
            (manifest(baseline), files | {c: migration("c", "a", "pass")}),
            (manifest(baseline), {k: v for k, v in files.items() if k != a}),
            (manifest(baseline), files | {c: migration("a", "b", "pass")}),
            (manifest(baseline), files | {c: migration("c", "missing", "pass")}),
            (manifest(baseline), files | {PREFIX + "helper.py": "pass"}),
        ]
        for previous, candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    online_revisions(previous, candidate)

    def test_real_additive_chain_and_backfill_refusal(self):
        files = sources(ROOT)
        latest = next(p for p in files if "9e4a1c3d6f82_" in p)
        daily = next(p for p in files if "8d3f0b2c5e71_" in p)
        baseline = {p: s for p, s in files.items() if p not in {latest, daily}}
        self.assertEqual(
            online_revisions(manifest(baseline), files),
            ["8d3f0b2c5e71", "9e4a1c3d6f82"],
        )
        for prefix in ("9e7a1c4b8d20_", "3d9a5e1c7b42_", "2c8f4d0b6e31_"):
            source = next(s for p, s in files.items() if prefix in p)
            self.assertFalse(safe_upgrade(ast.parse(source)))
