"""Shared, dependency-free Kubernetes resources for production and development.

The output is a Kubernetes List (JSON is valid YAML), consumed by kubectl and
Tilt. Credentials are separate per-role Secrets, never ConfigMaps.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from urllib.parse import urlparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))
from validate_deploy_env import read_env_file  # noqa: E402

ROLE_KEYS = json.loads((Path(__file__).parent / "environment-keys.json").read_text())
IMAGES = {
    "postgres": "postgres:16.14-alpine",
    "dragonfly": "docker.dragonflydb.io/dragonflydb/dragonfly:v1.39.0",
    "garage": "dxflrs/garage:v2.3.0",
    "clamav": "clamav/clamav:1.5.3",
    "meilisearch": "getmeili/meilisearch:v1.51.0",
    "caddy": "caddy:2.11.4-alpine",
    "livekit": "livekit/livekit-server:v1.13.3",
    "prometheus": "prom/prometheus:v3.13.1",
    "grafana": "grafana/grafana:13.1.0",
    "loki": "grafana/loki:3.7.3",
}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def resource(kind: str, name: str, namespace: str, **fields: object) -> dict:
    api = {
        "Deployment": "apps/v1",
        "StatefulSet": "apps/v1",
        "Job": "batch/v1",
        "NetworkPolicy": "networking.k8s.io/v1",
        "Ingress": "networking.k8s.io/v1",
    }.get(kind, "v1")
    metadata = {"name": name, "labels": {"app.kubernetes.io/part-of": "kaede"}}
    if kind != "Namespace":
        metadata["namespace"] = namespace
    return {"apiVersion": api, "kind": kind, "metadata": metadata, **fields}


def env_secret(name: str, namespace: str, values: dict) -> dict:
    return resource(
        "Secret",
        name,
        namespace,
        type="Opaque",
        data={k: base64.b64encode(str(v).encode()).decode() for k, v in values.items()},
    )


def service(name: str, namespace: str, ports: list[int]) -> dict:
    return resource(
        "Service",
        name,
        namespace,
        spec={
            "selector": {"app": name},
            "ports": [{"name": f"tcp-{p}", "port": p, "targetPort": p} for p in ports],
        },
    )


def workload(
    name: str,
    namespace: str,
    image: str,
    *,
    command: list[str] | None = None,
    env: dict | None = None,
    ports: list[int] = (),
    volumes: dict | None = None,
    stateful: bool = False,
    replicas: int = 1,
    hardened: bool = False,
    development: bool = False,
) -> list[dict]:
    container = {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}},
    }
    if command:
        container["command"] = command
    result = []
    if env:
        secret_name = f"{name}-env-{digest(env)}"
        result.append(env_secret(secret_name, namespace, env))
        container["envFrom"] = [{"secretRef": {"name": secret_name}}]
    pod = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "terminationGracePeriodSeconds": 120,
        "containers": [container],
    }
    if hardened:
        container["securityContext"] = {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": not development,
            "capabilities": {"drop": ["ALL"]},
        }
        if not development:
            pod["securityContext"] = {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
            }
        volumes = {**(volumes or {}), "tmp": {"path": "/tmp", "emptyDir": {}}}
    if volumes:
        pod["volumes"] = []
        container["volumeMounts"] = []
        for volume_name, volume in volumes.items():
            source = {
                k: v
                for k, v in volume.items()
                if k not in {"path", "readOnly", "subPath"}
            }
            pod["volumes"].append({"name": volume_name, **source})
            mount = {"name": volume_name, "mountPath": volume["path"]}
            mount.update({k: volume[k] for k in ("readOnly", "subPath") if k in volume})
            container["volumeMounts"].append(mount)
    if ports:
        container["ports"] = [{"containerPort": p} for p in ports]
        container["readinessProbe"] = {
            "tcpSocket": {"port": ports[0]},
            "periodSeconds": 3,
        }
        result.append(service(name, namespace, list(ports)))
    template = {
        "metadata": {"labels": {"app": name, "app.kubernetes.io/part-of": "kaede"}},
        "spec": pod,
    }
    spec = {
        "replicas": replicas,
        "selector": {"matchLabels": {"app": name}},
        "template": template,
    }
    if stateful:
        spec["serviceName"] = name
        result.append(resource("StatefulSet", name, namespace, spec=spec))
    else:
        spec["strategy"] = (
            {"type": "Recreate"}
            if name == "scheduler"
            else {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            }
        )
        result.append(resource("Deployment", name, namespace, spec=spec))
    return result


def config_volume(name: str, path: str) -> dict:
    return {"path": path, "configMap": {"name": name}, "readOnly": True}


def persistent(name: str, path: str) -> dict:
    return {"path": path, "persistentVolumeClaim": {"claimName": name}}


def role_environment(values: dict, role: str) -> dict:
    result = (
        dict(values)
        if role == "preflight"
        else {k: v for k, v in values.items() if k in ROLE_KEYS[role]}
    )
    if role != "scheduler":
        result["KAEDE_SERVICE_ROLE"] = {"migrate": "migration"}.get(role, role)
    if role == "gateway":
        result["KAEDE_SECRET_KEY"] = values["KAEDE_GATEWAY_SECRET_KEY"]
    if role in {"api", "worker", "migrate", "preflight"}:
        result["KAEDE_VOICE_API_KEY"] = values.get("LIVEKIT_API_KEY", "")
        result["KAEDE_VOICE_API_SECRET"] = values.get("LIVEKIT_API_SECRET", "")
        result["KAEDE_MEDIA_CLAMAV_HOST"] = "clamav"
    if role == "api":
        result["KAEDE_PUSH_RELAY_VOIP_AVAILABLE"] = str(
            bool(values.get("KAEDE_PUSH_RELAY_APNS_KEY_B64"))
        ).lower()
    return result


def render(
    values: dict,
    namespace: str,
    *,
    development: bool = False,
    backend: str = "kaede-backend:development",
    frontend: str = "kaede-frontend:development",
    storage_class: str = "local-path",
    storage_size: str = "20Gi",
    edge_port: int = 0,
    api_port: int = 0,
    reload: bool = True,
) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]", namespace):
        raise ValueError("namespace must be a Kubernetes DNS label")
    values = dict(values)
    values.setdefault("KAEDE_MEDIA_STORAGE_BACKEND", "garage")
    values.setdefault("KAEDE_MEDIA_S3_ENDPOINT", "http://garage:3900")
    if development:
        values.update(KAEDE_API_WORKERS="1", KAEDE_GATEWAY_WORKERS="1")
    voice = values.get("KAEDE_VOICE_ENABLED") == "true"
    if voice:
        values["KAEDE_VOICE_LIVEKIT_URL"] = (
            f"http://livekit:{values.get('LIVEKIT_CONTROL_PORT', '7880')}"
        )
    items = [resource("Namespace", namespace, namespace)]
    claims = set()

    def add(name: str, image: str, **kwargs: object) -> dict:
        objects = workload(name, namespace, image, development=development, **kwargs)
        items.extend(objects)
        obj = objects[-1]
        for vol in obj["spec"]["template"]["spec"].get("volumes", []):
            if "persistentVolumeClaim" in vol:
                claims.add(vol["persistentVolumeClaim"]["claimName"])
        return obj

    def config(name: str, data: dict) -> str:
        full_name = f"{name}-{digest(data)}"
        items.append(resource("ConfigMap", full_name, namespace, data=data))
        return full_name

    pg = add(
        "postgres",
        IMAGES["postgres"],
        stateful=True,
        ports=[5432],
        env={
            "POSTGRES_DB": "kaede",
            "POSTGRES_USER": "kaede",
            "POSTGRES_PASSWORD": values["POSTGRES_PASSWORD"],
            "PGDATA": "/var/lib/postgresql/data/pgdata",
        },
        volumes={"data": persistent("postgres-data", "/var/lib/postgresql/data")},
    )
    pg["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] = {
        "exec": {"command": ["pg_isready", "-U", "kaede", "-d", "kaede"]},
        "periodSeconds": 3,
    }
    add(
        "dragonfly",
        IMAGES["dragonfly"],
        stateful=True,
        ports=[6379],
        command=[
            "/usr/local/bin/dragonfly",
            "--dir=/data",
            "--dbfilename=dump",
            "--snapshot_cron=*/5 * * * *",
            "--requirepass=$(REDISCLI_AUTH)",
            "--proactor_threads=1" if development else "--proactor_threads=2",
        ],
        env={"REDISCLI_AUTH": values["DRAGONFLY_PASSWORD"]},
        volumes={"data": persistent("dragonfly-data", "/data")},
    )
    if values["KAEDE_MEDIA_STORAGE_BACKEND"] == "garage":
        garage_config = config(
            "garage", {"garage.toml": (ROOT / "deploy/garage.toml").read_text()}
        )
        add(
            "garage",
            IMAGES["garage"],
            stateful=True,
            ports=[3900, 3901, 3903],
            command=["/garage", "server", "--single-node", "--default-bucket"],
            env={
                "GARAGE_RPC_SECRET": values["GARAGE_RPC_SECRET"],
                "GARAGE_ADMIN_TOKEN": values["GARAGE_ADMIN_TOKEN"],
                "GARAGE_DEFAULT_ACCESS_KEY": values["KAEDE_MEDIA_S3_ACCESS_KEY"],
                "GARAGE_DEFAULT_SECRET_KEY": values["KAEDE_MEDIA_S3_SECRET_KEY"],
                "GARAGE_DEFAULT_BUCKET": values.get(
                    "KAEDE_MEDIA_ATTACHMENTS_BUCKET", "kaede-attachments"
                ),
                "GARAGE_RPC_PUBLIC_ADDR": "garage:3901",
            },
            volumes={
                "config": {
                    **config_volume(garage_config, "/etc/garage.toml"),
                    "subPath": "garage.toml",
                },
                "meta": persistent("garage-meta", "/var/lib/garage/meta"),
                "data": persistent("garage-data", "/var/lib/garage/data"),
            },
        )
    if values.get("KAEDE_MEDIA_SCAN_ENABLED", "true") == "true":
        add(
            "clamav",
            IMAGES["clamav"],
            stateful=True,
            ports=[3310],
            volumes={"data": persistent("clamav-data", "/var/lib/clamav")},
        )
    if (
        values.get("KAEDE_SEARCH_ENABLED") == "true"
        and urlparse(values.get("KAEDE_SEARCH_URL", "http://meilisearch:7700")).hostname
        == "meilisearch"
    ):
        add(
            "meilisearch",
            IMAGES["meilisearch"],
            stateful=True,
            ports=[7700],
            env={
                "MEILI_ENV": "production",
                "MEILI_MASTER_KEY": values["KAEDE_SEARCH_MASTER_KEY"],
                "MEILI_NO_ANALYTICS": "true",
            },
            volumes={"data": persistent("meilisearch-data", "/meili_data")},
        )

    app_objects = {}
    for role in ("api", "gateway", "worker", "scheduler", "migrate", "preflight"):
        env = role_environment(values, role)
        volumes = {}
        if (
            values.get("KAEDE_PHOTODNA_ENABLED") == "true"
            and role in {"api", "worker", "preflight"}
            and values.get("PHOTODNA_EDGEHASHGENERATOR")
        ):
            env["PHOTODNA_EDGEHASHGENERATOR"] = "/opt/photodna"
            volumes["photodna"] = {
                "path": "/opt/photodna",
                "readOnly": True,
                "hostPath": {
                    "path": values["PHOTODNA_EDGEHASHGENERATOR"],
                    "type": "Directory",
                },
            }
        if role == "worker":
            volumes["models"] = persistent("rembg-models", "/models/rembg")
            env["REMBG_HOME"] = "/models/rembg"
        ports = [8000] if role == "api" else [8001] if role == "gateway" else []
        if ports:
            module = "app.main:app" if role == "api" else "app.gateway:app"
            command = [
                "uvicorn",
                module,
                "--host",
                "0.0.0.0",
                "--port",
                str(ports[0]),
                "--no-access-log",
                "--ws-max-size",
                "1048576" if role == "api" else "65536",
                "--timeout-graceful-shutdown",
                "60",
            ]
            if development and reload:
                command += ["--reload"]
            elif role == "api":
                command += ["--workers", values.get("KAEDE_API_WORKERS", "4")]
        else:
            command = {
                "worker": ["taskiq", "worker", "app.tasks:broker"],
                "scheduler": ["taskiq", "scheduler", "app.scheduler:scheduler"],
                "migrate": ["sh", "-ec", "alembic upgrade head && kaede bootstrap"],
                "preflight": ["kaede", "preflight", *(["--voice"] if voice else [])],
            }[role]
            if role == "scheduler" and development and reload:
                command = [
                    "watchfiles",
                    "--filter",
                    "python",
                    "taskiq scheduler app.scheduler:scheduler",
                    "/workspace/app",
                ]
            if role == "worker" and development:
                command += ["--workers", "1"]
                if reload:
                    command += ["--reload"]
        obj = add(
            role,
            backend,
            command=command,
            env=env,
            ports=ports,
            volumes=volumes,
            replicas=int(values.get("KAEDE_GATEWAY_WORKERS", "2"))
            if role == "gateway"
            else 1,
            hardened=True,
        )
        if "photodna" in volumes:
            obj["spec"]["template"]["spec"].setdefault("securityContext", {})[
                "supplementalGroups"
            ] = [int(values.get("OPERATOR_ENV_GID", "1000"))]
        app_objects[role] = obj
        container = obj["spec"]["template"]["spec"]["containers"][0]
        if ports:
            container["readinessProbe"] = {
                "httpGet": {"path": "/health/ready", "port": ports[0]},
                "periodSeconds": 3,
            }
            container["startupProbe"] = {
                "httpGet": {"path": "/health/live", "port": ports[0]},
                "periodSeconds": 3,
                "failureThreshold": 60,
            }
            container["lifecycle"] = {
                "preStop": {
                    "exec": {
                        "command": [
                            "python",
                            "-c",
                            f"import time; time.sleep({30 if role == 'gateway' else 5})",
                        ]
                    }
                }
            }
        if role in {"migrate", "preflight"}:
            obj["kind"], obj["apiVersion"] = "Job", "batch/v1"
            obj["spec"] = {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 600,
                "template": obj["spec"]["template"],
            }
            obj["spec"]["template"]["spec"]["restartPolicy"] = "Never"

    storage_job = json.loads(json.dumps(app_objects["preflight"]))
    storage_job["metadata"]["name"] = "storage-init"
    storage_job["spec"]["template"]["metadata"]["labels"]["app"] = "storage-init"
    storage_job["spec"]["template"]["spec"]["containers"][0]["command"] = [
        "python",
        "-m",
        "app.media.init_buckets",
    ]
    items.append(storage_job)

    add(
        "frontend",
        frontend,
        ports=[5173 if development else 8080],
        env={"__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": values["KAEDE_DOMAIN"]}
        if development
        else None,
    )
    caddy = (
        (ROOT / "deploy/Caddyfile")
        .read_text()
        .replace(
            "\t\troot * /srv/frontend/current\n\t\ttry_files {path} /index.html\n\t\tfile_server",
            f"\t\treverse_proxy frontend:{5173 if development else 8080}",
        )
    )
    caddy = caddy.replace("host.docker.internal:", "livekit:")
    if development:
        caddy = caddy.replace(
            "\t@untrusted_edge not header X-Kaede-Edge-Secret {$KAEDE_EDGE_SECRET}\n\trespond @untrusted_edge 403\n",
            "",
        )
    caddy += "\n:8081 {\n respond /health 200\n}\n"
    edge_config = config("caddy", {"Caddyfile": caddy})
    edge = add(
        "caddy",
        IMAGES["caddy"],
        ports=[80, 8081],
        env={
            key: values.get(key, "false" if key == "KAEDE_VOICE_ENABLED" else "")
            for key in (
                "KAEDE_DOMAIN",
                "KAEDE_PROXY_SECRET",
                "KAEDE_EDGE_SECRET",
                "KAEDE_VOICE_ENABLED",
                "LIVEKIT_CONTROL_PORT",
            )
        },
        volumes={
            "config": config_volume(edge_config, "/etc/caddy"),
            "data": persistent("caddy-data", "/data"),
            "state": {"path": "/config", "emptyDir": {}},
        },
    )
    edge["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] = {
        "httpGet": {"path": "/health", "port": 8081}
    }
    # Host nginx reaches a stable Service address through a loopback-only host
    # bridge. No public NodePort or LoadBalancer is created implicitly.
    for name, host_port, container_port in (
        ("caddy", edge_port, 80),
        ("api", api_port, 8000),
        (
            "grafana",
            int(values.get("KAEDE_GRAFANA_HOST_PORT", "18084"))
            if edge_port
            and (
                values.get("KAEDE_OBSERVABILITY_ENABLED") == "true"
                or "observability" in values.get("COMPOSE_PROFILES", "").split(",")
            )
            else 0,
            3000,
        ),
    ):
        if host_port:
            bridge_config = config(
                f"{name}-bridge",
                {
                    "Caddyfile": (
                        "{\n admin off\n auto_https off\n}\n"
                        f"http://:{host_port} {{\n bind 127.0.0.1\n reverse_proxy {name}.{namespace}.svc.cluster.local:{container_port} {{\n"
                        " header_up X-Forwarded-For {http.request.header.X-Forwarded-For}\n"
                        " header_up X-Forwarded-Proto {http.request.header.X-Forwarded-Proto}\n }\n}\n"
                    )
                },
            )
            bridge = add(
                f"{name}-bridge",
                IMAGES["caddy"],
                volumes={
                    "config": config_volume(bridge_config, "/etc/caddy"),
                    "state": {"path": "/config", "emptyDir": {}},
                    "data": {"path": "/data", "emptyDir": {}},
                },
            )
            bridge["spec"]["strategy"] = {"type": "Recreate"}
            bridge["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] = {
                "tcpSocket": {"host": "127.0.0.1", "port": host_port},
                "periodSeconds": 3,
            }
            bridge["spec"]["template"]["spec"].update(
                hostNetwork=True, dnsPolicy="ClusterFirstWithHostNet"
            )

    if voice:
        control, tcp, udp, turn_tls, turn_udp = [
            int(values.get(k, d))
            for k, d in (
                ("LIVEKIT_CONTROL_PORT", "7880"),
                ("LIVEKIT_RTC_TCP_PORT", "7881"),
                ("LIVEKIT_RTC_UDP_PORT", "7882"),
                ("LIVEKIT_TURN_TLS_PORT", "5349"),
                ("KAEDE_TURN_UDP_PORT", "13478"),
            )
        ]
        livekit_config = {
            "port": control,
            "bind_addresses": ["0.0.0.0"],
            "rtc": {"tcp_port": tcp, "udp_port": udp, "use_external_ip": True},
            "keys": {values["LIVEKIT_API_KEY"]: values["LIVEKIT_API_SECRET"]},
            "webhook": {
                "api_key": values["LIVEKIT_API_KEY"],
                "urls": [
                    f"http://api.{namespace}.svc.cluster.local:8000/internal/livekit/webhook"
                ],
            },
            "room": {"empty_timeout": 300},
        }
        tls = {}
        if values.get("LIVEKIT_TURN_CERT_PATH") and values.get("LIVEKIT_TURN_KEY_PATH"):
            livekit_config["turn"] = {
                "enabled": True,
                "domain": values["KAEDE_DOMAIN"],
                "udp_port": turn_udp,
                "tls_port": turn_tls,
                "cert_file": "/tls/tls.crt",
                "key_file": "/tls/tls.key",
            }
            tls = {
                "cert": {
                    "path": "/tls/tls.crt",
                    "readOnly": True,
                    "hostPath": {
                        "path": values["LIVEKIT_TURN_CERT_PATH"],
                        "type": "File",
                    },
                },
                "key": {
                    "path": "/tls/tls.key",
                    "readOnly": True,
                    "hostPath": {
                        "path": values["LIVEKIT_TURN_KEY_PATH"],
                        "type": "File",
                    },
                },
            }
        lk = add(
            "livekit",
            IMAGES["livekit"],
            ports=[control],
            env={"LIVEKIT_CONFIG": json.dumps(livekit_config)},
            volumes=tls,
        )
        lk["spec"]["strategy"] = {"type": "Recreate"}
        lk["spec"]["template"]["spec"].update(
            hostNetwork=True, dnsPolicy="ClusterFirstWithHostNet"
        )

    if values.get(
        "KAEDE_OBSERVABILITY_ENABLED"
    ) == "true" or "observability" in values.get("COMPOSE_PROFILES", "").split(","):
        prom_config = config(
            "prometheus",
            {
                p.name: p.read_text()
                for p in (ROOT / "deploy/observability").glob("*.yml")
            },
        )
        add(
            "prometheus",
            IMAGES["prometheus"],
            ports=[9090],
            stateful=True,
            volumes={
                "config": config_volume(prom_config, "/etc/prometheus"),
                "data": persistent("prometheus-data", "/prometheus"),
            },
        )
        add(
            "loki",
            IMAGES["loki"],
            ports=[3100],
            command=["/usr/bin/loki", "-config.file=/etc/loki/local-config.yaml"],
        )
        grafana_volumes = {"data": persistent("grafana-data", "/var/lib/grafana")}
        for subpath in (
            "provisioning/datasources",
            "provisioning/dashboards",
            "dashboards",
        ):
            folder = ROOT / "deploy/observability/grafana" / subpath
            cm = config(
                "grafana-" + folder.name,
                {p.name: p.read_text() for p in folder.iterdir() if p.is_file()},
            )
            path = (
                "/etc/grafana/" + subpath
                if subpath.startswith("provisioning")
                else "/var/lib/grafana/dashboards"
            )
            grafana_volumes[subpath.replace("/", "-")] = config_volume(cm, path)
        add(
            "grafana",
            IMAGES["grafana"],
            ports=[3000],
            stateful=True,
            volumes=grafana_volumes,
            env={
                "GF_SECURITY_ADMIN_USER": values.get("GRAFANA_ADMIN_USER", "admin"),
                "GF_SECURITY_ADMIN_PASSWORD": values["GRAFANA_ADMIN_PASSWORD"],
                "GF_USERS_ALLOW_SIGN_UP": "false",
                "GF_AUTH_ANONYMOUS_ENABLED": "false",
            },
        )

    for claim in sorted(claims):
        items.append(
            resource(
                "PersistentVolumeClaim",
                claim,
                namespace,
                spec={
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": storage_class,
                    "resources": {"requests": {"storage": storage_size}},
                },
            )
        )
    # Application credentials are never available via the Kubernetes API from a pod.
    # Keep databases private to this instance; edge access is through Caddy only.
    items.append(
        resource(
            "NetworkPolicy",
            "private-data",
            namespace,
            spec={
                "podSelector": {
                    "matchExpressions": [
                        {
                            "key": "app",
                            "operator": "In",
                            "values": [
                                "postgres",
                                "dragonfly",
                                "garage",
                                "meilisearch",
                                "clamav",
                            ],
                        }
                    ]
                },
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": {}}]}],
            },
        )
    )
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--namespace", default="kaede-dev")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--backend-image", default="kaede-backend:development")
    parser.add_argument("--frontend-image", default="kaede-frontend:development")
    args = parser.parse_args()
    print(
        json.dumps(
            render(
                read_env_file(args.env_file),
                args.namespace,
                development=args.development,
                backend=args.backend_image,
                frontend=args.frontend_image,
            )
        )
    )


if __name__ == "__main__":
    main()
