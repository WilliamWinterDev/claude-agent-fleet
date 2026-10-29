# Any MCP client

The bridge speaks Streamable-HTTP MCP (stateless, JSON responses) at `/mcp`. Any client that can
send custom headers works. Values below use Option B (tailnet only, `BRIDGE_AUTH=bearer-only`);
for Option A add the two `CF-Access-Client-*` headers and use the https URL.

## Claude Code

```bash
claude mcp add --transport http claude-bridge http://agents-host.your-tailnet.ts.net:8793/mcp \
  --header "Authorization: Bearer $BRIDGE_TOKEN"
```

## JSON config (clients that take an `mcpServers` block)

```json
{
  "mcpServers": {
    "claude-bridge": {
      "type": "http",
      "url": "http://agents-host.your-tailnet.ts.net:8793/mcp",
      "headers": {"Authorization": "Bearer ${BRIDGE_TOKEN}"}
    }
  }
}
```

## curl

```bash
URL=http://agents-host.your-tailnet.ts.net:8793/mcp
H=(-H "Content-Type: application/json" -H "Authorization: Bearer $BRIDGE_TOKEN")
curl -s "${H[@]}" "$URL" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
curl -s "${H[@]}" "$URL" -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_sessions","arguments":{}}}'
curl -s "${H[@]}" "$URL" -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_session","arguments":{"id":"engineering","last_n":10}}}'
```

## Python (stdlib)

[`mcp_client.py`](mcp_client.py) is a tiny client for scripts and testing:

```bash
export BRIDGE_URL=http://agents-host.your-tailnet.ts.net:8793/mcp BRIDGE_TOKEN=...
# Option A also: CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=...
python3 mcp_client.py list
python3 mcp_client.py get engineering 10
python3 mcp_client.py send engineering "Check the open PRs and tell me which are ready to merge"
```
