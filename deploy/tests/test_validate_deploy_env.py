from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate_deploy_env import (
    DeploymentConfigurationError,
    read_env_file,
    validate_file_permissions,
    validate_values,
)


class DeploymentEnvironmentValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.production = {
            "KAEDE_DOMAIN": "chat.kaede.test",
            "KAEDE_ENVIRONMENT": "production",
            "KAEDE_MEDIA_SCAN_ENABLED": "true",
        }

    def test_safe_minimal_production_boundary(self) -> None:
        validate_values(self.production, observability=False)

    def test_production_requires_media_scanning(self) -> None:
        values = self.production | {"KAEDE_MEDIA_SCAN_ENABLED": "false"}
        with self.assertRaisesRegex(DeploymentConfigurationError, "must be true"):
            validate_values(values, observability=False)

    def test_documented_secret_placeholder_is_rejected(self) -> None:
        values = self.production | {"KAEDE_ADMIN_TOKEN": "replace-with-a-token"}
        with self.assertRaisesRegex(DeploymentConfigurationError, "placeholder"):
            validate_values(values, observability=False)

    def test_observability_requires_an_independent_password(self) -> None:
        with self.assertRaisesRegex(DeploymentConfigurationError, "at least 20"):
            validate_values(self.production, observability=True)
        validate_values(
            self.production | {"GRAFANA_ADMIN_PASSWORD": "unique-observability-secret"},
            observability=True,
        )

    def test_duplicate_file_assignment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "operator.env")
            path.write_text("KAEDE_DOMAIN=one.test\nKAEDE_DOMAIN=two.test\n", encoding="utf-8")
            with self.assertRaisesRegex(DeploymentConfigurationError, "duplicate"):
                read_env_file(path)

    def test_production_file_must_be_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "operator.env")
            path.write_text("KAEDE_ENVIRONMENT=production\n", encoding="utf-8")
            os.chmod(path, 0o640)
            with self.assertRaisesRegex(DeploymentConfigurationError, "group/world"):
                validate_file_permissions(path, {"KAEDE_ENVIRONMENT": "production"})
            os.chmod(path, 0o600)
            validate_file_permissions(path, {"KAEDE_ENVIRONMENT": "production"})


if __name__ == "__main__":
    unittest.main()
