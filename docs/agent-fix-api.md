# agent-fix API

`agent-fix` lets a dashboard (anything that tracks alerts) ask the fleet to fix something. The
dashboard can't reach the agent host, so the host polls it every minute.

Config, root-only: `/etc/claude-agents/agent-fix.env`

```
FIX_API_URL=https://dashboard.example.com
FIX_API_TOKEN=...
FIX_AGENT=engineering
```

## Endpoints the dashboard provides

`GET /v1/fixes` returns open alerts that have a fix:

```json
{"fixes": [{"id": "abc123", "key": "web:down", "severity": "warning", "source": "host",
            "title": "Web is down", "body": "HTTP 503", "started_at": "2026-01-01T12:00:00Z",
            "state": "acknowledged", "timeline": [{"at": "...", "text": "Alert raised"}],
            "fix": {"state": "requested", "note": "optional note from the owner", "session": ""}}]}
```

`fix.state`: `requested` (start a task session), `running`, `stopping` (owner pressed Stop).
`state: resolved` on the alert tells a running session to stop and write a root cause analysis.

`POST /v1/fixes/<id>` with any of `{"state", "session", "step", "detail", "summary", "pr_url"}`.
The agent itself calls it through `agent-fix report <id> step|done|failed|stopped "<text>"`.
