from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate_compose import (
    ComposePolicyError,
    validate_caddy_edge,
    validate_development,
    validate_migration_startup,
)


def development_model(port: str = "29443") -> dict[str, object]:
    alpha = f"https://media.alpha.localhost:{port}"
    beta = f"https://media.beta.localhost:{port}"
    return {
        "services": {
            "alpha-api": {"environment": {"KAEDE_MEDIA_PUBLIC_BASE_URL": alpha}},
            "beta-api": {"environment": {"KAEDE_MEDIA_PUBLIC_BASE_URL": beta}},
            "frontend": {
                "environment": {"KAEDE_MEDIA_UPLOAD_ORIGINS": f"{alpha} {beta}"}
            },
        }
    }


class DevelopmentComposePolicyTests(unittest.TestCase):
    def test_media_urls_and_csp_follow_the_selected_port(self) -> None:
        validate_development(development_model(), https_port="29443")

    def test_hardcoded_media_port_is_rejected(self) -> None:
        with self.assertRaisesRegex(ComposePolicyError, "KAEDE_DEV_HTTPS_PORT"):
            validate_development(development_model("18443"), https_port="29443")


class EdgeComposePolicyTests(unittest.TestCase):
    def test_caddy_is_required(self) -> None:
        validate_caddy_edge({"caddy": {}})

    def test_missing_caddy_is_rejected(self) -> None:
        with self.assertRaisesRegex(ComposePolicyError, "internal Caddy"):
            validate_caddy_edge({})


def migration_services() -> dict[str, object]:
    wait_for_migration = {
        "depends_on": {"migrate": {"condition": "service_completed_successfully"}}
    }
    return {
        "migrate": {
            "command": ["sh", "-ec", "alembic upgrade head && kaede bootstrap"],
            "restart": "no",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
                "dragonfly": {"condition": "service_healthy"},
            },
        },
        **{
            service_name: wait_for_migration
            for service_name in ("api", "gateway", "worker", "caddy")
        },
    }


class MigrationStartupPolicyTests(unittest.TestCase):
    def test_ordered_one_shot_migration_startup_is_accepted(self) -> None:
        validate_migration_startup(migration_services())

    def test_runtime_cannot_start_before_migration_success(self) -> None:
        services = migration_services()
        services["gateway"] = {"depends_on": {}}
        with self.assertRaisesRegex(ComposePolicyError, "gateway must wait"):
            validate_migration_startup(services)

    def test_bootstrap_cannot_run_before_schema_upgrade(self) -> None:
        services = migration_services()
        services["migrate"]["command"] = [
            "sh",
            "-ec",
            "kaede bootstrap && alembic upgrade head",
        ]
        with self.assertRaisesRegex(ComposePolicyError, "bootstrap must run after"):
            validate_migration_startup(services)


if __name__ == "__main__":
    unittest.main()
