from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate_deploy_env import (
    DeploymentConfigurationError,
    FEDERATION_INTEGER_DEFAULTS,
    REMOTE_MEDIA_CACHE_BYTES_DEFAULT,
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

    def test_federation_budget_defaults_are_valid(self) -> None:
        values = self.production | {
            name: str(value) for name, value in FEDERATION_INTEGER_DEFAULTS.items()
        }
        values["KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED"] = "true"
        validate_values(values, observability=False)

    def test_federation_budgets_reject_invalid_numbers_and_relationships(self) -> None:
        invalid = (
            {"KAEDE_FEDERATION_INBOX_MAX_EVENTS_PER_ORIGIN": "many"},
            {
                "KAEDE_FEDERATION_INBOX_MAX_EVENTS_PER_ORIGIN": "2000",
                "KAEDE_FEDERATION_INBOX_MAX_EVENTS_TOTAL": "1000",
            },
            {
                "KAEDE_FEDERATION_DM_REPLICA_CACHE_MESSAGES_PER_CONVERSATION": "1001",
                "KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_CONVERSATION": "1000",
            },
            {
                "KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_GUILD": "2000",
                "KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_ORIGIN": "1000",
            },
            {"KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED": "sometimes"},
            {"KAEDE_MEDIA_REMOTE_CACHE_BYTES": "0"},
            {
                "KAEDE_MEDIA_MAX_ATTACHMENT_BYTES": "2000",
                "KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_PER_ORIGIN": "1000",
            },
            {
                "KAEDE_MEDIA_MAX_ATTACHMENT_BYTES": "2000",
                "KAEDE_MEDIA_INFLIGHT_QUOTA_BYTES": "1000",
            },
        )
        for overrides in invalid:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(DeploymentConfigurationError),
            ):
                validate_values(self.production | overrides, observability=False)

    def test_federation_budget_defaults_are_exposed_and_setup_preserves_tuning(
        self,
    ) -> None:
        repository = Path(__file__).resolve().parents[2]
        env_examples = (
            (repository / ".env.example").read_text(encoding="utf-8"),
            (repository / "deploy/reference.env.example").read_text(encoding="utf-8"),
        )
        compose = (repository / "deploy/compose.yml").read_text(encoding="utf-8")
        setup = (repository / "setup.sh").read_text(encoding="utf-8")
        direct_integer_settings = {
            "KAEDE_FEDERATION_CLOCK_SKEW_SECONDS",
            "KAEDE_FEDERATION_EVENT_RETENTION_DAYS",
            "KAEDE_FEDERATION_REMOTE_IDENTITY_RETENTION_DAYS",
            "KAEDE_FEDERATION_REMOTE_IDENTITY_GC_BATCH_SIZE",
            "KAEDE_FEDERATION_HISTORY_EXPORT_TTL_MINUTES",
            "KAEDE_FEDERATION_HISTORY_MERGE_CHUNK_SIZE",
        }

        for name, value in FEDERATION_INTEGER_DEFAULTS.items():
            with self.subTest(name=name):
                assignment = f"{name}={value}"
                self.assertTrue(all(assignment in example for example in env_examples))
                self.assertEqual(compose.count(f"${{{name}:-{value}}}"), 2)
                if name in direct_integer_settings:
                    self.assertIn(f"old_uint {name} {value}", setup)
                elif name != "KAEDE_FEDERATION_HISTORY_MAX_MESSAGES":
                    self.assertIn(f"quota_load {name} {value}", setup)
        for example in env_examples:
            self.assertIn("KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED=true", example)
        self.assertEqual(
            compose.count("${KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED:-true}"),
            2,
        )
        self.assertIn(
            "old_bool KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED true",
            setup,
        )
        remote_cache_assignment = (
            f"KAEDE_MEDIA_REMOTE_CACHE_BYTES={REMOTE_MEDIA_CACHE_BYTES_DEFAULT}"
        )
        self.assertTrue(
            all(remote_cache_assignment in example for example in env_examples)
        )
        self.assertEqual(
            compose.count(
                "${KAEDE_MEDIA_REMOTE_CACHE_BYTES:-"
                f"{REMOTE_MEDIA_CACHE_BYTES_DEFAULT}}}"
            ),
            2,
        )
        self.assertIn(
            "quota_load_upgrade_default KAEDE_MEDIA_REMOTE_CACHE_BYTES "
            f"21474836480 {REMOTE_MEDIA_CACHE_BYTES_DEFAULT}",
            setup,
        )
        self.assertIn(
            "quota_load_upgrade_default KAEDE_FEDERATION_HISTORY_MAX_MESSAGES "
            "250000 2000000",
            setup,
        )
        self.assertIn("Customize common storage limits", setup)
        self.assertIn("Advanced: customize every quota", setup)
        self.assertIn("validate_quota_relationships", setup)
        self.assertIn('source "$ROOT/deploy/setup-inputs.sh"', setup)
        self.assertIn(
            "KAEDE_FEDERATION_INBOX_MAX_BYTES_TOTAL "
            "'Retained federation event bytes instance-wide' "
            '"${QUOTA[KAEDE_FEDERATION_INBOX_MAX_BYTES_PER_ORIGIN]}"',
            setup,
        )
        self.assertIn(
            "KAEDE_FEDERATION_DM_REPLICA_CACHE_BYTES_PER_CONVERSATION "
            "'Rolling remote DM bytes cached per conversation' 1048576 "
            '"${QUOTA[KAEDE_FEDERATION_DM_MAX_BYTES_PER_CONVERSATION]}"',
            setup,
        )
        self.assertIn(
            "KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_ORIGIN "
            "'Remote replica bytes per origin' "
            '"${QUOTA[KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_GUILD]}"',
            setup,
        )
        self.assertIn(
            "Raising remote media in-flight capacity per origin",
            setup,
        )
        self.assertIn(
            "quota_load KAEDE_MEDIA_INFLIGHT_QUOTA_BYTES 524288000",
            setup,
        )
        self.assertIn(
            "Raising local upload in-flight capacity",
            setup,
        )
        self.assertIn(
            'emit KAEDE_MEDIA_INFLIGHT_QUOTA_BYTES "${QUOTA[KAEDE_MEDIA_INFLIGHT_QUOTA_BYTES]}"',
            setup,
        )

    def test_setup_human_quota_parsers(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        subprocess.run(
            ["bash", str(repository / "deploy/tests/test_setup_inputs.sh")],
            check=True,
            cwd=repository,
            capture_output=True,
            text=True,
        )

    def test_valid_auto_update_configuration(self) -> None:
        validate_values(
            self.production
            | {
                "AUTO_UPDATE_ENABLED": "true",
                "AUTO_UPDATE_REMOTE": "origin",
                "AUTO_UPDATE_BRANCH": "release/stable",
                "AUTO_UPDATE_INTERVAL": "12h",
                "AUTO_UPDATE_JITTER": "45m",
                "AUTO_UPDATE_BACKUP_HOOK": "/usr/local/sbin/kaede-backup",
                "AUTO_UPDATE_WAIT_TIMEOUT_SECONDS": "600",
            },
            observability=False,
        )

    def test_invalid_auto_update_configuration_is_rejected(self) -> None:
        invalid = (
            ("AUTO_UPDATE_ENABLED", "yes"),
            ("AUTO_UPDATE_REMOTE", "origin;touch-x"),
            ("AUTO_UPDATE_BRANCH", "../main"),
            ("AUTO_UPDATE_INTERVAL", "every day"),
            ("AUTO_UPDATE_JITTER", "0m"),
            ("AUTO_UPDATE_BACKUP_HOOK", "relative/backup"),
            ("AUTO_UPDATE_WAIT_TIMEOUT_SECONDS", "30"),
        )
        for name, value in invalid:
            with (
                self.subTest(name=name),
                self.assertRaises(DeploymentConfigurationError),
            ):
                validate_values(self.production | {name: value}, observability=False)

    def test_invalid_auto_update_error_explains_format_without_echoing_secrets(
        self,
    ) -> None:
        unsafe_value = "https://operator:do-not-display@example.test/repository"
        with self.assertRaisesRegex(
            DeploymentConfigurationError,
            r"AUTO_UPDATE_REMOTE must be a Git remote name.*Do not put a remote URL",
        ) as caught:
            validate_values(
                self.production | {"AUTO_UPDATE_REMOTE": unsafe_value},
                observability=False,
            )
        self.assertNotIn("do-not-display", str(caught.exception))

    def test_missing_environment_file_explains_how_to_create_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory, "missing.env")
            with self.assertRaisesRegex(
                DeploymentConfigurationError,
                r"Run `make setup` to create it",
            ):
                read_env_file(missing)

    def test_non_utf8_environment_file_explains_how_to_recover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "operator.env")
            path.write_bytes(b"KAEDE_DOMAIN=chat.test\n\xff")
            with self.assertRaisesRegex(
                DeploymentConfigurationError,
                r"not valid UTF-8.*Save it as UTF-8 text",
            ):
                read_env_file(path)

    def test_production_requires_media_scanning(self) -> None:
        values = self.production | {"KAEDE_MEDIA_SCAN_ENABLED": "false"}
        with self.assertRaisesRegex(DeploymentConfigurationError, "must be true"):
            validate_values(values, observability=False)

    def test_documented_secret_placeholder_is_rejected(self) -> None:
        values = self.production | {"KAEDE_ADMIN_TOKEN": "replace-with-a-token"}
        with self.assertRaisesRegex(DeploymentConfigurationError, "placeholder"):
            validate_values(values, observability=False)

    def test_enabled_interaction_services_require_private_credentials(self) -> None:
        with self.assertRaisesRegex(DeploymentConfigurationError, "KLIPY_API_KEY"):
            validate_values(
                self.production | {"KAEDE_KLIPY_ENABLED": "true"}, observability=False
            )

    def test_mobile_push_requires_a_valid_service_account(self) -> None:
        with self.assertRaisesRegex(
            DeploymentConfigurationError, "FCM_SERVICE_ACCOUNT"
        ):
            validate_values(
                self.production | {"KAEDE_PUSH_ENABLED": "true"}, observability=False
            )
        with self.assertRaisesRegex(DeploymentConfigurationError, "base64-encoded"):
            validate_values(
                self.production
                | {
                    "KAEDE_PUSH_ENABLED": "true",
                    "KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64": "not-base64",
                },
                observability=False,
            )
        credential = base64.b64encode(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "kaede-mobile",
                    "client_email": "firebase@example.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            ).encode()
        ).decode()
        validate_values(
            self.production
            | {
                "KAEDE_PUSH_ENABLED": "true",
                "KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64": credential,
            },
            observability=False,
        )
        alternate_endpoint = base64.b64encode(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "kaede-mobile",
                    "client_email": "firebase@example.iam.gserviceaccount.com",
                    "private_key": "test",
                    "token_uri": "https://oauth.example.test/token",
                }
            ).encode()
        ).decode()
        with self.assertRaisesRegex(
            DeploymentConfigurationError, "valid Firebase service account"
        ):
            validate_values(
                self.production
                | {
                    "KAEDE_PUSH_ENABLED": "true",
                    "KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64": alternate_endpoint,
                },
                observability=False,
            )
        with self.assertRaisesRegex(DeploymentConfigurationError, "TURNSTILE_SECRET"):
            validate_values(
                self.production
                | {
                    "KAEDE_TURNSTILE_ENABLED": "true",
                    "KAEDE_TURNSTILE_SITE_KEY": "site-key",
                },
                observability=False,
            )

    def test_observability_requires_an_independent_password(self) -> None:
        with self.assertRaisesRegex(DeploymentConfigurationError, "at least 20"):
            validate_values(self.production, observability=True)
        validate_values(
            self.production | {"GRAFANA_ADMIN_PASSWORD": "unique-observability-secret"},
            observability=True,
        )

    def test_custom_livekit_port_set_is_accepted(self) -> None:
        validate_values(
            self.production
            | {
                "KAEDE_VOICE_ENABLED": "true",
                "LIVEKIT_CONTROL_PORT": "7890",
                "LIVEKIT_RTC_TCP_PORT": "7891",
                "LIVEKIT_RTC_UDP_PORT": "7892",
                "LIVEKIT_TURN_TLS_PORT": "5350",
                "KAEDE_TURN_UDP_PORT": "13489",
                "KAEDE_VOICE_LIVEKIT_URL": "http://host.docker.internal:7890",
            },
            observability=False,
        )

    def test_duplicate_livekit_port_is_rejected(self) -> None:
        values = self.production | {
            "KAEDE_VOICE_ENABLED": "true",
            "LIVEKIT_CONTROL_PORT": "7890",
            "LIVEKIT_RTC_TCP_PORT": "7890",
        }
        with self.assertRaisesRegex(DeploymentConfigurationError, "must be distinct"):
            validate_values(values, observability=False)

    def test_livekit_control_url_must_match_selected_port(self) -> None:
        values = self.production | {
            "KAEDE_VOICE_ENABLED": "true",
            "LIVEKIT_CONTROL_PORT": "7890",
            "KAEDE_VOICE_LIVEKIT_URL": "http://host.docker.internal:7880",
        }
        with self.assertRaisesRegex(DeploymentConfigurationError, "must match"):
            validate_values(values, observability=False)

    def test_livekit_port_must_be_in_range(self) -> None:
        values = self.production | {
            "KAEDE_VOICE_ENABLED": "true",
            "LIVEKIT_CONTROL_PORT": "70000",
        }
        with self.assertRaisesRegex(DeploymentConfigurationError, "1024 to 65535"):
            validate_values(values, observability=False)

    def test_duplicate_file_assignment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "operator.env")
            path.write_text(
                "KAEDE_DOMAIN=one.test\nKAEDE_DOMAIN=two.test\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(DeploymentConfigurationError, "duplicate"):
                read_env_file(path)

    def test_production_file_must_be_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "operator.env")
            path.write_text("KAEDE_ENVIRONMENT=production\n", encoding="utf-8")
            os.chmod(path, 0o640)
            with self.assertRaisesRegex(
                DeploymentConfigurationError, r"chmod 600 .*operator\.env"
            ):
                validate_file_permissions(path, {"KAEDE_ENVIRONMENT": "production"})
            os.chmod(path, 0o600)
            validate_file_permissions(path, {"KAEDE_ENVIRONMENT": "production"})


if __name__ == "__main__":
    unittest.main()
