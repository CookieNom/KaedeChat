#!/usr/bin/env python3
"""Run existing acceptance commands in a disposable Kubernetes cluster."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from dev import K3S_IMAGE, api_address, instances
from manage import apply, job, kubectl, run, wait_workload
from stack import ROOT, config_volume, read_env_file, render, resource, workload

COMMANDS = json.loads((Path(__file__).parent / "checks.json").read_text())


def test_job(
    template: dict, name: str, command: list[str], image: str, env: dict | None = None
) -> dict:
    obj = copy.deepcopy(template)
    obj["metadata"]["name"] = name
    pod = obj["spec"]["template"]["spec"]
    obj["spec"]["template"]["metadata"]["labels"]["app"] = name
    obj["spec"]["activeDeadlineSeconds"] = 1800
    container = pod["containers"][0]
    container.update(name=name, image=image, command=command)
    container["env"] = [{"name": k, "value": v} for k, v in (env or {}).items()]
    # Match the old acceptance checks' source contracts without mounting the
    # Docker socket or giving the test access to production credentials.
    pod.setdefault("volumes", []).append(
        {
            "name": "repository",
            "hostPath": {"path": "/kaede-source", "type": "Directory"},
        }
    )
    mounts = container.setdefault("volumeMounts", [])
    for source, target in (
        ("backend", "/workspace"),
        ("frontend", "/frontend"),
        ("desktop", "/desktop"),
        ("mobile", "/mobile"),
        ("sdk", "/sdk"),
    ):
        mounts.append(
            {
                "name": "repository",
                "mountPath": target,
                "subPath": source,
                "readOnly": source != "backend",
            }
        )
    for source in (
        ".env.example",
        ".env.s3.example",
        "deploy/reference.env.example",
        "deploy/.env.alpha",
        "deploy/.env.beta",
        "deploy/.env.schema",
        "deploy/nginx",
        "deploy/Caddyfile",
    ):
        mounts.append(
            {
                "name": "repository",
                "mountPath": "/repository/" + source,
                "subPath": source,
                "readOnly": True,
            }
        )
    if name.startswith("frontend"):
        mounts.append(
            {
                "name": "repository",
                "mountPath": "/deploy/Caddyfile",
                "subPath": "deploy/Caddyfile",
                "readOnly": True,
            }
        )
    if not name.startswith("frontend"):
        pod["securityContext"] = {"runAsUser": os.getuid(), "runAsGroup": os.getgid()}
    if name.startswith("frontend"):
        # Frontend image contains its own dependencies; /workspace must not hide them.
        mounts[:] = [m for m in mounts if m.get("mountPath") != "/workspace"]
    return obj


def run_check(action: str) -> None:
    name = f"kaede-check-{time.time_ns():x}"
    backend, frontend = f"kaede-check-backend:{name}", f"kaede-check-frontend:{name}"
    run(
        [
            "docker",
            "build",
            "--target",
            "development",
            "-t",
            backend,
            str(ROOT / "backend"),
        ]
    )
    if action in {"check", "test", "audit"}:
        run(
            [
                "docker",
                "build",
                "--target",
                "development",
                "-t",
                frontend,
                str(ROOT / "frontend"),
            ]
        )
    with tempfile.TemporaryDirectory(prefix="kaede-check-") as temporary:
        kubeconfig = Path(temporary) / "kubeconfig"
        cfg = {
            "kubeconfig": str(kubeconfig),
            "context": f"k3d-{name}",
            "namespace": "kaede-validation",
            "wait_seconds": 1200,
        }
        created = False
        try:
            run(
                [
                    "k3d",
                    "cluster",
                    "create",
                    name,
                    "--image",
                    K3S_IMAGE,
                    "--no-lb",
                    "--kubeconfig-update-default=false",
                    "--kubeconfig-switch-context=false",
                    "--volume",
                    f"{ROOT}:/kaede-source@server:0",
                    "--api-port",
                    api_address(),
                    "--k3s-arg",
                    "--disable=traefik,servicelb@server:0",
                    "--k3s-arg",
                    "--kubelet-arg=feature-gates=KubeletInUserNamespace=true@server:0",
                ]
            )
            created = True
            kubeconfig.write_text(run(["k3d", "kubeconfig", "get", name], capture=True))
            kubeconfig.chmod(0o600)
            images = (
                [backend, frontend]
                if action in {"check", "test", "audit"}
                else [backend]
            )
            run(["k3d", "image", "import", "--cluster", name, *images])
            if action.startswith("federation"):
                federation(cfg, backend, action.endswith("tls-check"), Path(temporary))
                return
            values = read_env_file(ROOT / "deploy/.env.schema")
            objects = render(
                values,
                cfg["namespace"],
                development=True,
                backend=backend,
                reload=False,
            )["items"]
            apply(
                cfg,
                [
                    o
                    for o in objects
                    if o["kind"]
                    in {
                        "Namespace",
                        "Secret",
                        "ConfigMap",
                        "Service",
                        "PersistentVolumeClaim",
                        "NetworkPolicy",
                    }
                ],
            )
            needed = {"postgres", "dragonfly"}
            if action in {"chat-check", "media-check"}:
                needed.add("garage")
            if action == "media-check":
                needed.add("clamav")
            stateful = [
                o
                for o in objects
                if o["kind"] == "StatefulSet" and o["metadata"]["name"] in needed
            ]
            apply(cfg, stateful)
            for obj in stateful:
                wait_workload(cfg, obj)
            jobs = {o["metadata"]["name"]: o for o in objects if o["kind"] == "Job"}
            extra = {
                "HOME": "/tmp",
                "KAEDE_GENERATED_OUTPUT": "/frontend/src/lib/generated",
                "TEST_REDIS_URL": values["KAEDE_DRAGONFLY_URL"],
                "KAEDE_SERVICE_ROLE": "api",
            }
            if action in {"chat-check", "media-check"}:
                job(cfg, jobs["migrate"])
                job(cfg, jobs["storage-init"])
            if action == "chat-check":
                worker = next(
                    o
                    for o in objects
                    if o["kind"] == "Deployment" and o["metadata"]["name"] == "worker"
                )
                apply(cfg, [worker])
                wait_workload(cfg, worker)
            if action == "voice-check":
                extra.update(
                    KAEDE_VOICE_ENABLED="true",
                    KAEDE_VOICE_PUBLIC_URL="ws://livekit-validation:7880",
                    KAEDE_VOICE_LIVEKIT_URL="http://livekit-validation:7880",
                    KAEDE_VOICE_API_KEY="LKvoicevalidation",
                    KAEDE_VOICE_API_SECRET="voice-validation-secret-000000000000000000000000",
                )
                livekit = workload(
                    "livekit-validation",
                    cfg["namespace"],
                    "livekit/livekit-server:v1.13.3",
                    ports=[7880],
                    env={
                        "LIVEKIT_CONFIG": json.dumps(
                            {
                                "port": 7880,
                                "bind_addresses": ["0.0.0.0"],
                                "rtc": {
                                    "tcp_port": 7881,
                                    "udp_port": 7882,
                                    "use_external_ip": False,
                                },
                                "keys": {
                                    extra["KAEDE_VOICE_API_KEY"]: extra[
                                        "KAEDE_VOICE_API_SECRET"
                                    ]
                                },
                            }
                        )
                    },
                )
                apply(cfg, livekit)
                wait_workload(cfg, livekit[-1])
            actions = {
                "check": ["backend-check", "frontend-check"],
                "test": ["backend-test", "frontend-test"],
                "audit": ["backend-audit", "frontend-audit"],
            }.get(action, [action])
            for item in actions:
                command = COMMANDS.get(item)
                if item == "migration":
                    extra["REVISION_MESSAGE"] = os.environ["REVISION_MESSAGE"]
                    command = [
                        "sh",
                        "-ec",
                        'alembic upgrade head && alembic revision --autogenerate -m "$REVISION_MESSAGE"',
                    ]
                if item == "backend-audit":
                    command = ["pip-audit", "--skip-editable"]
                if item == "frontend-audit":
                    command = ["pnpm", "audit", "--audit-level=moderate"]
                template = jobs["migrate"]
                test_env = extra
                if item.startswith("backend-") or item in {
                    "migration",
                    "migration-check",
                }:
                    template = copy.deepcopy(template)
                    template["spec"]["template"]["spec"]["containers"][0].pop(
                        "envFrom", None
                    )
                    test_env = {
                        key: values[key]
                        for key in (
                            "KAEDE_DOMAIN",
                            "KAEDE_ENVIRONMENT",
                            "KAEDE_SECRET_KEY",
                            "KAEDE_PROXY_SECRET",
                            "KAEDE_DATABASE_URL",
                            "KAEDE_DRAGONFLY_URL",
                        )
                    } | extra
                    test_env["KAEDE_SERVICE_ROLE"] = "full"
                job(
                    cfg,
                    test_job(
                        template,
                        item,
                        command,
                        frontend if item.startswith("frontend") else backend,
                        test_env,
                    ),
                )
        finally:
            if created:
                subprocess.run(
                    ["kubectl", "--kubeconfig", str(kubeconfig), "get", "pods", "-A"],
                    check=False,
                )
                run(["k3d", "cluster", "delete", name])


def federation(cfg: dict, backend: str, tls: bool, temporary: Path) -> None:
    sets = instances(True)
    tls_data = {}
    address = ""
    if tls:
        folder = temporary / "tls"
        folder.mkdir()
        run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{folder}:/tls",
                backend,
                "python",
                "-m",
                "scripts.generate_federation_tls",
            ]
        )
        tls_data = {
            p.name: base64.b64encode(p.read_bytes()).decode() for p in folder.iterdir()
        }
        cfg["namespace"] = "kaede-alpha"
        apply(cfg, [resource("Namespace", "kaede-alpha", "kaede-alpha")])
        nginx = (ROOT / "deploy/nginx/federation-validation.conf").read_text()
        nginx = nginx.replace(
            "alpha-caddy:80", "caddy.kaede-alpha.svc.cluster.local:80"
        ).replace("beta-caddy:80", "caddy.kaede-beta.svc.cluster.local:80")
        apply(
            cfg,
            [
                resource("Secret", "federation-tls", "kaede-alpha", data=tls_data),
                resource(
                    "ConfigMap", "tls-edge", "kaede-alpha", data={"default.conf": nginx}
                ),
            ],
        )
        edge = workload(
            "tls-edge",
            "kaede-alpha",
            "nginx:1.29.0-alpine",
            ports=[443],
            volumes={
                "config": config_volume("tls-edge", "/etc/nginx/conf.d"),
                "tls": {"path": "/tls", "secret": {"secretName": "federation-tls"}},
            },
        )
        apply(cfg, [o for o in edge if o["kind"] != "Deployment"])
        address = kubectl(
            cfg,
            "get",
            "service",
            "tls-edge",
            "-o",
            "jsonpath={.spec.clusterIP}",
            capture=True,
        )
    last_job = None
    deployed = []
    for namespace, values in sets.items():
        cfg["namespace"] = namespace
        values["KAEDE_SEARCH_ENABLED"] = "false"
        if tls:
            peer = "beta" if namespace.endswith("alpha") else "alpha"
            values["KAEDE_FEDERATION_PEER_OVERRIDES"] = json.dumps(
                {f"{peer}.localhost": f"https://{peer}.localhost"}
            )
            values["KAEDE_FEDERATION_CA_FILE"] = "/tls/ca.crt"
        objects = render(
            values, namespace, development=True, backend=backend, reload=False
        )["items"]
        apply(
            cfg,
            [
                o
                for o in objects
                if o["kind"]
                in {
                    "Namespace",
                    "Secret",
                    "ConfigMap",
                    "Service",
                    "PersistentVolumeClaim",
                    "NetworkPolicy",
                }
            ],
        )
        if tls:
            apply(cfg, [resource("Secret", "federation-tls", namespace, data=tls_data)])
            for obj in objects:
                if obj["kind"] in {"Deployment", "Job"}:
                    pod = obj["spec"]["template"]["spec"]
                    pod["hostAliases"] = [
                        {
                            "ip": address,
                            "hostnames": ["alpha.localhost", "beta.localhost"],
                        }
                    ]
                    pod.setdefault("volumes", []).append(
                        {"name": "tls", "secret": {"secretName": "federation-tls"}}
                    )
                    pod["containers"][0].setdefault("volumeMounts", []).append(
                        {"name": "tls", "mountPath": "/tls", "readOnly": True}
                    )
        stateful = [o for o in objects if o["kind"] == "StatefulSet"]
        apply(cfg, stateful)
        for obj in stateful:
            wait_workload(cfg, obj)
        for obj in objects:
            if obj["kind"] == "Job" and obj["metadata"]["name"] in {
                "migrate",
                "storage-init",
            }:
                job(cfg, obj)
                if obj["metadata"]["name"] == "migrate":
                    last_job = obj
        apps = [
            o
            for o in objects
            if o["kind"] == "Deployment"
            and o["metadata"]["name"]
            in {"api", "gateway", "worker", "scheduler", *(["caddy"] if tls else [])}
        ]
        apply(cfg, apps)
        deployed.append((dict(cfg), apps))
    if tls:
        edge_cfg = dict(cfg, namespace="kaede-alpha")
        apply(edge_cfg, [edge[-1]])
        wait_workload(edge_cfg, edge[-1])
    for instance_cfg, apps in deployed:
        for obj in apps:
            wait_workload(instance_cfg, obj)
    extra = {
        "KAEDE_DOMAIN": "validation.localhost",
        "KAEDE_ENVIRONMENT": "test",
        "KAEDE_SERVICE_ROLE": "full",
        "KAEDE_SECRET_KEY": sets["kaede-alpha"]["KAEDE_SECRET_KEY"],
        "KAEDE_DATABASE_URL": "postgresql+asyncpg://kaede:kaede@postgres.kaede-alpha.svc.cluster.local:5432/kaede",
        "KAEDE_DRAGONFLY_URL": "redis://:schema-validation-dragonfly-secret@dragonfly.kaede-alpha.svc.cluster.local:6379/0",
        "ALPHA_URL": "https://alpha.localhost"
        if tls
        else "http://api.kaede-alpha.svc.cluster.local:8000",
        "BETA_URL": "https://beta.localhost"
        if tls
        else "http://api.kaede-beta.svc.cluster.local:8000",
        "ALPHA_DATABASE_URL": "postgresql+asyncpg://kaede:kaede@postgres.kaede-alpha.svc.cluster.local:5432/kaede",
        "BETA_DATABASE_URL": "postgresql+asyncpg://kaede:kaede@postgres.kaede-beta.svc.cluster.local:5432/kaede",
        "BETA_DRAGONFLY_URL": "redis://:schema-validation-dragonfly-secret@dragonfly.kaede-beta.svc.cluster.local:6379/0",
        "BETA_BOT_GATEWAY_URL": "ws://api.kaede-beta.svc.cluster.local:8000/api/v1/bots/gateway",
    }
    # The federation gate intentionally reads both databases. Allow only its
    # namespace in this disposable cluster; production retains private data policy.
    for namespace in sets:
        cfg["namespace"] = namespace
        kubectl(cfg, "delete", "networkpolicy", "private-data")
    if tls:
        extra["TLS_CA_FILE"] = "/tls/ca.crt"
    job(
        cfg,
        test_job(
            last_job,
            "federation-check",
            ["python", "-m", "scripts.verify_federation"],
            backend,
            extra,
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "check",
            "test",
            "audit",
            "migration",
            *COMMANDS,
            "federation-check",
            "federation-tls-check",
        ],
    )
    run_check(parser.parse_args().action)
