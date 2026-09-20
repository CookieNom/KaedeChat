#!/usr/bin/env python3
"""Single-instance development, or explicit alpha/beta federation, on k3d."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from manage import run
from stack import ROOT, digest, read_env_file, render

K3S_IMAGE = "rancher/k3s:v1.36.4-k3s1"


def api_address() -> str:
    # k3d records a literal :0 in kubeconfig; reserve a real ephemeral port.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{listener.getsockname()[1]}"


def cluster_name(federation: bool = False) -> str:
    return f"kaede-{'federation' if federation else 'dev'}-{digest(str(ROOT))[:8]}"


def development_values(path: Path | None = None) -> dict:
    values = read_env_file(ROOT / "deploy/.env.schema")
    default_port = int(os.environ.get("DEV_HTTP_PORT", "28081"))
    values.update(
        KAEDE_DOMAIN="dev.localhost",
        KAEDE_CADDY_HOST_PORT="28081",
        KAEDE_API_HOST_PORT="28082",
        KAEDE_ENVIRONMENT="development",
        KAEDE_APP_URL=f"http://dev.localhost:{default_port}",
        KAEDE_MEDIA_PUBLIC_BASE_URL=f"http://media.dev.localhost:{default_port}",
        KAEDE_SEARCH_ENABLED="false",
        KAEDE_MEDIA_SCAN_ENABLED="false",
        KAEDE_VOICE_ENABLED="false",
        KAEDE_PUSH_RELAY_ENABLED="false",
    )
    selected = path or Path(os.environ.get("ENV_FILE", str(ROOT / ".env")))
    if selected.is_file():
        # Existing instances must never inherit disposable fixture credentials.
        values = read_env_file(selected)
    values.update(KAEDE_API_WORKERS="1", KAEDE_GATEWAY_WORKERS="1")
    return values


def instances(federation: bool, env_file: Path | None = None) -> dict[str, dict]:
    if not federation:
        return {"kaede-dev": development_values(env_file)}
    result = {}
    for name, other in (("alpha", "beta"), ("beta", "alpha")):
        values = (
            read_env_file(ROOT / "deploy/.env.schema")
            | {
                "KAEDE_MEDIA_SCAN_ENABLED": "false",
                "KAEDE_PUSH_RELAY_ENABLED": "false",
            }
            | development_values(ROOT / "deploy" / f".env.{name}")
        )
        for key in (
            "KAEDE_DATABASE_URL",
            "KAEDE_DRAGONFLY_URL",
            "KAEDE_MEDIA_S3_ENDPOINT",
            "KAEDE_SEARCH_URL",
        ):
            values[key] = values[key].replace(f"{name}-", "")
        # Each namespace has its own services and persistent data.
        values["KAEDE_DRAGONFLY_URL"] = (
            "redis://:schema-validation-dragonfly-secret@dragonfly:6379/0"
        )
        values["KAEDE_FEDERATION_PEER_OVERRIDES"] = json.dumps(
            {f"{other}.localhost": f"http://api.kaede-{other}.svc.cluster.local:8000"}
        )
        port = int(os.environ.get("DEV_HTTP_PORT", "28081")) + (
            0 if name == "alpha" else 100
        )
        values["KAEDE_APP_URL"] = f"http://{name}.localhost:{port}"
        values["KAEDE_MEDIA_PUBLIC_BASE_URL"] = f"http://media.{name}.localhost:{port}"
        values["KAEDE_EDGE_SECRET"] = "kaede-validation-edge-secret-0001"
        result[f"kaede-{name}"] = values
    return result


def manifests(
    federation: bool, env_file: Path | None = None, reload: bool = True
) -> list[dict]:
    items = []
    for namespace, values in instances(federation, env_file).items():
        objects = render(values, namespace, development=True, reload=reload)["items"]
        for obj in objects:
            # Pin the exposed service ports inside the disposable k3d node.
            # Docker binds their corresponding host ports to 127.0.0.1 only.
            if obj["kind"] == "Service" and obj["metadata"]["name"] in {"caddy", "api"}:
                base = 30080 if namespace != "kaede-beta" else 30180
                obj["spec"]["type"] = "NodePort"
                obj["spec"]["ports"] = [obj["spec"]["ports"][0]]
                obj["spec"]["ports"][0]["nodePort"] = (
                    base if obj["metadata"]["name"] == "caddy" else base + 2
                )
        items.extend(objects)
    return items


def create_cluster(federation: bool, env_file: Path | None = None) -> None:
    name = cluster_name(federation)
    existing = json.loads(
        run(["k3d", "cluster", "list", "-o", "json"], capture=True) or "[]"
    )
    if any(c["name"] == name for c in existing):
        run(["k3d", "cluster", "start", name])
        return
    args = [
        "k3d",
        "cluster",
        "create",
        name,
        "--image",
        K3S_IMAGE,
        "--servers",
        "1",
        "--agents",
        "0",
        "--no-lb",
        "--kubeconfig-update-default=false",
        "--kubeconfig-switch-context=false",
        "--api-port",
        api_address(),
        "--k3s-arg",
        "--disable=traefik,servicelb@server:0",
        "--k3s-arg",
        "--cluster-cidr=10.52.0.0/16@server:0",
        "--k3s-arg",
        "--service-cidr=10.53.0.0/16@server:0",
        "--k3s-arg",
        "--cluster-dns=10.53.0.10@server:0",
        "--k3s-arg",
        "--kubelet-arg=feature-gates=KubeletInUserNamespace=true@server:0",
    ]
    for index, (namespace, values) in enumerate(
        instances(federation, env_file).items()
    ):
        host_port = (
            int(
                os.environ.get(
                    "DEV_HTTP_PORT", values.get("KAEDE_CADDY_HOST_PORT", "28081")
                )
            )
            + index * 100
        )
        api_host_port = (
            host_port + 1
            if federation or "DEV_HTTP_PORT" in os.environ
            else int(values.get("KAEDE_API_HOST_PORT", str(host_port + 1)))
        )
        node_port = 30080 + index * 100
        args += [
            "--port",
            f"127.0.0.1:{host_port}:{node_port}@server:0:direct",
            "--port",
            f"127.0.0.1:{api_host_port}:{node_port + 2}@server:0:direct",
        ]
        if values.get("KAEDE_VOICE_ENABLED") == "true":
            for key, default, protocol in (
                ("LIVEKIT_RTC_TCP_PORT", "7881", "tcp"),
                ("LIVEKIT_RTC_UDP_PORT", "7882", "udp"),
                ("LIVEKIT_TURN_TLS_PORT", "5349", "tcp"),
                ("KAEDE_TURN_UDP_PORT", "13478", "udp"),
            ):
                port = values.get(key, default)
                args += ["--port", f"{port}:{port}/{protocol}@server:0:direct"]
            for key in ("LIVEKIT_TURN_CERT_PATH", "LIVEKIT_TURN_KEY_PATH"):
                if values.get(key):
                    args += [
                        "--volume",
                        f"{Path(values[key]).resolve()}:{values[key]}:ro@server:0",
                    ]
        if values.get("KAEDE_PHOTODNA_ENABLED") == "true" and values.get(
            "PHOTODNA_EDGEHASHGENERATOR"
        ):
            path = values["PHOTODNA_EDGEHASHGENERATOR"]
            args += ["--volume", f"{path}:{path}:ro@server:0"]
    run(args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["up", "down", "logs", "render", "metadata", "cluster", "build-image"],
    )
    parser.add_argument("--federation", action="store_true")
    parser.add_argument("--service", choices=["backend", "frontend"])
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args()
    name = cluster_name(args.federation)
    unit = f"{name}-tilt.service"
    if args.action == "build-image":
        if not args.service:
            raise ValueError("build-image requires --service")
        image = os.environ["EXPECTED_REF"]
        command = ["docker", "build", "--target", "development", "-t", image]
        if args.service == "frontend":
            values = next(iter(instances(args.federation, args.env_file).values()))
            if args.federation:
                values["KAEDE_MEDIA_UPLOAD_ORIGINS"] = " ".join(
                    v["KAEDE_MEDIA_PUBLIC_BASE_URL"] for v in instances(True).values()
                )
            for key, value in values.items():
                if key.startswith("KAEDE_LEGAL_") or key in {
                    "KAEDE_MEDIA_PUBLIC_BASE_URL",
                    "KAEDE_MEDIA_UPLOAD_ORIGINS",
                    "KAEDE_MEDIA_S3_ADDRESSING_STYLE",
                    "KAEDE_MEDIA_ATTACHMENTS_BUCKET",
                    "KAEDE_LANDING_PAGE",
                }:
                    command += ["--build-arg", f"{key}={value}"]
        run([*command, str(ROOT / args.service)])
        run(["k3d", "image", "import", "--cluster", name, image])
    elif args.action == "render":
        print(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "List",
                    "items": manifests(args.federation, args.env_file),
                }
            )
        )
    elif args.action == "metadata":
        print(
            json.dumps(
                {
                    "cluster": name,
                    "context": f"k3d-{name}",
                    "namespaces": list(instances(args.federation, args.env_file)),
                }
            )
        )
    elif args.action == "logs":
        run(["journalctl", "--user", "--unit", unit, "--follow", "--lines=100"])
    elif args.action == "down":
        # Stop node containers instead of deleting namespaces/PVCs. Tilt down
        # deletes resources, so it is deliberately not used for this command.
        if (
            subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit]
            ).returncode
            == 0
        ):
            run(["systemctl", "--user", "stop", unit])
        run(["k3d", "cluster", "stop", name])
    else:
        create_cluster(args.federation, args.env_file)
        target = ROOT / (
            ".kaede-federation-kubeconfig"
            if args.federation
            else ".kaede-dev-kubeconfig"
        )
        target.touch(mode=0o600, exist_ok=True)
        target.chmod(0o600)
        target.write_text(run(["k3d", "kubeconfig", "get", name], capture=True))
        if args.action == "up":
            if (
                subprocess.run(
                    ["systemctl", "--user", "is-active", "--quiet", unit]
                ).returncode
                == 0
            ):
                print("Tilt is already running in the background.")
                return
            env = dict(os.environ, KUBECONFIG=str(target))
            command = [
                shutil.which("tilt") or "tilt",
                "up",
                "--file",
                str(ROOT / "Tiltfile"),
                "--port",
                os.environ.get("TILT_PORT", "10351" if args.federation else "10350"),
                "--stream=true",
                "--",
                "--federation=" + str(args.federation).lower(),
            ]
            if args.env_file:
                command += ["--env-file=" + str(args.env_file.resolve())]
            if args.foreground:
                subprocess.run(command, env=env, check=True)
            else:
                run(
                    [
                        "systemd-run",
                        "--user",
                        "--collect",
                        "--unit",
                        unit,
                        "--working-directory",
                        str(ROOT),
                        "--setenv",
                        "PATH=" + env["PATH"],
                        "--setenv",
                        "KUBECONFIG=" + str(target),
                        *command,
                    ]
                )
                print("Tilt started in the background. Builds continue asynchronously.")
                print(
                    "Logs: make dev"
                    + ("-federation" if args.federation else "")
                    + "-logs"
                )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Development setup failed: {error}", file=sys.stderr)
        sys.exit(1)
