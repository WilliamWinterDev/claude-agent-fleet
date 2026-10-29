"""Tests for the claude-bridge add-on (offline: fake helper, no sudo, no tmux, no network, no Cloudflare)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

CB = Path(__file__).resolve().parent.parent / "bridge"
sys.path.insert(0, str(CB / "lib"))

import bridge_core as core  # noqa: E402
import bridge_events  # noqa: E402
import bridge_server  # noqa: E402
import webhook  # noqa: E402

pytest.importorskip("cryptography")
_loader = importlib.machinery.SourceFileLoader("claude_bridge_helper", str(CB / "claude-bridge-helper"))
_spec = importlib.util.spec_from_loader("claude_bridge_helper", _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)

NOW = 1_800_000_000.0
T = "2027-01-15T08:00:00.000Z"
PREFIX = helper.PREFIX.format(client="Grok", owner="Alice")


def iso(offset=0.0):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW + offset, timezone.utc).isoformat().replace("+00:00", "Z")


def text(role, s, ts=0.0, stop=None):
    return {"role": role, "ts": iso(ts), "stop": stop, "blocks": [{"t": "text", "text": s}]}


def tool(name="Bash", desc="run tests", ts=0.0, **extra):
    return {"role": "assistant", "ts": iso(ts), "stop": "tool_use",
            "blocks": [{"t": "tool_use", "name": name, "id": "t1", "desc": desc, **extra}]}


def snap(entries, running=True, mtime=None):
    return {"id": "engineering", "running": running, "entries": entries,
            "mtime": NOW if mtime is None else mtime}


SESSIONS = {"sessions": [{"id": "engineering", "name": "Engineering", "running": True}]}


def fake(fn):
    """Wrap fn(cmd, req) as a helper that also answers `sessions`."""
    return lambda cmd, req=None: SESSIONS if cmd == "sessions" else fn(cmd, req or {})


# ---- helper: transcript reduction ------------------------------------------------------

def test_reduce_entry_drops_tool_inputs_and_outputs():
    o = {"type": "assistant", "timestamp": T, "message": {"role": "assistant", "stop_reason": "tool_use", "content": [
        {"type": "thinking", "thinking": "secret thoughts"},
        {"type": "tool_use", "id": "x1", "name": "Bash",
         "input": {"command": "curl -H 'Authorization: Bearer sk-live-abcdefghijklmnop' gh pr create", "description": "Open the PR"}}]}}
    e = helper.reduce_entry(o)
    blob = json.dumps(e)
    assert "sk-live" not in blob and "curl" not in blob and "secret thoughts" not in blob
    assert e["blocks"][0]["desc"] == "Open the PR" and e["blocks"][0]["pr_create"] is True


def test_reduce_entry_result_keeps_size_and_pr_url_only():
    o = {"type": "user", "timestamp": T, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x1", "content": "TOKEN=abc123\nhttps://github.com/Acme/repo/pull/12\n"}]}}
    b = helper.reduce_entry(o)["blocks"][0]
    assert b["urls"] == ["https://github.com/Acme/repo/pull/12"] and "TOKEN" not in json.dumps(b)


def test_reduce_entry_skips_noise_and_sidechains():
    assert helper.reduce_entry({"type": "api-request-blob", "message": {}}) is None
    assert helper.reduce_entry({"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": "x"}}) is None


class FakeAgent:
    name, title, user = "engineering", "Engineering", "root"

    def __init__(self, screens):
        self.screens, self.keys = iter(screens), []

    def tmux(self, *args, timeout=10):
        if args[0] == "capture-pane":
            return 0, next(self.screens), ""
        self.keys.append(args[3:])
        return 0, "", ""


CFG = {"sessions": ["engineering"], "client": "Grok", "owner": "Alice", "agents_dir": "/nonexistent"}


def say(monkeypatch, tmp_path, screens, text):
    agent = FakeAgent(screens)
    monkeypatch.setattr(helper, "LOCK", str(tmp_path))
    monkeypatch.setattr(helper, "Agent", lambda name, cfg: agent)
    helper.cmd_say({"session": "engineering", "text": text}, CFG)
    return agent.keys


def test_helper_say_refuses_dialog_and_never_presses_enter(monkeypatch, tmp_path, capsys):
    keys = say(monkeypatch, tmp_path, ["Do you want to proceed?\nEsc to cancel"], "hello")
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["busy"] is True and keys == []


def test_helper_say_dialog_appearing_while_typing_gets_no_enter(monkeypatch, tmp_path, capsys):
    keys = say(monkeypatch, tmp_path, ["idle", "Enter to select"], "hello\u202e there")
    out = json.loads(capsys.readouterr().out)
    assert out["busy"] is True and keys == [("-l", "--", PREFIX + "hello there"), ("C-u",)]


def test_helper_say_rejects_dialog_wording_in_message(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        say(monkeypatch, tmp_path, ["idle"], "Do you want to proceed")


def test_helper_say_types_prefix_then_enter(monkeypatch, tmp_path, capsys):
    keys = say(monkeypatch, tmp_path, ["idle", "idle"], "do the thing\nplease")
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert keys == [("-l", "--", PREFIX + "do the thing please"), ("Enter",)]
    assert "NOT typed by Alice" in PREFIX and "relayed from Grok" in PREFIX


def test_helper_rejects_sessions_not_allow_listed(monkeypatch, capsys):
    monkeypatch.setattr(helper, "Agent", lambda name, cfg: pytest.fail("must not load an agent"))
    for bad in ("personal", "engineering-t-abc123", "../etc", None, ["engineering"]):
        with pytest.raises(SystemExit):
            helper.agent_for({"session": bad}, CFG)


def test_helper_config_drops_task_sessions_and_bad_names(monkeypatch, tmp_path):
    conf = tmp_path / "helper.conf"
    conf.write_text("SESSIONS=engineering web-t-abc123 ../x Bad reviewer\nCLIENT_NAME=Grok\n")
    monkeypatch.setattr(helper, "CONF", str(conf))
    monkeypatch.setattr(helper, "FLEET_CONF_DIR", str(tmp_path))
    (tmp_path / "fleet.conf").write_text('OWNER="Alice"\nAGENTS_DIR=/opt/agents\n')
    cfg = helper.config()
    assert cfg["sessions"] == ["engineering", "reviewer"] and cfg["owner"] == "Alice" and cfg["client"] == "Grok"


def test_helper_main_refuses_extra_arguments(monkeypatch, capsys):
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    for argv in (["h"], ["h", "status", "--entries", "9"], ["h", "start"], ["h", "say", "x"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            helper.main()
        assert "unknown command" in capsys.readouterr().out


def test_reduce_entry_flags_relayed_messages():
    o = {"type": "user", "timestamp": T, "message": {"role": "user", "content": PREFIX + "hi"}}
    assert helper.reduce_entry(o)["blocks"][0]["relayed"] is True
    o = {"type": "user", "timestamp": T, "message": {"role": "user", "content": "hi"}}
    assert "relayed" not in helper.reduce_entry(o)["blocks"][0]


def test_screen_dialog_only_looks_at_bottom_lines():
    old = "Do you want to proceed?\n" + "\n".join(f"output line {i}" for i in range(30))
    assert core.screen_has_dialog(old) is False
    assert core.screen_has_dialog("x\n" * 5 + "Esc to cancel") is True


def test_surrogates_do_not_use_up_rate_limit():
    b, _ = make_bridge()
    with pytest.raises(ValueError, match="invalid characters"):
        b.send_message(None, "bad \ud800 text")
    assert len(b.send_limit.hits) == 0


def test_helper_has_no_start_stop_or_shell():
    src = (CB / "claude-bridge-helper").read_text()
    assert set(helper.COMMANDS) == {"sessions", "status", "transcript", "peek", "say"}
    assert "shell=True" not in src and "os.system" not in src
    sudoers = (CB / "claude-bridge.sudoers").read_text()
    for c in helper.COMMANDS:
        assert f"/usr/local/sbin/claude-bridge-helper {c}," in sudoers or sudoers.rstrip().count(f"helper {c}\n")
    assert "*" not in sudoers


# ---- session listing / status ----------------------------------------------------------

def test_list_sessions_single_master_running():
    b = core.Bridge(helper=fake(lambda c, r: snap([text("assistant", "Deployed the app.", -5, "end_turn")])), now=lambda: NOW)
    out = b.list_sessions()["sessions"]
    assert len(out) == 1 and out[0]["id"] == "engineering"
    assert out[0]["status"] == "idle" and out[0]["summary"] == "Deployed the app."


def test_status_running_when_tool_call_recent():
    s = core.compute_status(snap([text("user", "do it", -20), tool(ts=-3)]), NOW)
    assert s["status"] == "running"


def test_status_stale_running_becomes_idle():
    s = core.compute_status(snap([tool(ts=-5000)], mtime=NOW - 5000), NOW)
    assert s["status"] == "idle"


def test_status_needs_input_from_ask_user_question():
    ask = json.dumps({"questions": [{"question": "Merge now?", "options": [{"label": "Yes"}, {"label": "No"}]}]})
    s = core.compute_status(snap([tool("AskUserQuestion", "", -2, ask=ask)]), NOW)
    assert s["status"] == "needs_input" and "Merge now?" in s["question"] and "Yes / No" in s["question"]


def test_status_needs_input_from_trailing_question():
    s = core.compute_status(snap([text("assistant", "Done.\n\nShall I restart the pod?", -1, "end_turn")]), NOW)
    assert s["status"] == "needs_input" and s["question"] == "Shall I restart the pod?"


def test_status_pending_tool_with_dialog_on_screen():
    e = [tool(ts=-120)]
    assert core.compute_status(snap(e, mtime=NOW - 120), NOW, peek=lambda: "Do you want to proceed?\n Esc to cancel")["status"] == "needs_input"
    assert core.compute_status(snap(e, mtime=NOW - 120), NOW, peek=lambda: "working...")["status"] == "running"


def test_status_stopped():
    assert core.compute_status(snap([], running=False), NOW)["status"] == "stopped"


def test_summary_is_redacted_and_capped():
    long = "token=hunter2hunter2 " + "x " * 600
    s = core.compute_status(snap([text("assistant", long, -1, "end_turn")]), NOW)
    assert "hunter2" not in s["summary"] and len(s["summary"]) <= core.SUMMARY_CAP


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "Authorization: Bearer abcdefghijklmnop1234", "CF_ACCESS_CLIENT_SECRET=0123456789abcdef0123456789abcdef",
    "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk", "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
    "hvs.CAESIabcdefghijklmnopqrst", "xai-abcdefghijklmnopqrstuvwx", "AIzaSyabcdefghijklmnopqrstuvw",
    'postgres://admin:S3cretPass!abcdefghijklmnop@db:5432/x', '"api_key": "abcdefghijklmnop1234"',
    "the password is hunter2hunter2abcdefghijklmnop", "mysql -pS3cretPw1x", "DB_PASS=abc12345", "token abcdefghijklmnop12",
])
def test_redact(secret):
    out = core.redact(f"before {secret} after")
    assert "abcdefghijklmnop" not in out and "MIIE" not in out and "eyJhbGci" not in out and "0123456789abcdef" not in out
    assert out.startswith("before") and out.endswith("after")


def test_redact_leaves_ordinary_words_alone():
    t = "session_identifier worktrees/engineering-t-abc123 deploying successfully author: Alice"
    assert core.redact(t) == t


def test_invisible_characters_are_stripped_from_sent_text():
    b, calls = make_bridge()
    b.send_message(None, "hi\u202eevil\u200bzero\x85width\ttab")
    assert calls[-1] == ("say", {"session": "engineering", "text": "hi evil zero width tab"})


def test_send_message_helper_busy_means_nothing_sent():
    calls = []

    def h(cmd, req):
        calls.append(cmd)
        return {"status": snap([text("assistant", "ok", -30, "end_turn")]), "peek": {"screen": ""},
                "say": {"ok": False, "busy": True}}[cmd]
    b = core.Bridge(helper=fake(h), now=lambda: NOW)
    with pytest.raises(ValueError, match="busy"):
        b.send_message(None, "x")


def test_reads_are_cached_briefly_and_limited():
    n = {"c": 0}

    def h(cmd, req):
        n["c"] += 1
        return snap([text("assistant", "ok", -30, "end_turn")])
    b = core.Bridge(helper=fake(h), now=lambda: NOW)
    for _ in range(10):
        b.list_sessions()
    assert n["c"] == 1
    b.read_limit = core.RateLimiter(2, 60)
    b._cache.clear()
    b.list_sessions()
    b.list_sessions()
    with pytest.raises(ValueError, match="rate limited"):
        b.list_sessions()


def test_user_text_is_shortened():
    b = core.Bridge(helper=fake(lambda c, r: snap([text("user", "handoff " * 100, -50), text("assistant", "ok", -30, "end_turn")])), now=lambda: NOW)
    out = b.get_session()
    assert max(len(ln) for ln in out.splitlines() if ln.startswith("User:")) <= core.USER_TEXT_CAP + 10


def test_get_session_text_has_no_tool_output_and_is_capped():
    entries = [text("user", "check it", -50), tool(desc="kubectl get pods", ts=-40),
               {"role": "user", "ts": iso(-39), "stop": None, "blocks": [{"t": "tool_result", "id": "t1", "err": False, "n": 99999}]}]
    entries += [text("assistant", "word " * 250, -i, "end_turn") for i in range(30, 0, -1)]
    b = core.Bridge(helper=fake(lambda c, r: snap(entries)), now=lambda: NOW)
    out = b.get_session("engineering", 50)
    assert len(out) <= core.GET_SESSION_CAP and out.startswith(core.UNTRUSTED_NOTE)
    assert "99999 chars" not in out or "[result ok, 99999 chars]" in out
    short = core.Bridge(helper=fake(lambda c, r: snap(entries[:3])), now=lambda: NOW).get_session(None, 5)
    assert "[tool Bash: kubectl get pods]" in short and "[result ok, 99999 chars]" in short


def test_get_session_rejects_other_ids():
    b = core.Bridge(helper=fake(lambda c, r: snap([])), now=lambda: NOW)
    for sid in ("personal", "engineering-t-abc123", "../etc", 5):
        with pytest.raises(ValueError):
            b.get_session(sid)


def test_empty_id_needs_exactly_one_session():
    two = {"sessions": [{"id": "engineering"}, {"id": "reviewer"}]}
    b = core.Bridge(helper=lambda c, r=None: two if c == "sessions" else snap([]), now=lambda: NOW)
    with pytest.raises(ValueError, match="unknown session"):
        b.get_session(None)
    assert b.get_session("reviewer").startswith(core.UNTRUSTED_NOTE)


# ---- send_message ------------------------------------------------------------------------

def make_bridge(screen="idle prompt", running=True, entries=None, say_ok=True):
    calls = []

    def h(cmd, req):
        calls.append((cmd, req))
        if cmd in ("status", "transcript"):
            return snap(entries or [text("assistant", "ok", -30, "end_turn")], running)
        if cmd == "peek":
            return {"screen": screen}
        if cmd == "say":
            return {"ok": say_ok}
    return core.Bridge(helper=fake(h), now=lambda: NOW), calls


def test_send_message_prefixes_and_delivers():
    b, calls = make_bridge()
    assert b.send_message("engineering", "deploy the thing") == "delivered"
    assert calls[-1] == ("say", {"session": "engineering", "text": "deploy the thing"})


def test_send_message_busy_when_dialog_on_screen_and_nothing_typed():
    b, calls = make_bridge(screen="Enter to select · Esc to cancel")
    with pytest.raises(ValueError, match="busy"):
        b.send_message(None, "hello")
    assert not any(c[0] == "say" for c in calls)


def test_send_message_reports_queued_when_running():
    b, _ = make_bridge(entries=[tool(ts=-2)])
    assert "queued" in b.send_message(None, "later")


def test_send_message_not_running_unknown_id_empty_and_too_long():
    b, calls = make_bridge(running=False)
    with pytest.raises(ValueError, match="not running"):
        b.send_message(None, "x")
    b, _ = make_bridge()
    for bad in ("", "   ", "x" * (core.MAX_MESSAGE + 1)):
        with pytest.raises(ValueError):
            b.send_message(None, bad)
    with pytest.raises(ValueError, match="unknown session"):
        b.send_message("personal", "x")


def test_send_message_rate_limit_and_audit_has_no_body(tmp_path):
    audit = core.Audit(str(tmp_path / "audit.log"))
    clock = [0.0]
    b, _ = make_bridge()
    b.audit, b.send_limit = audit, core.RateLimiter(2, 60, clock=lambda: clock[0])
    b.send_message(None, "secret body one")
    b.send_message(None, "secret body two")
    with pytest.raises(ValueError, match="rate limited"):
        b.send_message(None, "secret body three")
    clock[0] = 61
    b.send_message(None, "secret body four")
    log = (tmp_path / "audit.log").read_text()
    assert "secret body" not in log
    rec = [json.loads(line) for line in log.splitlines()]
    assert [r["result"] for r in rec] == ["ok", "ok", "rate_limited", "ok"] and rec[0]["len"] == len("secret body one")
    assert {"ts", "tool", "session", "len"} <= set(rec[0])


# ---- webhook dedupe ----------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def make_watcher(tmp_path, script):
    """script: list that tests pop snapshots from; posts are collected."""
    posts, clock = [], Clock()
    cur = {"snap": snap([])}
    b = core.Bridge(helper=fake(lambda c, r: cur["snap"] if c in ("transcript", "status") else {"screen": ""}), now=clock)
    w = bridge_events.Watcher(b, "engineering", "Engineering", "/nonexistent", str(tmp_path / "state.json"),
                              post=lambda p: (posts.append(p), {"ok": True})[1], now=clock)
    return w, posts, cur, clock


def client_msg(ts):
    e = text("user", PREFIX + "check the deploy", ts)
    e["blocks"][0]["relayed"] = True             # set by the helper's reduce_entry
    return e


def test_webhook_baseline_posts_nothing(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    w.mark_client_turn()
    cur["snap"] = snap([client_msg(-5), text("assistant", "Which one?", -1, "end_turn")])
    w.poll_once()
    assert posts == []


def test_webhook_owner_started_turn_sends_nothing(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    w.poll_once()
    cur["snap"] = snap([text("user", "deploy it", -30), tool(ts=-1)])
    w.poll_once()
    clock.t += 60
    cur["snap"] = snap([text("user", "deploy it", -90), tool(ts=-61), text("assistant", "Deployed.", -1, "end_turn")])
    for _ in range(4):
        w.poll_once()
        clock.t += 20
    cur["snap"] = snap([text("assistant", "Deployed.", -100, "end_turn"), text("user", "ok", 5), text("assistant", "Merge it?", 30, "end_turn")])
    pr = [tool("Bash", "Open PR", 31, pr_create=True),
          {"role": "user", "ts": iso(32), "stop": None, "blocks": [{"t": "tool_result", "id": "t1", "err": False, "n": 5, "urls": ["https://github.com/Acme/repo/pull/9"]}]}]
    cur["snap"] = snap(pr)
    for _ in range(3):
        w.poll_once()
        clock.t += 20
    assert posts == []
    assert "https://github.com/Acme/repo/pull/9" in w.state["prs"]       # recorded, never replayed


def test_webhook_client_started_turn_sends_exactly_one_done(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    w.poll_once()                                                    # baseline
    w.mark_client_turn()                                               # send_message delivered
    cur["snap"] = snap([text("assistant", "Earlier.", -200, "end_turn"), client_msg(2), tool(ts=3)])
    clock.t += 10
    w.poll_once()
    assert posts == [] and w.state["client"]["user_ts"] is not None
    cur["snap"] = snap([client_msg(2), tool(ts=3), text("assistant", "All done, PR merged.", 40, "end_turn")])
    clock.t += 60
    for _ in range(5):
        w.poll_once()
        clock.t += 20
    assert [p["status"] for p in posts] == ["done"] and posts[0]["summary"] == "All done, PR merged."
    assert "client" not in w.state
    cur["snap"] = snap([text("assistant", "All done, PR merged.", 40, "end_turn"), text("user", "thanks", 500), text("assistant", "Anytime.", 520, "end_turn")])
    for _ in range(3):
        clock.t += 20
        w.poll_once()
    assert len(posts) == 1                                           # owner's later turn: nothing


def test_webhook_client_turn_not_started_while_will_turn_still_running(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    w.poll_once()
    w.mark_client_turn()
    cur["snap"] = snap([text("user", "big job", -50), tool(ts=-1)])      # owner's turn, client's message still queued
    w.poll_once()
    cur["snap"] = snap([text("user", "big job", -50), tool(ts=-40), text("assistant", "Big job done.", -30, "end_turn")])
    for _ in range(3):
        clock.t += 20
        w.poll_once()
    assert posts == [] and w.state["client"]["user_ts"] is None


def test_webhook_client_turn_needs_input_and_pr_then_done(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    w.poll_once()
    w.mark_client_turn()
    ask = json.dumps({"questions": [{"question": "Merge now?", "options": [{"label": "Yes"}]}]})
    cur["snap"] = snap([client_msg(1), tool("AskUserQuestion", "", 2, ask=ask)])
    for _ in range(3):
        clock.t += 20
        w.poll_once()
    assert [p["status"] for p in posts] == ["needs_input"] and "Merge now?" in posts[0]["question"]
    assert w.state.get("client")                                       # mid-turn dialog: turn still Grok's
    pr = [client_msg(1), tool("Bash", "Open PR", 3, pr_create=True),
          {"role": "user", "ts": iso(4), "stop": None, "blocks": [{"t": "tool_result", "id": "t1", "err": False, "n": 50,
                                                                    "urls": ["https://github.com/Acme/repo/pull/7"]}]},
          tool("Bash", "next", 5)]
    cur["snap"] = snap(pr)
    for _ in range(3):
        clock.t += 20
        w.poll_once()
    assert [(p["status"], p.get("pr_url")) for p in posts][1:] == [("pr_opened", "https://github.com/Acme/repo/pull/7")]
    cur["snap"] = snap(pr + [text("assistant", "Opened.", 60, "end_turn")])
    for _ in range(3):
        clock.t += 20
        w.poll_once()
    assert [p["status"] for p in posts] == ["needs_input", "pr_opened", "done"]


def test_webhook_turn_ending_on_a_question_posts_needs_input_and_clears_mark(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    w.poll_once()
    w.mark_client_turn()
    cur["snap"] = snap([client_msg(1), text("assistant", "Delete the branch?", 30, "end_turn")])
    for _ in range(4):
        clock.t += 20
        w.poll_once()
    assert [p["status"] for p in posts] == ["needs_input"] and "client" not in w.state


def test_webhook_client_mark_survives_restart(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    w.poll_once()
    w.mark_client_turn()
    w.poll_once()                                                    # the mark is persisted by the next poll
    w2 = bridge_events.Watcher(w.bridge, "engineering", "Engineering", "/nonexistent", str(tmp_path / "state.json"),
                               post=lambda p: (posts.append(p), {"ok": True})[1], now=clock)
    w2.poll_once()                                                   # baseline in the new process
    cur["snap"] = snap([client_msg(1), text("assistant", "Done.", 30, "end_turn")])
    clock.t += 20
    w2.poll_once()
    assert [p["status"] for p in posts] == ["done"]


def test_webhook_stale_client_mark_is_dropped(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    w.poll_once()
    w.mark_client_turn()
    clock.t += bridge_events.CLIENT_STALE + 10
    cur["snap"] = snap([text("assistant", "x", -1, "end_turn")])
    w.poll_once()
    assert "client" not in w.state and posts == []


def test_send_message_marks_a_client_turn():
    b, _ = make_bridge()
    marked = []
    b.on_delivered = lambda sid: marked.append(sid)
    b.send_message(None, "go")
    assert marked == ["engineering"]
    b2, _ = make_bridge(screen="Esc to cancel")
    b2.on_delivered = lambda sid: marked.append(2)
    with pytest.raises(ValueError):
        b2.send_message(None, "go")
    assert marked == ["engineering"]                                             # a refused send is not a Grok turn


def test_webhook_failed_always_posts_even_without_client_turn(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    cur["snap"] = snap([], running=False)
    w.poll_once()
    w.poll_once()
    assert posts == []                                               # never seen running: no event
    cur["snap"] = snap([tool(ts=-1)])
    w.poll_once()
    w.mark_client_turn()
    cur["snap"] = snap([], running=False)
    w.poll_once()
    w.poll_once()
    assert [p["status"] for p in posts] == ["failed"] and "client" not in w.state


def test_webhook_failed_post_is_retried_not_lost(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    results = iter([{"ok": False, "error": "HTTP 500"}, {"ok": True}])
    w.post = lambda p: (posts.append(p), next(results))[1]
    w.poll_once()
    w.mark_client_turn()
    cur["snap"] = snap([client_msg(1), tool("AskUserQuestion", "", 2, ask=json.dumps({"questions": [{"question": "Ready?"}]}))])
    w.poll_once()
    w.poll_once()
    w.poll_once()
    assert len(posts) == 2 and w.pending == {}
    clock.t += 30
    w.poll_once()
    assert len(posts) == 2          # delivered once, not again


def test_webhook_refuses_plain_http_and_redirects(tmp_path):
    env = tmp_path / "s.env"
    env.write_text("BRIDGE_WEBHOOK_URL=http://example.invalid/h\nBRIDGE_WEBHOOK_KEY=k-123\n")
    assert webhook.post_json(str(env), {"a": 1}, urlopen=lambda *a, **k: pytest.fail("posted")) == {"ok": True, "skipped": "not https"}
    assert webhook._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid") is None


def test_webhook_sends_compact_body_and_headers(tmp_path):
    seen = {}

    class R:
        status = 200

        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=0):
        seen["body"], seen["h"] = req.data, dict(req.header_items())
        return R()

    env = tmp_path / "s.env"
    env.write_text("BRIDGE_WEBHOOK_URL=https://example.invalid/hook\nBRIDGE_WEBHOOK_KEY=k-123\n")
    res = webhook.post_json(str(env), {"a": 1, "b": "x"}, urlopen=fake_open)
    assert res["ok"] and seen["body"] == b'{"a":1,"b":"x"}'
    assert seen["h"]["Authorization"] == "Bearer k-123" and seen["h"]["X-webhook-key"] == "k-123"
    assert seen["h"]["User-agent"] == "claude-bridge/1"


def test_webhook_errors_never_contain_url_or_key(tmp_path):
    env = tmp_path / "s.env"
    env.write_text("BRIDGE_WEBHOOK_URL=https://hook.example.invalid/secret-path\nBRIDGE_WEBHOOK_KEY=k-123\n")

    def boom(req, timeout=0):
        raise OSError("connect to https://hook.example.invalid/secret-path with k-123 failed")
    res = webhook.post_json(str(env), {"a": 1}, urlopen=boom)
    assert res["ok"] is False and "secret-path" not in json.dumps(res) and "k-123" not in json.dumps(res)


def test_webhook_disabled_while_unconfigured(tmp_path):
    env = tmp_path / "s.env"
    env.write_text("BRIDGE_WEBHOOK_URL=REPLACE_ME\nBRIDGE_WEBHOOK_KEY=REPLACE_ME\n")
    assert webhook.post_json(str(env), {"a": 1}) == {"ok": True, "skipped": "unconfigured"}


# ---- HTTP / MCP / auth ---------------------------------------------------------------------

TOKEN = "t" * 40


@pytest.fixture
def server(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"BRIDGE_BEARER_TOKEN={TOKEN}\n")
    b = core.Bridge(helper=fake(lambda c, r: snap([text("assistant", "hi", -30, "end_turn")]) if c != "peek" else {"screen": ""}),
                    audit=core.Audit(str(tmp_path / "a.log")), now=lambda: NOW)
    verifier = bridge_server.AccessVerifier("team.example", "aud-1", fetch=lambda: {"keys": []})
    verifier.verify = lambda tok: tok == "good-jwt"
    app = bridge_server.App(b, str(secrets), verifier, access_required=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), bridge_server.make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app
    srv.shutdown()


def post(url, body, headers=None):
    req = urllib.request.Request(url + "/mcp", json.dumps(body).encode(), {"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


GOOD = {"Authorization": f"Bearer {TOKEN}", "Cf-Access-Jwt-Assertion": "good-jwt"}
CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {}}}


def test_mcp_requires_both_layers(server):
    url, _ = server
    assert post(url, CALL, GOOD)[0] == 200
    assert post(url, CALL)[0] == 401
    assert post(url, CALL, {"Authorization": f"Bearer {TOKEN}"})[0] == 401                       # no Access JWT
    assert post(url, CALL, {"Cf-Access-Jwt-Assertion": "good-jwt"})[0] == 401                    # no bearer
    assert post(url, CALL, {**GOOD, "Authorization": "Bearer wrong" + "t" * 35})[0] == 401       # wrong bearer
    assert post(url, CALL, {**GOOD, "Cf-Access-Jwt-Assertion": "forged"})[0] == 401
    assert post(url, CALL, {**GOOD, "Origin": "https://evil.example"})[0] == 403


def test_mcp_fails_closed_without_configured_token(server, tmp_path):
    url, app = server
    open(app.secrets_path, "w").write("BRIDGE_BEARER_TOKEN=REPLACE_ME\n")
    assert post(url, CALL, {**GOOD, "Authorization": "Bearer REPLACE_ME"})[0] == 401


def test_mcp_malformed_params_and_huge_last_n_do_not_crash(server):
    url, _ = server
    for bad in ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_session", "arguments": {"last_n": 1e999}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_session", "arguments": {"last_n": "x"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "send_message", "arguments": {"text": 5}}}):
        code, body = post(url, bad, GOOD)
        assert code == 200 and json.loads(body)


def test_error_responses_close_the_connection(server):
    url, _ = server
    req = urllib.request.Request(url + "/mcp", b"{}", {"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req)
    assert ei.value.headers.get("Connection") == "close"


def test_mcp_failed_auth_is_throttled(server):
    url, _ = server
    codes = [post(url, CALL)[0] for _ in range(25)]
    assert 429 in codes and codes[0] == 401


def test_mcp_tools_and_no_start_or_stop(server):
    url, _ = server
    _, body = post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, GOOD)
    names = [t["name"] for t in json.loads(body)["result"]["tools"]]
    assert names == ["list_sessions", "get_session", "send_message"]
    _, body = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "start_task", "arguments": {}}}, GOOD)
    assert json.loads(body)["error"]["code"] == -32602


def test_mcp_initialize_notification_and_methods(server):
    url, _ = server
    _, body = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, GOOD)
    assert json.loads(body)["result"]["protocolVersion"] == "2025-03-26"
    assert post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, GOOD)[0] == 202
    _, body = post(url, CALL, GOOD)
    res = json.loads(body)["result"]
    assert res["isError"] is False and json.loads(res["content"][0]["text"])["sessions"][0]["id"] == "engineering"
    req = urllib.request.Request(url + "/mcp", headers=GOOD)
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req)
    assert ei.value.code == 405


def test_mcp_tool_errors_are_results_not_crashes(server):
    url, _ = server
    _, body = post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "send_message", "arguments": {"id": "personal", "text": "hi"}}}, GOOD)
    r = json.loads(body)["result"]
    assert r["isError"] is True and "unknown session" in r["content"][0]["text"]


def test_body_size_limit(server):
    url, _ = server
    assert post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": "y" * 70000}}, GOOD)[0] == 413


def test_access_verifier_rejects_garbage_and_wrong_aud():
    v = bridge_server.AccessVerifier("team.example", "aud-1", fetch=lambda: {"keys": []})
    assert v.verify("") is False and v.verify("a.b.c") is False


def test_access_verifier_accepts_real_signature():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    import base64

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.public_key().public_numbers()
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": b64(nums.n.to_bytes(256, "big")), "e": b64(nums.e.to_bytes(3, "big"))}]}

    def mint(aud="aud-1", iss="https://team.example", exp=NOW + 100, kid="k1"):
        h = b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
        p = b64(json.dumps({"aud": [aud], "iss": iss, "exp": exp}).encode())
        sig = key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{h}.{p}.{b64(sig)}"

    v = bridge_server.AccessVerifier("team.example", "aud-1", fetch=lambda: jwks, clock=lambda: NOW)
    assert v.verify(mint()) is True
    assert v.verify(mint(aud="other")) is False
    assert v.verify(mint(iss="https://evil.example")) is False
    assert v.verify(mint(exp=NOW - 1)) is False
    good = mint()
    assert v.verify(good[:-4] + "AAAA") is False


def test_bearer_only_mode_needs_no_access_jwt_but_still_the_token(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"BRIDGE_BEARER_TOKEN={TOKEN}\n")
    app = bridge_server.App(core.Bridge(helper=fake(lambda c, r: snap([]))), str(secrets), None, access_required=False)
    assert app.authorize({"Authorization": f"Bearer {TOKEN}"}) == (0, "")
    assert app.authorize({})[0] == 401
    assert app.authorize({"Authorization": f"Bearer {TOKEN}", "Origin": "http://x"})[0] == 403


def test_access_mode_without_config_refuses_everything(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"BRIDGE_BEARER_TOKEN={TOKEN}\n")
    app = bridge_server.App(core.Bridge(helper=fake(lambda c, r: snap([]))), str(secrets), None, access_required=True)
    assert app.authorize({"Authorization": f"Bearer {TOKEN}", "Cf-Access-Jwt-Assertion": "x"})[0] == 401


@pytest.mark.parametrize("bind", ["0.0.0.0:8793", ":8793", "[::]:8793", "*:8793"])
def test_wildcard_bind_is_refused(bind):
    with pytest.raises(SystemExit):
        bridge_server.parse_bind(bind)


def test_bind_parses_one_address():
    assert bridge_server.parse_bind("127.0.0.1:8793") == ("127.0.0.1", 8793)
    assert bridge_server.parse_bind("[fd7a::1]:8793") == ("fd7a::1", 8793)


def test_example_secrets_are_placeholders():
    lines = [ln for ln in (CB / "secrets.env.example").read_text().splitlines() if ln and not ln.startswith("#")]
    assert lines and all(ln.endswith("=REPLACE_ME") for ln in lines)


def test_helper_refuses_config_others_can_write(monkeypatch, tmp_path):
    conf = tmp_path / "helper.conf"
    conf.write_text("SESSIONS=engineering\n")
    conf.chmod(0o666)
    with pytest.raises(helper.ConfigError):
        helper.read_kv(str(conf))


def test_helper_agent_reads_fleet_config_without_sourcing(monkeypatch, tmp_path):
    import getpass
    (tmp_path / "web.env").write_text(f'RC_NAME="Web $(touch {tmp_path}/pwned)"\nRUN_AS={getpass.getuser()}\n')
    monkeypatch.setattr(helper, "FLEET_CONF_DIR", str(tmp_path))
    a = helper.Agent("web", {"agents_dir": "/opt/agents"})
    assert a.dir == "/opt/agents/web" and a.sock == "claude-web" and a.title.startswith("Web $(")
    assert not (tmp_path / "pwned").exists()


def test_example_client_end_to_end(tmp_path):
    """bridge/examples/mcp_client.py against a live bearer-only server."""
    import subprocess
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"BRIDGE_BEARER_TOKEN={TOKEN}\n")
    b = core.Bridge(helper=fake(lambda c, r: snap([text("assistant", "All green.", -30, "end_turn")]) if c != "peek" else {"screen": ""}),
                    now=lambda: NOW)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), bridge_server.make_handler(bridge_server.App(b, str(secrets), None, False)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = {"PATH": "/usr/bin:/bin", "BRIDGE_URL": f"http://127.0.0.1:{srv.server_address[1]}/mcp", "BRIDGE_TOKEN": TOKEN}
        r = subprocess.run([sys.executable, str(CB / "examples" / "mcp_client.py"), "list"], env=env,
                           capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["sessions"][0]["summary"] == "All green."
        env["BRIDGE_TOKEN"] = "wrong" * 8
        r = subprocess.run([sys.executable, str(CB / "examples" / "mcp_client.py"), "list"], env=env,
                           capture_output=True, text=True, timeout=20)
        assert r.returncode != 0 and "401" in r.stderr
    finally:
        srv.shutdown()


def test_helper_say_lock_is_the_directory_and_busy_when_held(monkeypatch, tmp_path, capsys):
    import fcntl
    import os
    monkeypatch.setattr(helper, "LOCK_WAIT", 0.3)
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        keys = say(monkeypatch, tmp_path, ["idle", "idle"], "hello")
    finally:
        os.close(fd)
    assert keys == [] and json.loads(capsys.readouterr().out)["busy"] is True
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    monkeypatch.setattr(helper, "LOCK", str(link))
    monkeypatch.setattr(helper, "Agent", lambda name, cfg: FakeAgent(["idle"]))
    with pytest.raises(SystemExit):
        helper.cmd_say({"session": "engineering", "text": "x"}, CFG)


def test_helper_refuses_symlinked_or_foreign_transcripts(tmp_path):
    import os
    real = tmp_path / "other.jsonl"
    real.write_text(json.dumps({"type": "user", "timestamp": T, "message": {"role": "user", "content": "secret"}}) + "\n")
    assert helper.read_entries(str(real), 5, os.getuid())[0]
    assert helper.read_entries(str(real), 5, os.getuid() + 1) == ([], 0.0)
    link = tmp_path / "mine.jsonl"
    link.symlink_to(real)
    assert helper.read_entries(str(link), 5, os.getuid()) == ([], 0.0)


def test_reduce_entry_strips_url_query():
    o = {"type": "assistant", "timestamp": T, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "x", "name": "WebFetch", "input": {"url": "https://files.example.com/a.zip?X-Amz-Signature=abc&code=zz"}}]}}
    assert helper.reduce_entry(o)["blocks"][0]["desc"] == "https://files.example.com/a.zip"


def test_helper_sessions_reports_misconfigured_agent_instead_of_failing(monkeypatch, capsys):
    def agent(name, cfg):
        if name == "broken":
            raise helper.ConfigError("session not configured")
        a = FakeAgent([])
        a.running = lambda: True
        return a
    monkeypatch.setattr(helper, "Agent", agent)
    helper.cmd_sessions({}, {**CFG, "sessions": ["engineering", "broken"]})
    out = json.loads(capsys.readouterr().out)["sessions"]
    assert out[0]["running"] is True and out[1] == {"id": "broken", "name": "broken", "running": False, "misconfigured": True}


def test_refused_sends_do_not_spend_the_message_quota():
    b, _ = make_bridge(screen="Esc to cancel")
    b.send_limit = core.RateLimiter(1, 600)
    for _ in range(3):
        with pytest.raises(ValueError, match="busy"):
            b.send_message(None, "x")
    assert len(b.send_limit.hits) == 0


def test_throttled_auth_failures_are_not_logged(server, tmp_path):
    url, _ = server
    for _ in range(40):
        post(url, CALL)
    lines = (tmp_path / "a.log").read_text().splitlines()
    assert len(lines) == 20 and all("denied_401" in ln for ln in lines)


def test_webhook_second_send_during_client_turn_gets_its_own_done(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    w.poll_once()
    w.mark_client_turn()
    cur["snap"] = snap([client_msg(1), tool(ts=2)])
    clock.t += 20
    w.poll_once()
    w.mark_client_turn()                                             # second message while the first runs
    cur["snap"] = snap([client_msg(1), tool(ts=2), text("assistant", "First done.", 30, "end_turn")])
    clock.t += 20
    w.poll_once()
    assert [p["summary"] for p in posts] == ["First done."] and w.state.get("client")
    cur["snap"] = snap([text("assistant", "First done.", 30, "end_turn"), client_msg(45), tool(ts=46)])
    clock.t += 20
    w.poll_once()
    cur["snap"] = snap([client_msg(45), tool(ts=46), text("assistant", "Second done.", 70, "end_turn")])
    clock.t += 40
    w.poll_once()
    assert [p["summary"] for p in posts] == ["First done.", "Second done."] and "client" not in w.state


def test_mark_client_turn_never_waits_for_a_slow_poll(tmp_path):
    w, posts, cur, clock = make_watcher(tmp_path, None)
    started, release = threading.Event(), threading.Event()

    def slow(cmd, req=None):
        started.set()
        release.wait(5)
        return snap([])
    w.bridge.helper = slow
    t = threading.Thread(target=w.poll_once)
    t.start()
    started.wait(5)
    done = threading.Event()
    threading.Thread(target=lambda: (w.mark_client_turn(), done.set())).start()
    assert done.wait(1)                                              # not blocked by the poll's helper call
    release.set()
    t.join(5)


def test_reduce_entry_reads_queued_commands_as_user_messages():
    o = {"type": "attachment", "timestamp": T, "attachment": {"type": "queued_command", "prompt": PREFIX + "status?"}}
    e = helper.reduce_entry(o)
    assert e["role"] == "user" and e["blocks"][0]["relayed"] is True
    assert helper.reduce_entry({"type": "attachment", "attachment": {"type": "queued_command", "prompt": [{"type": "image"}]}}) is None
    assert helper.reduce_entry({"type": "attachment", "attachment": {"type": "environment"}}) is None


def test_helper_pads_trailing_semicolon(monkeypatch, tmp_path, capsys):
    keys = say(monkeypatch, tmp_path, ["idle", "idle"], "run a; b;")
    assert keys[0] == ("-l", "--", PREFIX + "run a; b; ")


def test_origin_requests_are_throttled_too(server, tmp_path):
    url, _ = server
    codes = [post(url, CALL, {"Origin": "https://evil.example"})[0] for _ in range(25)]
    assert codes[0] == 403 and 429 in codes
    assert len((tmp_path / "a.log").read_text().splitlines()) == 20


def test_helper_refuses_symlinked_project_dir(monkeypatch, tmp_path):
    import getpass
    import os
    (tmp_path / "web.env").write_text(f"RUN_AS={getpass.getuser()}\n")
    monkeypatch.setattr(helper, "FLEET_CONF_DIR", str(tmp_path))
    a = helper.Agent("web", {"agents_dir": "/opt/agents"})
    home = tmp_path / "home"
    proj = home / ".claude" / "projects"
    proj.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    (other / "t.jsonl").write_text("{}\n")
    a.home = str(home)
    (proj / "-opt-agents-web").symlink_to(other)
    assert a.transcript_file() is None
    (proj / "-opt-agents-web").unlink()
    (proj / "-opt-agents-web").mkdir()
    (proj / "-opt-agents-web" / "t.jsonl").write_text("{}\n")
    assert a.transcript_file() == os.path.join(os.path.realpath(home), ".claude", "projects", "-opt-agents-web", "t.jsonl")
