from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NGINX_EXAMPLE = ROOT / "deploy" / "nginx" / "kaede.conf.example"


class NginxVoiceRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = NGINX_EXAMPLE.read_text(encoding="utf-8")

    def test_livekit_has_a_dedicated_websocket_location(self) -> None:
        marker = "location ~ ^/livekit(?:/|$) {"
        start = self.config.index(marker)
        end = self.config.index("\n    }", start)
        block = self.config[start:end]

        self.assertIn("proxy_set_header Upgrade $http_upgrade;", block)
        self.assertIn(
            "proxy_set_header Connection $kaede_connection_upgrade;", block
        )
        self.assertIn("proxy_read_timeout 3600s;", block)
        self.assertIn("proxy_buffering off;", block)

    def test_livekit_handshakes_have_dedicated_admission_limits(self) -> None:
        self.assertIn("zone=kaede_livekit_connections:10m", self.config)
        self.assertIn("zone=kaede_livekit_upgrades:10m rate=10r/s", self.config)
        self.assertIn("limit_conn kaede_livekit_connections 64;", self.config)
        self.assertIn(
            "limit_req zone=kaede_livekit_upgrades burst=20 nodelay;", self.config
        )


if __name__ == "__main__":
    unittest.main()
