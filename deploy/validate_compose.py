"""Assert security properties on a rendered production Compose model."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


class ComposePolicyError(ValueError):
    """The rendered deployment violates a repository security invariant."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ComposePolicyError(message)


def validate_hardening(services: dict[str, dict[str, Any]]) -> None:
    hardened = {
        "api",
        "gateway",
        "migrate",
        "preflight",
        "scheduler",
        "storage-init",
        "worker",
    }
    for name in sorted(hardened):
        service = services[name]
        require(service.get("read_only") is True, f"{name} must have a read-only root filesystem")
        require("ALL" in service.get("cap_drop", []), f"{name} must drop all capabilities")
        require(
            "no-new-privileges:true" in service.get("security_opt", []),
            f"{name} must set no-new-privileges",
        )
    frontend = services["frontend-build"]
    require(frontend.get("network_mode") == "none", "frontend build must have no network")
    require(
        frontend.get("build", {}).get("target") == "build",
        "frontend must be compiled in the image build stage",
    )
    require(
        "pnpm build" not in " ".join(str(part) for part in frontend.get("command", [])),
        "frontend publisher must not compile application code at runtime",
    )
    require("ALL" in frontend.get("cap_drop", []), "frontend build must drop all capabilities")
    require(
        "no-new-privileges:true" in frontend.get("security_opt", []),
        "frontend build must set no-new-privileges",
    )
    require(
        bool(services["preflight"].get("user"))
        and services["preflight"].get("user") != "0:0",
        "preflight must be able to read the mode-0600 operator environment bind mount",
    )


def validate_ports(services: dict[str, dict[str, Any]]) -> None:
    allowed = {"api", "caddy", "grafana"}
    for name, service in services.items():
        ports = service.get("ports", [])
        require(not ports or name in allowed, f"{name} unexpectedly publishes a host port")
        for port in ports:
            require(
                port.get("host_ip") in {"127.0.0.1", "::1"},
                f"{name} port {port.get('published')} must bind to loopback",
            )


def validate_mounts(services: dict[str, dict[str, Any]]) -> None:
    for name, service in services.items():
        for mount in service.get("volumes", []):
            source = str(mount.get("source", ""))
            target = str(mount.get("target", ""))
            require(
                source != "/var/run/docker.sock" and target != "/var/run/docker.sock",
                f"{name} must not mount the Docker socket",
            )


def validate_secrets(services: dict[str, dict[str, Any]]) -> None:
    api_environment = services["api"].get("environment", {})
    gateway_environment = services["gateway"].get("environment", {})
    require(
        gateway_environment.get("KAEDE_SECRET_KEY") != api_environment.get("KAEDE_SECRET_KEY"),
        "gateway and API must not share the instance master key",
    )
    forbidden_gateway_names = {
        "KAEDE_ADMIN_TOKEN",
        "KAEDE_MAILTRAP_API_TOKEN",
        "KAEDE_MEDIA_S3_ACCESS_KEY",
        "KAEDE_MEDIA_S3_SECRET_KEY",
        "KAEDE_MEDIA_S3_SESSION_TOKEN",
        "KAEDE_SMTP_URL",
        "KAEDE_VOICE_API_SECRET",
    }
    leaked = sorted(forbidden_gateway_names.intersection(gateway_environment))
    require(not leaked, f"gateway receives forbidden credentials: {', '.join(leaked)}")

    scheduler_names = set(services["scheduler"].get("environment", {}))
    require(
        scheduler_names <= {"KAEDE_DRAGONFLY_URL", "KAEDE_LOG_LEVEL"},
        "scheduler receives credentials outside its narrow runtime role",
    )


def validate_images(services: dict[str, dict[str, Any]]) -> None:
    for name, service in services.items():
        image = service.get("image")
        if image is None:
            continue
        require(not image.endswith(":latest"), f"{name} uses a floating latest image")
        require("@sha256:" in image or ":" in image.rsplit("/", 1)[-1], f"{name} image has no tag")


def validate_lifecycle(services: dict[str, dict[str, Any]]) -> None:
    stable = {
        "api",
        "clamav",
        "dragonfly",
        "gateway",
        "postgres",
        "scheduler",
        "worker",
        "caddy",
    }
    if "garage" in services:
        stable.add("garage")
    for name in sorted(stable):
        require(
            services[name].get("restart") == "unless-stopped",
            f"{name} needs an explicit restart policy",
        )


def validate_caddy_edge(services: dict[str, dict[str, Any]]) -> None:
    require("caddy" in services, "rendered topology is missing the internal Caddy edge")


def validate_migration_startup(services: dict[str, dict[str, Any]]) -> None:
    migrate = services.get("migrate")
    require(migrate is not None, "rendered topology is missing the migration service")
    command = " ".join(str(part) for part in migrate.get("command", []))
    upgrade = command.find("alembic upgrade head")
    bootstrap = command.find("kaede bootstrap")
    require(upgrade >= 0, "migration service must upgrade to Alembic head")
    require(bootstrap > upgrade, "instance bootstrap must run after migrations")
    require(migrate.get("restart") == "no", "migration service must be one-shot")
    for dependency in ("postgres", "dragonfly"):
        condition = migrate.get("depends_on", {}).get(dependency, {}).get("condition")
        require(
            condition == "service_healthy",
            f"migration service must wait for healthy {dependency}",
        )
    for service_name in ("api", "gateway", "worker", "caddy"):
        condition = (
            services.get(service_name, {})
            .get("depends_on", {})
            .get("migrate", {})
            .get("condition")
        )
        require(
            condition == "service_completed_successfully",
            f"{service_name} must wait for successful migrations",
        )


def validate_observability(services: dict[str, dict[str, Any]]) -> None:
    for name in ("grafana", "loki", "prometheus"):
        service = services.get(name)
        require(service is not None, f"observability profile is missing {name}")
        require(service.get("restart") == "unless-stopped", f"{name} needs a restart policy")
        require(bool(service.get("healthcheck")), f"{name} needs a health check")
        dependencies = service.get("depends_on", {})
        require(
            "observability-preflight" in dependencies,
            f"{name} must wait for observability credential validation",
        )
    preflight = services.get("observability-preflight", {})
    command = " ".join(preflight.get("command", []))
    require(
        "--observability" in command,
        "observability preflight does not enforce its password gate",
    )
    require(
        bool(preflight.get("user")) and preflight.get("user") != "0:0",
        "observability preflight must be able to read the operator environment",
    )


def validate_voice(services: dict[str, dict[str, Any]]) -> None:
    preflight = services.get("voice-preflight")
    livekit = services.get("livekit")
    require(preflight is not None, "voice profile is missing voice-preflight")
    require(livekit is not None, "voice profile is missing LiveKit")
    require(
        "--voice" in " ".join(preflight.get("command", [])),
        "voice preflight does not enforce voice credentials and certificate paths",
    )
    require(
        bool(preflight.get("user")) and preflight.get("user") != "0:0",
        "voice preflight must be able to read the operator environment",
    )
    require(
        "voice-preflight" in livekit.get("depends_on", {}),
        "LiveKit must wait for voice preflight",
    )
    require(livekit.get("image") == "livekit/livekit-server:v1.13.3", "unexpected LiveKit image")
    require(livekit.get("network_mode") == "host", "LiveKit must use host networking")
    require(livekit.get("restart") == "unless-stopped", "LiveKit needs a restart policy")
    require(not livekit.get("ports"), "host-networked LiveKit must not declare port mappings")
    config = str(livekit.get("environment", {}).get("LIVEKIT_CONFIG", ""))
    for required in ("port: 7880", "tcp_port: 7881", "udp_port: 7882", "tls_port: 5349"):
        require(required in config, f"LiveKit configuration is missing {required}")
    mounts = {str(mount.get("target", "")): mount for mount in livekit.get("volumes", [])}
    for target in ("/run/secrets/livekit-turn-cert.pem", "/run/secrets/livekit-turn-key.pem"):
        require(target in mounts, f"LiveKit is missing the {target} mount")
        require(mounts[target].get("read_only") is True, f"LiveKit mount {target} must be read-only")
    require(
        services["api"].get("environment", {}).get("KAEDE_VOICE_ENABLED") == "true",
        "voice topology did not enable voice in the API",
    )


def validate_development(model: dict[str, Any], *, https_port: str) -> None:
    services = model.get("services", {})
    require(services, "rendered development model contains no services")
    expected = {
        "alpha-api": f"https://media.alpha.localhost:{https_port}",
        "beta-api": f"https://media.beta.localhost:{https_port}",
    }
    for name, origin in expected.items():
        require(
            services[name].get("environment", {}).get("KAEDE_MEDIA_PUBLIC_BASE_URL") == origin,
            f"{name} media URL does not follow KAEDE_DEV_HTTPS_PORT",
        )
    upload_origins = services["frontend"].get("environment", {}).get(
        "KAEDE_MEDIA_UPLOAD_ORIGINS", ""
    )
    for origin in expected.values():
        require(origin in upload_origins, f"frontend CSP is missing development origin {origin}")


def validate(
    model: dict[str, Any],
    *,
    external_s3: bool,
    observability: bool,
    voice: bool = False,
) -> None:
    services = model.get("services", {})
    networks = model.get("networks", {})
    require(services, "rendered model contains no services")
    for name in ("data", "edge"):
        require(networks.get(name, {}).get("internal") is True, f"{name} network must be internal")
    require(
        networks.get("egress", {}).get("internal") is not True,
        "egress network cannot be internal",
    )

    validate_caddy_edge(services)
    validate_migration_startup(services)
    validate_hardening(services)
    validate_ports(services)
    validate_mounts(services)
    validate_secrets(services)
    validate_images(services)
    validate_lifecycle(services)

    require(
        services["api"].get("environment", {}).get("KAEDE_MEDIA_SCAN_ENABLED") == "true",
        "production API must not disable malware scanning",
    )
    require(
        bool(services["api"].get("environment", {}).get("KAEDE_EMAIL_BACKEND")),
        "production API must receive the selected email policy",
    )

    preflight_command = " ".join(services["preflight"].get("command", []))
    require(
        "validate_deploy_env.py" in preflight_command,
        "production preflight must run the deployment environment guard",
    )
    if external_s3:
        require("garage" not in services, "external-S3 topology unexpectedly includes Garage")
        require(
            services["api"]["environment"].get("KAEDE_MEDIA_STORAGE_BACKEND") == "s3",
            "external-S3 topology did not select the S3 backend",
        )
    else:
        require("garage" in services, "Garage topology is missing Garage")
        require(
            services["api"]["environment"].get("KAEDE_MEDIA_STORAGE_BACKEND") == "garage",
            "Garage topology did not select the Garage backend",
        )
    if observability:
        validate_observability(services)
    if voice:
        validate_voice(services)
    else:
        require("livekit" not in services, "voice-disabled topology unexpectedly includes LiveKit")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--dev-https-port", default="18443")
    parser.add_argument("--external-s3", action="store_true")
    parser.add_argument("--observability", action="store_true")
    parser.add_argument("--voice", action="store_true")
    arguments = parser.parse_args()
    model = json.load(sys.stdin)
    if arguments.development:
        validate_development(model, https_port=arguments.dev_https_port)
        print("rendered development Compose policy validation passed")
        return
    validate(
        model,
        external_s3=arguments.external_s3,
        observability=arguments.observability,
        voice=arguments.voice,
    )
    print("rendered Compose policy validation passed")


if __name__ == "__main__":
    try:
        main()
    except (ComposePolicyError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Compose policy error: {error}") from error
