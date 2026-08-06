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
    validate_voice,
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


class VoiceComposePolicyTests(unittest.TestCase):
    def test_custom_livekit_ports_are_accepted_when_caddy_matches(self) -> None:
        services = {
            "voice-preflight": {"command": ["validate", "--voice"], "user": "1000:1000"},
            "api": {
                "environment": {
                    "KAEDE_VOICE_ENABLED": "true",
                    "KAEDE_VOICE_LIVEKIT_URL": "http://host.docker.internal:7890",
                }
            },
            "caddy": {"environment": {"LIVEKIT_CONTROL_PORT": "7890"}},
            "livekit": {
                "image": "livekit/livekit-server:v1.13.3",
                "network_mode": "host",
                "restart": "unless-stopped",
                "depends_on": {"voice-preflight": {}},
                "environment": {
                    "LIVEKIT_CONTROL_PORT": "7890",
                    "LIVEKIT_RTC_TCP_PORT": "7891",
                    "LIVEKIT_RTC_UDP_PORT": "7892",
                    "LIVEKIT_TURN_TLS_PORT": "5350",
                    "KAEDE_TURN_UDP_PORT": "13489",
                    "LIVEKIT_CONFIG": """port: 7890
rtc:
  tcp_port: 7891
  udp_port: 7892
turn:
  udp_port: 13489
  tls_port: 5350
""",
                },
                "volumes": [
                    {
                        "target": "/run/secrets/livekit-turn-cert.pem",
                        "read_only": True,
                    },
                    {
                        "target": "/run/secrets/livekit-turn-key.pem",
                        "read_only": True,
                    },
                ],
            },
        }
        validate_voice(services)

    def test_caddy_control_port_mismatch_is_rejected(self) -> None:
        services = {
            "voice-preflight": {"command": ["validate", "--voice"], "user": "1000:1000"},
            "api": {
                "environment": {
                    "KAEDE_VOICE_ENABLED": "true",
                    "KAEDE_VOICE_LIVEKIT_URL": "http://host.docker.internal:7890",
                }
            },
            "caddy": {"environment": {"LIVEKIT_CONTROL_PORT": "7880"}},
            "livekit": {
                "image": "livekit/livekit-server:v1.13.3",
                "network_mode": "host",
                "restart": "unless-stopped",
                "depends_on": {"voice-preflight": {}},
                "environment": {
                    "LIVEKIT_CONTROL_PORT": "7890",
                    "LIVEKIT_CONFIG": (
                        "port: 7890\ntcp_port: 7881\nudp_port: 7882\n"
                        "udp_port: 13478\ntls_port: 5349\n"
                    ),
                },
                "volumes": [
                    {"target": "/run/secrets/livekit-turn-cert.pem", "read_only": True},
                    {"target": "/run/secrets/livekit-turn-key.pem", "read_only": True},
                ],
            },
        }
        with self.assertRaisesRegex(ComposePolicyError, "same control port"):
            validate_voice(services)

if __name__ == "__main__":
    unittest.main()
