#!/usr/bin/env python3
"""Check the configured upstream without checking out or executing upstream code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import manage
from stack import ROOT, read_env_file
from migration_safety import online_revisions
from validate_deploy_env import validate_file_permissions


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
        check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout.decode()


def target_sources(revision: str) -> dict[str, str]:
    paths = git(
        "ls-tree", "-r", "--name-only", revision, "--", "backend/migrations"
    ).splitlines()
    return {
        path: git("show", f"{revision}:{path}")
        for path in sorted(paths)
        if path.endswith(".py")
    }


def target_schema(revision: str, files: dict[str, str] | None = None) -> str:
    digest = hashlib.sha256()
    for path, source in sorted(
        (files if files is not None else target_sources(revision)).items()
    ):
        digest.update(path.encode())
        digest.update(source.encode())
    return digest.hexdigest()


def classify_update(
    previous: dict,
    schema: str,
    infrastructure: str,
    paths: list[str] | None,
    files: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    reasons = []
    if previous.get("schema") and previous["schema"] != schema:
        try:
            if files is None:
                raise ValueError("unverified migrations")
            online_revisions(previous.get("migration_manifest"), files)
        except ValueError:
            reasons.append("database")
    if previous.get("infrastructure") and previous["infrastructure"] != infrastructure:
        reasons.append("infrastructure")
    if reasons:
        return "required", reasons
    if (
        paths is None
        or not previous.get("schema")
        or not previous.get("infrastructure")
    ):
        return "unknown", ["unverified"]
    if any(
        path.startswith("deploy/") or path in {"Makefile", "setup.sh"} for path in paths
    ):
        return "unknown", ["deployment"]
    return "not_required", []


def check_updates(env_file: Path, config: Path) -> None:
    if (ROOT / ".kaede-setup.in-progress").exists():
        raise ValueError("Setup transaction is incomplete")
    values = read_env_file(env_file)
    validate_file_permissions(env_file, values)
    remote = values.get("AUTO_UPDATE_REMOTE", "origin")
    branch = values.get("AUTO_UPDATE_BRANCH", "main")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        raise ValueError("Invalid AUTO_UPDATE_REMOTE name")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
        or ".." in branch
        or branch.endswith("/")
    ):
        raise ValueError("Invalid AUTO_UPDATE_BRANCH name")
    cfg = manage.settings(config)
    manage.verify_cluster(cfg)
    previous = json.loads(
        manage.kubectl(
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
    ).get("data", {})
    current = previous.get("revision")
    if not current:
        return  # Nothing deployed yet.
    git(
        "fetch",
        "--quiet",
        remote,
        f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
    )
    target = git(
        "rev-parse", "--verify", f"refs/remotes/{remote}/{branch}^{{commit}}"
    ).strip()
    if current == target:
        return
    paths = None
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", current):
        try:
            # Don't advertise a downgrade or divergent history as an update.
            git("merge-base", "--is-ancestor", current, target)
            paths = git("diff", "--name-only", current, target, "--").splitlines()
        except subprocess.CalledProcessError:
            # A missing deployed object cannot be compared. A known divergent
            # history is not a newer version and must not be advertised.
            try:
                git("cat-file", "-e", f"{current}^{{commit}}")
            except subprocess.CalledProcessError:
                pass
            else:
                return
    objects = manage.deployment(cfg, values, manage.image_names(cfg, values, target))[
        "items"
    ]
    files = target_sources(target)
    maintenance, reasons = classify_update(
        previous,
        target_schema(target, files),
        manage.infrastructure_digest(objects),
        paths,
        files,
    )
    report = {
        "revision": target,
        "current_revision": current,
        "maintenance": maintenance,
        "reasons": reasons,
    }
    manage.kubectl(
        cfg,
        "exec",
        "-i",
        "deployment/api",
        "--",
        "kaede",
        "notify-update",
        data=json.dumps(report),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--config", type=Path, default=ROOT / ".kaede-kubernetes.json")
    args = parser.parse_args()
    try:
        check_updates(args.env_file, args.config)
    except (ValueError, OSError, subprocess.SubprocessError):
        # Git/kubectl errors can contain operator credentials. Keep the timer's
        # error generic; its failure never enables or initiates a deployment.
        parser.exit(
            1,
            "Update notification check failed; verify the Git source and cluster access.\n",
        )


if __name__ == "__main__":
    main()
