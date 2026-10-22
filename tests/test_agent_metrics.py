"""Unit tests for bin/agent-metrics against a fake cgroup tree, swap files and event logs."""

import importlib.machinery
import importlib.util
from pathlib import Path

_p = Path(__file__).resolve().parent.parent / "bin" / "agent-metrics"
_l = importlib.machinery.SourceFileLoader("agent_metrics", str(_p))
am = importlib.util.module_from_spec(importlib.util.spec_from_loader("agent_metrics", _l))
_l.exec_module(am)

NOW = 1_000_000_000  # an arbitrary fixed clock; all fixture times are relative to it


def cfg(tmp_path, **kw):
    c = dict(am.CFG, cgroup_root=str(tmp_path / "cg"), proc_swaps=str(tmp_path / "swaps"),
             zram_mm_stat=str(tmp_path / "mm_stat"), agent_events=str(tmp_path / "events.log"),
             job_events=str(tmp_path / "jobs.log"), conf_dir=str(tmp_path / "conf"),
             tmux_sockets=str(tmp_path / "tmux" / "claude-*-t-*"))
    c.update(kw)
    return c


def cgroup(root, rel, current, high="max", mx="max", events="high 0\nmax 0\noom_kill 0\n"):
    d = root / rel
    d.mkdir(parents=True)
    (d / "memory.current").write_text(f"{current}\n")
    (d / "memory.high").write_text(f"{high}\n")
    (d / "memory.max").write_text(f"{mx}\n")
    (d / "memory.events").write_text(events)


def lines(text, name):
    return [ln for ln in text.splitlines() if ln.startswith(name + "{") or ln.startswith(name + " ")]


def test_load_config_file_then_env(tmp_path):
    f = tmp_path / "agent-metrics.conf"
    f.write_text('# comment\nJOB_EVENTS="/x/jobs.log"\nEXTRA_CGROUPS=a=b\nUNKNOWN=1\n')
    c = am.load_config(str(f), {"EXTRA_CGROUPS": "c=d"})
    assert c["job_events"] == "/x/jobs.log" and c["extra_cgroups"] == "c=d" and "unknown" not in c


def test_empty_host_exports_only_health(tmp_path):
    text = am.collect(cfg(tmp_path), now=NOW)
    for s in ("cgroups", "swap", "sessions", "queues"):
        assert f'agent_metrics_section_ok{{section="{s}"}} 1' in text
    assert f"agent_metrics_last_run_timestamp_seconds {NOW}" in text
    assert not lines(text, "agent_cgroup_memory_current_bytes")


def test_cgroups_agents_and_extra(tmp_path):
    root = tmp_path / "cg"
    cgroup(root, "system.slice/claude-agent@web.service", 100, high=800, mx=1000,
           events="high 3\nmax 1\noom_kill 0\n")
    cgroup(root, "user.slice/user-1500.slice", 50)
    text = am.collect(cfg(tmp_path, extra_cgroups="web-tasks=user.slice/user-1500.slice bad name=x"), now=NOW)
    assert 'agent_cgroup_memory_current_bytes{cgroup="web"} 100' in text
    assert 'agent_cgroup_memory_max_bytes{cgroup="web"} 1000' in text
    assert 'agent_cgroup_memory_current_bytes{cgroup="web-tasks"} 50' in text
    # "max" means no limit: no series
    assert 'agent_cgroup_memory_max_bytes{cgroup="web-tasks"}' not in text
    assert 'agent_cgroup_memory_events_total{cgroup="web",event="high"} 3' in text


def test_swap_and_zram(tmp_path):
    (tmp_path / "swaps").write_text(
        "Filename Type Size Used Priority\n/dev/zram0 partition 1024 512 100\n/swapfile file 2048 0 -2\n")
    (tmp_path / "mm_stat").write_text("4000 1000 1200 0 0 0 0 0 0\n")
    text = am.collect(cfg(tmp_path), now=NOW)
    assert 'agent_swap_size_bytes{device="zram"} 1048576' in text
    assert 'agent_swap_used_bytes{device="swapfile"} 0' in text
    assert "agent_zram_compression_ratio 4" in text


def test_sessions_from_event_log_and_live_sockets(tmp_path, monkeypatch):
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "web.env").write_text("RC_NAME=Web\n")
    (tmp_path / "events.log").write_text("".join(f"{NOW + t}\t{e}\tweb\t{tid}\t{k}\ttitle\n" for t, e, tid, k in [
        (-7200, "task_start", "aaaaaa", "task"),
        (-3600, "stop", "aaaaaa", "task"),
        (-600, "fix_start", "bbbbbb", "fix"),
        (-300, "resume", "cccccc", "task"),
    ]) + "garbage line\n")
    sock = tmp_path / "tmux" / "claude-web-t-bbbbbb"
    sock.parent.mkdir()
    sock.touch()
    monkeypatch.setattr(am, "run_tmux", lambda s: f"web-t-bbbbbb|{NOW - 600}|1|Fix: tests\n")
    text = am.collect(cfg(tmp_path), now=NOW)
    assert 'agent_tasks_started_total{agent="web",kind="task"} 1' in text
    assert 'agent_tasks_started_total{agent="web",kind="fix"} 1' in text
    assert 'agent_tasks_running{agent="web",kind="fix"} 1' in text
    assert 'agent_tasks_running{agent="web",kind="task"} 0' in text
    assert 'agent_task_oldest_age_seconds{agent="web",kind="fix"} 600' in text
    assert 'agent_tasks_resumed_total{agent="web"} 1' in text
    assert 'agent_task_lifetime_seconds_sum{agent="web",kind="task"} 3600' in text


def test_queues_from_job_log(tmp_path):
    rows = [(-5000, 300, 10, "build", "lib", 0), (-4000, 500, 0, "build", "app", 1),
            (-2 * am.DAY, 900, 60, "e2e", "all", 0), (-100, 50, 5, "Bad Queue", "x", 0)]
    (tmp_path / "jobs.log").write_text("".join(
        f"{NOW + end - d}\t{NOW + end}\t{d}\t{w}\t{q}\t{lab}\t{rc}\n" for end, d, w, q, lab, rc in rows))
    text = am.collect(cfg(tmp_path), now=NOW)
    assert 'agent_queue_runs_total{queue="build",result="ok"} 1' in text
    assert 'agent_queue_runs_total{queue="build",result="fail"} 1' in text
    assert 'agent_queue_runs_total{queue="e2e",result="fail"} 0' in text
    assert 'agent_queue_run_duration_seconds_sum{queue="build"} 800' in text
    assert 'agent_queue_run_duration_seconds_avg_24h{queue="build"} 400' in text
    # the e2e run is older than 24h: totals but no 24h gauges
    assert 'agent_queue_run_duration_seconds_count{queue="e2e"} 1' in text
    assert 'agent_queue_run_duration_seconds_avg_24h{queue="e2e"}' not in text
    assert "Bad Queue" not in text


def test_broken_section_does_not_stop_others(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "swap", lambda out, c: 1 / 0)
    text = am.collect(cfg(tmp_path), now=NOW)
    assert 'agent_metrics_section_ok{section="swap"} 0' in text
    assert 'agent_metrics_section_ok{section="sessions"} 1' in text


def test_write_is_atomic_file(tmp_path):
    c = cfg(tmp_path, textfile_dir=str(tmp_path / "tf"))
    am.write(c, "agent_metrics", "x 1\n")
    assert (tmp_path / "tf" / "agent_metrics.prom").read_text() == "x 1\n"
    assert not list((tmp_path / "tf").glob("*.tmp"))
