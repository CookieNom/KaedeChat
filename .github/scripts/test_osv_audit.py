import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "osv_audit", Path(__file__).with_name("osv-audit.py")
)
osv_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(osv_audit)


def report(*scores):
    return {"results": [{
        "source": {"path": "desktop/Cargo.lock"},
        "packages": [{
            "package": {"ecosystem": "crates.io", "name": "glib", "version": "0.18.5"},
            "groups": [
                {"ids": ["RUSTSEC-2024-0429", "GHSA-wrw7-89jp-8q8g"],
                 "max_severity": score}
                for score in scores
            ],
        }],
    }]}


class OsvAuditTest(unittest.TestCase):
    def run_scan(self, stdout, returncode=1, stderr="", ignored=None):
        output = io.StringIO()
        errors = io.StringIO()
        scan = subprocess.CompletedProcess(["scanner"], returncode, stdout, stderr)
        with patch.object(osv_audit.subprocess, "run", return_value=scan), \
                patch.object(osv_audit, "load_ignored_packages", return_value=ignored or {}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = osv_audit.main(["scanner"])
        return status, output.getvalue(), errors.getvalue()

    def test_cvss_threshold_is_strict_and_unscored_findings_are_visible(self):
        for score, expected in (
            ("", 0), ("0.0", 0), ("5.9", 0), ("6.0", 0),
            ("6.1", 1), ("6.9", 1), ("10.0", 1),
        ):
            with self.subTest(score=score):
                status, output, errors = self.run_scan(json.dumps(report(score)))
                self.assertEqual(status, expected)
                self.assertIn(f"CVSS {score or 'unscored'}", output)
                self.assertIn("RUSTSEC-2024-0429, GHSA-wrw7-89jp-8q8g", output)
                self.assertEqual(errors, "")

    def test_clean_scan_passes(self):
        self.assertEqual(self.run_scan('{"results": []}', returncode=0)[0], 0)

    def test_checks_all_sources_and_groups(self):
        combined = report("", "6.0")
        combined["results"].extend(report("6.1", "9.8")["results"])
        status, output, _ = self.run_scan(json.dumps(combined))
        self.assertEqual(status, 1)
        self.assertIn("2 non-ignored vulnerability group(s) above CVSS 6.0", output)

    def test_ignored_packages_remain_visible_with_scores_and_reason(self):
        reason = "GTK dependency pending upstream migration"
        status, output, errors = self.run_scan(
            json.dumps(report("", "6.9", "10.0")),
            ignored={"crates.io": {"glib": reason}},
        )
        self.assertEqual(status, 0)
        self.assertEqual(errors, "")
        self.assertEqual(output.count("IGNORED: glib 0.18.5"), 3)
        for score in ("unscored", "6.9", "10.0"):
            self.assertIn(f"CVSS {score}", output)
        self.assertIn("RUSTSEC-2024-0429, GHSA-wrw7-89jp-8q8g", output)
        self.assertIn(reason, output)
        self.assertIn("0 non-ignored vulnerability group(s) above CVSS 6.0", output)

    def test_ignore_matches_exact_package_and_ecosystem_in_mixed_scan(self):
        combined = report("6.9")
        for ecosystem, name in (("crates.io", "glib-sys"), ("PyPI", "glib")):
            other = report("9.8")
            other["results"][0]["packages"][0]["package"].update(
                ecosystem=ecosystem, name=name
            )
            combined["results"].extend(other["results"])
        status, output, errors = self.run_scan(
            json.dumps(combined), ignored={"crates.io": {"glib": "GTK dependency"}}
        )
        self.assertEqual(status, 1)
        self.assertEqual(errors, "")
        self.assertIn("IGNORED: glib 0.18.5", output)
        self.assertIn("FAIL: glib-sys 0.18.5", output)
        self.assertIn("FAIL: glib 0.18.5", output)
        self.assertIn("2 non-ignored vulnerability group(s) above CVSS 6.0", output)

    def test_ignores_do_not_hide_scan_errors_or_invalid_scores(self):
        for score, code, stderr in (
            ("6.9", 129, ""),
            ("6.9", 1, "failed to scan a lockfile\n"),
            ("NaN", 1, ""),
        ):
            with self.subTest(score=score, code=code, stderr=stderr):
                status, _, errors = self.run_scan(
                    json.dumps(report(score)), code, stderr,
                    ignored={"crates.io": {"glib": "GTK dependency"}},
                )
                self.assertEqual(status, 2)
                self.assertTrue(errors)

    def test_load_ignored_packages_rejects_invalid_or_missing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "ignored.json"
            with patch.object(osv_audit, "IGNORE_FILE", config):
                expected = {"crates.io": {"glib": "GTK dependency"}}
                config.write_text(json.dumps(expected), encoding="utf-8")
                self.assertEqual(osv_audit.load_ignored_packages(), expected)
                for data in (
                    "{", "[]", '{"": {}}', '{"crates.io": []}',
                    '{"crates.io": {"": "reason"}}',
                    '{"crates.io": {"glib": " "}}',
                    '{"crates.io": {"glib": 1}}',
                ):
                    with self.subTest(data=data):
                        config.write_text(data, encoding="utf-8")
                        with self.assertRaises(ValueError):
                            osv_audit.load_ignored_packages()
                config.unlink()
                with patch.object(osv_audit.subprocess, "run") as scan, \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(osv_audit.main(["scanner"]), 2)
                    scan.assert_not_called()

    def test_scan_errors_fail_even_with_nonblocking_findings(self):
        for code in (2, 125, 127, 128, 129, 130):
            with self.subTest(code=code):
                status, _, errors = self.run_scan(json.dumps(report("")), code)
                self.assertEqual(status, 2)
                self.assertIn("OSV scan failed", errors)
        for code in (0, 1):
            with self.subTest(code=code):
                status, _, errors = self.run_scan(
                    json.dumps(report("5.9")), code, "failed to scan a lockfile\n"
                )
                self.assertEqual(status, 2)
                self.assertIn("failed to scan a lockfile", errors)

    def test_invalid_reports_and_scores_fail(self):
        for data in ("", "{", "{}", '{"results": null}', '{"results": {}}'):
            with self.subTest(data=data):
                self.assertEqual(self.run_scan(data)[0], 2)
        for score in (None, "invalid", "NaN", "Infinity", "-1", "11"):
            with self.subTest(score=score):
                self.assertEqual(self.run_scan(json.dumps(report(score)))[0], 2)


if __name__ == "__main__":
    unittest.main()
