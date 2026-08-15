#!/usr/bin/env python3
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "relay"


class ServiceScriptTests(unittest.TestCase):
    def test_service_wrapper_execs_configured_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            marker = tmp / "marker"
            fake_uv = tmp / "uv"
            fake_uv.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" > "{marker}"\n')
            fake_uv.chmod(0o755)
            config = tmp / "config.env"
            config.write_text(
                f"HERDR_UV_PATH={fake_uv}\n"
                f"HERDR_RELAY_DIR={RELAY}\n"
                "HERDR_RELAY_PORT=8375\n"
            )

            result = subprocess.run(
                [RELAY / "service.sh", "relay"],
                env={**os.environ, "HERDR_CONFIG_DIR": str(tmp)},
                capture_output=True,
                text=True,
                timeout=5,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text().strip(), f"run {RELAY}/herdr_relay.py")

    def test_service_wrapper_exports_tunnel_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            marker = tmp / "marker"
            fake_cloudflared = tmp / "cloudflared"
            fake_cloudflared.write_text(
                f'#!/bin/sh\nprintf "%s\\n" "$HTTPS_PROXY" > "{marker}"\n'
            )
            fake_cloudflared.chmod(0o755)
            (tmp / "config.env").write_text(
                f"HERDR_CLOUDFLARED_PATH={fake_cloudflared}\n"
                "HERDR_RELAY_PORT=8375\n"
                "HERDR_TUNNEL_MODE=temp\n"
                "HERDR_TUNNEL_PROXY=http://127.0.0.1:7890\n"
            )

            result = subprocess.run(
                [RELAY / "service.sh", "tunnel"],
                env={**os.environ, "HERDR_CONFIG_DIR": str(tmp)},
                capture_output=True,
                text=True,
                timeout=5,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text().strip(), "http://127.0.0.1:7890")

    def test_guard_never_backgrounds_start_script(self):
        text = (RELAY / "guard.sh").read_text()
        self.assertIn("launchctl kickstart -k", text)
        self.assertIn('export HTTPS_PROXY="$HERDR_TUNNEL_PROXY"', text)
        self.assertNotIn("nohup", text)
        self.assertNotIn("start.sh", text)
        self.assertNotIn("pkill", text)

    def test_installer_preserves_runtime_credentials(self):
        text = (RELAY / "install-service.sh").read_text()
        self.assertIn("HERDR_VAPID_PUBLIC=${HERDR_VAPID_PUBLIC:-}", text)
        self.assertIn("HERDR_VAPID_PRIVATE=${HERDR_VAPID_PRIVATE:-}", text)
        self.assertIn("HERDR_REMOTES=${HERDR_REMOTES:-}", text)

    def test_installer_uses_managed_service_wrapper(self):
        text = (RELAY / "install-service.sh").read_text()
        self.assertIn("<string>$SCRIPT_DIR/service.sh</string>", text)
        self.assertIn("<string>relay</string>", text)
        self.assertIn("<string>tunnel</string>", text)
        relay_block = text[text.index('<string>$LABEL_RELAY</string>'):text.index('echo "  Relay service installed."')]
        tunnel_block = text[text.index('<string>$LABEL_TUNNEL</string>'):text.index('echo "  Tunnel service installed."')]
        self.assertNotIn("<string>-lc</string>", relay_block)
        self.assertNotIn("<string>-lc</string>", tunnel_block)

    def test_installed_launch_agents_are_valid_plists(self):
        launch_agents = Path.home() / "Library/LaunchAgents"
        for name in ("com.herdr-remote.relay.plist", "com.herdr-remote.tunnel.plist"):
            path = launch_agents / name
            if path.exists():
                with path.open("rb") as file:
                    plistlib.load(file)


if __name__ == "__main__":
    unittest.main()
