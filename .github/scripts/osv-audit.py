"""Run OSV's JSON scan and apply the CVSS threshold and package ignores."""

import json
from pathlib import Path
import subprocess
import sys


IGNORE_FILE = Path(__file__).resolve().parents[1] / "osv-ignored-packages.json"


def load_ignored_packages():
    ignored = json.loads(IGNORE_FILE.read_text(encoding="utf-8"))
    if not isinstance(ignored, dict):
        raise ValueError("package ignores must be an object keyed by ecosystem")
    for ecosystem, packages in ignored.items():
        if not ecosystem.strip() or not isinstance(packages, dict):
            raise ValueError("each ignore ecosystem must map package names to reasons")
        for name, reason in packages.items():
            if not name.strip() or not isinstance(reason, str) or not reason.strip():
                raise ValueError("each ignored package must have a name and a reason")
    return ignored


def main(command):
    try:
        ignored_packages = load_ignored_packages()
        scan = subprocess.run(command, capture_output=True, encoding="utf-8")
        # OSV can return findings before checking logged errors. The caller uses
        # --verbosity=error and suppresses Docker pull progress with --quiet.
        if scan.stderr.strip():
            print(scan.stderr, end="", file=sys.stderr)
        if scan.returncode not in (0, 1) or scan.stderr.strip():
            print(f"OSV scan failed (exit {scan.returncode}).", file=sys.stderr)
            return 2

        results = json.loads(scan.stdout)["results"]
        if not isinstance(results, list):
            raise ValueError("expected a results list")
        blocking = 0
        for result in results:
            for item in result["packages"]:
                package = item["package"]
                reason = ignored_packages.get(package["ecosystem"], {}).get(package["name"])
                for group in item["groups"]:
                    # OSV computes the maximum CVSS across each advisory's aliases.
                    raw_score = group["max_severity"]
                    score = None if raw_score == "" else float(raw_score)
                    if score is not None and not 0 <= score <= 10:
                        raise ValueError(f"invalid CVSS score: {raw_score}")
                    blocked = score is not None and score > 6 and reason is None
                    blocking += int(blocked)
                    status = "IGNORED" if reason is not None else "FAIL" if blocked else "INFO"
                    print(
                        f"{status}: "
                        f"{package['name']} {package['version']} | "
                        f"CVSS {raw_score or 'unscored'} | "
                        f"{', '.join(group['ids'])} | {result['source']['path']}"
                        + (f" | Reason: {reason}" if reason is not None else "")
                    )
        print(f"Dependency audit: {blocking} non-ignored vulnerability group(s) above CVSS 6.0.")
        return int(blocking > 0)
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f"OSV audit could not complete: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
