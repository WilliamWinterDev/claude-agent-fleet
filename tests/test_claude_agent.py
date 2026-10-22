"""Tests for bin/claude-agent and bin/agent-handoff that need no tmux and no claude: dry runs,
listing and the handoff inbox, against a throwaway config dir (CLAUDE_AGENT_CONF_DIR)."""

import getpass
import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


@pytest.fixture
def fleet(tmp_path):
    conf, agents = tmp_path / "conf", tmp_path / "agents"
    (agents / "handoffs" / "inbox").mkdir(parents=True)
    (agents / "web").mkdir()
    conf.mkdir()
    (conf / "fleet.conf").write_text(f'AGENTS_DIR={agents}\nOWNER="Will"\n')
    (conf / "web.env").write_text(f"RC_NAME=Web\nRUN_AS={getpass.getuser()}\nTASK_MODEL=sonnet\n")
    env = dict(os.environ, CLAUDE_AGENT_CONF_DIR=str(conf), TMUX_TMPDIR=str(tmp_path))
    env.pop("AGENT_NAME", None)
    return conf, agents, env


def run(env, *args, cwd=None, stdin=None):
    return subprocess.run(list(args), env=env, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=30)


def test_list_shows_stopped_agent(fleet):
    _, _, env = fleet
    r = run(env, BIN / "claude-agent", "list")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[:2] == ["web", "Web"] and "stopped" in r.stdout


def test_task_dry_run_uses_owner_and_agent_dir(fleet):
    _, agents, env = fleet
    r = run(env, BIN / "claude-agent", "task", "web", "--dry-run", "Fix the login page")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout and "RC name: Web · Fix the login page" in r.stdout
    assert "ask Will here" in r.stdout
    assert f"rules for this agent are in {agents}/web/CLAUDE.md" in r.stdout
    assert "worktree: none" in r.stdout


def test_fix_task_title_is_prefixed(fleet):
    _, _, env = fleet
    r = run(env, BIN / "claude-agent", "task", "web", "--fix", "--dry-run", "e2e failed")
    assert r.returncode == 0, r.stderr
    assert "Web · Fix: e2e failed" in r.stdout and "FIX session" in r.stdout


def test_guarded_commands_only_with_task_settings(fleet):
    conf, _, env = fleet
    r = run(env, BIN / "claude-agent", "task", "web", "--dry-run", "Fix the login page")
    assert r.returncode == 0, r.stderr
    assert "must not run" not in r.stdout
    (conf / "web.task-settings.json").write_text('{"permissions": {"deny": ["Bash(*e2e.sh*)"]}}\n')
    with open(conf / "web.env", "a") as f:
        f.write('GUARDED="the e2e suite"\n')
    r = run(env, BIN / "claude-agent", "task", "web", "--dry-run", "Fix the login page")
    assert "You must not run the e2e suite" in r.stdout
    assert "web.task-settings.json" in r.stdout
    r = run(env, BIN / "claude-agent", "task", "web", "--fix", "--dry-run", "e2e failed")
    assert "the only session allowed to run the e2e suite" in r.stdout
    assert "web.settings.json" not in r.stdout and "web.task-settings.json" not in r.stdout


def test_model_with_effort(fleet):
    _, _, env = fleet
    r = run(env, BIN / "claude-agent", "task", "web", "--model", "opus:high", "--dry-run", "x")
    assert r.returncode == 0, r.stderr
    assert "--model opus --effort high" in r.stdout


def test_unknown_agent_and_bad_name(fleet):
    _, _, env = fleet
    assert "no " in run(env, BIN / "claude-agent", "peek", "nope").stderr
    assert "bad agent name" in run(env, BIN / "claude-agent", "peek", "../x").stderr


def test_handoff_queues_file_from_agent_folder(fleet):
    _, agents, env = fleet
    (agents / "ops").mkdir()
    r = run(env, BIN / "agent-handoff", "web", "Deploy it", cwd=agents / "ops")
    assert r.returncode == 0, r.stderr
    [f] = list((agents / "handoffs" / "inbox").glob("*.md"))
    assert f.name.endswith("__ops__web__request.md")
    assert "From: ops\nTo: web\nKind: request" in f.read_text() and "Deploy it" in f.read_text()


def test_handoff_result_from_stdin_outside_agents_is_owner(fleet, tmp_path):
    _, agents, env = fleet
    r = run(env, BIN / "agent-handoff", "--result", "web", cwd=tmp_path, stdin="done\n")
    assert r.returncode == 0, r.stderr
    [f] = list((agents / "handoffs" / "inbox").glob("*.md"))
    assert f.name.endswith("__owner__web__result.md")


def test_handoff_rejects_unknown_agent(fleet):
    _, _, env = fleet
    r = run(env, BIN / "agent-handoff", "ghost", "hi")
    assert r.returncode == 1 and "unknown agent" in r.stderr
