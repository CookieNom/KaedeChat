#!/usr/bin/env python3
"""Kaede Kubernetes deployment commands. Every call selects a cluster explicitly."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
import subprocess
import sys
import tempfile
from pathlib import Path

from stack import ROOT, digest, read_env_file, render, resource
from validate_deploy_env import validate_file_permissions, validate_values


def run(command: list[str], *, data: str | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        input=data,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        check=True,
    )
    return result.stdout or ""


def settings(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(
            f"Missing {path}; run make setup to select the production cluster, namespace, and image repository"
        )
    cfg = json.loads(path.read_text())
    for key in (
        "kubeconfig",
        "context",
        "namespace",
        "cluster_uid",
        "image_repository",
    ):
        if not cfg.get(key):
            raise ValueError(f"{path}: {key} is required")
    if cfg["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}:
        raise ValueError("Use a dedicated namespace for Kaede")
    if cfg.get("image_transport", "local") not in {"local", "registry"}:
        raise ValueError("image_transport must be local or registry")
    if (
        not isinstance(cfg.get("image_repository"), str)
        or not cfg["image_repository"].strip()
    ):
        raise ValueError("image_repository must be a nonempty image prefix")
    return cfg


def kubectl(
    cfg: dict, *args: str, data: str | None = None, capture: bool = False
) -> str:
    return run(
        [
            "kubectl",
            "--kubeconfig",
            str(Path(cfg["kubeconfig"]).expanduser()),
            "--context",
            cfg["context"],
            "--namespace",
            cfg["namespace"],
            *args,
        ],
        data=data,
        capture=capture,
    )


def verify_cluster(cfg: dict) -> None:
    uid = kubectl(
        cfg,
        "get",
        "namespace",
        "kube-system",
        "-o",
        "jsonpath={.metadata.uid}",
        capture=True,
    )
    if uid != cfg["cluster_uid"]:
        raise ValueError(
            "Cluster identity differs from setup; refusing to deploy. Review the kubeconfig and rerun make setup"
        )


def apply(cfg: dict, items: list[dict]) -> None:
    if items:
        kubectl(
            cfg,
            "apply",
            "--server-side",
            "--field-manager=kaede",
            "-f",
            "-",
            data=json.dumps({"apiVersion": "v1", "kind": "List", "items": items}),
        )


def wait_workload(cfg: dict, obj: dict) -> None:
    kubectl(
        cfg,
        "rollout",
        "status",
        f"{obj['kind'].lower()}/{obj['metadata']['name']}",
        f"--timeout={cfg.get('wait_seconds', 600)}s",
    )


def job(cfg: dict, obj: dict) -> None:
    name = obj["metadata"]["name"]
    kubectl(cfg, "delete", "job", name, "--ignore-not-found", "--wait=true")
    apply(cfg, [obj])
    try:
        deadline = time.monotonic() + int(cfg.get("wait_seconds", 600))
        while time.monotonic() < deadline:
            status = json.loads(
                kubectl(cfg, "get", "job", name, "-o", "json", capture=True)
            ).get("status", {})
            conditions = {
                c["type"] for c in status.get("conditions", []) if c["status"] == "True"
            }
            if "Complete" in conditions:
                return
            if "Failed" in conditions or "FailureTarget" in conditions:
                raise ValueError(f"Job {name} failed; inspect its logs before retrying")
            time.sleep(2)
        raise ValueError(f"Job {name} did not finish before the deployment timeout")
    finally:
        try:
            kubectl(cfg, "logs", f"job/{name}", "--all-containers=true", "--tail=100")
        except subprocess.CalledProcessError:
            print(f"Logs for {name} are not available yet", file=sys.stderr)


def schema_digest() -> str:
    h = hashlib.sha256()
    for path in sorted((ROOT / "backend/migrations").rglob("*.py")):
        h.update(str(path.relative_to(ROOT)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def build(cfg: dict, values: dict, revision: str) -> tuple[str, str]:
    images = []
    for name in ("backend", "frontend"):
        image = image_names(cfg, values, revision)[0 if name == "backend" else 1]
        command = ["docker", "build", "--pull", "--target", "runtime", "-t", image]
        if name == "frontend":
            for key, value in values.items():
                if key.startswith("KAEDE_LEGAL_") or key in {
                    "KAEDE_MEDIA_PUBLIC_BASE_URL",
                    "KAEDE_MEDIA_UPLOAD_ORIGINS",
                    "KAEDE_MEDIA_S3_ADDRESSING_STYLE",
                    "KAEDE_MEDIA_ATTACHMENTS_BUCKET",
                    "KAEDE_LANDING_PAGE",
                }:
                    command += ["--build-arg", f"{key}={value}"]
        run([*command, str(ROOT / name)])
        if cfg.get("image_transport", "local") == "local":
            # Local imports are deliberately explicit; unattended updates require
            # the operator to provision permission for this import command.
            with tempfile.TemporaryDirectory(prefix="kaede-image-") as folder:
                archive = str(Path(folder) / "image.tar")
                run(["docker", "save", "-o", archive, image])
                with open(archive, "rb") as stream:
                    subprocess.run(
                        ["sudo", "-n", "/usr/local/sbin/kaede-import-image"],
                        stdin=stream,
                        check=True,
                    )
        else:
            run(["docker", "push", image])
        images.append(image)
    return tuple(images)


def image_names(cfg: dict, values: dict, revision: str) -> tuple[str, str]:
    return tuple(
        f"{cfg['image_repository'].rstrip('/')}/{name}:{revision}-{digest(values)}"
        for name in ("backend", "frontend")
    )


def infrastructure_digest(objects: list[dict]) -> str:
    # Include credentials/config references, storage, ports, and all optional
    # infrastructure. Any change requires an explicit maintenance deployment.
    return digest(
        [
            o
            for o in objects
            if o["kind"] in {"StatefulSet", "PersistentVolumeClaim"}
            or o["kind"] == "Deployment"
            and o["metadata"]["name"]
            in {"livekit", "api-bridge", "caddy-bridge", "grafana-bridge", "loki"}
        ]
    )


def deployment(cfg: dict, values: dict, images: tuple[str, str]) -> dict:
    result = render(
        values,
        cfg["namespace"],
        backend=images[0],
        frontend=images[1],
        storage_class=cfg.get("storage_class", "local-path"),
        storage_size=cfg.get("storage_size", "20Gi"),
        edge_port=int(values.get("KAEDE_CADDY_HOST_PORT", "18081")),
        api_port=int(values.get("KAEDE_API_HOST_PORT", "18082")),
    )
    pull_secret = cfg.get("image_pull_secret")
    if pull_secret:
        for obj in result["items"]:
            if obj["kind"] in {"Deployment", "StatefulSet", "Job"}:
                obj["spec"]["template"]["spec"]["imagePullSecrets"] = [
                    {"name": pull_secret}
                ]
    return result


def deploy(
    cfg: dict, values: dict, revision: str, images: tuple[str, str], maintenance: bool
) -> None:
    verify_cluster(cfg)
    objects = deployment(cfg, values, images)["items"]
    namespace = objects[0]
    apply(cfg, [namespace])
    current = json.loads(
        kubectl(
            cfg,
            "get",
            "configmap",
            "kaede-release",
            "--ignore-not-found",
            "-o",
            "json",
            capture=True,
        )
        or "{}"
    )
    previous = current.get("data", {})
    live = json.loads(
        kubectl(
            cfg,
            "get",
            "deployments",
            "-l",
            "app.kubernetes.io/part-of=kaede",
            "-o",
            "json",
            capture=True,
        )
    ).get("items", [])
    writers = {obj["metadata"]["name"] for obj in live} & {
        "api",
        "gateway",
        "worker",
        "scheduler",
    }
    if writers and not previous and not maintenance:
        raise ValueError(
            "Application workloads exist without a completed release; review them and retry with MAINTENANCE=1"
        )
    schema = schema_digest()
    requires_migration = previous.get("schema") != schema
    if previous and requires_migration and not maintenance:
        raise ValueError(
            "Database migrations changed. Automatic rolling update refused; back up the instance and run make deploy MAINTENANCE=1"
        )
    infra = infrastructure_digest(objects)
    if previous and previous.get("infrastructure") != infra and not maintenance:
        raise ValueError(
            "Infrastructure configuration changed; back up and run make deploy MAINTENANCE=1"
        )
    cfg["wait_seconds"] = int(values.get("AUTO_UPDATE_WAIT_TIMEOUT_SECONDS", "600"))
    # Creating immutable credential/config revisions does not change running pods.
    apply(
        cfg,
        [
            o
            for o in objects
            if o["kind"]
            in {
                "Secret",
                "ConfigMap",
                "PersistentVolumeClaim",
                "Service",
                "NetworkPolicy",
            }
        ],
    )
    jobs = {o["metadata"]["name"]: o for o in objects if o["kind"] == "Job"}
    job(cfg, jobs["preflight"])
    hook = values.get("AUTO_UPDATE_BACKUP_HOOK")
    if hook:
        run([hook])
    stateful = [o for o in objects if o["kind"] == "StatefulSet"]
    if maintenance and writers:
        for name in sorted(writers):
            kubectl(
                cfg,
                "scale",
                f"deployment/{name}",
                "--replicas=0",
                "--field-manager=kaede",
            )
        for name in sorted(writers):
            kubectl(
                cfg,
                "wait",
                "--for=delete",
                "pod",
                "-l",
                f"app={name}",
                "--timeout=120s",
            )
    if maintenance:
        for kind in ("Deployment", "StatefulSet"):
            desired = {o["metadata"]["name"] for o in objects if o["kind"] == kind}
            existing = json.loads(
                kubectl(
                    cfg,
                    "get",
                    kind.lower(),
                    "-l",
                    "app.kubernetes.io/part-of=kaede",
                    "-o",
                    "json",
                    capture=True,
                )
            ).get("items", [])
            for old in existing:
                if old["metadata"]["name"] not in desired:
                    kubectl(
                        cfg,
                        "delete",
                        kind.lower(),
                        old["metadata"]["name"],
                        "--wait=true",
                    )
    apply(cfg, stateful)
    for obj in stateful:
        wait_workload(cfg, obj)
    if requires_migration:
        job(cfg, jobs["migrate"])
    job(cfg, jobs["storage-init"])
    apps = [o for o in objects if o["kind"] == "Deployment"]
    # The scheduler's Recreate strategy prevents overlapping schedulers.
    saved = json.loads(
        kubectl(
            cfg,
            "get",
            "deployments",
            "-l",
            "app.kubernetes.io/part-of=kaede",
            "-o",
            "json",
            capture=True,
        )
    )["items"]
    try:
        apply(cfg, apps)
        for obj in apps:
            wait_workload(cfg, obj)
    except subprocess.CalledProcessError:
        if previous and not maintenance and not requires_migration:
            print(
                "Rollout failed; restoring the previous application specifications",
                file=sys.stderr,
            )
            for obj in saved:
                obj.pop("status", None)
                obj["metadata"] = {
                    k: obj["metadata"][k] for k in ("name", "namespace", "labels")
                }
            apply(cfg, saved)
            for obj in saved:
                wait_workload(cfg, obj)
        raise
    apply(
        cfg,
        [
            resource(
                "ConfigMap",
                "kaede-release",
                cfg["namespace"],
                data={
                    "revision": revision,
                    "schema": schema,
                    "infrastructure": infra,
                    "backend": images[0],
                    "frontend": images[1],
                },
            )
        ],
    )
    print(f"Deployed {revision} to {cfg['context']}/{cfg['namespace']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["render", "build", "deploy", "status", "logs", "exec", "validate"],
    )
    parser.add_argument("--config", type=Path, default=ROOT / ".kaede-kubernetes.json")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--maintenance", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--update-only", action="store_true")
    parser.add_argument("--revision")
    args, extra = parser.parse_known_args()
    if (ROOT / ".kaede-setup.in-progress").exists():
        raise ValueError("Setup transaction is incomplete; rerun make setup")
    cfg = settings(args.config)
    if args.action in {"status", "logs", "exec"}:
        verify_cluster(cfg)
        if args.action == "status":
            kubectl(cfg, "get", "deployments,statefulsets,jobs,pods,pvc")
        elif args.action == "logs":
            kubectl(
                cfg,
                "logs",
                "-l",
                f"app={extra[0] if extra else 'api'}",
                "--all-containers=true",
                "--tail=200",
            )
        else:
            if not extra:
                raise ValueError("exec requires SERVICE COMMAND [ARG...]")
            kind = (
                "statefulset"
                if extra[0]
                in {
                    "postgres",
                    "dragonfly",
                    "garage",
                    "clamav",
                    "meilisearch",
                    "prometheus",
                    "grafana",
                }
                else "deployment"
            )
            kubectl(cfg, "exec", f"{kind}/{extra[0]}", "--", *extra[1:])
        return
    values = read_env_file(args.env_file)
    validate_file_permissions(args.env_file, values)
    validate_values(
        values, observability=values.get("KAEDE_OBSERVABILITY_ENABLED") == "true"
    )
    revision = (
        args.revision
        or run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture=True).strip()
    )
    if not re_full_revision(revision):
        raise ValueError("revision must be a Git commit ID or a safe image tag")
    if (
        not args.revision
        and args.action in {"build", "deploy"}
        and run(["git", "-C", str(ROOT), "status", "--porcelain"], capture=True).strip()
    ):
        revision += f"-dirty-{time.time_ns():x}"
    images = image_names(cfg, values, revision)
    if args.action == "render":
        print(json.dumps(deployment(cfg, values, images)))
        return
    verify_cluster(cfg)
    if args.update_only:
        released = kubectl(
            cfg,
            "get",
            "configmap",
            "kaede-release",
            "--ignore-not-found",
            "-o",
            "name",
            capture=True,
        )
        if not released.strip():
            raise ValueError(
                "No Kubernetes release exists; complete data migration and make deploy before enabling updates"
            )
    if args.action == "validate":
        kubectl(cfg, "auth", "can-i", "create", "deployments")
        print("Configuration and cluster identity validated")
        return
    os.umask(0o077)
    lock_path = ROOT / ".kaede-kubernetes.lock"
    if lock_path.is_symlink() or (
        lock_path.exists() and lock_path.stat().st_nlink != 1
    ):
        raise ValueError("Deployment lock must be a private regular file")
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.no_build:
            images = build(cfg, values, revision)
        if args.action == "deploy":
            deploy(cfg, values, revision, images, args.maintenance)


def re_full_revision(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,109}", value))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        sys.exit(1)
