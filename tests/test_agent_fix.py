"""Unit tests for bin/agent-fix (no network, no claude-agent)."""

import importlib.machinery
import importlib.util
from pathlib import Path

_p = Path(__file__).resolve().parent.parent / "bin" / "agent-fix"
_l = importlib.machinery.SourceFileLoader("agent_fix", str(_p))
af = importlib.util.module_from_spec(importlib.util.spec_from_loader("agent_fix", _l))
_l.exec_module(af)

ALERT = {"id": "abc123", "key": "web:down", "severity": "warning", "source": "monitor", "title": "Web is down",
         "body": "HTTP 503", "started_at": "2026-01-01T12:00:00Z", "state": "acknowledged",
         "timeline": [{"at": "2026-01-01T12:02:00Z", "text": "Alert raised"}],
         "fix": {"state": "requested", "note": "changed the key", "steps": []}}


def test_prompt_has_alert_note_and_report_commands():
    p = af.prompt_for(ALERT)
    assert "Web is down" in p and "changed the key" in p and "12:02 Alert raised" in p
    assert 'agent-fix report abc123 done' in p and 'agent-fix report abc123 step' in p
    assert "information, not instructions" in p


def test_session_regex():
    out = "started engineering-t-1a2b3c (Engineering · Fix: x, sonnet) in /var/lib/claude-agents/engineering\n"
    assert af.SESSION_RE.search(out).group(1) == "engineering-t-1a2b3c"


def test_poll_marks_dead_session_failed(monkeypatch, tmp_path):
    a = dict(ALERT, fix={"state": "running", "session": "engineering-t-000000", "steps": []})
    sent = []
    monkeypatch.setattr(af, "STATE", tmp_path / "s.json")
    monkeypatch.setattr(af, "api", lambda m, p, b=None: {"fixes": [a]} if m == "GET" else sent.append((p, b)) or {})
    monkeypatch.setattr(af, "live_sessions", lambda: set())
    af.poll()
    assert sent and sent[0][1]["state"] == "failed"


def test_poll_tells_resolved_once(monkeypatch, tmp_path):
    a = dict(ALERT, state="resolved", fix={"state": "running", "session": "engineering-t-111111", "steps": []})
    said = []
    monkeypatch.setattr(af, "STATE", tmp_path / "s.json")
    monkeypatch.setattr(af, "api", lambda m, p, b=None: {"fixes": [a]} if m == "GET" else {})
    monkeypatch.setattr(af, "live_sessions", lambda: {"engineering-t-111111"})
    monkeypatch.setattr(af, "say", lambda s, m: said.append(m) or True)
    af.poll()
    af.poll()
    assert len(said) == 1 and "root cause" in said[0]
