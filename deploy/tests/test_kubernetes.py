from __future__ import annotations

import base64
import copy
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kubernetes"))
import manage
import update_notices
from dev import development_values, instances
from stack import ROOT, read_env_file, render


class KubernetesTests(unittest.TestCase):
    def setUp(self):
        self.values = read_env_file(ROOT / "deploy/.env.schema")

    def test_update_notice_maintenance_classification(self):
        previous = {"schema": "same-schema", "infrastructure": "same-infra"}
        classify = update_notices.classify_update
        self.assertEqual(
            classify(previous, "same-schema", "same-infra", ["frontend/src/app.css"]),
            ("not_required", []),
        )
        self.assertEqual(
            classify(previous, "new-schema", "same-infra", []),
            ("required", ["database"]),
        )
        self.assertEqual(
            classify(previous, "same-schema", "new-infra", []),
            ("required", ["infrastructure"]),
        )
        self.assertEqual(
            classify(
                previous, "same-schema", "same-infra", ["deploy/kubernetes/stack.py"]
            ),
            ("unknown", ["deployment"]),
        )
        self.assertEqual(
            classify(previous, "same-schema", "same-infra", None),
            ("unknown", ["unverified"]),
        )
        self.assertEqual(
            classify({}, "same-schema", "same-infra", []), ("unknown", ["unverified"])
        )

    def test_update_check_reads_upstream_without_checking_out_or_executing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], text=True
                ).strip()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Update test")
            git("config", "user.email", "test@example.invalid")
            migrations = root / "backend/migrations"
            migrations.mkdir(parents=True)
            source = migrations / "initial.py"
            source.write_text("# baseline\n")
            git("add", ".")
            git("commit", "-qm", "baseline")
            current = git("rev-parse", "HEAD")
            git("checkout", "-qb", "upstream")
            marker = root / "executed-upstream"
            source.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            )
            git("commit", "-qam", "candidate")
            target = git("rev-parse", "HEAD")
            git("checkout", "-q", "main")
            git("remote", "add", "origin", str(root))
            report = None

            def kubectl(_cfg, *args, **kwargs):
                nonlocal report
                if args[0] == "get":
                    return json.dumps(
                        {
                            "data": {
                                "revision": current,
                                "schema": "old",
                                "infrastructure": "same",
                            }
                        }
                    )
                self.assertEqual(
                    args,
                    ("exec", "-i", "deployment/api", "--", "kaede", "notify-update"),
                )
                report = json.loads(kwargs["data"])
                return ""

            with (
                patch.object(update_notices, "ROOT", root),
                patch.object(
                    update_notices,
                    "read_env_file",
                    return_value={"AUTO_UPDATE_BRANCH": "upstream"},
                ),
                patch.object(update_notices, "validate_file_permissions"),
                patch.object(manage, "settings", return_value={}),
                patch.object(manage, "verify_cluster"),
                patch.object(manage, "kubectl", side_effect=kubectl),
                patch.object(
                    manage, "image_names", return_value=("backend", "frontend")
                ),
                patch.object(manage, "deployment", return_value={"items": []}),
                patch.object(manage, "infrastructure_digest", return_value="same"),
            ):
                update_notices.check_updates(root / ".env", root / "cluster.json")
            self.assertEqual(report["revision"], target)
            self.assertEqual(report["maintenance"], "required")
            self.assertEqual(git("rev-parse", "HEAD"), current)
            self.assertFalse(marker.exists())
            self.assertEqual(source.read_text(), "# baseline\n")

    def test_role_credentials_and_rollout_contract(self):
        items = render(self.values, "kaede-test")["items"]
        index = {(o["kind"], o["metadata"]["name"]): o for o in items}

        def env(role):
            obj = index[("Deployment", role)]
            ref = obj["spec"]["template"]["spec"]["containers"][0]["envFrom"][0][
                "secretRef"
            ]["name"]
            return {
                k: base64.b64decode(v).decode()
                for k, v in index[("Secret", ref)]["data"].items()
            }

        self.assertNotIn("KAEDE_DATABASE_URL", env("scheduler"))
        self.assertNotIn("KAEDE_ADMIN_TOKEN", env("gateway"))
        self.assertNotIn("KAEDE_MEDIA_S3_SECRET_KEY", env("gateway"))
        self.assertEqual(
            env("gateway")["KAEDE_SECRET_KEY"], self.values["KAEDE_GATEWAY_SECRET_KEY"]
        )
        self.assertNotEqual(
            env("gateway")["KAEDE_SECRET_KEY"], env("api")["KAEDE_SECRET_KEY"]
        )
        for role in ("api", "gateway"):
            obj = index[("Deployment", role)]
            self.assertEqual(
                obj["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"], 0
            )
            pod = obj["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(
                pod["containers"][0]["readinessProbe"]["httpGet"]["path"],
                "/health/ready",
            )
            self.assertTrue(
                pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
            )
        self.assertEqual(
            index[("Deployment", "scheduler")]["spec"]["strategy"]["type"], "Recreate"
        )
        self.assertEqual(index[("Job", "migrate")]["spec"]["backoffLimit"], 0)
        for obj in items:
            if obj["kind"] in {"Deployment", "StatefulSet", "Job"}:
                self.assertFalse(obj["spec"]["template"]["spec"]["enableServiceLinks"])
            if obj["kind"] == "Service":
                self.assertNotIn("nodePort", obj["spec"]["ports"][0])
                self.assertNotEqual(obj["spec"].get("type"), "LoadBalancer")

    def test_external_s3_and_search_do_not_start_bundled_services(self):
        values = self.values | {
            "KAEDE_MEDIA_STORAGE_BACKEND": "s3",
            "KAEDE_MEDIA_S3_ENDPOINT": "http://s3.example.test:9000",
            "KAEDE_SEARCH_ENABLED": "true",
            "KAEDE_SEARCH_URL": "http://search.example.test:7700",
        }
        items = render(values, "kaede-test")["items"]
        names = {o["metadata"]["name"] for o in items}
        self.assertTrue(
            {"garage", "garage-data", "garage-meta", "meilisearch"}.isdisjoint(names)
        )

    def test_configuration_changes_are_immutable_and_infrastructure_is_gated(self):
        before = render(self.values, "kaede-test")["items"]
        after = render(self.values | {"POSTGRES_PASSWORD": "changed"}, "kaede-test")[
            "items"
        ]

        def names(items):
            return {o["metadata"]["name"] for o in items if o["kind"] == "Secret"}

        self.assertNotEqual(names(before), names(after))
        self.assertNotEqual(
            manage.infrastructure_digest(before), manage.infrastructure_digest(after)
        )
        apps = render(self.values, "kaede-test", backend="new-image")["items"]
        self.assertEqual(
            manage.infrastructure_digest(before), manage.infrastructure_digest(apps)
        )

    def test_development_is_one_instance_and_federation_is_explicit(self):
        with (
            patch(
                "dev.read_env_file",
                side_effect=[self.values.copy(), {"KAEDE_DOMAIN": "existing.example"}],
            ),
            patch("pathlib.Path.is_file", return_value=True),
        ):
            configured = development_values(Path("existing.env"))
        self.assertNotIn("KAEDE_ADMIN_TOKEN", configured)
        self.assertNotIn("KAEDE_SECRET_KEY", configured)
        with patch("dev.development_values", return_value=self.values.copy()):
            self.assertEqual(list(instances(False)), ["kaede-dev"])
        self.assertEqual(set(instances(True)), {"kaede-alpha", "kaede-beta"})
        items = render(
            self.values | {"KAEDE_GATEWAY_WORKERS": "6", "KAEDE_API_WORKERS": "8"},
            "kaede-dev",
            development=True,
        )["items"]
        for obj in items:
            if obj["kind"] == "Deployment" and obj["metadata"]["name"] in {
                "api",
                "gateway",
            }:
                self.assertEqual(obj["spec"]["replicas"], 1)
        self.assertEqual(
            instances(True)["kaede-beta"]["KAEDE_APP_URL"],
            "http://beta.localhost:28181",
        )

    def test_wrong_cluster_and_migration_refuse_before_workload_changes(self):
        cfg = {
            "namespace": "kaede-test",
            "cluster_uid": "expected",
            "image_repository": "local",
        }
        with patch("manage.kubectl", return_value="wrong"):
            with self.assertRaisesRegex(ValueError, "identity"):
                manage.verify_cluster(cfg)
        current = json.dumps({"data": {"schema": "old", "revision": "previous"}})
        with (
            patch("manage.verify_cluster"),
            patch("manage.kubectl", return_value=current),
            patch("manage.apply") as apply,
        ):
            with self.assertRaisesRegex(ValueError, "migrations changed"):
                manage.deploy(
                    cfg, self.values, "new", ("backend:new", "frontend:new"), False
                )
            self.assertEqual(len(apply.call_args_list), 1)
            self.assertEqual(apply.call_args.args[1][0]["kind"], "Namespace")

    def test_maintenance_stops_writers_before_migration_and_restores_replicas(self):
        cfg = {"namespace": "kaede-test", "context": "test"}
        writers = ["api", "gateway", "scheduler", "worker"]
        events = []

        def kubectl(_cfg, *args, **kwargs):
            if args[0] == "get":
                return json.dumps(
                    {}
                    if "configmap" in args
                    else {"items": [{"metadata": {"name": name}} for name in writers]}
                )
            events.append(args)
            return ""

        def apply(_cfg, items, *, force_conflicts=False):
            for obj in items:
                if obj["kind"] == "Deployment":
                    self.assertTrue(force_conflicts)
                    events.append(
                        ("restore", obj["metadata"]["name"], obj["spec"]["replicas"])
                    )
                else:
                    self.assertFalse(force_conflicts)

        def job(_cfg, obj):
            events.append(("job", obj["metadata"]["name"]))

        with (
            patch("manage.verify_cluster"),
            patch("manage.kubectl", side_effect=kubectl),
            patch("manage.job", side_effect=job),
            patch("manage.wait_workload"),
            patch("manage.apply", side_effect=apply),
        ):
            manage.deploy(
                cfg, self.values, "new", ("backend:new", "frontend:new"), True
            )

        self.assertEqual(
            events[:9],
            [
                ("job", "preflight"),
                *[
                    (
                        "patch",
                        f"deployment/{name}",
                        "--type=merge",
                        '-p={"spec":{"replicas":0}}',
                        "--field-manager=kaede",
                    )
                    for name in writers
                ],
                *[
                    (
                        "wait",
                        "--for=delete",
                        "pod",
                        "-l",
                        f"app={name}",
                        "--timeout=120s",
                    )
                    for name in writers
                ],
            ],
        )
        migration = events.index(("job", "migrate"))
        self.assertGreaterEqual(migration, 9)
        for name in writers:
            restored = next(event for event in events if event[:2] == ("restore", name))
            self.assertGreater(restored[2], 0)
            self.assertGreater(events.index(restored), migration)

    def test_apply_conflict_takeover_is_explicit(self):
        items = [{"apiVersion": "apps/v1", "kind": "Deployment"}]
        for force in (False, True):
            with self.subTest(force=force), patch("manage.kubectl") as kubectl:
                manage.apply({}, items, force_conflicts=force)
                self.assertEqual("--force-conflicts" in kubectl.call_args.args, force)
                self.assertIn("--server-side", kubectl.call_args.args)
                self.assertEqual(
                    json.loads(kubectl.call_args.kwargs["data"])["items"], items
                )

    def test_failed_compatible_rollout_restores_previous_application(self):
        cfg = {
            "namespace": "kaede-test",
            "cluster_uid": "expected",
            "image_repository": "local",
        }
        objects = manage.deployment(cfg, self.values, ("backend:new", "frontend:new"))[
            "items"
        ]
        saved = [copy.deepcopy(o) for o in objects if o["kind"] == "Deployment"]
        for obj in saved:
            obj["spec"]["template"]["spec"]["containers"][0]["image"] = "old-image"
        current = {
            "data": {
                "schema": manage.schema_digest(),
                "infrastructure": manage.infrastructure_digest(objects),
            }
        }

        def kubectl(cfg, *args, **kwargs):
            return json.dumps(current if "configmap" in args else {"items": saved})

        failed = False

        def wait(cfg, obj):
            nonlocal failed
            if obj["kind"] == "Deployment" and not failed:
                failed = True
                raise manage.subprocess.CalledProcessError(1, "rollout")

        with (
            patch("manage.verify_cluster"),
            patch("manage.kubectl", side_effect=kubectl),
            patch("manage.job"),
            patch("manage.wait_workload", side_effect=wait),
            patch("manage.apply") as apply,
        ):
            with self.assertRaises(manage.subprocess.CalledProcessError):
                manage.deploy(
                    cfg, self.values, "new", ("backend:new", "frontend:new"), False
                )
            self.assertEqual(apply.call_args.args[1], saved)
            self.assertFalse(
                any(
                    call.kwargs.get("force_conflicts", False)
                    for call in apply.call_args_list
                )
            )


if __name__ == "__main__":
    unittest.main()
