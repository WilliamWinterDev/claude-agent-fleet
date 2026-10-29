"""claude-bridge webhook sender (stdlib only): one JSON POST to BRIDGE_WEBHOOK_URL.

Reads BRIDGE_WEBHOOK_URL and BRIDGE_WEBHOOK_KEY from the secrets file on every call (so editing the
file takes effect without a restart). Sends the key as `Authorization: Bearer <key>` and
`X-Webhook-Key: <key>` (some bot platforms read one, some the other). Only https:// URLs, never
follows redirects, never raises, and never puts the URL or key in a return value.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):      # never forward the webhook key to another host
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def load_env(path: str) -> tuple[str, str]:
    """(url, key) from the secrets file, or ("", "") while unset or REPLACE_ME."""
    vals: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    url, key = vals.get("BRIDGE_WEBHOOK_URL", ""), vals.get("BRIDGE_WEBHOOK_KEY", "")
    if not url or "REPLACE_ME" in url or not key or "REPLACE_ME" in key:
        return "", ""
    return url, key


def post_json(secrets_path: str, payload: dict, *, urlopen=None, timeout: float = 15) -> dict:
    url, key = load_env(secrets_path)
    if not url:
        return {"ok": True, "skipped": "unconfigured"}
    if urllib.parse.urlparse(url).scheme != "https":
        return {"ok": True, "skipped": "not https"}         # never send the key in clear text
    body = json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(url, body, {
        "Content-Type": "application/json", "User-Agent": "claude-bridge/1",
        "Authorization": f"Bearer {key}", "X-Webhook-Key": key})
    try:
        with (urlopen or _OPENER.open)(req, timeout=timeout) as resp:
            return {"ok": True, "status": getattr(resp, "status", 200)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
