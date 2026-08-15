#!/usr/bin/env python3
"""Guard the zero-setup Pro/Air relay defaults shipped in the phone web app."""
from __future__ import annotations

import pathlib
import re
import sys

web = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
block = re.search(r"const DEFAULT_PROFILES = \{(.*?)\n\};", web, re.S)
assert block, "DEFAULT_PROFILES is missing"

for machine in ("pro", "air"):
    profile = re.search(rf"{machine}: \{{(.*?)\}},", block.group(1), re.S)
    assert profile, f"{machine} default profile is missing"
    assert f"macbook-{machine}.kunkunzheten.top" in profile.group(1), f"{machine} host is missing"
    token = re.search(r"token:'([^']+)'", profile.group(1))
    assert token and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token.group(1)), f"{machine} token is not embedded"

assert "const token = activeProfile().token;\n  let wsUrl = url;" in web, "WebSocket connections must use the active profile token"
assert "const vapidToken = activeProfile().token || '';" in web, "push setup must use the active profile token"
assert "if (pushSubscription) socket.send(JSON.stringify({type: 'push_subscribe', subscription: pushSubscription.toJSON()}));" in web, "profile reconnect must register existing push subscription"
assert "profile.token = document.getElementById('relayToken').value.trim();" in web, "manual connection override must apply only to the active profile"
assert "const legacyToken = raw.token || localStorage.getItem('herdr_relay_token') || '';" in web, "legacy shared token must migrate even without saved profiles"
assert "profile.host = url;" in web, "manual relay URL must apply to the active profile"
assert "token: raw.pro?.token || legacyToken || DEFAULT_PROFILES.pro.token" in web, "saved Pro override must survive reload"
assert "token: raw.air?.token || legacyToken || DEFAULT_PROFILES.air.token" in web, "saved Air override must survive reload"
assert "function resetProfiles()" in web, "settings must provide a profile reset"
assert "if (params.has('reset'))" in web, "reset link must clear stale profile storage"
