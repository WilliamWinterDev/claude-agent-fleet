#!/usr/bin/env python3
"""Minimal claude-bridge MCP client (stdlib only), for scripts and testing.

    BRIDGE_URL=https://bridge.example.com/mcp BRIDGE_TOKEN=... \
    [CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=...] \
    python3 mcp_client.py list | get <id> [n] | send <id> "<text>"
"""
import itertools
import json
import os
import sys
import urllib.request


IDS = itertools.count(1)


def call(method, params=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Bearer {os.environ['BRIDGE_TOKEN']}"}
    if os.environ.get("CF_ACCESS_CLIENT_ID"):
        headers["CF-Access-Client-Id"] = os.environ["CF_ACCESS_CLIENT_ID"]
        headers["CF-Access-Client-Secret"] = os.environ["CF_ACCESS_CLIENT_SECRET"]
    body = json.dumps({"jsonrpc": "2.0", "id": next(IDS), "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(os.environ["BRIDGE_URL"], body, headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
    if "error" in res:
        raise SystemExit(f"error: {res['error'].get('message')}")
    return res["result"]


def tool(name, **args):
    res = call("tools/call", {"name": name, "arguments": args})
    text = "\n".join(c.get("text", "") for c in res.get("content", []))
    if res.get("isError"):
        raise SystemExit(f"error: {text}")
    return text


def main(argv):
    call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "mcp_client.py", "version": "1"}})
    if argv[:1] == ["list"]:
        print(tool("list_sessions"))
    elif argv[:1] == ["get"] and len(argv) >= 2:
        print(tool("get_session", id=argv[1], last_n=int(argv[2]) if len(argv) > 2 else 20))
    elif argv[:1] == ["send"] and len(argv) == 3:
        print(tool("send_message", id=argv[1], text=argv[2]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
