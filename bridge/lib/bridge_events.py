"""claude-bridge webhook watcher (Claude -> client): quiet by design, one POST per state change.

Only work the client started goes back to the client. When send_message delivers a message the
bridge calls Watcher.mark_client_turn(): a "client turn" is pending for that session (persisted in
its state file). It becomes active once the relayed message shows up as a user entry in the
session's transcript (so an owner-started turn that is still running when the client's message is
queued behind it is not mistaken for the client's). While active:
  - needs_input: the turn asks a question (AskUserQuestion, a prompt on the terminal, or ends in a question)
  - pr_opened: a `gh pr create` result with a PR URL
  - done: the turn ends (the session is idle again) -> posted once, then the mark is cleared
Turns the owner starts from the Claude app send nothing. The one exception is `failed` (the
session's tmux session disappears after it was seen running): it always posts, so the client knows
the session it can message is gone, and clears any pending mark.
Dedupe: each event has a key (question hash, last assistant timestamp, PR URL, ...) kept in the state
file, so it is posted once however many polls see it and across restarts. No heartbeats. The first
poll after a start only records a baseline. While BRIDGE_WEBHOOK_URL / BRIDGE_WEBHOOK_KEY are unset or
REPLACE_ME nothing is posted. Failed POSTs are retried for 10 minutes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time

import bridge_core as core
import webhook

POLL = 20
RETRY_FOR = 600          # a failed POST is retried every poll for this many seconds
CLIENT_STALE = 6 * 3600  # a client mark whose message never showed up in the transcript is dropped after this


def _key(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


class Watcher:
    def __init__(self, bridge: core.Bridge, session: str, title: str, secrets_path: str, state_path: str,
                 post=None, now=time.time):
        self.bridge, self.session, self.title = bridge, session, title
        self.secrets_path, self.state_path, self.now = secrets_path, state_path, now
        self.post = post or (lambda body: webhook.post_json(self.secrets_path, body))
        self.pending: dict[str, tuple[dict, float]] = {}
        self.marks, self.marks_since = 0, None   # sends since the last poll (mark_client_turn, HTTP threads)
        self.state = self._load()
        self.baselined = False
        self.was_running = False
        self.lock = threading.RLock()

    def _retry_pending(self):
        for kind, (body, since) in list(self.pending.items()):
            if self.now() - since > RETRY_FOR or self.post(body).get("ok"):
                del self.pending[kind]

    def _load(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                d = json.load(fh)
                return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def mark_client_turn(self):
        """Called (on an HTTP thread) when send_message delivered a message. Only counts it, so it never
        waits behind a poll's helper calls or webhook POSTs; the poll turns it into a mark."""
        with self.lock:
            self.marks += 1
            self.marks_since = self.marks_since or self.now()

    def _take_marks(self):
        """Start a client turn for sends since the last poll. A send during an active client turn re-arms
        the mark after that turn's `done`, so the next turn reports too."""
        with self.lock:
            n, since, self.marks, self.marks_since = self.marks, self.marks_since, 0, None
        if not n:
            return
        g = self.state.get("client")
        if not g:
            self.state["client"] = {"since": since or self.now(), "user_ts": None}
        else:
            g["more"] = since or self.now()
        self._save()

    def _clear_client(self, rearm=True):
        g = self.state.pop("client", None)
        if rearm and g and g.get("more"):         # a later send is still waiting for its own turn
            self.state["client"] = {"since": g["more"], "user_ts": None}
        self._save()

    def _save(self):
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _body(self, payload: dict) -> dict:
        return {"source": "claude-bridge", "session": self.session, "title": self.title, **payload}

    def _emit(self, kind: str, key: str, payload: dict):
        """Post once per (kind, key). During the baseline poll just record."""
        if self.state.get(kind) == key:
            return
        self.state[kind] = key
        self._save()
        if not self.baselined:
            return
        body = self._body(payload)
        if not self.post(body).get("ok"):
            self.pending[kind] = (body, self.now())      # retried each poll for a while

    def _client_active(self, entries) -> dict | None:
        """The pending client turn once its relayed message is in the transcript, else None."""
        g = self.state.get("client")
        if not g:
            return None
        if self.now() - g["since"] > CLIENT_STALE:
            self._clear_client(rearm=False)
            return None
        if g.get("user_ts") is None:
            for e in entries:
                if e["role"] == "user" and core.parse_ts(e["ts"]) >= g["since"] - 2 and any(
                        b["t"] == "text" and b.get("relayed") for b in e["blocks"]):
                    g["user_ts"] = core.parse_ts(e["ts"])
                    self._save()
                    break
        return g if g.get("user_ts") is not None else None

    def poll_once(self):
        """Only this (watcher) thread touches self.state; HTTP threads just count sends (mark_client_turn)."""
        self._take_marks()
        self._retry_pending()
        req = {"session": self.session}
        snap = self.bridge.helper("transcript", req)
        st = core.compute_status(snap, self.now(), peek=lambda: self.bridge.helper("peek", req).get("screen", ""))
        entries = snap.get("entries") or []
        status = st["status"]

        if status == "stopped":
            if self.was_running:
                self._emit("failed", _key("failed", int(self.now() // 60)), {
                    "status": "failed", "summary": "The session is no longer running."})
                self._clear_client(rearm=False)
            self.was_running = False
            self.baselined = True
            return
        self.was_running = True

        last = entries[-1] if entries else None
        last_asst = next((e for e in reversed(entries) if e["role"] == "assistant"), None)
        asst_key = last_asst["ts"] if last_asst else ""
        turn_ended = bool(last and last["role"] == "assistant"
                          and not any(b["t"] == "tool_use" for b in last["blocks"]))
        client = self._client_active(entries)

        if client and status == "needs_input":
            self._emit("needs_input", _key(asst_key, st["question"]), {
                "status": "needs_input", "summary": st["summary"], "question": st["question"]})
            if turn_ended:               # the turn is over (it ended on a question); the client's reply starts a new one
                self._clear_client()
        elif client and status == "idle" and turn_ended and core.parse_ts(last["ts"]) > client["user_ts"]:
            self._emit("done", _key("done", asst_key), {"status": "done", "summary": st["summary"]})
            self._clear_client()

        for url in core.find_pr_urls(entries):
            seen = self.state.setdefault("prs", [])
            if url not in seen:
                if self.baselined and client:
                    body = self._body({"status": "pr_opened", "summary": st["summary"], "pr_url": url})
                    if not self.post(body).get("ok"):
                        self.pending["pr:" + url] = (body, self.now())
                seen.append(url)         # recorded even when not posted, so it never replays later
                del seen[:-50]
                self._save()
        self.baselined = True


def run_forever(bridge: core.Bridge, secrets_path: str, state_dir: str):
    watchers: dict[str, Watcher] = {}
    lock = threading.Lock()

    def watcher(sid, name=""):
        with lock:
            if sid not in watchers and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", sid):
                watchers[sid] = Watcher(bridge, sid, core.clean(name or sid, 60),
                                        secrets_path, os.path.join(state_dir, f"events-{sid}.json"))
            return watchers.get(sid)

    def delivered(sid):
        w = watcher(sid)
        if w:
            w.mark_client_turn()

    bridge.on_delivered = delivered
    while True:
        try:
            for s in bridge.helper("sessions").get("sessions") or []:
                if isinstance(s.get("id"), str):
                    watcher(s["id"], str(s.get("name") or ""))
        except Exception as exc:
            print(f"claude-bridge watcher: {type(exc).__name__}", file=sys.stderr)
        with lock:
            current = list(watchers.values())
        for w in current:
            try:
                w.poll_once()
            except Exception as exc:     # a bad poll must never kill the thread
                print(f"claude-bridge watcher: {type(exc).__name__}", file=sys.stderr)
        time.sleep(POLL)
