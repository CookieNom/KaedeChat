from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NGINX_EXAMPLE = ROOT / "deploy" / "nginx" / "kaede.conf.example"


def directives(block: str) -> list[list[str]]:
    return [
        shlex.split(clause.strip()) for clause in block.split(";") if clause.strip()
    ]


class NginxVoiceRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = re.sub(r"(?m)#.*$", "", NGINX_EXAMPLE.read_text(encoding="utf-8"))
        cls.livekit = re.search(
            r"location\s+~\s+\^/livekit\(\?:/\|\$\)\s*\{([^{}]*)\}", cls.config
        ).group(1)
        cls.policy = directives(cls.livekit)

    def test_livekit_has_a_dedicated_websocket_location(self) -> None:
        self.assertIn(["proxy_set_header", "Upgrade", "$http_upgrade"], self.policy)
        connection = next(
            item[2]
            for item in self.policy
            if item[:2] == ["proxy_set_header", "Connection"]
        )
        mapping = re.search(
            r"map\s+\$http_upgrade\s+" + re.escape(connection) + r"\s*\{([^{}]*)\}",
            self.config,
        )
        self.assertIsNotNone(mapping)
        self.assertIn(["default", "upgrade"], directives(mapping.group(1)))
        self.assertIn(["", "close"], directives(mapping.group(1)))
        self.assertIn(["proxy_buffering", "off"], self.policy)
        timeout = next(
            item[1] for item in self.policy if item[0] == "proxy_read_timeout"
        )
        self.assertRegex(timeout, r"^[1-9]\d*[smhd]?$")

    def test_livekit_handshakes_have_dedicated_admission_limits(self) -> None:
        connection = next(item for item in self.policy if item[0] == "limit_conn")
        request = next(item for item in self.policy if item[0] == "limit_req")
        self.assertGreater(int(connection[2]), 0)
        request_zone = next(
            value.removeprefix("zone=")
            for value in request[1:]
            if value.startswith("zone=")
        )
        self.assertIn("nodelay", request)
        self.assertTrue(
            any(value.startswith("burst=") and int(value[6:]) > 0 for value in request)
        )
        self.assertRegex(
            self.config,
            r"limit_conn_zone\s+\$binary_remote_addr\s+zone="
            + re.escape(connection[1])
            + r":[1-9]\d*[kKmM]?\s*;",
        )
        self.assertRegex(
            self.config,
            r"limit_req_zone\s+\$binary_remote_addr\s+zone="
            + re.escape(request_zone)
            + r":[1-9]\d*[kKmM]?\s+rate=[1-9]\d*r/[sm]\s*;",
        )
        outside = self.config.replace(self.livekit, "")
        self.assertNotRegex(
            outside, r"limit_conn\s+" + re.escape(connection[1]) + r"\s"
        )
        self.assertNotRegex(
            outside, r"limit_req\s+zone=" + re.escape(request_zone) + r"\s"
        )


if __name__ == "__main__":
    unittest.main()
