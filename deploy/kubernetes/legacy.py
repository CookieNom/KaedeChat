#!/usr/bin/env python3
"""Inspect, stop, or export an explicitly named legacy Compose project.

Never removes containers or volumes. Restore only accepts a fresh namespace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from manage import (
    apply,
    deployment,
    kubectl,
    run,
    settings,
    verify_cluster,
    wait_workload,
)
from stack import ROOT, IMAGES, read_env_file, resource

VOLUMES = {
    "worker": {"/models/rembg": "rembg-models"},
    "clamav": {"/var/lib/clamav": "clamav-data"},
    "dragonfly": {"/data": "dragonfly-data"},
    "garage": {
        "/var/lib/garage/meta": "garage-meta",
        "/var/lib/garage/data": "garage-data",
    },
    "meilisearch": {"/meili_data": "meilisearch-data"},
    "prometheus": {"/prometheus": "prometheus-data"},
    "grafana": {"/var/lib/grafana": "grafana-data"},
}


def containers(project: str) -> dict[str, dict]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", project):
        raise ValueError(
            "Pass the exact legacy project with --project; use docker compose ls to find it"
        )
    ids = run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        capture=True,
    ).split()
    if not ids:
        raise ValueError(f"No containers found for project {project}")
    result = {}
    for obj in json.loads(run(["docker", "inspect", *ids], capture=True)):
        role = obj["Config"]["Labels"]["com.docker.compose.service"]
        if role in result:
            if role in {"postgres", *VOLUMES}:
                raise ValueError(
                    f"Multiple {role} containers; export this topology manually"
                )
            role += "-" + obj["Id"][:8]
        result[role] = obj
    if "postgres" not in result or "api" not in result:
        raise ValueError(
            "Project does not look like a Kaede instance (api/postgres missing)"
        )
    return result


def stop(objects: dict[str, dict]) -> None:
    ids = [o["Id"] for o in objects.values() if o["State"]["Running"]]
    if ids:
        run(["docker", "stop", "--time", "120", *ids])


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def export(project: str, folder: Path, env_file: Path) -> None:
    objects = containers(project)
    if not objects["postgres"]["State"]["Running"]:
        raise ValueError("Start the legacy PostgreSQL container before exporting")
    source_env = dict(
        item.split("=", 1) for item in objects["api"]["Config"]["Env"] if "=" in item
    )
    values = read_env_file(env_file)
    for key in ("KAEDE_DOMAIN", "KAEDE_SECRET_KEY"):
        if not values.get(key) or source_env.get(key) != values[key]:
            raise ValueError(
                f"Selected operator environment does not match the source {key}; no containers were stopped"
            )
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    shutil.copyfile(env_file, folder / "operator.env")
    (folder / "operator.env").chmod(0o600)
    # Quiesce all clients before the logical dump and the cache snapshot.
    stop(
        {
            k: v
            for k, v in objects.items()
            if k
            not in {
                "postgres",
                "dragonfly",
                "garage",
                "meilisearch",
                "clamav",
                "prometheus",
                "grafana",
            }
        }
    )
    pg = objects["postgres"]["Id"]
    with (folder / "postgres.dump").open("wb") as output:
        subprocess.run(
            ["docker", "exec", pg, "pg_dump", "-U", "kaede", "-d", "kaede", "-Fc"],
            stdout=output,
            check=True,
        )
    if "dragonfly" in objects and objects["dragonfly"]["State"]["Running"]:
        run(["docker", "exec", objects["dragonfly"]["Id"], "redis-cli", "SAVE"])
    stop(objects)
    images = {"postgres": objects["postgres"]["Config"]["Image"]}
    for role, paths in VOLUMES.items():
        if role not in objects:
            continue
        obj = objects[role]
        if role in IMAGES:
            images[role] = obj["Config"]["Image"]
        mounted = {m["Destination"] for m in obj["Mounts"]}
        for path, claim in paths.items():
            if path not in mounted:
                raise ValueError(
                    f"Expected {role} data mount {path} is missing; source remains stopped"
                )
            with (folder / f"{claim}.tar").open("wb") as output:
                subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--volumes-from",
                        obj["Id"] + ":ro",
                        "alpine:3.23",
                        "tar",
                        "-cf",
                        "-",
                        "-C",
                        path,
                        ".",
                    ],
                    stdout=output,
                    check=True,
                )
    files = {p.name: checksum(p) for p in folder.iterdir() if p.is_file()}
    (folder / "backup.json").write_text(
        json.dumps({"project": project, "images": images, "files": files}, indent=2)
        + "\n"
    )
    print(
        f"Export complete: {folder}. Legacy containers are stopped; their volumes are retained."
    )


def restore(cfg: dict, folder: Path, env_file: Path, development: bool = False) -> None:
    verify_cluster(cfg)
    manifest = json.loads((folder / "backup.json").read_text())
    for name, expected in manifest["files"].items():
        if Path(name).name != name or checksum(folder / name) != expected:
            raise ValueError("Backup checksum mismatch")
    for name, image in manifest["images"].items():
        if IMAGES.get(name) != image:
            raise ValueError(
                f"Restore with the source {name} image version before upgrading"
            )
    source, values = read_env_file(folder / "operator.env"), read_env_file(env_file)
    for key in (
        "KAEDE_DOMAIN",
        "KAEDE_SECRET_KEY",
        "KAEDE_GATEWAY_SECRET_KEY",
        "KAEDE_MEDIA_S3_ACCESS_KEY",
        "KAEDE_MEDIA_S3_SECRET_KEY",
        "KAEDE_MEDIA_STORAGE_BACKEND",
        "KAEDE_MEDIA_ATTACHMENTS_BUCKET",
        "KAEDE_MEDIA_DERIVED_BUCKET",
        "KAEDE_MEDIA_REMOTE_CACHE_BUCKET",
    ):
        if source.get(key) != values.get(key):
            raise ValueError(
                f"Restore requires the original {key}; retain the source operator environment"
            )
    found = kubectl(
        cfg,
        "get",
        "namespace",
        cfg["namespace"],
        "--ignore-not-found",
        "-o",
        "name",
        capture=True,
    )
    if found.strip():
        raise ValueError(
            "Restore requires a namespace that does not exist; never overwrite a running instance"
        )
    if development:
        from dev import manifests

        objects = manifests(False, env_file)
    else:
        objects = deployment(
            cfg, values, ("kaede-restore-unused", "kaede-restore-unused")
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
    claims = {
        o["metadata"]["name"] for o in objects if o["kind"] == "PersistentVolumeClaim"
    }
    archives = [p for p in folder.glob("*.tar")]
    if source.get("KAEDE_MEDIA_STORAGE_BACKEND") == "s3":
        # Older Compose stacks started Garage even when external S3 was selected.
        # Keep its archives in the backup, but do not start unused storage.
        unused = {"garage-data", "garage-meta"}
        if any(p.stem in unused for p in archives):
            print(
                "Unused Garage archives retained in backup; external S3 remains selected."
            )
        archives = [p for p in archives if p.stem not in unused]
    if any(p.stem not in claims for p in archives):
        raise ValueError(
            "Target storage configuration differs from backup; retain the source optional services"
        )
    pod = resource(
        "Pod",
        "legacy-restore",
        cfg["namespace"],
        spec={
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "restore",
                    "image": "alpine:3.23",
                    "command": ["sleep", "86400"],
                    "volumeMounts": [
                        {"name": p.stem, "mountPath": f"/restore/{p.stem}"}
                        for p in archives
                    ],
                }
            ],
            "volumes": [
                {"name": p.stem, "persistentVolumeClaim": {"claimName": p.stem}}
                for p in archives
            ],
        },
    )
    apply(cfg, [pod])
    try:
        kubectl(
            cfg, "wait", "--for=condition=Ready", "pod/legacy-restore", "--timeout=300s"
        )
        prefix = [
            "kubectl",
            "--kubeconfig",
            cfg["kubeconfig"],
            "--context",
            cfg["context"],
            "-n",
            cfg["namespace"],
            "exec",
            "-i",
        ]
        for archive in archives:
            with archive.open("rb") as stream:
                subprocess.run(
                    [
                        *prefix,
                        "legacy-restore",
                        "--",
                        "tar",
                        "-xf",
                        "-",
                        "-C",
                        f"/restore/{archive.stem}",
                    ],
                    stdin=stream,
                    check=True,
                )
        pg = next(
            o
            for o in objects
            if o["kind"] == "StatefulSet" and o["metadata"]["name"] == "postgres"
        )
        apply(cfg, [pg])
        wait_workload(cfg, pg)
        with (folder / "postgres.dump").open("rb") as stream:
            subprocess.run(
                [
                    *prefix,
                    "postgres-0",
                    "--",
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "-U",
                    "kaede",
                    "-d",
                    "kaede",
                ],
                stdin=stream,
                check=True,
            )
    finally:
        kubectl(cfg, "delete", "pod", "legacy-restore", "--ignore-not-found")
    print(
        "Data restored. Run make deploy to migrate and start the application. Keep the old containers stopped."
    )


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "stop", "export", "restore"])
    parser.add_argument("--project", default="")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--config", type=Path, default=ROOT / ".kaede-kubernetes.json")
    args = parser.parse_args()
    if args.action in {"export", "restore"} and args.backup is None:
        parser.error("--backup is required")
    if args.action == "restore":
        if args.development:
            from dev import cluster_name

            cfg = {
                "kubeconfig": str(ROOT / ".kaede-dev-kubeconfig"),
                "context": "k3d-" + cluster_name(),
                "namespace": "kaede-dev",
            }
            cfg["cluster_uid"] = kubectl(
                cfg,
                "get",
                "namespace",
                "kube-system",
                "-o",
                "jsonpath={.metadata.uid}",
                capture=True,
            )
        else:
            cfg = settings(args.config)
        restore(cfg, args.backup, args.env_file, args.development)
    elif args.action == "export":
        export(args.project, args.backup, args.env_file)
    else:
        objects = containers(args.project)
        if args.action == "stop":
            stop(objects)
        else:
            print(
                "\n".join(
                    f"{role}: {obj['Name']} ({obj['State']['Status']})"
                    for role, obj in objects.items()
                )
            )
